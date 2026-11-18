"""Frontend assets for the loopback web UI, embedded as Python strings.

The page is a static shell: it owns no conversation state, replays the
server-side transcript, and renders every model-provided string as text (never
as markup). Assets live in Python so packaging needs no extra data files.
"""

from __future__ import annotations

import html
from urllib.parse import urlencode

from deeppyro.web.security import TOKEN_QUERY

TITLE = "Deeppyro Web"

#: Placeholder replaced by the URL-encoded token in the two asset URLs.
TOKEN_PLACEHOLDER = "__TOKEN_QUERY__"

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Deeppyro Web</title>
<link rel="stylesheet" href="/assets/app.css?__TOKEN_QUERY__">
</head>
<body>
<div class="banner" role="status">
  <strong>Localhost only.</strong> This page is served from 127.0.0.1 and is reachable by this machine only.
  It is a shell over the same deeppyro plugin kernel and native agent runtime - not a second agent.
</div>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <h1>deeppyro</h1>
      <button id="new-session" type="button" class="primary">New session</button>
    </div>
    <div class="sidebar-label">Sessions</div>
    <ul id="session-list" class="sessions"></ul>
    <div class="sidebar-foot">
      <span id="connection" class="conn" data-state="offline">offline</span>
      <span id="host-label">127.0.0.1</span>
    </div>
  </aside>
  <main class="main">
    <header class="topbar">
      <div class="titles">
        <div id="session-title" class="session-title">No session selected</div>
        <div id="session-id" class="session-id">session: -</div>
      </div>
      <div class="meta">
        <span id="status-model" class="meta-item">model: -</span>
        <span id="status-preset" class="meta-item">preset: -</span>
        <span id="status-context" class="meta-item">context: -</span>
        <span id="status-usage" class="meta-item">tokens: -</span>
      </div>
    </header>
    <section id="notice" class="notice" hidden></section>
    <section id="transcript" class="transcript" aria-live="polite"></section>
    <footer class="composer">
      <label class="composer-label" for="composer-input">Message</label>
      <textarea id="composer-input" rows="2" placeholder="Ask deeppyro... (Ctrl+Enter to send)" spellcheck="false"></textarea>
      <div class="composer-actions">
        <button id="stop" type="button" class="danger" disabled>Stop</button>
        <button id="send" type="button" class="primary">Send</button>
      </div>
      <div class="hint">Ctrl+Enter send &middot; Esc clears the composer &middot; Stop interrupts through the native agent host</div>
    </footer>
  </main>
