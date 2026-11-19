# web: the loopback web host for the native runtime

> **状态**：迁移中（0.3.0 · React SPA） · **层级**：Web 宿主规格 · **最后核对**：2026-11-20
>
> 2026-11-20 起浏览器层已切换为 React/TS SPA，NiceGUI 已移除。本文的路由/组件段落仍保留旧 NiceGUI 描述，
> 待按 `docs/design/web-ui-followups.md` 重写；JSON API、鉴权与宿主生命周期仍然有效。
> 实现：`src/deeppyro/web/`；使用指南：[guides/web-ui.md](../guides/web-ui.md)。
> 关联契约：[native/schema.md](../native/schema.md)（schema 1 会话）、[loop.md](loop.md)（AgentLoop）、
> [sandbox.md](sandbox.md)、[plugin-platform.md](plugin-platform.md)、[tui.md](tui.md)（splash F1 入口）。

## 1. 定位与范围（Scope）

Web UI 是 native agent runtime 的**宿主/外壳（host and shell）**，不是第二个 agent 实现。
浏览器只做三件事：把用户动作 POST 给宿主、用 EventSource 接收事件、用 timeline/transcript 回放事实。
会话状态全部在 append-only 的 native session log（schema 1）里；Web 进程只持有一个
§active binding§、一个有界发送队列、一个内存事件 journal 和一个审批 broker。进程重启后，
浏览器凭 session id 重新回放，不依赖隐藏对话状态。

0.3.0 的 Web 范围：真实 native 运行时的单会话 Web 宿主、Discord 密度的原创前端、SSE、
工具调用卡、**工具审批 UI**、命令面板与 slash 命令、全量设置页、插件设置表单、CLI、
TUI splash F1 接线、测试与本文档。**不包含**：多会话并行、远程访问/TLS、团队模式 UI、
附件上传、compaction UI、退出命令（关闭浏览器标签即可）。

core bundle **不包含** web 插件行：`WebHostPlugin` 由 launcher/集成方直接构造，不以 bundle 行挂载。

硬性约束：

- 只监听回环地址：只接受 `127.0.0.1`（`localhost` 归一化），没有外部绑定开关。
- 浏览器资产同源、无构建步骤、无 CDN、无外部字体；字体/图标随包分发。
- 不 import `deeppyro.tui`、不 import 已删除的 v4 会话/loop 模块；测试用 AST 守卫固定。
- 命令与设置只在 Python 侧执行，浏览器不直接写 config、不持有凭据明文。

## 2. 架构（Architecture）

    kernel boot (plugin composition)
      |
      +-- WebHostPlugin (Service, provide="web")      src/deeppyro/web/plugin.py
      |     owns RouteRegistry + WebSurfaces; setup() NEVER listens
      |     start() -> WebHost.start(...)   stop() -> WebHost.stop()
      |
      +-- WebHost                                      src/deeppyro/web/host.py
      |     WebSessionHost (runtime)  +  UiServer | WebServer (legacy stdlib)
      |
      +-- UiServer (FastAPI + uvicorn + NiceGUI)       src/deeppyro/web/ui/server.py
      |     127.0.0.1 only, loopback auth middleware, security headers,
      |     JSON API + SSE, five NiceGUI page routes, /brand static assets
      |
      +-- WebSessionHost (WebService)                  src/deeppyro/web/service.py
            SessionsPlugin.native_*        (facts: create/open/list/read/fold)
            AgentsPlugin.host(session, build) -> NativeAgentHost
            SandboxPlugin.gate + PermissionGate + ApprovalBroker
            UsageSink / TraceExportSink in the AgentBuild services bag
            WebAgentHooks -> EventJournal -> SSE
            NativeSessionControl / fold (facts for settings/commands/inspector)

    NiceGUI page (Python element tree -> Quasar/Vue in the browser)
      pages.py  -> layout.py (sidebar/stage/inspector + narrow top bar)
                -> chat.py | settings_view.py | palette.py
      --fetch()-->      /api/ui/...          (sessions, timeline, commands, settings)
      --EventSource-->  /api/ui/sessions/{id}/events   (live turn + approvals)
      --websocket-->    /_nicegui/ws         (element updates, dialogs, timers)

