# tools/spec/ptc.md — Python PTC（run_code 呈现模式）

> 状态：与 DSH 的 PTC 架构对齐（呈现模式 `native`/`ptc`、自动 Python SDK、`run_code` 保留工具、
> 内层工具走完整流水线、执行器并发与额度、runtime seam、失败分类）。默认是 `native` 呈现，
> 因此默认行为与没有 PTC 时完全一致。启用方式有两种：**推荐**在会话里 `/preset ptc`
> （per-session agent preset，见 `docs/spec/agent-presets.md` §2），或全局配置
> `[ptc].mode = "ptc"`（所有会话）。
>
> 对齐范围：`dsh-tools` 的呈现模式、`dsh-tool-executor-ptc` 的 SDK 渲染与配额语义、
> `dsh-ptc-runtime` 的 resolve/run seam 与失败分类。SDK 文本由 DSH 源码生成的 6 个 golden
> 逐字节校准（见 §13）。本实现仍不是 DSH 的 Node/vm-script runtime，细节差异见 §12。

## 1. 目标与形态

### 1.1 两种呈现模式

| mode | 模型直接可见的工具 | 说明 |
| --- | --- | --- |
| `native`（默认） | 全部工具 | 传统逐次 tool call；`run_code` 不注册，提示词里也没有 SDK。 |
| `ptc` | 只有 `run_code` | 模型写一段 Python 程序；程序通过 `tools` binding 调用其余工具。提示词注入自动生成的 Python SDK。 |

`ptc` 模式下 “模型写程序 → 程序调工具 → 只把程序输出回模型上下文” 是核心收益：一次模型响应可以
完成多步工具编排，中间结果不进入对话历史。

呈现模式由 agent preset 的 `presentation` 字段决定：内置 preset `ptc` 的
`presentation = "ptc"`，在会话里 `/preset ptc` 即可，无需改配置，也不影响其他会话。全局
`[ptc].mode = "ptc"` 保留给「所有会话都用 PTC」的宿主：它让默认模式（`standard`/旧 `default`）
preset 也使用 `run_code`-only 呈现，但对 `minimal`/`cordis` 等没有 `run_code` 的 preset 不生效。

### 1.2 程序化调用的语义

- `run_code` 参数是 `code`（Python 源码）与 `description`（一句话说明），可选 `timeoutMs`。
- 每次调用启动一个**全新的 CPython 子进程**（`sys.executable -I -S -X utf8 worker.py`），
  用 JSON-line stdio 协议通信；没有持久 worker、没有跨调用状态。
- 程序是 async 函数体，可用 top-level `await` 与 `return`；返回值必须是 lossless JSON。
- 程序内只绑定 `tools` 与 `ToolCallError` 两个名字。工具调用形如
  `await tools.read({"file_path": "a.txt"})`，也可以用 `tools["read"](...)` 访问特殊名字。
- 内层工具调用走**普通工具管线**（参数解码、权限闸门、pre/execute/post hooks、输出保留），
  但**不是**模型可见的 tool call：只写 `tool/ptc-dispatch-start` / `tool/ptc-dispatch` 日志，
  不进 surface fold。
- 内层工具失败（含权限拒绝、工具自身报错）变成 `ToolCallError`，程序可以 `try/except` 处理；
  额度耗尽则是整次 run 的致命失败。
- 图片类内层结果不能塞进 tool message，收集为 deferred parts，由 `run_code` 的 `ToolResult.parts`
  交给上层；会话把图片作为 `source={"kind": "ptc-mode"}` 的 user 消息追加到 `tool/result` 之后。

### 1.3 非目标

- 不是操作系统级沙箱，也不是安全边界（见 §11）。
- 不做 streaming logs/progress、持久 worker、image context 注入（图片是 deferred 附件，不是程序输入）。
- 不实现 DSH 的 Node vm-script / `maxOldGenerationSizeMb` / `maxMessageBytes`。

## 2. 文件布局与公开 API