</div>
<script src="/assets/app.js?__TOKEN_QUERY__"></script>
</body>
</html>
"""

APP_CSS = """/* Deeppyro Web - embedded stylesheet, no build step, no external fonts. */
:root {
  --bg: #0a0e14;
  --panel: #111826;
  --panel-2: #17202f;
  --line: #263247;
  --text: #e7edf6;
  --muted: #8b99ae;
  --accent: #4f8cff;
  --accent-soft: rgba(79, 140, 255, 0.16);
  --danger: #ff6f6f;
  --danger-soft: rgba(255, 111, 111, 0.14);
  --ok: #46d59a;
  --warn: #e5b567;
  --radius: 10px;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
button {
  font: inherit;
  color: var(--text);
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 6px 12px;
  cursor: pointer;
}
button:hover:not(:disabled) { border-color: var(--accent); }
button:disabled { opacity: 0.45; cursor: not-allowed; }
button.primary { background: var(--accent); border-color: var(--accent); color: #06101f; font-weight: 600; }
button.danger { border-color: var(--danger); color: var(--danger); background: var(--danger-soft); }
button:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.banner {
  padding: 8px 14px;
  background: var(--accent-soft);
  border-bottom: 1px solid var(--line);
  color: var(--text);
  font-size: 12.5px;
}
.banner strong { color: var(--accent); }
.app { display: grid; grid-template-columns: 264px minmax(0, 1fr); height: calc(100dvh - 37px); }
.sidebar {
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--panel);
  border-right: 1px solid var(--line);
  padding: 12px;
  gap: 10px;
}
.brand { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.brand h1 { margin: 0; font-size: 16px; letter-spacing: 0.02em; }
.sidebar-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; }
.sessions { list-style: none; margin: 0; padding: 0; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 4px; }
.sessions li button {
  width: 100%;
  text-align: left;
  background: transparent;
  border-color: transparent;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.sessions li button[aria-current="true"] { background: var(--accent-soft); border-color: var(--line); }
.session-title { font-weight: 600; }
.session-id, .session-meta, .meta-item, .sidebar-foot, .hint, .sidebar-label {
  color: var(--muted);
  font-size: 11.5px;
}
.session-meta { font-family: var(--mono); }
.sessions .empty, .transcript .empty {
  color: var(--muted);
  font-size: 12.5px;
  padding: 8px;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
}
.sidebar-foot { display: flex; align-items: center; justify-content: space-between; border-top: 1px solid var(--line); padding-top: 8px; }
.main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.topbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
  flex-wrap: wrap;
}
.titles { min-width: 0; }
.session-title { font-size: 14px; font-weight: 600; }
.session-id { font-family: var(--mono); }
.meta { display: flex; gap: 12px; flex-wrap: wrap; }
.meta-item { font-family: var(--mono); }
.notice { margin: 8px 14px 0; padding: 8px 10px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel-2); font-size: 12.5px; }
.notice[data-kind="error"] { border-color: var(--danger); background: var(--danger-soft); color: var(--danger); }
.notice[data-kind="ok"] { border-color: var(--ok); color: var(--ok); }
.transcript { flex: 1; min-height: 0; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.msg { max-width: min(760px, 92%); display: flex; flex-direction: column; gap: 4px; }
.msg--user { align-self: flex-end; align-items: flex-end; }
.msg--assistant { align-self: flex-start; }
.msg__role { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; }
.msg__text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  padding: 10px 12px;
  border-radius: var(--radius);
  border: 1px solid var(--line);
  background: var(--panel);
  font-size: 13.5px;
}
.msg--user .msg__text { background: var(--accent-soft); border-color: transparent; }
.msg--system .msg__text { color: var(--muted); font-family: var(--mono); font-size: 12px; }
.thinking { margin-top: 2px; color: var(--muted); font-size: 12px; }
.thinking summary { cursor: pointer; }
.thinking pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 4px 0 0; font-family: var(--mono); }
.tool {
  align-self: flex-start;
  max-width: min(760px, 92%);
  border: 1px solid var(--line);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius);
  background: var(--panel-2);
  padding: 6px 10px;
  font-family: var(--mono);
  font-size: 12px;
}
.tool[data-state="done"] { border-left-color: var(--ok); }
.tool[data-state="error"] { border-left-color: var(--danger); }
.tool__head { color: var(--warn); }
.tool__args, .tool__result { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--muted); }
.tool[data-state="error"] .tool__result { color: var(--danger); }
.composer { border-top: 1px solid var(--line); padding: 10px 14px 12px; display: flex; flex-direction: column; gap: 8px; }
.composer-label { display: none; }
textarea {
  width: 100%;
  resize: vertical;
  min-height: 46px;
  max-height: 40vh;
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 8px 10px;
  font: inherit;
}
.composer-actions { display: flex; justify-content: flex-end; gap: 8px; }
.hint { text-align: right; }
.conn { font-family: var(--mono); font-size: 11.5px; }
.conn::before { content: ""; display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: var(--muted); vertical-align: middle; }
.conn[data-state="online"] { color: var(--ok); }
.conn[data-state="online"]::before { background: var(--ok); }
.conn[data-state="thinking"] { color: var(--warn); }
.conn[data-state="thinking"]::before { background: var(--warn); }
.conn[data-state="retry"] { color: var(--warn); }
.conn[data-state="retry"]::before { background: var(--warn); }
.conn[data-state="offline"] { color: var(--danger); }
.conn[data-state="offline"]::before { background: var(--danger); }
@media (max-width: 760px) {
  .app { grid-template-columns: 1fr; grid-template-rows: minmax(120px, 30dvh) minmax(0, 1fr); height: auto; min-height: calc(100dvh - 37px); }
  .sidebar { border-right: 0; border-bottom: 1px solid var(--line); max-height: 30dvh; }
  .main { min-height: 60dvh; }
  .transcript { padding: 12px; }
  .msg { max-width: 96%; }
}
[hidden] { display: none !important; }
"""

APP_JS = """/* Deeppyro Web frontend: vanilla ES2020, no dependencies, no build step.
 * The page is a shell: it never invents conversation state. All text comes
 * from the server (transcript replay and SSE events) and is rendered with
 * textContent so model output can never become markup.
 */
