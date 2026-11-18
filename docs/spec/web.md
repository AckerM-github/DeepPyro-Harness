# web: the loopback web host for the native runtime

> **状态**：现行（phase 1，0.3.0 原生运行时） · **层级**：Web 宿主规格 · **最后核对**：2026-11-18
> 实现：`src/deeppyro/web/`；使用指南：[guides/web-ui.md](../guides/web-ui.md)。
> 关联契约：[tui-native-runtime.md](../design/tui-native-runtime.md)（native 事件/会话事实）、
> [sessions.md](session.md)、[sandbox.md](sandbox.md)、[plugin-platform.md](plugin-platform.md)。

## 1. 定位与范围（Scope）

Web UI 是 native agent runtime 的**宿主/外壳（host and shell）**，不是第二个 agent 实现。
浏览器页面只做三件事：把命令 POST 给宿主、用 EventSource 接收事件、用 transcript 回放事实。
会话状态全部保存在 append-only 的 native session log（JSONL schema 1）中；Web 进程只持有一个
§active binding§（当前挂接的会话）、一个有界发送队列和一个内存事件日志（journal）。进程重启后，
浏览器凭 session id 重新回放，不依赖任何隐藏状态。

Phase 1 范围：独立的 Web 宿主进程（stdlib-only）、单页前端、CLI、插件缝、测试与本文档。
**不包含**：TUI splash 的 F1/F2 接线（由 lead 在后续集成步骤完成，本任务禁止修改
`src/deeppyro/tui/`）、core bundle 行（lead 接线）、工具审批 UI、远程访问/TLS、团队预设。

硬性约束：

- 只监听回环地址：只允许 `127.0.0.1`（`localhost` 归一化），没有外部绑定选项。
- 只依赖标准库（服务器、前端、CLI）；`httpx` 仅用于测试，且已是既有 dev 依赖。
- 不 import `deeppyro.tui`、不 import/使用 v4 会话 API；测试用 AST 守卫固定这两条。
- 不新增运行时依赖，前端无构建步骤、无 CDN、无外部字体；资产是 Python 字符串常量，
  因此打包成单文件可执行体时无需 pyproject/静态文件安装步骤。

## 2. 架构（Architecture）

    kernel boot (plugin composition)
      |
      +-- WebHostPlugin (Service, provide="web")      src/deeppyro/web/plugin.py
      |     owns RouteRegistry + default WebSurfaces; setup() NEVER listens
      |     start() -> WebHost.start(...)   stop() -> WebHost.stop()
      |
      +-- RouteRegistry (WebSurface Protocol)         src/deeppyro/web/surface.py
      |     AssetSurface:  /, /assets/app.css, /assets/app.js
      |     ApiSurface:    /api/... typed JSON + SSE
      |     <- other plugins may register_surface()/register_routes()
      |
      +-- WebServer (asyncio stdlib HTTP/1.1)         src/deeppyro/web/server.py
      |     127.0.0.1 only, keep-alive, bearer token, Host/Origin checks,
      |     body limits, security headers, chunked SSE, graceful shutdown
      |
      +-- WebSessionHost (WebService)                 src/deeppyro/web/service.py
            WebSessionHost wraps:
              SessionsPlugin.native_*        (facts: create/open/list/transcript)
              AgentsPlugin.host(session, build) -> NativeAgentHost
              NativeSessionControl/NewSession (native session creation)
              SandboxPlugin.gate + PermissionGate (permission facts)
              PresetsPlugin.ensure (preset realm materialization)
              WebAgentHooks -> EventJournal -> SSE (output tap only)
              UsageSink / TraceExportSink in the AgentBuild services bag
              (usage()/context_tokens() stay live through the native taps)

    browser (vanilla JS shell)  --fetch()-->  /api/...   (commands)
                                --EventSource--> /api/sessions/{id}/events (stream)
                                --transcript--> /api/sessions/{id}/transcript (facts replay)

