import type { ChatMessage, Timeline, ToolCall, TurnMeta, WebEvent } from '../types';

/**
 * Live turn state machine for the browser transcript.
 *
 * The backend renders each model step as its own assistant message, but the
 * event stream only carries deltas. This module keeps one assistant message
 * per step and never lets a late `final` overwrite a previous turn, which is
 * what made multi-turn streaming jump around before.
 */

const LOCAL_PREFIXES = ['pending-', 'step-', 'stream-', 'notice-'] as const;

let localSeq = 0;

function localId(prefix: string): string {
  localSeq += 1;
  return `${prefix}-${Date.now().toString(36)}-${localSeq}`;
}

function isLocalId(id: string | number): boolean {
  const value = String(id);
  return LOCAL_PREFIXES.some((prefix) => value.startsWith(prefix));
}

/** Tool rows are rendered through their parent assistant message. */
export function isVisibleMessage(message: ChatMessage): boolean {
  return message.role !== 'tool' && !message.tool_call_id;
}

export interface LiveTurnContext {
  userMessageId: string | null;
  stepMessageId: string | null;
  turn: number | null;
  step: number | null;
  busy: boolean;
}

export function createLiveContext(): LiveTurnContext {
  return { userMessageId: null, stepMessageId: null, turn: null, step: null, busy: false };
}

export function resetLiveContext(context: LiveTurnContext): void {
  context.userMessageId = null;
  context.stepMessageId = null;
  context.turn = null;
  context.step = null;
  context.busy = false;
}

function blankMessage(role: ChatMessage['role'], text = ''): ChatMessage {
  return {
    id: localId(role === 'user' ? 'pending' : role === 'notice' ? 'notice' : 'step'),
    role,
    created_at: Date.now(),
    text,
    reasoning: '',
    source: { kind: 'browser' },
    tool_call_id: null,
    is_error: false,
    tool_calls: [],
    images: [],
    has_text: text.length > 0,
    streaming: role === 'assistant',
    turn_meta: role === 'user' ? { started_at: Date.now(), status: 'running' } : undefined,
  };
}

export function newNotice(text: string, kind: 'ok' | 'error' = 'ok'): ChatMessage {
  const message = blankMessage('notice', text);
  message.is_error = kind === 'error';
  message.streaming = false;
  return message;
}

function replaceAt<T>(items: T[], index: number, value: T): T[] {
  const next = items.slice();
  next[index] = value;
  return next;
}

function settleUserTurn(messages: ChatMessage[], context: LiveTurnContext, status: TurnMeta['status']): ChatMessage[] {
  const id = context.userMessageId;
  if (id === null) return messages;
  let changed = false;
  const next = messages.map((message) => {
    if (String(message.id) !== id) return message;
    changed = true;
    const base = message.turn_meta ?? { started_at: message.created_at || Date.now() };
    return {
      ...message,
      turn_meta: { ...base, ended_at: Date.now(), status },
    };
  });
  return changed ? next : messages;
}

function clearStreaming(messages: ChatMessage[]): ChatMessage[] {
  return messages.some((message) => message.streaming)
    ? messages.map((message) => (message.streaming ? { ...message, streaming: false } : message))
    : messages;
}

/**
 * When a turn start was missed (reload, reconnect), bind the newest persisted
 * user message so its turn_meta still tracks the running turn.
 */
function ensureUserAnchor(messages: ChatMessage[], context: LiveTurnContext): ChatMessage[] {
  if (context.userMessageId !== null) return messages;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== 'user') continue;
    // Never reopen a turn that already finished. When a run starts from outside
    // the composer there is no optimistic anchor yet, and binding the previous
    // user message would show the old turn as running until the next refresh.
    if (message.turn_meta && message.turn_meta.status !== 'running') return messages;
    context.userMessageId = String(message.id);
    if (message.turn_meta) return messages;
    const turn_meta: TurnMeta = { started_at: message.created_at || Date.now(), status: 'running' };
    return replaceAt(messages, index, { ...message, turn_meta });
  }
  return messages;
}

function ensureAssistant(
  timeline: Timeline,
  context: LiveTurnContext,
): { messages: ChatMessage[]; index: number; created: boolean } {
  let messages = ensureUserAnchor(timeline.messages, context);
  if (context.stepMessageId !== null) {
    const existing = messages.findIndex((message) => String(message.id) === context.stepMessageId);
    if (existing >= 0) return { messages, index: existing, created: false };
  }
  const message = { ...blankMessage('assistant'), detached: context.userMessageId === null };
  context.stepMessageId = String(message.id);
  messages = [...messages, message];
  context.busy = true;
  return { messages, index: messages.length - 1, created: true };
}

