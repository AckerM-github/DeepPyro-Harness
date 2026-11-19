# TUI 入口

> **状态**：现行（0.3.0 native-only） · **层级**：TUI 规格 · **最后核对**：2026-11-19
> **关联契约**：[native/schema.md](../native/schema.md)（schema 1 会话）、[loop.md](loop.md)（AgentLoop）、
> [events.md](events.md)（总线目录）、[web.md](web.md)（Web 宿主）、[guides/web-ui.md](../guides/web-ui.md)
> **实现**：`src/deeppyro/tui/`

## 1. 产品形态与入口

整个产品是一个可执行文件 `dpyro`（python 入口 `deeppyro.tui.main:main`）：

- `dpyro`（无参数）→ 全屏 Textual TUI；先显示 splash，用户选择入口。
- `dpyro PROMPT` / `dpyro --headless` → headless 一次性运行（见 [headless.md](headless.md)），**不**进入 TUI。
- splash 的两个入口：
  - **F1 = Web**：本进程转为 loopback Web 宿主（`deeppyro.web.launch.launch`），打印带 token 的 URL；
  - **F2 / Enter / Escape = Terminal**：进入终端 TUI。
- Web 宿主只监听 `127.0.0.1`（无远程、无 TLS），细节见 [web.md](web.md)；不另做 Electron/桌面壳。
- 0.3.0 起 TUI 只驱动 native 运行时（`AgentLoop` + schema 1 会话）；旧 v4 TUI 路径与
  `DEEPPYRO_TUI_NATIVE` 开关已删除，没有第二套事件/会话实现。

## 2. 模块布局

| 路径 | 职责 |
|---|---|
| `main.py` | CLI 入口：参数解析、`plugin` 子命令分派、TUI/headless 分流、`--version`；`app.run()` 返回 `"web"` 时调用 Web launcher |
| `app.py`、`app_*.py` | Textual `DeepPyroApp` 与各关注点：`app_{approval,base,events,goal,models,panels,policy,schedule,sessions,status,support,turns,ui}.py` |
| `screens/` | 模态屏：`splash`、`approval`、`hook_trace`、`jobs`、`path_prompt`、`picker`、`plan`、`plugins`、`plugin_panel`、`question`、`slot_screen`、`team`、`thinking` |
| `widgets/` | 可复用控件：conversation、completion、history（输入历史）、prompt_area/prompt_input、status_bar、activity_bar、thinking、tool_card、plugin_slots、agent_process |
| `runtime.py`、`runtime_*.py` | TUI 运行时装配与状态：`runtime_{catalog,model,preset,schedule,schedule_host,services,session,state,status,support}.py`；启动内核与 native 会话 |
| `native_hooks.py` | `TuiAgentHooks`（typed AgentHooks → TUI 事件）与 `NativeHookRecorder`（观察 HookService） |
| `native_history.py` | 从 schema 1 会话日志 fold 出 TUI 会话视图（旧 `tui/history.py` 已删除） |
| `native_activity.py`、`native_facts.py`、`native_hook_trace.py` | 活动栏、native 事实投影（goal/team/subagent）、hook 决策轨迹 |
| `session_view.py`、`sink.py`、`slots.py` | 会话视图模型、TUI 事件 sink、插件 slot 契约 |
| `commands.py`、`format.py`、`health.py`、`ui.py`、`ui_package.py`、`wordmark.py`、`theme.tcss` | 命令解析/渲染、文本格式化、健康检查、UI 组件契约、splash 字标与主题 |

## 3. 启动流程

1. `tui/main.py` 解析 CLI 参数；`dpyro --version` 只打印版本。
2. `migrate_legacy_layout()`（`deeppyro.paths`）只做**配置文件目录布局**的兼容改名，与 v4 会话无关。
3. 非 headless：构造 `DeepPyroApp(cwd, provider, model, resume)` 并 `run()`。
4. splash 打开时运行时在后台装配：`boot_runtime`（与 headless 共用）加载配置、解析
   provider/model/preset、启动 kernel、装配领域服务与 tools/PermissionGate，然后由
   `sessions` 服务创建或附着 schema 1 native 会话。
5. `--resume SESSION_ID` 只对 TUI 生效：按 native session id 附着；headless 会提示并忽略。
6. splash 选择结果：`"terminal"` → 进入聊天界面；`"web"` → `tui/main.py:140-148` 调用
   `deeppyro.web.launch.launch(...)`（阻塞；Ctrl-C 返回 130），本进程成为 Web 宿主。

## 4. 事件流（typed AgentHooks → TUI）

TUI 不消费字符串事件的 loop 生命周期；它实现 `deeppyro.agent.events.AgentHooks`：

