import type {
  Approval,
  Bootstrap,
  CommandResult,
  ConnectionState,
  FsDirsPayload,
  FsRootsPayload,
  ModelChangeResult,
  ProviderProbe,
  QueuedMessage,
  SessionPage,
  SettingsMutation,
  SettingsPayload,
  Timeline,
  WebEvent,
} from './types';

const TOKEN_KEY = 'deeppyro.web.token';

/** Capture a dev-mode ?token=, then drop it from the address bar. */
export function takeTokenFromUrl(): void {
  try {
    const url = new URL(window.location.href);
    const token = url.searchParams.get('token');
    if (!token) return;
    window.sessionStorage.setItem(TOKEN_KEY, token);
    url.searchParams.delete('token');
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);
  } catch {
    // sessionStorage may be unavailable; the httpOnly cookie still works.
  }
}

function authToken(): string {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown;
  timeoutMs?: number;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, timeoutMs = 60_000, headers: rawHeaders, ...rest } = options;
  const headers = new Headers(rawHeaders);
  const token = authToken();
  if (token) headers.set('authorization', `Bearer ${token}`);
  if (body !== undefined) headers.set('content-type', 'application/json');
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  let response: Response;
  try {
    response = await fetch(path, {
      ...rest,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      signal: rest.signal ?? controller.signal,
    });
  } catch (error) {
    window.clearTimeout(timer);
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError(0, 'The local runtime did not answer in time.');
    }
    throw new ApiError(0, 'The local runtime is unreachable.');
  }
  window.clearTimeout(timer);
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!response.ok) {
    const record = payload !== null && typeof payload === 'object' ? (payload as Record<string, unknown>) : null;
    const nested = record?.error;
    let message = `Request failed (${response.status})`;
    if (nested !== null && typeof nested === 'object') {
      const candidate = (nested as Record<string, unknown>).message;
      if (typeof candidate === 'string' && candidate) message = candidate;
    } else if (typeof record?.message === 'string') {
      message = record.message;
    } else if (typeof payload === 'string' && payload.trim()) {
      message = payload.trim().slice(0, 400);
    }
    throw new ApiError(response.status, message);
  }
  return payload as T;
}