function startNewRun(messages: ChatMessage[], context: LiveTurnContext): ChatMessage[] {
  // A new run started without a final event: settle the previous turn and
  // forget its anchors so the next deltas cannot leak into it.
  const settled = settleUserTurn(messages, context, 'settled');
  context.userMessageId = null;
  context.stepMessageId = null;
  context.turn = null;
  context.step = null;
  return clearStreaming(settled);
}

function startStep(timeline: Timeline, event: Extract<WebEvent, { type: 'step' }>, context: LiveTurnContext): Timeline {
  let messages = timeline.messages;
  const boundary = context.turn !== null && context.step !== null && event.step === 1;
  if (boundary) messages = startNewRun(messages, context);
  messages = ensureUserAnchor(messages, context);
  context.turn = event.turn;
  context.step = event.step;
  context.stepMessageId = null;
  context.busy = true;
  const message = {
    ...blankMessage('assistant'),
    turn: event.turn,
    detached: context.userMessageId === null,
  };
  context.stepMessageId = String(message.id);
  return { ...timeline, busy: true, messages: [...messages, message] };
}

function applyStatus(
  timeline: Timeline,
  event: Extract<WebEvent, { type: 'status' }>,
  context: LiveTurnContext,
): Timeline {
  if (event.phase === 'thinking') {
    let messages = timeline.messages;
    if (!context.busy && (context.userMessageId !== null || context.turn !== null)) {
      messages = startNewRun(messages, context);
    }
    context.busy = true;
    return { ...timeline, busy: true, messages };
  }
  const status: TurnMeta['status'] = event.phase === 'interrupted' ? 'stopped' : 'settled';
  const messages = clearStreaming(settleUserTurn(timeline.messages, context, status));
  resetLiveContext(context);
  return { ...timeline, busy: false, messages };
}

function applyToolResult(timeline: Timeline, event: Extract<WebEvent, { type: 'tool_result' }>): Timeline {
  for (let index = timeline.messages.length - 1; index >= 0; index -= 1) {
    const message = timeline.messages[index];
    const callIndex = message.tool_calls.findIndex((call) => call.id === event.call_id);
    if (callIndex < 0) continue;
    const call = message.tool_calls[callIndex];
    const updated: ToolCall = { ...call, result: event.text, is_error: event.is_error, pending: false };
    const nextMessage = { ...message, tool_calls: replaceAt(message.tool_calls, callIndex, updated) };
    return { ...timeline, messages: replaceAt(timeline.messages, index, nextMessage) };
  }
  return timeline;
}

export function applyLiveEvent(timeline: Timeline, event: WebEvent, context: LiveTurnContext): Timeline {
  if (event.session_id !== timeline.session_id) return timeline;
  switch (event.type) {
    case 'status':
      return applyStatus(timeline, event, context);
    case 'step':
      if (context.turn !== null && event.turn === context.turn && context.step !== null && event.step <= context.step) {
        return timeline;
      }
      return startStep(timeline, event, context);
    case 'text': {
      const { messages, index } = ensureAssistant(timeline, context);
      const message = messages[index];
      const next = { ...message, text: message.text + event.text, has_text: true, streaming: true };
      return { ...timeline, busy: true, messages: replaceAt(messages, index, next) };
    }
    case 'thinking': {
      const { messages, index } = ensureAssistant(timeline, context);
      const message = messages[index];
      const next = { ...message, reasoning: message.reasoning + event.text, streaming: true };
      return { ...timeline, busy: true, messages: replaceAt(messages, index, next) };
    }
    case 'tool_call': {
      const { messages, index } = ensureAssistant(timeline, context);
      const message = messages[index];
      if (message.tool_calls.some((call) => call.id === event.call_id)) return timeline;
      const call: ToolCall = {
        id: event.call_id,
        name: event.name,
        arguments: event.arguments,
        result: null,
        is_error: false,
        pending: true,
      };
      return {
        ...timeline,
        busy: true,
        messages: replaceAt(messages, index, { ...message, streaming: true, tool_calls: [...message.tool_calls, call] }),
      };
    }
    case 'tool_result':
      return applyToolResult(timeline, event);
    case 'final': {
      const { messages, index } = ensureAssistant(timeline, context);
      const message = messages[index];
      const text = event.text || message.text;
      const settled = settleUserTurn(
        replaceAt(messages, index, { ...message, text, has_text: text.length > 0, streaming: false }),
        context,
        'settled',
      );
      context.stepMessageId = null;
      context.busy = false;
      return { ...timeline, busy: false, messages: settled };
    }
    case 'error': {
      const messages = clearStreaming(settleUserTurn(timeline.messages, context, 'failed'));
      resetLiveContext(context);
      return { ...timeline, busy: false, messages };
    }
    case 'usage':
      return {
        ...timeline,
        usage: {
          input_tokens: event.input_tokens,
          output_tokens: event.output_tokens,
          cached_input_tokens: event.cached_input_tokens,
        },
      };
    default:
      return timeline;
  }
}

