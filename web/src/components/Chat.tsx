import { memo, useEffect, useState } from 'react';
import type { ChatMessage, Timeline } from '../types';
import { basename, formatElapsed } from '../lib/format';
import { isVisibleMessage } from '../lib/liveTurn';
import { BrandMark, Icon } from './Icon';
import { Markdown } from './Markdown';
import { ToolCard } from './ToolCard';

function CopyAction({ value, label = 'Copy' }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="message-action"
      type="button"
      aria-label={copied ? 'Copied' : label}
      title={label}
      onClick={() => {
        void navigator.clipboard
          ?.writeText(value)
          .then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1200);
          })
          .catch(() => undefined);
      }}
    >
      <Icon name={copied ? 'check' : 'copy'} size={13} />
    </button>
  );
}

function MessageActions({ text, onRetry }: { text: string; onRetry?: () => void }) {
  if (!text.trim() && !onRetry) return null;
  return (
    <div className="message-actions">
      {text.trim() ? <CopyAction value={text} /> : null}
      {onRetry ? (
        <button className="message-action" type="button" aria-label="Try again" title="Try again" onClick={onRetry}>
          <Icon name="refresh" size={13} />
        </button>
      ) : null}
    </div>
  );
}

/** Latest completed paragraph, used as the collapsed reasoning preview. */
function reasoningPreview(text: string): string {
  const paragraphs = text
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);
  const last = paragraphs[paragraphs.length - 1] ?? '';
  const line = last.split('\n').filter(Boolean).pop() ?? '';
  return line.length > 180 ? `${line.slice(0, 180)}…` : line;
}

function Reasoning({ text, streaming }: { text: string; streaming: boolean }) {
  const [open, setOpen] = useState(false);
  if (!text.trim()) return null;
  const preview = streaming && !open ? reasoningPreview(text) : '';
  return (
    <div className={`reasoning ${open ? 'open' : ''} ${streaming ? 'streaming' : ''}`}>
      <button className="reasoning-head" type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <Icon name="sparkles" size={14} />
        <span>Reasoning</span>
        <Icon name={open ? 'chevron-down' : 'chevron-right'} size={13} className="reasoning-chevron" />
      </button>
      {preview ? <div className="reasoning-preview">{preview}</div> : null}
      {open ? <div className="reasoning-body">{text}</div> : null}
    </div>
  );
}

/**
 * One collapsible process block per turn: reasoning, intermediate step text and
 * tool calls live inside it, while the final answer stays in the transcript
 * body. The block stays folded while the turn runs too — the header carries the
 * live status and elapsed time, so expanding it is optional.
 */
function TurnProcess({
  running,
  summary,
  startedAt,
  toolCount,
  children,
}: {
  running: boolean;
  summary: string;
  startedAt: number;
  toolCount: number;
  children: React.ReactNode;
}) {
  const [manual, setManual] = useState<boolean | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  useEffect(() => {
    if (!running) setManual(null);
  }, [running]);
  const open = manual ?? false;
  const tools = toolCount > 0 ? ` · ${toolCount} tool${toolCount === 1 ? '' : 's'}` : '';
  const label = running ? `Working… ${formatElapsed(Math.max(0, now - startedAt))}${tools}` : summary;
  return (
    <div className={`turn-process ${open ? 'open' : ''} ${running ? 'running' : ''}`}>
      <button
        className="turn-process-head"
        type="button"
        aria-expanded={open}
        onClick={() => setManual((value) => !(value ?? false))}
      >
        {running ? <span className="mini-spinner" /> : <Icon name="sparkles" size={13} />}
        <span className="turn-process-label">{label}</span>
        <Icon name={open ? 'chevron-down' : 'chevron-right'} size={13} className="turn-process-chevron" />
      </button>
      <div className="turn-process-body" aria-hidden={!open}>
        <div className="turn-process-inner" inert={open ? undefined : true}>
          {children}
        </div>
      </div>
    </div>
  );
}

