# TUI 对话渲染、活动栏与状态栏规格（widgets）

状态：M1/M2/M3 已实现；对话流按 user turn 组织，思考与工具合并为每 turn 一个可折叠 AgentProcess
（默认收起为一行摘要，展开按时间顺序显示 step 行与工具行），全屏查看器查看完整 reasoning；
新增活动栏、计划/任务/团队面板；设置改走 slash 命令（/policy /approval /cwd）。

本文件描述 `src/deeppyro/tui/widgets/` 与相关 screens 的**渲染语义**，以及从 fixtures 事件与终端
截图逆向出的取舍。总体形态、键位与启动流程见 `docs/spec/tui.md`；跨模块事件字段以
`docs/spec/INTERFACES.md` §7 为准。

## 1. 负责文件

| 文件 | 职责 |
| --- | --- |
| `widgets/conversation.py` | `ConversationView`：user/assistant/system 块、流式文本、同 turn 标签去重、每 turn 一个 AgentProcess |
| `widgets/agent_process.py` | `AgentProcess` / `ToolProcessLine`：思考 step 行与工具行按时间顺序融合、默认收起、展开详情、点击 step 打开查看器 |
| `screens/thinking.py` | `ThinkingScreen`：全屏按 Turn/Step 查看完整 reasoning |
| `tui/history.py` | `ReasoningStep`/`TurnReasoning`/`TurnProcess`/`processes_from_session`/`process_map`：从 durable 日志重建每个 turn 的思考与工具过程 |
| `widgets/activity_bar.py` | `ActivityBar`：plan/jobs/agents 三段活动栏（空段隐藏，点击发事件） |
| `screens/plan.py` | `PlanScreen`：待办按 in_progress / pending / completed 分组 |
| `screens/jobs.py` / `screens/team.py` | `JobsPanelScreen` / `TeamPanelScreen` / `TaskBoardScreen`：只读快照 |
| `widgets/tool_card.py` | 工具摘要辅助函数：`parse_arguments` / `summarize_arguments` / `clean_result_text` / `diff_preview_lines`（AgentProcess 复用，纯函数） |
| `widgets/status_bar.py` | `StatusBar`：单行状态（session/model/tokens/ctx/policy/phase） |
| `theme.tcss` | 调色板、user/assistant/tool/活动栏/状态栏样式、Markdown 边距覆盖 |
| `tests/tui/` | test_conversation / test_agent_process / test_tool_card / test_status_bar / test_thinking_screen / test_activity_bar / test_plan_screen / test_jobs_screen / test_team_screen / test_history |

公开 API 兼容性是硬约束：`ConversationView` 的 `add_user/add_info/add_error/stream_text/`
`stream_thinking/finish_assistant/add_tool_call/add_tool_result/clear` 方法名与参数保持不变（`app.py`
直接调用）；在此基础上新增 `begin_step(turn, step)`、`finish_turn(final_text)`、`current_reasoning`、
`process`、`add_history_process(TurnProcess)`。`StatusBar.show()` 的关键字参数已精简为
`session_id/provider/model/input_tokens/output_tokens/cached_tokens/context_window/phase`
（`cwd` 与 `approval` 改由 slash 命令 /cwd 与 /approval 管理，不再进状态栏）。

## 2. 事件到 widget 的映射

`app.py` 的 `handle_event` 把事件 dict 转成 widget 调用；widgets 不 import agent/llm/session。

| 事件 `type` | 字段（实际使用） | 渲染 |
| --- | --- | --- |
| `text` | `text` | 追加到当前 assistant 块，Markdown 增量预览 |
| `step` | `turn`、`step` | `ConversationView.begin_step`：同一 user turn 复用同一个 `AgentProcess`，切到新 step（UI-only hint，紧跟 durable `step/start`） |
| `thinking` | `text` | 追加到当前 `AgentProcess` 的当前 step 行（实时只显示当前 step 最新一句） |
| `final` | `text` | `finish_turn`：定稿 assistant 文本并收起当前 `AgentProcess` |
| `tool_call` | `callId`、`name`、`arguments`（JSON 字符串） | 在当前 `AgentProcess` 内新增一条工具行，按 `callId` 登记（只收尾 assistant，不结束 process） |
| `tool_result` | `callId`、`isError`、`text` | 回填对应工具行；找不到调用时建 fallback 行 `tool` |
| `error` | `message` | `finish_turn(None)` 收起 process 后 `add_error` 红色系统行 |
| `status` | `phase` | 只更新状态栏 phase |
| `usage` | `input` 等 | app 记上下文占用；status 刷新 |

