/*
 * Semantic tool activity card: a compact, quiet row that expands into the
 * meaningful part of a tool call instead of raw argument and result JSON.
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { ToolCall } from '../types';
import {
  agentView,
  askView,
  baseName,
  detailFields,
  directoryView,
  editView,
  globView,
  goalInfo,
  grepView,
  presentView,
  rawArgsText,
  readView,
  resultText,
  shellView,
  todoView,
  toolMeta,
  urlHost,
  webView,
  writeView
} from '../lib/toolMeta';
import type { DetailField, ToolKind } from '../lib/toolMeta';
import { Icon } from './Icon';
import './toolcard.css';

function cx(...values: Array<string | false | null | undefined>): string {
  const parts: string[] = [];
  for (const value of values) {
    if (typeof value === 'string' && value) parts.push(value);
  }
  return parts.join(' ');
}

/* ---------------------------------- shared --------------------------- */

function EmptyNote({ call, text }: { call: ToolCall; text: string }) {
  return <div className="tc-empty">{call.pending ? 'Running…' : text}</div>;
}

function OutputBody({ call }: { call: ToolCall }) {
  const output = resultText(call);
  if (!output) return <EmptyNote call={call} text="No output." />;
  return <pre className="tc-output">{output}</pre>;
}

function FileHeader({ path, meta }: { path?: string; meta?: string }) {
  if (!path) return null;
  return (
    <div className="tc-filebar">
      <Icon name="file" size={13} />
      <span className="tc-file" title={path}>
        {path}
      </span>
      {meta ? <span className="tc-meta">{meta}</span> : null}
    </div>
  );
}

function FieldList({ fields }: { fields: DetailField[] }) {
  return (
    <div className="tc-fields">
      {fields.map(function (field, index) {
        return (
          <div className="tc-field" key={field.label + '-' + String(index)}>
            <span className="tc-field-label">{field.label}</span>
            <span className={cx('tc-field-value', field.mono && 'tc-mono')}>{field.value}</span>
          </div>
        );
      })}
    </div>
  );
}

function prefixLines(text: string, sign: string, cap = 40): string {
  const lines = text.split(/\r?\n/);
  const shown: string[] = [];
  for (const line of lines.slice(0, cap)) shown.push(sign + ' ' + line);
  if (lines.length > cap) shown.push(sign + ' … ' + String(lines.length - cap) + ' more lines');
  return shown.join('\n');
}

/* ---------------------------------- bodies --------------------------- */

function ReadBody({ call }: { call: ToolCall }) {
  const view = readView(call);
  const lineCount = view.total !== undefined ? String(view.total) + ' lines' : undefined;
  if (!view.lines.length) {
    if (!view.path) return <OutputBody call={call} />;
    const fallback = resultText(call);
    return (
      <div className="tc-read">
        <FileHeader path={view.path} meta={lineCount} />
        {fallback && !call.pending ? <pre className="tc-output">{fallback}</pre> : <EmptyNote call={call} text="No file contents." />}
      </div>
    );
  }
  return (
    <div className="tc-read">
      <FileHeader path={view.path} meta={lineCount} />
      <div className="tc-code">
        {view.lines.map(function (line) {
          return (
            <div className="tc-line" key={line.num}>
              <span className="tc-gutter" aria-hidden="true">
                {line.num}
              </span>
              <span className="tc-code-text">{line.text || ' '}</span>
            </div>
          );
        })}
        {view.remaining > 0 ? <div className="tc-more">… {view.remaining} more lines</div> : null}
      </div>
    </div>
  );
}

function WriteBody({ call }: { call: ToolCall }) {
  const view = writeView(call);
  if (!view.path && !view.lines.length) return <OutputBody call={call} />;
  return (
    <div className="tc-write">
      <FileHeader
        path={view.path}
        meta={view.lines.length ? '+' + String(view.totalLines) + (view.totalLines === 1 ? ' line' : ' lines') : undefined}
      />
      {view.lines.length ? (
        <pre className="tc-codeblock">{view.lines.join('\n')}</pre>
      ) : (
        <EmptyNote call={call} text="No content returned." />
      )}
      {view.remaining > 0 ? <div className="tc-more">… {view.remaining} more lines</div> : null}
      {view.status ? <div className="tc-caption">{view.status}</div> : null}
    </div>
  );
}

