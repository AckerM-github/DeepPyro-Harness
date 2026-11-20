/*
 * Presentation metadata and output parsing for tool activity cards.
 *
 * Everything here is pure: given a ToolCall it derives a human title, a quiet
 * one-line summary and the structured data the card body renders. No raw JSON
 * is surfaced by default and no machine ids end up in collapsed rows.
 */

import type { IconName } from '../components/Icon';
import type { ToolCall } from '../types';

export type ToolKind =
  | 'read'
  | 'write'
  | 'edit'
  | 'shell'
  | 'search'
  | 'find'
  | 'web'
  | 'todo'
  | 'present'
  | 'goal'
  | 'job'
  | 'agent'
  | 'ask'
  | 'directory'
  | 'schedule'
  | 'skill'
  | 'plugin'
  | 'memory'
  | 'task'
  | 'unknown';

export interface ToolMeta {
  kind: ToolKind;
  icon: IconName;
  title: string;
  summary: string;
}

export interface ReadLine {
  num: number;
  text: string;
}

export interface ReadView {
  path: string;
  lines: ReadLine[];
  total?: number;
  remaining: number;
}

export interface WriteView {
  path?: string;
  lines: string[];
  remaining: number;
  totalLines: number;
  status?: string;
}

export interface EditView {
  path?: string;
  oldText?: string;
  newText?: string;
  patch?: string;
  added: number;
  removed: number;
  replaceAll: boolean;
  status?: string;
}

export interface ShellView {
  command: string;
  cwd?: string;
  description?: string;
  background: boolean;
  output: string;
  exitCode?: number;
}

export interface GrepHit {
  line: number;
  text: string;
}

export interface GrepFile {
  file: string;
  hits: GrepHit[];
}

export interface GrepView {
  pattern: string;
  header?: string;
  files: GrepFile[];
}

export interface GlobView {
  pattern: string;
  files: string[];
  more: number;
}

export interface WebSource {
  title?: string;
  url: string;
}

export interface WebView {
  mode: 'search' | 'fetch';
  queries: string[];
  url?: string;
  status?: string;
  sources: WebSource[];
  excerpt: string;
}

export type TodoStatus = 'pending' | 'in_progress' | 'completed';

export interface TodoItem {
  content: string;
  status: TodoStatus;
}

export interface PresentFile {
  path: string;
  description?: string;
}

export interface AskOption {
  label: string;
  description?: string;
}

export interface AskQuestion {
  id?: string;
  question: string;
  header?: string;
  options: AskOption[];
  multiSelect: boolean;
  selected: string[];
  custom?: string;
}

export interface DirectoryView {
  path?: string;
  requested?: string;
}

export interface AgentView {
  name?: string;
  description?: string;
  message?: string;
  prompt?: string;
  action?: string;
}

export interface GoalInfo {
  objective?: string;
  status?: string;
  phase?: string;
  blockedReason?: string;
}

export interface DetailField {
  label: string;
  value: string;
  mono: boolean;
}

/* ------------------------------------------------------------------ */
/* generic helpers                                                     */
/* ------------------------------------------------------------------ */

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function textValue(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

function jsonString(value: unknown, pretty: boolean): string {
  try {
    const text = JSON.stringify(value, null, pretty ? 2 : 0);
    return typeof text === 'string' ? text : '';
  } catch {
    return '';
  }
}

function truncate(text: string, max: number): string {
  return text.length > max ? text.slice(0, max - 1) + '…' : text;
}

export function oneLine(text: string, max = 72): string {
  const lines = text.split(/\r?\n/);
  let first = '';
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed) {
      first = trimmed;
      break;
    }
  }
  if (!first) return '';
  return truncate(first.replace(/\s+/g, ' '), max);
}

export function baseName(path: string): string {
  const normalized = path.replace(/\\/g, '/').replace(/\/+$/, '');
  const parts = normalized.split('/');
  const last = parts[parts.length - 1];
  return last ? last : path;
}

export function shortPath(path: string, depth = 3): string {
  const normalized = path.replace(/\\/g, '/').replace(/\/+$/, '');
  const parts = normalized.split('/').filter(function (part) {
    return part.length > 0;
  });
  if (!parts.length) return path;
  if (parts.length <= depth) return parts.join('/');
  return '…/' + parts.slice(parts.length - depth).join('/');
}

export function urlHost(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.host.replace(/^www\./, '');
  } catch {
    const stripped = url.replace(/^https?:\/\//, '');
    const host = stripped.split('/')[0];
    return host || url;
  }
}

function between(text: string, start: string, end: string): string | undefined {
  const startIndex = text.indexOf(start);
  if (startIndex < 0) return undefined;
  const endIndex = text.indexOf(end, startIndex + start.length);
  if (endIndex < 0) return undefined;
  return text.slice(startIndex + start.length, endIndex);
}

