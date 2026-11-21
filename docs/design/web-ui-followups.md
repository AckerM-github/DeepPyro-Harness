# Web UI 后续事项（React SPA 迁移后）

状态：2026-11-21 本轮收口。NiceGUI 已移除，生产 Web UI 是 Vite + React + TypeScript SPA；
Python 仍负责 host/auth/API/SSE。下列 P0/P1/P2 为迁移评审时的清单，已完成项保留证据，
未完成项不再阻塞提交。评审原始证据在 `D:/dshworkspace/webui-review/`。

## 2026-11-20 第二轮：conversation-first 优化（已完成）

迁移评审后收到「Web UI 用起来像 TUI」的反馈，于是又做了一轮 P0/P1 改造，只动 `web/`
与 `src/deeppyro/web/`（TUI 未改）。核心变化：

- **转录区语义化**：新增 `web/src/components/ToolCard.tsx` 与 `web/src/lib/toolMeta.ts`，
  按工具名渲染 read/shell/write/edit/grep/glob/web/todo/present/goal/team/ask 等卡片；
  raw tool call id、整段参数 JSON、结果原文默认都不出现；推理与工具调用合并为折叠过程层。
  时间线的 `role="tool"` 消息不再当正文渲染（结果已 join 到 `tool_calls`）。
- **右栏默认关闭**：`Inspector` 改为覆盖式抽屉（模型/上下文/usage/权限），顶栏 info 按钮打开；
  context 用量改为 composer 下方常驻一行。
- **composer 成为控制台**：`ModelMenu`（会话级 `POST /sessions/{id}/model`）、
  `PermissionMenu`、上下文用量、Stop/发送；busy 时发送进入队列（`QueueDock`，后端原已支持
  202/queued）；审批在 composer 上方以 `ApprovalBar` 出现，新增 Allow for this session
  （`scope=session`）。附件死按钮与键盘 hint 已删除。
- **计划可见**：timeline 新增顶层 `state`（goal/todos/deliverables/tasks/handoffs），
  `PlanBar` 在 goal 进行中或还有未完成 todo 时显示，全部完成后不再常驻。
- **侧栏与入口**：删除 Commands/Plugins 一级导航与全部 kbd 提示；会话行菜单改为
  Rename/Archive（双击标题也可改名）；命令面板降级为 `Ctrl/Cmd+K` 快速切换器，命令只在
  输入以 `/` 开头时出现，且不再有参数步；命令结果作为 transcript notice 展示，不再只弹 toast；
  错误文案统一走 `humanError()`（网络/401/404/409/429 的人话映射）。
- **新建会话**：`NewSession` 重写为「目录选择器 + 最近目录 + 标题 + preset」，不再要求手打
  绝对路径；目录选择器用 `GET /api/ui/fs/roots|dirs`（只列目录、跳过隐藏项）。
- **新增后端端点**：timeline `state`、`POST /sessions/{id}/model`、`GET/DELETE
  /sessions/{id}/queue`、approval `scope=once|session`、`GET /api/ui/fs/{roots,dirs}`。
  这些是纯 Web 层扩展，不改 native/TUI 行为；`tests/web` 207 passed。
- **仍未做（P2）**：设置覆盖层还是旧视觉（8 分区信息架构未动）；附件上传、消息分叉/编辑、
  会话内搜索、虚拟列表、主题/字号设置、自动化浏览器测试入库。

注意：下面 P1 清单里的「命令面板参数（combobox/arg mode/inline 参数）」已被这一轮**取代**——
palette 不再承担带参命令输入，参数改由 GUI 对象（模型菜单、权限菜单、目录选择器）承担，
旧 arg mode 实现已删除；Sidebar 的 Commands/Plugins 一级导航也已移除，Plugins 分区仍可从
Settings 进入。以下内容保留为迁移轮的历史记录。

## 2026-11-20 第三轮：turn 分组与流式状态机（已完成）

第二轮之后实测多轮流式还有三个问题：上一轮回答会随着新一轮的增量被改写；流式期间用户
消息要等 canonical 刷新才出现；每个 delta 都触发布局/滚动跳动。根因是前端把 SSE 增量写进
同一条「当前 assistant 消息」，晚到的 `final` 会落到错误的消息上；后台刷新又整表替换
timeline，丢掉本地节点与滚动位置。

