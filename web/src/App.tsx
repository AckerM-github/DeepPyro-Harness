import { useCallback, useEffect, useRef, useState } from 'react';
import { api, subscribeEvents, takeTokenFromUrl } from './api';
import type {
  Approval,
  Bootstrap,
  ChatMessage,
  CommandSpec,
  CommandResult,
  ConnectionState,
  LiveApply,
  ProviderProbe,
  SessionSummary,
  SettingsPayload,
  Timeline,
  WebEvent,
} from './types';
import { Sidebar } from './components/Sidebar';
import { Transcript } from './components/Chat';
import { Composer } from './components/Composer';
import { Inspector } from './components/Inspector';
import { Settings, type SectionId } from './components/Settings';
import { Palette } from './components/Palette';
import { NewSession } from './components/NewSession';
import { Icon } from './components/Icon';

const SETTINGS_SECTIONS: Record<string, SectionId> = {
  provider: 'provider',
  agent: 'agent',
  permission: 'permissions',
  permissions: 'permissions',
  command: 'commands',
  commands: 'commands',
  plugin: 'plugins',
  plugins: 'plugins',
  session: 'sessions',
  sessions: 'sessions',
  web: 'web',
  about: 'about',
};

const CONNECTION_LABEL: Record<ConnectionState, string> = {
  connecting: 'Connecting',
  live: 'Live',
  reconnecting: 'Reconnecting',
  offline: 'Offline',
};

