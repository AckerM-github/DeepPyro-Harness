import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { subscribeEvents } from './api';
import type { ConnectionState, WebEvent } from './types';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((message: { lastEventId: string; data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  open() {
    this.onopen?.();
  }

  message(seq: number, event: Partial<WebEvent>) {
    this.onmessage?.({ lastEventId: String(seq), data: JSON.stringify({ ...event, seq }) });
  }

  fail() {
    this.onerror?.();
  }
}

let listeners: Map<string, Set<() => void>>;

function installBrowser() {
  listeners = new Map();
  const fakeWindow = {
    sessionStorage: { getItem: () => null },
    setTimeout,
    clearTimeout,
    addEventListener: (type: string, handler: () => void) => {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type)?.add(handler);
    },
    removeEventListener: (type: string, handler: () => void) => {
      listeners.get(type)?.delete(handler);
    },
  };
  vi.stubGlobal('window', fakeWindow);
  vi.stubGlobal('navigator', { onLine: true });
  vi.stubGlobal('EventSource', FakeEventSource);
}

describe('subscribeEvents', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    installBrowser();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('subscribes from the requested sequence and reports state', () => {
    const states: ConnectionState[] = [];
    const events: WebEvent[] = [];
    const handle = subscribeEvents('session-1', 0, (event) => events.push(event), (state) => states.push(state));

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toContain('/api/ui/sessions/session-1/events?after=0');
    expect(states).toEqual(['connecting']);

    FakeEventSource.instances[0].open();
    FakeEventSource.instances[0].message(5, { type: 'text', session_id: 'session-1', text: 'hi' });

    expect(states).toEqual(['connecting', 'live']);
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ type: 'text', text: 'hi', seq: 5 });
    handle.close();
  });

  it('reconnects with backoff and resumes after the last seen sequence', async () => {
    const states: ConnectionState[] = [];
    const handle = subscribeEvents('session-1', 0, () => undefined, (state) => states.push(state));
    const first = FakeEventSource.instances[0];

    first.open();
    first.message(7, { type: 'text', session_id: 'session-1', text: 'before' });
    first.fail();
    expect(states.at(-1)).toBe('reconnecting');
    expect(first.closed).toBe(true);

    await vi.advanceTimersByTimeAsync(799);
    expect(FakeEventSource.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);

    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toContain('after=7');

    FakeEventSource.instances[1].open();
    expect(states.at(-1)).toBe('live');
    handle.close();
  });

  it('tears the stream down immediately when the browser reports offline', () => {
    const states: ConnectionState[] = [];
    const handle = subscribeEvents('session-1', 0, () => undefined, (state) => states.push(state));
    const first = FakeEventSource.instances[0];
    first.open();

    (globalThis.navigator as { onLine: boolean }).onLine = false;
    listeners.get('offline')?.forEach((handler) => handler());

    expect(first.closed).toBe(true);
    expect(states.at(-1)).toBe('offline');
    handle.close();
  });

  it('waits for the online event instead of burning the retry budget', async () => {
    const states: ConnectionState[] = [];
    const handle = subscribeEvents('session-1', 3, () => undefined, (state) => states.push(state));
    const first = FakeEventSource.instances[0];

    (globalThis.navigator as { onLine: boolean }).onLine = false;
    first.fail();
    expect(states.at(-1)).toBe('offline');

    await vi.advanceTimersByTimeAsync(10_000);
    expect(FakeEventSource.instances).toHaveLength(1);

    (globalThis.navigator as { onLine: boolean }).onLine = true;
    listeners.get('online')?.forEach((handler) => handler());
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toContain('after=3');
    handle.close();
  });
});
