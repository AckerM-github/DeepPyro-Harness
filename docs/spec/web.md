# Web UI 宿主规格（React SPA + 回环 JSON API）

> **状态**：现行（0.3.0 · React SPA） · **层级**：功能规格 · **最后核对**：2026-11-20
> 实现事实以 `src/deeppyro/web/`、`web/` 与 `tests/web/` 为准；本文件描述对外契约与静态产物边界。
> 文档入口见 [docs/README.md](../README.md)，规格索引见 [README.md](README.md)；SPA 架构与
> 技术取舍见 [docs/design/react-web-ui.md](../design/react-web-ui.md)。

## 1. 范围与硬约束

Web UI 是 native 运行时的**浏览器外壳**，不是第二个 agent：

- 浏览器只做三件事：调用回环 JSON API 发动作、订阅 SSE 接收事件、回放 native session facts。
- 会话状态全部在 append-only 的 native session log（schema 1）里；Web 进程只持有当前绑定、
  有界发送队列（`DEFAULT_PENDING_TURNS = 8`）、内存事件 journal 与审批 broker。
- 0.3.0 范围内：React/Vite 单页应用、conversation-first 工作台（左栏会话、中栏对话、右侧
  详情抽屉默认关闭）、流式聊天气泡、语义化工具卡与折叠过程层、审批条（once/session 两种
  scope）、排队条、计划进度条、composer 宿主控件（模型/权限/上下文用量）、快速切换器、
  目录选择器、slash 命令、全屏设置面板、TUI splash F1 与 `python -m deeppyro.web` 两种入口、
  web 测试。
- 0.3.0 范围外：多会话并行、远程/TLS、team UI、附件上传、压缩 UI、移动优先布局、消息编辑/
  重新生成、会话内全文搜索、退出命令（关页面或 Ctrl-C）。

硬约束：

1. **只监听回环**：只接受 `127.0.0.1`/`localhost`（统一归一化为 `127.0.0.1`），不提供任何外部
   绑定、TLS 或反向代理模式。`python -m deeppyro.web --host` 传入其它值直接 `parser.error`。
2. **同源资产、无 CDN**：脚本、样式、字体、图标全部由本进程提供；构建产物随 Python 包发布，
   页面不引用任何外部 URL。
3. **浏览器不执行 Python、不写配置**：命令与设置全部经 JSON API 在 Python 侧执行；浏览器不持有
   provider 凭据，也不直接改 `config.toml`。
4. **core bundle 不含 web 插件行**：`WebHostPlugin` 由 launcher/集成方显式装配；`setup()` 永不监听。
5. **no Python import of deleted modules**：web 层不得 import `deeppyro.tui` 或已删除的 v4 模块；
   由 AST 守卫测试保证。

## 2. 进程与模块

```
browser (React SPA, typed fetch + EventSource)
  -> UiServer (FastAPI/uvicorn ASGI, loopback auth, static shell, JSON API, SSE)
  -> WebSessionHost (web/service.py: sessions, queue, journal, approvals, hooks)
  -> NativeAgentHost / plugin kernel / native session log (schema 1)
```

- `web/plugin.py`：`WebHostPlugin(Service, provide="web")` 持有 `RouteRegistry` 与 `WebSurfaces`。
  `setup()` 只登记，不监听；`start()` 调 `WebHost.start()`；`stop()/unmount` 关闭宿主。
- `web/host.py`：`WebHost` 组合 `WebSessionHost` 与服务器。`ui=True`（生产默认）用 `UiServer`；
  `ui=False` 保留旧的 stdlib asyncio HTTP 宿主，只服务 legacy `/api/*` 家族与内联 HTML shell，
  供兼容客户端/测试使用，不是 SPA 外壳。
- `web/ui/server.py`：`UiServer` + `create_app()`。FastAPI 应用，`docs_url/redoc_url/openapi_url` 全关，
  默认响应类 `JSONResponse`；挂载 `/assets`（Vite 产物）与 `/brand`（共享品牌资产），
  中间件顺序 security（外）→ auth（内）。
- `web/service.py`：`WebSessionHost`（`WebService`）把 native sessions、`AgentsPlugin.host`、
  `SandboxPlugin.gate/PermissionGate/ApprovalBroker`、usage/trace sink、`WebAgentHooks`、
  `NativeSessionControl`/fold 接起来。turn 路径：`POST /messages` → 有界队列 →
  `NativeAgentHost.send()` → `AgentLoop` `AgentHooks` → `WebAgentHooks` typed wire events →
  journal → SSE。打断走 `NativeAgentHost.interrupt()`，不是关 socket。