function parseJson(text: string): unknown {
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  if (trimmed.charAt(0) !== '{' && trimmed.charAt(0) !== '[') return undefined;
  try {
    return JSON.parse(trimmed);
  } catch {
    return undefined;
  }
}

function findBlockedReason(value: unknown, depth = 0): string | undefined {
  if (depth > 4) return undefined;
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findBlockedReason(item, depth + 1);
      if (found) return found;
    }
    return undefined;
  }
  if (!isRecord(value)) return undefined;
  const direct = value.blockedReason ?? value.blocked_reason;
  if (isRecord(direct)) {
    const message = textValue(direct.message) ?? textValue(direct.reason) ?? textValue(direct.code);
    if (message) return message;
  } else {
    const text = textValue(direct);
    if (text) return text;
  }
  for (const child of Object.values(value)) {
    const found = findBlockedReason(child, depth + 1);
    if (found) return found;
  }
  return undefined;
}

function findText(value: unknown, key: string, depth = 0): string | undefined {
  if (depth > 4) return undefined;
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findText(item, key, depth + 1);
      if (found) return found;
    }
    return undefined;
  }
  if (!isRecord(value)) return undefined;
  const direct = value[key];
  if (typeof direct === 'string' && direct.trim()) return direct.trim();
  for (const child of Object.values(value)) {
    const found = findText(child, key, depth + 1);
    if (found) return found;
  }
  return undefined;
}

/* ------------------------------------------------------------------ */
/* tool call accessors                                                 */
/* ------------------------------------------------------------------ */

export function parseArgs(call: ToolCall): Record<string, unknown> {
  const raw = call.arguments;
  if (raw === null || raw === undefined) return {};
  if (isRecord(raw)) return raw;
  const text = raw.trim();
  if (!text) return {};
  try {
    const parsed: unknown = JSON.parse(text);
    if (isRecord(parsed)) return parsed;
    if (typeof parsed === 'string' && parsed.trim()) return { input: parsed };
    return {};
  } catch {
    return { input: text };
  }
}

export function rawArgsText(call: ToolCall): string {
  const raw = call.arguments;
  if (raw === null || raw === undefined) return '';
  if (typeof raw === 'string') {
    const text = raw.trim();
    if (!text) return '';
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      return text;
    }
  }
  return jsonString(raw, true);
}

export function resultText(call: ToolCall): string {
  const raw = call.result;
  if (typeof raw !== 'string') return '';
  const trimmed = raw.trim();
  if (!trimmed) return '';
  const parsed = parseJson(trimmed);
  if (parsed !== undefined) {
    const pretty = jsonString(parsed, true);
    if (pretty) return pretty;
  }
  return trimmed;
}

/* ------------------------------------------------------------------ */
/* kind / title / icon                                                 */
/* ------------------------------------------------------------------ */

const KIND_BY_NAME: Record<string, ToolKind> = {
  read: 'read',
  write: 'write',
  edit: 'edit',
  apply_patch: 'edit',
  bash: 'shell',
  pwsh: 'shell',
  shell: 'shell',
  run_command: 'shell',
  grep: 'search',
  search: 'search',
  glob: 'find',
  find_files: 'find',
  web_search: 'web',
  web_fetch: 'web',
  fetch: 'web',
  todo_write: 'todo',
  todo: 'todo',
  present: 'present',
  get_goal: 'goal',
  create_goal: 'goal',
  update_goal: 'goal',
  goal: 'goal',
  job_output: 'job',
  job_kill: 'job',
  job_list: 'job',
  ask_user_question: 'ask',
  working_directory: 'directory',
  cd: 'directory',
  schedule_create: 'schedule',
  schedule_list: 'schedule',
  schedule_view: 'schedule',
  schedule_edit: 'schedule',
  skill: 'skill',
  plugin_manager: 'plugin',
  plugin_create: 'plugin',
  plugin_inspect: 'plugin',
  settings_read: 'plugin',
  settings_write: 'plugin',
  subagent: 'agent',
  subagent_fork: 'agent',
  team_spawn: 'agent',
  team_send: 'agent',
  team_wait: 'agent',
  team_cancel: 'agent',
  team_members: 'agent',
  team_task_create: 'task',
  team_task_list: 'task',
  team_task_get: 'task',
  team_task_update: 'task',
  task_create: 'task',
  task_update: 'task',
  task_complete: 'task',
  task_list: 'task',
  task_handoff: 'task',
  mnemon_runtime_memory: 'memory',
  mnemon_remember: 'memory',
  mnemon_recall: 'memory',
  mnemon_forget: 'memory',
  mnemon_link: 'memory',
  mnemon_related: 'memory',
  mnemon_document_search: 'memory',
  mnemon_document_create: 'memory',
  mnemon_document_manage: 'memory'
};

