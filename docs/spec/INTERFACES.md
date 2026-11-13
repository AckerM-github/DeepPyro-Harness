# DeepPyro Harness — 模块契约（INTERFACES）

> 本文件是并行开发的**唯一接口事实源**。任何跨模块调用都必须对齐这里给出的签名。
> Lead 拥有 `src/deeppyro/core/**`、`src/deeppyro/config.py`、`pyproject.toml`、`fixtures/`、`tools/` 的所有权；
> 其他目录见 §1 所有权表。需要改契约 → 先给 Lead 发消息，不要单方面改。

## 0. 目标与不变量

1. **纯 Python**，运行时不依赖 Node/Electron；仅 `tools/gen_fixtures.mjs` 用 Node 只读生成测试数据。
2. **日志兼容**：读写 DSH `session.v4.jsonl`，字段名逐字一致（camelCase），未知字段保留不丢。
3. **终端 only**：一次性 CLI + 交互式 TUI。不做 Web/Electron。
4. **日志是唯一事实源**（durable log first）：先 `append` 落盘，再发 UI 事件。
5. 一步（step）= 派生历史 + 工具 schema → 模型流式 → 工具流水线 → 结果落盘 → 下一步。
6. 取消是协作式的：保留已交付前缀，记录 `interrupt` 语义，不产生半条消息。

## 1. 目录与所有权

| 目录 | 所有者 | 内容 |
|---|---|---|
| `src/deeppyro/core/**` | **Lead** | wire 模型、runtime protocols、errors、ids（冻结） |
| `src/deeppyro/config.py` | **Lead** | 配置模型与加载 |
| `src/deeppyro/paths.py` | **Lead** | 用户目录解析（`~/.dpro` / `DEEPPYRO_HOME`）与旧数据迁移 |
| `src/deeppyro/session/**` | session-dev | 日志模型/读写/derive/fork/store |
| `src/deeppyro/llm/**` | llm-dev | OpenAI 兼容 provider、SSE、重试 |
| `src/deeppyro/tools/**`、`src/deeppyro/sandbox/**` | tools-dev | 工具注册表/流水线/内置工具/权限闸门 |
| `src/deeppyro/agent/**`、`src/deeppyro/prompt/**`、`src/deeppyro/compact/**` | loop-dev | turn/step 循环、事件、提示词、压缩 |
| `src/deeppyro/tui/**` | ui-dev/tui-dev | Textual 全屏 TUI（唯一前端）：widgets、命令、选择器、审批 |
| `src/deeppyro/headless.py` | **Lead** | 一次性 headless 运行（`--headless` / `--json`），自包含 |
| `docs/spec/*.md` | 各自 | 除本文件外，按模块各写一份 |
| `tests/<area>/**` | 各自 | 单测；跨模块集成测试归 Lead |
| `fixtures/**`、`tools/**` | **Lead** | 真实会话样本与生成器（只读使用） |

**禁止**：跨目录直接修改他人文件；在 `pyproject.toml` 加依赖（找 Lead）；改 `core/**`；把 fixtures 写回。

## 2. 通用约定

- Python 3.13，`from __future__ import annotations`，全异步优先用 `asyncio`。
- 路径一律 `pathlib.Path`；时间戳一律 int 毫秒。
- 公开 API 都在各包 `__init__.py` 里 re-export，便于外部导入。
- 依赖只用已装好的：httpx、pydantic v2、rich、textual、zstandard、typing-extensions、pytest/respx。typer、platformdirs 已移除。
- 风格：ruff line-length=110；pytest `asyncio_mode=auto`（async 测试不用加装饰器）。
- 每个模块自带 docstring；错误用 `deeppyro.core.errors` 里的类型，不要裸 `Exception`。

## 3. core（冻结，Lead 维护）

`deeppyro.core` 已导出：`Message/TextPart/ReasoningPart/ToolCallPart/ImagePart/ToolAdditionPart/ToolRemovalPart/UnknownPart/SessionHeader/LogRecord/WireModel`、
`LlmRequest/ToolSchema/TextDelta/ReasoningDelta/ToolCallStart/ToolCallArgsDelta/Usage/Finish/StreamEvent`、
`Tool/ToolResult/ToolContext/SessionLogLike/EventSink`、`errors`、`ids`。