- `web/ui/api.py`：`/api/ui/*` 路由（浏览器唯一入口）+ legacy `/api/*` 路由。
- `web/ui/auth.py`：cookie/token/Host/Origin 中间件与 `UI_CSP`。
- `web/ui/commands.py`：slash/命令面板执行；`web/ui/settings.py`：配置编辑与插件 settings CAS。
- `web/security.py`：回环、token 常量时间比较、Host/Origin 校验、安全头；`web/wire.py`：请求/响应
  边界与错误类型；`web/events.py`：typed wire 事件；`web/journal.py`：内存回放日志；
  `web/hooks.py`：`AgentHooks` → wire 事件；`web/approvals.py`：审批 broker；
  `web/surface.py`：legacy stdlib surface。
- `web/launch.py` / `web/__main__.py`：`serve()` 与 `launch()`、CLI 参数与 banner。

## 3. 前端源码与构建产物

前端是一个独立的 Vite/React/TypeScript 工程，位于 `web/`：

| 路径 | 内容 |
| --- | --- |
| `web/index.html` | Vite 入口 HTML（`#root` + `/src/main.tsx`） |
| `web/src/main.tsx` | `createRoot(#root)`，StrictMode，渲染 `App` |
| `web/src/App.tsx` | 工作台 shell、SSE 订阅、会话/审批/队列/计划/设置状态 |
| `web/src/api.ts` | typed fetch API + 自管重连的 `subscribeEvents()` |
| `web/src/types.ts` | wire 类型（`Bootstrap`、`Timeline`、`WebEvent`、`SettingsPayload` 等） |
| `web/src/styles.css` | DSH 风格暗色 token + conversation-first 版式 |
| `web/src/components/` | `Sidebar`、`Chat`（Transcript）、`Composer`、`ToolCard`、`PermissionMenu`、`ModelMenu`、`ApprovalBar`、`QueueDock`、`PlanBar`、`Inspector`、`Markdown`、`Palette`、`Settings`、`NewSession`、`DirectoryPicker`、`Icon` |
| `web/src/lib/` | `format.ts`（含 `humanError`）、`toolMeta.ts`、`commandArgs.ts`、`liveTurn.ts`（流式状态机） |
| `web/public/brand/cyborg-mark.svg` | Vite 公共资产（构建时复制为 `static/brand/`） |

浏览器侧单测由 vitest（`pnpm test:unit`）覆盖：`web/src/lib/commandArgs.test.ts`（10）、
`web/src/lib/liveTurn.test.ts`（15，流式状态机）与 `web/src/api.sse.test.ts`（4，SSE 重连），
共 29 条。

构建命令（Node.js + pnpm；构建需要网络安装依赖）：

    cd web
    pnpm install
    pnpm build        # = tsc --noEmit && vite build
    pnpm typecheck    # = tsc --noEmit
    pnpm dev          # 开发服务器 127.0.0.1:5173，/api 代理到 DEEPPYRO_WEB_ORIGIN（默认 http://127.0.0.1:8765）
    pnpm test:unit    # vitest

`vite.config.ts` 固定 `base: "/"`、`build.outDir: "../src/deeppyro/web/ui/static"`、
`assetsDir: "assets"`、`emptyOutDir: true`、`sourcemap: false`。

**静态产物契约**：构建输出目录 `src/deeppyro/web/ui/static` 是**提交进仓库的构建产物**，
不是运行期生成物：

- `static/index.html`：SPA shell，包含 `<div id="root">`、一个 `type="module"` 的
  `/assets/index-<hash>.js`、一个 `/assets/index-<hash>.css`，以及 `/brand/cyborg-mark.svg` favicon。
  不含内联脚本/样式，不含 token 占位符（token 只出现在服务器打印的 URL 中）。
- `static/assets/`：Vite 输出的内容哈希 JS/CSS。旧 hash 文件由 `emptyOutDir` 清理，不要手工增删。
- `static/brand/`：`public/` 的拷贝，随包发布；`/brand` 请求实际由 `ui/assets/` 提供（见 §4）。
- 源码与产物的对应关系由 `tests/web/test_frontend.py` 守卫：shell 引用的每个 `/assets/*` 与
  `/brand/*` 文件都必须存在，且 pyproject 的 wheel 打包配置必须收录 `src/deeppyro`。
- wheel 打包：`[tool.hatch.build.targets.wheel] packages = ["src/deeppyro"]` 默认收录包内全部文件
  （含 `web/ui/static` 与 `web/ui/assets`），无需额外 `force-include`；`uv build --wheel` 后应能在
  产物的 `deeppyro/web/ui/static/index.html`、`static/assets/*`、`assets/*` 中找到它们。