这一轮只动前端（`web/`；TUI 与 Python host/API/SSE 契约未改），把转录区改成 turn 分组 +
浏览器侧流式状态机：

- **新增 `web/src/lib/liveTurn.ts`（371 行）**：乐观用户回显（`beginUserTurn()`/
  `dropUserTurn()`）；每个 backend `step` 新开一条 assistant 消息，`text`/`thinking`/
  `tool_call` 只写当前 step；`final` 只结算当前 step（不回写历史消息）；
  `status(done|interrupted)` 结算整轮（settled/stopped），`error` 结算为 failed；
  `step(turn,1)` 先结算上一轮再开新消息，晚到的 `final` 无法改写旧消息。后台刷新用
  `reconcileTimeline()` 合并 canonical timeline，保留本地 id、`turn_meta` 与流式消息。
- **新增 `web/src/lib/liveTurn.test.ts`（15 条用例）**：锁定多 turn 不互相覆盖、tool step
  聚合为一条消息、同 step 重放忽略、失败/中断解锁 composer、composer 之外发起的 turn
  不重开已结算轮、发送失败撤回乐观消息，以及 reconciliation 的保 id、结算 running、
  追加缺失消息、排序、跨 session 忽略，和 notice 的错误样式。
- **App 接线**（`web/src/App.tsx`）：SSE 回调在 `applyLiveEvent()` 之前先做队列提升（排队
  turn 开始时立即回显队列首条）；后台刷新 180ms 去抖后经 `commitTimeline()` 合并；贴底
  跟随，距底超过 96px 时改为悬浮的 **Jump to latest**；发送失败撤回乐观消息并恢复
  composer。
- **Chat 渲染**（`web/src/components/Chat.tsx`）：`groupTurns()` 以用户消息分组、按 step 的
  `turn` 归属；过程层 `TurnProcess` 运行中与结束后都默认折叠，header 每秒计时
  `Working… 12s · 1 tool`，settle 后变为 `Worked for 12s · 1 tool`（失败/中断为
  `Failed after …`/`Stopped after …`），点击 header 可随时展开（运行中也可查看已发生的
  step/tool）；答案正文不折叠。推理流式时预览最后一行并扫光；答案提供 Copy / Try again。
- **类型与样式**：`web/src/types.ts` 增加浏览器专有字段 `streaming`、`turn_meta`
  （`TurnMeta` 的 started_at/ended_at/status）、`turn`，服务端 wire 不含它们；
  `web/src/styles.css` 新增第 23 节 turn grouping/live streaming（过程层 0fr↔1fr 折叠、
  running header 计时、jump-latest、reasoning sweep、移动端适配）。
- **文档**：`docs/design/react-web-ui.md` 新增「会话渲染与流式状态机」一节并更新文件图；
  `docs/spec/web.md` §5 补客户端流式语义（wire 契约不变、timeline 仍权威）。

验证：`pnpm typecheck` 干净；Vitest **29 passed**（`liveTurn` 15 + `commandArgs` 10 +
`api.sse` 4；第二轮时是 14）；`pnpm build` 成功并更新 `src/deeppyro/web/ui/static/`；
两次 CDP 驱动真实浏览器的端到端捕获验证本轮场景，控制台无 error。

已知边界：**composer 之外发起的 turn**（其它入口写入的用户消息，或排队 turn 早于
canonical 用户消息到达）在 timeline 刷新前会先显示成匿名 assistant 组，刷新后归位；
消息编辑/分叉、会话内搜索、虚拟列表仍是 P2；上一轮 P2 的其它遗留（设置覆盖层视觉、
附件上传、主题/字号、自动化浏览器测试入库、动态 import 与单包体积）本轮未动。

## 2026-11-21 第四轮：team 会话接入与设置 P2（已完成）

这一轮补两个遗留：Web 运行时此前对 team preset 直接 409 拒绝（只能用单 agent 会话），
设置覆盖层仍是迁移前的 admin console 视觉。改动只落在 `web/` 与 `src/deeppyro/web/`，
native/TUI 行为未改。

