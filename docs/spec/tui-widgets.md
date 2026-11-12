# TUI 对话渲染与状态栏规格（widgets）

状态：task-6 已实现；对应 M0 截图问题的修复已落地并有视觉验证。

本文件描述 `src/deeppyro/tui/widgets/` 中对话流、思考块、工具卡与状态栏的**渲染语义**，
以及从 fixtures 事件与终端截图逆向出的取舍。总体形态、键位与启动流程见 `docs/spec/tui.md`；
跨模块事件字段以 `docs/spec/INTERFACES.md` §7 为准。

## 1. 负责文件

| 文件 | 职责 |
| --- | --- |
| `widgets/conversation.py` | `ConversationView`：user/assistant/system 块、流式文本、同 turn 标签去重、工具卡挂载 |
| `widgets/thinking.py` | `ThinkingBlock`：思考流式尾部、结束折叠、点击展开 |
| `widgets/tool_card.py` | `ToolCallCard`：调用行摘要、结果预览、错误着色、write/edit diff 推断 |
| `widgets/status_bar.py` | `StatusBar`：单行状态；`short_cwd` 路径压缩 |
| `theme.tcss` | 调色板、user/assistant/tool/thinking/状态栏样式、Markdown 边距覆盖 |
| `tests/tui/test_conversation.py`、`test_tool_card.py`、`test_status_bar.py` | 组件与纯函数回归测试 |

公开 API 兼容性是硬约束：`ConversationView` 的 `add_user/add_info/add_error/stream_text/`
`stream_thinking/finish_assistant/add_tool_call/add_tool_result/clear` 方法名、参数与语义保持不变
（`app.py` 直接调用）；`StatusBar.show()` 的关键字参数保持不变。只做了新增（如 `ThinkingBlock`
的只读属性、`ToolCallCard.summary`）。

## 2. 事件到 widget 的映射

`app.py` 的 `handle_event` 把事件 dict 转成 widget 调用；widgets 不 import agent/llm/session。

| 事件 `type` | 字段（实际使用） | 渲染 |
| --- | --- | --- |
| `text` | `text` | 追加到当前 assistant 块，Markdown 增量预览 |
| `thinking` | `text` | 追加到当前 assistant 块的 `ThinkingBlock` |
| `tool_call` | `callId`、`name`、`arguments`（JSON 字符串） | 新建 `ToolCallCard`，按 `callId` 登记 |
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
- assistant 块：`AssistantBlock(show_label=...)`，包含可选 `DeepPyro` 标签、`ThinkingBlock`、
  `Markdown`。首个块懒挂载；流式文本 60ms 节流（`STREAM_REFRESH_SECONDS`），未挂载时先累积，
  `on_mount` 时强制刷新，因此“事件早于 mount”不会丢字或崩溃。
- 标签去重：同一 user turn 内只有一个 `DeepPyro` 标签。tool step 之间的过渡块、`final` 后新建的块
  都不再显示标签；下一次 `add_user`（或 `clear`）后恢复。工具调用/结果会先收尾当前 assistant 块，
  保证卡片顺序 = 事件顺序。
- Markdown 局部非法时回退为纯文本，绝不让渲染异常中断 turn。
- `clear()` 清空子节点、assistant/tool 引用与标签状态；历史回放复用 `stream_text + finish_assistant`，
  因此历史 assistant 消息也遵守同样的标签规则。
- `add_info` 为 dim 系统行；`add_error` 为红色 `error:` 行。

## 4. 思考块（ThinkingBlock）

- 流式：只显示最后 `STREAMING_LINES = 6` 行，单行超过 `STREAMING_LINE_CHARS = 120` 截断，
  dim italic。长思考不会把正文挤出屏幕。
- 结束（`finish()`，幂等）：折叠成一行 `· thinking (N chars)`；N >= 1000 显示 `1.2k`。
- 交互：折叠状态下点击展开，再点收起；展开上限 `EXPANDED_LINES = 40` / `EXPANDED_CHARS = 4000`
  （`compact_text`），避免超长 reasoning 卡死布局。
- 没有内容时 `display = False`，不占行。
- 历史回放不重放 thinking（`_render_history` 只处理 user/assistant/tool 文本），见 `tui.md` §10。

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

## 6. 状态栏（StatusBar）

单行字段顺序：`session <短id> · <provider>/<model> · <cwd>   ↑输入 ↓输出 [⚡缓存] · ctx N% · approval · phase`。

- 短 id：去掉 `session-` 前缀后前 8 位（fixtures 的 id 是 `session-<uuid>`，字面前 8 位无区分度）。
- cwd：`short_cwd` 超长时保留最后两段，如 `…\deep\nested\folder`；`MAX_CWD_CHARS = 42`。
- model：`MAX_MODEL_CHARS = 40`，超长截断加 `…`；provider 为空时只显示 model。
- ctx：`round(100 * input / context_window)`，>= 75% 用黄色；token 数量 k/M 缩写。
- 颜色：session 蓝、model 青、token 绿、ask 黄、phase 非 idle 蓝；其余 dim。
- 行级 `text-wrap: nowrap` + ellipsis，保证永远一行；不换行比截断选项更可预期。

## 7. 主题与 CSS 取舍

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

## 8. 视觉验证

- 预览脚本：`D:/dshworkspace/.dsh-tmp/tui_preview_v2.py`（假 runtime，固定 local 模型与假工具流，
  在创建 App 前 `os.environ.pop("NO_COLOR")`；否则 Textual 会去色）。
- 生成物：`tui_v2_normal.png`（流式正文 + 折叠 thinking + Markdown 标题/代码块）、
  `tui_v2_tools.png`（read/edit diff/pwsh 失败 + 错误行 + 滚动条）、`tui_v2_expanded.png`
  （点击展开 thinking + 长 cwd 压缩）。SVG → PNG 用 `npx --yes sharp-cli`。
- 检查点：无异常空白带、标题不居中、同一 turn 单标签、工具卡 name 与摘要有分隔、diff 红绿、
  状态栏单行且长路径压缩。
- SVG 由字体渲染，个别间距（如 user 左边条的分段）与真实终端不同；以终端为准，截图只用于布局回归。

## 9. 测试

```
uv run pytest tests/tui -q
uv run ruff check src/deeppyro/tui tests/tui
```

`tests/tui/` 用 `App.run_test(size=...)` 挂最小 host（`ConversationHost`/`StatusHost`），不启动
kernel；覆盖：挂载前流式不丢字、同 turn 单标签、`clear` 重置、thinking 尾部行数/折叠/点击展开、
工具卡摘要/结果清洗/错误/diff/孤儿结果 fallback、状态栏字段/长 cwd/长 model/ctx 告警。

## 10. 已知限制与风险

1. diff 是“参数推断”而非真实文件 diff；不能保证与实际落盘内容一致。
2. 同 turn 单标签是启发式：以 `add_user` 为 turn 边界；若 agent 在同一 user 消息内产生多个
   独立回答块（例如多次 final），只有第一个带标签。
3. 历史回放不重放 thinking/usage 明细，因此展开状态无法在 /resume 后恢复。
4. 状态栏 rich Text 的颜色是十六进制常量，不能引用 CSS 变量；改主题需同步改 `status_bar.py`。
5. 工具参数若非合法 JSON 且不是 Python 字面量，只显示原文摘要，不显示 diff。
6. 预览/截图依赖本机 `npx sharp-cli`；无终端环境下颜色与字符宽度可能与真实终端不同。
