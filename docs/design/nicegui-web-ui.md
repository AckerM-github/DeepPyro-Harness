# NiceGUI Web UI (0.3 line, post-tag)

> **已废弃（2026-11-20）**：本文件描述旧的 NiceGUI 实现，现已被 React/TS SPA 取代。
> 当前实现与未完成事项见 `docs/design/web-ui-followups.md`。

Status: implemented (0.3 line, post-tag). This document is the contract for
the production switch away from the P1 hand-written single-page shell. The host
runtime, JSON API and native session model do not change; the old `shell.py`,
`app.js` and `app.css` are deleted.

## Why a Python UI

The P1 shell is excellent at density but every new control costs hand-written
DOM, state and CSS. NiceGUI gives the project Python-native pages, Quasar form
controls, dialogs, drawers, tabs, keyboard handling and a WebSocket transport.
The look remains ours: a small stylesheet defines the three-column shell, the
Discord-inspired dark scale and the compact message/tool/approval treatments.

NiceGUI is mounted into the existing FastAPI application rather than starting a
second server. It is a rendering layer over the same `WebSessionHost`, not a
second agent runtime.

## Process shape

```
browser (cookie)
  -> FastAPI (loopback auth, JSON API, /brand assets)
  -> NiceGUI pages (ui.page, one per route)
  -> WebUiBridge (plain dicts)
  -> WebSessionHost (native sessions, SSE journal, approvals)
  -> NativeAgentHost / plugin kernel
```

One process may create one NiceGUI UI server. The NiceGUI core app is global;
tests that need a second server must use a fresh process or a single fixture.

## Modules

| module | owner | responsibility |
| --- | --- | --- |
| `web/ui/bridge.py` | integration | The only API pages may call. Wraps session/timeline/send/approval/commands/settings. |
| `web/ui/theme.py` | integration | Icon names, initials avatars, relative time, shared helpers. |
| `web/ui/layout.py` | integration | Three-column shell, session sidebar with brand, chat stage, inspector, narrow top bar, new-session dialog. |
| `web/ui/chat.py` | chat | Stage header, timeline, streaming draft, tool cards, approval cards, composer. |
| `web/ui/settings_view.py` | settings | Full-screen settings surface for provider/agent/permissions/commands/plugins/sessions/web/about. |
| `web/ui/palette.py` | settings | Command palette dialog, filters and navigation. |
| `web/ui/pages.py` | integration | `ui.page` registrations and composition. |
| `web/ui/server.py` | integration | Mount NiceGUI, static assets, lifespan, remove the old HTML shell. |
| `assets/style.css` | integration | Tokens, reset, three-column shell, sidebar/stage/inspector, narrow top bar. |
| `assets/chat.css` | chat | Chat stage, messages, tool cards, approvals, composer. |
| `assets/settings.css` | settings | Settings modal/page, forms, palette. |

Every stylesheet is local and linked from `/brand/<name>.css`. No CDN.

## Routes

| route | page |
| --- | --- |
| `/` | shell + active session (or empty state) |
| `/c/{session_id}` | shell + that session; read-only if it is not attached |
| `/palette` | shell + palette open |
| `/settings` | full-screen settings |
| `/settings/{section}` | full-screen settings at one section |

The old `/ui/app.css`, `/ui/app.js` and `web/ui/shell.py` are deleted.

## Bridge contract

Pages receive a `WebUiBridge` and call:

```python
await bridge.bootstrap() -> dict          # version, sessions, commands, presets, ...
await bridge.sessions(include_archived=False) -> dict
await bridge.timeline(session_id) -> dict # never raises HttpError; errors are data
await bridge.facts(session_id) -> dict
await bridge.create(cwd=..., title=..., preset=..., provider=..., model=...) -> dict
await bridge.resume(session_id) -> dict
await bridge.rename(session_id, title) -> dict
await bridge.archive(session_id, archived=True) -> dict
await bridge.permission(session_id, preset) -> dict
await bridge.send(session_id, text) -> dict
await bridge.interrupt(session_id) -> dict
await bridge.approvals(session_id) -> list[dict]
bridge.decide_approval(approval_id, decision) -> bool
async for frame in bridge.sse(session_id, after=0): ...  # SSE bytes
await bridge.execute(name, args, session_id) -> CommandResult
await bridge.settings() -> dict
await bridge.update_agent(**changes) -> Config
await bridge.update_provider(provider, **changes) -> Config
await bridge.test_provider(provider) -> dict
await bridge.set_permission(preset) -> Config
bridge.plugin_settings(plugin_id) -> dict
bridge.update_plugin_settings(plugin_id, values, expected_revision="") -> dict
```

`CommandResult` has `status` (`ok`/`error`/`redirect`), `message`, `redirect`
and `data`. A command that creates or switches a session puts the id in
`data["session_id"]`; pages navigate to `/c/<id>`.

## Chat behaviour

- On mount, load the timeline and paint all persisted messages.
- Subscribe to `bridge.sse(session_id)` in a page task. Append text/thinking
  deltas to a draft assistant bubble; final replaces the draft; tool call and
  tool result events schedule a timeline refresh.
- Poll the timeline every 0.8 s as a safety net and to update status/usage.
- `POST` text through `bridge.send`; slash text goes through
  `bridge.execute` instead.
- Approval cards come from `bridge.approvals(session_id)`; the buttons call
  `bridge.decide_approval` and refresh.
- A non-active session is read-only and offers Resume.
- On disconnect, cancel the SSE task and timers.

## Visual contract

CSS class names shared across modules:

- Shell: `.dp-app`, `.dp-sidebar`, `.dp-sidebar-lockup`, `.dp-logo`, `.dp-brand`,
  `.dp-nav`, `.dp-session-list`, `.dp-stage`, `.dp-inspector`, `.dp-mobile-bar`,
  `.dp-mobile-btn`.
- Chat: `.dp-chat`, `.dp-stage-head`, `.dp-messages`, `.dp-message`,
  `.dp-message-body`, `.dp-tool`, `.dp-approval`, `.dp-composer`.
- Settings: `.dp-settings`, `.dp-settings-nav`, `.dp-settings-body`,
  `.dp-field`, `.dp-field-label`, `.dp-save-bar`.
- Palette: `.dp-palette`, `.dp-palette-input`, `.dp-palette-group`,
  `.dp-palette-row`.

Use CSS variables from `style.css` (`--dp-bg`, `--dp-panel`, `--dp-panel-2`,
`--dp-line`, `--dp-text`, `--dp-muted`, `--dp-accent`, `--dp-ok`,
`--dp-warn`, `--dp-danger`). Body text is 14–15px; labels 11–12px; monospace
is Cascadia Code/Consolas. Never introduce a second accent hue.

## Security

- NiceGUI needs inline style and script execution; the CSP therefore grants
  `'unsafe-inline'` for script/style, and WebSocket connections. Everything is
  still same-origin, the server still binds 127.0.0.1, and the token/cookie
  middleware still covers HTTP and WebSocket scopes. No CDN or remote font.
- Pages never receive the token; the browser only holds the HttpOnly cookie.
- All dynamic text uses NiceGUI labels/markdown sanitisation. Raw HTML is only
  for static icons and the static shell.

## Cutover

1. Add `nicegui>=4.0,<5`.
2. Mount NiceGUI, switch CSP/lifespan, mount `/brand`.
3. Land chat/settings/palette and the shell.
4. Delete `shell.py`, `assets/app.css`, `assets/app.js`, `test_ui_shell.py`.
5. Rewrite the web tests as bridge/page tests plus one real-server smoke.
6. Keep `/api/ui/*` for tests, scripts and non-browser clients.
