import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { CommandSpec, SessionSummary } from '../types';
import { sessionTitle, splitArgs } from '../lib/format';
import { Icon } from './Icon';
import type { IconName } from './Icon';

export interface PaletteProps {
  open: boolean;
  commands: CommandSpec[];
  sessions: SessionSummary[];
  onClose: () => void;
  onRunCommand: (command: CommandSpec, args: string[]) => void;
  onSelectSession: (sessionId: string) => void;
  onOpenSettings: () => void;
  onNewSession: () => void;
}

/** Hard cap on rendered rows so the palette stays scannable. */
const MAX_RESULTS = 12;

interface ActionSpec {
  id: 'new' | 'settings';
  name: string;
  description: string;
  icon: IconName;
}

const ACTIONS: readonly ActionSpec[] = [
  { id: 'new', name: '/new', description: 'Start a new session', icon: 'plus' },
  { id: 'settings', name: '/settings', description: 'Open settings', icon: 'settings' },
];

interface ActionRow {
  key: string;
  group: 'Actions';
  kind: 'action';
  name: string;
  description: string;
  icon: IconName;
  actionId: ActionSpec['id'];
}

interface CommandRow {
  key: string;
  group: 'Commands';
  kind: 'command';
  name: string;
  description: string;
  icon: IconName;
  command: CommandSpec;
}

interface SessionRow {
  key: string;
  group: 'Sessions';
  kind: 'session';
  name: string;
  description: string;
  icon: IconName;
  sessionId: string;
}

type PaletteRow = ActionRow | CommandRow | SessionRow;

/** Every token must appear in the lower-cased haystack. */
function matchTokens(haystack: string, tokens: string[]): boolean {
  return tokens.every((token) => haystack.includes(token));
}

function commandHaystack(command: CommandSpec): string {
  return [command.name, command.summary, ...command.aliases].join(' ').toLowerCase();
}

/** Commands match by name/summary; a leading slash narrows to the invoked name. */
function matchesCommand(command: CommandSpec, tokens: string[]): boolean {
  if (tokens.length === 0) return true;
  const head = tokens[0];
  if (head.startsWith('/')) {
    const bare = head.slice(1);
    if (!bare) return true;
    const forms = [command.name, ...command.aliases].map((form) => form.toLowerCase());
    return forms.some((form) => form.startsWith(bare));
  }
  return matchTokens(commandHaystack(command), tokens);
}

/**
 * Arguments typed after the command name, e.g. "/model gpt-4" -> ["gpt-4"].
 * Commands that take no argument always receive an empty list.
 */
function commandArgs(command: CommandSpec, query: string): string[] {
  if (!command.takes_arg) return [];
  const trimmed = query.trim().replace(/^\/+/, '');
  if (!trimmed) return [];
  const lower = trimmed.toLowerCase();
  const forms = [command.name, ...command.aliases].map((form) => form.toLowerCase());
  for (const form of forms) {
    if (!form) continue;
    if (lower === form) return [];
    if (lower.startsWith(form) && /^\s/.test(trimmed.charAt(form.length))) {
      return splitArgs(trimmed.slice(form.length));
    }
  }
  return [];
}