### 2.1 从 P1 静态 shell 切到 NiceGUI

P1 曾用 FastAPI + 服务端 HTML + 手写 vanilla JS 实现四栏外壳；0.3 的 Web UI 按
产品决定**全量切到 NiceGUI 4**：页面、三栏布局、消息流、工具/审批卡、命令面板、
设置表单都是 Python 元素树，浏览器侧由 NiceGUI 自带的 Quasar/Vue 渲染，不再维护
手写 `app.js`/`app.css`。FastAPI 仍是 ASGI 宿主，`/api/ui/*` 与 SSE 契约不变；
NiceGUI 只负责页面与交互，不持有会话状态。视觉由 `theme.py` + 三份
`/brand/*.css` 控制，Discord 密度的排版仍是自有设计，不复用第三方组件库。

一次 turn 的路径与 TUI 相同：`POST /messages` -> 有界队列 -> `NativeAgentHost.send()`
-> AgentLoop 发出 `AgentHooks` 事件 -> `WebAgentHooks` 转 typed wire event -> journal -> SSE。
中断走 `NativeAgentHost.interrupt()`（取消 native 任务），不靠关闭 socket。

## 3. 模块（Module map）

| 模块 | 职责 |
| --- | --- |
| `security.py` | 令牌生成/比较、Host/Origin/Sec-Fetch-Site 校验、安全响应头、CSP、SSE 头 |
| `wire.py` | 冻结 dataclass 请求/响应、`HttpError` 状态映射、JSON 解码与字段校验、body 上限 |
| `events.py` | `WebEventType`/`StatusPhase` StrEnum、typed 事件（含 `ApprovalEvent`）、snake_case、SSE 帧 |
| `journal.py` | 单调 seq 事件日志（容量 4096）、按 seq 回放、心跳、关闭语义 |
| `hooks.py` | `WebAgentHooks`：`AgentHooks` 适配器（无 Textual），deltas/tool 事件 -> typed 事件 |
| `approvals.py` | `ApprovalBroker`/`ApprovalPrompt`/`ApprovalDecision`：待审批、超时、取消、通知 |
| `surface.py` | `WebSurface` Protocol、`Route`、`RouteRegistry`、`WebService` Protocol（扩展缝） |
| `server.py` | 旧 stdlib asyncio HTTP/1.1 服务器（`ui=False`；测试与最小宿主继续使用） |
| `service.py` | `WebSessionHost`：attach/resume/queue/send/interrupt/timeline/facts/approvals/settings 数据源 |
| `host.py` | `WebHost`：组合 service + UiServer/WebServer（可自 boot kernel），`start()`/`stop()` |
| `launch.py` | 进程级启动器（`serve()`/`launch()`）：CLI 与 TUI splash 共用；`ui=True` 为默认 |
| `plugin.py` | `WebHostPlugin` + `WebHostConfig`：插件缝，`setup()` 不监听 |
| `ui/auth.py` | token/HttpOnly cookie、Host/Origin/Sec-Fetch-Site、首次页面 303 去 token |
| `ui/api.py` | `/api/ui/*` JSON 路由、命令分发、设置读写、审批决策、SSE |
| `ui/commands.py` | 25 个 slash 命令的 Web 执行器（真实动作或诚实拒绝） |
| `ui/settings.py` | `ConfigEditor`（文本级编辑 + 校验 + 原子写 + .bak）、`settings_payload`、provider 探活 |
| `ui/server.py` | FastAPI app、NiceGUI `run_with`、安全/鉴权中间件、5 个页面路由、`/brand` 静态资产、`UiServer` 生命周期 |
| `ui/pages.py` | NiceGUI 页面注册：`/`、`/c/{id}`、`/palette`、`/settings`、`/settings/{section}` |
| `ui/bridge.py` | `WebUiBridge`：页面到 `WebSessionHost` 的 typed 边界（bootstrap/sessions/timeline/send/approvals/settings/commands） |
| `ui/layout.py` | 三栏外壳：会话侧栏（顶部品牌区）、`ChatView` 舞台、inspector、窄屏顶栏、New session 弹窗 |
| `ui/chat.py` | 消息流、SSE/轮询、工具卡、审批卡、composer、slash 命令 |
| `ui/settings_view.py` | 全屏设置：provider/agent/permissions/commands/plugins/sessions/web/about |
| `ui/palette.py` | 命令面板：命令/会话/设置搜索、键盘导航、执行与导航 |
| `ui/theme.py` | `/brand` 样式表、Material 图标、头像/徽标/时间/主按钮样式 |
| `ui/assets/style.css` | 设计 token、reset、三栏、sidebar/stage/inspector、窄屏顶栏 |
| `ui/assets/chat.css` | 消息、工具卡、审批卡、composer |
| `ui/assets/settings.css` | 设置页与命令面板 |
| `ui/assets/cyborg-mark.svg` | 原创半面罩机器标（品牌 logo/头像/favicon） |
| `__main__.py` / `version.py` | CLI 入口、独立版本常量 |

