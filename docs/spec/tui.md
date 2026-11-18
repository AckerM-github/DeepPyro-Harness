# DeepPyro TUI 规格（Textual 8.2.8）

> **状态**：现行（过渡：v4 待移除） · **层级**：TUI 功能规格 · **最后核对**：2026-11-18
>
> 交互 TUI 已完整实现（FIGlet 启动页、每 turn 一个统一 AgentProcess、全屏思考查看器、对话流、活动栏、
> 多行输入与 slash 补全、选择器、审批、插件管理/槽位、后台任务、团队面板与任务板、状态栏）；全量测试
> 全量 pytest 通过、ruff 全绿。**TUI 交互路径仍走 v4**（0.2.0 未迁移）；headless 一次性模式已默认 native。
> TUI 仍只写 v4，但 `/sessions` 与 `/resume` 已能**只读查看** native（schema 1）会话：列表把 v4 与 native
> 按最近时间合并，native 行标注 `native · read-only`；`/resume` 对 native id 只渲染转录（标题、工作目录、
> turn/事件数、user/assistant/tool 消息与工具名）并提示迁移，不接管、不替换当前 live 会话，也不改动 turn 引擎；
> 要继续 native 会话先执行 `dpyro migrate --to v4 --session <ID>`。native 会话服务层已到模块级：
> `session/native_query.py` 提供有界查询/搜索/过滤/排序、重命名（`session.title`）与归档
> （`plugin.session.archived` 插件事件，schema 1 无归档事件类型）、`session/native_fork.py` 提供全量快照 fork，
> `builtin/session_title.py` 新增 native 模式（可直接为 NativeSession 命名；原生 loop 发出 AGENT_TURN_STOPPING 后自动生效），
> `InspectPlugin.native_session`
> 可输出 header/事件计数/run/turn/tool/trace 链接报告；带归档标记的 native 会话默认不出现在 `/sessions` 列表
> （`list_native_sessions(include_archived=True)` 才列出）。本波只落到模块与只读视图，TUI 接线（搜索/过滤/重命名/
> 归档/fork 入口）属下一波。
>
> **native TUI 路径（0.2.x 过渡，flag 门控）**：设置 `DEEPPYRO_TUI_NATIVE=1`（也接受 `true`/`yes`/`on`）时，
> TUI 的 runtime 改由 schema 1 control plane 装配（SessionsPlugin native API + `AgentsPlugin.host` +
> `NativeAgentFactory`/`PresetPromptSource`），并支持 `--resume SESSION_ID`、native `/sessions`/`/resume`/
> `/fork`、native rename/archive、native `/team on|off`（roster/任务板/queued-delivered 状态/冷恢复）、native
> `/hooks`（同一 HookService 的 registry + 最近决策）、发送前自动 compaction、PTC preset 与 native subagent
> 面板；未设置该变量时走原 v4 路径且行为逐字节不变。仍显式失败的 native 能力见 §11.5。详见 §11。

## 1. 形态与范围

- 唯一产品形态：全屏终端 TUI（Textual，alt-screen）。不做 Web/Electron UI。
- 保留 headless 一次性模式（dpyro --headless / dpyro <prompt>，NDJSON 输出），不属于 TUI 渲染层。
- 终端能力：True Color、Markdown 渲染、鼠标可选（v1 以键盘为主）。
- 目标平台：Windows 优先（PowerShell/Windows Terminal），Linux/macOS 尽力兼容。

## 2. 模块布局（src/deeppyro/tui/）

| 文件 | 职责 | 所有者 |
| --- | --- | --- |
| main.py | argparse 入口 dpyro；--version/--headless/--json/--provider/--model/--preset/--permission-mode/--approval-policy/--cwd/--max-steps | Lead |
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
| widgets/status_bar.py | 底部状态栏（session/model/tokens/ctx/permission/phase） | ui-dev |
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

领域模块（session/llm/tools/agent/compact）与内核（kernel/）不允许 import textual；
渲染层也不允许 import 运行时内部——唯一装配点是 kernel.bundle.boot 与 TuiRuntime。

## 3. 启动流程