一次 turn 的路径与 TUI 完全一致：`POST /messages` -> 有界队列 -> `NativeAgentHost.send()` ->
AgentLoop 发出 `AgentHooks` 事件 -> `WebAgentHooks` 转成 typed wire event -> journal -> SSE。
中断走 `NativeAgentHost.interrupt()`（取消 native 任务），不靠关闭 socket。

## 3. 模块（Module map）

| 模块 | 职责 |
| --- | --- |
| `security.py` | 令牌生成/比较、Host/Origin/Sec-Fetch-Site 校验、安全响应头、CSP、SSE 头 |
| `wire.py` | 冻结 dataclass 请求/响应、`HttpError` 状态映射、JSON 解码与字段校验、body 上限 |
| `events.py` | `WebEventType`/`StatusPhase` StrEnum、10 种 typed 事件、snake_case 序列化、SSE 帧 |
| `journal.py` | 单调 seq 事件日志（容量 4096）、按 seq 回放、心跳、关闭语义 |
| `hooks.py` | `WebAgentHooks`：`AgentHooks` 适配器（无 Textual），deltas/tool 事件 -> typed 事件 |
| `surface.py` | `WebSurface` Protocol、`Route`、`RouteRegistry`、`WebService` Protocol、内置 surfaces |
| `server.py` | stdlib asyncio HTTP/1.1 服务器：解析、鉴权、路由分发、SSE、keep-alive、关闭 |
| `service.py` | `WebSessionHost`：native 会话附加/回放/发送/中断/状态/transcript/SSE |
| `host.py` | `WebHost`：组合 service + server（可自 boot kernel），`start()`/`stop()` |
| `plugin.py` | `WebHostPlugin` + `WebHostConfig`：插件缝，`setup()` 不监听 |
| `assets.py` | 单页 HTML/CSS/JS 字符串常量，token 模板注入 |
| `__main__.py` | `python -m deeppyro.web` CLI |
| `version.py` | `__version__`（独立模块，避免包初始化循环） |

## 4. HTTP 路由（Routes）

所有路由（**包括静态资产**）都要求令牌。两种携带方式：请求头
`Authorization: Bearer <token>`，或查询参数 `?token=<token>`（EventSource 与页面资产链接
无法设置请求头，因此使用查询参数；页面由宿主打印的 URL 打开时自动带上）。

| 方法 | 路径 | 请求体 | 响应 |
| --- | --- | --- | --- |
| GET | `/` | - | 单页 HTML（token 已注入资产 URL） |
| GET | `/assets/app.css` | - | CSS |
| GET | `/assets/app.js` | - | JS |
| GET | `/api/health` | - | `{status, version, active_session_id}` |
| GET | `/api/sessions` | - | `{sessions:[...], active_session_id}`，新的在前，最多 50 条 |
| POST | `/api/sessions` | 可选 `{cwd,preset,provider,model,title}` | 201 `{session_id,resumed,title,provider,model,preset,cwd}` |
| GET | `/api/sessions/{id}` | - | 状态（见下） |
| POST | `/api/sessions/{id}/resume` | `{}` | 200 绑定快照（同 create 的字段） |
| GET | `/api/sessions/{id}/transcript` | - | `{session_id,title,readable,issues,entries:[{role,text,is_error,tool_names}],active,busy}` |
| POST | `/api/sessions/{id}/messages` | `{text}`（1..100000 字符） | 202 `{session_id,accepted,queued}` |
| POST | `/api/sessions/{id}/interrupt` | `{}` | `{session_id,interrupted,detail,dropped}` |
| GET | `/api/sessions/{id}/events` | - | `text/event-stream`（SSE） |

状态文档（active 会话）：`session_id, active, title, provider, model, preset, cwd,
permission_preset, context_tokens, context_window, usage{input_tokens,output_tokens,
cached_input_tokens}, busy, pending`。未挂接的会话返回同样的键，但 `active=false`、
usage 为零、context_tokens 为 0（native 事实里没有按会话持久化的 token 账本）。

