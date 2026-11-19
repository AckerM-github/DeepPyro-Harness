/* DeepPyro local web client.

The shell ships no data. Every value rendered here comes from the same-origin
JSON API, and every dynamic string reaches the DOM through textContent or a
validated attribute, never through parsed markup.

Structure:
  constants, small DOM helpers, API layer, toasts
  bootstrap + sidebar/inspector rendering
  routing between chat, palette and settings
  transcript rendering and the SSE stream
  composer actions and slash commands
  command palette
  settings sections, forms and saves
  keyboard shortcuts
*/

'use strict';

(function () {
  // ------------------------------------------------------------------
  // constants
  // ------------------------------------------------------------------

  var SVG_NS = 'http://www.w3.org/2000/svg';

  var ASSETS = {
    mark: '/ui/cyborg-mark.svg'
  };

  var API = {
    bootstrap: '/api/ui/bootstrap',
    sessions: '/api/ui/sessions',
    commands: '/api/ui/commands',
    settings: '/api/ui/settings',
    settingsAgent: '/api/ui/settings/agent',
    settingsProvider: '/api/ui/settings/provider/',
    settingsTest: '/api/ui/settings/providers/',
    settingsPermission: '/api/ui/settings/permission',
    configAgent: '/api/ui/config/agent',
    configProvider: '/api/ui/config/provider/',
    configTest: '/api/ui/config/test',
    permission: '/api/ui/permission',
    pluginSettings: '/api/ui/plugins/',
    events: '/api/ui/sessions/',
    approvalsList: '/api/ui/approvals',
    approvalsGlobal: '/api/ui/approvals/',
    approvals: '/api/sessions/',
    transcript: '/api/sessions/'
  };

  var ICONS = {
    plus: ['M12 5v14M5 12h14'],
    stop: ['M6 6h12v12H6z'],
    'arrow-up': ['M12 19V5M5 12l7-7 7 7'],
    x: ['M18 6 6 18M6 6l12 12'],
    refresh: ['M20 11a8 8 0 0 0-14.9-4M4 4v5h5M4 13a8 8 0 0 0 14.9 4M20 20v-5h-5'],
    command: ['M18 3a3 3 0 0 0-3 3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 0 0 3-3z'],
    settings: ['M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z'],
    search: ['M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z', 'M21 21l-4.35-4.35'],
    message: ['M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z'],
    layers: ['m12 2 9 5-9 5-9-5 9-5z', 'M3 12l9 5 9-5', 'M3 17l9 5 9-5'],
    cpu: ['M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4', 'M6 6h12v12H6z', 'M9 9h6v6H9z'],
    monitor: ['M2 3h20v14H2z', 'M8 21h8M12 17v4'],
    external: ['M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6'],
    check: ['M20 6 9 17l-5-5'],
    alert: ['M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h16.9a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z'],
    info: ['M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z', 'M12 16v-4M12 8h.01'],
    tool: ['M14.7 6.3a4 4 0 0 1 5 5l-9.4 9.4a2.8 2.8 0 0 1-4-4z', 'M15 5l4 4'],
    code: ['m16 18 6-6-6-6M8 6l-6 6 6 6'],
    clock: ['M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z', 'M12 6v6l4 2'],
    key: ['M15 7a4 4 0 1 0-4 4l-7 7v3h3l7-7a4 4 0 0 0 4-4z'],
    user: ['M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M4 21a8 8 0 0 1 16 0'],
    plug: ['M12 22v-5M9 7V2M15 7V2M6 7h12v5a6 6 0 0 1-12 0z'],
    dot: ['M12 12h.01'],
    'chevron-down': ['m6 9 6 6 6-6'],
    'chevron-right': ['m9 18 6-6-6-6']
  };

  var SETTINGS_SECTIONS = [
    {id: 'provider', label: 'Provider and models', icon: 'cpu', summary: 'Model provider, API base URL, credentials and model routing.'},
    {id: 'agent', label: 'Agent behavior', icon: 'settings', summary: 'Temperature, token budget and step budget used by the local agent.'},
    {id: 'permissions', label: 'Permissions', icon: 'key', summary: 'Sandbox and approval preset applied to new sessions.'},
    {id: 'commands', label: 'Commands', icon: 'command', summary: 'Slash commands registered by the running plugins.'},
    {id: 'plugins', label: 'Plugins', icon: 'layers', summary: 'Installed plugins, their state and their settings schemas.'},
    {id: 'sessions', label: 'Sessions', icon: 'message', summary: 'Native sessions stored on this machine.'},
    {id: 'web', label: 'Web host', icon: 'monitor', summary: 'Loopback bind address served by the local web plugin.'},
    {id: 'about', label: 'About', icon: 'info', summary: 'Runtime version, platform and configuration path.'}
  ];

  var state = {
    bootstrapped: false,
    version: '',
    sessions: [],
    activeSessionId: '',
    commands: [],
    presets: [],
    providers: [],
    plugins: [],
    config: {
      path: '',
      agent: {provider: '', model: '', temperature: null, max_tokens: null, max_steps: null}
    },
    permission: {default_preset: '', approval: '', presets: []},
    web: {},
    about: {},
    home: '',
    cwd: '',
    counts: null,
    session: {
      id: '',
      title: '',
      cwd: '',
      readable: true,
      active: false,
      busy: false,
      pending: false,
      read_only: false,
      preset: '',
      permission_preset: '',
      turns: null,
      archived: false
    },
    status: {
      provider: '',
      model: '',
      context_tokens: null,
      context_window: null,
      usage: {input_tokens: null, output_tokens: null, cached_input_tokens: null}
    },
    messages: [],
    approvals: [],
    source: null,
    streamSessionId: '',
    lastEventSeq: 0,
    streamFloor: 0,
    draft: null,
    reconnectTimer: null,
    reconnectDelay: 1000,
    connected: false,
    loadingSessionId: '',
    queued: 0,
    palette: {items: [], filtered: [], index: 0, open: false},
    settings: {
      open: false,
      section: 'provider',
      loaded: false,
      loading: false,
      error: '',
      degraded: '',
      data: null,
      sections: [],
      pluginForms: {}
    },
    timelineTimer: null
  };

  // ------------------------------------------------------------------
  // small helpers
  // ------------------------------------------------------------------

  function byId(id) {
    return document.getElementById(id);
  }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function textOf(value, fallback) {
    if (value === undefined || value === null) return fallback === undefined ? '' : fallback;
    var text = String(value);
    return text === '' ? (fallback === undefined ? '' : fallback) : text;
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function intOrNull(value) {
    if (value === undefined || value === null || value === '') return null;
    var number = Number(value);
    return isFinite(number) ? Math.round(number) : null;
  }

  function numberOrNull(value) {
    if (value === undefined || value === null || value === '') return null;
    var number = Number(value);
    return isFinite(number) ? number : null;
  }

  function setText(id, value) {
    var node = byId(id);
    if (!node) return;
    var text = value === undefined || value === null || value === '' ? '\u2014' : String(value);
    if (node.textContent !== text) node.textContent = text;
  }

  function show(node, visible) {
    if (!node) return;
    node.classList.toggle('is-hidden', !visible);
  }

  function formatCount(value) {
    var number = intOrNull(value);
    return number === null ? '\u2014' : number.toLocaleString('en-US');
  }

  function formatTokens(value) {
    var number = intOrNull(value);
    if (number === null) return '\u2014';
    if (number >= 1000000) return (number / 1000000).toFixed(1) + 'M';
    if (number >= 1000) return (number / 1000).toFixed(1) + 'k';
    return String(number);
  }

  function formatPercent(part, total) {
    var got = numberOrNull(part);
    var all = numberOrNull(total);
    if (got === null || all === null || all <= 0) return 0;
    return Math.max(0, Math.min(100, (got / all) * 100));
  }

  function toDate(value) {
    if (value === undefined || value === null || value === '') return null;
    var date;
    if (typeof value === 'number') {
      date = new Date(value > 100000000000 ? value : value * 1000);
    } else {
      date = new Date(String(value));
    }
    return isNaN(date.getTime()) ? null : date;
  }

  function pad(number) {
    return (number < 10 ? '0' : '') + String(number);
  }

  function timeLabel(value) {
    var date = toDate(value);
    return date === null ? '\u2014' : pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  function dayLabel(value) {
    var date = toDate(value);
    return date === null ? '' : date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
  }

  function relativeLabel(value) {
    var date = toDate(value);
    if (date === null) return '';
    var seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return seconds + 's ago';
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + 'm ago';
    var hours = Math.round(minutes / 60);
    if (hours < 24) return hours + 'h ago';
    var days = Math.round(hours / 24);
    return days + 'd ago';
  }

  function shortId(value) {
    var text = textOf(value);
    if (!text) return '';
    return text.length > 12 ? text.slice(0, 8) + '\u2026' : text;
  }

  function truncate(value, limit) {
    var text = textOf(value);
    if (text.length <= limit) return text;
    return text.slice(0, limit) + '\n\u2026 truncated';
  }

  function icon(name, size, className) {
    var paths = ICONS[name] || ICONS.dot;
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'icon' + (className ? ' ' + className : ''));
    svg.setAttribute('width', String(size || 16));
    svg.setAttribute('height', String(size || 16));
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.75');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    paths.forEach(function (d) {
      var path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      svg.appendChild(path);
    });
    return svg;
  }

  function brandMark(size, className) {
    var image = document.createElement('img');
    image.setAttribute('src', ASSETS.mark);
    image.setAttribute('width', String(size || 24));
    image.setAttribute('height', String(size || 24));
    image.setAttribute('alt', '');
    if (className) image.className = className;
    return image;
  }

  function spinner() {
    var dot = make('span', 'spinner');
    dot.setAttribute('aria-hidden', 'true');
    return dot;
  }

  function stateBlock(kind, title, detail, actionLabel, action) {
    var wrap = make('div', 'state-block standalone');
    wrap.setAttribute('data-state', kind);
    if (kind === 'loading') {
      wrap.appendChild(spinner());
    } else {
      wrap.appendChild(icon(kind === 'error' ? 'alert' : 'info', 18));
    }
    var meta = make('div', 'state-meta');
    meta.appendChild(make('b', 'state-title', title));
    if (detail) meta.appendChild(make('p', 'state-detail', detail));
    if (actionLabel && typeof action === 'function') {
      var button = make('button', 'primary-btn state-action', actionLabel);
      button.type = 'button';
      button.addEventListener('click', action);
      meta.appendChild(button);
    }
    wrap.appendChild(meta);
    return wrap;
  }

  function healthStrip(ok, message) {
    var strip = make('div', 'health-strip' + (ok ? '' : ' is-bad'));
    strip.appendChild(icon(ok ? 'check' : 'alert', 13));
    var text = make('span', 'health-text');
    var strong = make('b', '', ok ? 'OK' : 'Error');
    text.appendChild(strong);
    text.appendChild(document.createTextNode(' ' + textOf(message)));
    strip.appendChild(text);
    return strip;
  }

  // ------------------------------------------------------------------
  // API layer
  // ------------------------------------------------------------------

  function encode(value) {
    return encodeURIComponent(String(value === undefined || value === null ? '' : value));
  }

  var api = {
    bootstrap: function () { return API.bootstrap; },
    sessions: function () { return API.sessions; },
    session: function (id) { return API.sessions + '/' + encode(id); },
    timeline: function (id) { return API.sessions + '/' + encode(id) + '/timeline'; },
    resume: function (id) { return API.sessions + '/' + encode(id) + '/resume'; },
    messages: function (id) { return API.sessions + '/' + encode(id) + '/messages'; },
    interrupt: function (id) { return API.sessions + '/' + encode(id) + '/interrupt'; },
    rename: function (id) { return API.sessions + '/' + encode(id) + '/rename'; },
    archive: function (id) { return API.sessions + '/' + encode(id) + '/archive'; },
    sessionPermission: function (id) { return API.sessions + '/' + encode(id) + '/permission'; },
    events: function (id) { return API.events + encode(id) + '/events'; },
    transcript: function (id) { return API.transcript + encode(id) + '/transcript'; },
    approvals: function (id, approvalId) { return API.approvals + encode(id) + '/approvals/' + encode(approvalId); },
    approvalsList: function (sessionId) { return API.approvalsList + '?session_id=' + encode(sessionId); },
    approvalsGlobal: function (approvalId) { return API.approvalsGlobal + encode(approvalId); },
    commands: function () { return API.commands; },
    settings: function () { return API.settings; },
    settingsAgent: function () { return API.settingsAgent; },
    settingsProvider: function (name) { return API.settingsProvider + encode(name); },
    settingsTest: function (name) { return API.settingsTest + encode(name) + '/test'; },
    settingsPermission: function () { return API.settingsPermission; },
    configAgent: function () { return API.configAgent; },
    configProvider: function (name) { return API.configProvider + encode(name); },
    configTest: function () { return API.configTest; },
    permission: function () { return API.permission; },
    pluginSettings: function (id) { return API.pluginSettings + encode(id) + '/settings'; }
  };

  function request(path, options) {
    var settings = options || {};
    var init = {
      method: settings.method || 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {'Accept': 'application/json'}
    };
    if (settings.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(settings.body);
    }
    return fetch(path, init).then(function (response) {
      return response.text().then(function (raw) {
        var payload = null;
        if (raw) {
          try {
            payload = JSON.parse(raw);
          } catch (parseError) {
            payload = null;
          }
        }
        if (!response.ok) {
          var detail = '';
          if (payload && payload.error) {
            detail = textOf(payload.error.message) || textOf(payload.error.code);
          }
          if (!detail) detail = truncate(raw || ('Request failed with status ' + response.status), 300);
          var failure = new Error(detail);
          failure.status = response.status;
          throw failure;
        }
        return payload;
      });
    }).catch(function (error) {
      if (error && typeof error.status === 'number') throw error;
      var wrapped = new Error('Cannot reach the local API: ' + textOf(error && error.message, 'network error'));
      wrapped.status = 0;
      throw wrapped;
    });
  }

  function apiGet(path) {
    return request(path, {method: 'GET'});
  }

  function apiPost(path, body) {
    return request(path, {method: 'POST', body: body === undefined ? {} : body});
  }

  function apiPut(path, body) {
    return request(path, {method: 'PUT', body: body === undefined ? {} : body});
  }

  function apiPatch(path, body) {
    return request(path, {method: 'PATCH', body: body === undefined ? {} : body});
  }

  function fallbackOnMissing(promise, fallback, statuses) {
    return promise.catch(function (error) {
      var allowed = statuses || [404, 405];
      if (error && allowed.indexOf(error.status) !== -1) return fallback();
      throw error;
    });
  }

  function errorText(error) {
    if (!error) return 'Unknown error';
    if (error.status === 0) return textOf(error.message, 'Network error');
    if (error.status === 401 || error.status === 403) return 'This browser is not authenticated for the local API. Reload the page with the launch token.';
    return textOf(error.message, 'Request failed');
  }

  // ------------------------------------------------------------------
  // toasts
  // ------------------------------------------------------------------

  function toast(kind, message) {
    var root = byId('toast-root');
    if (!root) return;
    var node = make('div', 'toast toast-' + (kind || 'info'));
    node.appendChild(icon(kind === 'error' ? 'alert' : kind === 'success' ? 'check' : 'info', 13));
    node.appendChild(make('span', 'toast-text', textOf(message)));
    root.appendChild(node);
    while (root.children.length > 4) root.removeChild(root.firstChild);
    window.setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, kind === 'error' ? 6500 : 3800);
  }

  // ------------------------------------------------------------------
  // normalization of API payloads
  // ------------------------------------------------------------------

  function normalizeProvider(provider) {
    var item = provider || {};
    var models = asArray(item.models).map(function (model) {
      var entry = model || {};
      return {
        id: textOf(entry.id, textOf(entry.name)),
        name: textOf(entry.name, textOf(entry.id)),
        context_window: intOrNull(entry.context_window),
        capabilities: entry.capabilities,
        cost: entry.cost
      };
    });
    return {
      name: textOf(item.name, textOf(item.id)),
      api: textOf(item.api),
      base_url: textOf(item.base_url),
      api_key_set: item.api_key_set !== undefined ? Boolean(item.api_key_set) : Boolean(item.configured),
      models: models,
      selected: Boolean(item.selected)
    };
  }

  function normalizeSession(session) {
    var item = session || {};
    return {
      session_id: textOf(item.session_id, textOf(item.id)),
      title: textOf(item.title, 'Untitled session'),
      cwd: textOf(item.cwd),
      turns: intOrNull(item.turns),
      preset: textOf(item.preset),
      created_at: item.created_at,
      updated_at: item.updated_at,
      read_only: Boolean(item.read_only),
      readable: item.readable !== false,
      active: Boolean(item.active),
      busy: Boolean(item.busy)
    };
  }

  function normalizeCommand(command) {
    var item = command || {};
    return {
      name: textOf(item.name),
      aliases: asArray(item.aliases).map(function (alias) { return textOf(alias); }),
      summary: textOf(item.summary),
      usage: textOf(item.usage),
      group: textOf(item.group, 'commands'),
      icon: textOf(item.icon, 'command'),
      enabled: item.enabled !== false,
      reason: textOf(item.reason),
      keybinding: textOf(item.keybinding),
      plugin: textOf(item.plugin)
    };
  }

  function normalizePermissionPreset(preset) {
    var item = preset || {};
    return {
      name: textOf(item.name, textOf(item.id)),
      sandbox: textOf(item.sandbox),
      approval: textOf(item.approval),
      description: textOf(item.description)
    };
  }

  function normalizePlugin(plugin) {
    var item = plugin || {};
    return {
      id: textOf(item.id, textOf(item.plugin_id)),
      name: textOf(item.name, textOf(item.id, textOf(item.plugin_id))),
      version: textOf(item.version),
      state: textOf(item.state, textOf(item.status)),
      description: textOf(item.description),
      provides: asArray(item.provides).map(function (entry) { return textOf(entry); }),
      enabled: item.enabled !== false
    };
  }

  function normalizeSection(section) {
    var item = section || {};
    var fallback = findSectionMeta(textOf(item.id));
    return {
      id: textOf(item.id),
      label: textOf(item.label, fallback ? fallback.label : textOf(item.id)),
      icon: textOf(item.icon, fallback ? fallback.icon : 'settings'),
      summary: textOf(item.summary, fallback ? fallback.summary : '')
    };
  }

  function findSectionMeta(id) {
    for (var index = 0; index < SETTINGS_SECTIONS.length; index += 1) {
      if (SETTINGS_SECTIONS[index].id === id) return SETTINGS_SECTIONS[index];
    }
    return null;
  }

  function activeSessionRecord(id) {
    for (var index = 0; index < state.sessions.length; index += 1) {
      if (state.sessions[index].session_id === id) return state.sessions[index];
    }
    return null;
  }

  function normalizeBootstrap(payload) {
    var data = payload || {};
    var config = data.config || {};
    var agent = config.agent || data.defaults || {};
    state.bootstrapped = true;
    state.version = textOf(data.version);
    state.sessions = asArray(data.sessions).map(normalizeSession);
    state.activeSessionId = textOf(data.active_session_id);
    state.commands = asArray(data.commands).map(normalizeCommand);
    state.presets = asArray(data.presets).map(function (preset) {
      var item = preset || {};
      return {id: textOf(item.id), name: textOf(item.name, textOf(item.id)), description: textOf(item.description), mode: textOf(item.mode), model: textOf(item.model)};
    });
    state.providers = asArray(config.providers || data.providers).map(normalizeProvider);
    state.config = {
      path: textOf(config.path, textOf(data.config_path)),
      agent: {
        provider: textOf(agent.provider),
        model: textOf(agent.model),
        temperature: numberOrNull(agent.temperature),
        max_tokens: intOrNull(agent.max_tokens),
        max_steps: intOrNull(agent.max_steps)
      }
    };
    var permission = data.permission || {};
    var presets = asArray(permission.presets || data.permissions).map(normalizePermissionPreset);
    state.permission = {
      default_preset: textOf(permission.default_preset, textOf(permission.current, presets.length ? presets[0].name : '')),
      approval: textOf(permission.approval, presets.length ? presets[0].approval : ''),
      presets: presets
    };
    state.plugins = asArray(data.plugins).map(normalizePlugin);
    state.web = data.web || {};
    state.about = data.about || {};
    state.home = textOf(data.home);
    state.counts = data.counts || null;
    var record = activeSessionRecord(state.activeSessionId);
    if (!record) {
      for (var index = 0; index < state.sessions.length; index += 1) {
        if (state.sessions[index].active) {
          record = state.sessions[index];
          state.activeSessionId = record.session_id;
          break;
        }
      }
    }
    if (!record && state.sessions.length) {
      record = state.sessions[0];
      state.activeSessionId = record.session_id;
    }
    state.cwd = textOf(data.cwd, record ? record.cwd : '');
  }

  function commandCount() {
    var total = 0;
    state.commands.forEach(function (command) { if (command.enabled) total += 1; });
    return total;
  }

  // ------------------------------------------------------------------
  // sidebar, workspace and inspector chrome
  // ------------------------------------------------------------------

  function renderWorkspace() {
    var name = textOf(state.about.name, 'DeepPyro');
    setText('workspace-name', name);
    setText('workspace-path', state.cwd || state.home || '\u2014');
    var pathNode = byId('workspace-path');
    if (pathNode) pathNode.setAttribute('title', state.cwd || state.home || 'No working directory reported');
    setText('foot-name', name);
    setText('foot-status', state.version ? 'local runtime \u00b7 v' + state.version : 'local runtime');
    setText('sidebar-model', state.config.agent.model || '\u2014');
    var web = state.web || {};
    var address = web.host && web.port ? textOf(web.host) + ':' + textOf(web.port) : window.location.host;
    setText('sidebar-web', address || '\u2014');
    var root = byId('dpyro-app');
    if (root && state.version) root.setAttribute('data-version', state.version);
    document.title = 'DeepPyro';
  }

  function renderSidebarSessions() {
    var list = byId('session-list');
    if (!list) return;
    clear(list);
    if (!state.sessions.length) {
      list.appendChild(stateBlock('empty', 'No sessions yet', 'Create a session to start talking to the local agent.', 'New session', createSession));
      return;
    }
    state.sessions.forEach(function (session) {
      var row = make('button', 'channel session-row');
      row.type = 'button';
      row.setAttribute('data-action', 'select-session');
      row.setAttribute('data-session-id', session.session_id);
      if (session.session_id === state.session.id || session.session_id === state.activeSessionId) {
        row.classList.add('active');
      }
      var dot = make('span', 'session-dot');
      if (session.busy) dot.classList.add('busy');
      if (session.active) dot.classList.add('live');
      row.appendChild(dot);
      var body = make('span', 'session-main');
      body.appendChild(make('span', 'session-title', session.title));
      var meta = make('span', 'session-meta');
      var parts = [];
      if (session.turns !== null) parts.push(session.turns + ' turns');
      if (session.preset) parts.push(session.preset);
      if (session.read_only) parts.push('read only');
      meta.textContent = parts.length ? parts.join(' \u00b7 ') : shortId(session.session_id);
      body.appendChild(meta);
      row.appendChild(body);
      var time = make('span', 'session-time', relativeLabel(session.updated_at));
      row.appendChild(time);
      list.appendChild(row);
    });
  }

  function renderRuntimePanel() {
    var sessions = state.counts && intOrNull(state.counts.sessions) !== null ? state.counts.sessions : state.sessions.length;
    var commands = state.counts && intOrNull(state.counts.commands) !== null ? state.counts.commands : commandCount();
    var plugins = state.counts && intOrNull(state.counts.plugins) !== null ? state.counts.plugins : state.plugins.length;
    setText('nav-count-sessions', formatCount(sessions));
    setText('nav-count-commands', formatCount(commands));
    setText('nav-count-plugins', formatCount(plugins));
    setText('inspector-count-sessions', formatCount(sessions));
    setText('inspector-count-commands', formatCount(commands));
    setText('inspector-count-plugins', formatCount(plugins));
    var web = state.web || {};
    var host = textOf(web.host, window.location.hostname) || '\u2014';
    var port = web.port === undefined || web.port === null || web.port === '' ? window.location.port : web.port;
    setText('web-host', port ? host + ':' + port : host);
    setText('web-single', web.single_session === true ? 'yes' : web.single_session === false ? 'no' : '\u2014');
    setText('sidebar-stream-state', state.connected ? 'live' : 'connecting');
  }

  function renderRunbar() {
    var session = state.session;
    setText('run-id', shortId(session.id) || '\u2014');
    setText('run-provider', state.status.provider || '\u2014');
    setText('run-model', state.status.model || '\u2014');
    setText('run-step', session.busy ? 'working' : '\u2014');
    var meter = byId('run-meter');
    if (meter) meter.style.width = formatPercent(state.status.context_tokens, state.status.context_window).toFixed(1) + '%';
    var status = byId('run-status');
    if (status) {
      status.textContent = session.busy ? '\u25cf running' : '\u25cf idle';
      status.classList.toggle('busy', Boolean(session.busy));
    }
    var live = byId('inspector-live');
    if (live) live.classList.toggle('is-hidden', !(session.busy || session.active));
  }

  function renderInspector() {
    var session = state.session;
    var status = state.status;
    setText('inspector-title', session.title || 'No session');
    setText('inspector-id', session.id || '\u2014');
    setText('metric-model', status.model || state.config.agent.model || '\u2014');
    setText('metric-provider', status.provider || state.config.agent.provider || '\u2014');
    setText('metric-preset', session.preset || '\u2014');
    setText('metric-turns', session.turns === null ? '\u2014' : session.turns);
    setText('context-tokens', status.context_tokens === null ? '\u2014' : formatTokens(status.context_tokens));
    setText('context-window', status.context_window === null ? '\u2014' : formatTokens(status.context_window));
    var track = byId('context-track');
    if (track) track.style.width = formatPercent(status.context_tokens, status.context_window).toFixed(1) + '%';
    setText('permission-mode', session.permission_preset || state.permission.default_preset || '\u2014');
    var preset = null;
    state.permission.presets.forEach(function (entry) {
      if (entry.name === (session.permission_preset || state.permission.default_preset)) preset = entry;
    });
    setText('permission-approval', state.permission.approval || (preset ? preset.approval : '') || '\u2014');
    setText('session-cwd', session.cwd || state.cwd || '\u2014');
    var usage = status.usage || {};
    setText('usage-input', formatCount(usage.input_tokens));
    setText('usage-output', formatCount(usage.output_tokens));
    setText('usage-cached', formatCount(usage.cached_input_tokens));
    var link = byId('transcript-link');
    if (link) {
      if (session.id) {
        link.setAttribute('href', api.transcript(session.id));
        link.classList.remove('is-hidden');
      } else {
        link.setAttribute('href', '#');
        link.classList.add('is-hidden');
      }
    }
    renderRunbar();
  }

  function renderComposerState() {
    var input = byId('composer-input');
    var send = byId('composer-send');
    var stop = byId('composer-stop');
    var session = state.session;
    var ready = Boolean(session.id) && session.readable && !session.read_only;
    if (input) {
      input.disabled = !ready;
      if (!session.id) input.setAttribute('placeholder', 'Select a session to send a message');
      else if (!session.readable) input.setAttribute('placeholder', 'This session is not readable');
      else if (session.read_only) input.setAttribute('placeholder', 'This session is read only');
      else input.setAttribute('placeholder', 'Message the session');
    }
    if (send) send.disabled = !ready;
    show(stop, Boolean(session.id) && session.busy);
    var count = byId('composer-queue-count');
    if (count) {
      count.textContent = state.queued > 0 ? String(state.queued) : '';
      show(count, state.queued > 0);
    }
    var hint = byId('composer-queue-hint');
    if (hint) hint.textContent = state.queued > 0 ? state.queued + ' queued' : '';
    var connection = byId('connection-state');
    if (connection) {
      connection.textContent = state.connected ? '\u25cf live' : (state.source ? '\u25cf reconnecting' : '\u25cf offline');
      connection.classList.toggle('ready', state.connected);
    }
  }

  // ------------------------------------------------------------------
  // routing
  // ------------------------------------------------------------------

  function readRoute() {
    var path = textOf(window.location.pathname, '/');
    var clean = path.split('?')[0].split('#')[0] || '/';
    if (clean === '/palette' || clean.indexOf('/palette/') === 0) return {view: 'palette'};
    if (clean === '/settings' || clean.indexOf('/settings/') === 0) {
      var segment = '';
      if (clean.indexOf('/settings/') === 0) segment = clean.slice('/settings/'.length).split('/')[0];
      return {view: 'settings', section: segment ? decodeURIComponent(segment) : 'provider'};
    }
    if (clean.indexOf('/c/') === 0) {
      var id = clean.slice(3).split('/')[0];
      return {view: 'chat', sessionId: id ? decodeURIComponent(id) : ''};
    }
    return {view: 'chat', sessionId: ''};
  }

  function sessionPath() {
    return state.session.id ? '/c/' + encode(state.session.id) : '/';
  }

  function navigate(path, replace) {
    if (replace) window.history.replaceState(null, '', path);
    else window.history.pushState(null, '', path);
    applyRoute();
  }

  function applyView(view) {
    var root = byId('dpyro-app');
    if (root) root.setAttribute('data-view', view);
    var shell = byId('dpyro-shell');
    if (shell) shell.classList.toggle('is-blurred', view !== 'chat');
  }

  function applyRoute() {
    var route = readRoute();
    if (route.view === 'palette') {
      openPalette();
      return;
    }
    if (route.view === 'settings') {
      openSettings(route.section);
      return;
    }
    closeOverlays();
    var target = route.sessionId || state.activeSessionId;
    if (target && target !== state.session.id) {
      loadSession(target, true, false);
    } else if (target && target === state.session.id) {
      renderSessionFrame();
      renderTranscript();
    } else if (!target) {
      renderNoSession();
    }
  }

  function closeOverlays() {
    state.palette.open = false;
    state.settings.open = false;
    applyView('chat');
  }

  function setConnection(connected, label) {
    state.connected = Boolean(connected);
    var sidebar = byId('sidebar-stream-state');
    if (sidebar) sidebar.textContent = label;
    var dot = byId('sidebar-live-dot');
    if (dot) dot.classList.toggle('amber', !connected);
    renderComposerState();
  }

  // ------------------------------------------------------------------
  // sessions
  // ------------------------------------------------------------------

  function renderSessionFrame() {
    var session = state.session;
    setText('session-title', session.title || (session.id ? shortId(session.id) : 'Session'));
    var topic = [];
    if (session.preset) topic.push('preset ' + session.preset);
    if (state.status.provider) topic.push(state.status.provider);
    if (state.status.model) topic.push(state.status.model);
    if (session.cwd) topic.push(session.cwd);
    setText('session-topic', topic.length ? topic.join(' \u00b7 ') : 'Live view of the selected local session.');
    document.title = session.title ? session.title + ' \u00b7 DeepPyro' : 'DeepPyro';
    renderSidebarSessions();
    renderComposerState();
    renderInspector();
  }

  function renderNoSession() {
    state.session = {id: '', title: '', cwd: '', readable: true, active: false, busy: false, pending: false, read_only: false, preset: '', permission_preset: '', turns: null, archived: false};
    state.messages = [];
    state.approvals = [];
    renderSessionFrame();
    renderRunbar();
    renderInspector();
    var list = byId('message-list');
    if (list) {
      clear(list);
      list.appendChild(stateBlock('empty', 'No session selected', 'Pick a session from the sidebar or create a new local session.', 'New session', createSession));
    }
  }

  function showTranscriptLoading() {
    var list = byId('message-list');
    if (!list) return;
    clear(list);
    list.appendChild(stateBlock('loading', 'Loading transcript', 'Reading this session from the local API.'));
  }

  function renderTranscriptFailure(error) {
    var list = byId('message-list');
    if (!list) return;
    clear(list);
    list.appendChild(stateBlock('error', 'Could not load the transcript', errorText(error), 'Retry', function () {
      if (state.session.id) loadSession(state.session.id, false, false);
    }));
  }

  function normalizeTimeline(payload) {
    var data = payload || {};
    var messages = asArray(data.messages).map(function (message) {
      var item = message || {};
      return {
        id: textOf(item.id),
        role: textOf(item.role, 'assistant'),
        time: item.created_at !== undefined ? item.created_at : item.ts,
        text: textOf(item.text),
        reasoning: textOf(item.reasoning),
        is_error: Boolean(item.is_error),
        tool_call_id: textOf(item.tool_call_id),
        tool_calls: asArray(item.tool_calls).map(function (tool) {
          var entry = tool || {};
          return {
            id: textOf(entry.id, textOf(entry.call_id)),
            call_id: textOf(entry.call_id, textOf(entry.id)),
            name: textOf(entry.name, 'tool'),
            arguments: textOf(entry.arguments),
            result: entry.result === undefined ? null : entry.result,
            is_error: Boolean(entry.is_error),
            pending: Boolean(entry.pending)
          };
        })
      };
    });
    var status = data.status || {};
    var usage = status.usage || data.usage || {};
    var sessionId = textOf(data.session_id);
    var turns = 0;
    messages.forEach(function (message) { if (message.role === 'user') turns += 1; });
    var session = {
      id: sessionId,
      title: textOf(data.title, 'Untitled session'),
      cwd: textOf(data.cwd, textOf(status.cwd)),
      readable: data.readable !== false,
      active: data.active !== false,
      busy: Boolean(data.busy),
      pending: Boolean(data.pending),
      read_only: Boolean(data.read_only),
      archived: Boolean(data.archived),
      preset: textOf(status.preset, textOf(data.preset)),
      permission_preset: textOf(status.permission_preset, textOf(data.permission_preset)),
      turns: intOrNull(data.turns) === null ? turns : intOrNull(data.turns)
    };
    return {
      session: session,
      stream_floor: intOrNull(data.journal_seq) || 0,
      messages: messages,
      approvals: asArray(data.approvals).map(normalizeApproval),
      issues: asArray(data.issues).map(function (issue) { return textOf(issue); }),
      status: {
        provider: textOf(status.provider, textOf(data.provider)),
        model: textOf(status.model, textOf(data.model)),
        context_tokens: intOrNull(status.context_tokens) === null ? intOrNull(data.context_tokens) : intOrNull(status.context_tokens),
        context_window: intOrNull(status.context_window) === null ? intOrNull(data.context_window) : intOrNull(status.context_window),
        usage: {
          input_tokens: intOrNull(usage.input_tokens),
          output_tokens: intOrNull(usage.output_tokens),
          cached_input_tokens: intOrNull(usage.cached_input_tokens)
        }
      }
    };
  }

  function normalizeApproval(approval) {
    var item = approval || {};
    return {
      id: textOf(item.id, textOf(item.approval_id)),
      tool_name: textOf(item.tool_name, textOf(item.tool, 'tool')),
      summary: textOf(item.summary),
      detail: textOf(item.detail),
      status: textOf(item.status, 'pending')
    };
  }

  function applyTimeline(id, payload) {
    var timeline = normalizeTimeline(payload);
    timeline.session.id = timeline.session.id || id;
    var sessionChanged = state.streamSessionId !== timeline.session.id;
    if (sessionChanged) {
      state.lastEventSeq = 0;
      state.streamFloor = 0;
    }
    if (timeline.stream_floor) {
      state.streamFloor = Math.max(state.streamFloor, timeline.stream_floor);
    }
    state.session = timeline.session;
    state.messages = timeline.messages;
    state.approvals = mergeApprovals(state.approvals, timeline.approvals);
    state.status = timeline.status;
    state.activeSessionId = timeline.session.id || id;
    state.queued = timeline.session.pending ? state.queued : 0;
    renderSessionFrame();
    renderTranscript();
    var route = readRoute();
    if (route.view === 'chat' && state.session.id) {
      window.history.replaceState(null, '', '/c/' + encode(state.session.id));
    }
    if (sessionChanged || !state.source) {
      connectStream(state.session.id);
    }
    refreshApprovals();
  }

  function loadSession(id, shouldResume, quiet) {
    if (!id) return Promise.resolve();
    var changed = id !== state.session.id;
    state.loadingSessionId = id;
    if (changed) {
      closeStream();
      showTranscriptLoading();
    }
    var resumed = Promise.resolve(null);
    if (shouldResume) {
      resumed = apiPost(api.resume(id), {}).catch(function (error) {
        if (!quiet) {
          if (error && (error.status === 404 || error.status === 409)) throw error;
          toast('warn', 'Resume reported: ' + errorText(error));
        }
        return null;
      });
    }
    return resumed.then(function () {
      return apiGet(api.timeline(id));
    }).then(function (payload) {
      if (state.loadingSessionId !== id) return;
      applyTimeline(id, payload);
    }).catch(function (error) {
      if (state.loadingSessionId !== id) return;
      renderTranscriptFailure(error);
      setConnection(false, 'offline');
      if (error && error.status && error.status !== 404) toast('error', 'Could not load the session: ' + errorText(error));
    });
  }

  function refreshSessions() {
    return apiGet(api.sessions()).then(function (payload) {
      var data = payload || {};
      if (Array.isArray(data.sessions)) {
        state.sessions = data.sessions.map(normalizeSession);
        if (textOf(data.active_session_id)) state.activeSessionId = textOf(data.active_session_id);
        renderSidebarSessions();
        renderRuntimePanel();
      }
      return data;
    }).catch(function (error) {
      toast('error', 'Could not refresh sessions: ' + errorText(error));
      return null;
    });
  }

  function createSession() {
    return apiPost(api.sessions(), {}).then(function (payload) {
      var data = payload || {};
      var created = textOf(data.session_id, textOf(data.id, textOf(data.session && data.session.session_id)));
      return refreshSessions().then(function () {
        if (created) {
          state.activeSessionId = created;
          navigate('/c/' + encode(created));
        } else {
          toast('warn', 'The server did not return a session id. Reload the page to refresh the list.');
        }
      });
    }).catch(function (error) {
      toast('error', 'Could not create a session: ' + errorText(error));
    });
  }

  function refreshActiveTimeline() {
    if (!state.session.id) return Promise.resolve();
    var id = state.session.id;
    return apiGet(api.timeline(id)).then(function (payload) {
      if (state.session.id !== id) return;
      applyTimeline(id, payload);
      toast('success', 'Transcript reloaded');
    }).catch(function (error) {
      toast('error', 'Could not reload the transcript: ' + errorText(error));
    });
  }

  function reloadTimelineQuiet() {
    if (!state.session.id) return Promise.resolve();
    var id = state.session.id;
    return apiGet(api.timeline(id)).then(function (payload) {
      if (state.session.id === id) applyTimeline(id, payload);
    }).catch(function () {
      /* the next SSE event or a manual reload will reconcile */
    });
  }

  function refreshApprovals() {
    if (!state.session.id) return Promise.resolve();
    var id = state.session.id;
    return apiGet(api.approvalsList(id)).then(function (payload) {
      if (state.session.id !== id) return;
      var incoming = asArray((payload || {}).approvals).map(normalizeApproval);
      var merged = mergeApprovals(state.approvals, incoming);
      var changed = merged.length !== state.approvals.length;
      for (var index = 0; !changed && index < merged.length; index += 1) {
        changed = merged[index].id !== state.approvals[index].id || merged[index].status !== state.approvals[index].status;
      }
      state.approvals = merged;
      if (changed && !state.draft) renderTranscript();
    }).catch(function () {
      /* pending approvals also arrive over SSE; absence is not an error */
    });
  }

  // ------------------------------------------------------------------
  // transcript rendering
  // ------------------------------------------------------------------

  function scrollToBottom(force) {
    var scroll = byId('chat-scroll');
    if (!scroll) return;
    var nearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140;
    if (force || nearBottom) scroll.scrollTop = scroll.scrollHeight;
  }

  function renderTranscript() {
    var list = byId('message-list');
    if (!list) return;
    clear(list);
    state.draft = null;
    var session = state.session;
    if (!session.id) {
      list.appendChild(stateBlock('empty', 'No session selected', 'Pick a session from the sidebar or create a new local session.', 'New session', createSession));
      return;
    }
    if (!session.readable) {
      list.appendChild(stateBlock('empty', 'Transcript unavailable', 'The local API marks this session as not readable.', 'Reload', refreshActiveTimeline));
      return;
    }
    if (!state.messages.length && !state.approvals.length) {
      list.appendChild(stateBlock('empty', 'No messages yet', 'Send the first message to this session from the composer below.'));
    } else {
      if (state.messages.length) {
        var divider = make('div', 'date-divider', dayLabel(state.messages[0].time) || 'Conversation');
        list.appendChild(divider);
      }
      var cards = {};
      state.messages.forEach(function (message) {
        renderMessage(list, message, cards);
      });
      state.approvals.forEach(function (approval) {
        list.appendChild(approvalCard(approval));
      });
    }
    renderComposerState();
    scrollToBottom(true);
  }

  function renderMessage(list, message, cards) {
    if (message.role === 'tool') {
      var known = message.tool_call_id ? cards[message.tool_call_id] : null;
      if (known) {
        fillToolResult(known, message.text, message.is_error);
      } else {
        var standalone = toolCard({name: 'tool', call_id: message.tool_call_id, arguments: '', result: null, is_error: false, pending: false});
        fillToolResult(standalone, message.text, message.is_error);
        list.appendChild(messageShell('tool').node);
        var toolNode = list.lastChild;
        toolNode.querySelector('.msg-body').appendChild(standalone);
      }
      return;
    }
    var shell = messageShell(message.role);
    shell.node.classList.add('msg-' + message.role);
    var timeNode = shell.head.querySelector('.time');
    if (timeNode) timeNode.textContent = timeLabel(message.time);
    if (message.reasoning) shell.body.appendChild(reasoningNode(message.reasoning));
    var text = make('div', 'msg-text');
    if (message.is_error) text.classList.add('is-error');
    renderMarkdown(text, message.text);
    if (message.text) shell.body.appendChild(text);
    message.tool_calls.forEach(function (tool) {
      var card = toolCard(tool);
      if (tool.result !== null && tool.result !== undefined) fillToolResult(card, tool.result, tool.is_error);
      cards[tool.call_id || tool.id] = card;
      shell.body.appendChild(card);
    });
    list.appendChild(shell.node);
  }

  function messageShell(role) {
    var node = make('div', 'msg');
    var avatar;
    if (role === 'assistant') {
      avatar = make('div', 'avatar agent');
      avatar.appendChild(brandMark(40));
    } else if (role === 'user') {
      avatar = make('div', 'avatar', 'Y');
      avatar.classList.add('user');
    } else {
      avatar = make('div', 'avatar', '?');
      avatar.classList.add('system');
    }
    var body = make('div', 'msg-body');
    var head = make('div', 'msg-head');
    var author = make('span', 'author', role === 'assistant' ? 'DeepPyro' : role === 'user' ? 'You' : textOf(role, 'Message'));
    author.classList.add(role === 'assistant' ? 'agent' : role === 'user' ? 'user' : 'system');
    head.appendChild(author);
    if (role === 'assistant') head.appendChild(make('span', 'app-badge', 'AGENT'));
    head.appendChild(make('span', 'time', ''));
    body.appendChild(head);
    node.appendChild(avatar);
    node.appendChild(body);
    return {node: node, body: body, head: head};
  }

  function reasoningNode(reasoning) {
    var details = document.createElement('details');
    details.className = 'reasoning';
    var summary = document.createElement('summary');
    summary.textContent = 'Reasoning';
    details.appendChild(summary);
    details.appendChild(make('div', 'reasoning-text', textOf(reasoning)));
    return details;
  }

  function codeCardNode(lang, code) {
    var card = make('div', 'code-card');
    var head = make('div', 'code-head');
    head.appendChild(make('span', 'code-lang', textOf(lang, 'code')));
    var copy = make('button', 'code-copy', 'copy');
    copy.type = 'button';
    copy.addEventListener('click', function () {
      copyCode(code, copy);
    });
    head.appendChild(copy);
    card.appendChild(head);
    var pre = document.createElement('pre');
    var codeNode = document.createElement('code');
    codeNode.textContent = textOf(code).replace(/\n$/, '');
    pre.appendChild(codeNode);
    card.appendChild(pre);
    return card;
  }

  function copyCode(code, button) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(code).then(function () {
        button.textContent = 'copied';
        window.setTimeout(function () { button.textContent = 'copy'; }, 1400);
      }).catch(function () {
        toast('error', 'The browser refused clipboard access.');
      });
    } else {
      toast('error', 'This browser does not expose the clipboard API.');
    }
  }

  function renderMarkdown(target, value) {
    clear(target);
    var source = textOf(value);
    if (!source) return;
    var position = 0;
    while (position < source.length) {
      var start = source.indexOf('\u0060\u0060\u0060', position);
      if (start < 0) {
        renderParagraphs(target, source.slice(position));
        break;
      }
      renderParagraphs(target, source.slice(position, start));
      var headerEnd = source.indexOf('\n', start + 3);
      if (headerEnd < 0) {
        renderParagraphs(target, source.slice(start));
        break;
      }
      var lang = source.slice(start + 3, headerEnd).trim();
      var end = source.indexOf('\u0060\u0060\u0060', headerEnd + 1);
      var code = end < 0 ? source.slice(headerEnd + 1) : source.slice(headerEnd + 1, end);
      target.appendChild(codeCardNode(lang, code));
      position = end < 0 ? source.length : end + 3;
    }
  }

  function renderParagraphs(target, chunk) {
    var text = textOf(chunk).replace(/^\n+/, '').replace(/\n+$/, '');
    if (!text) return;
    var blocks = text.split(/\n{2,}/);
    blocks.forEach(function (block) {
      if (!block.trim()) return;
      var paragraph = make('p', 'md-paragraph');
      var lines = block.split('\n');
      lines.forEach(function (line, index) {
        if (index > 0) paragraph.appendChild(document.createElement('br'));
        renderInline(paragraph, line);
      });
      target.appendChild(paragraph);
    });
  }

  function renderInline(target, line) {
    var pattern = /\*\*([^*]+)\*\*|\*([^*]+)\*|\u0060([^\u0060]+)\u0060/g;
    var cursor = 0;
    var match;
    while ((match = pattern.exec(line)) !== null) {
      if (match.index > cursor) target.appendChild(document.createTextNode(line.slice(cursor, match.index)));
      if (match[1] !== undefined) {
        target.appendChild(make('strong', '', match[1]));
      } else if (match[2] !== undefined) {
        target.appendChild(make('em', '', match[2]));
      } else {
        target.appendChild(make('code', '', match[3]));
      }
      cursor = pattern.lastIndex;
    }
    if (cursor < line.length) target.appendChild(document.createTextNode(line.slice(cursor)));
  }

  function prettyArguments(value) {
    var raw = textOf(value);
    if (!raw) return '';
    try {
      return truncate(JSON.stringify(JSON.parse(raw), null, 2), 6000);
    } catch (error) {
      return truncate(raw, 6000);
    }
  }

  function summaryArguments(value) {
    var raw = textOf(value);
    if (!raw) return '';
    try {
      var parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        var keys = Object.keys(parsed);
        if (!keys.length) return '';
        var key = keys[0];
        var entry = parsed[key];
        var rendered = typeof entry === 'string' ? entry : JSON.stringify(entry);
        return key + ': ' + truncate(String(rendered).replace(/\s+/g, ' '), 64);
      }
      return truncate(String(parsed), 64);
    } catch (error) {
      return truncate(raw.replace(/\s+/g, ' '), 64);
    }
  }

  function toolCard(tool) {
    var card = make('div', 'tool-card');
    var callId = textOf(tool.call_id, textOf(tool.id));
    if (callId) card.setAttribute('data-call-id', callId);
    var head = make('div', 'tool-head');
    var badge = make('div', 'tool-icon');
    badge.appendChild(icon('tool', 14));
    head.appendChild(badge);
    var titles = make('div', 'tool-titles');
    titles.appendChild(make('span', 'tool-name', textOf(tool.name, 'tool')));
    var summary = summaryArguments(tool.arguments);
    if (summary) titles.appendChild(make('span', 'tool-cmd', summary));
    head.appendChild(titles);
    head.appendChild(make('span', 'tool-status', tool.pending ? 'PENDING' : '\u2014'));
    card.appendChild(head);
    var body = make('div', 'tool-body');
    var args = prettyArguments(tool.arguments);
    if (args) {
      body.appendChild(make('div', 'tool-label', 'ARGUMENTS'));
      body.appendChild(make('pre', 'tool-args', args));
    }
    card.appendChild(body);
    return card;
  }

  function fillToolResult(card, result, isError) {
    if (!card) return;
    if (card.getAttribute('data-filled') === '1') return;
    var body = card.querySelector('.tool-body');
    if (!body) return;
    card.setAttribute('data-filled', '1');
    var status = card.querySelector('.tool-status');
    if (status) {
      status.textContent = isError ? 'ERROR' : 'DONE';
      status.classList.toggle('is-error', Boolean(isError));
    }
    card.classList.toggle('is-error', Boolean(isError));
    var rendered = textOf(result);
    if (!rendered) return;
    body.appendChild(make('div', 'tool-label', isError ? 'ERROR OUTPUT' : 'RESULT'));
    body.appendChild(make('pre', 'tool-result', truncate(rendered, 6000)));
  }

  function approvalSettled(status) {
    var value = textOf(status, 'pending').toLowerCase();
    return value === 'allow' || value === 'allowed' || value === 'approved' || value === 'resolved' || value === 'deny' || value === 'denied' || value === 'rejected' || value === 'expire' || value === 'expired' || value === 'cancel' || value === 'cancelled' || value === 'canceled';
  }

  function mergeApprovals(existing, incoming) {
    var byId = {};
    var order = [];
    function put(item) {
      if (!item || !item.id) return;
      var known = byId[item.id];
      if (known && approvalSettled(known.status) && !approvalSettled(item.status)) return;
      if (!known) order.push(item.id);
      byId[item.id] = item;
    }
    asArray(existing).forEach(function (item) {
      if (approvalSettled(item.status)) put(item);
    });
    asArray(incoming).forEach(put);
    return order.map(function (id) { return byId[id]; });
  }

  function findApprovalRecord(approvalId) {
    var found = null;
    state.approvals.forEach(function (item) {
      if (!found && item.id === approvalId) found = item;
    });
    return found;
  }

  function findApprovalCard(approvalId) {
    var list = byId('message-list');
    if (!list || !approvalId) return null;
    var cards = list.querySelectorAll('.approval[data-approval-id]');
    for (var index = 0; index < cards.length; index += 1) {
      if (cards[index].getAttribute('data-approval-id') === approvalId) return cards[index];
    }
    return null;
  }

  function approvalFromCard(card, status) {
    var title = card ? card.querySelector('.approval-title') : null;
    var sub = card ? card.querySelector('.approval-sub') : null;
    var body = card ? card.querySelector('.approval-body') : null;
    return {
      id: card ? textOf(card.getAttribute('data-approval-id')) : '',
      tool_name: title ? textOf(title.textContent, 'tool') : 'tool',
      summary: sub ? textOf(sub.textContent) : '',
      detail: body ? textOf(body.textContent) : '',
      status: textOf(status, 'pending')
    };
  }

  function approvalButtons(card) {
    if (!card) return [];
    return Array.prototype.slice.call(card.querySelectorAll('.approval-btn'));
  }

  function markApprovalCard(card, label) {
    approvalButtons(card).forEach(function (button) { button.disabled = true; });
    var badge = card ? card.querySelector('.approval-badge') : null;
    if (badge) badge.textContent = label;
  }

  function approvalCard(approval) {
    var status = textOf(approval.status, 'pending').toLowerCase();
    var allowed = status === 'allow' || status === 'allowed' || status === 'approved' || status === 'resolved';
    var denied = status === 'deny' || status === 'denied' || status === 'rejected';
    var expired = status === 'expire' || status === 'expired' || status === 'cancel' || status === 'cancelled' || status === 'canceled';
    var settled = allowed || denied || expired;
    var card = make('div', 'approval');
    card.setAttribute('data-approval-id', textOf(approval.id));
    card.setAttribute('data-status', status);
    if (allowed) card.classList.add('resolved', 'allowed', 'approved');
    if (denied) card.classList.add('denied');
    if (expired) card.classList.add('expired');
    var head = make('div', 'approval-head');
    var iconBox = make('div', 'approval-icon');
    iconBox.appendChild(icon(allowed ? 'check' : 'alert', 14));
    head.appendChild(iconBox);
    var titles = make('div', '');
    titles.appendChild(make('div', 'approval-title', textOf(approval.tool_name, 'Tool approval')));
    if (approval.summary) titles.appendChild(make('div', 'approval-sub', approval.summary));
    head.appendChild(titles);
    var badge = make('span', 'approval-badge', allowed ? 'ALLOWED' : denied ? 'DENIED' : expired ? 'EXPIRED' : 'PENDING');
    if (allowed) badge.classList.add('allowed');
    if (denied) badge.classList.add('denied');
    if (expired) badge.classList.add('expired');
    if (!settled) badge.classList.add('pending');
    head.appendChild(badge);
    card.appendChild(head);
    if (approval.detail) {
      var body = make('div', 'approval-body');
      body.appendChild(make('span', '', approval.detail));
      card.appendChild(body);
    }
    var actions = make('div', 'approval-actions');
    var allow = make('button', 'approval-btn primary', 'Allow once');
    allow.type = 'button';
    allow.setAttribute('data-action', 'approval-allow');
    allow.setAttribute('data-approval-id', approval.id);
    allow.disabled = settled;
    var deny = make('button', 'approval-btn danger', 'Deny');
    deny.type = 'button';
    deny.setAttribute('data-action', 'approval-deny');
    deny.setAttribute('data-approval-id', approval.id);
    deny.disabled = settled;
    actions.appendChild(allow);
    actions.appendChild(deny);
    card.appendChild(actions);
    card.appendChild(make('div', 'approval-result', allowed ? 'Allowed once. The tool may run in this turn.' : denied ? 'Denied. The tool will not run.' : expired ? 'This approval is no longer waiting for a decision.' : 'Waiting for a decision.'));
    return card;
  }

  function decideApproval(approvalId, decision) {
    if (!approvalId || !state.session.id) return;
    var card = findApprovalCard(approvalId);
    markApprovalCard(card, decision === 'deny' ? 'DENYING' : 'ALLOWING');
    decideApprovalRequest(approvalId, decision).then(function () {
      toast('success', decision === 'allow' ? 'Approval allowed once.' : 'Approval denied.');
      var known = findApprovalRecord(approvalId);
      var record;
      if (known) {
        record = {id: known.id, tool_name: known.tool_name, summary: known.summary, detail: known.detail, status: decision};
      } else {
        record = approvalFromCard(card, decision);
        record.status = decision;
      }
      state.approvals = mergeApprovals(state.approvals, [record]);
      var current = findApprovalCard(approvalId);
      if (current && current.parentNode) current.parentNode.replaceChild(approvalCard(record), current);
      reloadTimelineQuiet();
    }).catch(function (error) {
      toast('error', 'Could not record the approval: ' + errorText(error));
      var current = findApprovalCard(approvalId);
      if (current && current.parentNode) {
        current.parentNode.replaceChild(approvalCard(approvalFromCard(current, 'pending')), current);
      }
    });
  }

  // ------------------------------------------------------------------
  // SSE stream
  // ------------------------------------------------------------------

  function setBusy(busy) {
    state.session.busy = Boolean(busy);
    renderComposerState();
    renderRunbar();
    var live = byId('inspector-live');
    if (live) live.classList.toggle('is-hidden', !(state.session.busy || state.session.active));
    renderSidebarSessions();
  }

  function connectStream(id, force) {
    if (!force && state.source && state.streamSessionId === id && state.source.readyState !== EventSource.CLOSED) {
      return;
    }
    closeStream();
    if (!id) {
      setConnection(false, 'offline');
      return;
    }
    var source;
    var after = Math.max(state.lastEventSeq, state.streamFloor);
    var url = api.events(id) + (after > 0 ? '?after=' + encodeURIComponent(String(after)) : '');
    try {
      source = new EventSource(url);
    } catch (error) {
      setConnection(false, 'offline');
      toast('error', 'This browser could not open the event stream.');
      return;
    }
    state.source = source;
    state.streamSessionId = id;
    source.onopen = function () {
      state.reconnectDelay = 1000;
      setConnection(true, 'live');
    };
    source.onerror = function () {
      setConnection(false, 'reconnecting');
      if (source.readyState === EventSource.CLOSED) {
        closeStream();
        state.reconnectTimer = window.setTimeout(function () {
          if (state.session.id === id) connectStream(id);
        }, state.reconnectDelay);
        state.reconnectDelay = Math.min(8000, Math.round(state.reconnectDelay * 1.7));
      }
    };
    source.onmessage = function (event) {
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      handleEvent(data);
    };
  }

  function closeStream() {
    if (state.reconnectTimer) {
      window.clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
    if (state.source) {
      try {
        state.source.close();
      } catch (error) {
        /* already closed */
      }
      state.source = null;
    }
  }

  function handleEvent(data) {
    var eventSeq = intOrNull(data.seq);
    if (eventSeq !== null && eventSeq > state.lastEventSeq) state.lastEventSeq = eventSeq;
    var type = textOf(data.type);
    if (!type) return;
    if (data.session_id && state.session.id && data.session_id !== state.session.id) return;
    if (type === 'status') {
      var phase = textOf(data.phase);
      if (phase === 'thinking') {
        setBusy(true);
      } else {
        setBusy(false);
        finishDraft();
        if (phase === 'interrupted') toast('info', 'The turn was interrupted.');
      }
      return;
    }
    if (type === 'step') {
      setText('run-step', 'turn ' + textOf(data.turn, '?') + ' \u00b7 step ' + textOf(data.step, '?'));
      setBusy(true);
      return;
    }
    if (type === 'text') {
      appendStreamText(textOf(data.text));
      return;
    }
    if (type === 'thinking') {
      appendStreamThinking(textOf(data.text));
      return;
    }
    if (type === 'tool_call') {
      upsertStreamTool(textOf(data.call_id), textOf(data.name, 'tool'), textOf(data.arguments));
      return;
    }
    if (type === 'tool_result') {
      completeStreamTool(textOf(data.call_id), Boolean(data.is_error), textOf(data.text));
      return;
    }
    if (type === 'final') {
      var draft = ensureDraft();
      if (draft && textOf(data.text)) {
        draft.text = textOf(data.text);
        renderMarkdown(draft.textNode, draft.text);
      }
      return;
    }
    if (type === 'usage') {
      state.status.usage.input_tokens = intOrNull(data.input_tokens);
      state.status.usage.output_tokens = intOrNull(data.output_tokens);
      state.status.usage.cached_input_tokens = intOrNull(data.cached_input_tokens);
      renderInspector();
      return;
    }
    if (type === 'error') {
      toast('error', textOf(data.message, 'The agent reported an error.'));
      finishDraft();
      setBusy(false);
      return;
    }
    if (type === 'approval') {
      var approval = normalizeApproval(data);
      if (!approval.id) return;
      state.approvals = mergeApprovals(state.approvals, [approval]);
      var existing = findApprovalCard(approval.id);
      if (existing && existing.parentNode) {
        existing.parentNode.replaceChild(approvalCard(approval), existing);
      } else {
        var list = byId('message-list');
        if (list) {
          var placeholder = list.querySelector('.state-block.standalone');
          if (placeholder) list.removeChild(placeholder);
          list.appendChild(approvalCard(approval));
          scrollToBottom(false);
        }
      }
      return;
    }
    if (type === 'session') {
      refreshSessions();
    }
  }

  function ensureDraft() {
    if (state.draft) return state.draft;
    var list = byId('message-list');
    if (!list) return null;
    var empty = list.querySelector('.state-block.standalone');
    if (empty) list.removeChild(empty);
    var shell = messageShell('assistant');
    shell.node.classList.add('is-streaming');
    var textNode = make('div', 'msg-text');
    shell.body.appendChild(textNode);
    var tools = make('div', 'stream-tools');
    shell.body.appendChild(tools);
    list.appendChild(shell.node);
    state.draft = {node: shell.node, body: shell.body, textNode: textNode, text: '', tools: {}, reasoning: null, reasoningText: '', toolHost: tools};
    scrollToBottom(false);
    return state.draft;
  }

  function appendStreamText(chunk) {
    if (!chunk) return;
    var draft = ensureDraft();
    if (!draft) return;
    draft.text += chunk;
    renderMarkdown(draft.textNode, draft.text);
    setBusy(true);
    scrollToBottom(false);
  }

  function appendStreamThinking(chunk) {
    if (!chunk) return;
    var draft = ensureDraft();
    if (!draft) return;
    if (!draft.reasoning) {
      var details = document.createElement('details');
      details.className = 'reasoning';
      var summary = document.createElement('summary');
      summary.textContent = 'Reasoning';
      details.appendChild(summary);
      var body = make('div', 'reasoning-text');
      details.appendChild(body);
      draft.body.insertBefore(details, draft.textNode);
      draft.reasoning = body;
      draft.reasoningText = '';
    }
    draft.reasoningText += chunk;
    draft.reasoning.textContent = draft.reasoningText;
    setBusy(true);
    scrollToBottom(false);
  }

  function upsertStreamTool(callId, name, args) {
    var draft = ensureDraft();
    if (!draft) return;
    var key = callId || ('tool-' + Object.keys(draft.tools).length);
    var card = draft.tools[key];
    if (!card) {
      card = toolCard({call_id: key, name: name, arguments: args, pending: true});
      draft.tools[key] = card;
      draft.toolHost.appendChild(card);
    }
    var status = card.querySelector('.tool-status');
    if (status) status.textContent = 'RUNNING';
    setBusy(true);
    scrollToBottom(false);
  }

  function completeStreamTool(callId, isError, text) {
    var draft = state.draft;
    if (!draft) {
      scheduleTimelineRefresh();
      return;
    }
    var card = draft.tools[callId];
    if (!card) return;
    fillToolResult(card, text, isError);
    scrollToBottom(false);
  }

  function finishDraft() {
    var draft = state.draft;
    if (draft) {
      draft.node.classList.remove('is-streaming');
      var hasContent = Boolean(draft.text) || Boolean(draft.reasoningText) || Object.keys(draft.tools).length > 0;
      if (!hasContent && draft.node.parentNode) draft.node.parentNode.removeChild(draft.node);
      state.draft = null;
    }
    scheduleTimelineRefresh();
  }

  function scheduleTimelineRefresh() {
    if (state.timelineTimer) window.clearTimeout(state.timelineTimer);
    state.timelineTimer = window.setTimeout(function () {
      state.timelineTimer = null;
      var id = state.session.id;
      if (!id) return;
      apiGet(api.timeline(id)).then(function (payload) {
        if (state.session.id !== id) return;
        applyTimeline(id, payload);
      }).catch(function () {
        /* transient: the next event or manual refresh retries */
      });
    }, 450);
  }

  // ------------------------------------------------------------------
  // composer and commands
  // ------------------------------------------------------------------

  function autoGrowInput() {
    var input = byId('composer-input');
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = Math.min(130, Math.max(25, input.scrollHeight)) + 'px';
  }

  function appendUserEcho(text) {
    var list = byId('message-list');
    if (!list) return;
    var empty = list.querySelector('.state-block.standalone');
    if (empty) list.removeChild(empty);
    var shell = messageShell('user');
    var timeNode = shell.head.querySelector('.time');
    if (timeNode) timeNode.textContent = timeLabel(Date.now() / 1000);
    var body = make('div', 'msg-text');
    renderMarkdown(body, text);
    shell.body.appendChild(body);
    list.appendChild(shell.node);
    scrollToBottom(true);
  }

  function sendComposer() {
    var input = byId('composer-input');
    if (!input) return;
    var text = input.value.trim();
    if (!text) return;
    if (!state.session.id) {
      toast('error', 'Select a session before sending a message.');
      return;
    }
    if (!state.session.readable || state.session.read_only) {
      toast('error', 'This session cannot accept new messages.');
      return;
    }
    input.value = '';
    autoGrowInput();
    if (text.charAt(0) === '/' && text.charAt(1) !== '/') {
      submitCommand(text.slice(1));
      return;
    }
    appendUserEcho(text);
    setBusy(true);
    apiPost(api.messages(state.session.id), {text: text}).then(function (payload) {
      var data = payload || {};
      if (data.queued !== undefined) {
        state.queued = intOrNull(data.queued) || 0;
        renderComposerState();
      }
    }).catch(function (error) {
      toast('error', 'Could not send the message: ' + errorText(error));
      setBusy(false);
    });
  }

  function submitCommand(raw) {
    var parts = raw.trim().split(/\s+/);
    var name = parts.shift() || '';
    if (!name) return;
    executeCommand(name, parts);
  }

  function executeCommand(name, args) {
    var body = {name: name, args: args || [], session_id: state.session.id || null};
    apiPost(api.commands(), body).then(function (payload) {
      var data = payload || {};
      var status = textOf(data.status, 'ok');
      var message = textOf(data.message);
      if (status === 'error') {
        toast('error', message || 'The command failed.');
        return;
      }
      if (status === 'redirect' && textOf(data.redirect)) {
        if (message) toast('info', message);
        navigate(textOf(data.redirect));
        return;
      }
      if (message) toast('success', message);
      if (data.data && textOf(data.data.session_id)) {
        navigate('/c/' + encode(textOf(data.data.session_id)));
      }
    }).catch(function (error) {
      toast('error', 'Could not run the command: ' + errorText(error));
    });
  }

  function interruptTurn() {
    if (!state.session.id) return;
    apiPost(api.interrupt(state.session.id), {}).then(function (payload) {
      var data = payload || {};
      if (data.interrupted) toast('info', 'Interrupt requested.');
      else toast('info', textOf(data.detail, 'There is no running turn to interrupt.'));
      if (data.dropped !== undefined) {
        state.queued = intOrNull(data.dropped) || 0;
        renderComposerState();
      }
    }).catch(function (error) {
      toast('error', 'Could not interrupt the turn: ' + errorText(error));
    });
  }

  // ------------------------------------------------------------------
  // command palette
  // ------------------------------------------------------------------

  function buildPaletteItems() {
    var items = [];
    state.commands.forEach(function (command) {
      if (!command.enabled) return;
      items.push({
        kind: 'command',
        id: 'command:' + command.name,
        label: '/' + command.name,
        hint: command.summary || command.usage,
        group: 'commands',
        icon: command.icon || 'command',
        key: command.keybinding || '',
        command: command.name
      });
    });
    state.sessions.forEach(function (session) {
      items.push({
        kind: 'session',
        id: 'session:' + session.session_id,
        label: session.title,
        hint: [session.preset, session.cwd].filter(Boolean).join(' \u00b7 '),
        group: 'sessions',
        icon: 'message',
        key: relativeLabel(session.updated_at),
        sessionId: session.session_id
      });
    });
    var sections = state.settings.sections.length ? state.settings.sections : SETTINGS_SECTIONS.map(normalizeSection);
    sections.forEach(function (section) {
      items.push({
        kind: 'settings',
        id: 'settings:' + section.id,
        label: textOf(section.label, section.id),
        hint: section.summary,
        group: 'settings',
        icon: textOf(section.icon, 'settings'),
        key: '/settings/' + section.id,
        sectionId: section.id
      });
    });
    items.push({kind: 'action', id: 'action:new-session', label: 'New session', hint: 'Start a fresh local session', group: 'actions', icon: 'plus', key: 'new', action: 'new-session'});
    items.push({kind: 'action', id: 'action:refresh-sessions', label: 'Refresh sessions', hint: 'Reload the session list from the local API', group: 'actions', icon: 'refresh', key: 'refresh', action: 'refresh-sessions'});
    items.push({kind: 'action', id: 'action:open-settings', label: 'Open settings', hint: 'Provider, agent, permissions, plugins and web host', group: 'actions', icon: 'settings', key: '/settings', action: 'open-settings'});
    return items;
  }

  function paletteMatches(item, query) {
    if (!query) return true;
    var haystack = [item.label, item.hint, item.key, item.group, item.command].join(' ').toLowerCase();
    return haystack.indexOf(query.toLowerCase()) !== -1;
  }

  function openPalette() {
    state.palette.open = true;
    state.settings.open = false;
    state.palette.items = buildPaletteItems();
    state.palette.index = 0;
    applyView('palette');
    var search = byId('palette-search');
    if (search) {
      search.value = '';
      window.setTimeout(function () { search.focus(); }, 20);
    }
    renderPalette('');
  }

  function closePalette() {
    state.palette.open = false;
    applyView('chat');
    window.history.replaceState(null, '', sessionPath());
  }

  function renderPalette(query) {
    var host = byId('palette-results');
    if (!host) return;
    clear(host);
    var filtered = state.palette.items.filter(function (item) { return paletteMatches(item, query || ''); });
    state.palette.filtered = filtered;
    if (state.palette.index >= filtered.length) state.palette.index = Math.max(0, filtered.length - 1);
    if (!filtered.length) {
      host.appendChild(stateBlock('empty', 'Nothing matches', query ? 'No command, session or setting matches this filter.' : 'No palette entries are available.'));
      return;
    }
    var groups = [];
    var byGroup = {};
    filtered.forEach(function (item) {
      if (!byGroup[item.group]) {
        byGroup[item.group] = [];
        groups.push(item.group);
      }
      byGroup[item.group].push(item);
    });
    var order = ['commands', 'sessions', 'settings', 'actions'];
    groups.sort(function (left, right) {
      var leftIndex = order.indexOf(left);
      var rightIndex = order.indexOf(right);
      return (leftIndex < 0 ? 99 : leftIndex) - (rightIndex < 0 ? 99 : rightIndex);
    });
    groups.forEach(function (group) {
      host.appendChild(make('div', 'palette-group', group));
      byGroup[group].forEach(function (item) {
        var index = filtered.indexOf(item);
        var row = make('button', 'palette-row');
        row.type = 'button';
        row.setAttribute('data-action', 'palette-item');
        row.setAttribute('data-palette-index', String(index));
        if (index === state.palette.index) row.classList.add('active');
        var iconBox = make('span', 'pr-icon');
        iconBox.appendChild(icon(item.icon, 14));
        row.appendChild(iconBox);
        var main = make('span', 'pr-main');
        main.appendChild(make('span', 'pr-title', item.label));
        if (item.hint) main.appendChild(make('span', 'pr-sub', item.hint));
        row.appendChild(main);
        if (item.key) row.appendChild(make('span', 'pr-key', item.key));
        host.appendChild(row);
      });
    });
  }

  function movePalette(delta) {
    var total = state.palette.filtered.length;
    if (!total) return;
    state.palette.index = (state.palette.index + delta + total) % total;
    renderPalette(byId('palette-search') ? byId('palette-search').value : '');
    var active = document.querySelector('.palette-row.active');
    if (active && active.scrollIntoView) active.scrollIntoView({block: 'nearest'});
  }

  function activatePaletteItem(index) {
    var item = state.palette.filtered[index];
    if (!item) return;
    if (item.kind === 'command') {
      executeCommand(item.command, []);
      closePalette();
      return;
    }
    if (item.kind === 'session') {
      closePalette();
      navigate('/c/' + encode(item.sessionId));
      return;
    }
    if (item.kind === 'settings') {
      navigate('/settings/' + encode(item.sectionId));
      return;
    }
    if (item.kind === 'action') {
      if (item.action === 'new-session') {
        closePalette();
        createSession();
      } else if (item.action === 'refresh-sessions') {
        refreshSessions();
        closePalette();
      } else if (item.action === 'open-settings') {
        navigate('/settings');
      }
    }
  }

  // ------------------------------------------------------------------
  // settings
  // ------------------------------------------------------------------

  function bootstrapSettingsData() {
    return {
      sections: SETTINGS_SECTIONS.map(function (section) {
        return {id: section.id, label: section.label, icon: section.icon, summary: section.summary};
      }),
      config: {path: state.config.path, agent: state.config.agent, providers: state.providers},
      agent: state.config.agent,
      provider: {current: state.config.agent.provider, providers: state.providers},
      permission: {presets: state.permission.presets, current: state.permission.default_preset, default_preset: state.permission.default_preset},
      commands: state.commands,
      plugins: {items: state.plugins},
      sessions: {items: state.sessions, count: state.sessions.length, active_session_id: state.activeSessionId},
      web: state.web,
      about: state.about
    };
  }

  function normalizeSettings(payload) {
    var data = payload || {};
    var config = data.config || {};
    var providerBlob = data.provider || {};
    var agent = data.agent || config.agent || {};
    var providers = asArray(providerBlob.providers || config.providers || state.providers).map(normalizeProvider);
    var permission = data.permission || data.permissions || state.permission || {};
    var presets = asArray(permission.presets).map(normalizePermissionPreset);
    var commandList = asArray(data.commands).map(normalizeCommand);
    if (!commandList.length) commandList = state.commands;
    var pluginsRaw = data.plugins;
    var pluginList = asArray(pluginsRaw && pluginsRaw.items ? pluginsRaw.items : pluginsRaw).map(normalizePlugin);
    if (!pluginList.length) pluginList = state.plugins;
    var sessionsRaw = data.sessions;
    var sessionList = asArray(sessionsRaw && sessionsRaw.items ? sessionsRaw.items : sessionsRaw).map(function (session) {
      var item = session || {};
      return {
        id: textOf(item.id, textOf(item.session_id)),
        title: textOf(item.title, 'Untitled session'),
        cwd: textOf(item.cwd),
        turns: intOrNull(item.turns),
        updated_at: item.updated_at,
        preset: textOf(item.preset),
        preview: textOf(item.preview)
      };
    });
    if (!sessionList.length) {
      sessionList = state.sessions.map(function (session) {
        return {id: session.session_id, title: session.title, cwd: session.cwd, turns: session.turns, updated_at: session.updated_at, preset: session.preset, preview: ''};
      });
    }
    var sections = asArray(data.sections).map(normalizeSection);
    if (!sections.length) sections = SETTINGS_SECTIONS.map(normalizeSection);
    var web = data.web || state.web || {};
    var about = data.about || state.about || {};
    return {
      raw: data,
      sections: sections,
      configPath: textOf(config.path, textOf(about.config_path, state.config.path)),
      agent: {
        provider: textOf(agent.provider, state.config.agent.provider),
        model: textOf(agent.model, state.config.agent.model),
        temperature: numberOrNull(agent.temperature),
        max_tokens: intOrNull(agent.max_tokens),
        max_steps: intOrNull(agent.max_steps)
      },
      providers: providers,
      permission: {
        default_preset: textOf(permission.default_preset, textOf(permission.current, state.permission.default_preset)),
        current: textOf(permission.current, textOf(permission.default_preset, state.permission.default_preset)),
        approval: textOf(permission.approval, state.permission.approval),
        custom: Boolean(permission.custom),
        presets: presets
      },
      commands: commandList,
      plugins: pluginList,
      sessions: sessionList,
      web: {
        host: textOf(web.host, window.location.hostname),
        port: intOrNull(web.port),
        single_session: web.single_session
      },
      about: {
        version: textOf(about.version, state.version),
        python: textOf(about.python),
        platform: textOf(about.platform),
        plugins: intOrNull(about.plugins),
        config_path: textOf(about.config_path, state.config.path)
      }
    };
  }

  function loadSettings(force) {
    if (state.settings.loading) return;
    if (state.settings.loaded && !force) {
      renderSettings();
      return;
    }
    state.settings.loading = true;
    state.settings.error = '';
    state.settings.degraded = '';
    var body = byId('settings-body');
    if (body) {
      clear(body);
      body.appendChild(stateBlock('loading', 'Loading settings', 'Fetching configuration from the local API.'));
    }
    var footer = byId('settings-footer-actions');
    if (footer) clear(footer);
    apiGet(api.settings()).then(function (payload) {
      state.settings.data = normalizeSettings(payload);
      state.settings.loaded = true;
      state.settings.degraded = '';
    }).catch(function (error) {
      state.settings.data = normalizeSettings(bootstrapSettingsData());
      state.settings.degraded = errorText(error);
      state.settings.error = '';
    }).then(function () {
      state.settings.loading = false;
      renderSettings();
    });
  }

  function renderSettings() {
    if (!state.settings.data) return;
    renderSettingsNav();
    renderSettingsSection();
  }

  function renderSettingsNav() {
    var host = byId('settings-nav-list');
    if (!host) return;
    clear(host);
    var sections = state.settings.data.sections;
    state.settings.sections = sections;
    sections.forEach(function (section) {
      var anchor = make('a', 'settings-nav-item');
      anchor.setAttribute('href', '/settings/' + encode(section.id));
      if (section.id === state.settings.section) anchor.classList.add('active');
      anchor.appendChild(icon(textOf(section.icon, 'settings'), 14));
      anchor.appendChild(make('span', 'settings-nav-label', section.label));
      var badge = settingsBadge(section.id);
      if (badge !== null) anchor.appendChild(make('span', 'nav-badge', String(badge)));
      host.appendChild(anchor);
    });
    var foot = byId('settings-nav-foot');
    if (foot) foot.textContent = state.version ? 'local runtime \u00b7 v' + state.version : 'local runtime';
  }

  function settingsBadge(sectionId) {
    if (sectionId === 'commands') return state.settings.data.commands.length;
    if (sectionId === 'plugins') return state.settings.data.plugins.length;
    if (sectionId === 'sessions') return state.settings.data.sessions.length;
    return null;
  }

  function setSettingsHeading(section) {
    setText('settings-title', section ? section.label : 'Settings');
    setText('settings-subtitle', section ? section.summary : 'Configuration served by the local API.');
    var note = byId('settings-footer-note');
    if (note) note.textContent = (section ? section.label : 'Settings') + ' \u00b7 local API';
  }

  function renderSettingsSection() {
    var body = byId('settings-body');
    if (!body) return;
    clear(body);
    state.settings.pluginForms = {};
    var sections = state.settings.data.sections;
    var section = null;
    sections.forEach(function (entry) { if (entry.id === state.settings.section) section = entry; });
    if (!section) {
      var known = findSectionMeta(state.settings.section);
      section = known ? normalizeSection(known) : {id: state.settings.section, label: state.settings.section, icon: 'settings', summary: ''};
    }
    setSettingsHeading(section);
    if (state.settings.degraded) {
      body.appendChild(healthStrip(false, 'The settings endpoint is unavailable (' + state.settings.degraded + '). Showing the live bootstrap values; saving may fail.'));
    }
    var renderer = {
      provider: renderProviderSection,
      agent: renderAgentSection,
      permissions: renderPermissionsSection,
      commands: renderCommandsSection,
      plugins: renderPluginsSection,
      sessions: renderSessionsSection,
      web: renderWebSection,
      about: renderAboutSection
    };
    var render = renderer[section.id] || renderUnknownSection;
    render(body, section);
    renderSettingsFooter(section);
  }

  function settingsFooterActions(actions) {
    var host = byId('settings-footer-actions');
    if (!host) return;
    clear(host);
    actions.forEach(function (action) {
      var button = make('button', action.primary ? 'primary-btn' : 'ghost-btn', action.label);
      button.type = 'button';
      button.addEventListener('click', action.run);
      host.appendChild(button);
    });
  }

  function renderSettingsFooter(section) {
    settingsFooterActions([
      {label: 'Reload', run: function () { loadSettings(true); }},
      {label: 'Close', primary: true, run: closeSettings}
    ]);
    var note = byId('settings-footer-note');
    if (note) note.textContent = (section ? section.label : 'Settings') + ' \u00b7 ' + (state.settings.data.configPath || 'local API');
  }

  function formSection(title) {
    var wrapper = make('div', 'form-section');
    wrapper.appendChild(make('div', 'form-section-title', title));
    return wrapper;
  }

  function fieldNode(label, control, hint) {
    var field = make('div', 'field');
    var labelNode = make('div', 'field-label');
    labelNode.appendChild(make('span', '', label));
    field.appendChild(labelNode);
    field.appendChild(control);
    if (hint) field.appendChild(make('div', 'field-note', hint));
    return field;
  }

  function textInput(name, value, type, placeholder) {
    var input = document.createElement('input');
    input.type = type || 'text';
    if (name) input.setAttribute('name', name);
    input.value = value === undefined || value === null ? '' : String(value);
    if (placeholder) input.setAttribute('placeholder', placeholder);
    input.spellcheck = false;
    input.autocomplete = 'off';
    return input;
  }

  function shellInput(input, iconName) {
    var shell = make('div', 'input-shell');
    if (iconName) {
      var iconBox = make('span', 'input-icon');
      iconBox.appendChild(icon(iconName, 13));
      shell.appendChild(iconBox);
    }
    shell.appendChild(input);
    return shell;
  }

  function selectNode(name, options, value) {
    var select = document.createElement('select');
    select.className = 'settings-select';
    if (name) select.setAttribute('name', name);
    options.forEach(function (option) {
      var entry = document.createElement('option');
      entry.value = textOf(option.value);
      entry.textContent = textOf(option.label, option.value);
      if (entry.value === textOf(value)) entry.selected = true;
      select.appendChild(entry);
    });
    return select;
  }

  function renderProviderSection(body) {
    var data = state.settings.data;
    var agent = data.agent;
    var providers = data.providers.length ? data.providers : state.providers;
    var providerNames = providers.map(function (provider) { return {value: provider.name, label: provider.name}; });
    if (!providerNames.length) {
      body.appendChild(stateBlock('empty', 'No providers configured', 'Add a provider section to the local configuration file first.'));
      return;
    }
    var active = providerNames.filter(function (entry) { return entry.value === agent.provider; })[0] || providerNames[0];
    var activeProvider = providers.filter(function (provider) { return provider.name === active.value; })[0];
    var modelOptions = (activeProvider ? activeProvider.models : []).map(function (model) {
      return {value: model.id, label: model.name + (model.context_window ? ' \u00b7 ' + formatTokens(model.context_window) : '')};
    });
    if (!modelOptions.length) modelOptions = [{value: agent.model, label: agent.model || 'no models reported'}];

    var routing = formSection('Model routing');
    var grid = make('div', 'form-grid');
    var providerSelect = selectNode('provider', providerNames, agent.provider);
    providerSelect.addEventListener('change', function () {
      var chosen = providers.filter(function (provider) { return provider.name === providerSelect.value; })[0];
      var modelSelect = document.getElementById('settings-model-select');
      if (!modelSelect || !chosen) return;
      clear(modelSelect);
      chosen.models.forEach(function (model) {
        var option = document.createElement('option');
        option.value = model.id;
        option.textContent = model.name + (model.context_window ? ' \u00b7 ' + formatTokens(model.context_window) : '');
        modelSelect.appendChild(option);
      });
    });
    grid.appendChild(fieldNode('Provider', shellInput(providerSelect, 'cpu'), 'Providers come from the local configuration file.'));
    var modelSelect = selectNode('model', modelOptions, agent.model);
    modelSelect.id = 'settings-model-select';
    grid.appendChild(fieldNode('Model', shellInput(modelSelect, 'layers'), 'Models are reported by the selected provider.'));
    routing.appendChild(grid);
    var applyRow = make('div', 'form-actions');
    var apply = make('button', 'primary-btn', 'Apply provider and model');
    apply.type = 'button';
    apply.addEventListener('click', function () {
      var providerNode = providerSelect;
      var modelNode = document.getElementById('settings-model-select');
      saveAgentConfig({provider: providerNode.value, model: modelNode ? modelNode.value : agent.model});
    });
    applyRow.appendChild(apply);
    routing.appendChild(applyRow);
    body.appendChild(routing);

    var connections = formSection('Provider connections');
    providers.forEach(function (provider) {
      var card = make('div', 'provider-card provider-form');
      card.setAttribute('data-provider', provider.name);
      var logo = make('div', 'provider-logo', initials(provider.name));
      card.appendChild(logo);
      var meta = make('div', 'provider-meta');
      meta.appendChild(make('div', 'provider-name', provider.name));
      meta.appendChild(make('div', 'provider-sub', (provider.api || 'api') + ' \u00b7 ' + (provider.base_url || 'default base URL')));
      var keyChip = make('span', 'status-chip' + (provider.api_key_set ? ' is-on' : ''), provider.api_key_set ? 'key set' : 'no key');
      meta.appendChild(keyChip);
      card.appendChild(meta);
      var actions = make('div', 'provider-actions');
      var test = make('button', 'ghost-btn', 'Test');
      test.type = 'button';
      test.addEventListener('click', function () { testProvider(provider.name, card); });
      actions.appendChild(test);
      card.appendChild(actions);
      var fields = make('div', 'form-grid provider-fields');
      var baseUrl = textInput('base_url', provider.base_url, 'text', 'provider base URL');
      fields.appendChild(fieldNode('Base URL', shellInput(baseUrl, 'external'), 'Leave empty to use the provider default.'));
      var keyEnv = textInput('api_key_env', '', 'text', 'DEEPPYRO_PROVIDER_KEY');
      fields.appendChild(fieldNode('API key env', shellInput(keyEnv, 'key'), 'Environment variable that holds the key.'));
      var keyValue = textInput('api_key', '', 'password', 'new key value');
      fields.appendChild(fieldNode('API key', shellInput(keyValue, 'key'), 'The key is never returned by the API; leave empty to keep it.'));
      var actions2 = make('div', 'form-actions full');
      var save = make('button', 'primary-btn', 'Save provider');
      save.type = 'button';
      save.addEventListener('click', function () {
        saveProviderConfig(provider.name, card);
      });
      actions2.appendChild(save);
      fields.appendChild(actions2);
      card.appendChild(fields);
      card.appendChild(make('div', 'health-host', ''));
      connections.appendChild(card);
    });
    body.appendChild(connections);
  }

  function initials(name) {
    var text = textOf(name, '?');
    var words = text.split(/[\s_-]+/).filter(Boolean);
    if (words.length >= 2) return (words[0].charAt(0) + words[1].charAt(0)).toUpperCase();
    return text.slice(0, 2).toUpperCase();
  }

  function renderAgentSection(body) {
    var agent = state.settings.data.agent;
    var providers = state.settings.data.providers;
    var form = formSection('Agent behavior');
    var grid = make('div', 'form-grid');
    var temperature = document.createElement('input');
    temperature.type = 'range';
    temperature.min = '0';
    temperature.max = '2';
    temperature.step = '0.05';
    temperature.value = agent.temperature === null ? '0.2' : String(agent.temperature);
    temperature.setAttribute('name', 'temperature');
    var slider = make('div', 'slider-shell');
    slider.appendChild(temperature);
    var readout = make('span', 'slider-value', temperature.value);
    slider.appendChild(readout);
    temperature.addEventListener('input', function () { readout.textContent = temperature.value; });
    grid.appendChild(fieldNode('Temperature', slider, 'Sampling temperature sent to the provider.'));
    var maxTokens = textInput('max_tokens', agent.max_tokens, 'number');
    grid.appendChild(fieldNode('Max tokens', shellInput(maxTokens, 'layers'), 'Upper bound for one model response.'));
    var maxSteps = textInput('max_steps', agent.max_steps, 'number');
    grid.appendChild(fieldNode('Max steps', shellInput(maxSteps, 'refresh'), 'Tool loop budget for one turn.'));
    var providerOptions = providers.map(function (provider) { return {value: provider.name, label: provider.name}; });
    var providerSelect = selectNode('provider', providerOptions.length ? providerOptions : [{value: agent.provider, label: agent.provider}], agent.provider);
    grid.appendChild(fieldNode('Provider', shellInput(providerSelect, 'cpu')));
    var modelOptions = [];
    providers.forEach(function (provider) {
      if (provider.name === providerSelect.value) {
        provider.models.forEach(function (model) { modelOptions.push({value: model.id, label: model.name}); });
      }
    });
    if (!modelOptions.length) modelOptions = [{value: agent.model, label: agent.model || 'no models reported'}];
    var modelSelect = selectNode('model', modelOptions, agent.model);
    modelSelect.addEventListener('change', function () {});
    providerSelect.addEventListener('change', function () {
      clear(modelSelect);
      var selected = providers.filter(function (provider) { return provider.name === providerSelect.value; })[0];
      var list = selected ? selected.models : [];
      if (!list.length) list = [{id: agent.model, name: agent.model || 'no models reported'}];
      list.forEach(function (model) {
        var option = document.createElement('option');
        option.value = model.id;
        option.textContent = model.name;
        modelSelect.appendChild(option);
      });
    });
    grid.appendChild(fieldNode('Model', shellInput(modelSelect, 'layers')));
    form.appendChild(grid);
    var actions = make('div', 'form-actions');
    var save = make('button', 'primary-btn', 'Save agent behavior');
    save.type = 'button';
    save.addEventListener('click', function () {
      saveAgentConfig({
        provider: providerSelect.value,
        model: modelSelect.value,
        temperature: Number(temperature.value),
        max_tokens: Number(maxTokens.value),
        max_steps: Number(maxSteps.value)
      });
    });
    actions.appendChild(save);
    form.appendChild(actions);
    body.appendChild(form);
  }

  function renderPermissionsSection(body) {
    var permission = state.settings.data.permission;
    var presets = permission.presets;
    if (!presets.length) {
      body.appendChild(stateBlock('empty', 'No permission presets', 'The local permission service did not report any presets.'));
      return;
    }
    var form = formSection('Permission presets');
    presets.forEach(function (preset) {
      var card = make('div', 'provider-card permission-card');
      var logo = make('div', 'provider-logo', initials(preset.name));
      card.appendChild(logo);
      var meta = make('div', 'provider-meta');
      meta.appendChild(make('div', 'provider-name', preset.name));
      meta.appendChild(make('div', 'provider-sub', 'sandbox ' + (preset.sandbox || 'unknown') + ' \u00b7 approval ' + (preset.approval || 'unknown')));
      if (preset.description) meta.appendChild(make('div', 'provider-sub', preset.description));
      card.appendChild(meta);
      var current = permission.current === preset.name || permission.default_preset === preset.name;
      var actions = make('div', 'provider-actions');
      if (current) {
        actions.appendChild(make('span', 'status-chip is-on', 'active'));
      } else {
        var use = make('button', 'ghost-btn', 'Use preset');
        use.type = 'button';
        use.addEventListener('click', function () { applyPermissionPreset(preset.name); });
        actions.appendChild(use);
      }
      card.appendChild(actions);
      form.appendChild(card);
    });
    body.appendChild(form);
    if (permission.custom) body.appendChild(healthStrip(true, 'The current permission state is a custom override.'));
  }

  function renderCommandsSection(body) {
    var commands = state.settings.data.commands;
    var form = formSection('Registered commands (' + commands.length + ')');
    if (!commands.length) {
      body.appendChild(stateBlock('empty', 'No commands registered', 'No running plugin registered a slash command.'));
      return;
    }
    commands.forEach(function (command) {
      var row = make('div', 'command-row');
      var left = make('div', 'command-main');
      left.appendChild(make('span', 'command-name', '/' + command.name));
      if (command.aliases.length) left.appendChild(make('span', 'command-meta', 'aliases: ' + command.aliases.join(', ')));
      left.appendChild(make('span', 'command-summary', command.summary || command.usage || ''));
      row.appendChild(left);
      if (command.usage) row.appendChild(make('span', 'command-usage', command.usage));
      form.appendChild(row);
    });
    body.appendChild(form);
    body.appendChild(healthStrip(true, 'Commands run through the local command API. Type slash commands in the composer or use the palette.'));
  }

  function renderPluginsSection(body) {
    var plugins = state.settings.data.plugins;
    var form = formSection('Installed plugins (' + plugins.length + ')');
    if (!plugins.length) {
      body.appendChild(stateBlock('empty', 'No plugins reported', 'The plugin manager did not return any installed plugins.'));
      return;
    }
    plugins.forEach(function (plugin) {
      var card = make('div', 'provider-card plugin-card');
      var logo = make('div', 'provider-logo', initials(plugin.name));
      card.appendChild(logo);
      var meta = make('div', 'provider-meta');
      meta.appendChild(make('div', 'provider-name', plugin.name + (plugin.version ? ' ' + plugin.version : '')));
      if (plugin.description) meta.appendChild(make('div', 'provider-sub', plugin.description));
      var tags = make('div', 'plugin-tags');
      tags.appendChild(make('span', 'status-chip' + (plugin.state === 'started' || plugin.state === 'active' ? ' is-on' : ''), plugin.state || 'unknown'));
      plugin.provides.forEach(function (provide) { tags.appendChild(make('span', 'plugin-tag', provide)); });
      meta.appendChild(tags);
      card.appendChild(meta);
      var actions = make('div', 'provider-actions');
      var settingsButton = make('button', 'ghost-btn', 'Settings');
      settingsButton.type = 'button';
      settingsButton.addEventListener('click', function () { openPluginSettings(plugin, card); });
      actions.appendChild(settingsButton);
      card.appendChild(actions);
      card.appendChild(make('div', 'plugin-mount'));
      form.appendChild(card);
    });
    body.appendChild(form);
  }

  function renderSessionsSection(body) {
    var sessions = state.settings.data.sessions;
    var form = formSection('Sessions (' + sessions.length + ')');
    if (!sessions.length) {
      body.appendChild(stateBlock('empty', 'No sessions stored', 'No native session was found on this machine.'));
      return;
    }
    sessions.forEach(function (session) {
      var row = make('div', 'session-row settings-session-row');
      row.appendChild(make('span', 'session-title', session.title));
      row.appendChild(make('span', 'session-meta', [shortId(session.id), session.cwd, session.turns !== null ? session.turns + ' turns' : '', relativeLabel(session.updated_at)].filter(Boolean).join(' \u00b7 ')));
      if (session.preview) row.appendChild(make('span', 'session-preview', session.preview));
      var open = make('button', 'ghost-btn', 'Open');
      open.type = 'button';
      open.addEventListener('click', function () {
        closeSettings();
        navigate('/c/' + encode(session.id));
      });
      row.appendChild(open);
      form.appendChild(row);
    });
    body.appendChild(form);
    body.appendChild(healthStrip(true, 'Sessions are stored and executed locally by the native runtime.'));
  }

  function renderWebSection(body) {
    var web = state.settings.data.web;
    var form = formSection('Loopback web host');
    var grid = make('div', 'form-grid');
    var host = textInput('host', web.host, 'text');
    host.readOnly = true;
    grid.appendChild(fieldNode('Bind host', shellInput(host, 'monitor'), 'The local web plugin binds loopback only.'));
    var port = textInput('port', web.port, 'text');
    port.readOnly = true;
    grid.appendChild(fieldNode('Port', shellInput(port, 'plug'), 'The port is chosen when the web host starts.'));
    var single = textInput('single_session', web.single_session === true ? 'yes' : web.single_session === false ? 'no' : 'not reported', 'text');
    single.readOnly = true;
    grid.appendChild(fieldNode('Single session mode', shellInput(single, 'layers'), 'Restricts the surface to one active session when enabled.'));
    form.appendChild(grid);
    body.appendChild(form);
    body.appendChild(healthStrip(true, 'The page is served from this same origin. Remote access is not exposed by the host.'));
  }

  function renderAboutSection(body) {
    var about = state.settings.data.about;
    var form = formSection('DeepPyro runtime');
    var shortcuts = make('div', 'shortcuts');
    var rows = [
      ['Version', about.version || state.version || '\u2014'],
      ['Python', about.python || '\u2014'],
      ['Platform', about.platform || '\u2014'],
      ['Plugin count', about.plugins === null ? '\u2014' : String(about.plugins)],
      ['Config path', about.config_path || state.config.path || '\u2014'],
      ['Web host', (state.settings.data.web.host || '\u2014') + ':' + (state.settings.data.web.port || '\u2014')],
      ['Sessions', String(state.settings.data.sessions.length)],
      ['Commands', String(state.settings.data.commands.length)]
    ];
    rows.forEach(function (pair) {
      var row = make('div', 'shortcut');
      row.appendChild(make('span', '', pair[0]));
      row.appendChild(make('span', '', pair[1]));
      shortcuts.appendChild(row);
    });
    form.appendChild(shortcuts);
    body.appendChild(form);
    body.appendChild(healthStrip(true, 'DeepPyro is a local-first agent runtime. All data on this page comes from the same-origin API.'));
  }

  function renderUnknownSection(body, section) {
    var form = formSection('Unknown settings section');
    form.appendChild(make('p', 'field-note', 'The local API does not expose a renderer for section ' + section.id + '.'));
    body.appendChild(form);
  }

  function closeSettings() {
    state.settings.open = false;
    applyView('chat');
    window.history.replaceState(null, '', sessionPath());
  }

  function openSettings(sectionId) {
    state.settings.open = true;
    state.palette.open = false;
    var chosen = textOf(sectionId, 'provider');
    var known = false;
    state.settings.sections.forEach(function (entry) { if (entry.id === chosen) known = true; });
    if (!known) {
      SETTINGS_SECTIONS.forEach(function (entry) { if (entry.id === chosen) known = true; });
    }
    state.settings.section = known ? chosen : 'provider';
    applyView('settings');
    if (!state.settings.data) loadSettings(false);
    else renderSettings();
  }

  // -- settings saves -------------------------------------------------

  function saveAgentRequest(body) {
    return fallbackOnMissing(apiPatch(api.settingsAgent(), body), function () {
      return apiPut(api.configAgent(), body);
    });
  }

  function saveProviderRequest(name, body) {
    return fallbackOnMissing(apiPatch(api.settingsProvider(name), body), function () {
      return apiPut(api.configProvider(name), body);
    });
  }

  function testProviderRequest(name) {
    return fallbackOnMissing(apiPost(api.settingsTest(name), {}), function () {
      return apiPost(api.configTest(), {provider: name});
    });
  }

  function applyPermissionRequest(name) {
    return fallbackOnMissing(apiPost(api.settingsPermission(), {preset: name}), function () {
      return apiPost(api.permission(), {preset: name});
    });
  }

  function decideApprovalRequest(approvalId, decision) {
    return fallbackOnMissing(apiPost(api.approvalsGlobal(approvalId), {decision: decision}), function () {
      return apiPost(api.approvals(state.session.id, approvalId), {decision: decision});
    });
  }

  function saveAgentConfig(overrides) {
    var agent = state.settings.data ? state.settings.data.agent : state.config.agent;
    var body = {
      provider: textOf(overrides && overrides.provider, agent.provider),
      model: textOf(overrides && overrides.model, agent.model)
    };
    var temperature = numberOrNull(overrides && overrides.temperature);
    var maxTokens = intOrNull(overrides && overrides.max_tokens);
    var maxSteps = intOrNull(overrides && overrides.max_steps);
    if (temperature !== null) body.temperature = temperature;
    if (maxTokens !== null) body.max_tokens = maxTokens;
    if (maxSteps !== null) body.max_steps = maxSteps;
    saveAgentRequest(body).then(function (payload) {
      var data = payload || {};
      var saved = data.config && data.config.agent ? data.config.agent : body;
      state.config.agent.provider = textOf(saved.provider, body.provider);
      state.config.agent.model = textOf(saved.model, body.model);
      state.config.agent.temperature = numberOrNull(saved.temperature);
      state.config.agent.max_tokens = intOrNull(saved.max_tokens);
      state.config.agent.max_steps = intOrNull(saved.max_steps);
      if (state.settings.data) {
        state.settings.data.agent = {
          provider: state.config.agent.provider,
          model: state.config.agent.model,
          temperature: state.config.agent.temperature,
          max_tokens: state.config.agent.max_tokens,
          max_steps: state.config.agent.max_steps
        };
      }
      toast('success', 'Agent configuration saved.');
      renderWorkspace();
      renderSettings();
    }).catch(function (error) {
      toast('error', 'Could not save agent configuration: ' + errorText(error));
    });
  }

  function saveProviderConfig(name, card) {
    var body = {};
    var baseUrl = card.querySelector('input[name="base_url"]');
    var keyEnv = card.querySelector('input[name="api_key_env"]');
    var keyValue = card.querySelector('input[name="api_key"]');
    if (baseUrl && baseUrl.value.trim()) body.base_url = baseUrl.value.trim();
    if (keyEnv && keyEnv.value.trim()) body.api_key_env = keyEnv.value.trim();
    if (keyValue && keyValue.value.trim()) body.api_key = keyValue.value.trim();
    saveProviderRequest(name, body).then(function (payload) {
      var data = payload || {};
      var providers = asArray(data.config && data.config.providers).map(normalizeProvider);
      if (providers.length) {
        state.providers = providers;
        if (state.settings.data) state.settings.data.providers = providers;
      }
      var record = activeSessionRecord(state.activeSessionId);
      if (record) record.cwd = record.cwd;
      toast('success', 'Provider ' + name + ' saved.');
      if (keyValue) keyValue.value = '';
      renderSettings();
    }).catch(function (error) {
      toast('error', 'Could not save provider ' + name + ': ' + errorText(error));
    });
  }

  function testProvider(name, card) {
    var host = card ? card.querySelector('.health-host') : null;
    if (host) {
      clear(host);
      host.appendChild(healthStrip(true, 'Testing ' + name + ' \u2026'));
    }
    testProviderRequest(name).then(function (payload) {
      var data = payload || {};
      var ok = Boolean(data.ok);
      var detail = textOf(data.detail, ok ? 'Connection succeeded.' : 'Connection failed.');
      if (data.latency_ms !== undefined && data.latency_ms !== null) detail += ' (' + textOf(data.latency_ms) + ' ms)';
      if (host) {
        clear(host);
        host.appendChild(healthStrip(ok, detail));
      }
      toast(ok ? 'success' : 'error', detail);
    }).catch(function (error) {
      if (host) {
        clear(host);
        host.appendChild(healthStrip(false, errorText(error)));
      }
      toast('error', 'Could not test provider ' + name + ': ' + errorText(error));
    });
  }

  function applyPermissionPreset(name) {
    applyPermissionRequest(name).then(function (payload) {
      var data = payload || {};
      var permission = data.permission || {};
      if (state.settings.data) {
        state.settings.data.permission.current = textOf(permission.current, textOf(permission.default_preset, name));
        state.settings.data.permission.default_preset = textOf(permission.default_preset, textOf(permission.current, name));
        if (Array.isArray(permission.presets)) state.settings.data.permission.presets = permission.presets.map(normalizePermissionPreset);
      }
      state.permission.default_preset = textOf(permission.default_preset, name);
      if (permission.approval) state.permission.approval = textOf(permission.approval);
      toast('success', 'Permission preset set to ' + name + '.');
      renderSettings();
    }).catch(function (error) {
      toast('error', 'Could not apply the permission preset: ' + errorText(error));
    });
  }

  // -- plugin settings -------------------------------------------------

  function openPluginSettings(plugin, card) {
    var mount = card.querySelector('.plugin-mount');
    if (!mount) return;
    if (mount.getAttribute('data-loaded') === 'true') {
      clear(mount);
      mount.removeAttribute('data-loaded');
      return;
    }
    clear(mount);
    mount.appendChild(stateBlock('loading', 'Loading plugin settings', 'Reading the schema from the local API.'));
    apiGet(api.pluginSettings(plugin.id)).then(function (payload) {
      var data = payload || {};
      var schema = data.schema || {};
      var values = data.values || {};
      clear(mount);
      mount.setAttribute('data-loaded', 'true');
      mount.appendChild(healthStrip(true, 'Plugin settings for ' + plugin.id + (data.version ? ' v' + data.version : '') + '.'));
      var formHost = make('div', 'form-section plugin-form');
      var grid = make('div', 'form-grid');
      var fields = schemaFields(schema, values);
      if (!fields.length) {
        formHost.appendChild(make('p', 'field-note', 'This plugin exposes no settings schema.'));
      } else {
        fields.forEach(function (field) { grid.appendChild(field); });
        formHost.appendChild(grid);
      }
      mount.appendChild(formHost);
      if (fields.length) {
        var actions = make('div', 'form-actions');
        var save = make('button', 'primary-btn', 'Save plugin settings');
        save.type = 'button';
        save.addEventListener('click', function () { savePluginSettings(plugin.id, formHost, mount); });
        actions.appendChild(save);
        mount.appendChild(actions);
      }
    }).catch(function (error) {
      clear(mount);
      mount.appendChild(healthStrip(false, 'Could not load plugin settings: ' + errorText(error)));
      toast('error', 'Could not load plugin settings: ' + errorText(error));
    });
  }

  function schemaFields(schema, values) {
    var fields = [];
    var properties = schema && schema.properties ? schema.properties : {};
    Object.keys(properties).forEach(function (key) {
      var spec = properties[key] || {};
      var label = textOf(spec.title, key);
      var value = values ? values[key] : undefined;
      var control;
      if (Array.isArray(spec.enum)) {
        var options = spec.enum.map(function (entry) { return {value: entry, label: entry}; });
        control = shellInput(selectNode(key, options, value));
      } else if (spec.type === 'boolean') {
        var checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.setAttribute('name', key);
        checkbox.setAttribute('data-schema-key', key);
        checkbox.setAttribute('data-schema-type', 'boolean');
        checkbox.checked = Boolean(value);
        var wrapper = make('div', 'checkbox-shell');
        wrapper.appendChild(checkbox);
        wrapper.appendChild(make('span', '', checkbox.checked ? 'enabled' : 'disabled'));
        checkbox.addEventListener('change', function () {
          wrapper.lastChild.textContent = checkbox.checked ? 'enabled' : 'disabled';
        });
        control = wrapper;
      } else if (spec.type === 'number' || spec.type === 'integer') {
        var number = textInput(key, value, 'number');
        number.setAttribute('data-schema-key', key);
        number.setAttribute('data-schema-type', spec.type);
        control = shellInput(number);
      } else if (spec.format === 'password' || spec.writeOnly) {
        var secret = textInput(key, '', 'password');
        secret.setAttribute('data-schema-key', key);
        secret.setAttribute('data-schema-type', 'string');
        control = shellInput(secret, 'key');
      } else if (spec.type === 'object' || spec.type === 'array' || spec.format === 'textarea') {
        var area = document.createElement('textarea');
        area.className = 'settings-textarea';
        area.setAttribute('name', key);
        area.setAttribute('data-schema-key', key);
        area.setAttribute('data-schema-type', spec.type === 'object' || spec.type === 'array' ? 'json' : 'string');
        area.rows = 4;
        area.value = value === undefined || value === null ? '' : typeof value === 'string' ? value : JSON.stringify(value, null, 2);
        control = area;
      } else {
        var text = textInput(key, value, 'text');
        text.setAttribute('data-schema-key', key);
        text.setAttribute('data-schema-type', 'string');
        control = shellInput(text);
      }
      fields.push(fieldNode(label, control, textOf(spec.description)));
    });
    return fields;
  }

  function collectSchemaValues(container) {
    var values = {};
    var nodes = container.querySelectorAll('[data-schema-key]');
    for (var index = 0; index < nodes.length; index += 1) {
      var node = nodes[index];
      var key = node.getAttribute('data-schema-key');
      var type = node.getAttribute('data-schema-type');
      if (!key) continue;
      if (type === 'boolean') values[key] = Boolean(node.checked);
      else if (type === 'integer') values[key] = parseInt(node.value, 10);
      else if (type === 'number') values[key] = parseFloat(node.value);
      else if (type === 'json') {
        var raw = node.value.trim();
        if (!raw) values[key] = null;
        else {
          try {
            values[key] = JSON.parse(raw);
          } catch (error) {
            throw new Error('Field ' + key + ' is not valid JSON.');
          }
        }
      } else values[key] = node.value;
    }
    return values;
  }

  function savePluginSettings(pluginId, formHost, mount) {
    var values;
    try {
      values = collectSchemaValues(formHost);
    } catch (error) {
      toast('error', error.message);
      return;
    }
    apiPut(api.pluginSettings(pluginId), {values: values}).then(function (payload) {
      var data = payload || {};
      var saved = data.values || values;
      var nodes = formHost.querySelectorAll('[data-schema-key]');
      for (var index = 0; index < nodes.length; index += 1) {
        var node = nodes[index];
        var key = node.getAttribute('data-schema-key');
        if (saved[key] === undefined) continue;
        var value = saved[key];
        if (node.getAttribute('data-schema-type') === 'json') node.value = value === null ? '' : JSON.stringify(value, null, 2);
        else if (node.type === 'checkbox') node.checked = Boolean(value);
        else node.value = value === null ? '' : String(value);
      }
      toast('success', 'Plugin settings saved.');
      if (mount && mount.firstChild && mount.firstChild.classList.contains('health-strip')) {
        clear(mount.firstChild);
      }
    }).catch(function (error) {
      toast('error', 'Could not save plugin settings: ' + errorText(error));
    });
  }

  // ------------------------------------------------------------------
  // event wiring
  // ------------------------------------------------------------------

  function bindStaticEvents() {
    bindClick('rail-new-session', createSession);
    bindClick('new-session', createSession);
    bindClick('session-list-refresh', function () {
      refreshSessions().then(function () { toast('success', 'Session list refreshed.'); });
    });
    bindClick('composer-send', sendComposer);
    bindClick('composer-stop', interruptTurn);
    bindClick('refresh-session', refreshActiveTimeline);
    bindClick('open-palette', function () { navigate('/palette'); });
    bindClick('composer-commands', function () { navigate('/palette'); });
    bindClick('palette-close', closePalette);
    bindClick('settings-close', closeSettings);
    bindClick('inspector-refresh', function () {
      renderInspector();
      toast('info', 'Inspector refreshed.');
    });

    var input = byId('composer-input');
    if (input) {
      input.addEventListener('input', autoGrowInput);
      input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          sendComposer();
        }
      });
    }

    var search = byId('palette-search');
    if (search) {
      search.addEventListener('input', function () {
        state.palette.index = 0;
        renderPalette(search.value);
      });
      search.addEventListener('keydown', function (event) {
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          movePalette(1);
        } else if (event.key === 'ArrowUp') {
          event.preventDefault();
          movePalette(-1);
        } else if (event.key === 'Enter') {
          event.preventDefault();
          activatePaletteItem(state.palette.index);
        }
      });
    }

    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('click', onDocumentClick);
    window.addEventListener('popstate', function () {
      applyRoute();
    });
  }

  function bindClick(id, handler) {
    var node = byId(id);
    if (node) node.addEventListener('click', handler);
  }

  function onKeyDown(event) {
    var target = event.target;
    var inField = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable);
    var meta = event.metaKey || event.ctrlKey;
    if (meta && (event.key === 'k' || event.key === 'K')) {
      event.preventDefault();
      navigate('/palette');
      return;
    }
    if (meta && event.key === ',') {
      event.preventDefault();
      navigate('/settings');
      return;
    }
    if (meta && (event.key === 'n' || event.key === 'N')) {
      event.preventDefault();
      createSession();
      return;
    }
    if (event.key === 'Escape') {
      if (state.palette.open) {
        closePalette();
        return;
      }
      if (state.settings.open) {
        closeSettings();
      }
    }
    if (!inField && event.key === '?' && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      navigate('/palette');
    }
  }

  function onDocumentClick(event) {
    var target = event.target;
    if (!target) return;
    var anchor = target.closest ? target.closest('a') : null;
    if (anchor) {
      var href = anchor.getAttribute('href') || '';
      if (href.charAt(0) === '/' && href.charAt(1) !== '/') {
        event.preventDefault();
        navigate(href);
        return;
      }
    }
    var actionNode = target.closest ? target.closest('[data-action]') : null;
    if (!actionNode) return;
    var action = actionNode.getAttribute('data-action');
    if (action === 'select-session') {
      var sessionId = actionNode.getAttribute('data-session-id');
      if (sessionId) navigate('/c/' + encode(sessionId));
      return;
    }
    if (action === 'palette-item') {
      var index = parseInt(actionNode.getAttribute('data-palette-index'), 10);
      activatePaletteItem(isNaN(index) ? 0 : index);
      return;
    }
    if (action === 'approval-allow') {
      decideApproval(actionNode.getAttribute('data-approval-id'), 'allow');
      return;
    }
    if (action === 'approval-deny') {
      decideApproval(actionNode.getAttribute('data-approval-id'), 'deny');
      return;
    }
    if (action === 'reload-settings') {
      loadSettings(true);
    }
  }

  // ------------------------------------------------------------------
  // boot
  // ------------------------------------------------------------------

  function renderBootFailure(error) {
    var message = errorText(error);
    var list = byId('message-list');
    if (list) {
      clear(list);
      list.appendChild(stateBlock('error', 'Cannot reach the local API', message, 'Retry', boot));
    }
    var sessions = byId('session-list');
    if (sessions) {
      clear(sessions);
      sessions.appendChild(stateBlock('error', 'No session data', message, 'Retry', boot));
    }
    var palette = byId('palette-results');
    if (palette) {
      clear(palette);
      palette.appendChild(stateBlock('error', 'Cannot load the palette', message));
    }
    var settings = byId('settings-body');
    if (settings) {
      clear(settings);
      settings.appendChild(stateBlock('error', 'Cannot load settings', message));
    }
    setConnection(false, 'offline');
    setText('workspace-path', '\u2014');
    setText('foot-status', 'local API unreachable');
    toast('error', message);
  }

  function boot() {
    setConnection(false, 'connecting');
    return apiGet(api.bootstrap()).then(function (payload) {
      normalizeBootstrap(payload);
      renderWorkspace();
      renderSidebarSessions();
      renderRuntimePanel();
      renderComposerState();
      renderInspector();
      return applyRoute();
    }).catch(function (error) {
      renderBootFailure(error);
    });
  }

  function start() {
    var root = byId('dpyro-app');
    if (!root) return;
    bindStaticEvents();
    renderComposerState();
    boot();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
}());