export const api = {
  bootstrap: () => request<Bootstrap>('/api/ui/bootstrap'),
  sessions: ({ limit, offset, include_archived, query }: {
    limit?: number;
    offset?: number;
    include_archived?: boolean;
    query?: string;
  } = {}) => {
    const params = new URLSearchParams();
    if (limit !== undefined) params.set('limit', String(limit));
    if (offset !== undefined) params.set('offset', String(offset));
    if (include_archived) params.set('include_archived', 'true');
    if (query) params.set('query', query);
    const suffix = params.size > 0 ? `?${params.toString()}` : '';
    return request<SessionPage>(`/api/ui/sessions${suffix}`);
  },
  timeline: (sessionId: string) =>
    request<Timeline>(`/api/ui/sessions/${encodeURIComponent(sessionId)}/timeline`),
  createSession: (body: { cwd?: string; title?: string; preset?: string }) =>
    request<{ session_id: string; title: string; cwd: string; provider: string; model: string; preset: string }>(
      '/api/ui/sessions',
      { method: 'POST', body },
    ),
  resumeSession: (sessionId: string) =>
    request<{ session_id: string; resumed: boolean }>(
      `/api/ui/sessions/${encodeURIComponent(sessionId)}/resume`,
      { method: 'POST', body: {} },
    ),
  sendMessage: (sessionId: string, text: string) =>
    request<{ session_id: string; accepted?: boolean; queued?: boolean }>(
      `/api/ui/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: 'POST',
        body: { text },
      },
    ),
  interrupt: (sessionId: string) =>
    request<Record<string, unknown>>(`/api/ui/sessions/${encodeURIComponent(sessionId)}/interrupt`, {
      method: 'POST',
      body: {},
    }),
  rename: (sessionId: string, title: string) =>
    request<{ session_id: string; title: string }>(
      `/api/ui/sessions/${encodeURIComponent(sessionId)}/rename`,
      { method: 'POST', body: { title } },
    ),
  archive: (sessionId: string, archived = true) =>
    request<Record<string, unknown>>(`/api/ui/sessions/${encodeURIComponent(sessionId)}/archive`, {
      method: 'POST',
      body: { archived },
    }),
  permission: (sessionId: string, preset: string) =>
    request<Record<string, unknown>>(`/api/ui/sessions/${encodeURIComponent(sessionId)}/permission`, {
      method: 'POST',
      body: { preset },
    }),
  approvals: (sessionId?: string) =>
    request<{ approvals: Approval[] }>(
      `/api/ui/approvals${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`,
    ),
  decideApproval: (approvalId: string, decision: 'allow' | 'deny', scope?: 'once' | 'session') =>
    request<{ approval_id: string; settled: boolean; scope?: string }>(
      `/api/ui/approvals/${encodeURIComponent(approvalId)}`,
      { method: 'POST', body: scope ? { decision, scope } : { decision } },
    ),
  setSessionModel: (sessionId: string, provider: string, model: string) =>
    request<ModelChangeResult>(`/api/ui/sessions/${encodeURIComponent(sessionId)}/model`, {
      method: 'POST',
      body: { provider, model },
    }),
  queue: (sessionId: string) =>
    request<{ session_id: string; items: QueuedMessage[] }>(
      `/api/ui/sessions/${encodeURIComponent(sessionId)}/queue`,
    ),
  cancelQueued: (sessionId: string, queueId: string) =>
    request<{ ok: boolean; removed?: boolean }>(
      `/api/ui/sessions/${encodeURIComponent(sessionId)}/queue/${encodeURIComponent(queueId)}`,
      { method: 'DELETE' },
    ),
  fsDirs: (path?: string) => {
    const suffix = path ? `?path=${encodeURIComponent(path)}` : '';
    return request<FsDirsPayload>(`/api/ui/fs/dirs${suffix}`);
  },
  fsRoots: () => request<FsRootsPayload>('/api/ui/fs/roots'),
  runCommand: (name: string, args: string[], sessionId: string | null) =>
    request<CommandResult>('/api/ui/commands', {
      method: 'POST',
      body: { name, args, session_id: sessionId },
    }),
  settings: () => request<SettingsPayload>('/api/ui/settings'),
  updateAgent: (changes: Record<string, unknown>) =>
    request<SettingsMutation>('/api/ui/settings/agent', { method: 'PATCH', body: changes }),
  updateProvider: (name: string, changes: Record<string, unknown>) =>
    request<SettingsMutation>(`/api/ui/settings/provider/${encodeURIComponent(name)}`, {
      method: 'PATCH',
      body: changes,
    }),
  testProvider: (name: string) =>
    request<ProviderProbe>(`/api/ui/settings/providers/${encodeURIComponent(name)}/test`, {
      method: 'POST',
      body: {},
      timeoutMs: 90_000,
    }),
  updatePermission: (preset: string) =>
    request<SettingsMutation>('/api/ui/settings/permission', { method: 'POST', body: { preset } }),
};

export interface EventStreamHandle {
  close: () => void;
}

/**
 * One reconnecting SSE subscription. The browser's native EventSource retry is
 * bypassed on purpose: we control the backoff and resume from the last seq so a
 * laptop sleep or a host restart never leaves the transcript silently frozen.
 */
export function subscribeEvents(
  sessionId: string,
  after: number,
  onEvent: (event: WebEvent) => void,
  onState: (state: ConnectionState) => void,
): EventStreamHandle {
  let closed = false;
  let source: EventSource | null = null;
  let lastSeq = after;
  let retry = 0;
  let timer: number | null = null;

  const schedule = () => {
    if (closed || !navigator.onLine) return;
    const delay = Math.min(10_000, 800 * 2 ** retry);
    retry += 1;
    timer = window.setTimeout(connect, delay);
  };

  const connect = () => {
    if (closed) return;
    source?.close();
    const params = new URLSearchParams({ after: String(lastSeq) });
    const token = authToken();
    if (token) params.set('token', token);
    const url = `/api/ui/sessions/${encodeURIComponent(sessionId)}/events?${params.toString()}`;
    onState(retry === 0 ? 'connecting' : 'reconnecting');
    const next = new EventSource(url);
    source = next;
    next.onopen = () => {
      retry = 0;
      onState('live');
    };
    next.onmessage = (message) => {
      const seq = Number(message.lastEventId);
      if (Number.isFinite(seq) && seq > lastSeq) lastSeq = seq;
      try {
        const parsed = JSON.parse(message.data) as WebEvent;
        if (parsed && typeof parsed === 'object' && typeof parsed.type === 'string') {
          onEvent(parsed);
        }
      } catch {
        // A malformed frame is not worth breaking the stream over.
      }
    };
    next.onerror = () => {
      next.close();
      if (source === next) source = null;
      if (closed) return;
      onState(navigator.onLine ? 'reconnecting' : 'offline');
      schedule();
    };
  };

  const online = () => {
    if (closed) return;
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    retry = 0;
    connect();
  };
  const offline = () => {
    if (closed) return;
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    source?.close();
    source = null;
    onState('offline');
  };
  window.addEventListener('online', online);
  window.addEventListener('offline', offline);
  connect();

  return {
    close: () => {
      closed = true;
      window.removeEventListener('online', online);
      window.removeEventListener('offline', offline);
      if (timer !== null) window.clearTimeout(timer);
      source?.close();
      source = null;
    },
  };
}