语义依据：fixtures 中 `tool/call` 的 `arguments` 是 JSON 字符串，`tool/result` 通过
`toolCallId` 关联；`assistant/message` 的 content 同时含 reasoning 与 text part，因此思考与正文
必须是两个独立显示区。`app.py` 在转发时统一 `str()`，所以 `arguments` 也可能是 Python repr
（`{'path': ...}`），解析层同时接受 JSON 与 `ast.literal_eval`。

## 3. 对话流（ConversationView）

- user 块：`Vertical` 内含 `You` 标签与正文 `Static`；左侧 `thick $accent` 竖条、`$surface-2`
  背景、宽度 `1fr` 自动换行。`add_user` 同时把“本 turn 是否已有 assistant 块”重置为 False。
- assistant 块：`AssistantBlock(show_label=...)` 只含可选 `DeepPyro` 标签与 `Markdown`；思考与工具
  由独立的 `AgentProcess` 承载，不再嵌在 assistant 块内。首个块懒挂载；流式文本 60ms 节流
  （`STREAM_REFRESH_SECONDS`），未挂载时先累积，`on_mount` 时强制刷新，因此“事件早于 mount”不会
  丢字或崩溃。
- 标签去重：同一 user turn 内只有一个 `DeepPyro` 标签。tool step 之间的过渡块、`final` 后新建的块
  都不再显示标签；下一次 `add_user`（或 `clear`）后恢复。工具调用/结果会先收尾当前 assistant 块，
  保证卡片顺序 = 事件顺序。
- Markdown 局部非法时回退为纯文本，绝不让渲染异常中断 turn。
- `clear()` 清空子节点、assistant/tool 引用与标签状态；历史回放复用 `stream_text + finish_assistant`，
  因此历史 assistant 消息也遵守同样的标签规则。
- `add_info` 为 dim 系统行；`add_error` 为红色 `error:` 行。

## 4. Agent 过程块（AgentProcess）与查看器

- 每个 **user turn** 只有一个 `AgentProcess`（`Vertical.agent-process`），而不是每个 assistant step 一个；
  `begin_step(turn, step)` 在 turn 变化或上一条已结束时新建，否则复用并切换 step。
- 结构：header 一行 + body 内的有序条目。body 条目按事件到达顺序排列——思考 step 行（`.process-step`）
  与工具行（`.process-tool`）交替，**不再分成两个独立区域**；这对应“思考一行、工具一行，都属于 agent 的过程”。
- 运行中：**body 默认收起**，对话里只保留一条滚动 header：`✻ Step N · 当前 step 最新一句`；
  若当前有工具正在执行，header 改为 `✻ Step N · → 工具名 摘要`，结果回来后回到思考句。
  思考行只显示当前 step 的最新完整句（按句末标点/换行切句），单行上限 120 字符，超长保留尾部加 `…`。
  header 末尾有 `(click to view)` 提示，点击才展开下面的 step/tool 明细。
- 结束：`finish()` 幂等，`finish_turn` 调用；body 收起（display=False），header 变为
  `· agent process · N steps · M tools · K sentences  (click to view)`（单复数）。
- 交互：点击 header 切换展开/收起（展开后提示 `(click to collapse)`，运行中也可展开查看已发生
  的 step/tool 明细）；点击有内容的 step 行发
  `AgentProcess.ThinkingRequested(reasoning)`，app 打开 `ThinkingScreen([reasoning])`，按 Turn/Step
  显示完整文本（markup=False，可滚动）。
- 没有 reasoning 也没有工具时 `display = False`；turn 内 assistant 正文与工具行不会结束 process，
  只有 `final`、`error` 或下一条 user 消息会收尾。
- 历史回放：`process_map(session)` 从 `turn/start` + `assistant/message`（reasoning/tool-call parts）
  + `tool/result` 重建每个 turn 的有序 entries；`_render_history` 在 user 消息后
  `add_history_process(turn_process)` 挂一条已收起的块，可点击展开 / 点击 step 查看。
- 完整 reasoning 已由 AgentSession 持久化在 `assistant/message`，查看器不新增存储格式。

## 5. 过程内的工具行（ToolProcessLine）

