/** Turn transport/API failures into one sentence a person can act on. */
export function humanError(error: unknown): string {
  const raw = error instanceof Error ? error.message : typeof error === 'string' ? error : '';
  const lower = raw.toLowerCase();
  if (
    lower.includes('failed to fetch') ||
    lower.includes('networkerror') ||
    lower.includes('unreachable') ||
    lower.includes('did not answer')
  ) {
    return 'Can’t reach the local runtime. Check that DeepPyro is still running.';
  }
  if (/\b(401|403)\b/.test(raw) || lower.includes('unauthorized') || lower.includes('forbidden')) {
    return 'Your session expired. Reload the page to sign in again.';
  }
  if (/\b404\b/.test(raw) || lower.includes('not found')) {
    return 'That session is no longer available.';
  }
  if (/\b409\b/.test(raw) || lower.includes('attached')) {
    return 'Another tab is attached to this session. Close the other tab and try again.';
  }
  if (/\b429\b/.test(raw) || lower.includes('too many')) {
    return 'There are already too many queued messages. Wait for the current turn to finish.';
  }
  return raw || 'Something went wrong. Please try again.';
}

export function shortAgo(epochMs: number): string {
  if (!epochMs) return '';
  const delta = Math.max(0, Math.floor((Date.now() - epochMs) / 1000));
  if (delta < 60) return 'now';
  if (delta < 3600) return `${Math.floor(delta / 60)}m`;
  if (delta < 86_400) return `${Math.floor(delta / 3600)}h`;
  if (delta < 604_800) return `${Math.floor(delta / 86_400)}d`;
  return new Date(epochMs).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function groupLabel(epochMs: number): string {
  if (!epochMs) return 'Older';
  const date = new Date(epochMs);
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const value = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((start - value) / 86_400_000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days <= 7) return 'Previous 7 days';
  return 'Older';
}

export function clockTime(epochMs: number): string {
  if (!epochMs) return '';
  return new Date(epochMs).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatTokens(value: number): string {
  if (!Number.isFinite(value)) return '0';
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 100_000 ? 0 : 1)}k`;
  return String(Math.round(value));
}

/** Compact elapsed time for a running turn: 12s, 1m 04s, 2h 10m. */
export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes}m ${String(rest).padStart(2, '0')}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

export function basename(path: string): string {
  if (!path) return '';
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

export function sessionTitle(title: string): string {
  return title.trim() || '(untitled)';
}

export function prettyJson(value: unknown): string {
  if (value === null || value === undefined || value === '') return '';
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function splitArgs(text: string): string[] {
  const input = text.trim();
  if (!input) return [];
  const args: string[] = [];
  let current = '';
  let quote = '';
  for (const char of input) {
    if (quote) {
      if (char === quote) quote = '';
      else current += char;
    } else if (char === '"' || char === "'") {
      quote = char;
    } else if (/\s/.test(char)) {
      if (current) {
        args.push(current);
        current = '';
      }
    } else {
      current += char;
    }
  }
  if (current) args.push(current);
  return args;
}