错误统一为 JSON 信封 `{"error":{"code","message"}}`（405 额外带 `allow` 字段和 `Allow` 头），
状态码：400 请求体非法、401 令牌缺失/错误、403 Host/Origin/Sec-Fetch-Site 被拒、
404 路由或会话不存在、405 方法不允许、409 会话被锁定/团队预设/模型已不存在、
413 body 超过 1 MiB、429 队列满、500 内部错误、503 宿主正在关闭。

HTTP 语义：HTTP/1.1 keep-alive（HTTP/1.0 默认 close，显式 `Connection: close` 尊重）、
HEAD 复用 GET 路由且只回头部、OPTIONS 返回 204 + `Allow`、请求行/头部上限 64 KiB、
最多 128 个头、仅支持 `Content-Length`（拒绝 `Transfer-Encoding`）、空闲/写超时各 30 秒。
服务器不终止 TLS：明文 loopback，仅本机可连。

## 5. 事件流与回放（SSE, replay）

SSE 数据帧 = `id: <seq>` + `data: <json>`。每个事件都带 `seq`（单调递增）；
浏览器重连时 EventSource 自动发送 `Last-Event-ID`，服务器同时接受 `?after=<seq>`，
从 journal 中回放该 seq 之后的事件；没有事件时每 15 秒发送 `: keep-alive` 注释帧。
事件类型（全 snake_case）：

| type | 字段 | 说明 |
| --- | --- | --- |
| `session` | `session_id` | 会话已挂接（create/resume） |
| `status` | `phase` = `thinking`/`done`/`interrupted` | turn 生命周期 |
| `step` | `turn, step` | agent 步进（诊断用） |
| `text` | `text` | 助手可见文本增量 |
| `thinking` | `text` | 推理增量 |
| `tool_call` | `call_id, name, arguments` | 工具开始（arguments 为字符串） |
| `tool_result` | `call_id, is_error, text` | 工具结束 |
| `final` | `text` | 助手最终文本（替换流式文本） |
| `usage` | `input_tokens, output_tokens, cached_input_tokens` | turn 用量 |
| `error` | `message` | provider/budget/长度截断等错误 |

与 TUI sink 的 dict 契约（`docs/design/tui-native-runtime.md` 附录 A，camelCase：
`sessionId/callId/isError/input,output,cached`）**有意不同**：Web wire 一律 snake_case、
用量字段显式命名；TUI 契约不被改动，转换只发生在 `web/hooks.py` + `web/events.py`。

回放语义：`/transcript` 从 native facts 重建完整对话（进程重启也在）；
SSE journal 只是本次进程内的输出 tap（容量 4096 个事件），不承担历史。
因此“打开页面/重连”= 先 transcript 回放，再从 `after=0` 订阅；断线后按 seq 续订不重复不丢。
turn 完成顺序与 TUI 一致：先 `final` 再 `usage` 再 `status done`；若 `finish_reason
== "length"`，在 `done` 之后再补一条 `error "finish_reason: length"`；中断产生
`final`（partial）+ `status interrupted`，不产生 `error`。

## 6. 安全模型（Security model）

防线（全部在 server 分发前完成，顺序：Host/Origin -> token -> route）：

1. **只绑定回环**：`validate_bind_host()` 只接受 `127.0.0.1`/`localhost`，
   其余值（`0.0.0.0`、`::`、LAN IP、域名）抛 `ValueError`；CLI 用 `parser.error`
   拒绝并以退出码 2 结束；`WebServer` 不接受外部绑定参数。
2. **每进程随机 bearer token**：`secrets.token_urlsafe(32)`，只打印一次在 URL 里
   （`?token=...`），页面把它注入资产链接；缺失/错误一律 401，静态资产也不例外。
   比较使用 `hmac.compare_digest`。