(function () {
  'use strict';

  var TOKEN = 'token';
  var token = '';
  try {
    token = new URLSearchParams(window.location.search).get(TOKEN) || '';
  } catch (err) {
    token = '';
  }

  function byId(id) {
    return document.getElementById(id);
  }

  var ui = {
    banner: document.querySelector('.banner'),
    connection: byId('connection'),
    sessionList: byId('session-list'),
    sessionTitle: byId('session-title'),
    sessionId: byId('session-id'),
    statusModel: byId('status-model'),
    statusPreset: byId('status-preset'),
    statusContext: byId('status-context'),
    statusUsage: byId('status-usage'),
    notice: byId('notice'),
    transcript: byId('transcript'),
    input: byId('composer-input'),
    send: byId('send'),
    stop: byId('stop'),
    newSession: byId('new-session')
  };

  var state = {
    sessionId: null,
    sessions: [],
    seq: 0,
    source: null,
    busy: false,
    activeMessage: null,
    activeText: null,
    thinkingText: null,
    toolNodes: new Map(),
    cwd: null
  };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  function setConnection(kind, label) {
    ui.connection.setAttribute('data-state', kind);
    ui.connection.textContent = label;
  }

  function showNotice(message, kind) {
    if (!message) {
      ui.notice.hidden = true;
      ui.notice.textContent = '';
      return;
    }
    ui.notice.hidden = false;
    ui.notice.setAttribute('data-kind', kind || 'info');
    ui.notice.textContent = message;
  }

  function scrollDown() {
    ui.transcript.scrollTop = ui.transcript.scrollHeight;
  }

  function addMessage(role, text) {
    var wrapper = el('div', 'msg msg--' + role);
    wrapper.appendChild(el('div', 'msg__role', role));
    var body = el('div', 'msg__text', text || '');
    wrapper.appendChild(body);
    ui.transcript.appendChild(wrapper);
    scrollDown();
    return body;
  }

  function addSystem(text) {
    return addMessage('system', text);
  }

  function formatNumber(value) {
    if (typeof value !== 'number' || !isFinite(value)) { return '-'; }
    return String(value);
  }

  function formatUsage(usage) {
    if (!usage) { return 'tokens: -'; }
    return 'tokens: in ' + formatNumber(usage.input_tokens) +
      ' / out ' + formatNumber(usage.output_tokens) +
      ' / cached ' + formatNumber(usage.cached_input_tokens);
  }

  function renderStatus(data) {
    if (!data) { return; }
    if (data.session_id) {
      state.sessionId = data.session_id;
      ui.sessionId.textContent = 'session: ' + data.session_id;
    }
    if (data.title) { ui.sessionTitle.textContent = data.title; }
    ui.statusModel.textContent = 'model: ' + (data.model || '-');
    ui.statusPreset.textContent = 'preset: ' + (data.preset || '-');
    var context = '-';
    if (typeof data.context_tokens === 'number') {
      context = formatNumber(data.context_tokens);
      if (typeof data.context_window === 'number') {
        context = context + ' / ' + formatNumber(data.context_window);
      }
    }
    ui.statusContext.textContent = 'context: ' + context;
    ui.statusUsage.textContent = formatUsage(data.usage);
    setBusy(Boolean(data.busy));
  }

  function setBusy(busy) {
    state.busy = busy;
    ui.stop.disabled = !busy;
    if (busy) { setConnection('thinking', 'thinking'); }
    else if (state.source) { setConnection('online', 'live'); }
  }

  function renderSessions() {
    clear(ui.sessionList);
    if (!state.sessions.length) {
      var empty = el('li', 'empty', 'No native sessions yet.');
      ui.sessionList.appendChild(empty);
      return;
    }
    state.sessions.forEach(function (item) {
      var li = el('li');
      var button = el('button', '', null);
      button.type = 'button';
      if (item.session_id === state.sessionId) { button.setAttribute('aria-current', 'true'); }
      button.appendChild(el('span', 'session-title', item.title || '(untitled)'));
      var meta = 'turns ' + formatNumber(item.turns) + ' - ' + (item.session_id || '').slice(0, 8);
      if (item.read_only) { meta = meta + ' - read-only'; }
      if (item.readable === false) { meta = meta + ' - damaged'; }
      button.appendChild(el('span', 'session-meta', meta));
      button.addEventListener('click', function () {
        resumeSession(item.session_id).catch(handleError);
      });
      li.appendChild(button);
      ui.sessionList.appendChild(li);
    });
  }

  function handleError(err) {
    var message = err && err.message ? err.message : String(err);
    showNotice(message, 'error');
  }

  function request(path, options) {
    var opts = options || {};
    var init = {
      method: opts.method || 'GET',
      headers: { 'authorization': 'Bearer ' + token },
      cache: 'no-store'
    };
    if (opts.body !== undefined) {
      init.headers['content-type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var error = new Error((data.error && data.error.message) || ('HTTP ' + response.status));
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  function resetConversationView() {
    clear(ui.transcript);
    state.activeMessage = null;
    state.activeText = null;
    state.thinkingText = null;
    state.toolNodes.clear();
    showNotice('');
  }

  function loadTranscript(sessionId) {
    return request('/api/sessions/' + encodeURIComponent(sessionId) + '/transcript').then(function (data) {
      resetConversationView();
      if (data.readable === false) {
        showNotice('This session file could not be read completely: ' + (data.issues || []).join('; '), 'error');
      }
      var entries = data.entries || [];
      if (!entries.length) {
        addSystem('No messages yet. Type below to start the conversation.');
      }
      entries.forEach(function (entry) {
        var role = entry.role === 'user' ? 'user' : (entry.role === 'assistant' ? 'assistant' : 'system');
        addMessage(role, entry.text || '');
      });
      return data;
    });
  }

  function applyEvent(event) {
    var type = event.type;
    if (type === 'text') {
      appendStreamText(event.text);
    } else if (type === 'thinking') {
      appendThinking(event.text);
    } else if (type === 'tool_call') {
      addToolCall(event);
    } else if (type === 'tool_result') {
      finishToolCall(event);
    } else if (type === 'final') {
      finishMessage(event.text);
    } else if (type === 'usage') {
      ui.statusUsage.textContent = formatUsage({
        input_tokens: event.input_tokens,
        output_tokens: event.output_tokens,
        cached_input_tokens: event.cached_input_tokens
      });
    } else if (type === 'status') {
      if (event.phase === 'thinking') {
        setBusy(true);
      } else {
        setBusy(false);
        if (event.phase === 'interrupted') { addSystem('Interrupted.'); }
      }
    } else if (type === 'error') {
      addSystem('Error: ' + (event.message || 'unknown error'));
    } else if (type === 'step') {
      /* step counters are diagnostic only; the transcript shows activity. */
    }
  }

  function ensureAssistantMessage() {
    if (state.activeMessage) { return; }
    state.activeMessage = addMessage('assistant', '');
    state.activeText = state.activeMessage;
  }

  function appendStreamText(text) {
    if (!text) { return; }
    ensureAssistantMessage();
    state.activeText.textContent = state.activeText.textContent + text;
    scrollDown();
  }

  function appendThinking(text) {
    if (!text) { return; }
    ensureAssistantMessage();
    if (!state.thinkingText) {
      var details = el('details', 'thinking');
      details.appendChild(el('summary', '', 'thinking'));
      var pre = el('pre', 'thinking__text');
      details.appendChild(pre);
      state.activeMessage.parentNode.appendChild(details);
      state.thinkingText = pre;
    }
    state.thinkingText.textContent = state.thinkingText.textContent + text;
    scrollDown();
  }

  function addToolCall(event) {
    var node = el('div', 'tool');
    node.setAttribute('data-state', 'running');
    node.setAttribute('data-call-id', event.call_id || '');
    node.appendChild(el('div', 'tool__head', 'tool: ' + (event.name || 'unknown')));
    if (event.arguments) {
      node.appendChild(el('div', 'tool__args', event.arguments));
    }
    var result = el('div', 'tool__result', 'running...');
    node.appendChild(result);
    ui.transcript.appendChild(node);
    if (event.call_id) { state.toolNodes.set(event.call_id, node); }
    scrollDown();
  }

  function finishToolCall(event) {
    var node = event.call_id ? state.toolNodes.get(event.call_id) : null;
    if (!node) {
      node = el('div', 'tool');
      node.appendChild(el('div', 'tool__head', 'tool result'));
      ui.transcript.appendChild(node);
    }
    node.setAttribute('data-state', event.is_error ? 'error' : 'done');
    var result = node.querySelector('.tool__result') || el('div', 'tool__result', '');
    if (!result.parentNode) { node.appendChild(result); }
    result.textContent = event.text ? event.text : '(no output)';
    scrollDown();
  }

  function finishMessage(text) {
    if (state.activeText) {
      state.activeText.textContent = text || '';
    } else if (text) {
      addMessage('assistant', text);
    }
    state.activeMessage = null;
    state.activeText = null;
    state.thinkingText = null;
    scrollDown();
  }

  function connectStream(sessionId) {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
    var url = '/api/sessions/' + encodeURIComponent(sessionId) + '/events?token=' +
      encodeURIComponent(token) + '&after=' + state.seq;
    var source = new EventSource(url);
    state.source = source;
    source.onopen = function () {
      setConnection(state.busy ? 'thinking' : 'online', state.busy ? 'thinking' : 'live');
    };
    source.onerror = function () {
      if (source.readyState === EventSource.CLOSED) {
        setConnection('offline', 'disconnected');
      } else {
        setConnection('retry', 'reconnecting');
      }
    };
    source.onmessage = function (message) {
      var data;
      try {
        data = JSON.parse(message.data);
      } catch (err) {
        return;
      }
      if (typeof data.seq === 'number') { state.seq = data.seq; }
      applyEvent(data);
    };
  }

  function activateSession(sessionId) {
    state.sessionId = sessionId;
    state.seq = 0;
    ui.sessionTitle.textContent = '(untitled)';
    ui.sessionId.textContent = 'session: ' + sessionId;
    renderSessions();
    return Promise.all([
      loadTranscript(sessionId),
      request('/api/sessions/' + encodeURIComponent(sessionId)).then(renderStatus)
    ]).then(function () {
      connectStream(sessionId);
    });
  }

  function createSession() {
    return request('/api/sessions', { method: 'POST', body: {} }).then(function (data) {
      showNotice('');
      return refreshSessions().then(function () { return activateSession(data.session_id); });
    });
  }

  function resumeSession(sessionId) {
    return request('/api/sessions/' + encodeURIComponent(sessionId) + '/resume', {
      method: 'POST', body: {}
    }).then(function (data) {
      showNotice('');
      return refreshSessions().then(function () { return activateSession(data.session_id); });
    });
  }

  function refreshSessions() {
    return request('/api/sessions').then(function (data) {
      state.sessions = data.sessions || [];
      renderSessions();
      return data;
    });
  }

  function sendMessage() {
    var text = ui.input.value.trim();
    if (!text) { return; }
    if (!state.sessionId) {
      showNotice('Create or select a session first.', 'error');
      return;
    }
    ui.input.value = '';
    request('/api/sessions/' + encodeURIComponent(state.sessionId) + '/messages', {
      method: 'POST', body: { text: text }
    }).then(function (data) {
      showNotice('');
      addMessage('user', text);
      setBusy(true);
      if (data.queued) {
        addSystem('Queued behind the active turn (' + data.queued + ' waiting).');
      }
    }).catch(handleError);
  }

  function interruptTurn() {
    if (!state.sessionId) { return; }
    request('/api/sessions/' + encodeURIComponent(state.sessionId) + '/interrupt', {
      method: 'POST', body: {}
    }).then(function (data) {
      showNotice(data.detail || (data.interrupted ? 'Interrupt requested.' : 'Nothing to interrupt.'), 'ok');
    }).catch(handleError);
  }

  function bootstrap() {
    if (!token) {
      setConnection('offline', 'missing token');
      showNotice('Missing access token. Reopen the URL printed by python -m deeppyro.web.', 'error');
      ui.send.disabled = true;
      ui.stop.disabled = true;
      ui.input.disabled = true;
      return;
    }
    ui.send.addEventListener('click', sendMessage);
    ui.stop.addEventListener('click', interruptTurn);
    ui.newSession.addEventListener('click', function () {
      createSession().catch(handleError);
    });
    ui.input.addEventListener('keydown', function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        sendMessage();
      } else if (event.key === 'Escape') {
        ui.input.value = '';
        showNotice('');
      }
    });
    setConnection('offline', 'connecting');
    request('/api/health').then(function (data) {
      setConnection('online', 'ready');
      if (data.version) {
        document.title = 'Deeppyro Web ' + data.version;
      }
    }).catch(handleError);
    refreshSessions().then(function (data) {
      if (data.active_session_id) {
        return activateSession(data.active_session_id);
      }
      if (state.sessions.length) {
        return activateSession(state.sessions[0].session_id);
      }
      return undefined;
    }).catch(handleError);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
  } else {
    bootstrap();
  }
})();
"""


def index_html(token: str) -> str:
    """Return the app shell with the bearer token injected into asset URLs."""
    query = urlencode({TOKEN_QUERY: token})
    return INDEX_HTML.replace(TOKEN_PLACEHOLDER, html.escape(query, quote=True))


__all__ = ["APP_CSS", "APP_JS", "INDEX_HTML", "TITLE", "index_html"]