export function toolKind(name: string): ToolKind {
  const known = KIND_BY_NAME[name];
  if (known) return known;
  if (name.startsWith('team_')) return 'agent';
  if (name.startsWith('subagent')) return 'agent';
  if (name.startsWith('job_')) return 'job';
  if (name.startsWith('goal')) return 'goal';
  if (name.startsWith('schedule')) return 'schedule';
  if (name.startsWith('plugin') || name.startsWith('settings_')) return 'plugin';
  if (name.startsWith('mnemon_')) return 'memory';
  if (name.startsWith('todo')) return 'todo';
  if (name.startsWith('task_')) return 'task';
  return 'unknown';
}

const TITLE_BY_NAME: Record<string, string> = {
  read: 'Read',
  write: 'Write',
  edit: 'Edit',
  apply_patch: 'Apply patch',
  bash: 'Run command',
  pwsh: 'Run command',
  shell: 'Run command',
  grep: 'Search',
  glob: 'Find files',
  web_search: 'Search web',
  web_fetch: 'Fetch page',
  todo_write: 'Update todo list',
  todo: 'Update todo list',
  present: 'Present files',
  get_goal: 'Check goal',
  create_goal: 'Set goal',
  update_goal: 'Update goal',
  goal: 'Set goal',
  job_output: 'Read background job',
  job_kill: 'Stop background job',
  job_list: 'List background jobs',
  ask_user_question: 'Ask you a question',
  working_directory: 'Working directory',
  schedule_create: 'Schedule task',
  schedule_list: 'List schedules',
  schedule_view: 'View schedule',
  schedule_edit: 'Update schedule',
  skill: 'Load skill',
  plugin_manager: 'Manage plugin',
  plugin_create: 'Create plugin',
  plugin_inspect: 'Inspect plugin',
  settings_read: 'Read settings',
  settings_write: 'Update settings',
  subagent: 'Delegate task',
  subagent_fork: 'Delegate task',
  team_spawn: 'Spawn teammate',
  team_send: 'Message teammate',
  team_wait: 'Wait for teammate',
  team_cancel: 'Stop teammate',
  team_members: 'List teammates',
  team_task_create: 'Create task',
  team_task_list: 'List tasks',
  team_task_get: 'Read task',
  team_task_update: 'Update task',
  task_create: 'Create task',
  task_update: 'Update task',
  task_complete: 'Complete task',
  task_list: 'List tasks',
  task_handoff: 'Hand off task',
  mnemon_runtime_memory: 'Save memory',
  mnemon_remember: 'Save memory',
  mnemon_recall: 'Recall memory',
  mnemon_forget: 'Forget memory',
  mnemon_link: 'Link memories',
  mnemon_related: 'Related memories',
  mnemon_document_search: 'Search documents',
  mnemon_document_create: 'Create document',
  mnemon_document_manage: 'Update document'
};

export function humanizeName(name: string): string {
  const words = name
    .replace(/[_\-.]+/g, ' ')
    .trim()
    .split(/\s+/)
    .filter(function (word) {
      return word.length > 0;
    });
  if (!words.length) return 'Tool';
  return words
    .map(function (word) {
      const lower = word.toLowerCase();
      return lower.charAt(0).toUpperCase() + lower.slice(1);
    })
    .join(' ');
}

function capitalize(text: string): string {
  if (!text) return text;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function titleFor(name: string): string {
  const known = TITLE_BY_NAME[name];
  return known ? known : humanizeName(name);
}

function iconFor(kind: ToolKind): IconName {
  switch (kind) {
    case 'read':
      return 'file';
    case 'write':
      return 'pencil';
    case 'edit':
      return 'pencil';
    case 'shell':
      return 'terminal';
    case 'search':
      return 'search';
    case 'find':
      return 'folder';
    case 'web':
      return 'globe';
    case 'todo':
      return 'check';
    case 'present':
      return 'external';
    case 'goal':
      return 'sparkles';
    case 'job':
      return 'clock';
    case 'agent':
      return 'message';
    case 'ask':
      return 'message';
    case 'directory':
      return 'folder';
    case 'schedule':
      return 'clock';
    case 'skill':
      return 'sparkles';
    case 'plugin':
      return 'plug';
    case 'memory':
      return 'archive';
    case 'task':
      return 'check';
    default:
      return 'command';
  }
}

export function toolMeta(call: ToolCall): ToolMeta {
  const kind = toolKind(call.name);
  return {
    kind: kind,
    icon: iconFor(kind),
    title: titleFor(call.name),
    summary: summarize(call, kind)
  };
}

function plural(count: number, single: string, many: string): string {
  return String(count) + ' ' + (count === 1 ? single : many);
}

function firstMeaningfulArg(args: Record<string, unknown>): string {
  const keys = [
    'file_path',
    'path',
    'command',
    'query',
    'queries',
    'pattern',
    'url',
    'name',
    'description',
    'title',
    'subject',
    'prompt',
    'input',
    'objective',
    'reason',
    'content',
    'text'
  ];
  for (const key of keys) {
    const value = args[key];
    if (typeof value === 'string') {
      const text = oneLine(value, 64);
      if (!text) continue;
      if (key === 'file_path' || key === 'path') return shortPath(text);
      return text;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        if (typeof item === 'string' && item.trim()) return oneLine(item, 64);
      }
    }
  }
  for (const key of Object.keys(args)) {
    const value = args[key];
    if (typeof value === 'string') {
      const text = oneLine(value, 64);
      if (text) return text;
    }
  }
  return '';
}