| 文件 | 内容 |
| --- | --- |
| `src/deeppyro/ptc/types.py` | `PtcConfig`、`PtcRunSpec`、`PtcRunResult`、`PtcFailure`、`PtcBindingError`、失败 kind 与默认常量 |
| `src/deeppyro/ptc/worker.py` | 独立 worker 脚本（只用 stdlib；协议流固定 `sys.__stdout__`） |
| `src/deeppyro/ptc/runtime.py` | `PtcRuntime`（服务名 `ptc`）与 `Binding`、`WORKER_PATH` |
| `src/deeppyro/ptc/executor.py` | `NestedDispatcher`：binding 生成、单次调用流水线、并发/额度、dispatch 记录 |
| `src/deeppyro/ptc/tool.py` | `RunCodeTool`（模型可见工具） |
| `src/deeppyro/ptc/sdk.py` | `render_tools_sdk`：从 `ToolSchema` 生成 Python SDK 提示词段 |
| `src/deeppyro/builtin/ptc.py` | `PtcPlugin`：提供 `ptc` 服务；全局 `mode = "ptc"` 时注册全局 `run_code` 与两个提示词段，`mode = "native"` 时以 `scope="ptc"` 注册（供 `ptc` preset 使用） |

公开入口（`deeppyro.ptc` re-export 全部）：

```python
from deeppyro.ptc import (
    PtcConfig, PtcRunSpec, PtcRunResult, PtcFailure, PtcBindingError,
    PtcRuntime, PtcService, RunCodeTool, NestedDispatcher,
    RUN_CODE_NAME, NESTED_EXCLUDED_TOOLS, WORKER_PATH, PTC_FAILURE_KINDS, PTC_MODES,
)

svc = PtcRuntime(PtcConfig(mode="ptc"))
svc.mode                 # "native" | "ptc"
svc.language             # "python"
svc.program_format       # "python"
svc.sandbox_mode         # None：PTC 自身没有 OS 沙箱策略
svc.execution_instructions -> str
svc.timeout              # {"defaultMs": int, "maxMs": int}
svc.resolve(request, *, cwd=None) -> PtcRunSpec
async svc.run(spec, *, bindings: Mapping[str, Binding] | None, ctx: ToolContext) -> PtcRunResult
```

`PtcRuntime.provide = "ptc"`、`inject = ["tools", "sandbox"]`；它不是 `api.Service` 子类，
由 `PtcPlugin` 显式 `ctx.provide`。

## 3. 配置

`[ptc]` 表从 bundle 行 config 与 kernel `[ptc]` 合并，kernel 覆盖行 config；同时接受
camelCase（DSH 形态）与 snake_case 别名，未知键抛 `ConfigError`。

| 键（camel / snake） | 默认 | 语义 |
| --- | --- | --- |
| `mode` | `"native"` | `native` 或 `ptc`；决定**全局**呈现模式。`preset ptc` 的呈现由 preset 的
  `presentation` 字段决定，不受此值影响（全局 `ptc` 时 `standard` 也走 PTC）。 |
| `enabled` | 由 mode 派生 | 旧布尔开关，仅在未写 `mode` 时生效（`enabled=true` → `ptc`）。 |
| `executionMode` / `execution_mode` | `"per-call"` | 目前只接受 `per-call`。 |
| `timeoutMs` / `timeout_ms` | `120000` | 默认程序超时（毫秒），正整数，按 `maxTimeoutMs` 截断。 |
| `maxTimeoutMs` / `max_timeout_ms` | `600000` | 超时上限。 |
| `maxOutputBytes` / `max_output_bytes` | `67108864` | logs + 返回值的字节预算。 |
| `maxPendingCalls` / `max_pending_calls` | `128` | 同时在途的内层工具调用上限（host 侧信号量）。 |
| `graceMs` / `grace_ms` | `3000` | terminate 后等待多久再 kill；非负整数。 |
| `maxParallelSubCalls` / `max_parallel_sub_calls` | `10` | parallel-safe 内层工具的并发上限。 |
| `maxSubCallsPerMinute` / `max_sub_calls_per_minute` | `1024` | 每分钟内层调用额度。 |

示例（`~/.dpro/config.toml`）：

```toml
[ptc]
mode = "ptc"
timeoutMs = 180000
maxParallelSubCalls = 10
maxSubCallsPerMinute = 1024
```

`PtcConfig.timeout` 返回 DSH 形状的 `{"defaultMs", "maxMs"}`；`to_dict()` 用 camelCase。

