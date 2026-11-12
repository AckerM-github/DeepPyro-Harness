# TUI 输入层：多行输入、补全、历史、命令注册表与模态框

本文件描述 TUI 输入层（task-7）的模块、键位、数据格式与对 app.py 的集成契约。
范围：src/deeppyro/tui/widgets/prompt_input.py、widgets/prompt_area.py、
widgets/completion.py、widgets/history.py、commands.py、screens/picker.py、
screens/approval.py，以及 tests/tui/test_prompt.py、test_commands.py、
test_picker.py、test_approval.py。

## 1. 模块一览

| 文件 | 作用 |
| --- | --- |
| widgets/prompt_input.py | 旧单行 PromptInput（Input 子类，id="prompt"），保持可用 |
| widgets/prompt_area.py | PromptArea：多行输入、Shift+Enter 换行、自动增高、历史回放、斜杠补全 |
| widgets/completion.py | CompletionPopup（OptionList 子类）与 completion_prefix() 解析 |
| widgets/history.py | History：JSONL 持久化，上限 200 条 |
| commands.py | Command 注册表、parse()、candidates()、dispatch()、HELP_TEXT |
| screens/picker.py | PickerScreen：通用过滤列表模态（/resume、/model 选择器） |
| screens/approval.py | ApprovalScreen：审批模态，含风险徽标 |

## 2. PromptArea（多行输入）

PromptArea 是 textual.widgets.TextArea 的子类，id 固定为 "prompt"，默认占位符
沿用 widgets/prompt_input.py 的 PLACEHOLDER。

### 2.1 键位

| 键 | 行为 |
| --- | --- |
| Enter | 提交非空文本并 post PromptArea.Submitted(text)；空/纯空白不提交 |
| Shift+Enter（或 Ctrl+J） | 插入换行，输入框自动增高 |
| ↑ / ↓ | 补全浮层打开时切换候选项；否则在首行按 ↑ 回放历史、在末行按 ↓ 前进历史 |
| Tab | 补全浮层打开时接受当前候选；否则保持 TextArea 默认行为（焦点移动） |
| Esc | 关闭补全浮层 |
| 其他键 | 交给 TextArea 默认处理（光标移动、编辑、选择等） |

回放历史时，第一次按 ↑ 会把当前编辑内容保存为草稿；一路按 ↓ 回到最新一条后
再按 ↓ 会恢复这份草稿。回放历史不会重新打开补全浮层。

### 2.2 自动增高

高度由视觉行数决定，最多 8 行内容加 2 行边框：

    visible = min(MAX_VISIBLE_LINES, max(1, wrapped_document.height))
    styles.height = visible + BORDER_HEIGHT

MAX_VISIBLE_LINES = 8，BORDER_HEIGHT = 2。高度通过 inline styles.height 设置，
优先级高于 theme.tcss 中 #prompt 的 height: 3，所以初始 3 行、输入多行后自动变高、
超过 8 行封顶并内部滚动。

### 2.3 Submitted 消息

    class PromptArea(TextArea):
        class Submitted(Message):
            text: str

PromptArea 在提交前已经清空自己并把提交文本写入 history，因此 app 侧不需要再
清空输入框、也不应重复写历史。

### 2.4 斜杠补全

仅当满足以下条件时才打开补全浮层：

- 文本是单行（不含换行）；
- 以 / 开头；
- 第一个 token（去掉开头的 / 之后）不含空格、制表符或第二个 /。

即 /model 后跟参数、多行文本、普通消息都不会触发补全。候选来源是
commands.candidates(prefix)，按注册表顺序显示，同时匹配命令名与别名（前缀匹配）。
若唯一候选与当前输入完全相等（名字或别名），浮层自动关闭，直接回车即可提交。
浮层是 CompletionPopup（OptionList 子类，不抢焦点），挂载在 screen 上、绝对定位
在输入框上方，高度为 min(候选数, 8) + 2，宽度 28..72。焦点始终留在 PromptArea。
浮层打开时 Enter/Tab 只接受候选（把输入替换为 "/<name> "），不会提交消息。

## 3. 历史持久化（widgets/history.py）

- 路径优先级：环境变量 DEEPPYRO_HISTORY（完整文件路径）> ~/.dpro/history.jsonl
  （DEEPPYRO_HOME 可移动整个用户目录）。