function EditBody({ call }: { call: ToolCall }) {
  const view = editView(call);
  const hasSides = view.oldText !== undefined || view.newText !== undefined;
  if (!view.path && !hasSides && !view.patch) return <OutputBody call={call} />;
  const stat = view.added || view.removed ? '+' + String(view.added) + ' −' + String(view.removed) : undefined;
  return (
    <div className="tc-edit">
      <FileHeader path={view.path} meta={stat} />
      {view.replaceAll ? <div className="tc-caption">Replaces every match.</div> : null}
      {view.oldText ? (
        <div className="tc-diff">
          <div className="tc-diff-label">Before</div>
          <pre className="tc-codeblock tc-removed">{prefixLines(view.oldText, '−')}</pre>
        </div>
      ) : null}
      {view.newText ? (
        <div className="tc-diff">
          <div className="tc-diff-label">After</div>
          <pre className="tc-codeblock tc-added">{prefixLines(view.newText, '+')}</pre>
        </div>
      ) : null}
      {view.patch ? <pre className="tc-codeblock">{view.patch}</pre> : null}
      {view.status ? <div className="tc-caption">{view.status}</div> : null}
    </div>
  );
}

function ShellBody({ call }: { call: ToolCall }) {
  const view = shellView(call);
  return (
    <div className="tc-shell">
      <div className="tc-prompt">
        <span className="tc-prompt-sign" aria-hidden="true">
          $
        </span>
        <code className="tc-command">{view.command || 'Command'}</code>
      </div>
      {view.description ? <div className="tc-caption">{view.description}</div> : null}
      {view.cwd ? <div className="tc-cwd">{view.cwd}</div> : null}
      {view.output ? (
        <pre className="tc-output">{view.output}</pre>
      ) : (
        <div className="tc-caption">{call.pending ? 'Running…' : 'Completed with no output'}</div>
      )}
      {view.exitCode !== undefined && view.exitCode !== 0 ? <div className="tc-caption tc-failed">{'Exit code ' + String(view.exitCode)}</div> : null}
    </div>
  );
}

