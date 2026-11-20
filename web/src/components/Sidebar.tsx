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
  onNew: () => void;
  onOpenPalette: () => void;
  onOpenSettings: () => void;
  onOpenPlugins: () => void;
  onOpenInspector: () => void;
  commandCount: number;
  pluginCount: number;
}

const GROUPS = ['Today', 'Yesterday', 'Previous 7 days', 'Older'] as const;

interface SessionRowProps {
  session: SessionSummary;
  active: boolean;
  onSelect: () => void;
  onArchiveSession: (session: SessionSummary, archived: boolean) => void;
}

function SessionRow({ session, active, onSelect, onArchiveSession }: SessionRowProps) {
  const moreRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menu, setMenu] = useState<{ top: number; left: number } | null>(null);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<number | null>(null);
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

  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    },
    [],
  );

  const toggleMenu = () => {
    if (menu) {
      close();
      return;
    }
    const rect = moreRef.current?.getBoundingClientRect();
    if (!rect) return;
    const width = 192;
    setMenu({
      top: Math.min(rect.bottom + 4, window.innerHeight - 96),
      left: Math.max(8, Math.min(rect.right - width - 4, window.innerWidth - width - 8)),
    });
  };

  const archive = () => {
    close();
    onArchiveSession(session, !session.archived);
  };

  const copyId = async () => {
    try {
      await navigator.clipboard.writeText(session.session_id);
      setCopied(true);
      copyTimer.current = window.setTimeout(() => {
        setCopied(false);
        close();
      }, 700);
    } catch {
      close();
    }
  };

  return (
    <div className={`session-row ${active ? 'active' : ''}`}>
      <button
        className={`session-item ${active ? 'active' : ''}`}
        type="button"
        aria-current={active ? 'page' : undefined}
        onClick={onSelect}
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
              <button className="session-menu-item" type="button" role="menuitem" onClick={archive}>
                <Icon name={session.archived ? 'refresh' : 'archive'} size={15} />
                <span>{session.archived ? 'Unarchive session' : 'Archive session'}</span>
              </button>
              <button className="session-menu-item" type="button" role="menuitem" onClick={() => void copyId()}>
                <Icon name={copied ? 'check' : 'copy'} size={15} />
                <span>{copied ? 'Session ID copied' : 'Copy session ID'}</span>
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
  onNew,
  onOpenPalette,
  onOpenSettings,
  onOpenPlugins,
  onOpenInspector,
  commandCount,
  pluginCount,
}: SidebarProps) {
  const rootRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

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

  if (collapsed) {
    return (
      <aside className="sidebar rail" aria-label="Sidebar" ref={rootRef}>
        <button className="rail-logo" type="button" onClick={onToggleCollapse} title="Expand sidebar">
          <BrandMark size={28} />
        </button>
        <div className="rail-stack">
          <button className="rail-button" type="button" onClick={onNew} title="New session (Ctrl+N)">
            <Icon name="plus" size={19} />
          </button>
          <button
            className={`rail-button ${mobileOpen ? 'active' : ''}`}
            type="button"
            onClick={onOpenPalette}
            title="Commands (Ctrl+K)"
          >
            <Icon name="terminal" size={18} />
          </button>
          <button className="rail-button" type="button" onClick={onOpenInspector} title="Session details">
            <Icon name="message" size={18} />
          </button>
          <button className="rail-button" type="button" onClick={onOpenSettings} title="Settings (Ctrl+,)">
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
    <aside
      className={`sidebar ${mobileOpen ? 'mobile-open' : ''}`}
      aria-label="Sidebar"
      ref={rootRef}
    >
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
          <kbd>Ctrl N</kbd>
        </button>
        <label className="session-search">
          <Icon name="search" size={15} />
          <input
            ref={searchRef}
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="Find a session"
            aria-label="Find a session"
            spellCheck={false}
          />
          <kbd>/</kbd>
        </label>
      </div>

      <nav className="primary-nav" aria-label="Primary">
        <button
          className="nav-item active"
          type="button"
          onClick={() => searchRef.current?.focus()}
          aria-current="page"
        >
          <Icon name="message" size={16} />
          <span>Sessions</span>
          <span className="nav-count">{total}</span>
        </button>
        <button className="nav-item" type="button" onClick={onOpenPalette}>
          <Icon name="terminal" size={16} />
          <span>Commands</span>
          <span className="nav-count">{commandCount}</span>
        </button>
        <button className="nav-item" type="button" onClick={onOpenPlugins}>
          <Icon name="plug" size={16} />
          <span>Plugins</span>
          <span className="nav-count">{pluginCount}</span>
        </button>
      </nav>

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
    </aside>
  );
}
