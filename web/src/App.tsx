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
  QueuedMessage,
  SessionSummary,
  SettingsPayload,
  TeamState,
  Timeline,
  TimelineState,
} from './types';
import { Sidebar } from './components/Sidebar';
import { Transcript } from './components/Chat';
import { Composer } from './components/Composer';
import { Inspector } from './components/Inspector';
import { Settings, type SectionId } from './components/Settings';
import { Palette } from './components/Palette';
import { NewSession } from './components/NewSession';
import { Icon } from './components/Icon';
import { ApprovalBar } from './components/ApprovalBar';
import { QueueDock } from './components/QueueDock';
import { PlanBar } from './components/PlanBar';
import { ModelMenu } from './components/ModelMenu';
import { TeamBar } from './components/TeamBar';
import { PermissionMenu } from './components/PermissionMenu';
import { formatTokens, humanError } from './lib/format';
import {
  applyLiveEvent,
  beginUserTurn,
  createLiveContext,
  dropUserTurn,
  newNotice,
  reconcileTimeline,
  resetLiveContext,
} from './lib/liveTurn';

function isTeamPreset(state: TimelineState | null | undefined): boolean {
  const preset = state?.preset;
  return preset?.mode === 'team' || preset?.preset === 'team';
}

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
  const [queue, setQueue] = useState<QueuedMessage[]>([]);
  const [team, setTeam] = useState<TeamState | null>(null);
  const [teamBusy, setTeamBusy] = useState(false);
  const [notices, setNotices] = useState<ChatMessage[]>([]);
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
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [atBottom, setAtBottom] = useState(true);

  const [newOpen, setNewOpen] = useState(false);
  const [newCwd, setNewCwd] = useState<string | null>(null);
  // A new session is a local draft until its first message is sent: pressing
  // "Start session" must not persist an empty conversation.
  const [draft, setDraft] = useState<{ cwd: string; title: string; preset: string } | null>(null);

  const [paletteOpen, setPaletteOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [probe, setProbe] = useState<ProviderProbe | null>(null);

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
  const liveRef = useRef(createLiveContext());
  const queueRef = useRef<QueuedMessage[]>([]);
  // Only subscribe once the first timeline of this session is loaded, so the
  // event cursor starts at the stored journal sequence instead of replaying the
  // whole journal into an empty transcript.
  const [readySessionId, setReadySessionId] = useState<string | null>(null);
  const toastTimer = useRef<number | null>(null);

  useEffect(() => {
    queueRef.current = queue;
  }, [queue]);

  const notify = useCallback((text: string, kind: 'ok' | 'error' = 'ok') => {
    setToast({ kind, text });
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4200);
  }, []);

  const commitTimeline = useCallback((next: Timeline) => {
    journalRef.current = next.journal_seq ?? journalRef.current;
    // Merge instead of replacing so an in-flight step keeps its message
    // identity (and turn metadata) across background refreshes.
    setTimeline((current) =>
      current && current.session_id === next.session_id ? reconcileTimeline(current, next) : next,
    );
    // Only an attached session has a live event stream; a previewed one is
    // subscribed by the timeline that follows the first send.
    setReadySessionId(next.active ? next.session_id : null);
  }, []);

  const addNotice = useCallback((text: string, kind: 'ok' | 'error' = 'ok') => {
    if (!text.trim()) return;
    setNotices((current) => [...current, newNotice(text, kind)]);
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

  const loadQueue = useCallback(async (sessionId: string) => {
    try {
      const result = await api.queue(sessionId);
      if (activeRef.current === sessionId) setQueue(result.items);
    } catch {
      // Queue visibility is optional until the runtime exposes it.
    }
  }, []);

  const loadTeam = useCallback(async (sessionId: string) => {
    try {
      const result = await api.sessionTeam(sessionId);
      if (activeRef.current === sessionId) setTeam(result);
    } catch {
      // Only team sessions expose a team projection; other sessions stay quiet.
      if (activeRef.current === sessionId) setTeam(null);
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
        void loadQueue(sessionId);
        void loadTeam(sessionId);
      } catch (error) {
        if (ticket !== requestRef.current) return;
        setTimelineError(humanError(error));
        setTimelineLoading(false);
      }
    },
    [commitTimeline, loadApprovals, loadQueue, loadTeam],
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
      setReadySessionId(null);
      setActiveId(sessionId);
      setTimeline(null);
      setApprovals([]);
      setQueue([]);
      setTeam(null);
      setTeamBusy(false);
      queueRef.current = [];
      resetLiveContext(liveRef.current);
      stickRef.current = true;
      setAtBottom(true);
      setNotices([]);
      setTimelineError(null);
      setTurnError(null);
      setTimelineLoading(true);
      setMobileOpen(false);
      setDraft(null);
      window.history.replaceState(null, '', `/c/${encodeURIComponent(sessionId)}`);
      try {
        // Opening is a preview: reading the transcript must not detach another
        // session. The first message attaches this one server-side.
        const next = await api.timeline(sessionId);
        if (activeRef.current !== sessionId) return;
        setTimelineError(null);
        commitTimeline(next);
        setTimelineLoading(false);
        void loadApprovals(sessionId);
        void loadQueue(sessionId);
        void loadTeam(sessionId);
      } catch (error) {
        if (activeRef.current !== sessionId) return;
        setTimelineError(humanError(error));
        setTimelineLoading(false);
      }
    },
    [commitTimeline, loadApprovals, loadQueue, loadTeam],
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
          setDraft({ cwd: boot.defaults.cwd ?? '', title: '', preset: '' });
          setTimelineLoading(false);
        }
      } catch (error) {
        setBootError(humanError(error));
        setTimelineLoading(false);
      }
    })();
  }, [openSession]);

  useEffect(() => {
    if (!activeId || readySessionId !== activeId) return;
    setConnection('connecting');
    const handle = subscribeEvents(
      activeId,
      journalRef.current,
      (event) => {
        if (event.session_id !== activeId) return;
        const thinking = event.type === 'status' && event.phase === 'thinking';
        setTimeline((current) => {
          if (!current) return current;
          let next = current;
          if ((thinking || event.type === 'step') && liveRef.current.userMessageId === null) {
            // A queued turn just started: echo its text as the optimistic user
            // message instead of waiting for the next canonical refresh.
            const promoted = queueRef.current[0];
            if (promoted) {
              next = beginUserTurn(next, promoted.text, liveRef.current);
              stickRef.current = true;
            }
          }
          return applyLiveEvent(next, event, liveRef.current);
        });
        if (event.type === 'approval') void loadApprovals(activeId);
        if (event.type === 'error') {
          setTurnError(humanError(event.message));
          scheduleRefresh(activeId);
        }
        if (thinking || event.type === 'final' || (event.type === 'status' && event.phase !== 'thinking') || event.type === 'approval') {
          void loadQueue(activeId);
          void loadTeam(activeId);
          scheduleRefresh(activeId);
        }
      },
      setConnection,
    );
    return () => handle.close();
  }, [activeId, readySessionId, loadApprovals, loadQueue, loadTeam, scheduleRefresh]);

  // Team members run in their own loops, so the read-only projection refresh
  // cannot rely on the parent session events alone. Poll for any team session —
  // including a preview that has not been attached yet — so the bar picks up
  // members as soon as another client (or this one) attaches the session.
  const teamEnabled = team?.mode === 'team' || isTeamPreset(timeline?.state);
  useEffect(() => {
    if (!activeId || !teamEnabled) return;
    const timer = window.setInterval(() => {
      void loadTeam(activeId);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeId, teamEnabled, loadTeam]);

  useEffect(() => {
    const element = scrollerRef.current;
    if (element && stickRef.current) {
      element.scrollTop = element.scrollHeight;
      setAtBottom(true);
    }
  }, [timeline, notices, approvals, queue]);

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
      setSettingsError(humanError(error));
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
      setSettingsError(humanError(error));
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
        setNewOpen(true);
      } else if (event.key === 'Escape') {
        setPaletteOpen(false);
        setNewOpen(false);
        setSettingsOpen(false);
        setMobileOpen(false);
        setInspectorOpen(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [openSettings]);

  const handleSend = useCallback(async () => {
    const text = composer.trim();
    if (!text) return;
    if (draft) {
      setComposer('');
      setTurnError(null);
      stickRef.current = true;
      setAtBottom(true);
      try {
        // The draft session is created on the first send, so abandoned new
        // sessions never write an empty log.
        const created = await api.createSession({
          cwd: draft.cwd || undefined,
          title: draft.title || undefined,
          preset: draft.preset || undefined,
        });
        setDraft(null);
        await openSession(created.session_id);
        if (activeRef.current !== created.session_id) return;
        setTimeline((current) =>
          current ? beginUserTurn(current, text, liveRef.current) : current,
        );
        await api.sendMessage(created.session_id, text);
        scheduleRefresh(created.session_id);
        void refreshSessions();
      } catch (error) {
        setComposer(text);
        notify(humanError(error), 'error');
      }
      return;
    }
    if (!timeline || !timeline.readable) return;
    const sessionId = timeline.session_id;
    const queued = Boolean(timeline.busy);
    setComposer('');
    setTurnError(null);
    stickRef.current = true;
    setAtBottom(true);
    // Show the question immediately; the canonical timeline replaces it later.
    if (!queued) setTimeline((current) => (current ? beginUserTurn(current, text, liveRef.current) : current));
    try {
      const result = await api.sendMessage(sessionId, text);
      if (result.queued) void loadQueue(sessionId);
      scheduleRefresh(sessionId);
      void refreshSessions();
    } catch (error) {
      if (!queued) {
        setTimeline((current) => (current ? dropUserTurn(current, liveRef.current) : current));
      }
      setComposer(text);
      notify(humanError(error), 'error');
    }
  }, [composer, draft, loadQueue, notify, openSession, refreshSessions, scheduleRefresh, timeline]);

  const handleRetry = useCallback(
    async (text: string) => {
      if (!timeline || !timeline.active || !text.trim()) return;
      setTurnError(null);
      stickRef.current = true;
      setAtBottom(true);
      setTimeline((current) => (current ? beginUserTurn(current, text, liveRef.current) : current));
      try {
        await api.sendMessage(timeline.session_id, text);
        scheduleRefresh(timeline.session_id);
      } catch (error) {
        setTimeline((current) => (current ? dropUserTurn(current, liveRef.current) : current));
        notify(humanError(error), 'error');
      }
    },
    [notify, scheduleRefresh, timeline],
  );

  const handleStop = useCallback(async () => {
    if (!timeline) return;
    try {
      await api.interrupt(timeline.session_id);
      scheduleRefresh(timeline.session_id);
    } catch (error) {
      notify(humanError(error), 'error');
    }
  }, [notify, scheduleRefresh, timeline]);

  const handleDecide = useCallback(
    async (approvalId: string, decision: 'allow' | 'deny', scope: 'once' | 'session') => {
      try {
        await api.decideApproval(approvalId, decision, scope);
        setApprovals((current) => current.filter((approval) => approval.approval_id !== approvalId));
        if (scope === 'session' && decision === 'allow') addNotice('Allowed this tool for the rest of the session.');
        if (activeRef.current) scheduleRefresh(activeRef.current);
      } catch (error) {
        notify(humanError(error), 'error');
      }
    },
    [addNotice, notify, scheduleRefresh],
  );

  const handleCancelQueued = useCallback(
    async (queueId: string) => {
      if (!timeline) return;
      try {
        await api.cancelQueued(timeline.session_id, queueId);
        await loadQueue(timeline.session_id);
      } catch (error) {
        notify(humanError(error), 'error');
      }
    },
    [loadQueue, notify, timeline],
  );

  const handleRenameSession = useCallback(
    async (session: SessionSummary, title: string) => {
      try {
        await api.rename(session.session_id, title);
        await refreshSessions();
        if (activeRef.current === session.session_id) await loadTimeline(session.session_id, true);
      } catch (error) {
        notify(humanError(error), 'error');
      }
    },
    [loadTimeline, notify, refreshSessions],
  );

  const handleModelSelect = useCallback(
    async (nextProvider: string, nextModel: string) => {
      if (!timeline) return;
      try {
        const result = await api.setSessionModel(timeline.session_id, nextProvider, nextModel);
        setTimeline((current) =>
          current ? { ...current, provider: result.provider, model: result.model } : current,
        );
        notify(
          result.applied === 'deferred'
            ? 'Model will switch after the current turn.'
            : 'Model switched for the next message.',
        );
      } catch (error) {
        notify(humanError(error), 'error');
      }
    },
    [notify, timeline],
  );

  // Team mode is a session-level switch: the row above the composer either
  // enables it for this conversation or leaves it, matching /team on|off.
  const handleTeamToggle = useCallback(
    async (enabled: boolean) => {
      if (!timeline) return;
      setTeamBusy(true);
      try {
        const result = await api.setSessionTeam(timeline.session_id, enabled);
        setTeam(result);
        await loadTimeline(timeline.session_id, true);
        notify(enabled ? 'Agent team enabled.' : 'Agent team turned off.');
      } catch (error) {
        notify(humanError(error), 'error');
      } finally {
        setTeamBusy(false);
      }
    },
    [loadTimeline, notify, timeline],
  );

  const handleSessionArchive = useCallback(
    async (session: SessionSummary, archived: boolean) => {
      try {
        // The command layer keeps the UI reachable when the active session is
        // archived: it opens a replacement and returns its id for navigation.
        const result = await api.runCommand(archived ? '/archive' : '/unarchive', [session.session_id], activeId);
        if (result.message) addNotice(result.message, result.status === 'error' ? 'error' : 'ok');
        if (result.data?.session_id) {
          await openSession(result.data.session_id);
        }
        await refreshSessions();
      } catch (error) {
        notify(humanError(error), 'error');
      }
    },
    [activeId, addNotice, notify, openSession, refreshSessions],
  );

  const handleCreate = useCallback((input: { cwd: string; title: string; preset: string }) => {
    setNewOpen(false);
    setNewCwd(null);
    setDraft({ cwd: input.cwd, title: input.title, preset: input.preset });
    // Clear the previous conversation: the draft starts from the empty state,
    // and nothing is persisted until the first message.
    activeRef.current = null;
    journalRef.current = 0;
    setActiveId(null);
    setReadySessionId(null);
    setTimeline(null);
    setApprovals([]);
    setQueue([]);
    setTeam(null);
    setTeamBusy(false);
    queueRef.current = [];
    resetLiveContext(liveRef.current);
    stickRef.current = true;
    setAtBottom(true);
    setNotices([]);
    setTimelineError(null);
    setTurnError(null);
    setTimelineLoading(false);
    setMobileOpen(false);
    window.history.replaceState(null, '', '/');
  }, []);

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
        if (result.message) addNotice(result.message, result.status === 'error' ? 'error' : 'ok');
        if (result.data?.session_id) {
          await openSession(result.data.session_id);
        } else if (name === 'sessions' || name === 'resume' || name === 'archive' || name === 'unarchive') {
          await refreshSessions();
        }
      } catch (error) {
        addNotice(humanError(error), 'error');
      }
    },
    [activeId, addNotice, openSession, openSettings, refreshSessions],
  );

  const handlePermission = useCallback(
    async (preset: string) => {
      if (!timeline) return;
      try {
        await api.permission(timeline.session_id, preset);
        setTimeline((current) => (current ? { ...current, permission_preset: preset } : current));
      } catch (error) {
        notify(humanError(error), 'error');
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
        notify(humanError(error), 'error');
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
        notify(humanError(error), 'error');
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
        notify(humanError(error), 'error');
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
        notify(humanError(error), 'error');
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
  const provider = timeline?.provider ?? bootstrap?.defaults.provider ?? null;
  const model = timeline?.model ?? bootstrap?.defaults.model ?? null;
  const recentCwds = Array.from(new Set(sessions.map((session) => session.cwd).filter(Boolean))).slice(0, 5);
  const defaultCwd = bootstrap?.defaults.cwd ?? sessions[0]?.cwd ?? '';
  const modelProviders = (bootstrap?.providers ?? []).map((item) => ({
    name: item.id,
    models: item.models.map((entry) => entry.id),
  }));
  if (modelProviders.length === 0 && provider) {
    modelProviders.push({ name: provider, models: model ? [model] : [] });
  }
  const contextLabel = timeline
    ? `${formatTokens(timeline.context_tokens)} / ${formatTokens(timeline.context_window)} context`
    : undefined;

  return (
    <div className="app-shell">
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
          setNewOpen(true);
        }}
        onArchiveSession={(session, archived) => void handleSessionArchive(session, archived)}
        onRenameSession={(session, nextTitle) => void handleRenameSession(session, nextTitle)}
        onOpenSettings={() => void openSettings()}
        onOpenInspector={() => setInspectorOpen(true)}
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
            {connection !== 'live' ? (
              <span className={`status-pill ${connection}`}>
                <span className={`status-dot ${connection}`} />
                {CONNECTION_LABEL[connection]}
              </span>
            ) : null}
            <div className="header-actions">
              <button
                className="icon-button subtle"
                type="button"
                title="Reload conversation"
                aria-label="Reload conversation"
                onClick={() => {
                  if (activeId) void loadTimeline(activeId);
                }}
              >
                <Icon name="refresh" size={17} />
              </button>
              <button
                className={`icon-button subtle ${inspectorOpen ? 'active' : ''}`}
                type="button"
                title="Session details"
                aria-label="Session details"
                onClick={() => setInspectorOpen((value) => !value)}
              >
                <Icon name="info" size={18} />
              </button>
            </div>
          </div>
        </header>

        <div className="transcript-shell">
          <div
            className="transcript-scroll"
            ref={scrollerRef}
            onScroll={(event) => {
              const element = event.currentTarget;
              const next = element.scrollHeight - element.scrollTop - element.clientHeight < 96;
              stickRef.current = next;
              setAtBottom((current) => (current === next ? current : next));
            }}
          >
            <Transcript
              timeline={timeline}
              loading={timelineLoading}
              error={timelineError}
              turnError={turnError}
              onDismissTurnError={() => setTurnError(null)}
              onPick={setComposer}
              onRetry={(text) => void handleRetry(text)}
              notices={notices}
              draftCwd={draft?.cwd ?? null}
            />
          </div>
          {!atBottom && timeline && timeline.messages.length > 0 ? (
            <button
              className="jump-latest"
              type="button"
              onClick={() => {
                const element = scrollerRef.current;
                if (!element) return;
                stickRef.current = true;
                setAtBottom(true);
                element.scrollTo({ top: element.scrollHeight, behavior: 'smooth' });
              }}
            >
              <Icon name="arrow-right" size={13} className="jump-latest-icon" />
              Jump to latest
            </button>
          ) : null}
        </div>

        <div className="composer-area">
          {approvals.map((approval) => (
            <ApprovalBar
              key={approval.approval_id}
              approval={approval}
              onDecide={(approvalId, decision, scope) => void handleDecide(approvalId, decision, scope)}
            />
          ))}
          <QueueDock items={queue} onCancel={(queueId) => void handleCancelQueued(queueId)} />
          {timeline ? (
            <TeamBar
              state={team}
              enabled={teamEnabled}
              busy={Boolean(timeline.busy)}
              pending={teamBusy}
              onToggle={(enabled) => void handleTeamToggle(enabled)}
            />
          ) : null}
          <PlanBar state={timeline?.state ?? null} onOpenDetails={() => setInspectorOpen(true)} />
          <Composer
            value={composer}
            onChange={setComposer}
            onSend={() => void handleSend()}
            onStop={() => void handleStop()}
            busy={Boolean(timeline?.busy)}
            disabled={!draft && (!timeline || !timeline.readable)}
            placeholder="Message DeepPyro…"
            modelSlot={
              <ModelMenu
                provider={provider}
                model={model}
                providers={modelProviders}
                disabled={!timeline || !timeline.active}
                onSelect={(nextProvider, nextModel) => void handleModelSelect(nextProvider, nextModel)}
              />
            }
            permissionSlot={
              <PermissionMenu
                current={timeline?.permission_preset ?? 'ask'}
                presets={bootstrap?.permissions ?? []}
                disabled={!timeline || !timeline.active}
                onSelect={(preset) => void handlePermission(preset)}
              />
            }
            contextLabel={contextLabel}
            onOpenPalette={() => setPaletteOpen(true)}
          />
        </div>
      </main>

      {inspectorOpen ? (
        <>
          <button
            className="inspector-scrim"
            type="button"
            aria-label="Close session details"
            tabIndex={-1}
            onClick={() => setInspectorOpen(false)}
          />
          <Inspector
            timeline={timeline}
            permissionPresets={bootstrap?.permissions ?? []}
            onClose={() => setInspectorOpen(false)}
            onOpenSettings={() => void openSettings('permissions')}
            onPermission={(permission) => void handlePermission(permission)}
          />
        </>
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
        busy={false}
        error={null}
        onClose={() => setNewOpen(false)}
        onCreate={(input) => handleCreate(input)}
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