1. dpyro（无参数）→ main.main → DeepPyroApp(cwd/provider/model 可选)。
2. App.on_mount：先 push_screen(SplashScreen(...), callback=重新聚焦输入框)，再 await TuiRuntime.boot(sink=app.sink, ask=app.ask_approval)。
3. SplashScreen：内容按 40ms/行逐行揭示（宽 >= 72 且高 >= 24 用 22 行大字标，否则退化为单行字标），**揭示完成后一直停留，直到用户按任意键才进入主界面**（不自动关闭）；boot 完成后 App 调 set_runtime 回填 provider/model/cwd；boot 失败时立即关闭 splash 并显示错误。
4. TuiRuntime.boot：load_config → resolve_workspace → 校验 provider/model → kernel.boot（core bundle 39 行，首行 kernel 运行时）→ 取 llm.provider、tools.registry_for(workspace)、sandbox.gate(workspace, ask=ask)、activity/jobs/team/subagent 服务，并把 kernel/plugin_manager/inspect/settings/jobs/team/subagent 服务一并注入 AgentSession.services → presets.ensure_all() 物化全部 preset revision → 为 team 与 subagent 服务 configure + bind_session → new_session()。
5. new_session：preset_revision 清空 → SessionStore.create → _attach（agents.options(model=, context_window=) → agents.create(..., preset=, preset_revision=)）→ 写 model/selection（初始 provider/model）→ 写 agent-preset/selected（含 revision）→ AgentSession；open_session 先按日志最后一条 model/selection 恢复 provider/model（provider 不存在时忽略），再从 header 的 agentPreset 与日志里的 agent-preset/selected revision 恢复固定 revision，revision 已被删除时回退 latest。_attach 末尾若当前 preset 是 team 模式，则以会话 id 为键调用一次 TeamService.restore() 冷恢复团队（同一进程内再次 attach 不重放）。
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
| /permission [preset] | 查看/切换命名权限预设（read-only / workspace-write / danger-full-access），一个预设同时打包 sandbox mode 与 approval policy；无参打开选择器；写 permission/preset，并只对发生变化的旋钮写 sandbox/mode、approval/policy |

| /cwd [path] | 查看/切换工作目录；无参打开选择器（当前 + 最近会话目录 + 自定义路径模态）；写 working-directory/change 记录 |
| /status | 当前 provider（含 key 状态）与可用 providers、model、preset、permission preset/approval policy、cwd、session、tokens、上下文 |
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

- 权限预设把 sandbox mode 与 approval policy 打包（read-only→ask、workspace-write→ask、danger-full-access→never）；`/permission` 只切换 preset 身份，gate 动态从会话日志解析两个旋钮。手动把旋钮调成不在预设表里的组合时，当前值显示为派生的 `custom`（只读，不能选）。切换只写变化的事件，净零切换不写记录。
- ApprovalService：policy=never 时直接 rejected（不发 hook/模态）；policy=ask 时先 emit approval/asked，再走 APPROVAL_REQUEST waterfall，最后 emit approval/decided。
- App.ask_approval → await push_screen_wait(ApprovalScreen(request))：用户只能选择 once/deny（DSH 审批只允许一次；没有 always 选项，也没有全局放宽）。
- hook/answerer 都没有给出决定时按 unavailable fail closed，工具侧返回 isError 结果，不弹模态。

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
- 会话设置全部走 slash 命令，不再有顶部下拉：/permission 切命名权限预设、/cwd 切工作目录。切换立即作用于同一会话——sandbox gate 与工具注册表随工作目录更新，并写 permission/preset、sandbox/mode、approval/policy、working-directory/change 记录；turn 运行中拒绝切换并提示。无参命令打开 PickerScreen（当前值标记 current），/cwd 额外提供 Enter path… 选项打开 PathPromptScreen。
- 状态栏单行：session 短 id、provider/model、↑输入 ↓输出 ⚡缓存、ctx 百分比（>=75% 黄色）、permission（preset，组合不在预设表时显示 custom）、phase。
  ctx 用最近一次模型请求的 inputTokens（`Session.context_tokens()`），不是会话累计；累计值只出现在 ↑。