要点：
- `Message.content` 是 **list[ContentPart]**，不是字符串；取文本用 `Message.text()`。
- `ToolCallPart.arguments` 是 **JSON 字符串**（与 DSH 一致），不是 dict。
- `LogRecord.data` 是 `dict`，额外信封键（`surfaceOp`、`sourceEventSeqs`）允许出现在模型里。
- `ToolContext.log(type, data)` 是同步回调，由 agent 层提供，用于追加日志记录。

## 4. session 模块（session-dev）

```python
from deeppyro.session import SessionStore, Session, SessionInfo, derive_messages

class SessionStore:
    def __init__(self, root: pathlib.Path | None = None) -> None  # None → config.session_dir()
    def create(self, cwd: pathlib.Path, *, agent_preset: str | None = None,
               session_id: str | None = None) -> Session
    def open(self, session_id: str) -> Session          # 支持 zstd：*.jsonl 与 *.jsonl.zstd
    def list(self) -> list[SessionInfo]                 # 新→旧
    def latest(self) -> SessionInfo | None

class Session:  # 实现 core.protocols.SessionLogLike
    header: SessionHeader
    records: list[LogRecord]
    path: pathlib.Path
    @property
    def session_id(self) -> str
    def append(self, type: str, data: dict, **envelope: object) -> int   # 返回 seq，立即落盘
    def derive_messages(self) -> list[Message]
    def close(self) -> None
    # 便捷统计（TUI/压缩需要）
    def usage(self) -> dict            # {"input":..,"output":..,"cached":..}
    def turn_count(self) -> int
    def last_step(self) -> tuple[int, int]   # (turn, step)

@dataclasses.dataclass
class SessionInfo:
    id: str; path: pathlib.Path; cwd: str; createdAt: int
    title: str | None; turns: int; recordCount: int; agentPreset: str | None
```

契约要求：
- `append` 自动填 `seq = last+1`、`time = now_ms()`，JSON 单行写入 + flush（崩溃安全），行尾 `\n`。
- `derive_messages()` 规则必须写进 `docs/spec/session.md`，覆盖：`system/message`、`user/message`、`assistant/message`、`tool/result`、`developer/message`、`agent/inbox/spliced`、`assistant/attempt`、压缩相关记录、`instruction` 类记录。**用 fixtures 验证**。
- 写入的 tool/result 必须带 `meta` 与 `sourceEventSeqs` 结构，与真实样本一致（fixtures/records/by-type/ 下有全部 42 种样本）。
- `open` 只读懒加载即可，但必须能处理 12MB zstd 会话。
- `fork`（在同包 `fork.py`）：`fork_session(store, src_id, *, at_seq=None) -> Session`，复制 header（新 id + parentSession + delegationDepth+1）与前缀记录。

## 5. llm 模块（llm-dev）

```python
from deeppyro.llm import LlmRegistry, OpenAICompatProvider, create_provider

class LlmRegistry:
    def __init__(self, cfg: Config) -> None
    def get(self, provider: str) -> LlmProvider
    async def aclose(self) -> None

def create_provider(name: str, pc: ProviderConfig, *, timeout_s: float = 600.0) -> LlmProvider

class OpenAICompatProvider:  # 实现 LlmProvider
    name: str
    def __init__(self, name, base_url: str, api_key: str | None, *,
                 model_ctx: int | None = None, timeout_s: float = 600.0) -> None
    async def stream(self, req: LlmRequest) -> AsyncIterator[StreamEvent]
    async def aclose(self) -> None
```

流式契约（**必须**遵守）：
1. `stream()` 是 async generator；正常路径**最后一个事件必为 `Finish`**。
2. 文本 → `TextDelta`；reasoning/思考 → `ReasoningDelta`（DeepSeek 的 `reasoning_content` 也走这里）。
3. 工具调用：先 `ToolCallStart(call_id, name)`，再若干 `ToolCallArgsDelta(call_id, delta)`；参数碎片原样转发，**拼接**由消费方做。
4. usage 在 `Finish` 之前若有则从 `Usage` 事件发出。
5. 传输/协议错误：首个 token 之前 → 抛 `LlmError(retryable=...) `；已有内容之后 → 发 `Finish(kind="error", failure={...})` 并结束。
6. 取消（`asyncio.CancelledError`）原样传播，不吞。
7. 请求体：`{model, messages, tools, stream:true, stream_options:{include_usage:true}}`；`system` 作为 role=system 消息放在最前（若 messages 已有 system 则不重复）。
8. 消息转换：`ToolCallPart.arguments` 字符串 → json.loads 放进 `tool_calls[].function.arguments`；tool 消息用 `tool_call_id`。
9. `retry.py`：`async with RetryPolicy(max_attempts=3, base_delay=2.0)` 包装 `stream` 的首个事件获取；只在 `retryable` 且**尚未产出内容**时重试，指数退避 + jitter。

