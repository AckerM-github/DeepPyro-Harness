# React SPA Web UI（0.3.0 现行实现）

> **状态**：现行实现说明（非契约） · **层级**：设计说明 · **最后核对**：2026-11-20
> 功能契约见 [spec/web.md](../spec/web.md)；使用步骤见 [guides/web-ui.md](../guides/web-ui.md)。
> 本文记录 2026-11-20 从 NiceGUI 切到 React/Vite SPA 的架构与取舍，旧 NiceGUI 设计文档已删除。

## 决策

0.3.0 起浏览器层是 **React 19 + TypeScript + Vite 8** 单页应用，源码在 `web/`，构建产物
（`src/deeppyro/web/ui/static/`）提交进仓库并随 wheel 发布。Python 侧只保留一个
FastAPI/uvicorn 回环 ASGI 宿主：同源提供 shell、静态资产、JSON API 和 SSE。**没有 WebSocket**，
**没有 Python 页面元素树**，**没有 CDN/外部字体**。

切换原因：NiceGUI 让每个控件都要经 Python 端元素树往返，流式聊天、审批卡和设置表单的
交互粒度被迫跨进程；NiceGUI 还引入 `unsafe-inline`/`unsafe-eval` CSP 与 WebSocket 心跳
重连窗口。SPA 把交互留在浏览器内，Python 只暴露有类型的 JSON API/SSE，CSP 可以收紧到
`script-src 'self'`；交互既然不再跨进程，界面就按 conversation-first 工作台重排（语义化工具卡、
composer 宿主控件、抽屉式详情），而不是把终端界面的密度搬进浏览器。

被否决的替代方案：保留 NiceGUI 并只补构建（CSP/重连问题仍在）；服务端渲染 + 手写 vanilla JS
（0.1/P1 的维护债）；第二套独立前端服务（违反“一个宿主进程”与回环契约）。

## 结构

```
web/ (Vite/React 源码, 独立构建)
  index.html  src/main.tsx  src/App.tsx  src/api.ts  src/types.ts  src/styles.css
  src/components/{Sidebar,Chat,Composer,ToolCard,PermissionMenu,ModelMenu,ApprovalBar,
                 QueueDock,PlanBar,TeamBar,Inspector,Markdown,Palette,Settings,NewSession,
                 DirectoryPicker,Icon}.tsx
  src/components/{new-session,panels,toolcard,team}.css   ← 组件私有样式
  src/lib/{format,toolMeta,commandArgs,liveTurn}.ts
  src/lib/{commandArgs,liveTurn}.test.ts  src/api.sse.test.ts   ← vitest 单测
  public/brand/cyborg-mark.svg
        │  pnpm build (tsc --noEmit && vite build)
        ▼
src/deeppyro/web/ui/static/        ← 提交的构建产物（wheel 内同样存在）
  index.html  assets/index-<hash>.{js,css}  brand/cyborg-mark.svg
        │  由 UiServer 挂载/回退
        ▼
src/deeppyro/web/ui/server.py      FastAPI app + uvicorn（127.0.0.1）
  /            → static/index.html
  /c/{id} 等   → 同一 shell（前端路由）
  /assets/*    → static/assets（immutable 缓存）
  /brand/*     → ui/assets（no-store）
  /api/ui/*    → web/ui/api.py（JSON）
  /api/.../events → SSE（web/service.py + web/journal.py）
```

- `web/src/App.tsx` 持有 UI 状态（会话、时间线、连接态、面板开关）与键盘快捷键，通过
  `web/src/api.ts` 的 typed 方法调 API，并用 `subscribeEvents()` 订阅 SSE；SSE 事件不直接改
  服务端数据，而是交给 `web/src/lib/liveTurn.ts` 投影成浏览器侧时间线。
- `web/src/lib/liveTurn.ts` 是流式状态机：把增量 SSE 事件投影成「每个 backend step 一条
  assistant 消息」的浏览器时间线，后台刷新时再用 `reconcileTimeline()` 与 canonical timeline
  合并（保留本地 node id 与 turn 元数据）。`liveTurn.test.ts` 用 15 条用例锁定这些保证。
- `web/src/api.ts` 自己实现 EventSource 重连（指数退避、`after=` 游标、online/offline 状态），
  不使用浏览器原生重试，从而在宿主重启/休眠后从正确 seq 恢复。
- `web/src/types.ts` 是全量 wire 类型；后端字段保持 snake_case，只在组件内部用 camelCase。
- Python 侧没有任何 UI 框架依赖：FastAPI + uvicorn 是仅有的 web 运行时依赖（已在
  `pyproject.toml`），前端依赖不进入 Python 包。

## 会话渲染与流式状态机

