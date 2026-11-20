import { describe, expect, it } from 'vitest';
import type { ChatMessage, Timeline, WebEvent } from '../types';
import {
  applyLiveEvent,
  beginUserTurn,
  createLiveContext,
  dropUserTurn,
  newNotice,
  reconcileTimeline,
} from './liveTurn';

const SESSION = 'session-test';

function makeMessage(partial: Partial<ChatMessage> & { role: string }): ChatMessage {
  return {
    id: partial.id ?? Math.random().toString(36).slice(2),
    created_at: partial.created_at ?? 1_000,
    text: partial.text ?? '',
    reasoning: '',
    source: { kind: 'runtime' },
    tool_call_id: null,
    is_error: false,
    tool_calls: [],
    images: [],
    has_text: false,
    ...partial,
  };
}

function makeTimeline(messages: ChatMessage[] = [], busy = false): Timeline {
  return {
    session_id: SESSION,
    title: 'Test',
    cwd: 'D:/test',
    active: true,
    busy,
    pending: 0,
    readable: true,
    archived: false,
    provider: 'deepseek',
    model: 'deepseek-flash',
    preset: 'standard',
    permission_preset: 'ask',
    context_tokens: 0,
    context_window: 65_536,
    usage: { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0 },
    messages,
  };
}

function ev(payload: Record<string, unknown>): WebEvent {
  return { session_id: SESSION, ...payload } as WebEvent;
}

function runTextTurn(view: Timeline, ctx: ReturnType<typeof createLiveContext>, text: string): Timeline {
  view = beginUserTurn(view, text, ctx);
  view = applyLiveEvent(view, ev({ type: 'status', phase: 'thinking' }), ctx);
  view = applyLiveEvent(view, ev({ type: 'step', turn: 1, step: 1 }), ctx);
  view = applyLiveEvent(view, ev({ type: 'text', text }), ctx);
  view = applyLiveEvent(view, ev({ type: 'final', text }), ctx);
  return applyLiveEvent(view, ev({ type: 'status', phase: 'done' }), ctx);
}