> 注：本轮「把 team preset 暴露成新建会话选项、bootstrap 不隐藏 team preset、/team 只读」
> 的做法已被第六轮推翻——team 改为会话级开关；detach 时对 subagent `close()` 的问题也由
> 第五轮修正。以下保留当时的实现记录。

### team 会话

- **后端**（`src/deeppyro/web/`）：删除 `_refuse_team`；attach 时按 TUI 同源语义接线
  `TeamService`/`SubagentService`（`configure()` + `bind_session()`/`bind_parent()`，
  best-effort），detach 时 `stop()`/`close()` 并清掉恢复标记，team preset 会话每 session id
  只 `restore()` 一次；新增 `GET /api/ui/sessions/{id}/team`（只读 `TeamSnapshot`）；
  bootstrap 的 `presets` 不再隐藏 team preset；`/team` 改为只读文本输出。team 工具的可见性
  仍由 `native_factory` 按 mode 过滤（team preset 有 team_* 工具、普通会话有 subagent 工具）。
- **前端**：新增 `web/src/components/TeamBar.tsx` + `team.css`，在 composer 上方显示折叠条
  （`Agent team`、`N/M members · K running · d/t tasks`，有运行中成员/未完成任务时 live 点），
  popover 展示成员（phase、model、last reply/error）与共享任务板（状态排序、owner）；
  `App.tsx` 在 SSE settle/审批事件后刷新，并对 team preset 会话每 2 秒轮询（未 attach 的预览态也轮询，
  这样另一客户端 attach 后成员/任务会出现）。
- **预览态（2026-11-21 修正）**：未 attach 的 team 会话只显示 `Agent team · Preview`，发送首条消息
  attach 后才加载成员/任务。注意 timeline 的 `state.preset` 是 `{preset, mode, revision}` 对象、
  `state.model` 是 `{provider, model}` 对象，前端必须读 `mode`/`preset` 字段（`isTeamPreset`），
  不能与字符串比较——之前写成 `state.preset === 'team'` 导致预览中的 team 会话不渲染 TeamBar。
- **已知剩余**：TeamBar 只读（成员/任务仍由 agent 通过 team 工具写）；team 成员的子会话
  （`origin=subagent`）已从会话列表过滤；没有成员详情页/中断按钮。

### 设置覆盖层 P2

- `web/src/components/Settings.tsx` 重组为「分组导航 + 分区标题/说明」：Model（Providers、
  Agent）、Workspace（Permissions、Sessions）、Advanced（Commands、Plugins、Web runtime）、
  About；Providers 页改为 `Default model` 卡（provider/model + Save default）加每个 provider
  的连接卡（base URL、API key env、状态徽标、Test、保存）；Permissions 页改为可点选的
  read-only / workspace-write / danger-full-access 卡片。运行期控制（模型、权限 preset）仍只
  在 composer，设置只放默认值。
- `web/src/styles.css` 重写设置段样式（分组导航、面板 800px 级宽度、发丝线卡片、choice 卡、
  移动端单列），与主 shell 共用同一套 token；仍是覆盖层而非独立路由。
- **仍未做**：Appearance（主题/字号/密度）没有设置项；Commands/Plugins 仍留在 Advanced；
  设置没有搜索；JS 单包体积与动态 import 未处理。

### 验证

- `pytest tests/web` 220 passed（新增 `tests/web/test_ui_team.py` 13 条覆盖 team 接线、
  `/team`、bootstrap preset、detach 释放）；`ruff check`/`format --check` 干净。
- `pnpm typecheck` 干净；Vitest 29 passed；`pnpm build` 成功更新 `src/deeppyro/web/ui/static/`。
- CDP 驱动真实宿主验证：team preset 会话里 `team_spawn`/`team_wait` 与
  `team_task_create/update/list` 两轮，TeamBar 从空态到 `1/8 members · 1 running`、
  `1/8 members · 1/2 tasks`，popover 成员/任务正确；设置 Ctrl+, 打开各分区截图；移动端
  390x844 下 bar/popover 正常。控制台无 error。

## 2026-11-21 第五轮：会话生命周期与空会话清理（已完成）