- 文件格式：JSON Lines，每行一个对象，例如
  {"text": "hello", "at": "2026-11-12T22:39:26+00:00"}。
- 上限 MAX_HISTORY = 200：超出后丢弃最旧的记录；只保留最近 200 条。
- append() 会 rstrip、跳过纯空白、跳过与上一条完全相同的连续重复。
- save() 先写同目录 .tmp 再 os.replace 原子替换；任何 OSError 都静默忽略
  （历史是便利功能，绝不能阻止用户提交输入）。
- load() 容忍坏行：空行、非字符串 JSON、缺少 text 字段的对象忽略；无法解析为
  JSON 的行按纯文本历史条目处理（兼容手工编辑的文本文件）；读取失败同样静默。

## 4. 命令注册表（commands.py）

    @dataclass(frozen=True)
    class Command:
        name: str
        aliases: tuple[str, ...]
        summary: str
        usage: str
        handler: Callable[[Any, str], Awaitable[None]]

    COMMANDS: list[Command]  # 按帮助文本顺序注册

    parse(line) -> (Command | None, str)
    candidates(prefix) -> list[Command]
    async dispatch(app, line) -> bool

- parse()：去掉首尾空白与可选的前导 /，按第一个空格切出参数；命令名大小写不敏感，
  可通过别名匹配；未知命令返回 (None, "")。
- candidates()：按 name 或别名前缀过滤，保持 COMMANDS 顺序；空前缀返回全部。
- dispatch()：找到命令就调用 handler（await 协程），返回 True；未知命令返回 False。
  app.py 现有的 _run_command 兼容（未知命令由 app 渲染错误提示）。
- HELP_TEXT 由 COMMANDS 生成，行为与旧硬编码文本一致：首行 Commands，每行
  "  " + usage.ljust(18) + " " + summary，末行 Anything else is sent to the model.
- /plugins 仍在注册表内，handler 转发 app.show_plugins()。

当前命令（顺序）：/help（别名 ?、h）、/new、/resume [id|index]、/model [id]、
/status、/compact、/clear、/plugins、/exit（别名 quit、q）。

## 5. PickerScreen（screens/picker.py）

    PickerItem = tuple[str, str, str]  # (value, label, note)
    PickerScreen(title: str, items: Sequence[PickerItem])

ModalScreen[str | None]：顶部标题、过滤输入框、OptionList 列表。输入内容对
value/label/note 三个字段做大小写不敏感的子串过滤；无匹配时显示 No matches 提示。

- ↑ / ↓：移动高亮；Enter：选中高亮项并 dismiss(value)；
- Esc：dismiss(None)；
- 直接点击列表项也会 dismiss(value)；
- 列表为空时 Enter/Esc 都返回 None。

/resume 与 /model 的无参分支由 app.py 调用，示例：

    choice = await self.push_screen_wait(PickerScreen("Resume a session", items))
    if choice is not None:
        await self.resume(choice)

## 6. ApprovalScreen 精修（screens/approval.py）

对外契约保持不变：ALLOW_ONCE = "once"、ALLOW_ALWAYS = "always"、DENY = "deny"，
类名 ApprovalScreen、构造参数 request、按钮 id allow-once/allow-always/deny 全部不变，
app.py 的 ask_approval() 无需修改。

新增内容：

- 显示请求分类标题、工具名（Tool: shell / filesystem / sandbox，优先取 request.tool）、
  参数摘要（summary）、细节（detail，超长截断到 DETAIL_LIMIT = 500 字符）、风险徽标。
- 风险分级 risk_level(request) -> RISK_DANGER / RISK_WARN（RISK_DANGER = "danger"、
  RISK_WARN = "warn"）：
  - kind == "command-approval" 且 detail 命中 sandbox.gate.DANGEROUS_PATTERNS 中任一
    模式（rm -rf、format、git push --force、shutdown 等 9 类）→ DANGER，徽标显示
    DANGER · <危险模式标签>，红色（$text-error，类 risk-danger）；
  - kind == "sandbox-escalation" 且 summary 含 danger-full-access → DANGER；
  - 其余 → WARN，黄色 WARNING（$text-warning，类 risk-warn）。
  危险模式来自 deeppyro.sandbox.gate.DANGEROUS_PATTERNS，保持单一来源。
- 键位：y = allow once、a = always、n / Esc = deny；三个按钮可聚焦，初始焦点在
  Allow once，Tab/Shift+Tab 在按钮间移动，Enter/Space 触发按钮。