## 4. HTTP 路由（Routes）

鉴权：所有页面、资产与 API 都要求 token。首次页面打开用 `?token=...`；`LoopbackAuthMiddleware`
校验后 303 到去掉 token 的 URL，并设置 `dpyro_web`（HttpOnly、SameSite=Strict、Path=/）。
后续请求用 cookie；测试/CLI 可用 `Authorization: Bearer <token>`。API 不重定向，直接 401/403 JSON。

页面（五个 NiceGUI 路由，共用 `ShellView`；`/palette` 与 `/settings` 是路由，不是弹层）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 首页（当前或新建会话） |
| GET | `/c/{session_id}` | 指定会话 |
| GET | `/palette` | 命令面板打开态 |
| GET | `/settings` | 全屏设置 |
| GET | `/settings/{section}` | 指定设置分区 |
| GET | `/brand/{asset}` | style.css/chat.css/settings.css/cyborg-mark.svg/InterVariable.woff2 |
| GET/WS | `/_nicegui/*` | NiceGUI 自有静态资源、WebSocket、favicon |

Web API：

| 方法 | 路径 | 请求体 | 响应 |
| --- | --- | --- | --- |
| GET | `/api/ui/bootstrap` | - | version/health/sessions/active_session_id/commands/presets/permissions/plugins/providers/defaults（含 `defaults.cwd`） |
| GET | `/api/ui/sessions` | - | `{sessions,active_session_id}`；active 行 `read_only=false`、`busy` |
| GET | `/api/ui/sessions/{id}/timeline` | - | 对话时间线 + `journal_seq`（SSE 游标） |
| POST | `/api/ui/sessions` | `{cwd?,preset?,provider?,model?,title?}` | 201 绑定快照 |
| POST | `/api/ui/sessions/{id}/resume` | `{}` | 绑定快照 |
| POST | `/api/ui/sessions/{id}/messages` | `{text}`（1..100000） | 202 `{accepted,queued}` |
| POST | `/api/ui/sessions/{id}/interrupt` | `{}` | `{interrupted,detail,dropped}` |
| POST | `/api/ui/sessions/{id}/rename` | `{title}` | 会话信息 |
| POST | `/api/ui/sessions/{id}/archive` | `{archived?}` | 会话信息 |
| POST | `/api/ui/sessions/{id}/permission` | `{preset}` | 应用后的权限 |
| GET | `/api/ui/approvals?session_id=` | - | `{approvals:[{approval_id,tool,summary,detail,status,...}]}` |
| POST | `/api/ui/approvals/{approval_id}` | `{decision: allow|deny|expire|cancel}` | `{settled:true}`；未知 404 |
| POST | `/api/ui/commands` | `{name,args:[str],session_id?}` | `{status:ok|error|redirect,message?,redirect?,data?}` |
| GET | `/api/ui/settings` | - | config/provider/agent/permissions/commands/plugins/sessions/web/about |
| PATCH | `/api/ui/settings/agent` | provider/model/temperature/max_tokens/max_steps 的子集 | `{ok,config}` |
| PATCH | `/api/ui/settings/provider/{name}` | base_url/api_key/api_key_env 的子集 | `{ok,config}` |
| POST | `/api/ui/settings/providers/{name}/test` | `{}` | `{ok,detail,latency_ms}` |
| POST | `/api/ui/settings/permission` | `{preset}` | `{ok,config}` |
| GET | `/api/ui/plugins/{id}/settings` | - | 插件设置 schema/values/revision |
| PUT | `/api/ui/plugins/{id}/settings` | `{values,expected_revision?}` | 写回 + live/restart 字段 |

