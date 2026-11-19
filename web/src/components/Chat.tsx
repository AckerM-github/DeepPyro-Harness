import { useState } from 'react';
import type { Approval, ChatMessage, Timeline, ToolCall } from '../types';
import { clockTime, prettyJson } from '../lib/format';
import { BrandMark, Icon } from './Icon';
import { Markdown } from './Markdown';

function CopyButton({ value, label = 'Copy' }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="text-button"
      type="button"
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
      <Icon name={copied ? 'check' : 'copy'} size={12} />
      {copied ? 'Copied' : label}
    </button>
  );
}

function ToolCard({ call }: { call: ToolCall }) {
  const [open, setOpen] = useState(false);
  const status = call.pending ? 'running' : call.is_error ? 'error' : 'done';
  const label = status === 'running' ? 'Running' : status === 'error' ? 'Error' : 'Done';
  const args = prettyJson(call.arguments);
  const result = call.result ?? '';
  return (
    <div className={`tool-card ${status}`}>
      <button className="tool-head" type="button" onClick={() => setOpen((value) => !value)}>
        <span className="tool-icon">
          <Icon name="terminal" size={15} />
        </span>
        <span className="tool-name">{call.name}</span>
        <code className="tool-id">{call.id}</code>
        <span className={`tool-status ${status}`}>
          {status === 'running' ? <span className="mini-spinner" /> : null}
          {label}
        </span>
        <Icon name={open ? 'chevron-down' : 'chevron-right'} size={14} className="tool-chevron" />
      </button>
      {open ? (
        <div className="tool-body">
          {args ? (
            <div className="tool-section">
              <div className="tool-label">
                <span>Arguments</span>
                <CopyButton value={args} />
              </div>
              <pre className="tool-pre">{args}</pre>
            </div>
          ) : null}
          {result ? (
            <div className="tool-section">
              <div className="tool-label">
                <span>Result</span>
                <CopyButton value={result} />
              </div>
              <pre className={`tool-pre ${call.is_error ? 'error-text' : ''}`}>{result}</pre>
            </div>
          ) : call.pending ? (
            <div className="tool-waiting">Waiting for the tool to finish…</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function Reasoning({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (!text.trim()) return null;
  return (
    <div className={`reasoning ${open ? 'open' : ''}`}>
      <button className="reasoning-head" type="button" onClick={() => setOpen((value) => !value)}>
        <Icon name="sparkles" size={14} />
        <span>Reasoning</span>
        <Icon name={open ? 'chevron-down' : 'chevron-right'} size={13} className="reasoning-chevron" />
      </button>
      {open ? <div className="reasoning-body">{text}</div> : null}
    </div>
  );
}

function UserMessage({ message }: { message: ChatMessage }) {
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
      <div className="message-time">{clockTime(message.created_at)}</div>
    </article>
  );
}

function AssistantMessage({ message }: { message: ChatMessage }) {
  const hasBody = Boolean(message.text.trim() || message.reasoning.trim() || message.tool_calls.length);
  return (
    <article className={`message assistant-message ${message.is_error ? 'error' : ''}`}>
      <div className="assistant-gutter">
        <BrandMark size={26} className="assistant-mark" />
      </div>
      <div className="assistant-content">
        <div className="assistant-head">
          <span className="assistant-name">DeepPyro</span>
          <span className="message-time">{clockTime(message.created_at)}</span>
          {message.streaming ? <span className="live-label">STREAMING</span> : null}
        </div>
        {message.reasoning ? <Reasoning text={message.reasoning} /> : null}
        {message.tool_calls.length ? (
          <div className="tool-list">
            {message.tool_calls.map((call) => (
              <ToolCard key={call.id} call={call} />
            ))}
          </div>
        ) : null}
        {message.text.trim() ? (
          <div className="assistant-text">
            <Markdown>{message.text}</Markdown>
            {message.streaming ? <span className="stream-cursor" /> : null}
          </div>
        ) : null}
        {message.is_error && !message.text.trim() ? (
          <div className="assistant-error">The tool call failed. Open its card for the error.</div>
        ) : null}
        {!hasBody ? <div className="assistant-waiting">Thinking…</div> : null}
      </div>
    </article>
  );
}

function Notice({ message }: { message: ChatMessage }) {
  return (
    <div className={`transcript-notice ${message.is_error ? 'error' : ''}`}>
      <Icon name={message.is_error ? 'alert' : 'info'} size={14} />
      <span>{message.text || 'The runtime reported an empty message.'}</span>
    </div>
  );
}

function ApprovalCard({
  approval,
  onDecide,
  disabled,
}: {
  approval: Approval;
  onDecide: (approvalId: string, decision: 'allow' | 'deny') => void;
  disabled: boolean;
}) {
  return (
    <div className="approval-card">
      <div className="approval-head">
        <span className="approval-icon">
          <Icon name="alert" size={15} />
        </span>
        <span className="approval-title">Approval required · {approval.tool}</span>
        <span className="approval-badge">WAITING</span>
      </div>
      <div className="approval-summary">{approval.summary || approval.detail || 'A tool needs permission to run.'}</div>
      {approval.detail && approval.detail !== approval.summary ? (
        <div className="approval-detail">{approval.detail}</div>
      ) : null}
      <div className="approval-actions">
        <button
          className="button primary small"
          type="button"
          disabled={disabled}
          onClick={() => onDecide(approval.approval_id, 'allow')}
        >
          Allow once
        </button>
        <button
          className="button ghost small"
          type="button"
          disabled={disabled}
          onClick={() => onDecide(approval.approval_id, 'deny')}
        >
          Deny
        </button>
      </div>
    </div>
  );
}

function EmptyHero({ onPick }: { onPick: (prompt: string) => void }) {
  const suggestions = [
    'Explain the architecture of this repository.',
    'Review the last change for bugs.',
    'Plan a refactor and list the steps.',
  ];
  return (
    <div className="empty-hero">
      <div className="empty-mark-wrap">
        <BrandMark size={58} />
      </div>
      <h2>Where should we start?</h2>
      <p>Ask about the code, plan a change, or run a slash command.</p>
      <div className="suggestion-row">
        {suggestions.map((suggestion) => (
          <button
            className="suggestion"
            type="button"
            key={suggestion}
            onClick={() => onPick(suggestion)}
          >
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
    <div className="transcript-skeleton" aria-hidden="true">
      {[0, 1, 2].map((index) => (
        <div className="skeleton-row" key={index}>
          <span className="skeleton-avatar" />
          <div className="skeleton-lines">
            <span style={{ width: `${64 - index * 9}%` }} />
            <span style={{ width: `${88 - index * 7}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function Transcript({
  timeline,
  approvals,
  loading,
  error,
  onDecide,
  onResume,
  onPick,
  resumeBusy,
}: {
  timeline: Timeline | null;
  approvals: Approval[];
  loading: boolean;
  error: string | null;
  onDecide: (approvalId: string, decision: 'allow' | 'deny') => void;
  onResume: () => void;
  onPick: (prompt: string) => void;
  resumeBusy: boolean;
}) {
  if (loading && !timeline) return <Skeleton />;
  if (!timeline) {
    return (
      <div className="transcript-empty-state">
        <Icon name="message" size={28} />
        <p>Select a session to see its transcript.</p>
      </div>
    );
  }

  const readOnly = !timeline.active;

  return (
    <div className="transcript">
      {error ? (
        <div className="inline-error">
          <Icon name="alert" size={15} />
          <span>{error}</span>
        </div>
      ) : null}
      {readOnly && timeline.readable ? (
        <div className="read-only-banner">
          <div>
            <strong>Read-only session.</strong>
            <span>Resume it to send a message.</span>
          </div>
          <button className="button primary small" type="button" disabled={resumeBusy} onClick={onResume}>
            {resumeBusy ? 'Resuming…' : 'Resume'}
          </button>
        </div>
      ) : null}
      {timeline.messages.length === 0 && !timeline.busy ? (
        <EmptyHero onPick={onPick} />
      ) : (
        <div className="message-list">
          {timeline.messages.map((message) =>
            message.role === 'user' ? (
              <UserMessage key={String(message.id)} message={message} />
            ) : message.role === 'assistant' ? (
              <AssistantMessage key={String(message.id)} message={message} />
            ) : (
              <Notice key={String(message.id)} message={message} />
            ),
          )}
          {timeline.busy && timeline.messages.length === 0 ? (
            <div className="thinking-row">
              <span className="mini-spinner" />
              DeepPyro is thinking…
            </div>
          ) : null}
          {approvals.map((approval) => (
            <ApprovalCard
              key={approval.approval_id}
              approval={approval}
              onDecide={onDecide}
              disabled={!timeline.active}
            />
          ))}
        </div>
      )}
    </div>
  );
}