用户报告四个会话生命周期问题，这一轮只动 `web/`、`src/deeppyro/web/` 与 session 控制面
（TUI 未改）：

- **点击只预览、发送才激活**：`App.tsx` 的 `openSession()` 只加载 timeline 并换
  `/c/<id>` URL，不再自动 `POST /resume`（此前点一下就会 detach 当前会话、打断运行中的
  turn）；首次发送时后端 `send_message` 发现目标未 attach 会先 `resume_session()` 再入队。
  SSE 等 timeline 首次落地（`readySessionId`）后才订阅，避免历史 journal 被重放成重复消息；
  预览态 composer 可直接输入，`Chat` 删掉 "This session is read-only" 横幅与 Resume 按钮。
- **新建会话改纯前端草稿**：New session 对话框的 Start session 不再立刻建会话，只记录
  cwd/title/preset；首条消息发送时才 `POST /api/ui/sessions` 创建（native deferred session），
  未使用的草稿不落盘、不进入会话列表。
- **native agent 启动失败**：detach 时 `_release_tool_services()` 对进程级
  `NativeSubagentService` 调 `close()` 会把 `_closed` 永久置 True，之后所有 subagent/team
  调用都报 "native subagent service is closed"，直到重启宿主。改为 `cancel_all()` +
  `bind_session(None)` + `bind_parent(None)`，不再 close；team 仍用 `stop()`（其
  `bind_session` 会重置恢复标记）。另修 web 把 provider 字符串传给 subagent 管理器导致的
  "native subagent service requires a provider"：attach 时用 `services.llm.native_provider(name)`
  解析成 provider 对象。
- **空会话直接删除**（用户要求「空会话没有价值，直接删除」）：session 层新增锁安全的
  `delete_session_log()`（`src/deeppyro/session/schema.py`）、`delete_native_session()`
  （`session/native_query.py`）、`NativeSessionControl.delete()`（`session/native_control.py`）
  与 `SessionsPlugin.native_delete()`（`builtin/sessions.py`）；web `list_sessions()` 对
  非 attached、`turns==0` 且日志里既无 `user.message` 也无 `turn.*` 的会话物理删除，
  锁被其它进程占用时跳过、含 user message 的 crashed send 只隐藏，`_detach` 时也清掉
  从未使用的 draft。team/subagent 子会话（`origin=subagent`）同时从列表过滤。
  用户真实 home 已清掉 91 个历史空会话文件。

验证：`pytest tests/web` 225 passed（生命周期修复后）；`pytest tests/web tests/session`
377 passed（删除空会话后，新增 `tests/session/test_native_control.py` 2 条、
`tests/web/test_ui_api.py` 3 条、`tests/web/test_ui_team.py` 1 条）；`ruff check`/
`format --check` 干净；`pnpm typecheck` 干净、Vitest 29 passed、`pnpm build` 更新
`src/deeppyro/web/ui/static/`；CDP 驱动真实宿主验证点击预览不切换 active、预览态发送后
激活并回复、草稿首次发送才建会话、detach 前后 subagent 均成功、镜像目录空会话被删。

## 2026-11-21 第六轮：agent team 改为会话级开关（已完成）

第四轮把隐藏的 team preset 暴露成新建会话选项，用户指出方向错了：「team 应该是在 session
内部开启的，preset 只针对 agent 预设」。这一轮把 team 从「选 preset 建会话」改成「每个会话
常驻的开关」，只动 `web/` 与 `src/deeppyro/web/`，TUI 仍是会话内 `/team on|off`（未改）。