function GrepBody({ call }: { call: ToolCall }) {
  const view = grepView(call);
  if (!view.files.length) return <OutputBody call={call} />;
  return (
    <div className="tc-grep">
      <div className="tc-query">
        <Icon name="search" size={13} />
        <span className="tc-query-text">{view.pattern || 'Search'}</span>
        {view.header ? <span className="tc-meta">{view.header}</span> : null}
      </div>
      <div className="tc-hitlist">
        {view.files.map(function (file) {
          return (
            <div className="tc-hitgroup" key={file.file}>
              <div className="tc-hitfile" title={file.file}>
                {file.file}
              </div>
              {file.hits.map(function (hit) {
                return (
                  <div className="tc-hit" key={file.file + ':' + String(hit.line)}>
                    <span className="tc-hitline" aria-hidden="true">
                      {hit.line}
                    </span>
                    <span className="tc-hittext">{hit.text || ' '}</span>
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function GlobBody({ call }: { call: ToolCall }) {
  const view = globView(call);
  if (!view.files.length) return <OutputBody call={call} />;
  return (
    <div className="tc-files">
      {view.files.map(function (file) {
        return (
          <div className="tc-filerow" key={file}>
            <Icon name="file" size={12} />
            <span className="tc-path" title={file}>
              {file}
            </span>
          </div>
        );
      })}
      {view.more > 0 ? <div className="tc-more">… {view.more} more files</div> : null}
    </div>
  );
}

function WebBody({ call }: { call: ToolCall }) {
  const view = webView(call);
  if (!view.sources.length && !view.excerpt && !view.url && !view.queries.length) return <OutputBody call={call} />;
  return (
    <div className="tc-web">
      {view.mode === 'fetch' ? (
        <div className="tc-query">
          <Icon name="globe" size={13} />
          <span className="tc-query-text" title={view.url}>
            {view.url || 'Page'}
          </span>
          {view.status ? <span className="tc-meta">{view.status}</span> : null}
        </div>
      ) : view.queries.length ? (
        <div className="tc-query">
          <Icon name="search" size={13} />
          <span className="tc-query-text">{view.queries.join(' · ')}</span>
          {view.sources.length ? <span className="tc-meta">{view.sources.length} sources</span> : null}
        </div>
      ) : null}
      {view.sources.length ? (
        <div className="tc-sources">
          {view.sources.map(function (source) {
            return (
              <a className="tc-source" href={source.url} key={source.url} target="_blank" rel="noreferrer noopener">
                <Icon name="external" size={12} />
                <span className="tc-source-text">{source.title || urlHost(source.url)}</span>
              </a>
            );
          })}
        </div>
      ) : null}
      {view.excerpt ? <p className="tc-excerpt">{view.excerpt}</p> : null}
    </div>
  );
}

function TodoBody({ call }: { call: ToolCall }) {
  const items = todoView(call);
  if (!items.length) return <OutputBody call={call} />;
  return (
    <div className="tc-todos">
      {items.map(function (item, index) {
        return (
          <div className={cx('tc-todo', 'tc-todo-' + item.status)} key={String(index) + '-' + item.content}>
            <span className="tc-todo-mark">
              {item.status === 'completed' ? (
                <Icon name="check" size={12} />
              ) : item.status === 'in_progress' ? (
                <span className="tc-dot" />
              ) : (
                <span className="tc-ring" />
              )}
            </span>
            <span className="tc-todo-text">{item.content}</span>
          </div>
        );
      })}
    </div>
  );
}

function PresentBody({ call }: { call: ToolCall }) {
  const files = presentView(call);
  if (!files.length) return <OutputBody call={call} />;
  return (
    <div className="tc-present">
      {files.map(function (file) {
        return (
          <div className="tc-present-row" key={file.path}>
            <span className="tc-present-icon">
              <Icon name="file" size={14} />
            </span>
            <span className="tc-present-main">
              <span className="tc-present-name">{baseName(file.path)}</span>
              {file.description ? <span className="tc-caption">{file.description}</span> : null}
              <span className="tc-present-path" title={file.path}>
                {file.path}
              </span>
            </span>
          </div>
        );
      })}
    </div>
  );
}

function AskBody({ call }: { call: ToolCall }) {
  const questions = askView(call);
  if (!questions.length) return <OutputBody call={call} />;
  return (
    <div className="tc-questions">
      {questions.map(function (question, index) {
        return (
          <div className="tc-question" key={question.id ?? 'question-' + String(index)}>
            {question.header ? <div className="tc-caption">{question.header}</div> : null}
            <div className="tc-question-text">{question.question}</div>
            {question.options.length ? (
              <div className="tc-options">
                {question.options.map(function (option) {
                  const selected = question.selected.indexOf(option.label) >= 0;
                  return (
                    <div className={cx('tc-option', selected && 'tc-option-selected')} key={option.label}>
                      <span className="tc-option-mark">{selected ? <Icon name="check" size={12} /> : null}</span>
                      <span className="tc-option-label">{option.label}</span>
                      {option.description ? <span className="tc-option-desc">{option.description}</span> : null}
                    </div>
                  );
                })}
              </div>
            ) : null}
            {question.custom ? <div className="tc-answer-custom">{question.custom}</div> : null}
            {question.multiSelect ? <div className="tc-caption">You can pick more than one.</div> : null}
          </div>
        );
      })}
    </div>
  );
}

function DirectoryBody({ call }: { call: ToolCall }) {
  const view = directoryView(call);
  if (!view.path) return <OutputBody call={call} />;
  const requested = view.requested && view.requested !== view.path ? view.requested : undefined;
  return (
    <div className="tc-directory">
      {requested ? <div className="tc-caption">{'Requested ' + requested}</div> : null}
      <div className="tc-dir">
        <Icon name="folder" size={14} />
        <span className="tc-path">{view.path}</span>
      </div>
    </div>
  );
}

function AgentBody({ call }: { call: ToolCall }) {
  const view = agentView(call);
  const skip = new Set(['Name', 'Description', 'Message', 'Prompt', 'Agent']);
  const fields = detailFields(call).filter(function (field) {
    return !skip.has(field.label);
  });
  const output = resultText(call);
  const isEmpty = !view.name && !view.description && !view.message && !view.prompt && !fields.length && !output;
  return (
    <div className="tc-agent">
      {view.name || view.description ? (
        <div className="tc-agent-head">
          <Icon name="message" size={13} />
          <span className="tc-agent-name">{view.name ?? view.description}</span>
        </div>
      ) : null}
      {view.name && view.description ? <p className="tc-excerpt">{view.description}</p> : null}
      {view.message ? <pre className="tc-message">{view.message}</pre> : null}
      {view.prompt ? <p className="tc-excerpt">{view.prompt}</p> : null}
      {fields.length ? <FieldList fields={fields} /> : null}
      {output ? <pre className="tc-output">{output}</pre> : null}
      {isEmpty ? <EmptyNote call={call} text="No details." /> : null}
    </div>
  );
}

function GoalBody({ call }: { call: ToolCall }) {
  const info = goalInfo(call);
  const fields = detailFields(call).filter(function (field) {
    return field.label !== 'Objective';
  });
  const output = resultText(call);
  const hasInfo = Boolean(info.objective || info.status || info.phase || info.blockedReason);
  return (
    <div className="tc-goal">
      {info.objective ? <p className="tc-objective">{info.objective}</p> : null}
      {info.blockedReason ? <div className="tc-caption tc-failed">{'Blocked: ' + info.blockedReason}</div> : null}
      {info.status ? <div className="tc-caption">{'Status: ' + info.status}</div> : null}
      {info.phase && !info.status ? <div className="tc-caption">{'Phase: ' + info.phase}</div> : null}
      {fields.length ? <FieldList fields={fields} /> : null}
      {output && !hasInfo ? <pre className="tc-output">{output}</pre> : null}
      {!hasInfo && !fields.length && !output ? <EmptyNote call={call} text="No goal set." /> : null}
    </div>
  );
}

function FieldsBody({ call }: { call: ToolCall }) {
  const fields = detailFields(call);
  const output = resultText(call);
  if (!fields.length && !output) return <OutputBody call={call} />;
  return (
    <div className="tc-fields-wrap">
      {fields.length ? <FieldList fields={fields} /> : null}
      {output ? <pre className="tc-output">{output}</pre> : null}
    </div>
  );
}

function JobBody({ call }: { call: ToolCall }) {
  const fields = detailFields(call).filter(function (field) {
    return field.label !== 'Background job';
  });
  const raw = typeof call.result === 'string' ? call.result : '';
  const statusMatch = /\[status:\s*([a-z ]+?)(?:,\s*exit code:\s*(-?\d+))?\]/i.exec(raw);
  const status = statusMatch ? statusMatch[1].trim() : undefined;
  const exitCode = statusMatch && statusMatch[2] !== undefined ? statusMatch[2] : undefined;
  const output = raw.replace(/\[status:[^\]]*\]\s*$/i, '').trim();
  return (
    <div className="tc-fields-wrap">
      {status ? (
        <div className="tc-caption">{'Status: ' + status + (exitCode !== undefined ? ' · exit code ' + exitCode : '')}</div>
      ) : null}
      {fields.length ? <FieldList fields={fields} /> : null}
      {output ? <pre className="tc-output">{output}</pre> : null}
      {!status && !fields.length && !output ? <EmptyNote call={call} text="No job details." /> : null}
    </div>
  );
}

function UnknownBody({ call }: { call: ToolCall }) {
  const argsText = rawArgsText(call);
  const args = argsText === '{}' || argsText === '[]' ? '' : argsText;
  const output = resultText(call);
  return (
    <div className="tc-unknown">
      {args ? <pre className="tc-json">{args}</pre> : null}
      {output ? <pre className="tc-output">{output}</pre> : null}
      {!args && !output ? <EmptyNote call={call} text="No details." /> : null}
    </div>
  );
}

function CardBody({ call, kind }: { call: ToolCall; kind: ToolKind }) {
  switch (kind) {
    case 'read':
      return <ReadBody call={call} />;
    case 'write':
      return <WriteBody call={call} />;
    case 'edit':
      return <EditBody call={call} />;
    case 'shell':
      return <ShellBody call={call} />;
    case 'search':
      return <GrepBody call={call} />;
    case 'find':
      return <GlobBody call={call} />;
    case 'web':
      return <WebBody call={call} />;
    case 'todo':
      return <TodoBody call={call} />;
    case 'present':
      return <PresentBody call={call} />;
    case 'ask':
      return <AskBody call={call} />;
    case 'directory':
      return <DirectoryBody call={call} />;
    case 'agent':
      return <AgentBody call={call} />;
    case 'goal':
      return <GoalBody call={call} />;
    case 'job':
      return <JobBody call={call} />;
    case 'unknown':
      return <UnknownBody call={call} />;
    default:
      return <FieldsBody call={call} />;
  }
}

/* ---------------------------------- card ---------------------------- */

export function ToolCard({ call }: { call: ToolCall }) {
  const meta = toolMeta(call);
  const [open, setOpen] = useState<boolean>(call.is_error);
  const [copied, setCopied] = useState(false);
  const bodyId = useId();
  const timer = useRef<number | null>(null);

  useEffect(
    function () {
      if (call.is_error) setOpen(true);
    },
    [call.is_error]
  );

  useEffect(function () {
    return function () {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, []);

  const toggle = useCallback(function () {
    setOpen(function (value) {
      return !value;
    });
  }, []);

  const copy = useCallback(
    function () {
      const rawResult = typeof call.result === 'string' ? call.result : '';
      const text = rawResult.trim() ? rawResult : rawArgsText(call);
      if (!text) return;
      const clipboard = navigator.clipboard;
      if (!clipboard || typeof clipboard.writeText !== 'function') return;
      void clipboard
        .writeText(text)
        .then(function () {
          setCopied(true);
          if (timer.current !== null) window.clearTimeout(timer.current);
          timer.current = window.setTimeout(function () {
            setCopied(false);
          }, 1400);
        })
        .catch(function () {
          /* copying is best effort */
        });
    },
    [call]
  );

  return (
    <div className={cx('tc-card', open && 'tc-open', call.pending && 'tc-pending', call.is_error && 'tc-error')}>
      <button
        aria-controls={open ? bodyId : undefined}
        aria-expanded={open}
        className="tc-head"
        onClick={toggle}
        type="button"
      >
        <span className="tc-icon">
          <Icon name={meta.icon} size={16} />
        </span>
        <span className="tc-title">{meta.title}</span>
        {meta.summary ? (
          <span className="tc-summary" title={meta.summary}>
            {meta.summary}
          </span>
        ) : null}
        <span className="tc-state">
          {call.pending ? (
            <>
              <span className="tc-spinner" aria-hidden="true" />
              <span className="tc-state-text">Running</span>
            </>
          ) : call.is_error ? (
            <span className="tc-state-text tc-failed">Failed</span>
          ) : null}
        </span>
        <span className="tc-chevron" aria-hidden="true">
          <Icon name="chevron-right" size={14} />
        </span>
      </button>
      {open ? (
        <div className="tc-body" id={bodyId}>
          <div className="tc-copywrap">
            <button aria-label="Copy output" className={cx('tc-copy', copied && 'tc-copied')} onClick={copy} type="button">
              <Icon name={copied ? 'check' : 'copy'} size={13} />
            </button>
          </div>
          <CardBody call={call} kind={meta.kind} />
        </div>
      ) : null}
    </div>
  );
}