describe('live turn state machine', () => {
  it('keeps each user turn in its own assistant message', () => {
    const ctx = createLiveContext();
    let view = makeTimeline();
    view = runTextTurn(view, ctx, 'First answer');
    view = runTextTurn(view, ctx, 'Second answer');

    const assistants = view.messages.filter((message) => message.role === 'assistant');
    expect(view.messages.map((message) => message.role)).toEqual(['user', 'assistant', 'user', 'assistant']);
    expect(assistants.map((message) => message.text)).toEqual(['First answer', 'Second answer']);
    expect(view.messages[0].turn_meta?.status).toBe('settled');
    expect(view.messages[2].turn_meta?.status).toBe('settled');
    expect(ctx.busy).toBe(false);
    expect(view.busy).toBe(false);
  });

  it('aggregates a tool step into one assistant message', () => {
    const ctx = createLiveContext();
    let view = makeTimeline();
    view = beginUserTurn(view, 'run a command', ctx);
    view = applyLiveEvent(view, ev({ type: 'status', phase: 'thinking' }), ctx);
    view = applyLiveEvent(view, ev({ type: 'step', turn: 1, step: 1 }), ctx);
    view = applyLiveEvent(view, ev({ type: 'thinking', text: 'Need a shell.' }), ctx);
    view = applyLiveEvent(view, ev({ type: 'tool_call', call_id: 'call_1', name: 'pwsh', arguments: '{"command":"echo hi"}' }), ctx);
    view = applyLiveEvent(view, ev({ type: 'tool_result', call_id: 'call_1', is_error: false, text: 'hi' }), ctx);
    view = applyLiveEvent(view, ev({ type: 'step', turn: 1, step: 2 }), ctx);
    view = applyLiveEvent(view, ev({ type: 'text', text: 'The command printed hi.' }), ctx);
    view = applyLiveEvent(view, ev({ type: 'final', text: 'The command printed hi.' }), ctx);
    view = applyLiveEvent(view, ev({ type: 'status', phase: 'done' }), ctx);

    const assistants = view.messages.filter((message) => message.role === 'assistant');
    expect(assistants).toHaveLength(2);
    expect(assistants[0].tool_calls).toHaveLength(1);
    expect(assistants[0].tool_calls[0].result).toBe('hi');
    expect(assistants[0].tool_calls[0].pending).toBe(false);
    expect(assistants[0].reasoning).toBe('Need a shell.');
    expect(assistants[1].text).toBe('The command printed hi.');
  });

  it('never overwrites an earlier answer when a later turn finishes', () => {
    const ctx = createLiveContext();
    let view = makeTimeline([makeMessage({ id: 'user-0', role: 'user', text: 'old question' }), makeMessage({ id: 'assistant-0', role: 'assistant', text: 'old answer' })]);
    view = runTextTurn(view, ctx, 'new answer');
    expect(view.messages[1].text).toBe('old answer');
    expect(view.messages[3].text).toBe('new answer');
  });

  it('ignores replayed step events inside the same step', () => {
    const ctx = createLiveContext();
    let view = makeTimeline();
    view = beginUserTurn(view, 'q', ctx);
    view = applyLiveEvent(view, ev({ type: 'step', turn: 1, step: 1 }), ctx);
    view = applyLiveEvent(view, ev({ type: 'text', text: 'partial' }), ctx);
    view = applyLiveEvent(view, ev({ type: 'step', turn: 1, step: 1 }), ctx);
    expect(view.messages.filter((message) => message.role === 'assistant')).toHaveLength(1);
    expect(view.messages[1].text).toBe('partial');
  });

  it('marks a failed turn as failed and unlocks the composer', () => {
    const ctx = createLiveContext();
    let view = makeTimeline();
    view = beginUserTurn(view, 'q', ctx);
    view = applyLiveEvent(view, ev({ type: 'step', turn: 1, step: 1 }), ctx);
    view = applyLiveEvent(view, ev({ type: 'text', text: 'half' }), ctx);
    view = applyLiveEvent(view, ev({ type: 'error', message: 'boom' }), ctx);
    expect(view.busy).toBe(false);
    expect(view.messages[0].turn_meta?.status).toBe('failed');
    expect(view.messages[1].streaming).toBe(false);
  });

  it('stops the running turn on interrupt', () => {
    const ctx = createLiveContext();
    let view = makeTimeline();
    view = beginUserTurn(view, 'q', ctx);
    view = applyLiveEvent(view, ev({ type: 'step', turn: 1, step: 1 }), ctx);
    view = applyLiveEvent(view, ev({ type: 'final', text: 'partial' }), ctx);
    view = applyLiveEvent(view, ev({ type: 'status', phase: 'interrupted' }), ctx);
    expect(view.messages[0].turn_meta?.status).toBe('stopped');
    expect(view.busy).toBe(false);
  });

  it('does not reopen a settled turn when a run starts without an optimistic message', () => {
    const ctx = createLiveContext();
    let view = makeTimeline([
      makeMessage({
        id: 'user-0',
        role: 'user',
        text: 'old question',
        turn_meta: { started_at: 1_000, ended_at: 2_000, status: 'settled' },
      }),
      makeMessage({ id: 'assistant-0', role: 'assistant', text: 'old answer' }),
    ]);
    view = applyLiveEvent(view, ev({ type: 'status', phase: 'thinking' }), ctx);
    view = applyLiveEvent(view, ev({ type: 'step', turn: 2, step: 1 }), ctx);
    view = applyLiveEvent(view, ev({ type: 'text', text: 'external answer' }), ctx);

    expect(ctx.userMessageId).toBeNull();
    expect(view.messages[0].turn_meta?.status).toBe('settled');
    expect(view.messages).toHaveLength(3);
    expect(view.messages[2].role).toBe('assistant');
    expect(view.messages[2].text).toBe('external answer');
    expect(view.messages[2].turn).toBe(2);
  });

  it('drops the optimistic message when a send fails', () => {
    const ctx = createLiveContext();
    let view = makeTimeline([makeMessage({ id: 'assistant-0', role: 'assistant', text: 'hi' })]);
    view = beginUserTurn(view, 'doomed', ctx);
    expect(view.messages).toHaveLength(2);
    view = dropUserTurn(view, ctx);
    expect(view.messages).toHaveLength(1);
    expect(ctx.userMessageId).toBeNull();
  });
});