## 6. tools + sandbox（tools-dev）

```python
from deeppyro.tools import ToolRegistry, builtin_tools, run_tool_batch, ToolCallRequest, ToolOutcome
from deeppyro.sandbox import PermissionGate, Decision, ApprovalRequest

class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None
    def register(self, tool: Tool) -> None
    def get(self, name: str) -> Tool | None
    def schemas(self) -> list[ToolSchema]      # 按注册顺序，稳定排序

def builtin_tools(cfg: Config, workspace: Path) -> list[Tool]

@dataclasses.dataclass
class ToolCallRequest:
    call_id: str; name: str; arguments: str   # 原始 JSON 字符串

@dataclasses.dataclass
class ToolOutcome:
    call_id: str; name: str; result: ToolResult; duration_ms: int

async def run_tool_batch(requests: list[ToolCallRequest], *, registry: ToolRegistry,
                         ctx_factory: Callable[[ToolCallRequest], ToolContext],
                         max_concurrency: int = 8) -> list[ToolOutcome]
```

流水线语义：
- `parallel_safe=True` 的工具可并发；`parallel_safe=False` 的工具是**屏障**：先等待此前所有调用结束，自己独占执行，之后的调用再开始。
- 结果按**调用顺序**返回（不是完成顺序）。
- 未知工具、参数 JSON 解析失败、`PermissionDenied`、`ToolError` 都转换为 `ToolResult(is_error=True, text=...)`，绝不抛出（除非 `CancelledError`）。
- 输出截断：单结果 `max_output_chars=30000`（超出保留头尾，中间写 `... [truncated N chars] ...`，并把完整输出落盘到 `services["spill_dir"]`，路径写进 meta.spillPath）。

内置工具清单（语义对齐 DSH，用 fixtures 里真实 tool/call 参数与 tool/result 文本校准）：
`read`（offset/limit，行号输出）、`write`、`edit`（old/new 字面替换，唯一匹配）、`glob`、`grep`（ripgrep 不可用则纯 Python 回退）、`pwsh`（Windows）/ `bash`、`web_search`、`web_fetch`、`todo_write`、`present`。
每个工具：`name/description/parameters(JSON Schema)/parallel_safe` + `async run(args, ctx) -> ToolResult`。

sandbox：
```python
Preset = Literal["read-only", "workspace-write", "danger-full-access"]
Approval = Literal["ask", "never", "always"]

@dataclasses.dataclass
class Decision:
    allowed: bool; reason: str = ""; needs_approval: bool = False

@dataclasses.dataclass
class ApprovalRequest:
    kind: str; summary: str; detail: str = ""; tool: str | None = None

class PermissionGate:
    def __init__(self, preset: Preset, approval: Approval, workspace: Path,
                 ask: Callable[[ApprovalRequest], Awaitable[bool]] | None = None) -> None
    def check_read(self, path: Path) -> Decision
    def check_write(self, path: Path) -> Decision
    def check_command(self, command: str) -> Decision
    async def request(self, req: ApprovalRequest) -> Decision
```
- `read-only`：写/命令一律拒绝；`workspace-write`：仅 workspace 内写允许，命令允许（危险命令仍拦）；`danger-full-access`：全放行。
- `approval="never"`：`needs_approval` 一律直接拒绝（reason 说明"approval policy is never"）。
- `approval="ask"`：调用 `ask` 回调（CLI 会把它实现成终端 y/n；非交互时按拒绝）。

## 7. prompt + agent + compact（loop-dev）