- 插件状态栏项：状态栏上方另有一行 PluginStatusBar，插件通过 slots.add_status_item 注册的渲染器按 (order,id) 排序渲染；每次 refresh_status 重新求值，渲染异常显示为 dim 错误标记而不影响主状态栏。
- 活动栏：Header 与对话区之间一行 ActivityBar，数据来自 activity 服务对 todo/write、job/registered|settled、team/member、subagent/descriptor|catalog、plan/mode 的投影；空段隐藏，点击段打开 PlanScreen / JobsPanelScreen / TeamPanelScreen。
- 后台任务：JobsPanelScreen 只读展示 jobs 服务快照（超长输出由 job_output 工具按 head/tail 预览）；/jobs 与活动栏 jobs 段等价。
- Hook 决策链：/hooks 从当前会话日志取 hook/invoked 与 hook/result 记录并配对（优先 SessionQuery.hook_trace，退化到 records 过滤），显示最近 50 条；热 hook 不落盘所以不出现，approval/* 与 session 生命周期 hook 无 agent 同样不出现；无记录时显示 "no hook activity yet"。
- 团队：TeamPanelScreen 展示成员与任务，TaskBoardScreen 按 pending → in_progress → completed 排序；/team 无参打开团队面板，/team board 打开任务板；活动栏 agents 段打开团队面板。/team on 在当前会话进入 Team 模式（Lead 获得 `team_*`、收起 `subagent`），/team off 回到进入前的 preset；有 live 成员（spawning/running/idle）时拒绝离开并列出成员。default 模式且无成员/任务时 /team 提示用 /team on 显式开启 Team 模式。
- 团队冷恢复：/resume 打开 team 模式会话时，_attach 在 bind 之后按会话 id 调用一次 TeamService.restore()：从 Lead 的 team/member、team/task、team/message/queued|delivered 记录重建成员、任务板与待办 inbox，崩溃残留的 spawning/running 归一为 idle，缺失的子会话标记 failed，第一条 queued 消息自动续跑（其余保留）；成员 id 即子会话 id。该重放每进程每会话只发生一次（preset/工作目录重新 attach 不会重放）。
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
`[agent].max_tokens` 默认 8192（snake_case；camelCase 旧写法自 0.2.0 起被拒绝）：思考、答案与工具调用参数共用这个输出预算。输出被截断（finish.kind=length）时，本轮尚未执行的 tool call 会被丢弃（避免日志里留下没有 tool/result 的孤儿调用），TUI 显示 error “response truncated at max_tokens=...; N tool call(s) were not executed”，turn 以 max-tokens 结束；此时需要提高 max_tokens 或把工作拆成更小步骤。

## 10. 已知偏离与待办

- 图片输入/粘贴：v1 不支持（session 层 image part 只读展示）。
- 会话恢复按 turn 重建统一的 AgentProcess（思考 step 行 + 工具行按顺序）并折叠成摘要，可点击展开/查看完整 reasoning；usage 明细不重放。
- 自动滚动固定在底部；用户上滚时不抢滚动（v1 以 call_after_refresh(scroll_end) 为主，后续精修）。
- 审批没有 always 选项：每次升权都要一次批准（一次一审批），不存在 gate 级全放行。
- 无 PTY；后台任务由 jobs 服务托管（shell run_in_background），不是持久交互 shell。
- 团队成员 id 是子会话 id；旧版本 `member-*` 记录无法恢复（成员标记 failed），恢复成员的 created_at 记为 0。团队成员子会话没有专门的会话浏览器（可 /resume 打开）。
- Ctrl+C 语义依赖 Textual 绑定；真实终端下的双击退出阈值 1.2s。
- splash 是纯文本逐行揭示（无淡入/逐字动画），且依赖 Unicode 方块字符；极窄或极矮终端用单行字标。
- thinking 单行滚动不保留已滚过的句子，只有折叠后点击展开才能看到全文。
- /permission 切换不改写已生成的 system prompt；新的 sandbox:policy / approval:policy 两段随下一次 runtime context 刷新进入请求；
  session header 的 cwd 保持会话创建值，切换只写 working-directory/change 记录。
- 切换工作目录要求当前无运行中 turn；/cwd 无参列表只含当前目录与最近会话目录，任意路径走路径输入模态。
- 插件设置页（SETTINGS 槽位）已注册但暂无宿主界面；设置写入仍是 next-boot 生效（volatile 字段除外）。
- 后台任务输出保存在内存中（模型侧 job_output 有界预览），进程重启后不恢复；headless 退出会 cancel_owner 取消本会话任务。
- 插件状态栏项在 refresh_status 时才刷新，插件内部状态变化需自行触发；插件对话行只追加。
- native 会话服务层已就绪（`session/native_query.py` 的有界查询/搜索/过滤/排序、`rename_native_session`、
  `archive_native_session`、`session/native_fork.py` 的全量 fork、`builtin/session_title.py` 的 native 命名、
  `InspectPlugin.native_session` 报告）。v4 默认 TUI 对 native 会话仍是只读列表 + 转录查看；native TUI 路径
  （`DEEPPYRO_TUI_NATIVE=1`）已接线 /sessions、/resume（attach）、/fork、/rename、/archive，见 §11。native 归档用
  `plugin.session.archived` 事件表示，`list_native_sessions` 默认隐藏归档行，按 id 读取（/resume）仍可用。

## 11. native TUI 路径（DEEPPYRO_TUI_NATIVE=1）

### 11.1 开关与入口

- `DEEPPYRO_TUI_NATIVE`：`1`/`true`/`yes`/`on`（不区分大小写）为 native；未设置或其他值走 v4，默认路径行为逐字节不变。
  `TuiRuntime.boot(..., native=None)` 的显式参数优先于环境变量（测试/内嵌用）。
- `dpyro --resume SESSION_ID`：native 模式下按 id 打开 native 会话，重建 host 并从 fold 恢复 model/preset/工作目录/历史。
  v4 模式忽略该参数并提示 `--resume needs DEEPPYRO_TUI_NATIVE=1; starting a new session`；`--headless` 忽略该参数。
- native 模式不装配 v4 SessionStore/Projections（store/projections 为 None），v4 SessionStore 一次也不调用
  （tests/tui/test_native_runtime.py 用 monkeypatch 断言）。

### 11.2 装配

- boot 额外 require PromptPlugin；provider 走 `LlmPlugin.native_provider`（只有 OpenAI-compatible 有 native transport，
  其他 provider 报 no native transport）；context meter 用的 `TraceExportSink` 从 kernel 取出并合并进 host services，
  host 的 `_ContextTap` 据此把最近一次 ModelCallRecord 的 inputTokens 记为 context_tokens。native-compact 插件装了才有 compactor；
boot 另从 kernel 取 HookService 建立 `tui/native_hook_trace.py` 的 NativeHookRecorder（`/hooks` 的 observer）。
- new_session：`sessions.native_create(NewSession(cwd, preset, origin="user"))` → `NativeSessionHandle` →
  `_pin_permission`（写 sandbox/mode、approval/policy 事实）→ SESSION_CREATED hook → `_attach_native`；
  `_attach_native` 用 `AgentBuild(provider, model, cwd, preset, preset_revision, permission, hooks, prompt_facts,
  services, context_window)` 调 `agents.host(handle.native_session, build)`，随后 `self.session = host.handle`
  （usage/context 计数在 host.handle 上）、`self.agent = host`。
- open_session/resume_session：`sessions.native_open(id)`（被其他进程锁定抛 SessionLockedError，未知 id 抛 NativeQueryError，
  都不破坏当前会话）；从 `handle.fold()` 恢复 state.model/state.preset（含 revision/previous）后重建 host，并 emit
  `{"type":"session","sessionId":...}`。
- 同一 native 会话 re-attach 不关闭旧 host（关闭会释放 NativeSession 写锁，反而卡住自己）；切换/关闭走 `_detach()`
  （aclose host 释放锁）。
- 工作目录/权限是 native 事实：set_workspace 写 working-directory/change 后 rebuild；set_permission 写
  sandbox/mode + approval/policy 事实；native gate 没有可 bind 的 v4 session，提示词用同一份 policy 文本。
- 会话标题：turn 1 结束后 TurnLayer 调 `runtime.name_session_title()` → `builtin/session_title.name_native_session`
  （已有标题或没有首条 user 消息时跳过；失败只记日志，不中断 turn）。

### 11.3 事件与 turn

- `tui/native_hooks.py` 的 TuiAgentHooks 把 AgentHooks Protocol 适配成 widgets/slots 已消费的 v4 dict 事件：
  run_started→status(thinking)、step_started→step、text_delta→text、reasoning_delta→thinking、
  tool_call_started→tool_call、tool_call_finished→tool_result；assistant_message/checkpoint_written/budget_exceeded/
  run_finished 不产生 TUI 事件（run_finished 只清 active）。有 active-run 守卫、turn/step 计数与事件上限
  （DEFAULT_EVENT_LIMIT=4096），不产生死事件/重复 emission。
- TurnLayer native 分支：`host.send(text)`；CancelledError→final(host.last_assistant_text())+status interrupted；
  BudgetExceeded/ProviderStreamError→error(str)；其他异常→error("Type: msg")；成功→final(result.final_text)+usage+
  status done，`finish_reason=="length"` 再追加一条截断 error。
- usage/context：native loop 的 RunResult.usage 不累计（token 只在 UsageSink tap 上），所以 usage 事件与
  `session_usage()`/`context_tokens()` 取 host 累计计数器；host 的 trace_export tap 使 context_tokens 反映最近一次
  ModelCallRecord 的 inputTokens。没有 TraceExportSink 时上下文计数不可用（不伪造 0）。
- 发送前 compaction（native，D4 决策）：`_run_native_turn` 在 `host.send` 之前调用 NativeCompactService 的
  pre_send_compact；fold 后的 messages 超过 `context_window * threshold`（默认 0.85）才压缩。压缩期间发
  status(phase="compacting") 并刷新状态栏，成功后对话区提示 "compacted N message(s) into a context summary"。
  compaction.done 事实只由 NativeCompactor 追加**恰好一条**（covers=被替代的消息 id、message=摘要），切点经
  select_region/tool_safe_cuts 保证不拆 tool call/result 对；失败发 error("compaction failed: Type: msg") 后照常发送。
  native loop 自身从不自动压缩。转写重放由 fold/compaction.done 提供，`/resume` 后 transcript 与压缩一致。
- Ctrl+C：`host.interrupt()` 返回 bool；true → notify("interrupt requested")，false → notify("nothing to interrupt")。

### 11.4 /sessions、/resume、/fork、/rename、/archive

- /sessions 在 native 模式只列 `sessions.native_list`（不再合并 v4 行），行标 `native`；`--all` 含归档；`--resume` 提示改为 attach。
- /resume：可 attach 的会话直接 `open_session` 并重放历史；被别的进程锁定 → 只读转录 + "another process owns this session"；
  fold 失败（readable=False）→ 只读转录 + "this session log did not fold cleanly"；损坏/外来的 .jsonl 在列表里跳过，
  按 id 打开时报错并提示，不崩溃。
- /fork [index|id|文本]：`SessionsPlugin.native_fork(source)`（新会话 parent_id=source、origin=fork、
  delegation_depth+1；源会话无需可写锁），detach 当前 host、aclose 新 handle、`open_session(新 id)`，UI 显示
  "forked a -> b"。裸 uuid 自动补 `session-` 前缀（fork 解析要求完整 id）。
- /rename：当前 native 会话通过 live handle append `session/title`（v4 的 record_title 只对 v4 会话有效）。
- /archive：通过 live native session append `plugin.session.archived`（{"archived": true, "source": "user"}）后 new_session；
  /unarchive 用 `sessions.native_archive(id, archived=False)`（只能对非 live 会话，live 锁会报 locked）。
- v4 模式保持原行为：/sessions 合并 v4+native 并标 `native · read-only`，/resume 对 native id 只读转录并提示 migrate。

### 11.5 native 能力边界（0.3.0 native-only 目标：不再有 "disabled while native" 分支）

- /team on|off（native）：切换 team preset 并把 NativeTeamManager bind 到当前 NativeAgentHost/session；
  roster 与任务板从 `plugin.team.*` 事实折叠，TeamPanelScreen 追加 queued/delivered 状态与 native subagent
  run 行；member 结果/失败通过 `host.deliver` 投递。`/team off` 在有 live 成员时仍拒绝，否则停止 manager
  （`TeamService.stop()`）且**不改写会话日志**；`/resume` 后冷恢复由 `restore()` 重放 team 事实
  （running/spawning 归一为 idle，queued 消息重排）。
- /hooks（native）：不重引入 v4 HookJournal；从 NativeAgentHost 使用的**同一个 HookService** 注册 observer，
  显示 hook registry（名字/mode/listener 数）与最近决策（invoked/resolved、ok/error、duration ms）。native loop
  只 dispatch 工具管线（tools/pre-execute、tools/execute、tools/post-execute）与 session 生命周期 hook；v4 turn
  编排 hook 不会触发，显示 0 次而不是伪造。bundle 无 HookService 时显示
  "native hook registry: not recorded (the hook service is unavailable in this bundle)"。
- compaction（native）：发送前自动压缩，见 §11.3；`/compact` 仍可手动触发。bundle 无 native-compact 插件时
  `/compact`（及 auto-compaction）抛 "native compaction is not in this bundle; /compact is unavailable"。
- PTC preset（native）：`ptc/native_adapter.py` 在 native host 上装配 PTC——loop 可见面只有 `run_code`，完整
  registry 留在 tool context 供 nested dispatcher 与 SDK prompt；不静默回退 v4。缺 ptc runtime 时显式抛
  "preset ... needs the ptc runtime, which this bundle does not provide"；缺 presets 时抛
  "agent presets are not available in this bundle"。
- subagent（native）：ActivityBar/TeamPanelScreen 从 `plugin.subagent.*` 事实显示 run 与其 usage；没有 live
  service（冷恢复）时显示 "usage not recorded"，不估算。嵌套 subagent 被 contextvar 守卫拒绝：
  "nested subagent delegation is not supported on the native runtime; a subagent cannot start another subagent"
  （作为 tool error 返回，不崩溃）。
- 仍然显式暴露的真实边界（不伪造、不回退 v4）：
  - v4 会话在 native 模式不可见（native 会话在 v4 模式只读）；不做隐式迁移。
  - 上下文米需要 trace_export sink；缺失时显示不可用而不是 0。
  - `/hooks` 只能显示 boot 之后观察到的决策；更早的历史显示 "not recorded"（schema 1 没有 hook journal）。
  - native loop 不运行 v4 turn 编排 hook（agent/pre-step、agent/request、agent/turn-stopping、
    system-prompt/assemble 等），这些 hook 在 native 上保持 0 次。
  - team member / subagent 的 usage 不在父会话日志里（trace only）；冷恢复后显示 "usage not recorded"。

### 11.6 测试

- tests/tui/test_native_hooks.py：事件形状/顺序/去重/有界队列。
- tests/tui/test_native_resume.py：native /sessions 列表与 attach 提示、按 index/id/title 恢复、picker 路径、locked 只读转录、
  fold 损坏提示、损坏 jsonl 跳过、真实历史渲染（含 tool 过程）、/fork（当前与被命名来源）、/rename、/archive+/unarchive、fork 失败提示。
- tests/tui/test_native_runtime.py：flag 关闭保持 v4；flag 开启 native 装配（native session/host、store None、session 事件、
  session id 一致、fold model/preset）；环境变量开关；native 路径不调用 v4 SessionStore（monkeypatch 断言）；
  turn 精确事件序列；截断（finish_reason length）追加 error；provider 异常与普通异常映射；interrupt（运行中 true / 空闲 false）；
  resume 重建；native 标题写入；native 能力开关（/team on|off、hook recorder 存在、无 native-compact 时的显式错误）；
  CLI --resume 与 App 透传。
- tests/tui/test_native_team.py：/team on 绑定 native manager；off 停止且不写 plugin.team.* 事实；live 成员时拒绝 off；
  queued/delivered 事实与面板行；`/resume` 后冷恢复。
- tests/tui/test_native_hooks_ui.py：HookService observer 配对（invoked/resolved、error、时长、ring 上限）、registry 行与
  "not recorded" 回退；真实 native turn 的 tools/* hook 决策；`/hooks` 命令渲染。
- tests/tui/test_native_compaction.py：阈值判定、status(compacting)、恰好一条 compaction.done、covers/tool-pair 安全切点、
  fold 与重放、失败路径。
- tests/tui/test_native_ptc.py：PTC preset 切换到 run_code surface（完整 registry 保留在 tool context）、SDK 提示词、
  切回默认；缺 presets/ptc runtime 的显式错误。
- tests/tui/test_native_subagent.py：嵌套 subagent 守卫；`plugin.subagent.*` 事实折叠出 run/usage/"usage not recorded"；
  ActivityService 记录投影；面板行。