## 4. 呈现模式与请求装配

`ptc` 模式只收窄**模型直接可见的 surface**，不改变以下三处的全量工具来源：

1. `AgentsPlugin.create` 构造 `AgentSession` 时传 `surface_registry`：
   - 主条件：agent preset 的 `presentation == "ptc"`（即内置 `ptc` preset）；此时 registry 必须含
     `run_code`，否则抛 `PluginDependencyError`。
   - 兼容条件（旧全局开关）：`ptc` 服务 `mode == "ptc"`、preset 的 `mode == default`、registry 里
     有 `run_code`。
   - 结果：`registry.without([除 run_code 外的全部工具])`；否则为 `None`（= 原生 surface）。
   - Team mode 永远保持原生 surface（Lead 必须能直接调用 `team_*` 编排成员）。
2. `AgentSession` 用 surface 构建：`LlmRequest.tools`、`tool/call` 分组、`run_tool_batch` 的 registry、
   request header 的 `tools`，以及 `_prompt_context().direct_tool_names`。
3. `PromptContext.direct_tool_names` 只影响核心 `## Tools` 段（order 40）的列表；
   `tools:sdk` 段仍拿到**全量** schema（SDK 要声明所有可编程工具）。

`PtcPlugin` 注册两个提示词段。全局 `[ptc].mode = "ptc"` 时它们作为全局 contribution；默认
`mode = "native"` 时，`run_code` 工具与这两段都以 `scope = PTC_PRESET_ID` 注册，因此只对 `ptc`
preset 的 agent 可见，其他 preset 的工具集与系统提示词完全不变：

| section | order | 内容 |
| --- | --- | --- |
| `tools:ptc-only` | 41 | 固定一句：`run_code` 是唯一能直接调用的工具，其余工具必须从程序里访问。 |
| `tools:sdk` | 42 | `render_tools_sdk(全量 schema 去掉 run_code 与嵌套排除项)`；渲染异常时返回空串并记 warning。 |

于是 `ptc` 模式下的系统提示词是：核心 identity/environment/instructions（10/20/30）→
只列 `run_code` 的 `## Tools`（40）→ “只能直接调 run_code”（41）→ Python SDK（42）→ rules（50）。
preset `ptc` 与全局 `[ptc].mode = "ptc"` 走同一条装配路径，只是贡献的 scope 不同。

## 5. Python SDK 渲染器（`ptc/sdk.py`）

`render_tools_sdk(tools: Sequence[ToolSchema]) -> str` 是纯投影，不做过滤/排序之外的任何决策：

- 固定格式：`SDK_INSTRUCTIONS` 前言 + 空行 + **唯一一个** ```python 围栏。
- 顺序：工具名按 UTF-16 code unit 排序（等价 JS `<`，与 DSH 一致）。
- 每个工具在 `class Tools(Protocol)` 上贡献一个 `async def <name>(self, args: XArgs) -> XOutput`；
  名字不是合法属性（关键字、下划线开头、含连字符等）时退化为
  `# tools["<name>"](args: XArgs) -> XOutput` 注释，用下标访问。
- 参数/输出 schema 生成 `TypedDict`：对象节点成为命名类（后序声明，子类在前）；非法或匿名对象
  降级为 `dict[str, Any]`；`output=None` 渲染为 `Any`。
- JSON-Schema 子集校验（`type`/`oneOf`/`properties`/`required`/`additionalProperties`/
  `items`/`enum`/`const`）；越界或异常整体降级为 `Any`，**绝不让提示词装配失败**。
- `from typing import ...` 只列实际使用的符号，顺序固定；`Protocol` 恒存在；空工具集也输出
  `class Tools(Protocol): pass`。
- 输出必须确定：同一工具集在任何时候都渲染出逐字节相同的文本（利于 prompt cache）。

SDK 只是**静态声明**：运行时并不存在 `XArgs` 类，参数必须用普通 dict/list。`SDK_INSTRUCTIONS`
明确写了这一点，并说明失败抛 `ToolCallError`、并行用 `asyncio.gather`、只有程序输出进入对话。

## 6. `run_code` 工具契约

- `name = "run_code"`、`reserved_tool = True`、`parallel_safe = False`。
- JSON Schema：