3. **DNS rebinding / 跨站**：校验 `Host` 必须是回环名且端口等于绑定端口；
   `Origin` 若存在必须是 `http://127.0.0.1:<port>` 或 `http://localhost:<port>`
   （`null`/无 scheme 拒绝）；`Sec-Fetch-Site` 只允许 `same-origin`/`none`。
   不返回 CORS 头，因此其他源的 fetch 读不到响应。
4. **CSP 与响应头**：`default-src 'none'; script-src 'self'; style-src 'self';
   connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none';
   frame-ancestors 'none'`，外加 `X-Content-Type-Options: nosniff`、
   `Referrer-Policy: no-referrer`、`X-Frame-Options: DENY`、COOP/CORP、
   `Cache-Control: no-store`（含 secret URL 的页面不落缓存）。
5. **无内联代码**：页面没有内联 script/style/事件属性，JS/CSS 是独立同源资产，CSP
   不需要 `unsafe-inline`；测试静态守卫固定这一点。
6. **XSS 安全渲染**：模型输出只用 `textContent`/`createElement` 渲染，前端代码中
   没有 `innerHTML/outerHTML/insertAdjacentHTML/document.write/eval`（测试守卫）。
7. **输入边界**：JSON 只接受对象；`text` 长度 1..100000；session id 校验字符集与长度；
   body 上限 1 MiB；超限 413 且连接关闭。

明文 HTTP 是有意选择：宿主只绑回环，TLS 没有信任边界收益；token 进入 URL/历史是本机
单用户场景的可接受折衷（`Referrer-Policy: no-referrer` 且不提供跨源链接）。

**诚实缺口**：phase 1 没有工具审批 UI。`PermissionGate` 使用一个始终拒绝的 ask 回调，
因此需要审批的工具调用会被拒绝（错误显示在流里），不会静默放行；审批事件也不上报。
v4-only 的审批审计记录（`approval/asked`/`approval/decided`）在 native session 中不可表示，
`_NativeView.append` 会静默丢弃（返回 -1），不影响决策。

## 7. 生命周期与集成（Lifecycle）

CLI：

    python -m deeppyro.web --port 0 --cwd PATH [--session ID] [--provider P] [--model M]

- `--port 0` 选一个空闲 loopback 端口；启动后打印 banner：URL（带 token）、
  “Localhost only”警告、session id、Ctrl-C 提示。
- 不带 `--session` 时自动创建一个新 native 会话；带则 `resume` 该 id（被其他进程锁定
  则 409）。Ctrl-C 触发优雅关闭：停止监听、关闭连接与 SSE 流、detach 会话、停止
  自建 kernel。

TUI splash 后续接线（**本任务未改 TUI/bundle**）只需要一次调用；TUI 已 boot 好 kernel 时：

    from deeppyro.web.host import WebHost
    host = await WebHost.start(kernel_app=self.kernel_app, port=0, cwd=self.cwd)
    host.print_banner()        # 打印形如 http://127.0.0.1:<port>/?token=<token>
    # ... 浏览器使用期间继续 TUI 或等待 ...
    await host.stop()

F1 = `await WebHost.start(...)`（打印 URL，可选 `webbrowser.open(host.url)`）；
F2 = 不调用，继续 TUI。若走插件缝（推荐，bundle 行由 lead 添加）：

    [[plugins]]
    id = "web"
    plugin = "deeppyro.web.plugin:WebHostPlugin"
    # config = { autostart = false, port = 0 }

挂载后插件处于惰性状态（`setup()` 绝不监听）；启动是显式动作：
`plugin = app.get(WebHostPlugin)`，`host = await plugin.start(port=0, cwd=...)`，
`await plugin.stop()`（kernel unmount 也会调用 `stop()`）。`WebHostConfig.autostart`
目前只在显式 `start()` 中被读取，不会在 setup 期间自动监听。

