# TUI 对话渲染、活动栏与状态栏规格（widgets）

状态：M1/M2/M3 已实现；对话流按 user turn 组织，思考改为每 turn 一个 TurnProgress + 全屏查看器，
新增活动栏、计划/任务/团队面板；M0 截图问题修复、状态栏字段精简、设置改走 slash 命令（/policy
/approval /cwd）后已移除控制栏，视觉验证通过。

本文件描述 `src/deeppyro/tui/widgets/` 与相关 screens 的**渲染语义**，以及从 fixtures 事件与终端
截图逆向出的取舍。总体形态、键位与启动流程见 `docs/spec/tui.md`；跨模块事件字段以
`docs/spec/INTERFACES.md` §7 为准。

## 1. 负责文件

| 文件 | 职责 |
| --- | --- |
| `widgets/conversation.py` | `ConversationView`：user/assistant/system 块、流式文本、同 turn 标签去重、工具卡挂载、每 turn 一个 TurnProgress |
| `widgets/turn_progress.py` | `TurnProgress`：单行思考进度（当前 step 最新一句）、结束摘要、点击打开查看器 |
| `screens/thinking.py` | `ThinkingScreen`：全屏按 Turn/Step 查看完整 reasoning |
| `tui/history.py` | `ReasoningStep`/`TurnReasoning`/`reasoning_from_session`/`reasoning_map`：从 durable 日志重建 reasoning |
| `widgets/activity_bar.py` | `ActivityBar`：plan/jobs/agents 三段活动栏（空段隐藏，点击发事件） |
| `screens/plan.py` | `PlanScreen`：待办按 in_progress / pending / completed 分组 |
| `screens/jobs.py` / `screens/team.py` | `JobsPanelScreen` / `TeamPanelScreen` / `TaskBoardScreen`：只读快照 |
| `widgets/tool_card.py` | `ToolCallCard`：调用行摘要、结果预览、错误着色、write/edit diff 推断 |
| `widgets/status_bar.py` | `StatusBar`：单行状态（session/model/tokens/ctx/policy/phase） |
| `theme.tcss` | 调色板、user/assistant/tool/活动栏/状态栏样式、Markdown 边距覆盖 |
| `tests/tui/` | test_conversation / test_tool_card / test_status_bar / test_turn_progress / test_thinking_screen / test_activity_bar / test_plan_screen / test_jobs_screen / test_team_screen / test_history |

公开 API 兼容性是硬约束：`ConversationView` 的 `add_user/add_info/add_error/stream_text/`
`stream_thinking/finish_assistant/add_tool_call/add_tool_result/clear` 方法名与参数保持不变（`app.py`
直接调用）；在此基础上新增 `begin_step(turn, step)`、`finish_turn(final_text)`、`current_reasoning`、
`progress`、`add_history_reasoning(TurnReasoning)`。`StatusBar.show()` 的关键字参数已精简为
`session_id/provider/model/input_tokens/output_tokens/cached_tokens/context_window/phase`
（`cwd` 与 `approval` 改由 slash 命令 /cwd 与 /approval 管理，不再进状态栏）。只做了新增（如 `ThinkingBlock` 的只读属性、`ToolCallCard.summary`）。

## 2. 事件到 widget 的映射

`app.py` 的 `handle_event` 把事件 dict 转成 widget 调用；widgets 不 import agent/llm/session。

| 事件 `type` | 字段（实际使用） | 渲染 |
| --- | --- | --- |
| `text` | `text` | 追加到当前 assistant 块，Markdown 增量预览 |
| `step` | `turn`、`step` | `ConversationView.begin_step`：同一 user turn 复用同一个 `TurnProgress`，切到新 step（UI-only hint，紧跟 durable `step/start`） |
| `thinking` | `text` | 追加到当前 turn 的 `TurnProgress`（单行只显示当前 step 最新一句） |
| `final` | `text` | `finish_turn`：定稿 assistant 文本并结束 `TurnProgress` |
| `tool_call` | `callId`、`name`、`arguments`（JSON 字符串） | 新建 `ToolCallCard`，按 `callId` 登记（只收尾 assistant，不结束 progress） |
| `tool_result` | `callId`、`isError`、`text` | 回填对应卡片；找不到调用时建 fallback 卡片 `tool` |
| `final` | `text` | 用最终文本覆盖增量文本并收尾 |
| `error` | `message` | `add_error` 红色系统行 |
| `status` | `phase` | 只更新状态栏 phase |
| `usage` | `input` 等 | app 记上下文占用；status 刷新 |

语义依据：fixtures 中 `tool/call` 的 `arguments` 是 JSON 字符串，`tool/result` 通过
`toolCallId` 关联；`assistant/message` 的 content 同时含 reasoning 与 text part，因此思考与正文
必须是两个独立显示区。`app.py` 在转发时统一 `str()`，所以 `arguments` 也可能是 Python repr
（`{'path': ...}`），解析层同时接受 JSON 与 `ast.literal_eval`。

## 3. 对话流（ConversationView）

- user 块：`Vertical` 内含 `You` 标签与正文 `Static`；左侧 `thick $accent` 竖条、`$surface-2`
  背景、宽度 `1fr` 自动换行。`add_user` 同时把“本 turn 是否已有 assistant 块”重置为 False。
- assistant 块：`AssistantBlock(show_label=...)` 只含可选 `DeepPyro` 标签与 `Markdown`；思考由独立的
  `TurnProgress` 承载，不再嵌在 assistant 块内。首个块懒挂载；流式文本 60ms 节流
  （`STREAM_REFRESH_SECONDS`），未挂载时先累积，`on_mount` 时强制刷新，因此“事件早于 mount”不会
  丢字或崩溃。
