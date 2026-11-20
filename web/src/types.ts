/** Wire types shared by the DeepPyro JSON API and the browser SPA. */

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'offline';

export interface SessionPage {
  sessions: SessionSummary[];
  active_session_id: string | null;
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  query: string | null;
}

export interface LiveApply {
  applied: string[];
  restart_required: string[];
  deferred: boolean;
  detail: string | null;
}

export interface SettingsMutation {
  ok: boolean;
  config?: Record<string, unknown>;
  live?: LiveApply;
}

export interface SessionSummary {
  session_id: string;
  title: string;
  cwd: string;
  turns: number;
  preset: string;
  created_at: number;
  updated_at: number;
  read_only: boolean;
  readable: boolean;
  active: boolean;
  busy: boolean;
  archived: boolean;
}

export interface ProviderModel {
  id: string;
  name: string;
  context_window: number;
}

export interface Provider {
  id: string;
  api: string;
  base_url: string;
  configured: boolean;
  selected: boolean;
  models: ProviderModel[];
}

export interface Preset {
  id: string;
  name: string;
  description: string;
  model: string;
  mode: string;
  hidden: boolean;
  order: number;
}

export interface PermissionPreset {
  name: string;
  sandbox: string;
  approval: string;
  description: string;
}

export interface PluginSummary {
  id: string;
  state: string;
  version: string;
  description: string;
  plugin_id: string;
}

export interface CommandSpec {
  name: string;
  aliases: string[];
  summary: string;
  usage: string;
  group: string;
  takes_arg: boolean;
  addressable: boolean;
}

export interface Defaults {
  provider?: string;
  model?: string;
  temperature?: number;
  max_tokens?: number;
  max_steps?: number;
  cwd?: string;
}

export interface Bootstrap {
  version: string;
  health: unknown;
  sessions: SessionSummary[];
  active_session_id: string | null;
  commands: CommandSpec[];
  presets: Preset[];
  permissions: PermissionPreset[];
  plugins: PluginSummary[];
  providers: Provider[];
  defaults: Defaults;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: string | Record<string, unknown> | null;
  result: string | null;
  is_error: boolean;
  pending: boolean;
}

export interface ImagePart {
  attachment: string;
  media_type: string;
}

export interface MessageSource {
  kind: string;
  [key: string]: unknown;
}

/** Browser-side lifecycle of one user turn, used to group streaming steps. */
export interface TurnMeta {
  started_at: number;
  ended_at?: number;
  status: 'running' | 'settled' | 'failed' | 'stopped';
}

export interface ChatMessage {
  id: string | number;
  role: string;
  created_at: number;
  text: string;
  reasoning: string;
  source: MessageSource;
  tool_call_id: string | null;
  is_error: boolean;
  tool_calls: ToolCall[];
  images: ImagePart[];
  has_text: boolean;
  /** Browser-only flag: deltas are still arriving for this message. */
  streaming?: boolean;
  /** Browser-only turn grouping metadata. */
  turn_meta?: TurnMeta;
  /** Browser-only: backend turn number of a live assistant step. */
  turn?: number;
  /** Browser-only: live step created without a local user anchor. */
  detached?: boolean;
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
}

export interface Timeline {
  session_id: string;
  title: string;
  cwd: string;
  active: boolean;
  busy: boolean;
  pending: number;
  readable: boolean;
  archived: boolean;
  provider: string | null;
  model: string | null;
  preset: string;
  permission_preset: string;
  context_tokens: number;
  context_window: number;
  usage: Usage;
  messages: ChatMessage[];
  journal_seq?: number;
  /** Structured plan/goal/todo state when the backend exposes it. */
  state?: TimelineState | null;
}

export interface Approval {
  approval_id: string;
  session_id: string;
  tool: string;
  summary: string;
  detail: string;
  created_at: number;
  status?: string;
}

/** Goal state projected by the runtime for the plan bar. */
export interface GoalState {
  id?: string;
  revision?: number;
  objective: string;
  phase?: string;
  max_goal_rounds?: number;
  blocked_reason?: unknown;
}