核心 API（host.py）：

- `WebHost.start(*, kernel_app=None, services=None, config=None, port=0, cwd=None,
  session_id=None, provider=None, model=None, host="127.0.0.1", token=None,
  surfaces=(), registry=None) -> WebHost`：两样都不给时自行 `kernel.boot()` 并负责关闭；
  已有 kernel 时只传 `kernel_app`；插件传 `services` + `registry` + 固定 `token`。
- `host.url`（含 token，供打印/打开）、`host.base_url`（不含 token）、`host.token`、
  `host.port`、`host.active_session_id`、`host.initial_session`。
- `await host.serve_forever()`（直到关闭/取消）、`await host.stop()`（幂等）。

## 8. 插件扩展缝（WebSurface / RouteRegistry）

`WebSurface` 是 `runtime_checkable` Protocol：`name: str` + `routes() -> Iterable[Route]`。
`Route(method, pattern, handler, name)` 支持 `{param}` 路径段；`RouteRegistry` 拒绝重复的
`(method, pattern)`，并负责 405 的 `Allow` 计算（GET 自动获得 HEAD/OPTIONS）。
内置 surface = `default_surfaces(token_provider)`；另一个插件可以在不修改
`server.py`/`surface.py` 的情况下：

    plugin = ctx.require(WebHostPlugin)          # 或注入后取用
    plugin.register_surface(MyPanelSurface())     # 追加 GET /my/panel 等路由
    await plugin.start()                          # 由宿主/集成点显式启动

测试 `tests/web/test_server.py::test_custom_surface_route_is_served` 与
`::test_plugin_is_inert_until_started` 固定该扩展点。`WebHostPlugin` 是普通 kernel
Service（`provide="web"`），因此满足“一切皆插件”：没有特权第二应用路径、没有 TUI/v4 import。

**最小未来 kernel hook（缺口，不是本次改动）**：kernel 目前没有“交互式表面/宿主”枚举契约
（`ctx.ui` 是 post/notify 之类的 no-op UI 槽，不是路由注册表）。若以后要让 splash 在不
直接 import `deeppyro.web.host` 的情况下启动 Web，建议 kernel 增加一个通用的
`HostSurface`/`InteractiveSurface` 契约（提供 name、start()、url、stop()），由 Web 插件和
TUI 共同实现；phase 1 用 web 包内自有 `WebSurface` + 显式 `start()` 已足够，且不扩内核。

## 9. 三条原则映射（Three principles）

**原则 1：一切皆插件。**
Web 宿主是 `WebHostPlugin(Service, provide="web")`，由 bundle/manifest 像其他插件一样发现、
注入、卸载；路由和 UI 面板通过 `WebSurface`/`RouteRegistry` 注册（`plugin.py` +
`surface.py`），第三方插件无需改 `server.py`。测试断言 web 包不 import `textual`、
不 import `deeppyro.v4`、无 aiohttp/flask/uvicorn/fastapi/starlette。缺口：kernel 尚无
通用交互表面契约（见 §8），当前扩展点是 web 包内 Protocol，后续可提成内核契约。

**原则 2：Pythonic，非 TypeScript 形态。**
请求/响应/事件/路线全是冻结 slotted dataclass；`WebSurface`/`WebService` 是 Protocol；
`Method`/`WebEventType`/`StatusPhase` 是 StrEnum；服务器用 asyncio streams、async generator
和显式生命周期；HTTP JSON 一律 snake_case 并在边界校验（`from_payload`/`parse_session_id`），
Python 侧不出现 camelCase，也没有 JS 式事件总线（journal 是 typed 单调日志）。
camelCase 只允许存在于浏览器内部变量（原则原文允许“frontend JS may use camelCase only
inside the browser”），wire 字段测试固定 snake_case。