- 标签去重：同一 user turn 内只有一个 `DeepPyro` 标签。tool step 之间的过渡块、`final` 后新建的块
  都不再显示标签；下一次 `add_user`（或 `clear`）后恢复。工具调用/结果会先收尾当前 assistant 块，
  保证卡片顺序 = 事件顺序。
- Markdown 局部非法时回退为纯文本，绝不让渲染异常中断 turn。
- `clear()` 清空子节点、assistant/tool 引用与标签状态；历史回放复用 `stream_text + finish_assistant`，
  因此历史 assistant 消息也遵守同样的标签规则。
- `add_info` 为 dim 系统行；`add_error` 为红色 `error:` 行。

## 4. 思考进度（TurnProgress）与查看器

- 每个 **user turn** 只有一个 `TurnProgress`，而不是每个 assistant step 一个；`begin_step(turn, step)`
  在 turn 变化或上一条已结束时新建，否则复用并切换 step。
- 流式行：`✻ ` + 当前 step 的最新完整句（按句末标点/换行切句），单行上限 120 字符，超长保留尾部
  并加 `…`；dim italic，保证“只展示当前步骤”的滚动感。
- 结束行：`finish()` 幂等，折叠为 `· thinking · N steps · M sentences  (click to view)`（单复数）。
- 交互：点击有内容的结束行发 `TurnProgress.ThinkingRequested(reasoning)`；app 打开
  `ThinkingScreen([reasoning])`，按 Turn/Step 显示完整文本（markup=False，可滚动）。
- 没有 reasoning 时 `display = False`；turn 内 assistant 正文与工具卡不会结束 progress，只有 `final`
  或下一条 user 消息会收尾。
- 历史回放：`reasoning_map(session)` 从 `turn/start` + `assistant/message` 的 reasoning part 重建每个
  turn，`_render_history` 在 user 消息后 `add_history_reasoning` 挂一条已完成的行，可点击查看。
- 完整 reasoning 已由 AgentSession 持久化在 `assistant/message`，查看器不新增存储格式。

## 5. 工具卡（ToolCallCard）

一张卡 = 调用行（`.tool-call`）+ 结果行（`.tool-result-line`），同一 `callId` 原地回填，不新开块。

调用行：`→ name: 摘要`，其中摘要规则：

| 工具名 | 摘要 |
| --- | --- |
| `read`/`write`/`edit`/`multi_edit`/`multiedit` | `path`/`file_path`/`filePath`/`file`/`filename` 的值 |
| `bash`/`sh`/`shell`/`pwsh`/`powershell`/`cmd`/`exec`/`command` | `command`/`cmd`/`script`/`input` 前 60 字 |
| 其它 | 紧凑 JSON 前 80 字；无法解析则原文前 80 字 |

参数解析顺序：JSON → Python 字面量 → `{}`。解析失败不抛异常，退化为原文摘要。

结果行：

- 成功：`✓ name`，最多 3 行 / 400 字预览，dim；失败：`✗ name  failed`，整行红。
- 预览前剥离传输包裹：整段 `<content>...</content>` / `<output>...</output>` 直接去掉外层；
  文件系统工具的多段信封（`<path>..</path>` + `<type>..</type>` + `<content>..</content>` + footer）
  只保留 `<content>` 内的载荷，footer（`End of file` / `Showing lines ...`）不进预览。
  无法识别的包裹保持原文。
- write/edit 成功且能从 arguments 推断时显示 diff：edit 取 `old/new`、`old_string/new_string`、
  `oldString/newString`、`old_text/new_text`、`before/after` 或 `edits` 列表；write 取
  `content/new_text/text/new_string` 作为新增行。每方向最多 3 行、单行 110 字；header 显示 `-N +M`。
- diff 仅用于成功结果；错误优先红色预览。

**限制**：diff 由调用参数推断，不是真实文件 diff（未读文件、未做上下文合并）；多段 edit 只展示
首尾片段；无法解析参数时只有普通结果预览。这些都在卡片上以“摘要”而非“权威 diff”呈现。

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
不启动 kernel；覆盖：挂载前流式不丢字、同 turn 单标签、`clear` 重置、每 turn 一个 progress、step
切换、最新一句/长句截断、结束摘要与点击打开查看器、历史 reasoning 重放、工具卡摘要/结果清洗/错误/diff/
孤儿结果 fallback、状态栏字段/长 model/ctx 告警、活动栏空段隐藏与点击事件、计划分组、任务/团队空态与
排序。

## 11. 已知限制与风险

1. diff 是“参数推断”而非真实文件 diff；不能保证与实际落盘内容一致。
2. 同 turn 单标签是启发式：以 `add_user` 为 turn 边界；若 agent 在同一 user 消息内产生多个
   独立回答块（例如多次 final），只有第一个带标签。
3. 历史回放会重放 reasoning 摘要，但展开状态不持久化（/resume 后默认折叠）；usage 明细不重放。
4. 状态栏里的 rich Text 颜色是十六进制常量，不能引用 CSS 变量；改主题需同步改
   `status_bar.py` 与 `theme.tcss`。
5. 工具参数若非合法 JSON 且不是 Python 字面量，只显示原文摘要，不显示 diff。
6. 预览/截图依赖本机 `npx sharp-cli`；无终端环境下颜色与字符宽度可能与真实终端不同。
