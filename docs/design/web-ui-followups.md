# Web UI 后续事项（React SPA 迁移后）

状态：2026-11-20 本轮收口。NiceGUI 已移除，生产 Web UI 是 Vite + React + TypeScript SPA；
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