| AgentHooks 方法 | TUI 事件 |
|---|---|
| `run_started` | `status`（thinking） |
| `step_started` | `step` |
| `text_delta` | `text` |
| `reasoning_delta` | `thinking` |
| `tool_call_started` | `tool_call` |
| `tool_call_finished` | `tool_result` |
| `assistant_message`、`checkpoint_written`、`budget_exceeded`、`run_finished` | 不产生 TUI 事件（`run_finished` 只清除 active 标记） |

Turn 层（`runtime_support`/`runtime_session` 的 native 路径）行为：

- 发送：`host.send(text)`；收到 `CancelledError` → `final(last_assistant_text)` + `status interrupted`；
- `BudgetExceeded` / `ProviderStreamError` → `error(str)`；
- 成功 → `final(result.final_text)` + `usage` + `status done`；`finish_reason == "length"` 额外追加
  truncation error；
- TUI 事件上限 `DEFAULT_EVENT_LIMIT = 4096`。

**用量与上下文**：从 host 计数器读 usage；上下文 token 取最近一条 `ModelCallRecord` 的
inputTokens（经 `TraceExportSink` 的 tap）。没有 trace sink 时显示不可用，绝不伪造 0。

**预发送压缩**：turn 发送前调用 `NativeCompactService.pre_send_compact`，阈值
`context_window * 0.85`；期间 `status(phase="compacting")`；成功后**恰好一条** `compaction.done`
（覆盖被替换的消息 id、消息摘要），切点 tool-safe；失败报错后继续。native loop 自身**不会**
自动压缩；`/compact` 是手动入口。

## 5. Splash 与 Web 入口

`src/deeppyro/tui/screens/splash.py`：

- docstring：`F1 opens the loopback web host, F2 (or Enter/Escape) opens the terminal UI`；
- `_HINT = "F1 web · F2 terminal"`；`SplashScreen(ModalScreen[str | None])`；
- 绑定：`f1 → start_web`、`f2 → start_terminal`、`escape/enter → start_terminal`
  （f2/enter/escape 的按钮不常显）；
- 字标逐行揭示，`REVEAL_INTERVAL = 0.04`；小窗口用紧凑字标（`FULL_MIN_WIDTH = 72`、
  `FULL_MIN_HEIGHT = 24`）；
- `action_start_web` 以结果 `"web"` 关闭 splash；`tui/main.py` 随后把进程交给
  `deeppyro.web.launch.launch(open_browser=True)`（CLI 的 `python -m deeppyro.web` 与其
  共享 `serve()`）；无法拉起浏览器时只打印 URL。
- TUI 运行时装配异常时 splash 保持打开，`set_notice()` 在入口提示上方显示
  `failed to start: ...`；F1 仍可启动独立的 Web 宿主，避免终端 runtime 的失败连带
  封死 Web 入口。

Web 宿主是**宿主外壳**（`WebHostPlugin`，provide=`web`），不是第二个 agent；它由 launcher/宿主
直接构造，**不是** core bundle 的插件行。Web 运行时当前拒绝团队预设（返回 409），团队只在
TUI/headless 中可用；审批请求通过 `/api/ui/approvals` 与 SSE approval 事件完成（见
[web.md](web.md)）。

## 6. 键盘与斜杠命令

- `Ctrl+C` 中断当前 turn；`Ctrl+D` 退出。
- 斜杠命令由 commands 服务注册（`src/deeppyro/builtin/commands_builtin.py` 的 `_ROWS`），
  TUI 解析器从该服务读回，单一事实源：

| 命令 | 说明 |
|---|---|
| `/help`（`?`、`h`） | 显示帮助 |
| `/new` | 新建会话 |
| `/resume [id\|index\|text]` | 打开之前的会话（无参数则列出/筛选） |
| `/sessions [query\|--all]` | 列出会话（标题、时间、预览） |
| `/rename <title>` | 重命名当前会话 |
| `/archive` | 归档当前会话并新建一个 |
| `/unarchive [index\|id\|text]` | 恢复已归档会话 |
| `/provider [name]` | 查看/切换 LLM provider |
| `/model [id]` 或 `/model <provider>/<id>` | 查看/切换模型 |
| `/preset [id]` | 查看/切换 agent preset |
| `/permission [preset]` | 查看/切换权限预设（read-only / workspace-write / danger-full-access） |
| `/cwd [path]`（别名 `/workspace`） | 查看/切换 workspace |
| `/status`、`/plan` | 会话/运行时详情、当前 todo 计划 |
| `/goal [<objective>\|clear\|edit <objective>\|pause\|resume\|complete]` | 目标管理 |
| `/jobs`、`/team [on\|off\|board]`、`/thinking [turns]` | 后台任务、团队、完整推理 |
| `/hooks` | 最近 hook 决策（见 §9） |
| `/compact`、`/clear` | 立即压缩对话、清空视图 |
| `/plugins`、`/panel [id]`、`/settings [id]` | 插件列表、插件面板、插件设置页 |
| `/exit`（`quit`、`q`） | 退出 |

