import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { SessionSummary } from '../types';
import { groupLabel, sessionTitle, shortAgo } from '../lib/format';
import { BrandMark, Icon } from './Icon';

interface SidebarProps {
  sessions: SessionSummary[];
  total: number;
  hasMore: boolean;
  loadingMore: boolean;
  includeArchived: boolean;
  onLoadMore: () => void;
  onToggleArchived: (value: boolean) => void;
  activeId: string | null;
  collapsed: boolean;
  mobileOpen: boolean;
  query: string;
  onQueryChange: (value: string) => void;
  onToggleCollapse: () => void;
  onCloseMobile: () => void;
  onSelect: (sessionId: string) => void;
  onArchiveSession: (session: SessionSummary, archived: boolean) => void;
  onRenameSession: (session: SessionSummary, title: string) => void;
  onNew: () => void;
  onOpenSettings: () => void;
  onOpenInspector: () => void;
}

const GROUPS = ['Today', 'Yesterday', 'Previous 7 days', 'Older'] as const;

function RenameDialog({
  session,
  onCancel,
  onSubmit,
}: {
  session: SessionSummary;
  onCancel: () => void;
  onSubmit: (title: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState(sessionTitle(session.title));
  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.focus();
    input.select();
  }, []);
  const submit = () => {
    const title = value.trim();
    if (!title) return;
    onSubmit(title);
  };
  return (
    <div className="dialog-backdrop" onClick={(event) => event.target === event.currentTarget && onCancel()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label="Rename session">
        <div className="dialog-head">
          <span className="dialog-title">Rename session</span>
          <button className="icon-button tiny" type="button" onClick={onCancel} aria-label="Close">
            <Icon name="x" size={14} />
          </button>
        </div>
        <input
          ref={inputRef}
          className="dialog-input"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              submit();
            } else if (event.key === 'Escape') {
              event.preventDefault();
              onCancel();
            }
          }}
          aria-label="Session title"
          spellCheck={false}
        />
        <div className="dialog-actions">
          <button className="button ghost small" type="button" onClick={onCancel}>
            Cancel
          </button>
          <button className="button primary small" type="button" disabled={!value.trim()} onClick={submit}>
            Save
          </button>
        </div>
      </div>
    </div>
  );
}

interface SessionRowProps {
  session: SessionSummary;
  active: boolean;
  onSelect: () => void;
  onArchiveSession: (session: SessionSummary, archived: boolean) => void;
  onRename: (session: SessionSummary) => void;
}