```json
{
  "type": "object",
  "properties": {
    "code": {"type": "string", "description": "The Python program to execute. Top-level await and return are supported."},
    "description": {"type": "string", "description": "A short summary of what the program does."},
    "timeoutMs": {"type": "number", "minimum": 1, "maximum": 600000}
  },
  "required": ["code", "description"],
  "additionalProperties": false
}
```

`timeoutMs` 只在 runtime 暴露 `timeout` 时出现。`description` 必须是去空白后非空的字符串，
否则抛 `ToolError("invalid description: expected a non-empty string")`。

- `description` 字段 = 固定前缀（说明 code/description、SDK 用法、图片结果延后附上）
  + `runtime.execution_instructions` + 固定后缀（程序起始 cwd 固定）。
- 服务缺失：`ToolError("PTC runtime is not available in this session")`。
- `read-only` 策略：**不启动 worker**，直接返回 `is_error=True`、
  `text="run_code is unavailable under read-only policy"`、`meta.kind="sandbox-unavailable"`。
- 成功文本：logs（`rstrip("\n")`）与 value 用两个空行连接；字符串 value 原样输出，其它 value 用
  `json.dumps(indent=2, ensure_ascii=False)`；两者都空时输出 `(run_code completed with no output)`。
- 失败文本：`Error: code run failed (<kind>): <message>`；有 logs 时追加
  `\n\nCaptured output:\n<logs>`。
- `meta`：成功 `{logs, sandbox[, result]}`（`result` 仅 value 非 None）；失败 `{kind, sandbox, logs}`。
- `parts`：deferred 图片 parts（见 §8.4）。

## 7. worker 协议与执行语义（`ptc/runtime.py` + `ptc/worker.py`）

每次 `run` 启动：

```text
<sys.executable> -I -S -X utf8 <src/deeppyro/ptc/worker.py>
```

cwd 为 `spec.cwd`（会话当前目录）；环境变量白名单（`PATH`、`PATHEXT`、`SYSTEMROOT`、
`SYSTEMDRIVE`、`WINDIR`、`COMSPEC`、`TEMP`、`TMP`、`HOME`、`USERPROFILE`）并设置
`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。

### 7.1 帧

host → worker stdin（首帧一行）：

```json
{"protocol": 1, "program": "<source>", "bindings": ["read", "grep"]}
```

worker → stdout（一行一个对象）：

| kind | 字段 | 含义 |
| --- | --- | --- |
| `log` | `stream: "stdout"\|"stderr"`, `text` | 程序 print/stderr 输出 |
| `call` | `id: int`, `name: str`, `arguments: object` | 请求 host 执行一个工具 |
| `done` | `value`, `encodedBytes` | 程序成功返回 |
| `error` | `errorKind`, `message` | 程序失败 |

host → worker stdin（内层调用结果）：

```json
{"kind": "result", "id": 1, "ok": true, "value": "<tool text>"}
{"kind": "result", "id": 1, "ok": false, "error": {"name": "ToolError", "code": "TOOL_ERROR", "reason": "..."}}
```

所有 JSON 均为 UTF-8、`ensure_ascii=False`、`allow_nan=False`、紧凑分隔符。协议流固定使用
`sys.__stdout__`；程序的 `sys.stdout`/`sys.stderr` 被替换成 log writer，print 不会污染协议。

### 7.2 程序形态

源码被包装成：

```python
async def __ptc_main__():
    <program，整体缩进 4 空格>