## 7. 审批

- 审批由 native `ApprovalService` / sandbox `PermissionGate` 驱动；approval policy 为 `ask` 或
  `never`，permission preset 决定可写范围。
- 需要审批的调用弹出审批屏（`screens/approval.py`）；`ask` 之外一律拒绝。
- 0.3.0 不再派发 `approval/asked`、`approval/decided` 总线事件（它们只是 hooks/types.py 中
  可导入一个 minor 的 `DEPRECATED_NAMES`）；审批决策由 ApprovalService 与 native 工具记录表达。

## 8. 会话命令的 native 语义

全部基于 schema 1 会话日志与 `session.fold`：

- `/sessions`：只列 native 会话（`sessions.native_list`）；`--all` 包含已归档。
- `/resume`：附着会话。锁被其它进程持有 → 只读 transcript 并提示
  `another process owns this session`；日志无法 fold → `this session log did not fold cleanly`；
  损坏的 jsonl 在列表阶段跳过，按 id 附着时报错。
- `/fork`：`SessionsPlugin.native_fork(source)` 复制当前会话（新会话 `parent_id=source`、
  `origin=fork`、`delegation_depth+1`；源会话无需可写），并切换到新会话。
- `/rename`：追加 `session/title` 事实。
- `/archive`：追加 `plugin.session.archived`（`{"archived": true, "source": "user"}`），随后新建会话。
- `/unarchive`：`sessions.native_archive(id, archived=False)`，只对非活跃会话生效。

旧 v4 会话不再可见/可读，也没有隐式迁移：schema 1 之外的文件会被拒绝（`SessionV4Error`）。

## 9. `/hooks` 与 HookService

`/hooks` 是 `NativeHookRecorder`（`tui/native_hooks.py`）对**同一个** HookService 的观察视图：
显示 hook 注册表与最近决策。native loop 只派发工具流水线（`tools/*`）与生命周期
（`session/created`、`session/disposed`）等已注册 hook；v4 的 turn 级 `agent/*` 记录器与
`HookJournal` 已删除，因此这些行显示为未记录（`native hook registry: not recorded`），不是 0 条决策。
schema 1 没有 hook journal，观察只在 boot 之后可用。

## 10. 团队 / 目标 / PTC / subagent

- `/team`：切到 team preset 并绑定 `NativeTeamManager`；roster 与任务板来自 native 会话里的
  `plugin.team.*` 事实；有活跃成员时 `/team off` 被拒绝；停止团队不写会话日志；
  冷启动用 `TeamService.restore()` 恢复。
- `/goal`：目标事实来自 native 会话；轮次驱动为 `NativeRoundDriver`（`goal.changed` 的
  `rounds_started`）。`/goal` 的 create/edit/pause/resume/complete/clear 都写事实，不写 v4 记录。
- PTC（`run_code`）：走 `ptc/native_adapter.py`，在工具上下文中暴露完整注册表。
- subagent：子会话 `origin=subagent`、`parent_id`、`delegation_depth+1`，事实为 `plugin.subagent.*`；
  native 运行时**拒绝嵌套委托**：`nested subagent delegation is not supported on the native runtime; a subagent cannot start another subagent`。
  团队/子代理的 usage 只进 trace，不写父会话的 usage 记录（TUI 显示 `usage not recorded`）。

## 11. 已知限制

- Web 运行时（splash F1）不支持 team 预设（409）；也没有审批 UI，需要审批的工具直接拒绝。
- 上下文用量指示依赖 `TraceExportSink`；没有 trace sink 时显示不可用。
- `/hooks` 仅 boot 之后可用，且只观察已注册的 native hook；不存在 v4 hook journal 视图。
- 任务/团队/子代理的 usage 只落 trace，不合并进父会话。

## 12. 开发与验证

- 测试：`pytest tests/tui`（39 个测试文件，含 `test_native_*` 系列：
  hooks/resume/runtime/team/hooks_ui/compaction/ptc/subagent/sessions/history/activity、
  `test_headless`、`test_splash`、`test_main_entry`、`test_runtime_native`、`test_native_only_guard`）。
- 界面快照：`pytest tests/tui/test_app_behavior_snapshot.py`。
- 改事件映射同时改 `native_hooks.py`、本文件 §4 与对应测试；不要重新引入 v4 记录器。
- 源码出处：`tui/main.py`（CLI/splash 分流）、`tui/screens/splash.py`（F1/F2）、
  `tui/native_hooks.py`（映射）、`builtin/commands_builtin.py`（命令表）。