旧 `/api/*` 家族（`/api/health`、`/api/sessions...`、旧 SSE）保留给 stdlib 宿主与兼容客户端；
NiceGUI 前端只使用 `/api/ui/*`。错误统一 `{"error":{"code","message"}}`：
400 参数、401 token、403 Host/Origin、404 路由/会话/审批、405 方法、409 会话锁/设置版本冲突、
413 body 超过 1 MiB、429 队列满、500 内部、503 服务缺失/关闭中。

## 5. 事件流与回放（SSE, replay）

SSE 数据帧 = `id: <seq>` + `data: <json>`。浏览器重连时 EventSource 自动发送
`Last-Event-ID`，同时接受 `?after=<seq>`，从 journal 回放该 seq 之后的事件；
无事件时每 15 秒发送 `: keep-alive`。前端保存 `lastEventSeq`，连接 URL 使用
`after=max(lastEventSeq, timeline.journal_seq)`，避免重连时重放已由 timeline 渲染的历史。

| type | 字段 | 说明 |
| --- | --- | --- |
| `session` | `session_id` | 会话已挂接 |
| `status` | `phase=thinking/done/interrupted` | turn 生命周期 |
| `step` | `turn,step` | 步进（诊断） |
| `text` | `text` | 助手文本增量 |
| `thinking` | `text` | 推理增量 |
| `tool_call` | `call_id,name,arguments` | 工具开始 |
| `tool_result` | `call_id,is_error,text` | 工具结束 |
| `final` | `text` | 最终文本（替换流式文本） |
| `usage` | `input_tokens,output_tokens,cached_input_tokens` | turn 用量 |
| `approval` | `approval_id,tool,summary,detail,status` | 审批 pending/决策 |
| `error` | `message` | provider/budget/长度截断等 |

回放语义：`/timeline` 从 native facts 重建完整对话（重启也在）；SSE journal 只是本次
进程内的输出 tap（容量 4096），不承担历史。timeline 会把“无真实结果的工具调用”标为
`pending=true`（例如等待审批），不把它误画成错误；`data-journal_seq` 是 SSE 的起点。
turn 完成顺序与 TUI 一致：`final` -> `usage` -> `status done`；`finish_reason=length`
在 done 后再补一条 error；中断产生 partial final + status interrupted，不产生 error。

### 5.1 WebSocket 心跳与重连窗口

NiceGUI 的 Socket.IO 心跳由 `reconnect_timeout` 推导：ping interval = 80%、
ping timeout = 40%。默认 3 秒意味着 4 秒一次 ping、只等 2 秒 pong；首屏渲染或
繁忙页面偶尔错过一个 pong 就会触发重连，短暂显示 “Connection lost.”。
`src/deeppyro/web/ui/server.py` 把 `reconnect_timeout` 设为
`RECONNECT_TIMEOUT_SECONDS = 15.0`（ping 12 秒 / timeout 6 秒），给本地首屏和
工具审批留出停顿余量；真正的宿主退出仍会立即显示 offline，不会等满 15 秒。

