# DeepPyro TUI 规格（Textual 8.2.8）

状态：TUI 已完整实现（对话流、多行输入与 slash 补全、选择器、审批、插件管理、状态栏），642 测试与 ruff 全绿；真实模型端到端冒烟通过。

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
| runtime.py | TuiRuntime：boot 内核五服务、创建/切换会话、重建 AgentSession、compact_now、close | Lead |
| sink.py | TuiSink：EventSink 实现，累计 usage/final/error 并转发 dict 事件 | Lead |
| format.py | 纯函数格式化（短 session id、token、上下文百分比、文本截断） | Lead |
| commands.py | slash 命令定义与分发 | tui-dev |
| widgets/conversation.py | 对话流：user/assistant/tool/thinking 块 | ui-dev |
| widgets/status_bar.py | 底部状态栏 | ui-dev |
| widgets/prompt_input.py / prompt_area.py | 输入框（单行 → 多行） | tui-dev |
| widgets/completion.py / history.py | slash 补全、输入历史 | tui-dev |
| screens/approval.py | 审批模态 | tui-dev |
| screens/picker.py | 会话/模型选择器 | tui-dev |
| screens/plugins.py | 外部插件管理界面 | plugin-dev |
| theme.tcss | 深色主题与布局样式 | ui-dev |

领域模块（session/llm/tools/agent/compact）与内核（kernel/、_vendor/cordis_port）不允许 import textual；
渲染层也不允许 import cordis——唯一装配点是 kernel.bundle.boot 与 TuiRuntime。

## 3. 启动流程

1. dpyro（无参数）→ main.main → DeepPyroApp(cwd/provider/model 可选)。
2. App.on_mount：focus 输入框，然后 await TuiRuntime.boot(sink=app.sink, ask=app.ask_approval)。
3. TuiRuntime.boot：load_config → resolve_workspace → 校验 provider/model → kernel.boot（llm/sessions/sandbox/tools/agents 五服务）→ 取 llm.provider、tools.registry_for(workspace)、sandbox.gate(workspace, ask=ask) → new_session()。
4. new_session：SessionStore.create → agents.options(model=, context_window=) → agents.create(session/provider/registry/options/sink/permission/cwd) → AgentSession。
5. 失败：conversation.add_error(failed to start: ...)，App 仍可退出。
6. App.on_unmount：await runtime.close()（kernel_app.stop 逆序释放插件）；幂等。

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
| Esc | 关闭模态（审批/选择器/插件管理） |

## 6. Slash 命令

| 命令 | 说明 |
| --- | --- |
| /help 或 /? 或 /h | 帮助 |
| /new | 新建会话（保留当前 App 与 sink，重建 AgentSession） |
| /resume [id 或序号] | 恢复最近会话；无参打开选择器 |
| /model [id] | 切换模型；无参打开选择器 |
| /status | 当前 provider/model/cwd/session/tokens/上下文 |
| /compact | 手动压缩（要求无运行中 turn；Compactor.compact） |
| /clear | 清空对话视图（不删除会话日志） |
| /plugins | 外部插件管理界面（plugin-dev 提供 PluginScreen，Lead 接线） |
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
- thinking 流式显示 dim italic 尾部，结束后折叠一行，可点击展开。
- 工具调用：name + 参数摘要；结果 1-3 行预览，错误红色；write/edit 显示 diff（能力上限见 tui-widgets.md）。
- 用户消息块左 accent 边框；系统 info/error 用 dim/红色静态行。
- 状态栏单行：session 短 id、provider/model、cwd、↑输入 ↓输出 ⚡缓存、ctx 百分比（>=75% 黄色）、approval、phase。

## 9. 开发与验证

- 单元/组件测试：pytest tests/tui，用 App.run_test(size=...) 无终端渲染；假 runtime 注入（runtime_factory）。
- 视觉预览：D:/dshworkspace/.dsh-tmp/tui_preview.py 以假 runtime 渲染并 export_screenshot(title=...) 出 SVG；
  再用 npx --yes sharp-cli -i x.svg -o x.png 转 PNG 自查（本机无 cairo/ImageMagick）。
- 真实链路冒烟：D:/dshworkspace/.dsh-tmp/runtime_smoke.py（TuiRuntime.boot + agent.send，需本地 8000 推理服务）。
- 回归红线：不得再次出现 widget 方法名为 _render（Textual 私有名，会把 Widget._render 顶掉导致 render_strips 崩溃）。
- 所有 widget 刷新只允许在 Textual 事件循环线程内；后台线程只允许 put_message/post_message。

## 10. 已知偏离与待办

- 图片输入/粘贴：v1 不支持（session 层 image part 只读展示）。
- 会话恢复只渲染 user/assistant/tool 文本，不重放 thinking/usage 明细。
- 自动滚动固定在底部；用户上滚时不抢滚动（v1 以 call_after_refresh(scroll_end) 为主，后续精修）。
- 审批 always 粒度是 gate 级（本会话全局），未做 per-tool。
- 无 PTY/后台任务界面（run_in_background 工具未实现）。
- Ctrl+C 语义依赖 Textual 绑定；真实终端下的双击退出阈值 1.2s。