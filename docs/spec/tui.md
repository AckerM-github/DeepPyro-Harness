# DeepPyro TUI 规格（Textual 8.2.8）

状态：TUI 已完整实现（FIGlet 像素字标启动页、单行滚动思考 ticker、对话流、多行输入与 slash 补全、选择器、审批、插件管理、插件槽位、状态栏），885+ 测试与 ruff 全绿；真实模型端到端冒烟通过（启动页 → 任意键 → 工具调用 → 回答；真机用 /policy 切到 read-only、/approval 切到 never、/cwd 切换工作目录；插件贡献状态栏项/对话行/面板）。

## 1. 形态与范围

- 唯一产品形态：全屏终端 TUI（Textual，alt-screen）。不做 Web/Electron UI。
- 保留 headless 一次性模式（dpyro --headless / dpyro <prompt>，NDJSON 输出），不属于 TUI 渲染层。
- 终端能力：True Color、Markdown 渲染、鼠标可选（v1 以键盘为主）。
- 目标平台：Windows 优先（PowerShell/Windows Terminal），Linux/macOS 尽力兼容。

## 2. 模块布局（src/deeppyro/tui/）

| 文件 | 职责 | 所有者 |
| --- | --- | --- |
| main.py | argparse 入口 dpyro；--version/--headless/--json/--provider/--model/--cwd/--max-steps | Lead |
| app.py | DeepPyroApp：compose 布局、事件分发、turn 生命周期、快捷键、命令处理方法 | Lead |
| runtime.py | TuiRuntime：boot 内核服务（含 kernel/presets）、创建/切换会话、按 preset revision 重建 AgentSession、compact_now、close | Lead |
| sink.py | TuiSink：EventSink 实现，累计 usage/final/error 并转发 dict 事件 | Lead |
| format.py | 纯函数格式化（短 session id、token、上下文百分比、文本截断） | Lead |
| wordmark.py | FIGlet ansi_shadow 预渲染像素字标 + 逐字符渐变色（宽/窄两套） | Lead |
| commands.py | slash 命令定义与分发 | tui-dev |
| widgets/conversation.py | 对话流：user/assistant/tool/thinking 块 | ui-dev |
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
4. TuiRuntime.boot：load_config → resolve_workspace → 校验 provider/model → kernel.boot（core bundle 17 行，首行 kernel 运行时）→ 取 llm.provider、tools.registry_for(workspace)、sandbox.gate(workspace, ask=ask)，并把 kernel/plugin_manager/settings/inspect 服务一并注入 AgentSession.services → presets.ensure_all() 物化全部 preset revision → new_session()。
5. new_session：preset_revision 清空 → SessionStore.create → _attach（agents.options(model=, context_window=) → agents.create(..., preset=, preset_revision=)）→ 写 agent-preset/selected（含 revision）→ AgentSession；open_session 从 header 的 agentPreset 与日志里的 agent-preset/selected revision 恢复固定 revision，revision 已被删除时回退 latest。
6. 失败：conversation.add_error(failed to start: ...)，App 仍可退出。
7. App.on_unmount：await runtime.close()（kernel_app.stop 逆序释放插件）；幂等。

## 4. 事件流（单线程原则）

- AgentSession 在事件循环里同步 sink.emit(dict)；TuiSink.emit 先更新内部状态（usage/final/error 计数），再调用 on_event。
- on_event = App.handle_event：text/thinking → ConversationView.stream_*；tool_call/tool_result → 工具卡片；usage → 上下文占用；final → 收尾；error → 红色错误块；status → 状态栏 phase。
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
| Esc | 关闭模态（审批/选择器/插件管理/插件面板/路径输入/命令选择器） |

## 6. Slash 命令

| 命令 | 说明 |
| --- | --- |
| /help 或 /? 或 /h | 帮助 |
| /new | 新建会话（保留当前 App 与 sink，重建 AgentSession） |
| /resume [id 或序号] | 恢复最近会话；无参打开选择器 |
| /model [id] | 切换模型；无参打开选择器 |
| /preset [id] | 切换 agent preset（按会话组合工具/提示词）；无参打开选择器 |
| /policy [preset] | 查看/切换权限预设（read-only / workspace-write / danger-full-access）；无参打开选择器；写 permission/preset 记录 |
| /approval [policy] | 查看/切换审批策略（ask / never / always）；无参打开选择器；写 approval/policy 记录 |
| /cwd [path] | 查看/切换工作目录；无参打开选择器（当前 + 最近会话目录 + 自定义路径模态）；写 working-directory/change 记录 |
| /status | 当前 provider/model/preset/policy/cwd/session/tokens/上下文 |
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
- thinking 是单行滚动 ticker：流式只显示“最新一句”（按句末标点/换行切句，单行上限 120 字符，超长保留尾部），答句一开始就折叠成 “· thinking · N sentences (click to expand)”，点击展开全文（40 行 / 4000 字）。
- splash 使用 wordmark.py 里预渲染的 ansi_shadow 字形：DEEPPYRO 火色渐变、HARNESS 冰蓝色渐变，逐行揭示；不依赖 pyfiglet 等运行时字体工具。
- 工具调用：name + 参数摘要；结果 1-3 行预览，错误红色；write/edit 显示 diff（能力上限见 tui-widgets.md）。
- 用户消息块左 accent 边框；系统 info/error 用 dim/红色静态行。
- 会话设置全部走 slash 命令，不再有顶部下拉：/policy 切权限预设、/approval 切审批策略、/cwd 切工作目录。切换立即作用于同一会话——sandbox gate 与工具注册表随工作目录更新，并写 permission/preset、approval/policy、working-directory/change 记录；turn 运行中拒绝切换并提示。无参命令打开 PickerScreen（当前值标记 current），/cwd 额外提供 Enter path… 选项打开 PathPromptScreen。
- 状态栏单行：session 短 id、provider/model、↑输入 ↓输出 ⚡缓存、ctx 百分比（>=75% 黄色）、policy（preset/approval）、phase。
- 插件状态栏项：状态栏上方另有一行 PluginStatusBar，插件通过 slots.add_status_item 注册的渲染器按 (order,id) 排序渲染；每次 refresh_status 重新求值，渲染异常显示为 dim 错误标记而不影响主状态栏。
- 插件对话行：handle_event 在 status 事件后调用 apply_conversation_slots，插件按 predicate 匹配事件并追加自定义行（只追加、宿主不重放历史）。
- 插件面板：/panel 从 slots 的 PANEL 槽位取工厂，打开 PluginPanelScreen（表格/自定义 widget 二选一）。
- agent preset：会话创建时把 preset id 写进 session header 的 agentPreset，并把最新 revision 写进
  agent-preset/selected 记录；/resume 先按该记录固定 revision 重建 agent（revision 已不存在时回退
  最新并重新记录），/preset 切换物化最新 revision 并重建当前会话的 agent（当前会话 header 不重写，
  只影响后续新建会话）。prompt 段与工具贡献按 preset scope + revision 过滤，preset 内声明的子插件
  在独立 realm 中物化。