```

命名空间只含 `tools` 与 `ToolCallError`。`tools` 是代理对象：`await tools.name(args)` 或
`await tools["name"](args)`；每个工具名对应一个 async binding，返回值是工具结果文本
（`ToolResult.text`），失败抛 `ToolCallError(toolName, message)`。

### 7.3 输出与日志上限

- worker 侧每个流（stdout/stderr）最多保留 1 MiB，超限追加一次
  `[... output truncated by run_code ...]` 并丢弃后续内容。
- host 侧总预算 `max_output_bytes`（logs + 返回值编码）；超限为 `output-limit`。
- host 对每次 `log` 帧累加字节，超限立即结算；返回值的 JSON 编码也计入预算。
- 返回值必须是 lossless JSON（NaN/Infinity、set、循环引用等 → `invalid-output`）。

### 7.4 超时、取消与终止

- 超时覆盖启动 + 程序执行 + 内层工具/审批等待，默认 `timeoutMs`，上限 `maxTimeoutMs`。
- 超时或取消：先 `terminate()`，等 `graceMs`，仍未退出则 `kill()`，关闭管道。
- 启动前 `ctx.cancel` 已 set → 直接返回 `abort`，不启动进程。
- 超时 → `PtcFailure("timeout", "run_code timed out after <ms>ms")`；取消 → `abort`。

## 8. 内层 dispatch（`ptc/executor.py`）

### 8.1 binding 集合

- 来自 `ToolContext.services["tool_registry"].names()`，排除 `NESTED_EXCLUDED_TOOLS`
  = `{"run_code", "ask_user_question"}`（transport 自身与交互工具不能被程序递归调用）。
- 每次调用生成子 call id：`<root_call_id>:ptc:<n>`（`n` 从 1 递增）。
- 子 `ToolContext` 继承 cwd、session_id、turn、step、cancel、log、permission、services，
  只有 `call_id` 是新的。

### 8.2 记录形状

- `tool/ptc-dispatch-start`：`{rootCallId, parentCallId, subCallId, name, arguments}`。
- `tool/ptc-dispatch`：同一组字段 + `isError` + `content:[{"type":"text","text":...}]`，
  有 `meta` 时附带 `meta`，失败时附带 `error:{name, code, reason}`。
- 两个 type 都在 `core.wire.RECORD_TYPES` 内、且不在 `SURFACE_EVENTS`，所以只进日志、
  不会出现在 `derive_messages()` 里（模型看不到内层调用）。
- 参数不是 JSON 时抛 `PtcBindingError(name, "INVALID_ARGUMENTS", ...)`；工具错误码取
  `ToolResult.meta.code`（缺省 `TOOL_ERROR`），reason 去掉 `"Error: "` 前缀。

### 8.3 并发与额度

- parallel-safe 工具：受 `asyncio.Semaphore(max_parallel_sub_calls)` 限制并发；
  非 parallel-safe 工具是**独占屏障**：等待此前并行调用全结束，自己独占执行。
- 总调用额度：`max_total_sub_calls = max(1, floor(timeout_ms / 60000 * max_sub_calls_per_minute))`
  （默认 2 分钟 → 2048 次），再叠加 60 秒滑动窗口 `max_sub_calls_per_minute`。
- 超过额度：设置 `dispatcher.fatal = PtcFailure("exception", "quota exceeded: ...")`，
  该 binding 抛 `QUOTA_EXCEEDED`；即使程序捕获，`run_code` 结束后仍按**失败**上报（额度超限是
  run 级致命错误，不是可恢复的工具错误）。
- `max_pending_calls` 在 runtime 侧限制同时在途的 host 侧调用。

### 8.4 图片结果

内层工具返回的 `parts` 中 `type == "image"` 的项被收集到 `dispatcher.deferred_parts`，
由 `run_code` 放进自己的 `ToolResult.parts`。`AgentSession._append_tool_result_images` 在
`tool/result` 之后追加一条 `source={"kind": "ptc-mode"}` 的 user 消息承载这些图片：

- 模型在下一步能看到图片内容；
- 该消息不是用户输入：TUI 历史回放与实时渲染都会跳过它，但 turn 计数仍与记录对齐
  （见 `tui/history.py` 的 `is_ptc_mode_message`）。

## 9. 失败 kind

`PTC_FAILURE_KINDS` 八个值：

| kind | 触发条件 |
| --- | --- |
| `exception` | 程序抛出普通异常；额度耗尽也归此类（message 说明 quota）。 |
| `timeout` | 整个 run 超时。 |
| `abort` | `ctx.cancel`。 |
| `worker-exit` | worker 未发 done 即退出、启动失败、请求帧发送失败、worker 脚本缺失。 |
| `invalid-output` | 返回值不是 lossless JSON。 |
| `output-limit` | logs + 返回值超过 `max_output_bytes`。 |
| `protocol` | 坏 JSON、非对象帧、未知帧 kind、超过 stream limit。 |
| `sandbox-unavailable` | read-only 策略直接拒绝（不启动 worker）。 |

成功时 `sandbox` 固定为 `{"mode": "workspace-write", "enforcement": "none", "denial": "none"}`
（PTC 自身没有 OS enforcement）。

## 10. 插件与接线

`PtcPlugin`：`provide="ptc"`、`plugin_id="ptc"`、`inject=["config", "tools", "prompt"]`。
`prompt` 依赖只在 `mode = "ptc"` 时真正用到，但列进 `inject` 让顺序要求在 bundle 层可见：
`register_prompt_section` 会解析 `prompt` 服务，服务未挂载时插件加载失败。

```python
def setup(self, ctx, config):
    ctx.provide("ptc", self.service)          # 总是提供，便于 inspect
    if self.config.mode != "ptc":
        return                                # native：完全不动工具/prompt
    ctx.register_tool(lambda _cfg, _workspace: [RunCodeTool(self.service)])
    ctx.register_prompt_section("tools:ptc-only", 41, ...)
    ctx.register_prompt_section("tools:sdk", 42, ...)