## 6. 工具审批（Approvals）

`WebSessionHost` 在 attach 时把 `PermissionGate` 的 ask 回调接到 `ApprovalBroker`。
需要审批时：

1. Goan 的 gate 调用 `ask(session_id, ApprovalRequest)`，broker 生成 `approval_id`，
   登记 pending 并发出 `ApprovalEvent(status="pending")`；turn 挂起等待。
2. 浏览器 GET `/api/ui/approvals?session_id=...` 显示卡片（工具、摘要、详情、Allow once / Deny）。
3. `POST /api/ui/approvals/{id}` 决策；broker 唤醒等待者，发出已结算事件；allow 后工具继续，
   deny 后工具得到拒绝错误并继续收尾。
4. turn 结束/detach/close 会 `cancel_session`，不会留下永久 pending。

超时默认 300 秒（`ApprovalDecision.EXPIRE`）。审批摘要与详情来自 `sandbox.policy`，
对越界写入会显示目标路径与所需权限；工具卡在等待期间保持 PENDING，不显示伪造的
missing-result 错误。

## 7. 命令与设置 UI 化（Commands & settings）

**命令**：`ui/commands.py` 把 25 个 core command 映射到真实宿主动作：

- 真实执行：`new`、`resume`、`sessions`、`rename`、`archive`、`unarchive`、`permission`、
  `status`、`plan`、`goal`、`cwd`、`plugins`、`help`、`clear`。
- `cwd` 无参只读当前会话目录；带路径时调用 `create_session(CreateSessionRequest(cwd=path))`，
  新建并切换到该目录的 native 会话，旧会话保留在侧栏；结果 `data.session_id` 驱动前端跳转。
- 导航：`settings`、`panel`（跳设置分区）。
- 只读：`provider`/`model`/`preset` 无参时显示当前值；带参跳设置并明确说明只影响新会话。
- 诚实拒绝：`compact`、`jobs`、`team`、`thinking`、`hooks` 尚未接 Web 宿主时返回
  `status=error` 与原因；`exit` 提示关闭标签页。绝不假装成功。
- `archive` 当前会话时会新建一个 native 会话并在 `data.session_id` 返回新 id，前端据此跳转。

**新建会话目录**：侧栏 SESSIONS 的 `+`、窄屏顶栏的 `+`、`Ctrl/Cmd+N` 打开 **New session** 弹窗；路径留空
提交 `{}`，否则提交 `{"cwd": path}` 到 `POST /api/ui/sessions`。弹窗从现有会话去重出
Recent workspaces，服务端 `resolve_workspace` 的校验错误在弹窗内显示；成功后刷新侧栏并跳到
`/c/<new-id>`。每个会话的 cwd 在创建时写入 header，运行中不修改。

**设置**：`ConfigEditor` 对 `~/.dpro/config.toml` 做**文本级**编辑（保留注释/顺序/未知插件表），
每次写前用 `Config.model_validate` 全量校验，`NamedTemporaryFile` + fsync + `os.replace`
原子落盘，并生成编号 `.bak`。API key 只报告 `api_key_set`，永不回传明文。写 config 不会热改
已启动的 `KernelConfig`——前端明确提示“new turns only / restart 后完全生效”。

插件设置来自 `SettingsPlugin`/`SettingsStore`：`GET` 返回 schema/values/revision，`PUT` 带
`expected_revision` 做 CAS；`live` 字段热应用，`restart` 字段标记需要重启；冲突 409、
未知插件 404、空 values 400。插件不提供 web 面板时，用 schema 表单兜底。

## 8. 安全模型（Security model）

