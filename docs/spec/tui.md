# DeepPyro TUI 规格（Textual 8.2.8）

状态：TUI 已完整实现（FIGlet 像素字标启动页、每 turn 一个统一 AgentProcess（思考行 + 工具行按时间
顺序融合、默认收起、点击展开）与全屏思考查看器、对话流、活动栏、多行输入与 slash 补全、选择器、审批、
插件管理、插件槽位、后台任务面板、团队面板与任务板、状态栏），1075 测试与 ruff 全绿；真实模型端到端
冒烟通过（启动页 → 任意键 → 思考/工具过程 → 回答；真机用 /policy 切到 read-only、/approval 切到 never、
/cwd 切换工作目录；插件贡献状态栏项/对话行/面板；jobs 与 team 服务由真实内核装配）。

## 1. 形态与范围

- 唯一产品形态：全屏终端 TUI（Textual，alt-screen）。不做 Web/Electron UI。
- 保留 headless 一次性模式（dpyro --headless / dpyro <prompt>，NDJSON 输出），不属于 TUI 渲染层。
- 终端能力：True Color、Markdown 渲染、鼠标可选（v1 以键盘为主）。
- 目标平台：Windows 优先（PowerShell/Windows Terminal），Linux/macOS 尽力兼容。

## 2. 模块布局（src/deeppyro/tui/）

| 文件 | 职责 | 所有者 |
| --- | --- | --- |
| main.py | argparse 入口 dpyro；--version/--headless/--json/--provider/--model/--preset/--cwd/--max-steps | Lead |
| app.py | DeepPyroApp：compose 布局、事件分发、turn 生命周期、快捷键、命令处理方法 | Lead |
| runtime.py | TuiRuntime：boot 内核服务（含 kernel/presets）、创建/切换会话、按 preset revision 重建 AgentSession、compact_now、close | Lead |
| sink.py | TuiSink：EventSink 实现，累计 usage/final/error 并转发 dict 事件 | Lead |
| format.py | 纯函数格式化（短 session id、token、上下文百分比、文本截断） | Lead |
| wordmark.py | FIGlet ansi_shadow 预渲染像素字标 + 逐字符渐变色（宽/窄两套） | Lead |
| commands.py | slash 命令定义与分发 | tui-dev |
| widgets/conversation.py | 对话流：user/assistant/tool 块 + 每 turn 一个 AgentProcess | Lead |
| widgets/agent_process.py | AgentProcess / ToolProcessLine：思考 step 行与工具行按时间顺序融合、默认收起、展开详情、点击 step 打开查看器 | Lead |
| screens/thinking.py | 全屏按 step 查看 reasoning | tui-dev |
| widgets/activity_bar.py | 对话区上方活动栏：plan / jobs / agents 三段 | ui-dev |
| screens/plan.py | 待办计划模态（/plan、点击 plan 段） | ui-dev |
| screens/jobs.py | 后台任务面板（/jobs、点击 jobs 段） | tui-dev |
| screens/hook_trace.py | 最近 hook 决策链模态（/hooks：hook/mode/ok/durationMs/error 或 preview） | tui-dev |
| screens/team.py | 团队面板与任务板（/team、/team board、/team on/off、点击 agents 段） | ui-dev |
| screens/thinking.py | 全屏思考查看器（/thinking、点击折叠行） | tui-dev |
| history.py | 从会话日志按 turn 重建 ReasoningStep/TurnReasoning/TurnProcess（思考 + 工具有序过程） | Lead |
| widgets/status_bar.py | 底部状态栏（session/model/tokens/ctx/policy/phase） | ui-dev |
| slots.py | 插件槽位注册表（STATUS/CONVERSATION/PANEL/SETTINGS，Textual-free） | ui-dev |
| widgets/plugin_slots.py | 插件状态栏项、插件面板宿主与表格 | ui-dev |
| screens/plugin_panel.py | 插件面板模态（/panel） | ui-dev |
| screens/path_prompt.py | /cwd 自定义工作目录路径模态 | Lead |
| widgets/prompt_input.py / prompt_area.py | 输入框（单行 → 多行） | tui-dev |
| widgets/completion.py / history.py | slash 补全、输入历史 | tui-dev |
| screens/approval.py | 审批模态 | tui-dev |
| screens/picker.py | 会话/模型选择器 | tui-dev |
| screens/plugins.py | 外部插件管理界面（数据来自 plugin_manager 服务） | plugin-dev |
| screens/splash.py | 启动 splash：逐行揭示、运行时信息回填、任意键关闭 | Lead |
| theme.tcss | 深色主题与布局样式 | ui-dev |