一个工具调用 = `ToolProcessLine`（`.process-tool`）：默认一行调用/结果摘要，点击展开详情；同一 `callId`
原位回填，不新开块。行结构 = `.process-tool-line`（摘要）+ `.process-tool-detail`（展开详情）。

摘要行：

- 运行中：`→ name: 摘要`；成功：`✓ name: 摘要 · 结果首行预览`；失败：`✗ name  failed` 整行红。
- 结果预览最多 1 行 / 80 字符；成功结果保留首行，错误结果保留首行错误信息。
- 摘要规则：

| 工具名 | 摘要 |
| --- | --- |
| `read`/`write`/`edit`/`multi_edit`/`multiedit` | `path`/`file_path`/`filePath`/`file`/`filename` 的值 |
| `bash`/`sh`/`shell`/`pwsh`/`powershell`/`cmd`/`exec`/`command` | `command`/`cmd`/`script`/`input` 前 60 字 |
| 其它 | 紧凑 JSON 前 80 字；无法解析则原文前 80 字 |

参数解析顺序：JSON → Python 字面量 → `{}`。解析失败不抛异常，退化为原文摘要。

展开详情（点击工具行）：

- `arguments`：原始参数（JSON 美化，最多 20 行 / 2000 字符），因此 write/edit 的 old/new 内容可见。
- `result`：完整工具结果（最多 40 行 / 4000 字符）；预览前剥离传输包裹：整段
  `<content>...</content>` / `<output>...</output>` 直接去掉外层；文件系统工具的多段信封
  （`<path>..</path>` + `<type>..</type>` + `<content>..</content>` + footer）只保留
  `<content>` 内的载荷，footer（`End of file` / `Showing lines ...`）不进详情。无法识别的包裹保持原文。
- 详情只读；再次点击收起。错误行默认为红但详情仍可读。

**限制**：工具行不渲染格式化 diff（`diff_preview_lines` 仅作为辅助函数保留），展开的
`arguments` 会显示 old/new 原文；若需要真实 diff 应由工具结果或后续插件提供。

## 6. 活动栏与任务/团队面板

- `ActivityBar` 是纯视图：`update_state(ActivityState)` 是唯一入口，state 来自 activity 服务对
  `todo/write`、`job/registered|settled`、`team/member`、`subagent/descriptor|catalog`、`plan/mode`
  的投影。三段为空则整条隐藏；任一段为空则隐藏该段。
- 点击子段发 `ActivityBar.SegmentClicked(segment)`；app 映射 plan→`PlanScreen`、jobs→`JobsPanelScreen`、
  agents→`TeamPanelScreen`。
- `PlanScreen` 按 in_progress → pending → completed 分组（glyph ◐/☐/✓），空态 `No plan yet.`。
- `JobsPanelScreen` 列 ID/Kind/Status/Label/Started/Finished；running 绿、failed 红、cancelled/killed 黄；
  空态 `No background jobs.`。
- `TeamPanelScreen` 两张表（成员、任务）；`TaskBoardScreen` 单表并稳定排序 pending → in_progress →
  completed；两者接受 service 对象/dict/activity 对象，缺字段容错。
- 活动栏由 app 的 1 秒定时器刷新；事件处理末尾也会刷新。

## 7. 状态栏（StatusBar）

单行字段顺序：`session <短id> · <provider>/<model> · ↑输入 ↓输出 [⚡缓存] · ctx N% · phase`。

- 短 id：去掉 `session-` 前缀后前 8 位（fixtures 的 id 是 `session-<uuid>`，字面前 8 位无区分度）。
- model：`MAX_MODEL_CHARS = 40`，超长截断加 `…`；provider 为空时只显示 model。
- ctx：`round(100 * context_tokens / context_window)`，>= 75% 用黄色；token 数量 k/M 缩写。
  `context_tokens` 是最近一次模型请求的 `inputTokens`（`Session.context_tokens()`），不是会话累计；
  `input_tokens` 仍是累计值，只用于 ↑ 显示。
- 颜色：session 蓝、model 青、token 绿、phase 非 idle 蓝；其余 dim。
- 行级 `text-wrap: nowrap` + ellipsis，保证永远一行；不换行比截断选项更可预期。
- `show()` 关键字参数：`session_id/provider/model/input_tokens/output_tokens/cached_tokens/context_window/phase`，
  以及可选的 `context_tokens`（缺省回退 `input_tokens`）；
  `cwd` 与 `approval` 由 slash 命令（/cwd、/approval）管理，不占状态栏空间（见 `tui.md` §8）。