## 9. 开发与验证

- 单元/组件测试：pytest tests/tui，用 App.run_test(size=...) 无终端渲染；假 runtime 注入（runtime_factory）。
- 视觉预览：D:/dshworkspace/.dsh-tmp/tui_preview.py 与 splash_preview.py 以假 runtime 渲染并 export_screenshot(title=...) 出 SVG；
  再用 npx --yes sharp-cli -i x.svg -o x.png 转 PNG 自查（本机无 cairo/ImageMagick）。
- 截图脚本必须先 os.environ.pop("NO_COLOR", None) 并设 COLORTERM=truecolor，否则 Textual 导出全灰度 SVG（DSH 终端 env 默认 NO_COLOR=1）。
- 真实链路冒烟：D:/dshworkspace/.dsh-tmp/runtime_smoke.py（TuiRuntime.boot + agent.send，需本地 8000 推理服务）。
- 回归红线：不得再次出现 widget 方法名为 _render（Textual 私有名，会把 Widget._render 顶掉导致 render_strips 崩溃）。
- 所有 widget 刷新只允许在 Textual 事件循环线程内；后台线程只允许 put_message/post_message。
- 插件槽位测试：tests/tui/test_slots.py（注册表排序/去重/dispose、theme/locale）与 test_plugin_slots.py（状态栏、对话行、面板宿主）、test_ui_package.py（ui.toml 声明式包、坏 entry 隔离）；跨层集成在 tests/integration/test_plugin_services.py。
- preset revision 接线测试：tests/tui/test_runtime_presets.py（new_session 记录 revision、resume 固定 revision、revision 失效回退、/preset 物化最新 revision）。

## 9.1 思考过程的来源

TUI 只渲染 provider 返回的 reasoning 流（OpenAI-compatible 的 `reasoning_content` 字段）。本地 dshinfer 端点是否
返回思考由 `DSHINFER_ENABLE_THINKING` 控制（默认 `false`，即只出答案）；要看到逐句思考，用
`$env:DSHINFER_ENABLE_THINKING="true"; uv run uvicorn dshinfer.api:app --port 8000` 启动推理服务即可（本机真实冒烟即用此配置）。
`[agent] maxTokens` 默认 4096：思考与答案共用这个输出预算，太小会把答案截断。

## 10. 已知偏离与待办

- 图片输入/粘贴：v1 不支持（session 层 image part 只读展示）。
- 会话恢复只渲染 user/assistant/tool 文本，不重放 thinking/usage 明细。
- 自动滚动固定在底部；用户上滚时不抢滚动（v1 以 call_after_refresh(scroll_end) 为主，后续精修）。
- 审批 always 粒度是 gate 级（本会话全局），未做 per-tool。
- 无 PTY/后台任务界面（run_in_background 工具未实现）。
- Ctrl+C 语义依赖 Textual 绑定；真实终端下的双击退出阈值 1.2s。
- splash 是纯文本逐行揭示（无淡入/逐字动画），且依赖 Unicode 方块字符；极窄或极矮终端用单行字标。
- thinking 单行滚动不保留已滚过的句子，只有折叠后点击展开才能看到全文。
- /policy 与 /approval 切换不改写已生成的 system prompt（提示词里的沙箱说明仍是 turn 开始时的快照）；
  session header 的 cwd 保持会话创建值，切换只写 working-directory/change 记录。
- 切换工作目录要求当前无运行中 turn；/cwd 无参列表只含当前目录与最近会话目录，任意路径走路径输入模态。
- 插件设置页（SETTINGS 槽位）已注册但暂无宿主界面；设置写入仍是 next-boot 生效。
- 插件状态栏项在 refresh_status 时才刷新，插件内部状态变化需自行触发；插件对话行只追加。