export interface TodoItem {
  content: string;
  status: string;
}

export interface DeliverableState {
  path: string;
  description?: string;
}

export interface TaskState {
  task_id: string;
  objective: string;
  status: string;
  owner?: string;
  created_at?: number;
}

/** Structured session state exposed by the timeline (P1 backend). */
export interface TimelineState {
  title?: string;
  cwd?: string;
  model?: string | null;
  preset?: string;
  permission_policy?: string;
  sandbox_mode?: string;
  plan_mode?: boolean;
  rounds_started?: number;
  goal?: GoalState | null;
  todos?: TodoItem[];
  deliverables?: DeliverableState[];
  tasks?: TaskState[];
  handoffs?: unknown[];
}

/** One message waiting behind the running turn. */
export interface QueuedMessage {
  queue_id: string;
  text: string;
  created_at?: number;
}

/** Directory browser payload for the new-session folder picker. */
export interface FsDirEntry {
  name: string;
  path: string;
}

export interface FsDirsPayload {
  path: string;
  parent: string | null;
  entries: FsDirEntry[];
}

export interface FsRoot {
  label: string;
  path: string;
}

export interface FsRootsPayload {
  roots: FsRoot[];
}

export interface ModelChangeResult {
  ok: boolean;
  session_id: string;
  provider: string;
  model: string;
  applied: 'next_turn' | 'deferred';
}

export interface CommandResult {
  status: string;
  message: string;
  redirect?: string;
  data?: {
    session_id?: string;
    [key: string]: unknown;
  };
}

export interface ConfigSnapshot {
  path: string;
  agent: {
    provider: string;
    model: string;
    temperature: number;
    max_tokens: number;
    max_steps: number;
  };
  providers: Array<{
    name: string;
    api: string;
    base_url: string;
    api_key_env: string;
    api_key_set: boolean;
    models: ProviderModel[];
  }>;
}

export interface SettingsPayload {
  config: ConfigSnapshot;
  provider: {
    current: string;
    model: string;
    providers: ConfigSnapshot['providers'];
  };
  agent: ConfigSnapshot['agent'];
  permissions: {
    presets: Array<{
      name: string;
      sandbox: string;
      approval: string;
      description: string;
      mode?: string;
      label?: string;
    }>;
    current: string;
    default: string;
    custom: boolean;
  };
  commands: Array<{
    name: string;
    summary: string;
    usage: string;
    aliases: string[];
    plugin: string;
  }>;
  plugins: {
    revision: string;
    items: Array<Record<string, unknown>>;
  };
  sessions: {
    dir: string;
    count: number;
    active_session_id: string | null;
    items: Array<{
      id: string;
      title: string;
      cwd: string;
      turns: number;
      updated_at: number;
      preset: string;
      preview: string;
    }>;
  };
  web: { host: string; port: number };
  about: {
    version: string;
    python: string;
    platform: string;
    plugins: number;
  };
}

export interface ProviderProbe {
  ok?: boolean;
  status?: string;
  latency_ms?: number;
  detail?: string;
  message?: string;
  [key: string]: unknown;
}

interface EventBase {
  session_id: string;
}

export type WebEvent =
  | (EventBase & { type: 'session' })
  | (EventBase & { type: 'status'; phase: 'thinking' | 'done' | 'interrupted' })
  | (EventBase & { type: 'step'; turn: number; step: number })
  | (EventBase & { type: 'text'; text: string })
  | (EventBase & { type: 'thinking'; text: string })
  | (EventBase & { type: 'tool_call'; call_id: string; name: string; arguments: string })
  | (EventBase & { type: 'tool_result'; call_id: string; is_error: boolean; text: string })
  | (EventBase & {
      type: 'approval';
      approval_id: string;
      tool: string;
      summary: string;
      detail: string;
      status: string;
    })
  | (EventBase & { type: 'final'; text: string })
  | (EventBase & {
      type: 'usage';
      input_tokens: number;
      output_tokens: number;
      cached_input_tokens: number;
    })
  | (EventBase & { type: 'error'; message: string });