## 8. 主题与 CSS 取舍

调色板（唯一来源 `theme.tcss`，widget 的 rich Text 里只允许引用同一组十六进制值）：

| 变量 | 值 | 用途 |
| --- | --- | --- |
| `$bg` | `#0f1115` | 屏幕背景 |
| `$surface` / `$surface-2` | `#161a22` / `#1b2029` | 卡片、user 块、输入框 |
| `$border` | `#2a3040` | 普通边框、tool 结果左边条 |
| `$text` / `$dim` | `#c0caf5` / `#565f89` | 正文 / 次要信息 |
| `$accent` / `$cyan` | `#7aa2f7` / `#7dcfff` | user 边框、tool 调用条、model |
| `$green` / `$yellow` / `$red` | `#9ece6a` / `#e0af68` / `#f7768e` | 成功/警告/错误 |

- `.assistant-markdown` 把 Textual Markdown 默认的左右 padding、标题上下 margin 归零/收紧：
  `MarkdownHeader { margin: 0 0 1 0 }`、H1 左对齐且透明背景、`MarkdownParagraph/MarkdownFence`
  只留一行间距。这是 M0“正文与代码块之间巨大空白、标题居中”的根因修复。
- `.user-block` 用 `border-left: thick $accent` + `$surface-2` 背景；SVG 截图里竖条会显示成
  逐行小段（字体行高导致），真实终端中为连续竖条。
- `#conversation` 的滚动条改为 `$surface-2` 手柄 + `$bg` 轨道；默认 accent 手柄在长对话里像
  一条亮蓝色粗边。
- `#status-bar` 固定 1 行、`padding: 0 2`、nowrap + ellipsis。

## 9. 视觉验证

- 预览脚本：`D:/dshworkspace/.dsh-tmp/tui_preview_v2.py`（假 runtime，固定 local 模型与假工具流，
  在创建 App 前 `os.environ.pop("NO_COLOR")`；否则 Textual 会去色）。
- 生成物：`tui_v2_normal.png`（流式正文 + 折叠 thinking + Markdown 标题/代码块）、
  `tui_v2_tools.png`（read/edit diff/pwsh 失败 + 错误行 + 滚动条）、`tui_v2_expanded.png`
  （点击展开 thinking + 长 cwd 压缩）。SVG → PNG 用 `npx --yes sharp-cli`。
- 检查点：无异常空白带、标题不居中、同一 turn 单标签、工具卡 name 与摘要有分隔、diff 红绿、
  状态栏单行且长路径压缩。
- SVG 由字体渲染，个别间距（如 user 左边条的分段）与真实终端不同；以终端为准，截图只用于布局回归。

## 10. 测试

```
uv run pytest tests/tui -q
uv run ruff check src/deeppyro/tui tests/tui
```

`tests/tui/` 用 `App.run_test(size=...)` 挂最小 host（`ConversationHost`/`StatusHost`）或直接单测 widget，
不启动 kernel；覆盖：挂载前流式不丢字、同 turn 单标签、`clear` 重置、每 turn 一个 `AgentProcess`、
step 切换与工具行顺序、最新一句/长句截断、结束收起与 header 摘要、点击 step 打开查看器、点击 header
展开、点击工具行展开参数/结果、历史过程重放、工具行摘要/结果清洗/错误/孤儿结果 fallback、状态栏字段/
长 model/ctx 告警、活动栏空段隐藏与点击事件、计划分组、任务/团队空态与排序。

## 11. 已知限制与风险

1. diff 是“参数推断”而非真实文件 diff；不能保证与实际落盘内容一致。
2. 同 turn 单标签是启发式：以 `add_user` 为 turn 边界；若 agent 在同一 user 消息内产生多个
   独立回答块（例如多次 final），只有第一个带标签。
3. 历史回放会重放 agent 过程摘要，但展开状态不持久化（/resume 后默认折叠）；usage 明细不重放。
   - 工具行展开状态同样不持久化；`arguments` 只保留调用时的原始字符串。
4. 状态栏里的 rich Text 颜色是十六进制常量，不能引用 CSS 变量；改主题需同步改
   `status_bar.py` 与 `theme.tcss`。
5. 工具参数若非合法 JSON 且不是 Python 字面量，只显示原文摘要，不显示 diff。
6. 预览/截图依赖本机 `npx sharp-cli`；无终端环境下颜色与字符宽度可能与真实终端不同。