**原则 3：AI-native。**
Web 层不拥有对话状态：会话 = native append-only facts（`SessionsPlugin.native_*`），
turn 事件来自同一个 `NativeAgentHost`/AgentLoop `AgentHooks` 流，UI 先 `/transcript`
回放事实再订阅 SSE；usage/tokens/context 来自 native host 的 `usage()`/`context_tokens()`
（AgentBuild.services 中的 `usage`/`trace_export` sink 使其存活），不写进聊天消息；
状态端点只读这些 sink/事实。重连靠 seq/`Last-Event-ID` + transcript 回放，服务器不需要
隐藏的会话状态；中断走 `NativeAgentHost.interrupt()`，不靠 socket 关闭。唯一的内存状态是
传输层的一个 binding + 有界队列（8 条），已在 §10 明示。

## 10. 限制与诚实缺口（Limitations）

- 无审批 UI：需要审批的工具被拒绝（见 §6 末）。
- 每个进程只挂接一个会话；在另一个标签页 resume 会让旧标签页的 SSE 只收到该会话已发生
  的事件（journal 按 session 过滤），发送/中断只对当前 binding 有效。
- 发送队列是内存有界队列（8 条），进程退出即丢失；满载回 429。
- SSE journal 仅本次进程有效（容量 4096），历史回放靠 `/transcript`；重启后旧 token 和
  URL 失效。
- 无 TLS、仅单用户/本机；token 出现在 URL/浏览器历史，仅适用于 loopback。
- 团队预设（team mode）在 native 运行时未移植，创建/resume 返回 409。
- 未挂接会话的历史 usage/context 显示为 0（native 事实不含按会话 token 账本）。
- 无 compaction UI、无附件上传、无 markdown 渲染（纯文本，有意收敛 XSS 面）。
- F1/F2 splash 接线与 core bundle 行不在本任务内（由 lead 集成）；TUI 文件未改。
- 浏览器需支持 ES2020 + EventSource + fetch（现代 Chromium/Firefox/Safari 均可）。
- `--host` 只接受回环值；不存在“暴露到局域网”的开关，这是产品约束而非遗漏。

## 11. 测试（Tests）

`tests/web/test_server.py`（真实 127.0.0.1 临时端口；无外部网络；不支持回环时整模块 skip）：
绑定校验与 127.0.0.1 断言、token 401、Host/Origin/Sec-Fetch-Site 403、body 413、静态资产与
CSP/安全头、404/405/Allow、400 非法 JSON、keep-alive 同连接两请求、自定义 surface、
SSE 帧、API 到 service 的分发、优雅关闭释放端口、插件惰性与扩展点、wire snake_case、
journal 回放、AST 导入守卫，以及用 `LlmPlugin.native_provider` 脚本化 provider +
真实 kernel boot 的 create/send/stream/transcript/status、interrupt + 有界队列、
resume/列表/未挂接会话 404、队列满 429、插件缝经 kernel 启动宿主（全程 loopback，无外部网络）。

`tests/web/test_frontend.py`：页面无内联 script/style/事件属性、无外部 URL/CDN/字体、
CSP 仅同源、token 注入与转义、CSS 响应式、JS 无 markup 注入 API、EventSource+fetch+
Bearer+after/seq、快捷键、snake_case wire 字段、资产为纯模块字符串、无构建步骤。

验证命令（本任务实测）：

    .venv\Scripts\python.exe -m pytest tests/web -q          # 36 passed
    .venv\Scripts\python.exe -m ruff check src/deeppyro/web tests/web
    .venv\Scripts\python.exe -m ruff format --check src/deeppyro/web tests/web
    .venv\Scripts\python.exe scripts/typecheck.py            # web 文件无新增错误

（全仓 `ruff`/`typecheck` 仍有并发行其他 agent 文件的既知新增项，不在本任务范围内修复；
本任务只保证 `src/deeppyro/web/` 与 `tests/web/` 干净。）