- 若 `static/index.html` 缺失（未构建即安装源码），宿主不会崩：`GET /` 返回
  `503` 纯文本 `Web UI is not built. Run pnpm build in web/.`，`cache-control: no-store`。

## 4. HTTP 路由与静态文件

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/` | 返回 `static/index.html`（SPA shell）；缺失时 503 文本 |
| GET | `/c/{session_id}` 及其它非 `/api` 路径 | 同一个 SPA shell，由前端按 `/c/<id>` 深链选择会话 |
| GET | `/assets/{file}` | Vite 产物；`cache-control: public, max-age=31536000, immutable` |
| GET | `/brand/{file}` | `ui/assets/` 中的共享品牌资产；`cache-control: no-store` |
| * | `/api` 或 `/api/...` 未匹配时 | JSON 404（`{"error":{"code":"not_found",...}}`），绝不回 HTML shell |

要点：

- 未知 API 路径与未知页面路径分流：`_is_api_path()` 只把 `api` 与 `api/` 前缀交给 API；
  其余所有路径回 SPA shell，因此浏览器端路由（`/c/{id}`）刷新可用。
- shell 与品牌资产不哈希，必须每次重新验证（`no-store`）；只有 `/assets/*` 的内容哈希产物可长缓存。
- 所有响应都带安全头（§8）；API 与 shell 都是 `no-store`。
- 没有 WebSocket 路由：实时通道只有 SSE。

### 4.1 JSON API（浏览器使用）

| 方法 | 路径 | 请求/响应 |
| --- | --- | --- |
| GET | `/api/ui/bootstrap` | `{version, health, sessions, active_session_id, commands, presets, permissions, plugins, providers, defaults}` |
| GET | `/api/ui/sessions` | 查询参数 `limit`（默认 50，1..200）、`offset`、`query`、`include_archived`；返回 `{sessions: SessionSummary[], active_session_id, total, limit, offset, has_more, query}` |
| GET | `/api/ui/sessions/{id}/timeline` | `Timeline`；额外带 `journal_seq`（SSE 订阅起点）与顶层 `state`（goal/todos/deliverables/tasks/handoffs 只读快照，可为 null） |
| POST | `/api/ui/sessions` | `{cwd?, preset?, provider?, model?, title?}` → 201 `{session_id,title,cwd,provider,model,preset}` |
| POST | `/api/ui/sessions/{id}/resume` | `{}` → `{session_id,resumed}`；会话被其它进程锁定时 409 |
| POST | `/api/ui/sessions/{id}/messages` | `{text: 1..100000}` → 202 `{session_id,accepted,queued}` |
| POST | `/api/ui/sessions/{id}/interrupt` | `{}` → `{session_id,interrupted,detail,dropped}` |
| POST | `/api/ui/sessions/{id}/model` | `{provider, model}` → `{ok, session_id, provider, model, applied: "next_turn"\|"deferred"}`；未知 provider/model 400、未知会话 404、未 attach 409 |
| GET | `/api/ui/sessions/{id}/queue` | `{session_id, items:[{queue_id,text,created_at}]}`；只读有界队列 |
| DELETE | `/api/ui/sessions/{id}/queue/{queue_id}` | `{ok, removed}`；未知 queue_id 404 |
| POST | `/api/ui/sessions/{id}/rename` | `{title}` → `{session_id,title}` |
| POST | `/api/ui/sessions/{id}/archive` | `{archived?: bool}` → `{archived, detached, archived_session_id?, unarchived_session_id?, session_id?, session?}`；见 §7 的归档顺序契约 |
| POST | `/api/ui/sessions/{id}/permission` | `{preset}` → 会话权限 preset |
| GET | `/api/ui/approvals?session_id=` | `{approvals: Approval[]}`（可省略 session_id 查全部） |
| POST | `/api/ui/approvals/{approval_id}` | `{decision: "allow"\|"deny", scope?: "once"\|"session"}` → `{approval_id,settled}`；未知 404，非法 decision/scope 400 |
| POST | `/api/ui/commands` | `{name, args: string[], session_id?}` → `{status: ok\|error\|redirect, message, redirect?, data?}` |
| GET | `/api/ui/settings` | `SettingsPayload`（config/provider/agent/permissions/commands/plugins/sessions/web/about） |
| PATCH | `/api/ui/settings/agent` | `provider/model/temperature/max_tokens/max_steps` 的任意子集；响应带 `live`（见 §7） |
| PATCH | `/api/ui/settings/provider/{name}` | `base_url/api_key/api_key_env` 的任意子集；响应带 `live` |
| POST | `/api/ui/settings/providers/{name}/test` | `{}` → `{ok, detail, latency_ms}` |
| POST | `/api/ui/settings/permission` | `{preset}` → 新会话默认权限 preset；响应带 `live` |
| GET/PUT | `/api/ui/plugins/{id}/settings` | GET schema/values/revision；PUT `{values, expected_revision?}`，revision CAS |
| GET | `/api/ui/fs/dirs?path=` | 目录选择器数据源：`{path, parent, entries:[{name,path}]}`；只列目录、跳过隐藏项；相对路径/文件 400、不存在 404、不可读 409 |
| GET | `/api/ui/fs/roots` | `{roots:[{label,path}]}`：Home、Workspace 与最近会话 cwd，去重后最多 30 项 |

会话与消息的 wire 形状：

- `SessionSummary`：`session_id,title,cwd,turns,preset,created_at,updated_at,read_only,readable,active,busy,archived`。
- `Timeline`：`session_id,title,cwd,active,busy,pending,readable,archived,provider,model,preset,
  permission_preset,context_tokens,context_window,usage,messages,journal_seq?,state?`。
  `state` 与 `web/service.py:_state_facts` 同源（title/cwd/model/preset/permission_policy/
  sandbox_mode/plan_mode/rounds_started/goal/todos/deliverables/tasks/handoffs），active 与
  非 active 路径形状一致；计划条、进度条与详情抽屉都读它。
- `Approval`：`approval_id,session_id,tool,summary,detail,created_at,status`。
- 所有字段 snake_case；`messages` 为 native fold 出的消息列表（文本、reasoning、工具调用/结果等 part）。

### 4.2 legacy API 家族

`ui=False` 的 stdlib 宿主与兼容客户端继续使用，SPA 不依赖：

| 方法 | 路径 |
| --- | --- |
| GET | `/api/health` |
| GET/POST | `/api/sessions`（POST 201） |
| GET | `/api/sessions/{id}` |
| POST | `/api/sessions/{id}/resume` |
| GET | `/api/sessions/{id}/transcript` |
| POST | `/api/sessions/{id}/messages`（202） |
| POST | `/api/sessions/{id}/interrupt` |
| GET | `/api/sessions/{id}/events`（SSE，同 §5） |

### 4.3 错误形状

所有 JSON 错误：`{"error": {"code": "...", "message": "..."}}`，`cache-control: no-store`；
`405` 额外带 `allow` 头与 `error.allow` 列表。

| 状态 | code | 触发 |
| --- | --- | --- |
| 400 | `bad_request` | 参数/JSON/长度校验失败 |
| 401 | `unauthorized` | 缺 token/cookie，或 token 不匹配 |
| 403 | `forbidden` | Host/Origin/Sec-Fetch-Site 不是回环同源 |
| 404 | `not_found` | 未知 API 路由、未知会话、未知审批 id |
| 405 | `method_not_allowed` | 已注册路径用错方法 |
| 409 | `conflict` | 会话被其它进程锁定、插件 settings revision 冲突 |
| 413 | `payload_too_large` | body 超过 1 MiB |
| 429 | `too_many_requests` | 当前 turn 未结束且排队已达 8 条 |
| 500 | `internal_error` | 未预期异常 |
| 503 | `service_unavailable` | 宿主关闭中、缺少必需服务 |

## 5. SSE 与回放

订阅：`GET /api/ui/sessions/{session_id}/events?after=<seq>`（旧路径 `/api/sessions/{id}/events` 等价）。
EventSource 不能设置自定义头，所以首连 token 可用 `?token=` 查询参数，之后由 HttpOnly cookie 认证。

帧格式（`id: <seq>` + 单行 JSON `data:` + 空行）：

    id: 42
    data: {"type":"text","session_id":"...","text":"...","seq":42}

- `seq` 是进程内单调递增的事件序号；同一事件在 `data.seq` 与 SSE `id` 中都出现。
- `?after=N`：先 replay journal 中 `seq > N` 的事件，再转入实时流；journal 是内存输出 tap，
  容量 `4096` 条，不是会话历史。
- 没有事件时每 15 秒发一个 SSE 注释心跳（`:` 开头），保持连接与代理存活。
- 服务端关闭（`close()`）时所有流结束；客户端负责重连。
- 时间线是权威历史：`/timeline` 从 native facts 重建完整对话，重启后仍可读；journal 只覆盖
  本次进程内的实时输出。客户端先拉 timeline，再以 `timeline.journal_seq`（或本地
  `lastEventSeq`，取较大者）作为 `after` 订阅，避免重复渲染。

事件类型（`WebEvent`，均带 `session_id`）：

| type | 额外字段 | 含义 |
| --- | --- | --- |
| `session` | — | 会话绑定/状态变化 |
| `status` | `phase: thinking\|done\|interrupted` | turn 生命周期 |
| `step` | `turn, step` | 进入某一步 |
| `text` | `text` | 助手文本增量/片段 |
| `thinking` | `text` | reasoning 增量/片段 |
| `tool_call` | `call_id, name, arguments` | 工具调用开始（`arguments` 为 JSON 字符串） |
| `tool_result` | `call_id, is_error, text` | 工具结果 |
| `final` | `text` | 本轮最终回答 |
| `usage` | `input_tokens, output_tokens, cached_input_tokens` | token 用量 |
| `approval` | `approval_id, tool, summary, detail, status` | 审批请求/决策 |
| `error` | `message` | 可展示的错误 |

客户端流式语义（全部在浏览器侧 `web/src/lib/liveTurn.ts`，不改本节的 wire 契约）：

- `step` 启动一条新的浏览器侧 assistant 消息（带后端 `turn` 编号）；`text`/`thinking`/
  `tool_call` 增量只写入当前 step 的消息，同一 `step` 的重放事件被忽略。
- `final` 只结算当前 step（不回写之前 turn 的消息）；`status(done|interrupted)` 结算整轮；
  下一轮以 `step(turn,1)` 先结算上一轮，晚到的 `final` 不会改写旧消息。
- 消息上的 `streaming`/`turn_meta`/`turn`/`detached` 是浏览器专有字段，不会出现在
  服务端返回的 timeline 里；`detached` 标记没有本地用户锚点（composer 之外发起）的流式
  step，分组时不会并入已有 step 的组。后台刷新用 `reconcileTimeline()` 合并，timeline
  仍是唯一权威历史。
- 每个 turn 的推理、中间文本与工具执行收进同一个可折叠过程块。运行中默认折叠，
  header 显示 `Working… 12s · 1 tool`；结束后 header 变为 `Worked for 12s · 1 tool`
  （`Failed after …` / `Stopped after …`），同样保持折叠。点击 header 随时展开或收起，
  运行中也能展开查看已发生的步骤；答案正文始终展开显示，不参与折叠。属纯呈现逻辑，
  不改变 wire 契约。

计划进度来自 timeline 的顶层 `state`，与上述 per-step 消息分组相互独立。

顺序约定：一轮正常结束时 `final` → `usage` → `status(done)`；`finish_reason=length` 时在
`status(done)` 后补一条 `error`；被 interrupt 的 turn 产出部分 `final` 与
`status(interrupted)`，不产生 `error`。timeline 中只有 `tool_call` 没有真实结果的工具卡标
`pending=true`，不伪造结果。

### 5.1 浏览器重连状态机

`web/src/api.ts` 不使用 EventSource 的原生重试，而是自管重连（这样笔记本休眠/宿主重启后能
从正确 seq 续传）：

- 状态：`connecting` → `live`；出错且 `navigator.onLine` 为假 → `offline`，否则 `reconnecting`。
- 退避：`delay = min(10s, 800ms * 2^retry)`，连接成功（`onopen`）后 `retry` 归零。
- 每次重连都用当前 `lastSeq` 重新拼 `?after=`；收到消息时用 `message.lastEventId` 推进 `lastSeq`。
- 浏览器 `online` 事件会立即取消等待并重连；组件卸载时 `close()` 清理 timer/listener/连接。
- 无法解析的帧被忽略，不打断流。

## 6. 审批

- `Attach` 把 native `PermissionGate` 的 ask 回调接到 `ApprovalBroker`；每个待批请求有
  `approval_id`，发 `approval(pending)` 事件，turn 在 `ask()` 上等待。
- 浏览器 `GET /api/ui/approvals` 展示审批条；`POST /api/ui/approvals/{id}` 唤醒等待者：
  `allow` 继续执行工具，`deny` 让工具得到拒绝错误。
- `scope=once`（默认）只放行当前请求；`scope=session` 让 broker 按 `(session_id, tool)`
  记忆放行，之后同会话同工具的 `ask()` 直接返回 allow，不再弹审批。放行集合只在内存，
  `forget_session()`（detach 路径）与宿主重启都会清空；非法 decision/scope 返回 400。
- turn 结束/会话 detach/host close 时取消该会话的 pending 审批；超时 300 秒后 `EXPIRE`（fail closed）。
- `summary/detail` 来自 sandbox policy（目标路径、需要的权限）；等待中的工具卡保持 PENDING，
  不显示成“缺失结果”。

## 7. 命令与设置

- `web/ui/commands.py` 映射 25 个 core command：
  - 真实执行：`/new` `/resume` `/sessions` `/rename` `/archive` `/unarchive` `/permission`
    `/status` `/plan` `/goal` `/cwd` `/plugins` `/help`。
  - `/status` 的 `turns` 来自 native turn 计数，`goal rounds` 独立输出（`rounds_started / max_goal_rounds`），
    不再把 rounds 当 turns。
  - `/clear` 诚实拒绝：transcript 是持久会话日志且总会重放，提示用 `/new` 或侧栏新建；
    不再返回假成功。
  - `/cwd`：无参数只读当前目录；带路径等价于 `create_session(CreateSessionRequest(cwd=path))`，
    返回 `data.session_id` 供前端跳转。
  - 导航：`/settings`、`/panel`；只读查询 `/provider`、`/model`、`/preset` 返回当前值，带参数时
    跳设置并提示 provider/model 从下一 turn 生效（preset 仍只影响新会话）。
  - 诚实拒绝：`/compact`、`/jobs`、`/team`、`/thinking`、`/hooks` 返回 `status=error` + 原因；
    `/exit` 提示关闭标签页。
  - `/archive` 归档当前 active 会话时**先**创建替代 native 会话再归档；创建失败时旧会话保持 attached/unarchived，
    归档写失败返回 `status=error` 但仍带 `data.session_id` 供前端跟随替代会话。`data.session_id` 只用于导航；
    后台归档/取消归档用 `archived_session_id`/`unarchived_session_id`。`/unarchive` 对未归档会话是安全 no-op；
    取消归档当前打开且已归档的会话会重新 attach 并返回 `session_id`。
- 入口约定：侧栏 **New session**、`Ctrl/Cmd+N`、快速切换器的 New session 都打开同一个
  `NewSession` 弹窗；GUI 动作（模型菜单、权限菜单、目录选择器、会话行菜单）各自调对应的
  typed API，不拼接 slash 命令字符串；`/api/ui/commands` 仍是命令的唯一执行路径。
- 新建会话：弹窗由「Folder（目录选择器）+ Recent folders + 可选标题 + preset」组成；目录选择器
  调 `GET /api/ui/fs/roots` 与 `GET /api/ui/fs/dirs?path=`，只列目录、跳过隐藏项。空路径
  POST `{}`，否则 `{"cwd": path}`；服务端 `resolve_workspace` 的错误显示在弹窗内；成功后
  刷新列表并导航 `/c/<id>`。cwd 在会话创建时固定。preset 提交的是 `Preset.id`
  （`standard`/`ptc`/...），显示文本才用 `Preset.name`。
- 会话级模型切换：`POST /api/ui/sessions/{id}/model` 走 `_apply_selection`，重定向当前绑定
  的 provider/model；idle 返回 `applied=next_turn`，streaming 时返回 `deferred` 并在当前
  turn 结束后应用。只影响当前会话，不改 `config.toml`（全局默认仍走 PATCH /settings/agent）。
- 有界队列 UI：turn 未结束时发送返回 202 `queued=true`，前端以 `Queued · N` 条展示
  `GET /queue` 的结果，可 `DELETE /queue/{queue_id}` 单条撤回；interrupt 会丢弃整个队列。
- 新建会话是 **deferred native session**：header、system prompt、model/preset 选择和标题先只存在内存，不创建
  `sessions/<id>.jsonl` 或锁文件；首条 `user.message` 或 `turn.started` 才把 header + 缓冲事件原子写入
  （temp file + fsync + `os.replace`）。未对话就切换/关闭的 draft 直接丢弃，不落盘、不进入历史列表；只有
  active draft 会以 0 turns 出现在 `/api/ui/sessions` 与 timeline（来自内存投影）。归档未对话 draft 等同丢弃，
  返回 `data.discarded=true` 并启动替代会话。
- 会话行操作：行尾 `⋯` 菜单提供 **Rename**（`POST /rename`）与 **Archive / Unarchive**
  （`/archive` 命令）；双击标题也打开改名对话框。active 会话归档后跟随返回的
  `data.session_id` 导航。`Show archived` 切 `include_archived`，`Load more` 用
  `offset=已加载数量`；窄屏/触屏 `⋯` 常显。原始 session id 不再展示在 UI 里；排查时
  可从地址栏的 `/c/<id>` 读取。
- 设置：`ConfigEditor` 对 `~/.dpro/config.toml` 做文本级编辑（保留注释、顺序、未知插件表），
  `Config.model_validate` 校验后用临时文件 + fsync + `os.replace` 原子写入，保留编号 `.bak`；
  API key 只回报 `api_key_set`，绝不明文返回。
- 保存后的 live apply：agent / provider / permission 三类 PATCH 在写盘成功后调用
  `apply_settings_live()`，更新 `KernelConfig.value`、`LlmRegistry`（未变 provider 保留 transport）、
  `AgentsPlugin` 默认值、`PermissionPresetsService` 和 `WebSessionHost`；idle 会话立即重定向 binding，
  下一 turn 使用新 provider/model/temperature/budget。响应 additive 字段：
  `live:{applied, restart_required, deferred, detail}`；三者 `restart_required` 为空，streaming 时
  `deferred=true` 并在当前 turn 结束后应用。写盘/校验失败返回错误且不改内存；live apply 失败时
  文件保存仍成功，原因在 `live.detail`。Web host 端口等需要重启的字段不在此热更新范围。
- 插件设置来自 `SettingsPlugin/SettingsStore`：GET 返回 schema/values/revision；PUT 用
  `expected_revision` CAS；live 字段热生效，restart 字段标注；冲突 409、未知插件 404、
  空 values 400。

## 8. 安全模型

1. **回环绑定**：`validate_bind_host()` 只接受 `127.0.0.1`/`localhost` 并归一化；CLI 对其它值
   `parser.error`（退出码 2）；没有外部绑定开关。
2. **每进程随机 token**：`secrets.token_urlsafe(32)`，只出现在启动 banner 的 URL 中。
   首次页面请求 `?token=` 校验通过后 303 到去掉 token 的干净 URL，并设置
   `dpyro_web_<port>=<token>; Path=/; HttpOnly; SameSite=Strict`。cookie 名带端口是因为浏览器
   cookie 只按 host 隔离，避免同机其它 localhost 服务读取/覆盖。之后页面、assets、API、SSE 都接受
   cookie 或 `Authorization: Bearer <token>`（测试/CLI）。比较用 `hmac.compare_digest`。
   **API/assets 永不 303**；只有页面导航（非 `/api`、`/assets`、`/brand`）会重定向，避免预加载
   变成重定向循环。
3. **同源校验**：Host 必须是回环主机名 + 绑定端口；请求若带 Origin，必须是
   `http://127.0.0.1:<port>` 或 `http://localhost:<port>`；`Sec-Fetch-Site` 只允许
   `same-origin`/`none`。失败 403 `forbidden`。不发送任何 CORS 头。
4. **CSP 与安全头**：`SecurityHeadersMiddleware` 对每个 HTTP 响应移除上游 CSP 后统一写：
   ```
   default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self' ws: wss:;
   img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:;
   base-uri 'none'; form-action 'self'; frame-ancestors 'none'
   ```
   不用 `'unsafe-inline'`/`'unsafe-eval'`：React 的内联 style prop 走 CSSOM，CSP 不管辖；构建出的
   shell 没有内联 style 元素/属性。另带 `x-content-type-options: nosniff`、
   `referrer-policy: no-referrer`、`x-frame-options: DENY`、`cross-origin-opener-policy: same-origin`、
   `cross-origin-resource-policy: same-origin`。`/assets/*` 覆盖为 immutable 缓存，其它响应
   `cache-control: no-store`。
5. **XSS 安全渲染**：React 组件用文本节点渲染所有动态内容；Markdown 经 `react-markdown` +
   `remark-gfm`，不启用 raw HTML；没有任何 `dangerouslySetInnerHTML`/`innerHTML`/`eval` 使用；
   链接不导航到外部站点。
6. **输入边界**：JSON body 必须是对象；消息文本 1..100000 字符；session id 长度与字符集校验；
   body 上限 1 MiB → 413；当前 turn 未结束且队列满 8 条 → 429。
7. **凭据只写不读**：设置接口接受 provider API key 写入配置，但所有快照/日志只含
   `api_key_set` 布尔；错误信息与日志做过脱敏。
8. **纯 HTTP 是有意选择**：只绑回环，TLS 无信任收益；token 只用于换取 HttpOnly cookie，
   `referrer-policy: no-referrer` 防止外泄。

## 9. 生命周期与启动

```
python -m deeppyro.web --host 127.0.0.1 --port 0 --cwd PATH [--session ID] [--provider P] [--model M]
```

- `--port 0` 让 OS 分配空闲回环端口；`--host` 默认且只接受回环值；`--version` 打印版本后退出。
- 启动后终端打印 banner：版本、`URL: http://127.0.0.1:<port>/?token=<token>`、
  `Localhost only: bound to 127.0.0.1, never exposed on the network.`、`Session: <id>`、
  `Press Ctrl-C to stop.`。
- 无 `--session` 时创建新 native 会话；带 `--session` 时 resume 该会话；会话已被其它进程锁定则
  409，死进程留下的锁会自动回收。
- Ctrl-C 优雅停止：停止监听、关闭 SSE 连接、detach 会话、停止自举的 kernel；`launch()` 返回 130。
- TUI splash F1 让 `DeepPyroApp.run()` 返回 `"web"`，`tui/main.py` 调
  `deeppyro.web.launch.launch(...)`，本进程转为 Web 宿主；F2/Enter 进入终端 TUI。
- 程序化入口：

  ```python
  async def serve(*, cwd=None, session_id=None, provider=None, model=None,
                  port=0, host=LOCALHOST, banner=None, ui=True) -> None: ...
  def launch(**kwargs) -> int: ...  # Ctrl-C 130，正常 0
  ```

- 插件 seam：`[[plugins]] id="web" plugin="deeppyro.web.plugin:WebHostPlugin"`（`config={autostart=false,port=0}`）。
  `setup()` 永不监听；`start(port=0,cwd=...)` 启动；`stop()/unmount` 关闭。

## 10. 设计原则

1. **一切插件**：`WebHostPlugin provide="web"`，由 bundle/manifest 装配；`WebSurface/RouteRegistry`
   允许第三方注册路由；缺口是没有通用交互面契约，splash 直接 import `deeppyro.web.launch`。
2. **Pythonic**：request/response/event/command/approval 都是 frozen slotted dataclass；Protocol、
   StrEnum、TaskGroup/async generator 显式生命周期；JSON snake_case，camelCase 只存在于浏览器内部。
3. **AI 原生**：web 层不拥有会话状态；sessions 是 native append-only facts；turn 事件来自同一个
   `NativeAgentHost`/`AgentLoop` `AgentHooks`；timeline 回放再按 seq 订阅；usage/token/context 来自
   native taps；审批是显式事实 + 决策；interrupt 走 `NativeAgentHost.interrupt()`。进程内唯一状态：
   绑定、队列 8、journal 4096、审批 broker。

## 11. 已知限制

- 一个进程同时只绑定一个会话：多标签 resume 会移动绑定，旧标签的 send/interrupt 失效。
- 队列、审批、journal 都在内存：进程退出即丢失；历史只来自 native log。
- 无 TLS、单本地用户；token 出现在 URL/历史中一次，随后换成 HttpOnly cookie。
- team preset 在 web 运行时被 409 拒绝（仅 TUI/headless 支持）。
- 未 attach 的会话 usage 从 `~/.dpro/usage/usage.jsonl` 回填，context 取最后一次请求的
  input_tokens，不是实时值。
- 无附件上传、无压缩 UI、无多会话并行、无移动优先布局、无消息编辑/重新生成、无会话内
  全文搜索；无 exit 命令（关标签页或 Ctrl-C）。
- 审批 `scope=session` 的放行集合是内存态：detach 或宿主重启即失效；不会写入权限 preset。
- `/api/ui/fs/*` 只读目录列举（不创建/删除/重命名），且只在回环同源下可用。
- `ui=False` 的 stdlib 宿主仍存在，但没有 SPA shell/commands/settings/approvals；生产 launcher
  默认 `ui=True`。
- 浏览器需要 ES2020+、`EventSource`、`fetch`。

## 12. 测试与验证

`tests/web/` 覆盖：

| 文件 | 覆盖 |
| --- | --- |
| `test_server.py` / `test_ui_server.py` | UiServer 生命周期、SPA shell、API 404、缓存/安全头、缺构建 503 |
| `test_ui_auth.py` | token/cookie/303、Host/Origin、CSP |
| `test_frontend.py` | 提交的 static 构建产物与引用文件、Vite 配置、wheel 打包配置、CSP 契约、旧资源已删除 |
| `test_timeline.py` / `test_approvals.py` | timeline 回放、审批 broker（含 scope=session） |
| `test_ui_session_extras.py` | 会话级 model 切换、queue 列表/撤回、fs roots/dirs、timeline state |
| `test_ui_commands.py` / `test_ui_settings.py` | 命令执行、设置 CAS |
| `test_launch.py` | CLI/splash 启动路径 |

验证命令：

    .venv\Scripts\python.exe -m pytest tests/web -q
    .venv\Scripts\python.exe -m ruff check src/deeppyro/web tests/web
    .venv\Scripts\python.exe -m ruff format --check src/deeppyro/web tests/web
    cd web && pnpm typecheck && pnpm build
    uv build --wheel   # 另用 zipfile 检查 deeppyro/web/ui/static/index.html、static/assets/*、assets/*

构建产物缺失时 `test_frontend.py` 直接失败，提示先 `cd web && pnpm build`；wheel 内容由
`tests/web/test_frontend.py` 的打包配置断言与发布前手工检查共同保证。
