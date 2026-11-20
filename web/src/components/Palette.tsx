import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { CommandSpec, SessionSummary } from '../types';
import { sessionTitle } from '../lib/format';
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

/** Hard cap on rendered rows so the quick switcher stays scannable. */
const MAX_RESULTS = 14;

const RESULTS_ID = 'palette-results';

/** Commands duplicated by a palette action; the action is the canonical entry point. */
const ACTION_COMMAND_NAMES = ['new', 'settings'];

interface ActionSpec {
  id: 'new' | 'settings';
  label: string;
  description: string;
  icon: IconName;
}

const ACTIONS: readonly ActionSpec[] = [
  { id: 'new', label: 'New session', description: 'Start a fresh conversation', icon: 'plus' },
  { id: 'settings', label: 'Settings', description: 'Provider, agent, permissions', icon: 'settings' },
];

interface ActionRow {
  key: string;
  group: 'Actions';
  kind: 'action';
  label: string;
  description: string;
  icon: IconName;
  actionId: ActionSpec['id'];
}

interface CommandRow {
  key: string;
  group: 'Commands';
  kind: 'command';
  label: string;
  description: string;
  icon: IconName;
  command: CommandSpec;
}

interface SessionRow {
  key: string;
  group: 'Sessions';
  kind: 'session';
  label: string;
  description: string;
  icon: IconName;
  sessionId: string;
}

type PaletteRow = ActionRow | CommandRow | SessionRow;

function optionId(row: PaletteRow): string {
  return 'palette-option-' + row.key.replace(/[^a-zA-Z0-9_-]+/g, '-');
}

/** Quick switcher: sessions first, then plain actions; slash commands stay available but quiet. */
export function Palette(props: PaletteProps) {
  const { open, commands, sessions, onClose, onRunCommand, onSelectSession, onOpenSettings, onNewSession } = props;

  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const rows = useMemo<PaletteRow[]>(() => {
    const needle = query.trim().toLowerCase();
    const tokens = needle.split(/\s+/).filter(Boolean);
    const matches = (haystack: string) => tokens.every((token) => haystack.includes(token));
    const list: PaletteRow[] = [];

    for (const session of sessions) {
      if (session.archived) continue;
      const title = sessionTitle(session.title);
      if (!matches((title + ' ' + session.cwd).toLowerCase())) continue;
      list.push({
        key: 'session:' + session.session_id,
        group: 'Sessions',
        kind: 'session',
        label: title,
        description: session.cwd,
        icon: 'message',
        sessionId: session.session_id,
      });
    }

    for (const action of ACTIONS) {
      if (!matches((action.label + ' ' + action.description).toLowerCase())) continue;
      list.push({
        key: 'action:' + action.id,
        group: 'Actions',
        kind: 'action',
        label: action.label,
        description: action.description,
        icon: action.icon,
        actionId: action.id,
      });
    }

    if (needle.startsWith('/')) {
      const bare = needle.slice(1);
      for (const command of commands) {
        if (!command.name || ACTION_COMMAND_NAMES.includes(command.name.toLowerCase())) continue;
        const forms = [command.name, ...command.aliases].map((form) => form.toLowerCase());
        if (bare && !forms.some((form) => form.startsWith(bare))) continue;
        list.push({
          key: 'command:' + command.name,
          group: 'Commands',
          kind: 'command',
          label: '/' + command.name,
          description: command.summary || command.usage,
          icon: 'command',
          command,
        });
      }
    }

    return list.slice(0, MAX_RESULTS);
  }, [commands, query, sessions]);

  const groups = useMemo(() => {
    const order: Array<PaletteRow['group']> = ['Sessions', 'Actions', 'Commands'];
    return order
      .map((label) => ({
        label,
        items: rows.map((row, index) => ({ row, index })).filter((entry) => entry.row.group === label),
      }))
      .filter((group) => group.items.length > 0);
  }, [rows]);

  const activeIndex = rows.length === 0 ? 0 : Math.min(active, rows.length - 1);

  useEffect(() => {
    if (!open) return;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuery('');
    setActive(0);
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      const target = returnFocusRef.current;
      returnFocusRef.current = null;
      if (target && target.isConnected) target.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const node = listRef.current?.querySelector<HTMLElement>('.palette-item.active');
    node?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, open, rows]);

  const run = (command: CommandSpec, args: string[]) => {
    onRunCommand(command, args);
    onClose();
  };

  const activate = (row: PaletteRow) => {
    if (row.kind === 'session') {
      onSelectSession(row.sessionId);
      onClose();
      return;
    }
    if (row.kind === 'action') {
      if (row.actionId === 'new') onNewSession();
      else onOpenSettings();
      onClose();
      return;
    }
    const inlineIndex = query.indexOf(' ');
    const args = inlineIndex >= 0 ? query.slice(inlineIndex + 1).trim().split(/\s+/).filter(Boolean) : [];
    run(row.command, args);
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
        aria-label="Search sessions and actions"
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
            placeholder="Search sessions, actions…"
            aria-label="Search sessions and actions"
            role="combobox"
            aria-autocomplete="list"
            aria-controls={RESULTS_ID}
            aria-expanded
            aria-activedescendant={rows[activeIndex] ? optionId(rows[activeIndex]) : undefined}
            autoComplete="off"
            spellCheck={false}
          />
        </div>

        <div className="palette-results" role="listbox" aria-label="Results" ref={listRef} id={RESULTS_ID}>
          {rows.length === 0 ? (
            <div className="palette-empty">No matches</div>
          ) : (
            groups.map((group) => (
              <div className="palette-group" role="group" aria-label={group.label} key={group.label}>
                <div className="palette-group-label">{group.label}</div>
                {group.items.map(({ row, index }) => (
                  <div
                    key={row.key}
                    id={optionId(row)}
                    className={'palette-item ' + (index === activeIndex ? 'active' : '')}
                    role="option"
                    aria-selected={index === activeIndex}
                    onMouseEnter={() => setActive(index)}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => activate(row)}
                  >
                    <span className="palette-icon">
                      <Icon name={row.icon} size={15} />
                    </span>
                    <span className="palette-copy">
                      <span className="palette-name">{row.label}</span>
                      {row.description ? <span className="palette-desc">{row.description}</span> : null}
                    </span>
                  </div>
                ))}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