describe('timeline reconciliation', () => {
  it('keeps optimistic ids and turn metadata while absorbing canonical history', () => {
    const ctx = createLiveContext();
    let view = makeTimeline();
    view = beginUserTurn(view, 'hello', ctx);
    view = applyLiveEvent(view, ev({ type: 'step', turn: 1, step: 1 }), ctx);
    view = applyLiveEvent(view, ev({ type: 'text', text: 'partial answer' }), ctx);

    const localUserId = view.messages[0].id;
    const localStepId = view.messages[1].id;
    const canonical = makeTimeline(
      [
        makeMessage({ id: 'm-user', role: 'user', text: 'hello' }),
        makeMessage({ id: 'm-assistant', role: 'assistant', text: 'partial answer' }),
      ],
      true,
    );
    const merged = reconcileTimeline(view, canonical);
    expect(merged.messages[0].id).toBe(localUserId);
    expect(merged.messages[1].id).toBe(localStepId);
    expect(merged.messages[1].text).toBe('partial answer');
    expect(merged.messages[0].turn_meta?.status).toBe('running');
    expect(merged.messages[1].streaming).toBe(true);
  });

  it('settles a running turn when the refreshed timeline is idle', () => {
    const ctx = createLiveContext();
    let view = makeTimeline();
    view = beginUserTurn(view, 'hello', ctx);
    view = applyLiveEvent(view, ev({ type: 'step', turn: 1, step: 1 }), ctx);
    view = applyLiveEvent(view, ev({ type: 'text', text: 'done now' }), ctx);
    const canonical = makeTimeline(
      [
        makeMessage({ id: 'm-user', role: 'user', text: 'hello' }),
        makeMessage({ id: 'm-assistant', role: 'assistant', text: 'done now' }),
      ],
      false,
    );
    const merged = reconcileTimeline(view, canonical);
    expect(merged.messages[0].turn_meta?.status).toBe('settled');
    expect(merged.messages[1].streaming).toBe(false);
  });

  it('appends canonical messages that are not in the browser view', () => {
    const view = makeTimeline([makeMessage({ id: 'old-user', role: 'user', text: 'hi' })]);
    const canonical = makeTimeline(
      [
        makeMessage({ id: 'old-user', role: 'user', text: 'hi' }),
        makeMessage({ id: 'new-assistant', role: 'assistant', text: 'welcome' }),
      ],
      false,
    );
    const merged = reconcileTimeline(view, canonical);
    expect(merged.messages.map((message) => String(message.id))).toEqual(['old-user', 'new-assistant']);
  });

  it('preserves a local pending message the server has not stored yet', () => {
    const view = makeTimeline([
      makeMessage({ id: 'user-0', role: 'user', text: 'hi' }),
      makeMessage({ id: 'pending-123', role: 'user', text: 'not stored yet' }),
    ]);
    const canonical = makeTimeline([makeMessage({ id: 'user-0', role: 'user', text: 'hi' })], false);
    const merged = reconcileTimeline(view, canonical);
    expect(merged.messages.map((message) => String(message.id))).toEqual(['user-0', 'pending-123']);
  });

  it('places the canonical user message before an assistant that started first', () => {
    const view = makeTimeline(
      [
        makeMessage({
          id: 'user-0',
          role: 'user',
          text: 'old question',
          turn_meta: { started_at: 1_000, ended_at: 2_000, status: 'settled' },
        }),
        makeMessage({ id: 'assistant-0', role: 'assistant', text: 'old answer' }),
        makeMessage({ id: 'step-local', role: 'assistant', text: 'external answer' }),
      ],
      true,
    );
    const canonical = makeTimeline(
      [
        makeMessage({ id: 'user-0', role: 'user', text: 'old question' }),
        makeMessage({ id: 'assistant-0', role: 'assistant', text: 'old answer' }),
        makeMessage({ id: 'm-user-1', role: 'user', text: 'external question' }),
        makeMessage({ id: 'step-remote', role: 'assistant', text: 'external answer' }),
      ],
      true,
    );
    const merged = reconcileTimeline(view, canonical);
    expect(merged.messages.map((message) => String(message.id))).toEqual(['user-0', 'assistant-0', 'm-user-1', 'step-local']);
    expect(merged.messages[3].text).toBe('external answer');
  });

  it('ignores other sessions', () => {
    const view = makeTimeline();
    const other = { ...makeTimeline(), session_id: 'other' };
    expect(reconcileTimeline(view, other)).toBe(other);
  });

  it('creates notices with error styling', () => {
    expect(newNotice('nope', 'error').is_error).toBe(true);
    expect(newNotice('fine').is_error).toBe(false);
  });
});