- 兼容对象属性与 Mapping（测试可直接传 dict）。

## 7. 与 app.py 的集成契约（Lead 负责改 app.py）

当前 app.py 仍使用 PromptInput，现有行为不受影响；tests/tui/test_app.py 全部通过。
切换 PromptArea 时按下面 6 处修改（行号基于当前 437 行版本，仅作参考）：

1. 第 22 行：删除 textual.widgets 导入中的 Input（若 app.py 不再用 Input）。
2. 第 29 行：把 from deeppyro.tui.widgets.prompt_input import PromptInput 改为
   from deeppyro.tui.widgets.prompt_area import PromptArea。
3. 第 63 行：self._prompt: PromptArea | None = None。
4. 第 74 行：PromptInput() 改为 PromptArea()。
5. 第 78 行：self.query_one(PromptInput) 改为 self.query_one(PromptArea)；
   第 79 行 focus() 不变。
6. 第 157-173 行：@on(Input.Submitted) 改为 @on(PromptArea.Submitted)，参数类型改为
   PromptArea.Submitted，事件文本用 event.text；PromptArea 提交前已清空自己，删除
   event.input.value = "" 那一行，其余逻辑（strip、working 判断、/ 命令分发）不变。
   第 210-212 行的 Ctrl+C 清空分支：prompt.value 改为 prompt.text，清空用
   prompt.text = ""。

补充说明：

- PromptArea 的 id 仍是 "prompt"，theme.tcss 的 #prompt 边框/背景/边距规则继续生效；
  动态高度通过 inline style 覆盖 theme 的 height: 3。
- 补全浮层由 PromptArea 在 on_mount 时挂到 screen 上，不依赖 app.py 额外代码。
- /resume 与 /model 的无参选择器需要 app.py 自己 push_screen_wait(PickerScreen(...))；
  PickerScreen 已就绪但 app.py 属于 Lead 的写域。
- 若 Lead 暂时不切换，PromptInput 与旧 @on(Input.Submitted) 路径完全可用，只是没有
  多行输入、历史和补全功能。

## 8. 测试与验收

新增 53 个测试：

- tests/tui/test_prompt.py（20）：提交/清空、空输入忽略、Shift+Enter 换行、高度封顶 8 行、
  历史回放与草稿恢复、跨进程持久化、回放不弹补全、200 条上限、坏行容忍、保存失败静默、
  路径优先级、补全打开/过滤/↑↓/Tab/Enter 接受/Esc 关闭、精确命令关闭、未知与多行不触发。
- tests/tui/test_commands.py（12）：注册表顺序与元数据、别名、handler 可等待、
  HELP_TEXT 结构、parse 各分支、candidates、dispatch 全命令/别名/参数/未知。
- tests/tui/test_picker.py（8）：初始渲染与焦点、过滤、value/note 匹配、↑↓、Enter 返回、
  Esc 返回 None、无匹配、空列表。
- tests/tui/test_approval.py（13）：常量稳定、危险模式来源、风险分级、红/黄徽标、
  工具名回退、y/a/n/Esc、初始焦点与 Tab 循环、按钮点击、detail 截断与可省略、dict 请求。

验收命令：

    uv run pytest tests/tui -q
    uv run ruff check src/deeppyro/tui tests/tui

两项均通过。测试全部使用 Textual App.run_test，不依赖真实终端、不调用真实模型或
8000 端口；PromptArea 测试通过 history_path 参数注入 tmp_path，不写用户目录。

## 9. 已知限制与风险

- widget 中没有定义名为 _render 的方法（Textual 私有名，曾导致渲染崩溃）。
- 新组件的 DEFAULT_CSS 只使用 Textual 标准 design tokens（$text-error、$text-warning、
  $panel、$background 等），不引用 theme.tcss 的自定义变量；独立测试 App 也能正常渲染。
  theme.tcss 已有的 #prompt、#approval-*、#picker-* 规则按 id 继续生效。
- PromptArea 是 TextArea 子类，主题里的 #prompt-placeholder 规则对 TextArea 的
  placeholder 生效方式可能与 Input 略有差异；占位符文本与旧 PromptInput 一致。
- 历史文件按用户全局共享（同一台机器上的所有仓库/会话），去重只针对连续重复。
- 补全浮层目前只补全命令名/别名，不补全命令参数。