function summarize(call: ToolCall, kind: ToolKind): string {
  const args = parseArgs(call);
  const name = call.name;

  switch (kind) {
    case 'read': {
      const view = readView(call);
      const file = view.path || textValue(args.file_path);
      if (!file) return call.is_error ? 'Read failed' : '';
      if (view.total !== undefined) return shortPath(file) + ' · ' + plural(view.total, 'line', 'lines');
      return shortPath(file);
    }
    case 'write': {
      const path = textValue(args.file_path);
      return path ? shortPath(path) : '';
    }
    case 'edit': {
      const path = textValue(args.file_path);
      if (path) return shortPath(path);
      const patch = textValue(args.patch);
      return patch ? 'Apply patch' : '';
    }
    case 'shell': {
      const view = shellView(call);
      const command = view.command ? oneLine(view.command, 56) : '';
      const label = command || (view.description ? oneLine(view.description, 56) : 'Command');
      if (view.background) return label + ' · in background';
      if (call.pending) return label;
      if (view.exitCode !== undefined && view.exitCode !== 0) return label;
      if (call.is_error) return label;
      return label + ' · passed';
    }
    case 'search': {
      const pattern = textValue(args.pattern) || 'text';
      const scope = textValue(args.path);
      const view = grepView(call);
      let count = 0;
      for (const file of view.files) count += file.hits.length;
      const scopeLabel = scope ? ' in ' + shortPath(scope) : '';
      const countLabel = count > 0 ? ' · ' + plural(count, 'match', 'matches') : '';
      return 'Search ' + oneLine(pattern, 40) + scopeLabel + countLabel;
    }
    case 'find': {
      const view = globView(call);
      if (!call.pending) {
        if (!view.files.length && !view.more) return 'No files found';
        return plural(view.files.length + view.more, 'file', 'files');
      }
      const pattern = textValue(args.pattern);
      return pattern ? 'Find ' + oneLine(pattern, 40) : 'Find files';
    }
    case 'web': {
      const view = webView(call);
      if (view.mode === 'fetch') {
        const url = view.url || textValue(args.url);
        return url ? urlHost(url) : 'Page';
      }
      const query = view.queries.length ? oneLine(view.queries[0], 48) : 'Web search';
      if (view.sources.length) return query + ' · ' + plural(view.sources.length, 'source', 'sources');
      return query;
    }
    case 'todo': {
      const items = todoView(call);
      if (items.length) {
        let done = 0;
        for (const item of items) if (item.status === 'completed') done += 1;
        return String(done) + '/' + String(items.length) + ' done';
      }
      return call.pending ? 'Update todo list' : 'Updated todo list';
    }
    case 'present': {
      const files = presentView(call);
      if (files.length === 1) return baseName(files[0].path);
      if (files.length > 1) return plural(files.length, 'file', 'files');
      return 'Presented files';
    }
    case 'goal': {
      const info = goalInfo(call);
      if (info.objective) return oneLine(info.objective, 64);
      const action = textValue(args.action);
      if (action) return capitalize(action);
      return 'Goal';
    }
    case 'job': {
      const label = name === 'job_list' ? 'Background jobs' : name === 'job_kill' ? 'Stop background job' : 'Background job';
      const statusMatch = /\[status:\s*([a-z ]+)/.exec(call.result ?? '');
      if (statusMatch) return label + ' · ' + oneLine(statusMatch[1], 24);
      return label;
    }
    case 'agent': {
      const view = agentView(call);
      if (view.description) return oneLine(view.description, 64);
      if (view.message) return oneLine(view.message, 64);
      if (view.prompt) return oneLine(view.prompt, 64);
      if (view.name) return oneLine(view.name, 48);
      return name.startsWith('team_') ? humanizeName(name) : 'Teammate';
    }
    case 'ask': {
      const questions = askView(call);
      if (questions.length) return oneLine(questions[0].question, 64);
      return 'Question for you';
    }
    case 'directory': {
      const view = directoryView(call);
      return view.path ? shortPath(view.path) : 'Working directory';
    }
    case 'schedule': {
      const title = textValue(args.title);
      if (title) return oneLine(title, 56);
      const action = textValue(args.action);
      return action ? capitalize(action) : 'Schedules';
    }
    case 'skill': {
      const skillName = textValue(args.name);
      return skillName ? oneLine(skillName, 48) : 'Load skill';
    }
    case 'plugin': {
      const id = textValue(args.bundle_id) || textValue(args.plugin_id) || textValue(args.id);
      const action = textValue(args.action) || name;
      if (id) return capitalize(oneLine(action, 20)) + ' · ' + oneLine(id, 36);
      return humanizeName(name);
    }
    case 'memory': {
      const query = textValue(args.query) || textValue(args.content) || textValue(args.old_text);
      if (query) return oneLine(query, 64);
      return humanizeName(name);
    }
    case 'task': {
      const subject = textValue(args.subject) || textValue(args.title);
      if (subject) return oneLine(subject, 56);
      const action = textValue(args.action);
      return action ? capitalize(action) : 'Tasks';
    }
    default: {
      const value = firstMeaningfulArg(args);
      if (value) return value;
      return humanizeName(name);
    }
  }
}

/* ------------------------------------------------------------------ */
/* read                                                                */
/* ------------------------------------------------------------------ */

const READ_LINE_CAP = 200;

export function readView(call: ToolCall): ReadView {
  const args = parseArgs(call);
  const result = typeof call.result === 'string' ? call.result : '';
  const pathTag = between(result, '<path>', '</path>');
  const path = textValue(pathTag) || textValue(args.file_path) || '';
  const contentTag = between(result, '<content>', '</content>');
  const content = contentTag !== undefined ? contentTag : result;

  const parsed: ReadLine[] = [];
  for (const raw of content.split(/\r?\n/)) {
    const match = /^(\d+): ?(.*)$/.exec(raw);
    if (!match) continue;
    const num = Number.parseInt(match[1], 10);
    if (!Number.isFinite(num)) continue;
    parsed.push({ num: num, text: match[2] });
  }

  let total: number | undefined;
  const totalMatch = /total (\d+) lines?/i.exec(result);
  if (totalMatch) total = Number.parseInt(totalMatch[1], 10);
  if (total === undefined) {
    const rangeMatch = /lines? (\d+)-(\d+) of (\d+)/i.exec(result);
    if (rangeMatch) total = Number.parseInt(rangeMatch[3], 10);
  }

  const shownCount = Math.min(parsed.length, READ_LINE_CAP);
  const lines = parsed.slice(0, shownCount);
  const firstNum = lines.length ? lines[0].num : 1;
  let remaining = parsed.length - shownCount;
  if (total !== undefined) {
    remaining = Math.max(0, total - (firstNum - 1) - lines.length);
  }

  return { path: path, lines: lines, total: total, remaining: remaining };
}

/* ------------------------------------------------------------------ */
/* write / edit                                                        */
/* ------------------------------------------------------------------ */

const WRITE_LINE_CAP = 120;

function resultStatus(result: string | null): string | undefined {
  if (!result) return undefined;
  if (/Created file/i.test(result)) return 'Created';
  if (/Updated file/i.test(result)) return 'Updated';
  if (/has been updated successfully/i.test(result)) return 'Updated';
  return undefined;
}

export function writeView(call: ToolCall): WriteView {
  const args = parseArgs(call);
  const path = textValue(args.file_path);
  const rawContent = typeof args.content === 'string' ? args.content : '';
  const normalized = rawContent.replace(/\r?\n$/, '');
  const all = normalized.length ? normalized.split(/\r?\n/) : [];
  return {
    path: path,
    lines: all.slice(0, WRITE_LINE_CAP),
    remaining: Math.max(0, all.length - WRITE_LINE_CAP),
    totalLines: all.length,
    status: resultStatus(call.result)
  };
}

function countLines(text: string): number {
  if (!text.length) return 0;
  return text.split(/\r?\n/).length;
}

export function editView(call: ToolCall): EditView {
  const args = parseArgs(call);
  const path = textValue(args.file_path);
  const oldRaw = typeof args.old_string === 'string' ? args.old_string : undefined;
  const newRaw = typeof args.new_string === 'string' ? args.new_string : undefined;
  const patch = typeof args.patch === 'string' && args.patch.trim() ? args.patch : undefined;
  const replaceAll = args.replace_all === true;
  return {
    path: path,
    oldText: oldRaw && oldRaw.length ? oldRaw : undefined,
    newText: newRaw && newRaw.length ? newRaw : undefined,
    patch: patch,
    added: countLines(newRaw ?? ''),
    removed: countLines(oldRaw ?? ''),
    replaceAll: replaceAll,
    status: resultStatus(call.result)
  };
}

/* ------------------------------------------------------------------ */
/* shell                                                               */
/* ------------------------------------------------------------------ */

export function shellView(call: ToolCall): ShellView {
  const args = parseArgs(call);
  let output = typeof call.result === 'string' ? call.result : '';
  let exitCode: number | undefined;
  const exitMatch = /\[exit code: (-?\d+)\]\s*$/.exec(output);
  if (exitMatch) {
    exitCode = Number.parseInt(exitMatch[1], 10);
    output = output.slice(0, exitMatch.index);
  }
  const trimmed = output.trim();
  return {
    command: textValue(args.command) ?? '',
    cwd: textValue(args.workdir) ?? textValue(args.cwd),
    description: textValue(args.description),
    background: args.run_in_background === true || /started background job/i.test(trimmed),
    output: trimmed,
    exitCode: exitCode
  };
}

/* ------------------------------------------------------------------ */
/* grep / glob                                                         */
/* ------------------------------------------------------------------ */

const GREP_LINE = /^Line (\d+):\s?(.*)$/;

export function grepView(call: ToolCall): GrepView {
  const args = parseArgs(call);
  const result = typeof call.result === 'string' ? call.result : '';
  const lines = result.split(/\r?\n/);
  const files: GrepFile[] = [];
  let header: string | undefined;
  let current: GrepFile | undefined;

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    if (!header && /^Found\b/.test(line)) {
      header = line;
      continue;
    }
    if (line.charAt(0) === '(') continue;
    if (/^Showing\b/.test(line) || /^No matches\b/.test(line) || /^Cite\b/.test(line)) {
      if (/^No matches\b/.test(line)) header = header ?? line;
      continue;
    }
    const hit = GREP_LINE.exec(line);
    if (hit) {
      if (!current) {
        current = { file: '', hits: [] };
        files.push(current);
      }
      current.hits.push({ line: Number.parseInt(hit[1], 10), text: hit[2] });
      continue;
    }
    current = { file: line, hits: [] };
    files.push(current);
  }

  return { pattern: textValue(args.pattern) ?? '', header: header, files: files };
}