function sessionIdFromPath(): string | null {
  const match = window.location.pathname.match(/^\/c\/([^/]+)/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

function messageId(): string {
  return `stream-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
}

function newAssistantMessage(): ChatMessage {
  return {
    id: messageId(),
    role: 'assistant',
    created_at: Date.now(),
    text: '',
    reasoning: '',
    source: { kind: 'runtime' },
    tool_call_id: null,
    is_error: false,
    tool_calls: [],
    images: [],
    has_text: false,
    streaming: true,
  };
}

function lastAssistantIndex(messages: ChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'assistant') return index;
  }
  return -1;
}

function appendText(messages: ChatMessage[], text: string): ChatMessage[] {
  const index = lastAssistantIndex(messages);
  if (index < 0) {
    const created = { ...newAssistantMessage(), text, has_text: true };
    return [...messages, created];
  }
  const current = messages[index];
  if (current.role === 'assistant' && (current.streaming || current.text === '')) {
    const next = [...messages];
    next[index] = { ...current, text: current.text + text, has_text: true, streaming: true };
    return next;
  }
  return [...messages, { ...newAssistantMessage(), text, has_text: true }];
}

function appendThinking(messages: ChatMessage[], text: string): ChatMessage[] {
  const index = lastAssistantIndex(messages);
  if (index < 0) {
    return [...messages, { ...newAssistantMessage(), reasoning: text, streaming: true }];
  }
  const next = [...messages];
  next[index] = { ...messages[index], reasoning: messages[index].reasoning + text, streaming: true };
  return next;
}

function applyEvent(timeline: Timeline, event: WebEvent): Timeline {
  if (event.session_id !== timeline.session_id) return timeline;
  switch (event.type) {
    case 'status':
      return {
        ...timeline,
        busy: event.phase === 'thinking',
        messages:
          event.phase === 'thinking'
            ? timeline.messages
            : timeline.messages.map((message) => ({ ...message, streaming: false })),
      };
    case 'text':
      return { ...timeline, busy: true, messages: appendText(timeline.messages, event.text) };
    case 'thinking':
      return { ...timeline, busy: true, messages: appendThinking(timeline.messages, event.text) };
    case 'tool_call': {
      const index = lastAssistantIndex(timeline.messages);
      if (index < 0) {
        const message = newAssistantMessage();
        message.tool_calls = [
          { id: event.call_id, name: event.name, arguments: event.arguments, result: null, is_error: false, pending: true },
        ];
        return { ...timeline, busy: true, messages: [...timeline.messages, message] };
      }
      const next = [...timeline.messages];
      const current = next[index];
      next[index] = {
        ...current,
        streaming: true,
        tool_calls: [
          ...current.tool_calls,
          { id: event.call_id, name: event.name, arguments: event.arguments, result: null, is_error: false, pending: true },
        ],
      };
      return { ...timeline, busy: true, messages: next };
    }
    case 'tool_result':
      return {
        ...timeline,
        busy: true,
        messages: timeline.messages.map((message) => ({
          ...message,
          tool_calls: message.tool_calls.map((call) =>
            call.id === event.call_id
              ? { ...call, result: event.text, is_error: event.is_error, pending: false }
              : call,
          ),
        })),
      };
    case 'final': {
      const index = lastAssistantIndex(timeline.messages);
      if (index < 0) {
        const message = { ...newAssistantMessage(), text: event.text, has_text: Boolean(event.text), streaming: false };
        return { ...timeline, busy: false, messages: [...timeline.messages, message] };
      }
      const next = [...timeline.messages];
      next[index] = { ...next[index], text: event.text, has_text: Boolean(event.text), streaming: false };
      return { ...timeline, busy: false, messages: next };
    }
    case 'error':
      // A failed turn is terminal even if the transport never delivered the
      // matching status event. Clear busy/streaming so the composer unlocks.
      return {
        ...timeline,
        busy: false,
        messages: timeline.messages.map((message) => ({ ...message, streaming: false })),
      };
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

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return 'Something went wrong.';
}

function settingsSavedMessage(live: LiveApply | undefined, fallback: string): [string, 'ok' | 'error'] {
  if (live?.detail?.startsWith('live apply failed')) return [live.detail, 'error'];
  if (live?.deferred) return [fallback + ' Applies after the current turn.', 'ok'];
  if (live?.restart_required?.length) {
    return [fallback + ' Restart required: ' + live.restart_required.join(', ') + '.', 'ok'];
  }
  return [fallback, 'ok'];
}

export function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionTotal, setSessionTotal] = useState(0);
  const [sessionHasMore, setSessionHasMore] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [timelineLoading, setTimelineLoading] = useState(true);
  const [timelineError, setTimelineError] = useState<string | null>(null);
  const [turnError, setTurnError] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [connection, setConnection] = useState<ConnectionState>('connecting');
  const [composer, setComposer] = useState('');
  const [bootError, setBootError] = useState<string | null>(null);
  const [settingsSection, setSettingsSection] = useState<SectionId>('provider');

  const [isMobile, setIsMobile] = useState(() => window.innerWidth < 768);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => window.innerWidth >= 768 && window.innerWidth < 1024,
  );
  const [mobileOpen, setMobileOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [inspectorOpen, setInspectorOpen] = useState(() => window.innerWidth >= 1280);

  const [newOpen, setNewOpen] = useState(false);
  const [newCwd, setNewCwd] = useState<string | null>(null);
  const [newBusy, setNewBusy] = useState(false);
  const [newError, setNewError] = useState<string | null>(null);

  const [paletteOpen, setPaletteOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [probe, setProbe] = useState<ProviderProbe | null>(null);

  const [resumeBusy, setResumeBusy] = useState(false);
  const [toast, setToast] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);

  const activeRef = useRef<string | null>(null);
  const sessionsRequestRef = useRef(0);
  const sessionsQueryRef = useRef('');
  const includeArchivedRef = useRef(false);
  const journalRef = useRef(0);
  const requestRef = useRef(0);
  const initRef = useRef(false);
  const refreshTimer = useRef<number | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const toastTimer = useRef<number | null>(null);

  const notify = useCallback((text: string, kind: 'ok' | 'error' = 'ok') => {
    setToast({ kind, text });
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4200);
  }, []);

  const commitTimeline = useCallback((next: Timeline) => {
    journalRef.current = next.journal_seq ?? journalRef.current;
    setTimeline(next);
  }, []);

  const refreshSessions = useCallback(async () => {
    const ticket = ++sessionsRequestRef.current;
    const needle = sessionsQueryRef.current.trim();
    try {
      const result = await api.sessions({
        query: needle || undefined,
        include_archived: includeArchivedRef.current,
      });
      if (ticket !== sessionsRequestRef.current) return;
      setSessions(result.sessions);
      setSessionTotal(result.total);
      setSessionHasMore(result.has_more);
    } catch {
      // The transcript still works when the list is briefly unavailable.
    }
  }, []);

  const loadMoreSessions = useCallback(async () => {
    if (sessionsLoading || !sessionHasMore) return;
    const ticket = sessionsRequestRef.current;
    const needle = sessionsQueryRef.current.trim();
    setSessionsLoading(true);
    try {
      const result = await api.sessions({
        offset: sessions.length,
        query: needle || undefined,
        include_archived: includeArchivedRef.current,
      });
      if (ticket !== sessionsRequestRef.current) return;
      setSessions((current) => [...current, ...result.sessions]);
      setSessionTotal(result.total);
      setSessionHasMore(result.has_more);
    } catch {
      // Keep the already loaded page; the next refresh can retry.
    } finally {
      setSessionsLoading(false);
    }
  }, [sessionHasMore, sessions.length, sessionsLoading]);

  const toggleArchived = useCallback(
    (value: boolean) => {
      includeArchivedRef.current = value;
      setIncludeArchived(value);
      void refreshSessions();
    },
    [refreshSessions],
  );

  const loadApprovals = useCallback(async (sessionId: string) => {
    try {
      const result = await api.approvals(sessionId);
      if (activeRef.current === sessionId) setApprovals(result.approvals);
    } catch {
      // Approvals reappear on the next event.
    }
  }, []);

  const loadTimeline = useCallback(
    async (sessionId: string, quiet = false) => {
      const ticket = ++requestRef.current;
      if (!quiet) setTimelineLoading(true);
      try {
        const next = await api.timeline(sessionId);
        if (ticket !== requestRef.current || activeRef.current !== sessionId) return;
        setTimelineError(null);
        commitTimeline(next);
        setTimelineLoading(false);
        void loadApprovals(sessionId);
      } catch (error) {
        if (ticket !== requestRef.current) return;
        setTimelineError(errorMessage(error));
        setTimelineLoading(false);
      }
    },
    [commitTimeline, loadApprovals],
  );

  const scheduleRefresh = useCallback(
    (sessionId: string) => {
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
      refreshTimer.current = window.setTimeout(() => {
        refreshTimer.current = null;
        void loadTimeline(sessionId, true);
        void refreshSessions();
      }, 180);
    },
    [loadTimeline, refreshSessions],
  );

  const openSession = useCallback(
    async (sessionId: string) => {
      activeRef.current = sessionId;
      journalRef.current = 0;
      setActiveId(sessionId);
      setTimeline(null);
      setApprovals([]);
      setTimelineError(null);
      setTurnError(null);
      setTimelineLoading(true);
      setMobileOpen(false);
      window.history.replaceState(null, '', `/c/${encodeURIComponent(sessionId)}`);
      try {
        let next = await api.timeline(sessionId);
        if (!next.active && next.readable) {
          try {
            await api.resumeSession(sessionId);
            next = await api.timeline(sessionId);
          } catch {
            // The session is still shown read-only; do not hide the transcript.
          }
        }
        if (activeRef.current !== sessionId) return;
        setTimelineError(null);
        commitTimeline(next);
        setTimelineLoading(false);
        void loadApprovals(sessionId);
      } catch (error) {
        if (activeRef.current !== sessionId) return;
        setTimelineError(errorMessage(error));
        setTimelineLoading(false);
      }
    },
    [commitTimeline, loadApprovals],
  );

  useEffect(() => {
    if (initRef.current) return;
    initRef.current = true;
    takeTokenFromUrl();
    void (async () => {
      try {
        const boot = await api.bootstrap();
        setBootstrap(boot);
        setSessions(boot.sessions);
        setSessionTotal(boot.sessions.length);
        const candidate = sessionIdFromPath() ?? boot.active_session_id ?? boot.sessions[0]?.session_id ?? null;
        if (candidate) {
          await openSession(candidate);
        } else {
          setTimelineLoading(false);
        }
      } catch (error) {
        setBootError(errorMessage(error));
        setTimelineLoading(false);
      }
    })();
  }, [openSession]);

  useEffect(() => {
    if (!activeId) return;
    setConnection('connecting');
    const handle = subscribeEvents(
      activeId,
      journalRef.current,
      (event) => {
        if (event.session_id !== activeId) return;
        setTimeline((current) => (current ? applyEvent(current, event) : current));
        if (event.type === 'approval') void loadApprovals(activeId);
        if (event.type === 'error') {
          setTurnError(event.message);
          notify(event.message, 'error');
          scheduleRefresh(activeId);
        }
        if (event.type === 'final' || (event.type === 'status' && event.phase !== 'thinking')) {
          scheduleRefresh(activeId);
        }
      },
      setConnection,
    );
    return () => handle.close();
  }, [activeId, loadApprovals, notify, scheduleRefresh]);

  useEffect(() => {
    const element = scrollerRef.current;
    if (element && stickRef.current) element.scrollTop = element.scrollHeight;
  }, [timeline]);

  useEffect(() => {
    return () => {
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
      if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    };
  }, []);

  const openSettings = useCallback(async (section: SectionId = 'provider') => {
    setSettingsSection(section);
    setSettingsOpen(true);
    setSettingsLoading(true);
    setSettingsError(null);
    try {
      const payload = await api.settings();
      setSettings(payload);
    } catch (error) {
      setSettingsError(errorMessage(error));
    } finally {
      setSettingsLoading(false);
    }
  }, []);

  const reloadSettings = useCallback(async () => {
    setSettingsLoading(true);
    setSettingsError(null);
    try {
      const payload = await api.settings();
      setSettings(payload);
    } catch (error) {
      setSettingsError(errorMessage(error));
    } finally {
      setSettingsLoading(false);
    }
  }, []);

  useEffect(() => {
    sessionsQueryRef.current = query;
    const timer = window.setTimeout(() => void refreshSessions(), 220);
    return () => window.clearTimeout(timer);
  }, [query, refreshSessions]);

  useEffect(() => {
    const query = window.matchMedia('(max-width: 767px)');
    const sync = () => {
      setIsMobile(query.matches);
      if (query.matches) setSidebarCollapsed(false);
    };
    sync();
    query.addEventListener('change', sync);
    return () => query.removeEventListener('change', sync);
  }, []);

  useEffect(() => {
    if (!isMobile) return;
    document.body.classList.toggle('drawer-open', mobileOpen);
    return () => document.body.classList.remove('drawer-open');
  }, [isMobile, mobileOpen]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const mod = event.metaKey || event.ctrlKey;
      if (mod && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen((value) => !value);
      } else if (mod && event.key === ',') {
        event.preventDefault();
        void openSettings();
      } else if (mod && event.key.toLowerCase() === 'n') {
        event.preventDefault();
        setNewCwd(null);
        setNewError(null);
        setNewOpen(true);
      } else if (event.key === 'Escape') {
        setPaletteOpen(false);
        setNewOpen(false);
        setSettingsOpen(false);
        setMobileOpen(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [openSettings]);

  const handleSend = useCallback(async () => {
    const text = composer.trim();
    if (!text || !timeline || !timeline.active || timeline.busy) return;
    setComposer('');
    setTurnError(null);
    setTimeline((current) => (current ? { ...current, busy: true } : current));
    try {
      await api.sendMessage(timeline.session_id, text);
      scheduleRefresh(timeline.session_id);
      void refreshSessions();
    } catch (error) {
      setComposer(text);
      setTimeline((current) => (current ? { ...current, busy: false } : current));
      notify(errorMessage(error), 'error');
    }
  }, [composer, notify, refreshSessions, scheduleRefresh, timeline]);

  const handleStop = useCallback(async () => {
    if (!timeline) return;
    try {
      await api.interrupt(timeline.session_id);
      notify('Interrupt requested.');
      scheduleRefresh(timeline.session_id);
    } catch (error) {
      notify(errorMessage(error), 'error');
    }
  }, [notify, scheduleRefresh, timeline]);

  const handleDecide = useCallback(
    async (approvalId: string, decision: 'allow' | 'deny') => {
      try {
        await api.decideApproval(approvalId, decision);
        setApprovals((current) => current.filter((approval) => approval.approval_id !== approvalId));
        if (activeRef.current) scheduleRefresh(activeRef.current);
      } catch (error) {
        notify(errorMessage(error), 'error');
      }
    },
    [notify, scheduleRefresh],
  );

  const handleResume = useCallback(async () => {
    if (!timeline) return;
    setResumeBusy(true);
    try {
      await api.resumeSession(timeline.session_id);
      await loadTimeline(timeline.session_id, true);
      notify('Session resumed.');
      void refreshSessions();
    } catch (error) {
      notify(errorMessage(error), 'error');
    } finally {
      setResumeBusy(false);
    }
  }, [loadTimeline, notify, refreshSessions, timeline]);

  const handleSessionArchive = useCallback(
    async (session: SessionSummary, archived: boolean) => {
      try {
        // The command layer keeps the UI reachable when the active session is
        // archived: it opens a replacement and returns its id for navigation.
        const result = await api.runCommand(archived ? '/archive' : '/unarchive', [session.session_id], activeId);
        if (result.message) notify(result.message, result.status === 'error' ? 'error' : 'ok');
        if (result.data?.session_id) {
          await openSession(result.data.session_id);
        }
        await refreshSessions();
      } catch (error) {
        notify(errorMessage(error), 'error');
      }
    },
    [activeId, notify, openSession, refreshSessions],
  );

  const handleCreate = useCallback(
    async (input: { cwd: string; title: string; preset: string }) => {
      setNewBusy(true);
      setNewError(null);
      try {
        const created = await api.createSession({
          cwd: input.cwd || undefined,
          title: input.title || undefined,
          preset: input.preset || undefined,
        });
        setNewOpen(false);
        setNewCwd(null);
        await refreshSessions();
        await openSession(created.session_id);
        notify('Session created.');
      } catch (error) {
        setNewError(errorMessage(error));
      } finally {
        setNewBusy(false);
      }
    },
    [notify, openSession, refreshSessions],
  );

  const handleCommand = useCallback(
    async (command: CommandSpec, args: string[]) => {
      setPaletteOpen(false);
      const name = command.name.replace(/^\//, '');
      if (name === 'new') {
        setNewCwd(null);
        setNewOpen(true);
        return;
      }
      if (name === 'settings') {
        await openSettings(SETTINGS_SECTIONS[(args[0] ?? '').toLowerCase()] ?? 'provider');
        return;
      }
      try {
        const result: CommandResult = await api.runCommand(command.name, args, activeId);
        if (result.message) notify(result.message, result.status === 'error' ? 'error' : 'ok');
        if (result.data?.session_id) {
          await openSession(result.data.session_id);
        } else if (name === 'sessions' || name === 'resume' || name === 'archive' || name === 'unarchive') {
          await refreshSessions();
        }
      } catch (error) {
        notify(errorMessage(error), 'error');
      }
    },
    [activeId, notify, openSession, openSettings, refreshSessions],
  );

  const handlePermission = useCallback(
    async (preset: string) => {
      if (!timeline) return;
      try {
        await api.permission(timeline.session_id, preset);
        setTimeline((current) => (current ? { ...current, permission_preset: preset } : current));
        notify(`Permission preset: ${preset}`);
      } catch (error) {
        notify(errorMessage(error), 'error');
      }
    },
    [notify, timeline],
  );

  const handleUpdateAgent = useCallback(
    async (patch: { provider?: string; model?: string; temperature?: number; max_tokens?: number; max_steps?: number }) => {
      setSettingsBusy(true);
      try {
        const result = await api.updateAgent(patch);
        await reloadSettings();
        const [text, kind] = settingsSavedMessage(result.live, 'Agent settings saved.');
        notify(text, kind);
      } catch (error) {
        notify(errorMessage(error), 'error');
      } finally {
        setSettingsBusy(false);
      }
    },
    [notify, reloadSettings],
  );

  const handleUpdateProvider = useCallback(
    async (name: string, patch: { base_url?: string; api_key?: string; api_key_env?: string }) => {
      setSettingsBusy(true);
      try {
        const result = await api.updateProvider(name, patch);
        await reloadSettings();
        const [text, kind] = settingsSavedMessage(result.live, `Provider "${name}" saved.`);
        notify(text, kind);
      } catch (error) {
        notify(errorMessage(error), 'error');
      } finally {
        setSettingsBusy(false);
      }
    },
    [notify, reloadSettings],
  );

  const handleTestProvider = useCallback(
    async (name: string) => {
      setProbe(null);
      setSettingsBusy(true);
      try {
        const result = await api.testProvider(name);
        setProbe(result);
        notify(result.ok === false ? `Test failed: ${result.message ?? result.detail ?? 'unknown error'}` : 'Connection OK.', result.ok === false ? 'error' : 'ok');
      } catch (error) {
        notify(errorMessage(error), 'error');
      } finally {
        setSettingsBusy(false);
      }
    },
    [notify],
  );

  const handleUpdatePermission = useCallback(
    async (preset: string) => {
      setSettingsBusy(true);
      try {
        const result = await api.updatePermission(preset);
        await reloadSettings();
        const [text, kind] = settingsSavedMessage(result.live, `Default permission: ${preset}`);
        notify(text, kind);
      } catch (error) {
        notify(errorMessage(error), 'error');
      } finally {
        setSettingsBusy(false);
      }
    },
    [notify, reloadSettings],
  );

  const toggleSidebar = useCallback(() => {
    if (isMobile) {
      setMobileOpen((value) => !value);
    } else {
      setSidebarCollapsed((value) => !value);
    }
  }, [isMobile]);

  if (bootError) {
    return (
      <div className="boot-screen">
        <div className="boot-card">
          <Icon name="alert" size={28} />
          <h1>DeepPyro could not start</h1>
          <p>{bootError}</p>
          <button className="button primary" type="button" onClick={() => window.location.reload()}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  const title = timeline?.title?.trim() || 'New session';
  const preset = timeline?.preset || 'standard';
  const provider = timeline?.provider ?? bootstrap?.defaults.provider ?? 'provider';
  const model = timeline?.model ?? bootstrap?.defaults.model ?? 'model';
  const commandCount = bootstrap?.commands.length ?? 0;
  const pluginCount = bootstrap?.plugins.length ?? 0;
  const recentCwds = Array.from(new Set(sessions.map((session) => session.cwd).filter(Boolean))).slice(0, 5);
  const defaultCwd = bootstrap?.defaults.cwd ?? sessions[0]?.cwd ?? '';

  return (
    <div className={`app-shell ${inspectorOpen ? 'with-inspector' : ''}`}>
      <Sidebar
        sessions={sessions}
        total={sessionTotal}
        hasMore={sessionHasMore}
        loadingMore={sessionsLoading}
        includeArchived={includeArchived}
        onLoadMore={() => void loadMoreSessions()}
        onToggleArchived={toggleArchived}
        activeId={activeId}
        collapsed={!isMobile && sidebarCollapsed}
        mobileOpen={mobileOpen}
        query={query}
        onQueryChange={setQuery}
        onToggleCollapse={() => {
          if (isMobile) {
            setMobileOpen(false);
          } else {
            setSidebarCollapsed((value) => !value);
          }
        }}
        onCloseMobile={() => setMobileOpen(false)}
        onSelect={(sessionId) => void openSession(sessionId)}
        onNew={() => {
          setNewCwd(null);
          setNewError(null);
          setNewOpen(true);
        }}
        onArchiveSession={(session, archived) => void handleSessionArchive(session, archived)}
        onOpenPalette={() => setPaletteOpen(true)}
        onOpenSettings={() => void openSettings()}
        onOpenPlugins={() => void openSettings('plugins')}
        onOpenInspector={() => setInspectorOpen((value) => !value)}
        commandCount={commandCount}
        pluginCount={pluginCount}
      />

      {mobileOpen ? (
        <button
          className="sidebar-scrim"
          type="button"
          aria-label="Close sidebar"
          tabIndex={-1}
          onClick={() => setMobileOpen(false)}
        />
      ) : null}

      <main className="main-column">
        <header className="chat-header">
          <div className="chat-title-row">
            <button className="icon-button mobile-only" type="button" onClick={toggleSidebar} title="Toggle sidebar">
              <Icon name="menu" size={19} />
            </button>
            <h1 className="chat-title">{title}</h1>
            <span className={`status-pill ${connection}`}>
              <span className={`status-dot ${connection}`} />
              {CONNECTION_LABEL[connection]}
            </span>
            <div className="header-actions">
              <button
                className="icon-button subtle"
                type="button"
                title="Reload transcript"
                onClick={() => {
                  if (activeId) void loadTimeline(activeId);
                }}
              >
                <Icon name="refresh" size={17} />
              </button>
              <button className="icon-button subtle" type="button" title="Settings (Ctrl+,)" onClick={() => void openSettings()}>
                <Icon name="settings" size={18} />
              </button>
              <button
                className={`icon-button subtle ${inspectorOpen ? 'active' : ''}`}
                type="button"
                title="Session details"
                onClick={() => setInspectorOpen((value) => !value)}
              >
                <Icon name="more" size={19} />
              </button>
            </div>
          </div>
          <div className="header-meta">
            <span className="header-chip">
              <Icon name="sparkles" size={13} />
              {preset}
            </span>
            <span className="header-chip">
              <Icon name="plug" size={13} />
              {provider} · {model}
            </span>
            <button
              className="header-chip path-chip path-button"
              type="button"
              title={timeline?.cwd ? `Change working directory (${timeline.cwd})` : 'Choose a working directory'}
              aria-label="Change working directory"
              onClick={() => {
                setNewCwd(timeline?.cwd || defaultCwd);
                setNewError(null);
                setNewOpen(true);
              }}
            >
              <Icon name="folder" size={13} />
              {timeline?.cwd || 'no workspace'}
            </button>
            {timeline?.busy ? <span className="header-chip busy-chip">working</span> : null}
          </div>
        </header>

        <div
          className="transcript-scroll"
          ref={scrollerRef}
          onScroll={(event) => {
            const element = event.currentTarget;
            stickRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 96;
          }}
        >
          <Transcript
            timeline={timeline}
            approvals={approvals}
            loading={timelineLoading}
            error={timelineError}
            turnError={turnError}
            onDismissTurnError={() => setTurnError(null)}
            onDecide={(approvalId, decision) => void handleDecide(approvalId, decision)}
            onResume={() => void handleResume()}
            onPick={setComposer}
            resumeBusy={resumeBusy}
          />
        </div>

        <Composer
          value={composer}
          onChange={setComposer}
          onSend={() => void handleSend()}
          onStop={() => void handleStop()}
          busy={Boolean(timeline?.busy)}
          disabled={!timeline || !timeline.active}
          placeholder={timeline?.active ? 'Message DeepPyro — / for commands' : 'Read-only session — resume to send'}
          hint={timeline?.active ? 'Enter sends · Shift+Enter adds a line · / for commands' : 'Read-only session'}
          modelLabel={`${provider} · ${model}`}
          onOpenPalette={() => setPaletteOpen(true)}
        />
      </main>

      {inspectorOpen ? (
        <Inspector
          timeline={timeline}
          permissionPresets={bootstrap?.permissions ?? []}
          onClose={() => setInspectorOpen(false)}
          onOpenSettings={() => void openSettings('permissions')}
          onPermission={(permission) => void handlePermission(permission)}
        />
      ) : null}

      <NewSession
        open={newOpen}
        defaultCwd={newCwd ?? defaultCwd}
        workspaceMode={newCwd !== null}
        recent={recentCwds}
        presets={(bootstrap?.presets ?? []).map((item) => ({
          id: item.id,
          name: item.name,
          summary: item.description,
        }))}
        busy={newBusy}
        error={newError}
        onClose={() => {
          if (!newBusy) setNewOpen(false);
        }}
        onCreate={(input) => void handleCreate(input)}
      />

      <Palette
        open={paletteOpen}
        commands={bootstrap?.commands ?? []}
        sessions={sessions}
        onClose={() => setPaletteOpen(false)}
        onRunCommand={(command, args) => void handleCommand(command, args)}
        onSelectSession={(sessionId) => {
          setPaletteOpen(false);
          void openSession(sessionId);
        }}
        onOpenSettings={() => {
          setPaletteOpen(false);
          void openSettings();
        }}
        onNewSession={() => {
          setPaletteOpen(false);
          setNewCwd(null);
          setNewError(null);
          setNewOpen(true);
        }}
      />

      <Settings
        initialSection={settingsSection}
        open={settingsOpen}
        payload={settings}
        loading={settingsLoading}
        error={settingsError}
        busy={settingsBusy}
        probe={probe}
        onClose={() => setSettingsOpen(false)}
        onReload={() => void reloadSettings()}
        onUpdateAgent={(patch) => void handleUpdateAgent(patch)}
        onUpdateProvider={(name, patch) => void handleUpdateProvider(name, patch)}
        onTestProvider={(name) => void handleTestProvider(name)}
        onUpdatePermission={(preset) => void handleUpdatePermission(preset)}
      />

      {toast ? (
        <div className={`toast ${toast.kind}`} role="status">
          <Icon name={toast.kind === 'error' ? 'alert' : 'check'} size={15} />
          <span>{toast.text}</span>
          <button className="icon-button tiny" type="button" onClick={() => setToast(null)} title="Dismiss">
            <Icon name="x" size={13} />
          </button>
        </div>
      ) : null}
    </div>
  );
}