const UserMessage = memo(function UserMessage({ message, onRetry }: { message: ChatMessage; onRetry?: () => void }) {
  return (
    <article className="message user-message">
      <div className="user-bubble">
        {message.text ? <div className="user-text">{message.text}</div> : null}
        {message.images.length ? (
          <div className="image-chips">
            {message.images.map((image) => (
              <span className="image-chip" key={image.attachment}>
                <Icon name="file" size={12} />
                {image.attachment}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <MessageActions text={message.text} onRetry={onRetry} />
    </article>
  );
});

function turnDuration(user: ChatMessage | undefined, steps: ChatMessage[]): number {
  const meta = user?.turn_meta;
  if (meta) return Math.max(0, (meta.ended_at ?? Date.now()) - meta.started_at);
  if (steps.length > 1) {
    const first = steps[0].created_at;
    const last = steps[steps.length - 1].created_at;
    if (first && last) return Math.max(0, last - first);
  }
  return 0;
}

function processSummary(options: {
  user?: ChatMessage;
  steps: ChatMessage[];
  running: boolean;
  toolCount: number;
}): string {
  const { user, steps, running, toolCount } = options;
  if (running) return 'Working…';
  const duration = formatElapsed(turnDuration(user, steps));
  const status = user?.turn_meta?.status;
  if (status === 'failed') return `Failed after ${duration}`;
  if (status === 'stopped') return `Stopped after ${duration}`;
  const tools = toolCount > 0 ? ` · ${toolCount} tool${toolCount === 1 ? '' : 's'}` : '';
  return `Worked for ${duration}${tools}`;
}

function Turn({
  user,
  steps,
  latest,
  busy,
  active,
  onRetry,
}: {
  user?: ChatMessage;
  steps: ChatMessage[];
  latest: boolean;
  busy: boolean;
  active: boolean;
  onRetry?: (text: string) => void;
}) {
  const answer = steps.reduce<ChatMessage | null>((found, message) => (message.text.trim() ? message : found), null);
  const answerIndex = answer ? steps.indexOf(answer) : -1;
  const toolCount = steps.reduce((total, message) => total + message.tool_calls.length, 0);
  const streaming = steps.some((message) => message.streaming);
  const pendingCall = steps.some((message) => message.tool_calls.some((call) => call.pending));
  const running = Boolean(user?.turn_meta?.status === 'running' || streaming || pendingCall || (latest && busy && steps.length === 0));
  const hasProcess = steps.some((message) => message.reasoning.trim() || message.tool_calls.length || (message.text.trim() && message !== answer));
  const summary = processSummary({ user, steps, running, toolCount });
  const startedAt = user?.turn_meta?.started_at ?? steps[0]?.created_at ?? Date.now();
  const canRetry = Boolean(latest && !busy && active && user?.text.trim() && onRetry);
  const failed = !answer && steps.some((message) => message.is_error);
  const showActions = Boolean(answer && !running);

  const stepNodes = steps.map((message, index) => (
    <div className="turn-step" key={String(message.id)}>
      {message.reasoning.trim() ? (
        <Reasoning text={message.reasoning} streaming={Boolean(message.streaming)} />
      ) : null}
      {message.text.trim() && index !== answerIndex ? (
        <div className="turn-step-text">
          <Markdown>{message.text}</Markdown>
        </div>
      ) : null}
      {message.tool_calls.length ? (
        <div className="tool-list">
          {message.tool_calls.map((call) => (
            <ToolCard key={call.id} call={call} />
          ))}
        </div>
      ) : null}
    </div>
  ));

  return (
    <div className={`turn ${running ? 'running' : ''}`}>
      {user ? (
        <UserMessage
          message={user}
          onRetry={canRetry && onRetry ? () => onRetry(user.text) : undefined}
        />
      ) : null}

      {running || hasProcess ? (
        // One foldable process block per turn. It stays folded while running too:
        // the header carries the live status, and the block folds to a one-line
        // summary on settle. The answer below never folds.
        <TurnProcess running={running} summary={summary} startedAt={startedAt} toolCount={toolCount}>
          {stepNodes}
        </TurnProcess>
      ) : null}

      {answer ? (
        <div className={`assistant-text ${running ? 'streaming' : ''}`}>
          <Markdown>{answer.text}</Markdown>
          {answer.streaming ? <span className="stream-cursor" /> : null}
          {showActions ? (
            <MessageActions
              text={answer.text}
              onRetry={canRetry && onRetry && user ? () => onRetry(user.text) : undefined}
            />
          ) : null}
        </div>
      ) : null}

      {failed ? <div className="assistant-error">This step failed. Open the process above for details.</div> : null}

    </div>
  );
}

interface TurnGroup {
  key: string;
  user?: ChatMessage;
  steps: ChatMessage[];
}

function groupTurns(messages: ChatMessage[]): TurnGroup[] {
  const groups: TurnGroup[] = [];
  let current: TurnGroup | null = null;
  let currentTurn: number | null = null;
  for (const message of messages) {
    if (message.role === 'user') {
      current = { key: `turn-${String(message.id)}`, user: message, steps: [] };
      groups.push(current);
      currentTurn = null;
      continue;
    }
    if (message.role !== 'assistant') continue;
    // A live step that belongs to a later run starts its own group even when its
    // user message has not arrived yet (queued or external sends). The turn
    // number alone is not enough because every run can number its steps from 1.
    const nextRun =
      current !== null &&
      ((message.turn !== undefined && currentTurn !== null && message.turn !== currentTurn) ||
        // A detached step belongs to a run started outside this turn (external
        // or queued send), so it must not extend a group that already has steps.
        (message.detached === true && current.steps.length > 0));
    if (nextRun) {
      current = { key: `group-${String(message.id)}`, steps: [] };
      groups.push(current);
      currentTurn = null;
    }
    if (!current) {
      current = { key: `group-${String(message.id)}`, steps: [] };
      groups.push(current);
    }
    current.steps.push(message);
    if (message.turn !== undefined) currentTurn = message.turn;
  }
  return groups;
}

const Notice = memo(function Notice({ message }: { message: ChatMessage }) {
  return (
    <div className={`transcript-notice ${message.is_error ? 'error' : ''}`}>
      <Icon name={message.is_error ? 'alert' : 'info'} size={14} />
      <span>{message.text || 'The runtime reported an empty message.'}</span>
    </div>
  );
});

function EmptyHero({ onPick, cwd }: { onPick: (prompt: string) => void; cwd?: string }) {
  const suggestions = [
    'Explain the architecture of this repository.',
    'Review my latest changes for bugs.',
    'Plan the next feature and list the steps.',
  ];
  return (
    <div className="empty-hero">
      <div className="empty-mark-wrap">
        <BrandMark size={58} />
      </div>
      <h2>What should we build?</h2>
      <p>Describe a task, paste an error, or ask about this workspace.</p>
      {cwd ? (
        <div className="empty-workspace">
          <Icon name="folder" size={13} />
          {basename(cwd)}
        </div>
      ) : null}
      <div className="suggestion-row">
        {suggestions.map((suggestion) => (
          <button className="suggestion" type="button" key={suggestion} onClick={() => onPick(suggestion)}>
            <Icon name="arrow-right" size={13} />
            {suggestion}
          </button>
        ))}
      </div>
    </div>
  );
}

function Skeleton() {
  return (
    <div className="transcript-skeleton" role="status" aria-label="Loading transcript">
      {[0, 1, 2].map((index) => (
        <div className="skeleton-row" key={index}>
          <div className="skeleton-lines">
            <span style={{ width: `${52 - index * 8}%` }} />
            <span style={{ width: `${86 - index * 6}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function Transcript({
  timeline,
  loading,
  error,
  turnError,
  onDismissTurnError,
  onPick,
  onRetry,
  notices,
  draftCwd,
}: {
  timeline: Timeline | null;
  loading: boolean;
  error: string | null;
  turnError?: string | null;
  onDismissTurnError?: () => void;
  onPick: (prompt: string) => void;
  onRetry?: (text: string) => void;
  notices?: ChatMessage[];
  /** A local, unsaved new session: shows the hero instead of an empty state. */
  draftCwd?: string | null;
}) {
  if (loading && !timeline) return <Skeleton />;
  if (!timeline) {
    // A fresh draft is a real starting point, not a missing selection: the
    // browser creates the session only when the first message is sent.
    if (draftCwd !== null && draftCwd !== undefined) {
      return (
        <div className="transcript">
          <EmptyHero onPick={onPick} cwd={draftCwd || undefined} />
        </div>
      );
    }
    return (
      <div className="transcript-empty-state">
        <Icon name="message" size={28} />
        <p>Select a session to see its transcript.</p>
      </div>
    );
  }

  const readOnly = !timeline.active;
  const statusText = timeline.busy
    ? 'DeepPyro is working.'
    : readOnly
      ? 'This session is read-only.'
      : 'DeepPyro is ready.';
  // Tool results already arrive joined onto the assistant tool calls, so a
  // separate "tool" row would print the same output twice as a raw notice.
  const messages = timeline.messages.filter(isVisibleMessage);
  const groups = groupTurns(messages);
  const others = messages.filter((message) => message.role !== 'user' && message.role !== 'assistant');

  return (
    <div className="transcript" aria-busy={timeline.busy}>
      <div className="sr-only" role="status" aria-live="polite">
        {statusText}
      </div>
      {error ? (
        <div className="inline-error" role="alert">
          <Icon name="alert" size={15} />
          <span>{error}</span>
        </div>
      ) : null}
      {turnError ? (
        <div className="inline-error" role="alert">
          <Icon name="alert" size={15} />
          <span>{turnError}</span>
          {onDismissTurnError ? (
            <button className="text-button inline-error-dismiss" type="button" onClick={onDismissTurnError}>
              Dismiss
            </button>
          ) : null}
        </div>
      ) : null}
      {messages.length === 0 && !timeline.busy ? (
        <EmptyHero onPick={onPick} cwd={timeline.cwd} />
      ) : (
        <div className="message-list" role="log" aria-label="Conversation">
          {groups.map((group, index) => (
            <Turn
              key={group.key}
              user={group.user}
              steps={group.steps}
              latest={index === groups.length - 1}
              busy={timeline.busy}
              active={timeline.active}
              onRetry={onRetry}
            />
          ))}
          {others.map((message) => (
            <Notice key={String(message.id)} message={message} />
          ))}
          {notices?.map((notice) => (
            <Notice key={String(notice.id)} message={notice} />
          ))}
          {timeline.busy && messages.length === 0 ? (
            <div className="thinking-row">
              <span className="mini-spinner" />
              Getting started…
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