1. **只绑定回环**：`validate_bind_host()` 只接受 `127.0.0.1`/`localhost`；CLI 拒绝其余值
   并以 2 退出。没有外部绑定参数。
2. **每进程随机 token**：`secrets.token_urlsafe(32)`；页面首次带 `?token=`，校验后换
   HttpOnly/SameSite=Strict cookie 并 303 去 token；比较用 `hmac.compare_digest`。
3. **同源**：Host 必须是回环名 + 绑定端口；Origin 若存在必须是同端口回环；
   Sec-Fetch-Site 只允许 same-origin/none；不返回 CORS 头。
4. **CSP**：`default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self';
   img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none';
   frame-ancestors 'none'`，无 inline 授权；另有 nosniff/Referrer-Policy/X-Frame-Options/COOP/CORP。
5. **XSS 安全渲染**：前端只用 `createElement/textContent`，无 `innerHTML/eval`；模型输出
   走显式 markdown 渲染器，链接不外跳；测试守卫固定。
6. **输入边界**：JSON 仅对象；text 1..100000；session id 校验；body 1 MiB；超限 413。
7. **凭据**：API key 只写不读；设置快照仅 `api_key_set`；日志/错误消息脱敏。

明文 HTTP 是有意选择：只绑回环，TLS 没有信任边界收益；token 首次进入 URL 后立即换成
HttpOnly cookie（`Referrer-Policy: no-referrer`）。

## 9. 生命周期与集成（Lifecycle）

CLI：

    python -m deeppyro.web --port 0 --cwd PATH [--session ID] [--provider P] [--model M]

- `--port 0` 选空闲 loopback 端口；banner 打印 URL（带 token）、localhost-only 警告、
  session id、Ctrl-C 提示。
- 无 `--session` 时自动创建 native 会话；带则 resume（被其他进程锁定则 409，死进程锁会被回收）。
- Ctrl-C 优雅关闭：停止监听、关闭 SSE/连接、detach 会话、停止自建 kernel。

TUI splash：**F1** 让 `DeepPyroApp.run()` 返回 `"web"`，`tui/main.py` 调
`deeppyro.web.launch.launch(...)`，本进程变成 Web 宿主；**F2 / Enter** 进入终端 TUI。

共享启动器 `web/launch.py`：

    async def serve(*, cwd=None, session_id=None, provider=None, model=None,
                    port=0, host=LOCALHOST, banner=None, ui=True) -> None:
        web = await WebHost.start(..., ui=ui)
        web.print_banner(banner)
        try:
            await web.serve_forever()
        finally:
            await web.stop()

    def launch(**kwargs) -> int:      # Ctrl-C -> 130，正常关闭 -> 0

插件缝（**不是** 0.3.0 core bundle 的内容）：

    [[plugins]]
    id = "web"
    plugin = "deeppyro.web.plugin:WebHostPlugin"
    # config = { autostart = false, port = 0 }

挂载后插件惰性（`setup()` 绝不监听）；`plugin.start(port=0, cwd=...)` 显式启动，
`plugin.stop()`/unmount 关闭。`WebHost.start(ui=True)` 使用 UiServer（FastAPI + NiceGUI）；
`ui=False` 保留 stdlib WebServer（旧 API/测试）。

## 10. 三条原则映射（Three principles）

**原则 1：一切皆插件。** Web 宿主是 `WebHostPlugin(Service, provide="web")`，由 bundle/manifest
发现、注入、卸载；页面/API/设置都经插件边界取服务；`WebSurface`/`RouteRegistry` 允许第三方
注册路由，无需改 `server.py`。缺口：kernel 尚无通用“交互表面”契约，splash 直接 import
`deeppyro.web.launch`；当前扩展点是 web 包内 Protocol。