会话列表只显示真正有内容的会话：New session 只是前端草稿，首条消息发送时才创建 native session；
历史零 turn 空日志在会话目录刷新时由 Web host 直接删除（见 `docs/spec/web.md` §7）。点击侧栏
会话只是**预览**（加载 timeline 并换 `/c/<id>` URL），必须在 composer 发送后才 attach 激活，
因此查看历史不会打断正在运行的会话。

转录区按 **conversation-first** 渲染：一条用户消息与它触发的若干 assistant step 组成一个
turn（`Chat.tsx` 的 `groupTurns()`）；每个 backend step 是**独立的一条** assistant 消息，
turn 内的 reasoning、工具调用与中间文本收进可折叠过程层，只把最后一个有文本的 step 当答案
露出，并在答案上提供 Copy / Try again（Try again 重发该 turn 的用户消息）。`role="tool"`
消息已 join 到 assistant 的 `tool_calls`，不再当正文重复渲染。

服务端只发增量事件（`step`/`text`/`thinking`/`tool_call`/`tool_result`/`final`/`status`/
`error`/`usage`），浏览器侧状态机在 `web/src/lib/liveTurn.ts` 里把它们投影成时间线：

- **乐观回显**：发送/重试时先把用户消息以本地 id 挂到时间线（`beginUserTurn()`），发送失败
  再撤回（`dropUserTurn()`）；canonical timeline 到达后由 `reconcileTimeline()` 合并成同一条
  消息。有排队消息时，若 SSE 的 `thinking`/`step` 先到且还没有乐观锚点，App 会先把队列
  首条提升为乐观用户消息（在 `applyLiveEvent()` 之前），避免「消息还没显示就开始出字」。
- **每个 step 一条消息**：`step` 事件开启当前 step 的 assistant 消息（带后端 `turn` 编号）；
  `text`/`thinking`/`tool_call` 增量只追加到这条当前消息；同一 step 内重放的 `step` 事件
  被忽略，消息不会重复追加。
- **final 只结算当前 step**：`final` 更新当前 step 的文本并结束它，不触碰之前 turn 的消息；
  `status(done|interrupted)` 结算整个 turn（settled/stopped），`error` 结算为 failed。
  没有 `final` 就进入下一轮时，`step(turn,1)` 会先结算上一 turn 再开新消息，因此晚到的
  `final` 永远不会改写旧消息——这正是多轮流式此前「上一答被改写」的根因。
- **刷新不换 node id**：后台 refresh 的去抖（180ms）结果不直接替换本地视图，而是按 role 与
  顺序合并（`reconcileTimeline()`）：保留本地 id、`turn_meta` 与仍在流式的消息，把服务端
  新增消息插到正确位置，并丢弃已被服务器持久化的本地副本；其它 session 的 timeline 直接忽略。
- **turn 分组**：`groupTurns()` 以用户消息为组边界；流式 step 的 `turn` 与当前组不符时
  （例如 composer 之外发起的 turn，canonical 用户消息还没到）先开一个匿名 assistant 组，
  等下一轮 timeline 刷新把用户消息接上。这类没有本地用户锚点的 step 带浏览器专有的
  `detached` 标记（`liveTurn.ts` 在 `userMessageId === null` 时写入）；由于每轮 turn 编号
  都从 1 开始，`groupTurns()` 在 `detached` 且当前组已有 step 时也会为它新开组，因此它
  不会被并进上一组，reconcile 合并时该标记同样保留。
- **过程折叠与运行状态**：每个 turn 的推理、中间文本与工具调用收进同一个可折叠过程块。
  运行中默认折叠（body 收起），header 显示 `Working… 12s · 1 tool`；结束后 header 变为
  `Worked for 12s · 1 tool`（失败/中断为 `Failed after …`/`Stopped after …`），同样保持折叠。
  点击 header 随时展开或收起，运行中也能展开查看已发生的步骤；答案正文不参与折叠，
  始终在过程块下方显示。计时以 `turn_meta.started_at` 起算，settle 后冻结在 `ended_at`。
- **贴底与 Jump to latest**：只有用户本来就在底部（距底 <96px）时新消息才自动跟随滚动；
  否则停在原位并显示悬浮的 Jump to latest，点击后平滑滚到底并恢复跟随。

`streaming`、`turn_meta`、`turn`、`detached` 是**浏览器专有字段**：由 `liveTurn.ts` 在客户端写入，
服务端返回的 timeline 与消息不会包含它们（wire 字段仍是 snake_case 的 native facts）。

## 构建与发布

- 开发：`cd web && pnpm install && pnpm dev`（5173 端口，`/api` 代理到
  `DEEPPYRO_WEB_ORIGIN`，默认 `http://127.0.0.1:8765`）。