```python
from deeppyro.prompt import build_system_prompt, PromptContext
from deeppyro.agent import AgentSession, AgentOptions
from deeppyro.compact import Compactor

@dataclasses.dataclass
class PromptContext:
    cwd: Path; platform: str; session_id: str; started_at: int
    permission_preset: str; approval_policy: str; model: str
    instructions: list[str] = field(default_factory=list)   # AGENTS.md 等
    env: dict[str, str] = field(default_factory=dict)

def build_system_prompt(tools: list[ToolSchema], ctx: PromptContext) -> str

@dataclasses.dataclass
class AgentOptions:
    max_steps: int = 100
    max_tokens: int | None = None
    temperature: float | None = None
    compact_threshold: float = 0.85
    context_window: int = 32768

class AgentSession:
    def __init__(self, *, session: SessionLogLike, provider: LlmProvider,
                 registry: ToolRegistry, options: AgentOptions, sink: EventSink,
                 permission: PermissionGate, cwd: Path) -> None
    async def send(self, text: str) -> str          # 跑完一个 turn，返回最终文本
    async def interrupt(self) -> None               # 协作式取消；保留已落盘前缀
    @property
    def busy(self) -> bool
```

事件（`EventSink.emit(dict)`，字段名与 `dsh-headless --json` 对齐）：
`{"type":"session","sessionId":..}`、`{"type":"status","phase":"thinking|tool|done"}`、
`{"type":"text","text":..}`、`{"type":"thinking","text":..}`、
`{"type":"tool_call","callId":..,"name":..,"arguments":..}`、`{"type":"tool_result","callId":..,"isError":..,"text":..}`、
`{"type":"usage","input":..,"output":..,"cached":..}`、`{"type":"final","text":..}`、`{"type":"error","message":..}`。

落盘顺序（硬要求，测试会断言）：`turn/start` → 每步 `step/start` → `request/header`（首步）→ `assistant/message`（含 text/reasoning/tool-call parts）→ 每个调用 `tool/call` → 执行 → `tool/result` → `step/end` → … → `turn/end`。**事件在 append 之后发**。

compact：
```python
class Compactor:
    def __init__(self, *, provider: LlmProvider, model: str, keep_recent_tokens: int = 8000,
                 context_window: int = 32768, threshold: float = 0.85) -> None
    def should_compact(self, messages: list[Message]) -> bool
    async def compact(self, session: SessionLogLike, messages: list[Message]) -> bool
```
- token 估算：`len(text)/3.5 + 4/消息`（可替换为 provider usage）。
- 压缩是 region transaction。**落盘格式以 DSH 源码为准（不是 `session/compact*`），顺序固定**：
  1. `compaction/start` data `{compactionId, sourceCommandId?, turn?}`；
  2. `compaction/summary` data `{compactionId, sourceCommandId?, summary, rawOutput?, llmStreamCall?, shadowedRange:{start,end}, shadowedSeqs, shadowedTokenCount, provider, model, maxTokens?, usage?}`；
  3. `user/message` checkpoint（role=`user`，source `{"kind":"compact-checkpoint"}`），带 `surfaceOp={"op":"replace","startSeq":start,"endSeq":end}` 且 `sourceEventSeqs=[startEvent.seq, summaryEvent.seq, *shadowedSeqs]`；
  4. `compaction/end` data `{compactionId, sourceCommandId?, turn?, error?}`。
- 自动压缩必须在 open turn 内；手动压缩要求无 open turn。**不存在 `session/compact` 事件类型。**

**Sink 归属（唯一来源）**：`JsonLinesSink`/`RichSink` 属于 headless 前端层，唯一实现在 `deeppyro/headless.py`；TUI 用 `deeppyro/tui/sink.py` 的 `TuiSink`。`agent` 包只接收注入的 `EventSink`，不得定义或再导出任何 sink。测试需要 sink 时从 `deeppyro.headless` 导入，或写 5 行假 sink。

## 8. 终端前端：TUI + headless（Lead / ui-dev / tui-dev）

唯一产品形态是 Textual 全屏 TUI（`dpyro` 无参数启动）；一次性脚本路径是 headless（
`dpyro --headless "prompt"` 或 stdin 管道），二者都不做 Web UI。

```text
dpyro                               # 打开 TUI（唯一交互式产品形态）
dpyro --headless "prompt" [--model M] [--provider P] [--json] [--cwd DIR] [--max-steps N]
echo prompt | dpyro --headless --json
```