- **后端**（`src/deeppyro/web/`）：`bootstrap` 的 `presets` 恢复为只列非隐藏 preset
  （team 不再出现在 New session 里）；`SessionBinding` 新增 `team_previous_preset` 记住
  进入前的 agent preset；新增 `WebSessionHost.set_session_team(session_id, enabled)` 与
  `_rebuild_agent()`——用隐藏 team preset 重建同一个 native session 的 host（不 aclose 旧
  host，否则会关掉共享 session，与 TUI `_attach` 同规则），切换后调用
  `_configure_tool_services()` 重新绑定 TeamService/SubagentService，team 模式再
  `_restore_team()`；关闭时先检查 live 成员（spawning/running/idle）再 `team.stop()`，
  恢复进入前 preset（默认 standard）。选择通过既有 `agent-preset/selected` 事件持久化，
  resume 时由 fold 的 `state.preset` 读回。新增 `POST /api/ui/sessions/{id}/team`
  （body `{enabled: bool}`）；运行中/有排队消息或仍有 live 成员时 409，非法 body 400。
  `/team` 命令改为 `on|off|status`，无参输出摘要，不再返回「请用 Team preset」的提示。
- **前端**：`TeamBar` 从「只读状态条」改成常驻开关行——Off 显示 `Enable`，On 显示
  `N/M members · K running · d/t tasks` 与「Turn off agent team」（运行中或仍有 live 成员
  时禁用并在 title/note 里说明原因，pending 显示 `Switching…`）；`api.setSessionTeam()`
  调用新端点，`App.tsx` 用 `teamBusy` 处理 pending，成功后用返回快照更新并重新加载
  timeline；轮询条件从「team preset 会话」改为「team 已开启」（timeline 的
  `state.preset.mode === 'team'`）每 2 秒刷新成员/任务。
- **验证**：`pytest tests/web` 约 238 passed（`test_ui_team.py` 新增开关/幂等/busy 拒绝/
  live 成员拒绝/恢复 preset/预览会话自动 attach/POST 校验等 7 条，并改掉 bootstrap 与
  `/team` 的旧断言）；`ruff check`/`format --check` 干净；`pnpm typecheck` 干净、
  Vitest 29 passed、`pnpm build` 更新 `src/deeppyro/web/ui/static/`。CDP 真实宿主验证：
  新建会话 preset 列表无 team；普通会话点 Enable 后 team 工具可用、bar 出现成员/任务、
  `/team status` 正确；刷新后保持 On；`Turn off` 后 subagent 恢复可用；运行中或仍有
  running 成员时 Turn off 被禁用；控制台无 error。
- **已知剩余**：TeamBar 仍只读（不能操作成员/任务，写操作由 agent 用 team 工具完成）；
  切换在运行中/排队/live 成员时被拒绝，没有强制中断入口；团队仍受单 attached session 约束。

## P0 — 已完成

1. **文档改为 React SPA**
   - `docs/spec/web.md` 重写 routes/static/auth/CSP/cache/SSE/approval/commands/settings/lifecycle。
   - 新增 `docs/design/react-web-ui.md`；删除 `docs/design/nicegui-web-ui.md`。
   - 根 `README.md` 增加 `cd web && pnpm install && pnpm build` 与产物位置说明。
2. **静态产物进 wheel 已验证**
   - `uv build --wheel` 生成的 0.3.0 wheel 含 361 entries，包含
     `deeppyro/web/ui/static/index.html`、`static/assets/*.js|*.css`、`assets/cyborg-mark.svg`。
   - `tests/web/test_frontend.py` 改为 SPA/packaging guard。
3. **移动端会话抽屉**
   - `<=767px` 侧栏为 overlay drawer：backdrop、scrim、ESC、focus trap、打开时 focus 搜索框、
     路由/选择会话自动关闭；`768–1023px` 才是 icon rail。
   - 390x844 真实浏览器验证：drawer `mobile-open`、scrim、`body.drawer-open`、focus “Find a session”、
     Escape 关闭；截图 `D:/dshworkspace/e2e-mobile-focus.png`、`e2e-mobile-drawer.png`。
4. **设置页首屏慢**
   - `session/native_index.py` 增加 per-file metadata cache（mtime_ns+size）与 path cache；
     `SessionsPlugin` 增加 2s 全列表 TTL cache，warm list 只 stat，不再 parse 全部 JSONL。
   - warm `/api/ui/settings` 0.7ms median（max 1.6ms），端点到端点 0.6ms；冷进程首次仍约 0.5s
     （每进程一次全 fold），已记录为已知边界。
   - 前端 Settings 首开 skeleton + 8s 超时/Retry。