领域模块（session/llm/tools/agent/compact）与内核（kernel/、_vendor/cordis_port）不允许 import textual；
渲染层也不允许 import cordis——唯一装配点是 kernel.bundle.boot 与 TuiRuntime。

## 3. 启动流程

1. dpyro（无参数）→ main.main → DeepPyroApp(cwd/provider/model 可选)。
2. App.on_mount：先 push_screen(SplashScreen(...), callback=重新聚焦输入框)，再 await TuiRuntime.boot(sink=app.sink, ask=app.ask_approval)。
3. SplashScreen：内容按 40ms/行逐行揭示（宽 >= 72 且高 >= 24 用 22 行大字标，否则退化为单行字标），**揭示完成后一直停留，直到用户按任意键才进入主界面**（不自动关闭）；boot 完成后 App 调 set_runtime 回填 provider/model/cwd；boot 失败时立即关闭 splash 并显示错误。
4. TuiRuntime.boot：load_config → resolve_workspace → 校验 provider/model → kernel.boot（core bundle 39 行，首行 kernel 运行时）→ 取 llm.provider、tools.registry_for(workspace)、sandbox.gate(workspace, ask=ask)、activity/jobs/team/subagent 服务，并把 kernel/plugin_manager/inspect/settings/jobs/team/subagent 服务一并注入 AgentSession.services → presets.ensure_all() 物化全部 preset revision → 为 team 与 subagent 服务 configure + bind_session → new_session()。
5. new_session：preset_revision 清空 → SessionStore.create → _attach（agents.options(model=, context_window=) → agents.create(..., preset=, preset_revision=)）→ 写 model/selection（初始 provider/model）→ 写 agent-preset/selected（含 revision）→ AgentSession；open_session 先按日志最后一条 model/selection 恢复 provider/model（provider 不存在时忽略），再从 header 的 agentPreset 与日志里的 agent-preset/selected revision 恢复固定 revision，revision 已被删除时回退 latest。
6. 失败：conversation.add_error(failed to start: ...)，App 仍可退出。
7. App.on_unmount：await runtime.close()（kernel_app.stop 逆序释放插件）；幂等。

## 4. 事件流（单线程原则）

- AgentSession 在事件循环里同步 sink.emit(dict)；TuiSink.emit 先更新内部状态（usage/final/error 计数），再调用 on_event。
- on_event = App.handle_event：step → ConversationView.begin_step(turn, step)；thinking → stream_thinking（写入当前 AgentProcess 的 step 行）；text → stream_text；tool_call/tool_result → AgentProcess 内的工具行；usage → 上下文占用；final → finish_turn（收起当前 AgentProcess）；error → 先 finish_turn(None) 收起过程再红色错误块；status → 状态栏 phase。
- 每个事件处理后刷新活动栏（plan/jobs/agents 投影来自 activity 服务）；status 事件后还会应用插件对话行槽位。
- handle_event 整体 try/except：渲染异常不得中断 agent turn（只记录/忽略）。
- session 事件不由 AgentSession 发出；TUI 模式不产生该事件。
- 落盘顺序由 AgentSession 保证：先 append session 记录，再 emit 事件（widgets 观察到的事件总对应已落盘记录）。

## 5. 键位（v1）