- 入口：`deeppyro.tui.main:main`（argparse；唯一 console script `dpyro`/`deeppyro`）。
- TUI：`deeppyro.tui.app.DeepPyroApp` + `TuiRuntime`（kernel 五服务 llm/sessions/sandbox/tools/agents），
  slash 命令见 `docs/spec/tui.md`；只依赖 `AgentSession` + `EventSink`，不得直接读写 session 文件。
- headless：`deeppyro/headless.py` 自包含（`run_once`/`boot_runtime`/`JsonLinesSink`/`RichSink`），
  `--json` 输出 NDJSON 一行一事件，stderr 只放错误，stdout 纯净（供脚本消费）。
- 退出码：0 成功；1 运行错误；2 参数/配置错误；130 用户中断。
- 已删除：typer 命令面、`src/deeppyro/cli/`、`tests/cli/`、`run/chat/sessions/config show` 子命令。

## 9. fixtures 与黄金对照（Lead 提供，只读）

> 注意：真实会话样本含个人数据，**不入库**（`fixtures/` 已 gitignore）。本地用
> `node tools/gen_fixtures.mjs` 生成；缺失时相关测试自动 skip。

- `fixtures/manifest.json`：30 个真实会话（42 种记录类型全覆盖），含 `trimmed`、`seqBreaks`、类型直方图。
- `fixtures/sessions/*.jsonl`：解码后的会话（超过 3MB 的只保留首 70 + 末 70 条，`trimmed: true`）。
- `fixtures/records/by-type/<safe_type>.jsonl`：每种记录类型的第一份真实样本（单行）。
- 重新生成：`node tools/gen_fixtures.mjs`（只读 `%USERPROFILE%\\.dsh\\sessions`）。

各模块必须用这些 fixtures 做测试：
- session：全部 30 个文件解析 + derive + 不变量（seq 单调处、tool/call↔tool/result 配对、消息结构合法）。
- llm：用 fixtures 里真实 `request/header` + `assistant/message` 构造请求/响应；respx mock SSE。
- tools：从 fixtures 提取真实工具调用参数做回归样本。
- agent：脚本化假 provider 驱动完整 turn，断言记录序列与事件顺序。
- headless：`tests/tui/test_headless.py` 用假 runtime 断言 `--json` NDJSON 与退出码。

## 10. 完成定义（DoD）

每个 workstream 交付：代码 + `docs/spec/<area>.md`（逆向出的语义与取舍）+ 测试（`uv run pytest tests/<area> -q` 全绿）+ `uv run python -c "import deeppyro.<area>"` 通过。
Lead 负责：`core`、`config`、集成测试（`tests/integration`）、端到端验收（假 provider 全链路 + 真 API 冒烟）。

## 11. 内核 kernel 与插件核心（Lead；详见 docs/spec/plugin-core.md）

### 11.1 vendor 与分层

- 内核用 PyPI `cordis-port 0.1.8` 的 Python 移植，**已 vendor** 到
  `src/deeppyro/_vendor/cordis_port/`（MIT，纯标准库，9 个 .py + LICENSE）。
- 不可插件化的核心：`core/wire.py`、`core/protocols.py`、`session/**`、`kernel/**`、`api/**`、
  vendor 的 cordis。其余全部是插件：`builtin/**` 是默认插件集，TUI/headless 是宿主。
- 领域模块禁止 import cordis；`kernel/**` 是唯一装配层；前端只通过 `App.service(name)` 取服务。

### 11.2 插件契约（`deeppyro.api`）

- `Plugin`：`plugin_id`/`version`/`api_version`/`inject`/`Config`(pydantic)/`setup(ctx, config)`；
  可选 `async stop()` 清理。
- `Service(Plugin, cordis Service)`：类属性 `provide`，构造即注册服务。
- `PluginContext`：`service/get/has`、`tools/prompt/commands/llm` 快捷属性、
  `effect/on/once/emit/plugin/inject/isolate/intercept`，以及
  `register_tool/register_prompt_section/register_command/register_provider`（注册随 fiber 自动注销）。
- 插件类型判定：`issubclass(cls, deeppyro.api.Plugin)` 走新适配器（pydantic Config 校验 +
  PluginContext + setup + stop disposer）；旧式 cordis 类直接 `ctx.plugin(cls, config)` 兼容。

### 11.3 组合：bundle + row + patch