export function beginUserTurn(timeline: Timeline, text: string, context: LiveTurnContext): Timeline {
  resetLiveContext(context);
  const settledMessages = timeline.messages.some((message) => message.turn_meta?.status === 'running')
    ? timeline.messages.map((message) =>
        message.turn_meta?.status === 'running'
          ? { ...message, turn_meta: { ...message.turn_meta, ended_at: Date.now(), status: 'settled' as const } }
          : message,
      )
    : timeline.messages;
  const message = blankMessage('user', text);
  context.userMessageId = String(message.id);
  context.busy = true;
  return { ...timeline, busy: true, messages: [...clearStreaming(settledMessages), message] };
}

export function dropUserTurn(timeline: Timeline, context: LiveTurnContext): Timeline {
  const id = context.userMessageId;
  const messages = id === null ? timeline.messages : timeline.messages.filter((message) => String(message.id) !== id);
  resetLiveContext(context);
  return { ...timeline, busy: false, messages };
}

function mergeToolCalls(before: ToolCall[], after: ToolCall[]): ToolCall[] {
  if (after.length === 0) return before;
  if (before.length === 0) return after;
  const previous = new Map(before.map((call) => [call.id, call]));
  return after.map((call) => {
    const earlier = previous.get(call.id);
    if (!earlier) return call;
    const result = call.result ?? earlier.result;
    return {
      ...call,
      result,
      is_error: call.is_error || earlier.is_error,
      pending: result === null ? call.pending || earlier.pending : false,
    };
  });
}

function mergeTurnMeta(before?: TurnMeta, after?: TurnMeta, busy = false): TurnMeta | undefined {
  const base = before ?? after;
  if (!base) return undefined;
  const merged: TurnMeta = after ? { ...base, ...after, started_at: Math.min(base.started_at, after.started_at) } : { ...base };
  if (!busy && merged.status === 'running') return { ...merged, ended_at: merged.ended_at ?? Date.now(), status: 'settled' };
  return merged;
}

function mergeMessage(before: ChatMessage, after: ChatMessage, busy: boolean): ChatMessage {
  const text = after.text || before.text;
  const reasoning = after.reasoning || before.reasoning;
  return {
    ...after,
    id: before.id,
    created_at: after.created_at || before.created_at,
    text,
    reasoning,
    has_text: after.has_text || before.has_text || text.length > 0,
    tool_calls: mergeToolCalls(before.tool_calls, after.tool_calls),
    images: after.images.length > 0 ? after.images : before.images,
    is_error: after.is_error || before.is_error,
    streaming: busy ? Boolean(before.streaming || after.streaming) : false,
    turn_meta: mergeTurnMeta(before.turn_meta, after.turn_meta, busy),
    turn: before.turn ?? after.turn,
    detached: before.detached ?? after.detached,
    source: after.source ?? before.source,
  };
}

/**
 * Merge a canonical timeline into the browser view without replacing node
 * identities. Keeping local ids and turn_meta is what lets the live step keep
 * growing after a background refresh finishes.
 */
export function reconcileTimeline(previous: Timeline, incoming: Timeline): Timeline {
  if (previous.session_id !== incoming.session_id) return incoming;
  const busy = incoming.busy;
  const merged: ChatMessage[] = [];
  let cursor = 0;
  for (const after of incoming.messages) {
    let matched = -1;
    for (let index = cursor; index < previous.messages.length; index += 1) {
      const before = previous.messages[index];
      if (before.role === after.role && isVisibleMessage(before) === isVisibleMessage(after)) {
        matched = index;
        break;
      }
      if (isLocalId(before.id)) break;
    }
    if (matched >= 0) {
      merged.push(mergeMessage(previous.messages[matched], after, busy));
      cursor = matched + 1;
    } else {
      merged.push(after);
    }
  }
  for (let index = cursor; index < previous.messages.length; index += 1) {
    const leftover = previous.messages[index];
    if (isLocalId(leftover.id)) merged.push(leftover);
  }
  return { ...incoming, messages: merged };
}