| 键 | 行为 |
| --- | --- |
| Enter | 发送消息（补全浮层打开时选中补全） |
| Shift+Enter | 输入换行（多行输入，最多 8 行） |
| ↑ / ↓ | 输入历史（收到上一条/回到草稿）；补全打开时切换候选 |
| / | 行首触发 slash 命令补全 |
| Ctrl+C | turn 运行中：中断当前 turn；空闲且有输入：清空输入；空闲空输入 1.2 秒内双击：退出 |
| Ctrl+D | 退出 |
| Ctrl+P | 聚焦输入框（命令面板预留） |
| 任意键（splash 显示时） | 关闭启动画面进入对话（splash 不会自动关闭） |
| Esc | 关闭模态（审批/选择器/插件管理/插件面板/路径输入/计划/任务/团队/思考/命令选择器） |
| 点击 AgentProcess header | 展开/收起该 turn 的过程（思考行 + 工具行）；展开后点击 step 行打开 ThinkingScreen |
| 点击过程内的工具行 | 展开/收起该工具的完整 arguments 与 result |

## 6. Slash 命令

| 命令 | 说明 |
| --- | --- |
| /help 或 /? 或 /h | 帮助 |
| /new | 新建会话（保留当前 App 与 sink，重建 AgentSession） |
| /resume [id 或序号] | 恢复最近会话；无参打开选择器 |
| /provider [name] | 查看/切换 LLM provider；无参打开选择器（缺失 API key 会标注）；切换后重建 provider 与 AgentSession，写 model/selection 并持久化到 config [agent] |
| /model [id] | 切换模型；支持跨 provider 的 provider/model 形式；无参打开选择器（多 provider 时值为 provider/model）；写 model/selection 并持久化 |
| /preset [id] | 切换 agent preset（`standard` / `ptc` / `minimal` / `cordis`，按会话组合工具面、提示词、呈现模式与 agent mode）；无参打开选择器（英文 display name + id + presentation + 描述，当前项标记 current）。选择器与参数只列出可见 preset，`team` 隐藏；`/preset team` 会被拒绝并提示用 `/team on`。切换只在空闲时允许，不重启进程、历史连续 |
| /policy [preset] | 查看/切换权限预设（read-only / workspace-write / danger-full-access）；无参打开选择器；写 permission/preset 记录 |
| /approval [policy] | 查看/切换审批策略（ask / never / always）；无参打开选择器；写 approval/policy 记录 |
| /cwd [path] | 查看/切换工作目录；无参打开选择器（当前 + 最近会话目录 + 自定义路径模态）；写 working-directory/change 记录 |
| /status | 当前 provider（含 key 状态）与可用 providers、model、preset、policy、cwd、session、tokens、上下文 |
| /plan | 打开当前 todo 计划（PlanScreen，按 in_progress / pending / completed 分组） |
| /jobs | 打开后台任务面板（JobsPanelScreen：id/kind/status/label/started/finished） |
| /hooks | 最近 hook 决策链（HookTraceScreen：最近 50 条 hook/invoked + hook/result 配对，显示 hook、mode、ok、durationMs、error/preview） |
| /team [on|off|board] | 在当前会话进入/退出 Team 模式；`/team board` 打开完整任务板；无参打开团队面板（成员 + 任务） |
| /thinking [turns] | 打开全屏思考查看器：默认当前 turn，`all` 全部，数字指定最近 N 个 turn |
| /compact | 手动压缩（要求无运行中 turn；Compactor.compact） |
| /clear | 清空对话视图（不删除会话日志） |
| /plugins | 外部插件/ bundle 管理界面（PluginScreen，数据来自 plugin_manager 服务） |
| /panel [id] | 打开插件注册的面板（无参且只有一个时直接打开，多个时列出） |
| /exit、/quit、/q | 退出 |

未知命令不显示为错误、不提交给模型（返回 False，输入框恢复文本）。

## 7. 审批流

- PermissionGate(preset, approval, workspace, ask)：approval=ask 时工具流水线调用 ask(request)。
- App.ask_approval → await push_screen_wait(ApprovalScreen(request))：用户选择 once/always/deny。
- 选择 always 时调用 gate.set_approval(always)（本会话后续该 gate 全部自动允许）；once/deny 保持原策略。
- approval=never 时工具侧直接拒绝并返回 isError 结果，不弹模态。

## 8. 渲染约定