- bundle = 含 `bundle.toml` 的目录：`[bundle]` 元数据 + 有序 `[[plugins]]` 行；
  `plugin` 支持 `module:Class` 或相对 bundle 目录的 `file.py:Class`。
- 行字段：`id/plugin/config/disabled/before/after/critical/name/isolate/intercept`。
- 合并语义：同 id → `config` **整体替换**（绝不深合并）、`disabled/critical` 覆盖、`isolate/intercept` 合并、
  插件类不可更换；新 id → 按 `before/after` 插入否则追加。
- 层顺序：core bundle → `~/.dpro/bundles/*/bundle.toml`（按 order,id）→ 显式 bundle 路径 → 用户 manifest
  （`~/.dpro/plugins.toml`）。
- core 行 `critical=true`：失败抛 `KernelBootError` 并 dispose 已启动 fiber；外部行默认非 critical：
  失败记录进 `App.failures`，boot 继续。

### 11.4 服务图（core bundle 行）

| 行 id | 模块 | 服务名 / 关键方法 |
|---|---|---|
| `llm` | `builtin.llm` | `llm`：`.provider(name)`、`.add_provider` |
| `sessions` | `builtin.sessions` | `sessions`：`.store`（SessionStore） |
| `sandbox` | `builtin.sandbox` | `sandbox`：`.gate(workspace, ask=None)` |
| `tools` | `builtin.tools` | `tools`：`.add(factory, scope=...)`、`.registry_for(workspace, scope=None)` |
| `tools-builtin` | `builtin.tools` | 11 个内置工具 |
| `presets` | `builtin.presets` | `presets`：`.add`、`.resolve(id)`、`.all()`、`.ids()` |
| `prompt` | `builtin.prompt_service` | `prompt`：`.add_section(..., scope=None)`、`.build(tools, prompt_context, scope=None)` |
| `prompt-core` | `builtin.prompt_core` | 5 个核心提示词段（order 10/20/30/40/50） |
| `commands` | `builtin.commands` | `commands`：`.add`、`.all()`、`.resolve()`、`.invoke()` |
| `commands-builtin` | `builtin.commands_builtin` | 11 个内置 slash 命令（含 `/preset`、`/panel`） |
| `agents` | `builtin.agents` | `agents`：`.create(..., preset=None)`、`.options(**overrides)` |
| `plugin-tools` | `tools.builtin.plugin_tools` | 4 个 agent 工具：`plugin_manager`/`plugin_inspect`/`settings_read`/`settings_write` |
| `slots` | `tui.slots` | `slots`：TUI 插件槽位注册表（状态栏项/对话行/面板/设置页） |
| `plugin-manager` | `builtin.plugin_manager` | `plugin_manager`：外部插件与 bundle（`.manager`） |
| `settings` | `builtin.settings` | `settings`：插件配置读写 + revision CAS（`.store`） |
| `inspect` | `builtin.inspect` | `inspect`：live services/plugins/tools/prompts/commands/providers/config schema |

### 11.5 API

```python
from deeppyro.kernel import boot, create_app, default_composition

app = await boot(cfg)                       # core + ~/.dpro/bundles + ~/.dpro/plugins.toml
app = await boot(cfg, bundle_paths=[...])   # 额外 bundle
app = await create_app(cfg, extra_plugins=[PluginSpec(...)])  # 测试/嵌入
app.service("tools")                       # 不存在/未激活 -> LookupError
app.started                                # {row_id: fiber state name}
app.failures                               # 非 critical 行的失败状态
await app.stop()                           # 逆序 dispose
```

### 11.6 测试

- `tests/kernel/`：假插件 boot、patch 顺序、critical 失败、`tests/kernel/plugins_dummy.py`。
- `tests/integration/test_plugin_composition.py`：临时目录外部 bundle 贡献工具/提示词/命令/provider。
- `tests/tui/test_commands.py`：插件贡献的 slash 命令进入 TUI 实时命令表并被执行。
- `tests/integration/test_plugin_services.py`：D 层四个工具经 `ToolContext.services` 取内核服务并真实执行；外部 bundle 注册 TUI 槽位后在 `slots` 服务可见。
- `tests/tui/test_slots.py`/`test_plugin_slots.py`：槽位注册、排序、去重、状态栏渲染与面板打开。