function SessionRow({ session, active, onSelect, onArchiveSession, onRename }: SessionRowProps) {
  const moreRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menu, setMenu] = useState<{ top: number; left: number } | null>(null);
  const close = useCallback(() => setMenu(null), []);

  useEffect(() => {
    if (!menu) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (menuRef.current?.contains(target) || moreRef.current?.contains(target)) return;
      close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    window.addEventListener('resize', close);
    window.addEventListener('scroll', close, true);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('resize', close);
      window.removeEventListener('scroll', close, true);
    };
  }, [menu, close]);

  const toggleMenu = () => {
    if (menu) {
      close();
      return;
    }
    const rect = moreRef.current?.getBoundingClientRect();
    if (!rect) return;
    const width = 196;
    setMenu({
      top: Math.min(rect.bottom + 4, window.innerHeight - 96),
      left: Math.max(8, Math.min(rect.right - width - 4, window.innerWidth - width - 8)),
    });
  };

  return (
    <div className={`session-row ${active ? 'active' : ''}`}>
      <button
        className={`session-item ${active ? 'active' : ''}`}
        type="button"
        aria-current={active ? 'page' : undefined}
        onClick={onSelect}
        onDoubleClick={() => onRename(session)}
        title={session.cwd}
      >
        <span className={`session-dot ${session.busy ? 'busy' : ''} ${session.archived ? 'archived' : ''}`} />
        <span className="session-copy">
          <span className="session-name">{sessionTitle(session.title)}</span>
          <span className="session-meta">
            {session.archived ? 'Archived · ' : ''}
            {session.turns} {session.turns === 1 ? 'turn' : 'turns'} · {session.preset || 'standard'}
          </span>
        </span>
        <span className="session-time">{shortAgo(session.updated_at)}</span>
      </button>
      <button
        ref={moreRef}
        className={`session-more ${menu ? 'open' : ''}`}
        type="button"
        aria-label={`Actions for ${sessionTitle(session.title)}`}
        aria-haspopup="menu"
        aria-expanded={menu !== null}
        title="Session actions"
        onClick={toggleMenu}
      >
        <Icon name="more" size={15} />
      </button>
      {menu
        ? createPortal(
            <div
              ref={menuRef}
              className="session-menu"
              role="menu"
              aria-label={`Actions for ${sessionTitle(session.title)}`}
              style={{ top: menu.top, left: menu.left }}
            >
              <button
                className="session-menu-item"
                type="button"
                role="menuitem"
                onClick={() => {
                  close();
                  onRename(session);
                }}
              >
                <Icon name="pencil" size={15} />
                <span>Rename</span>
              </button>
              <button
                className="session-menu-item"
                type="button"
                role="menuitem"
                onClick={() => {
                  close();
                  onArchiveSession(session, !session.archived);
                }}
              >
                <Icon name={session.archived ? 'refresh' : 'archive'} size={15} />
                <span>{session.archived ? 'Unarchive' : 'Archive'}</span>
              </button>
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}

export function Sidebar({
  sessions,
  total,
  hasMore,
  loadingMore,
  includeArchived,
  onLoadMore,
  onToggleArchived,
  activeId,
  collapsed,
  mobileOpen,
  query,
  onQueryChange,
  onToggleCollapse,
  onCloseMobile,
  onSelect,
  onArchiveSession,
  onRenameSession,
  onNew,
  onOpenSettings,
  onOpenInspector,
}: SidebarProps) {
  const rootRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [renaming, setRenaming] = useState<SessionSummary | null>(null);

  useEffect(() => {
    if (!mobileOpen) return;
    const root = rootRef.current;
    if (!root) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusable = Array.from(
      root.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((element) => element.getClientRects().length > 0);

    (searchRef.current ?? focusable[0])?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !root.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !root.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      previous?.focus();
    };
  }, [mobileOpen]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list = sessions.filter((session) => includeArchived || !session.archived);
    if (!needle) return list;
    return list.filter(
      (session) =>
        sessionTitle(session.title).toLowerCase().includes(needle) ||
        session.session_id.toLowerCase().includes(needle) ||
        session.cwd.toLowerCase().includes(needle) ||
        session.preset.toLowerCase().includes(needle),
    );
  }, [includeArchived, query, sessions]);

  const groups = useMemo(() => {
    const buckets = new Map<string, SessionSummary[]>();
    for (const session of filtered) {
      const label = groupLabel(session.updated_at);
      const bucket = buckets.get(label) ?? [];
      bucket.push(session);
      buckets.set(label, bucket);
    }
    return GROUPS.map((label) => ({ label, items: buckets.get(label) ?? [] })).filter(
      (group) => group.items.length > 0,
    );
  }, [filtered]);

  const renameDialog = renaming ? (
    <RenameDialog
      session={renaming}
      onCancel={() => setRenaming(null)}
      onSubmit={(title) => {
        onRenameSession(renaming, title);
        setRenaming(null);
      }}
    />
  ) : null;

  if (collapsed) {
    return (
      <aside className="sidebar rail" aria-label="Sidebar" ref={rootRef}>
        <button className="rail-logo" type="button" onClick={onToggleCollapse} title="Expand sidebar">
          <BrandMark size={28} />
        </button>
        <div className="rail-stack">
          <button className="rail-button" type="button" onClick={onNew} title="New session">
            <Icon name="plus" size={19} />
          </button>
          <button className="rail-button" type="button" onClick={onOpenInspector} title="Session details">
            <Icon name="info" size={18} />
          </button>
          <button className="rail-button" type="button" onClick={onOpenSettings} title="Settings">
            <Icon name="settings" size={18} />
          </button>
        </div>
        <div className="rail-bottom">
          <button className="avatar avatar-small" type="button" onClick={onOpenSettings} title="You">
            Y
          </button>
        </div>
      </aside>
    );
  }

  return (
    <aside className={`sidebar ${mobileOpen ? 'mobile-open' : ''}`} aria-label="Sidebar" ref={rootRef}>
      <div className="sidebar-top">
        <button className="brand" type="button" onClick={onToggleCollapse} title="Collapse sidebar">
          <BrandMark size={26} className="brand-mark" />
          <span className="brand-name">DeepPyro</span>
        </button>
        <button className="icon-button subtle" type="button" onClick={onToggleCollapse} title="Collapse sidebar">
          <Icon name="panel-left" size={17} />
        </button>
      </div>

      <div className="sidebar-actions">
        <button className="new-session" type="button" onClick={onNew}>
          <Icon name="plus" size={17} />
          <span>New session</span>
        </button>
        <label className="session-search">
          <Icon name="search" size={15} />
          <input
            ref={searchRef}
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="Search sessions"
            aria-label="Search sessions"
            spellCheck={false}
          />
        </label>
      </div>

      <div className="session-section-head">
        <span>Sessions</span>
        <span className="session-section-count">{total}</span>
      </div>

      <div className="session-scroll">
        {groups.length === 0 ? (
          <div className="session-empty">{query ? 'No sessions match.' : 'No sessions yet.'}</div>
        ) : (
          groups.map((group) => (
            <section className="session-group" key={group.label}>
              <div className="section-label">
                <span>{group.label}</span>
                <button className="icon-button tiny" type="button" onClick={onNew} title="New session">
                  <Icon name="plus" size={13} />
                </button>
              </div>
              {group.items.map((session) => (
                <SessionRow
                  key={session.session_id}
                  session={session}
                  active={session.session_id === activeId}
                  onSelect={() => {
                    onSelect(session.session_id);
                    onCloseMobile();
                  }}
                  onArchiveSession={onArchiveSession}
                  onRename={setRenaming}
                />
              ))}
            </section>
          ))
        )}
      </div>

      <div className="session-list-footer">
        <label className="session-archived-toggle">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => onToggleArchived(event.target.checked)}
          />
          <span>Show archived</span>
        </label>
        {hasMore ? (
          <button className="load-more" type="button" onClick={onLoadMore} disabled={loadingMore}>
            {loadingMore ? 'Loading…' : 'Load more'}
          </button>
        ) : null}
      </div>

      <div className="sidebar-footer">
        <button className="user-row" type="button" onClick={onOpenSettings} title="You — local runtime">
          <span className="avatar">Y</span>
          <span className="user-copy">
            <span className="user-name">You</span>
            <span className="user-meta">local runtime</span>
          </span>
        </button>
        <button className="icon-button subtle" type="button" onClick={onOpenSettings} title="Settings">
          <Icon name="settings" size={17} />
        </button>
      </div>
      {renameDialog}
    </aside>
  );
}