export function globView(call: ToolCall): GlobView {
  const args = parseArgs(call);
  const result = typeof call.result === 'string' ? call.result : '';
  const files: string[] = [];
  let more = 0;
  const footerMatch = /\(Showing the first (\d+) of (\d+) files?/i.exec(result);
  if (footerMatch) more = Math.max(0, Number.parseInt(footerMatch[2], 10));
  for (const raw of result.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.charAt(0) === '(') continue;
    if (/^No files found/i.test(line)) continue;
    if (/^Showing\b/i.test(line)) continue;
    if (files.indexOf(line) >= 0) continue;
    files.push(line);
  }
  if (more > 0) more = Math.max(0, more - files.length);
  const shown = files.slice(0, 100);
  if (files.length > shown.length) more += files.length - shown.length;
  return { pattern: textValue(args.pattern) ?? '', files: shown, more: more };
}

/* ------------------------------------------------------------------ */
/* web                                                                 */
/* ------------------------------------------------------------------ */

const LINK_PATTERN = /https?:\/\/[^\s<>"')\]]+/g;

function extractLinks(text: string): WebSource[] {
  const seen = new Set<string>();
  const sources: WebSource[] = [];
  let match = LINK_PATTERN.exec(text);
  while (match && sources.length < 12) {
    const url = match[0].replace(/[.,;:]+$/, '');
    if (!seen.has(url)) {
      seen.add(url);
      sources.push({ url: url });
    }
    match = LINK_PATTERN.exec(text);
  }
  return sources;
}

function collapse(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

export function webView(call: ToolCall): WebView {
  const args = parseArgs(call);
  const result = typeof call.result === 'string' ? call.result : '';
  const isFetch = call.name.indexOf('fetch') >= 0;

  if (isFetch) {
    const url = textValue(args.url);
    const statusMatch = /HTTP (\d+)/i.exec(result);
    const status = statusMatch ? 'HTTP ' + statusMatch[1] : undefined;
    const clean = result
      .split(/\r?\n/)
      .filter(function (line) {
        return !/^External web content follows/i.test(line) && !/^Fetched URL/i.test(line) && !/^Cite the relevant/i.test(line);
      })
      .join(' ');
    return {
      mode: 'fetch',
      queries: [],
      url: url,
      status: status,
      sources: extractLinks(result),
      excerpt: truncate(collapse(clean), 320)
    };
  }

  const queries: string[] = [];
  if (Array.isArray(args.queries)) {
    for (const item of args.queries) {
      if (typeof item === 'string' && item.trim()) queries.push(item.trim());
    }
  }
  if (!queries.length) {
    const single = textValue(args.query);
    if (single) queries.push(single);
  }

  const lines = result.split(/\r?\n/);
  let sourcesStart = -1;
  for (let index = 0; index < lines.length; index += 1) {
    if (/^Sources:\s*$/.test(lines[index].trim())) {
      sourcesStart = index;
      break;
    }
  }
  const sources: WebSource[] = [];
  const listStart = sourcesStart < 0 ? 0 : sourcesStart + 1;
  for (let index = listStart; index < lines.length; index += 1) {
    const match = /^\s*-\s*\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/.exec(lines[index]);
    if (!match) continue;
    const title = match[1].trim();
    sources.push({ title: title || undefined, url: match[2] });
  }
  const excerptLines = (sourcesStart < 0 ? lines : lines.slice(0, sourcesStart)).filter(function (line) {
    const trimmed = line.trim();
    if (!trimmed) return false;
    if (/^External web content follows/i.test(trimmed)) return false;
    if (/^Cite the relevant/i.test(trimmed)) return false;
    if (trimmed.charAt(0) === '-') return false;
    return true;
  });
  return {
    mode: 'search',
    queries: queries,
    sources: sources,
    excerpt: truncate(collapse(excerptLines.join(' ')), 320)
  };
}

/* ------------------------------------------------------------------ */
/* todo / present / ask / directory                                    */
/* ------------------------------------------------------------------ */

export function todoView(call: ToolCall): TodoItem[] {
  const args = parseArgs(call);
  const raw = Array.isArray(args.todos) ? args.todos : [];
  const items: TodoItem[] = [];
  for (const entry of raw) {
    if (!isRecord(entry)) continue;
    const content = textValue(entry.content);
    if (!content) continue;
    let status: TodoStatus = 'pending';
    if (entry.status === 'completed') status = 'completed';
    else if (entry.status === 'in_progress') status = 'in_progress';
    items.push({ content: content, status: status });
  }
  return items;
}

export function presentView(call: ToolCall): PresentFile[] {
  const args = parseArgs(call);
  const files: PresentFile[] = [];
  if (Array.isArray(args.files)) {
    for (const entry of args.files) {
      if (isRecord(entry)) {
        const path = textValue(entry.path);
        if (!path) continue;
        files.push({ path: path, description: textValue(entry.description) });
      } else if (typeof entry === 'string' && entry.trim()) {
        files.push({ path: entry.trim() });
      }
    }
  }
  if (!files.length && typeof call.result === 'string') {
    for (const raw of call.result.split(/\r?\n/)) {
      const match = /^Presented\s+(.+)$/.exec(raw.trim());
      if (match) files.push({ path: match[1].trim() });
    }
  }
  return files;
}

export function askView(call: ToolCall): AskQuestion[] {
  const args = parseArgs(call);
  const raw = Array.isArray(args.questions) ? args.questions : [];
  const answers = new Map<string, { selected: string[]; custom?: string }>();
  const parsed = parseJson(typeof call.result === 'string' ? call.result : '');
  if (isRecord(parsed) && Array.isArray(parsed.answers)) {
    for (const entry of parsed.answers) {
      if (!isRecord(entry)) continue;
      const id = textValue(entry.id);
      if (!id) continue;
      const selected: string[] = [];
      if (Array.isArray(entry.selected)) {
        for (const item of entry.selected) if (typeof item === 'string') selected.push(item);
      }
      answers.set(id, { selected: selected, custom: textValue(entry.custom) });
    }
  }

  const questions: AskQuestion[] = [];
  for (const entry of raw) {
    if (!isRecord(entry)) continue;
    const question = textValue(entry.question);
    if (!question) continue;
    const id = textValue(entry.id);
    const options: AskOption[] = [];
    if (Array.isArray(entry.options)) {
      for (const option of entry.options) {
        if (!isRecord(option)) continue;
        const label = textValue(option.label);
        if (!label) continue;
        options.push({ label: label, description: textValue(option.description) });
      }
    }
    const answer = id ? answers.get(id) : undefined;
    questions.push({
      id: id,
      question: question,
      header: textValue(entry.header),
      options: options,
      multiSelect: entry.multi_select === true,
      selected: answer ? answer.selected : [],
      custom: answer ? answer.custom : undefined
    });
  }
  return questions;
}

export function directoryView(call: ToolCall): DirectoryView {
  const args = parseArgs(call);
  const requested = textValue(args.cd);
  let path: string | undefined;
  if (typeof call.result === 'string') {
    const first = call.result.trim().split(/\r?\n/)[0] ?? '';
    const cleaned = first.replace(/^Working directory:\s*/i, '').trim();
    if (cleaned && cleaned.indexOf(' ') < 0) path = cleaned;
  }
  if (!path) path = requested;
  return { path: path, requested: requested };
}

/* ------------------------------------------------------------------ */
/* agent / goal                                                        */
/* ------------------------------------------------------------------ */

export function agentView(call: ToolCall): AgentView {
  const args = parseArgs(call);
  return {
    name: textValue(args.name) ?? textValue(args.agent),
    description: textValue(args.description),
    message: textValue(args.message),
    prompt: textValue(args.prompt),
    action: textValue(args.action)
  };
}

export function goalInfo(call: ToolCall): GoalInfo {
  const args = parseArgs(call);
  const parsed = parseJson(typeof call.result === 'string' ? call.result : '');
  return {
    objective: textValue(args.objective) ?? findText(parsed, 'objective'),
    status: findText(parsed, 'status'),
    phase: findText(parsed, 'phase'),
    blockedReason: textValue(args.blocked_reason) ?? findBlockedReason(parsed)
  };
}

/* ------------------------------------------------------------------ */
/* generic field list (job / schedule / skill / plugin / memory / task)*/
/* ------------------------------------------------------------------ */

const FIELD_LABELS: Record<string, string> = {
  objective: 'Objective',
  action: 'Action',
  blocked_reason: 'Blocked reason',
  status: 'Status',
  reason: 'Reason',
  description: 'Description',
  name: 'Name',
  subject: 'Subject',
  title: 'Title',
  prompt: 'Prompt',
  message: 'Message',
  job_id: 'Background job',
  task_id: 'Task',
  member_id: 'Teammate',
  goal_id: 'Goal',
  agent: 'Agent',
  provider: 'Provider',
  model: 'Model',
  preset: 'Preset',
  file_path: 'File',
  path: 'Path',
  command: 'Command',
  cwd: 'Directory',
  workdir: 'Directory',
  pattern: 'Pattern',
  include: 'Include',
  query: 'Query',
  queries: 'Queries',
  url: 'URL',
  rrule: 'Schedule',
  target: 'Target',
  limit: 'Limit',
  offset: 'Offset',
  active: 'Active',
  enabled: 'Enabled',
  bundle_id: 'Plugin',
  plugin_id: 'Plugin',
  source: 'Source',
  dest: 'Destination',
  category: 'Category',
  importance: 'Importance',
  tags: 'Tags',
  entities: 'Entities',
  memoryBodyId: 'Memory space',
  mode: 'Mode',
  old_text: 'Replace',
  text: 'Goal',
  clear: 'Clear',
  id: 'ID',
  what: 'Target',
  content: 'Content',
  patch: 'Patch',
  expected_revision: 'Revision',
  expectedUpdatedAt: 'Last updated',
  input: 'Input',
  timeoutMs: 'Timeout',
  timeout_ms: 'Timeout',
  run_in_background: 'Background',
  replace_all: 'Replace all',
  multi_select: 'Multiple answers',
  files: 'Files',
  todos: 'To-dos',
  questions: 'Questions'
};

const FIELD_ORDER = [
  'objective',
  'action',
  'blocked_reason',
  'status',
  'reason',
  'description',
  'name',
  'subject',
  'title',
  'prompt',
  'message',
  'job_id',
  'task_id',
  'member_id',
  'goal_id',
  'agent',
  'provider',
  'model',
  'preset',
  'file_path',
  'path',
  'command',
  'cwd',
  'workdir',
  'pattern',
  'include',
  'query',
  'queries',
  'url',
  'rrule',
  'target',
  'limit',
  'offset',
  'active',
  'enabled',
  'bundle_id',
  'plugin_id',
  'source',
  'dest',
  'category',
  'importance',
  'tags',
  'entities',
  'memoryBodyId',
  'mode',
  'old_text',
  'text',
  'clear',
  'id',
  'what',
  'content',
  'patch',
  'expected_revision',
  'expectedUpdatedAt',
  'input',
  'timeoutMs',
  'run_in_background',
  'replace_all',
  'multi_select',
  'files',
  'todos',
  'questions'
];

const MONO_KEYS = new Set([
  'file_path',
  'path',
  'command',
  'cwd',
  'workdir',
  'url',
  'pattern',
  'include',
  'rrule',
  'job_id',
  'task_id',
  'member_id',
  'goal_id',
  'bundle_id',
  'plugin_id',
  'id',
  'dest',
  'source',
  'memoryBodyId',
  'mode'
]);

function fieldValue(value: unknown): string | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed ? truncate(trimmed, 260) : undefined;
  }
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) {
    const parts: string[] = [];
    for (const entry of value) {
      const text = fieldValue(entry);
      if (text) parts.push(text);
    }
    return parts.length ? truncate(parts.join(', '), 260) : undefined;
  }
  if (isRecord(value)) {
    const text = jsonString(value, false);
    return text ? truncate(text, 260) : undefined;
  }
  return undefined;
}

export function detailFields(call: ToolCall): DetailField[] {
  const args = parseArgs(call);
  const fields: DetailField[] = [];
  const used = new Set<string>();

  function push(key: string): void {
    if (used.has(key)) return;
    if (!(key in args)) return;
    const value = fieldValue(args[key]);
    if (!value) return;
    used.add(key);
    const label = FIELD_LABELS[key] ?? humanizeName(key);
    fields.push({ label: label, value: value, mono: MONO_KEYS.has(key) });
  }

  for (const key of FIELD_ORDER) push(key);
  for (const key of Object.keys(args)) push(key);

  return fields;
}