- 流式文本 60ms 节流（STREAM_REFRESH_SECONDS），避免每 token 重排。
- 流式阶段 Markdown 增量更新，结束后定稿；局部 Markdown 非法不得抛异常。
- agent 过程按用户 turn 组织：每个 turn 只有一个 AgentProcess，思考 step 行与工具行按发生顺序排列（同一过程，不分开）。运行中只显示一条滚动 header（当前 step 最新句，或正在执行的工具），明细 body 默认收起、点击 header 才展开；step 行只显示当前 step 的最新完整句（单行上限 120 字符，超长保留尾部）。
- turn 结束（final）后过程自动收起为一行摘要 “· agent process · N steps · M tools · K sentences  (click to view)”；点击 header 展开/收起，展开后点击 step 行打开 ThinkingScreen（按 Turn/Step 显示完整 reasoning），点击工具行展开该工具的完整 arguments/result；/thinking 打开同一查看器（可指定最近 N 个 turn 或 all）。
- reasoning 来源：AgentSession 把完整 ReasoningPart 写进 assistant/message，TUI 历史由 reasoning_map 从 durable 日志重建，不新增存储格式。
- splash 使用 wordmark.py 里预渲染的 ansi_shadow 字形：DEEPPYRO 火色渐变、HARNESS 冰蓝色渐变，逐行揭示；不依赖 pyfiglet 等运行时字体工具。
- 工具调用是过程的一部分：每个调用一行，运行中 “→ name 摘要”，完成 “✓ name 摘要 · 结果预览”，失败 “✗ name failed”（红色）；默认一行，点击展开完整参数与结果（20 行/2000 字符、40 行/4000 字符，write/edit 参数展开为 diff，能力上限见 tui-widgets.md）。
- 用户消息块左 accent 边框；系统 info/error 用 dim/红色静态行。
- provider/model 切换：/provider 重建 llm.client 与 AgentSession（同会话继续，不新建 session），/model 支持裸模型名或 provider/model；两者都写 model/selection 记录并调用 save_agent_selection 持久化到 config.toml 的 [agent]（写失败只提示，不打断切换）。
- 会话设置全部走 slash 命令，不再有顶部下拉：/policy 切权限预设、/approval 切审批策略、/cwd 切工作目录。切换立即作用于同一会话——sandbox gate 与工具注册表随工作目录更新，并写 permission/preset、approval/policy、working-directory/change 记录；turn 运行中拒绝切换并提示。无参命令打开 PickerScreen（当前值标记 current），/cwd 额外提供 Enter path… 选项打开 PathPromptScreen。
- 状态栏单行：session 短 id、provider/model、↑输入 ↓输出 ⚡缓存、ctx 百分比（>=75% 黄色）、policy（preset/approval）、phase。
  ctx 用最近一次模型请求的 inputTokens（`Session.context_tokens()`），不是会话累计；累计值只出现在 ↑。