export function Palette(props: PaletteProps) {
  const {
    open,
    commands,
    sessions,
    onClose,
    onRunCommand,
    onSelectSession,
    onOpenSettings,
    onNewSession,
  } = props;

  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  const rows = useMemo<PaletteRow[]>(() => {
    const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const list: PaletteRow[] = [];

    for (const action of ACTIONS) {
      const haystack = `${action.name} ${action.description}`.toLowerCase();
      if (!matchTokens(haystack, tokens)) continue;
      list.push({
        key: `action:${action.id}`,
        group: 'Actions',
        kind: 'action',
        name: action.name,
        description: action.description,
        icon: action.icon,
        actionId: action.id,
      });
    }

    for (const command of commands) {
      if (!command.name || !matchesCommand(command, tokens)) continue;
      list.push({
        key: `command:${command.name}`,
        group: 'Commands',
        kind: 'command',
        name: `/${command.name}`,
        description: command.summary || command.usage,
        icon: 'command',
        command,
      });
    }

    for (const session of sessions) {
      if (session.archived) continue;
      const title = sessionTitle(session.title);
      const haystack = `${title} ${session.cwd}`.toLowerCase();
      if (!matchTokens(haystack, tokens)) continue;
      list.push({
        key: `session:${session.session_id}`,
        group: 'Sessions',
        kind: 'session',
        name: title,
        description: session.cwd,
        icon: 'message',
        sessionId: session.session_id,
      });
    }

    return list.slice(0, MAX_RESULTS);
  }, [commands, query, sessions]);

  const groups = useMemo(() => {
    const out: Array<{ label: PaletteRow['group']; items: Array<{ row: PaletteRow; index: number }> }> = [];
    rows.forEach((row, index) => {
      const last = out[out.length - 1];
      if (last && last.label === row.group) last.items.push({ row, index });
      else out.push({ label: row.group, items: [{ row, index }] });
    });
    return out;
  }, [rows]);

  const activeIndex = rows.length === 0 ? 0 : Math.min(active, rows.length - 1);

  useEffect(() => {
    if (!open) return;
    setQuery('');
    setActive(0);
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const node = listRef.current?.querySelector<HTMLElement>('.palette-item.active');
    node?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, open, rows]);

  const activate = (row: PaletteRow) => {
    if (row.kind === 'action') {
      if (row.actionId === 'new') onNewSession();
      else onOpenSettings();
    } else if (row.kind === 'command') {
      onRunCommand(row.command, commandArgs(row.command, query));
    } else {
      onSelectSession(row.sessionId);
    }
    onClose();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (rows.length === 0) return;
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      setActive((current) => {
        const base = Math.min(Math.max(current, 0), rows.length - 1);
        return (base + step + rows.length) % rows.length;
      });
      return;
    }
    if (event.key === 'Enter') {
      const row = rows[activeIndex];
      if (!row) return;
      event.preventDefault();
      activate(row);
    }
  };

  if (!open) return null;

  return (
    <div
      className="palette-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={handleKeyDown}
      >
        <div className="palette-search">
          <span className="palette-icon">
            <Icon name="search" size={16} />
          </span>
          <input
            ref={inputRef}
            className="palette-input"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setActive(0);
            }}
            placeholder="Search actions, commands and sessions"
            aria-label="Search actions, commands and sessions"
            autoComplete="off"
            spellCheck={false}
          />
        </div>

        <div className="palette-results" role="listbox" aria-label="Results" ref={listRef}>
          {rows.length === 0 ? (
            <div className="palette-empty">No matches</div>
          ) : (
            groups.map((group) => (
              <div className="palette-group" role="group" aria-label={group.label} key={group.label}>
                <div className="palette-group-label">{group.label}</div>
                {group.items.map(({ row, index }) => (
                  <div
                    key={row.key}
                    className={`palette-item ${index === activeIndex ? 'active' : ''}`}
                    role="option"
                    aria-selected={index === activeIndex}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => activate(row)}
                  >
                    <span className="palette-icon">
                      <Icon name={row.icon} size={15} />
                    </span>
                    <span className="palette-copy">
                      <span className="palette-name">{row.name}</span>
                      {row.description ? <span className="palette-desc">{row.description}</span> : null}
                    </span>
                  </div>
                ))}
              </div>
            ))
          )}
        </div>

        <footer className="palette-footer">
          <span>
            <kbd className="palette-kbd">↑↓</kbd> navigate
          </span>
          <span>
            <kbd className="palette-kbd">↵</kbd> run
          </span>
          <span>
            <kbd className="palette-kbd">esc</kbd> close
          </span>
        </footer>
      </div>
    </div>
  );
}