- 发布：`cd web && pnpm install && pnpm build`；输出到 `src/deeppyro/web/ui/static/`，与源码一起
  提交。`emptyOutDir` 保证旧 hash 不残留。
- wheel：`[tool.hatch.build.targets.wheel] packages = ["src/deeppyro"]` 收录 `web/ui/static` 与
  `web/ui/assets`；`tests/web/test_frontend.py` 断言构建产物存在、shell 引用可解析、打包配置生效。
- 若未构建：`GET /` 返回 503 `Web UI is not built. Run pnpm build in web/.`，不会 500。
- PyInstaller 冻结包（`packaging/`）目前只收集 bundle/certifi/tzdata；**冻结发行版需要额外
  收集 `web/ui/static`**，这是已知打包 TODO，不影响 wheel 发行。

## 视觉语言

仍沿用 DSH 风格的暗色 token（`#151517` 基色、0.5px 发丝线、`#7aaaff` 只表示 live/链接/主动作），
但版式按 **conversation-first 桌面工作台** 组织，而不是 TUI 的三栏调试面板：

- 转录区是主体：用户消息右侧气泡，助手消息没有头像/名字/时间戳；推理与工具调用
  收进 turn 过程层——运行中与结束后都默认折叠成一行 header（`Working… 12s · 1 tool` /
  `Worked for 12s · 1 tool`，失败/中断为 `Failed after …`/`Stopped after …`），点击可展开。
  折叠只作用于工具与思考过程；答案正文始终展开显示，不会被折叠行遮住。
  工具卡按工具语义化（read 带行号、shell 用终端卡、edit 显示 Before/After、grep 按
  文件分组、web 显示来源链接），不展示 raw call id 或整段参数 JSON；错误先给结论句，
  原始细节收进折叠区。
- 右栏 **默认关闭**：会话详情（模型、上下文、usage、权限）放进覆盖式抽屉，由顶栏 info
  按钮打开；context 用量同时以一行文字常驻 composer 工具行。
- composer 是控制台而不是文本框：模型菜单、权限 preset、上下文用量都在这里；附件/命令
  之类的键盘教学 hint 不再出现。审批、排队消息、计划进度以条状卡接在 composer 上方，
  待处理事项不离开输入区。
- 侧栏只保留会话与设置；会话行菜单提供 Rename / Archive（双击标题也可改名）。命令面板降级为
  `Ctrl/Cmd+K` 快速切换器（只有输入以 `/` 开头才列命令），任何动作都必须先在 GUI 上可点到。
- agent team 是**会话级开关**，不是新建会话的 preset：composer 上方常驻一条 **Agent team**
  行。Off 时右侧是 `Enable`（会话未 attach 时点它才会 attach），On 时显示
  `N/M members · K running · d/t tasks` 与 live 点，点开 popover 查看成员状态
  （Starting/Working/Done…、model、last reply）与共享任务板（to do/in progress/done、owner），
  底部 `Turn off agent team` 在运行中或仍有 live 成员时禁用并说明原因。开关走
  `POST /api/ui/sessions/{id}/team`：enable 用隐藏 team preset 重建 native agent（team_* 工具、
  无 subagent 工具），disable 停止成员并恢复进入前的 preset（默认 standard），选择随
  `agent-preset/selected` 持久化，刷新/重开保留。数据来自
  `GET /api/ui/sessions/{id}/team`，SSE 结算后刷新，On 时每 2 秒轮询。成员/任务的创建与更新
  仍由 agent 通过 team 工具完成，浏览器侧不写 team 状态。
- 设置仍是覆盖层（不是独立路由）；窄屏（<768px）侧栏为抽屉，右栏详情全屏覆盖。所有图标是
  内联 SVG/CSS，不引外部图标库或字体。

## 已知取舍

- 构建产物入库意味着前端改动必须同时提交 `static/`，否则宿主仍服务旧 bundle；CI 后续可加
  “pnpm build 后 git diff 为空”的检查（当前未接）。
- 前端单测用 vitest（`pnpm test:unit`），当前覆盖 `lib/commandArgs.ts`、`lib/liveTurn.ts` 与
  `api.sse.test.ts`（共 29 条）；组件/E2E 测试仍是后续工作（见
  [web-ui-followups.md](web-ui-followups.md)）。
- 设置、审批、队列等状态仍在 Python 侧；SPA 只是视图，不存在第二份会话状态。

## 验证

    cd web && pnpm typecheck && pnpm build
    .venv\Scripts\python.exe -m pytest tests/web -q
    .venv\Scripts\python.exe -m ruff check src/deeppyro/web tests/web
    uv build --wheel   # 检查 wheel 内含 web/ui/static
