import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { CommandSpec, SessionSummary } from '../types';
import { sessionTitle } from '../lib/format';
import {
  commandNeedsArgument,
  inlineCommandArgs,
  missingArgumentMessage,
  parseCommandArgs,
} from '../lib/commandArgs';
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

/** Id shared by the results listbox and the input that controls it. */
const RESULTS_ID = 'palette-results';

/** Commands duplicated by a palette action; the action is the canonical entry point. */
const ACTION_COMMAND_NAMES = ['new', 'settings'];

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

/** DOM id for a result row, referenced by aria-activedescendant. */
function optionId(row: PaletteRow): string {
  return 'palette-option-' + row.key.replace(/[^a-zA-Z0-9_-]+/g, '-');
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
  const [argCommand, setArgCommand] = useState<CommandSpec | null>(null);
  const [argText, setArgText] = useState('');
  const [argError, setArgError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const rows = useMemo<PaletteRow[]>(() => {
    const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const list: PaletteRow[] = [];

    for (const action of ACTIONS) {
      const haystack = (action.name + ' ' + action.description).toLowerCase();
      if (!matchTokens(haystack, tokens)) continue;
      list.push({
        key: 'action:' + action.id,
        group: 'Actions',
        kind: 'action',
        name: action.name,
        description: action.description,
        icon: action.icon,
        actionId: action.id,
      });
    }

    for (const command of commands) {
      if (!command.name || ACTION_COMMAND_NAMES.includes(command.name.toLowerCase())) continue;
      if (!matchesCommand(command, tokens)) continue;
      list.push({
        key: 'command:' + command.name,
        group: 'Commands',
        kind: 'command',
        name: '/' + command.name,
        description: command.summary || command.usage,
        icon: 'command',
        command,
      });
    }

    for (const session of sessions) {
      if (session.archived) continue;
      const title = sessionTitle(session.title);
      const haystack = (title + ' ' + session.cwd).toLowerCase();
      if (!matchTokens(haystack, tokens)) continue;
      list.push({
        key: 'session:' + session.session_id,
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
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuery('');
    setActive(0);
    setArgCommand(null);
    setArgText('');
    setArgError(null);
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      const target = returnFocusRef.current;
      returnFocusRef.current = null;
      if (target && target.isConnected) target.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open || argCommand) return;
    const node = listRef.current?.querySelector<HTMLElement>('.palette-item.active');
    node?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, argCommand, open, rows]);

  const beginArgumentStep = (command: CommandSpec, initialText: string, initialError: string | null) => {
    setArgCommand(command);
    setArgText(initialText);
    setArgError(initialError);
    inputRef.current?.focus();
  };

  const leaveArgumentStep = () => {
    setArgCommand(null);
    setArgText('');
    setArgError(null);
    inputRef.current?.focus();
  };

  const runCommand = (command: CommandSpec, args: string[]) => {
    onRunCommand(command, args);
    onClose();
  };

  const activate = (row: PaletteRow) => {
    if (row.kind === 'action') {
      if (row.actionId === 'new') onNewSession();
      else onOpenSettings();
      onClose();
      return;
    }
    if (row.kind === 'session') {
      onSelectSession(row.sessionId);
      onClose();
      return;
    }
    const command = row.command;
    if (!commandNeedsArgument(command)) {
      runCommand(command, []);
      return;
    }
    const inline = inlineCommandArgs(command, query);
    if (!inline) {
      beginArgumentStep(command, '', null);
      return;
    }
    if (inline.parsed.error) {
      beginArgumentStep(command, inline.raw, inline.parsed.error);
      return;
    }
    if (inline.parsed.args.length === 0) {
      beginArgumentStep(command, inline.raw, null);
      return;
    }
    runCommand(command, inline.parsed.args);
  };

  const submitArguments = () => {
    const command = argCommand;
    if (!command) return;
    const parsed = parseCommandArgs(argText);
    if (parsed.error) {
      setArgError(parsed.error);
      return;
    }
    if (parsed.args.length === 0) {
      setArgError(missingArgumentMessage(command));
      return;
    }
    runCommand(command, parsed.args);
  };

  const handleChange = (value: string) => {
    if (argCommand) {
      setArgText(value);
      if (argError) setArgError(null);
      return;
    }
    setQuery(value);
    setActive(0);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Tab') {
      event.preventDefault();
      inputRef.current?.focus();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      if (argCommand) leaveArgumentStep();
      else onClose();
      return;
    }
    if (argCommand) {
      if (event.key === 'Enter') {
        event.preventDefault();
        submitArguments();
        return;
      }
      if (event.key === 'Backspace' && argText === '' && !event.nativeEvent.isComposing) {
        event.preventDefault();
        leaveArgumentStep();
        return;
      }
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

  const activeOption = rows[activeIndex];
  const activeDescendant = !argCommand && activeOption ? optionId(activeOption) : undefined;

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
            <Icon name={argCommand ? 'command' : 'search'} size={16} />
          </span>
          <input
            ref={inputRef}
            className="palette-input"
            value={argCommand ? argText : query}
            onChange={(event) => handleChange(event.target.value)}
            placeholder={
              argCommand
                ? argCommand.usage || 'Arguments for /' + argCommand.name
                : 'Search actions, commands and sessions'
            }
            aria-label={
              argCommand ? 'Arguments for /' + argCommand.name : 'Search actions, commands and sessions'
            }
            role={argCommand ? undefined : 'combobox'}
            aria-autocomplete={argCommand ? undefined : 'list'}
            aria-controls={argCommand ? undefined : RESULTS_ID}
            aria-expanded={argCommand ? undefined : true}
            aria-activedescendant={activeDescendant}
            autoComplete="off"
            spellCheck={false}
          />
        </div>

        <div
          className="palette-results"
          role={argCommand ? 'group' : 'listbox'}
          aria-label={argCommand ? 'Command arguments' : 'Results'}
          ref={listRef}
          id={argCommand ? undefined : RESULTS_ID}
        >
          {argCommand ? (
            <>
              <div className="palette-group">
                <div className="palette-group-label">Command</div>
                <div className="palette-item active">
                  <span className="palette-icon">
                    <Icon name="command" size={15} />
                  </span>
                  <span className="palette-copy">
                    <span className="palette-name">{'/' + argCommand.name}</span>
                    <span className="palette-desc">{argCommand.usage || argCommand.summary}</span>
                  </span>
                </div>
              </div>
              <div className="palette-group">
                <div className="palette-group-label">Arguments</div>
                {argError ? (
                  <div className="inline-error" role="alert">
                    <Icon name="alert" size={14} />
                    <span>{argError}</span>
                  </div>
                ) : (
                  <div className="palette-empty">
                    Type the arguments, then press Enter to run. Esc or Backspace returns to the command list.
                  </div>
                )}
              </div>
            </>
          ) : rows.length === 0 ? (
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
          {argCommand ? (
            <>
              <span>
                <kbd className="palette-kbd">↵</kbd> run
              </span>
              <span>
                <kbd className="palette-kbd">esc</kbd> back
              </span>
              <span>
                <kbd className="palette-kbd">⌫</kbd> back when empty
              </span>
            </>
          ) : (
            <>
              <span>
                <kbd className="palette-kbd">↑↓</kbd> navigate
              </span>
              <span>
                <kbd className="palette-kbd">↵</kbd> run
              </span>
              <span>
                <kbd className="palette-kbd">esc</kbd> close
              </span>
            </>
          )}
        </footer>
      </div>
    </div>
  );
}