```

接线位置：

1. core bundle 在 `prompt-core` 之后有 `ptc` 行（`critical = false`，默认 `mode = "native"`，
   不注册任何工具/prompt 段）。位置必须在 `prompt`/`prompt-core` 之后：`ptc` 模式启动时
   要 `register_prompt_section`，早于提示词服务会因 `requires service 'prompt'` 加载失败。
2. `AgentSession` 接受 `surface_registry`（keyword-only）并按它构建请求、dispatch 与 header。
3. `AgentsPlugin.create` 依据 `ptc.mode`、preset mode 与 `run_code` 是否存在选择 surface。
4. `TUI`：`run_code` 的折叠行显示 `description`（80 字符）；展开时显示完整 description、
   `code (N lines):` 与前 20 行，剩余行数省略；`ptc-mode` user 消息在实时与历史回放中都不显示。
5. `headless` 与 TUI 共用同一 `AgentSession` 装配，无需额外开关。

切换方式：改 `~/.dpro/config.toml` 的 `[ptc].mode` 后重启；改变配置不改会话历史格式。

## 11. 安全边界（必须明确）

本实现不是 OS 沙箱。worker 子进程以当前用户权限运行，可以读写用户可访问的任何路径，也可以启动
子进程。`read-only` 拒绝 `run_code` 是唯一的 PTC 策略边界；成功结果的 `sandbox` 固定上报
`enforcement: "none"`。**不要把 PTC 当作不可信代码的隔离机制。**

## 12. 与 DSH 的对齐与差异

对齐：

- 呈现模式 `native` / `ptc`，`ptc` 下仅 `run_code` 可直接调用；
- 自动生成 Python SDK（`tools:sdk`）与 “只能直接调 run_code” 规则（`tools:ptc-only`）；
- `run_code` 的 `code` + `description` 契约与图片结果延后附上的语义；
- `NestedDispatcher` 的 `subCallId` 形状、dispatch 记录、parallel-safe 并发模型；
- `maxParallelSubCalls` / `maxSubCallsPerMinute` 与 timeout 推导的总额度；
- `resolve` / `run` seam、`PtcRunSpec`、八类失败 kind、`sandbox` 字段形状。

差异：

- DSH 用 Node worker / vm-script；本实现是每次调用全新的 CPython 子进程。
- 未实现 `maxOldGenerationSizeMb`、`maxMessageBytes`、streaming logs、completion notice、
  持久 worker、DSH 的 sandbox 升级/审批语义。
- SDK 渲染器是 DSH `renderToolsSdkPy` 的 Python 重写（6 个 golden 逐字节一致），但模板里
  关于 `run_code` 参数的措辞是本实现自写的。
- `run_code` 的 description 文案是自写，不逐字复制 DSH。
- `PtcRuntime` 不是 `api.Service` 子类（便于无 ctx 单测），由插件显式 provide。

## 13. 测试

```text
uv run pytest -o addopts= -q tests/ptc tests/agent/test_ptc_surface.py tests/prompt/test_prompt_direct_tools.py tests/tui/test_ptc_ui.py
uv run ruff check src/deeppyro/ptc src/deeppyro/builtin/ptc.py tests/ptc
uv run ruff format --check src/deeppyro/ptc src/deeppyro/builtin/ptc.py tests/ptc
```

覆盖：

- `tests/ptc/test_sdk.py`（38）：DSH 源码生成的 6 个 golden 逐字节对照；排序、TypedDict 后序、
  特殊名字下标形式、`oneOf`、`enum`/`const`、非法 schema 降级、空工具集、from typing 顺序。
- `tests/ptc/test_runtime.py`：success value、print/stderr logs、top-level await、空程序、
  binding 成功/可捕获错误/未知工具、程序异常、timeout、abort、worker-exit、坏协议、
  invalid-output、output-limit、cwd、timeout cap、缺失 worker。
- `tests/ptc/test_executor.py`：`subCallId` 形状、dispatch 记录、错误码映射、权限拒绝、
  并发上限、非并行屏障、额度（总次数 + 分钟窗口）、图片 deferred、排除工具。
- `tests/ptc/test_tool.py`：schema/flags、服务缺失、read-only、成功/空输出/logs-only、
  失败文本与 Captured output、超时参数。
- `tests/ptc/test_plugin.py`：`native` 不注册全局工具/prompt、但以 `scope="ptc"` 注册 `run_code`
  与两个 section；`ptc` 全局注册；SDK 段过滤 run_code 与嵌套排除项；bundle 行与 kernel `[ptc]`
  合并/覆盖；legacy `enabled` 映射；未知键。
- `tests/ptc/test_records.py`：`tool/ptc-dispatch*` 在 `RECORD_TYPES` 且不在 `SURFACE_EVENTS`；
  写真实 `Session` 后 `derive_messages()` 为空。
- `tests/agent/test_ptc_surface.py`（6）：原生 surface 全工具、PTC surface 仅 run_code
  （header 也只 run_code、prompt 全量 schemas、`direct_tool_names == ("run_code",)`）、
  dispatch 用 surface、图片生成 `ptc-mode` user 消息、文本结果不生成、surface 选择条件。
- `tests/prompt/test_prompt_direct_tools.py`（4）：`direct_tool_names=None` 行为不变、
  指定名单只列名单内工具、空名单、`tools:sdk` 仍收到全量。
- `tests/tui/test_ptc_ui.py`（13）：`run_code` 折叠摘要与展开细节（20 行截断、
  description 与 code）、`ptc-mode` 消息实时/历史回放过滤与 turn 计数。

## 14. 已知风险与未完成项

1. 多行三引号字符串作为 program 时续行会被统一缩进，可能改变字符串内容；普通多行代码不受影响。
2. 内层调用失败若程序不捕获，会变成 `exception` failure；程序可捕获 `ToolCallError` 后继续。
3. 子进程管道清理是 best-effort；Windows Proactor 下极端取消路径仍可能产生 ResourceWarning。
4. `max_output_bytes` 只覆盖 logs 与返回值，不覆盖请求帧里的 program 源码本身。
5. 没有 worker 进程的内存上限；失控程序可能占满内存（非目标）。
6. 未知工具名不会进入 dispatcher：worker 的 binding 集合决定了程序能调用什么，直接访问不存在
   的名字在 Python 层就是 `AttributeError`/`KeyError`，不会产生 dispatch 记录。
7. 嵌套调用若产生输出落盘 spill，`call_id` 含 `:` 时 Windows 文件名可能降级为 `spill=None`。

## 15. 最小示例

`ptc` 模式下模型一次 `run_code`：

```json
{
  "code": "text = await tools.read({\"file_path\": \"a.txt\"})\nrows = [line for line in text.splitlines() if 'TODO' in line]\nprint(len(rows))\nreturn {\"todos\": rows[:5]}",
  "description": "Count TODO lines and show the first five"
}
```

程序内：

```python
text = await tools.read({"file_path": "a.txt"})
rows = [line for line in text.splitlines() if "TODO" in line]
print(len(rows))
return {"todos": rows[:5]}
```

成功时 `ToolResult.text` 形如：

```text
3

{
  "todos": [
    "1: # TODO: ..."
  ]
}
```

同时日志里留下成对的 `tool/ptc-dispatch-start` / `tool/ptc-dispatch`（`name = "read"`）。