5. **设置保存真实 E2E**
   - agent/provider/permission 的 PATCH/POST 写盘后调用 `apply_settings_live()`，响应带
     `live:{applied, restart_required, deferred, detail}`；idle 会话立即重定向，下一 turn 生效；
     streaming 时 deferred。
   - 隔离 home 验证：`deepseek-flash` → `deepseek-chat`，下一 turn 返回 `LIVE APPLY OK`，随后回滚；
     `/api/ui/settings/agent` 响应 `applied=[config,llm,agents,session]`。SPA toast 会提示 deferred /
     restart required / live apply failed。
6. **全量 pytest 与 CI 基线**
   - 合并工作树 `pytest -q` exit 0；`pytest tests/web -q` 172 passed；前端 Vitest 14 passed。
   - 唯一历史 flake：`tests/plugins/test_settings_lock.py::test_concurrent_processes_do_not_lose_updates`
     在 Windows 多进程临时文件场景偶发，隔离重跑 10 passed。

## P1 — 已完成或已闭环

- **会话分页/搜索/归档**：`GET /api/ui/sessions?limit&offset&query&include_archived`，默认 50、
  上限 200，返回 `total/limit/offset/has_more/query`；Sidebar 支持 Load more、Show archived、
  Archived 标记。真实浏览器验证 50→100 items、archived 1。
- **SSE 断线重连**：`subscribeEvents` 自管重连，`after=lastSeq` 续传，800ms 指数退避上限 10s；
  `offline` 事件立即关闭流并显示 Offline，`online` 立即重连。Vitest 4 条覆盖；真实浏览器
  offline 9s → Offline，恢复后 1s → Live，并收到恢复后新消息（`e2e-reconnect.png`）。
- **命令面板参数**：combobox/listbox、键盘 ↑↓/Enter/Esc、带参命令 arg mode、缺参提示、
  inline `/cmd text`、Windows 引号路径；真实键盘 CDP 63/63，Vitest 10 条。
- **审批与 interrupt**：后端测试覆盖多审批排队/乱序结算/300s 过期/allow-deny 幂等；
  interrupt 不再丢已接受队列，中断 await 期间接受的消息会重启 worker。
- **`/clear` 与 `/status`**：`/clear` 诚实拒绝（transcript 是持久日志，提示 `/new`）；
  `/status` 分开输出 native turns 与 goal rounds。
- **设置分区**：Provider/Agent/Permissions/Commands/Plugins/Sessions/Web/About 八个分区；
  Sidebar 的 Plugins 导航现在直接打开 Plugins 分区。
- **死控件巡检**：Sessions 导航聚焦搜索、Manage access → Permissions、Plugins → Plugins；
  保留的快捷入口均有行为。
- **旧资源清理**：删除 `ui/assets/{style,settings,chat}.css` 与 Inter 字体；`test_frontend.py` 已重写。
- **前端测试**：Vitest 覆盖 commandArgs 与 SSE 重连（14 tests）；浏览器 E2E 仍以 CDP 脚本手工执行，
  未把 Playwright 加入依赖。

## P2 — 已完成 / 部分完成

- **可访问性**：drawer focus trap、Escape、aria-current、tool/reasoning `aria-expanded`、
  chat `role=log` + `aria-live` 状态、approval group label；尚无完整 axe/键盘审计。
- **性能**：Inspector 用 `sameInspector` memo，流式时忽略 messages 变化；聊天消息与工具卡 memo。
  transcript 仍是 React 受控重绘，未做虚拟列表；贴底自动跟随与 Jump to latest 已在第三轮实现。
- **缓存**：`/assets/*` immutable 1 年，shell 与 `/brand` no-store。
- **安全**：CSP `script-src/style-src 'self'` 无 unsafe-inline/eval；Markdown 禁用 form/button/style/
  iframe 等并可跳过 raw HTML；cookie 按端口命名、303 去 token；mutation 同源校验。
- **仍未做**：JS 单包约 471KB（gzip 140KB），未做 Settings/Palette 动态 import；版本仍 0.3.0；
  PyInstaller frozen 构建未收集 `web/ui/static`（wheel 已正确，文档已记 caveat）；
  TUI 未接 live apply；自动化浏览器测试未入库。