**原则 2：Pythonic。** 请求/响应/事件/命令/审批全是 frozen slotted dataclass；
`WebSurface`/`WebService`/`UsageSink` 是 Protocol；`WebEventType`/`StatusPhase`/`ApprovalDecision`/
`ShellView` 是 StrEnum；服务用 asyncio TaskGroup/async generator 和显式生命周期；
HTTP JSON 一律 snake_case，camelCase 只存在于浏览器内部。

**原则 3：AI-native。** Web 层不拥有对话状态：会话 = native append-only facts，turn 事件来自
同一个 `NativeAgentHost`/AgentLoop `AgentHooks` 流；timeline 先回放事实，再按 seq 订阅；
usage/tokens/context 来自 native host 的 taps；审批是显式事实与显式决策；中断走
`NativeAgentHost.interrupt()`。唯一内存状态是 binding、队列（8）、journal（4096）与审批 broker。

## 11. 限制与诚实缺口（Limitations）

- 每个进程只挂接一个会话；多标签页 resume 会把 binding 移到新标签页，旧标签页的发送/中断失效。
- 发送队列/审批/journal 都在内存，进程退出即丢失；历史只在 native log。
- 无 TLS、仅本机单用户；token 首次进入 URL/历史，随后换成 HttpOnly cookie。
- 团队模式（team preset）在 Web 运行时仍被拒绝（409），团队只在 TUI/headless 可用。
- 未挂接会话的 usage 从本地 usage ledger（`~/.dpro/usage/usage.jsonl`）回填；context 取该会话最后一次请求的 input_tokens，不是 runtime 实时值。
- NiceGUI 需要放宽 CSP 的 `unsafe-inline`/`unsafe-eval`（Vue runtime compiler）；脚本仍只允许同源，页面不加载 CDN。
- 无附件上传、无 compaction UI、无多会话并行、无移动端专门布局。
- 无退出命令；关闭标签页/Ctrl-C 结束宿主。
- `ui=False` 的 stdlib 宿主仍存在，但无 NiceGUI 壳/命令/设置/审批；生产 launcher 默认 `ui=True`。
- 浏览器需支持 ES2020 + EventSource + fetch（现代 Chromium/Firefox/Safari）。

## 12. 测试（Tests）

`tests/web/` 183 passed（本机 loopback，无外部网络）：

- `test_server.py`：stdlib 宿主绑定/鉴权/SSE/队列/插件缝/真实 kernel boot。
- `test_ui_auth.py`：token→cookie 303、API 401/403、Host/Origin/Sec-Fetch-Site、WS 1008、CSP 头。
- `test_ui_bridge.py`：页面桥接的 bootstrap 投影、timeline journal_seq、typed 错误、命令/审批/设置转发。
- `test_ui_chat.py`：ChatView 渲染与 SSE/轮询、草稿、工具/审批卡、只读/中断、slash 命令。
- `test_ui_settings_view.py`：设置 schema、provider 表单、保存/测试反馈、插件 field。
- `test_ui_palette.py`：命令/会话/设置分组、过滤、键盘选择、执行结果路由。
- `test_ui_commands.py`：new/resume/sessions/rename/archive/permission/status/plan/goal/plugins/
  settings/clear、cwd 无参只读/带路径新建/无效目录报错与诚实拒绝。
- `test_ui_settings.py`：ConfigEditor 原子写/.bak/注释保留/字段校验/provider 探活/插件 CAS。
- `test_approvals.py`：broker pending/decide/timeout/cancel/session 过滤。
- `test_timeline.py`：missing tool result 保持 pending、真实 result 合并、错误合并。
- `test_ui_server.py`：NiceGUI mount 的 heartbeat/reconnect_timeout 接线。
- `test_launch.py`：launcher banner/Ctrl-C 130/关闭/open_browser。

验证命令：

    .venv\Scripts\python.exe -m pytest tests/web -q
    .venv\Scripts\python.exe -m ruff check src/deeppyro/web tests/web
    .venv\Scripts\python.exe -m ruff format --check src/deeppyro/web tests/web