- 插件状态栏项：状态栏上方另有一行 PluginStatusBar，插件通过 slots.add_status_item 注册的渲染器按 (order,id) 排序渲染；每次 refresh_status 重新求值，渲染异常显示为 dim 错误标记而不影响主状态栏。
- 活动栏：Header 与对话区之间一行 ActivityBar，数据来自 activity 服务对 todo/write、job/registered|settled、team/member、subagent/descriptor|catalog、plan/mode 的投影；空段隐藏，点击段打开 PlanScreen / JobsPanelScreen / TeamPanelScreen。
- 后台任务：JobsPanelScreen 只读展示 jobs 服务快照（超长输出由 job_output 工具按 head/tail 预览）；/jobs 与活动栏 jobs 段等价。
- Hook 决策链：/hooks 从当前会话日志取 hook/invoked 与 hook/result 记录并配对（优先 SessionQuery.hook_trace，退化到 records 过滤），显示最近 50 条；热 hook 不落盘所以不出现，approval/* 与 session 生命周期 hook 无 agent 同样不出现；无记录时显示 "no hook activity yet"。
- 团队：TeamPanelScreen 展示成员与任务，TaskBoardScreen 按 pending → in_progress → completed 排序；/team 无参打开团队面板，/team board 打开任务板；活动栏 agents 段打开团队面板。/team on 在当前会话进入 Team 模式（Lead 获得 `team_*`、收起 `subagent`），/team off 回到进入前的 preset；有 live 成员（spawning/running/idle）时拒绝离开并列出成员。default 模式且无成员/任务时 /team 提示用 /team on 显式开启 Team 模式。
- agent preset：可见的内置 preset 为 `standard`（默认，native 呈现）、`ptc`（PTC 呈现）、`minimal`（仅平台 shell + 极简提示词）、`cordis`（standard + 插件创作工具 + cordis 指南）；`team` 仍是 hidden，只由 /team on 进入。旧 id `default` 作为 `standard` 的别名继续可解析（会话 header/记录里的 legacy 值会在打开会话时归一）。欢迎页与 /status 显示 `agent preset Display Name (presentation · mode mode)`；状态栏标题在 team 模式追加 ` · team`。
- agent mode：default 模式注入 host-wide `subagent` 工具（一次性子会话委派）；team 模式只给 Lead 注入 `team_*` 工具并隐藏 `subagent`；Team 成员子会话两者都没有（防嵌套）。
- preset 的 `presentation` 决定模型直接看到的工具面：`native` 暴露全部工具，`ptc` 只暴露 `run_code` 并注入 Python SDK（见 ptc.md §4）。`minimal` 只注册平台 shell（Windows `pwsh`、其他平台 `bash`）、只渲染 minimal persona/tools/rules 三段提示词并关闭 runtime context。
- 插件对话行：handle_event 在 status 事件后调用 apply_conversation_slots，插件按 predicate 匹配事件并追加自定义行（只追加、宿主不重放历史）。
- 插件面板：/panel 从 slots 的 PANEL 槽位取工厂，打开 PluginPanelScreen（表格/自定义 widget 二选一）。
- agent preset：会话创建时把 preset id 写进 session header 的 agentPreset；之后每次创建/切换都向
  agent-preset/selected 记录写 `{preset, mode, revision?, previous?}`（revision 未固定时省略，team 模式
  额外记录进入前的 preset）。/resume 以最后一条记录为准（header 只作兜底），按记录固定 preset/revision
  重建 agent；team 记录还会恢复 previous，使之后 /team off 回到正确 preset。revision 已不存在时回退
  最新并重新记录。/preset 与 /team on/off 都物化目标 revision 并重建当前会话的 agent（当前会话 header
  不重写，只影响后续新建会话），且只在空闲时允许；切换失败会回滚 preset 字段与旧 agent。
  prompt 段与工具贡献按 preset scope + revision 过滤，preset 内声明的子插件在独立 realm 中物化。
  preset 的 `mode` 还决定委派工具集（见上条 agent mode）；AgentsService.create 会按 mode 做防御性
  过滤，即使显式传入 registry 也不会让 team 模式看到 `subagent`。

## 9. 开发与验证

- 单元/组件测试：pytest tests/tui，用 App.run_test(size=...) 无终端渲染；假 runtime 注入（runtime_factory）。
- 视觉预览：D:/dshworkspace/.dsh-tmp/tui_preview.py 与 splash_preview.py 以假 runtime 渲染并 export_screenshot(title=...) 出 SVG；
  再用 npx --yes sharp-cli -i x.svg -o x.png 转 PNG 自查（本机无 cairo/ImageMagick）。
- 截图脚本必须先 os.environ.pop("NO_COLOR", None) 并设 COLORTERM=truecolor，否则 Textual 导出全灰度 SVG（DSH 终端 env 默认 NO_COLOR=1）。
- 真实链路冒烟：D:/dshworkspace/.dsh-tmp/runtime_smoke.py（TuiRuntime.boot + agent.send，需本地 8000 推理服务）。
- 回归红线：不得再次出现 widget 方法名为 _render（Textual 私有名，会把 Widget._render 顶掉导致 render_strips 崩溃）。
- 所有 widget 刷新只允许在 Textual 事件循环线程内；后台线程只允许 put_message/post_message。
- 插件槽位测试：tests/tui/test_slots.py（注册表排序/去重/dispose、theme/locale）与 test_plugin_slots.py（状态栏、对话行、面板宿主）、test_ui_package.py（ui.toml 声明式包、坏 entry 隔离）；跨层集成在 tests/integration/test_plugin_services.py。
- preset/team 切换测试：tests/tui/test_runtime_presets.py（记录与 resume、legacy `default` 归一、team previous 恢复、原子切换与回滚、busy/隐藏 preset/live 成员守卫、revision 失效回退、canonical scope 的 registry）、tests/tui/test_app.py（四 preset 列表/选择、presentation 提示、`/preset team` 拒绝、`/team on/off`）、tests/integration/test_preset_modes.py（真实 bundle 的四种 preset 工具面与提示词）。
- provider/model 接线测试：tests/tui/test_runtime_models.py（set_provider/set_model、model/selection 记录、resume 恢复、_resolve_model 不跨 provider 泄漏、持久化失败兜底、team attach/shutdown）与 tests/test_config.py（save_agent_selection 注释保留/原子写/校验）。
- 活动/过程/任务/团队 UI 测试：tests/tui/test_activity_bar.py、test_plan_screen.py、test_agent_process.py、test_thinking_screen.py、test_jobs_screen.py、test_team_screen.py；对话过程接线在 tests/tui/test_conversation.py 与 test_app.py；history 重建在 tests/tui/test_history.py，activity 投影在 tests/builtin/test_activity.py。

## 9.1 思考过程的来源

TUI 只渲染 provider 返回的 reasoning 流（OpenAI-compatible 的 `reasoning_content` 字段）。本地 dshinfer 端点是否
返回思考由 `DSHINFER_ENABLE_THINKING` 控制（默认 `false`，即只出答案）；要看到逐句思考，用
`$env:DSHINFER_ENABLE_THINKING="true"; uv run uvicorn dshinfer.api:app --port 8000` 启动推理服务即可（本机真实冒烟即用此配置）。
`[agent] maxTokens` 默认 8192：思考、答案与工具调用参数共用这个输出预算。输出被截断（finish.kind=length）时，本轮尚未执行的 tool call 会被丢弃（避免日志里留下没有 tool/result 的孤儿调用），TUI 显示 error “response truncated at maxTokens=...; N tool call(s) were not executed”，turn 以 max-tokens 结束；此时需要提高 maxTokens 或把工作拆成更小步骤。

## 10. 已知偏离与待办

- 图片输入/粘贴：v1 不支持（session 层 image part 只读展示）。
- 会话恢复按 turn 重建统一的 AgentProcess（思考 step 行 + 工具行按顺序）并折叠成摘要，可点击展开/查看完整 reasoning；usage 明细不重放。
- 自动滚动固定在底部；用户上滚时不抢滚动（v1 以 call_after_refresh(scroll_end) 为主，后续精修）。
- 审批 always 粒度是 gate 级（本会话全局），未做 per-tool。
- 无 PTY；后台任务由 jobs 服务托管（shell run_in_background），不是持久交互 shell。
- 团队任务板只在内存中（重启清空），成员子会话没有专门的会话浏览器（可 /resume 打开）。
- Ctrl+C 语义依赖 Textual 绑定；真实终端下的双击退出阈值 1.2s。
- splash 是纯文本逐行揭示（无淡入/逐字动画），且依赖 Unicode 方块字符；极窄或极矮终端用单行字标。
- thinking 单行滚动不保留已滚过的句子，只有折叠后点击展开才能看到全文。
- /policy 与 /approval 切换不改写已生成的 system prompt（提示词里的沙箱说明仍是 turn 开始时的快照）；
  session header 的 cwd 保持会话创建值，切换只写 working-directory/change 记录。
- 切换工作目录要求当前无运行中 turn；/cwd 无参列表只含当前目录与最近会话目录，任意路径走路径输入模态。
- 插件设置页（SETTINGS 槽位）已注册但暂无宿主界面；设置写入仍是 next-boot 生效（volatile 字段除外）。
- 后台任务输出保存在内存中（模型侧 job_output 有界预览），进程重启后不恢复；headless 退出会 cancel_owner 取消本会话任务。
- 插件状态栏项在 refresh_status 时才刷新，插件内部状态变化需自行触发；插件对话行只追加。