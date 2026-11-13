# tools/spec/ptc.md — Python PTC（run_code）

> 状态：P1 最小实现，默认关闭（`[ptc].enabled = false`）。
> 本轮没有可用的 DSH PTC fixture 样本；测试中的程序、协议帧和期望结果均为自造回归样本，不声称与 DSH 逐字节对齐。

## 1. 目标与非目标

### 1.1 目标

- 提供保留工具名 `run_code`：模型写一段 Python 程序，程序内可以 `await` 当前 session 的工具。
- 每次调用启动一个全新的 CPython 子进程，通过 JSON-line stdio 协议通信。
- 程序可以使用 top-level `await` 与 top-level `return`，返回值必须是 lossless JSON。
- 程序失败（程序异常、超时、取消、worker 退出、协议错误、输出超限、非法返回值）都变成结构化的 `PtcRunResult.failure`，再转成 `ToolResult(is_error=True)`，不结束 turn。
- 内层工具调用通过 host 侧 registry dispatch，并写入成对的 `tool/ptc-dispatch-start` / `tool/ptc-dispatch` 日志记录；内层调用不进入模型可见的 surface。
- 默认关闭；只有 `[ptc].enabled = true` 时才注册 `run_code`，未启用时模型看不到该工具。

### 1.2 非目标（本轮明确不做）

- 不是操作系统级沙箱，也不是安全边界（见 §9）。
- 不做 streaming logs/progress、completion notice、持久 worker、跨调用状态。
- 不实现 DSH 的 `maxOldGenerationSizeMb`、`maxMessageBytes`、vm-script、image context。
- 不实现后台 job 与 PTC 的结合。

## 2. 文件布局与公开 API

- `src/deeppyro/ptc/types.py`：配置、run spec/result、failure、binding 错误。
- `src/deeppyro/ptc/worker.py`：独立 worker 脚本（只用 stdlib）。
- `src/deeppyro/ptc/runtime.py`：`PtcRuntime`（服务名 `ptc`）。
- `src/deeppyro/ptc/tool.py`：`RunCodeTool` 与内层 dispatch。
- `src/deeppyro/ptc/__init__.py`：全部公开符号 re-export。
- `src/deeppyro/builtin/ptc.py`：`PtcPlugin`（kernel 插件，提供 `ptc` 服务）。

公开 API：

```python
from deeppyro.ptc import (
    PtcConfig, PtcRunSpec, PtcRunResult, PtcFailure, PtcBindingError,
    PtcRuntime, PtcService, RunCodeTool, RUN_CODE_NAME, WORKER_PATH,
)

PtcRuntime(
    config: PtcConfig | Mapping[str, Any] | None = None,
    *,
    worker_path: str | Path | None = None,
)
PtcRuntime.timeout -> {"defaultMs": int, "maxMs": int}
PtcRuntime.resolve(request, *, cwd: Path | None = None) -> PtcRunSpec
async PtcRuntime.run(
    spec: PtcRunSpec, *, bindings: Mapping[str, Callable] | None, ctx: ToolContext
) -> PtcRunResult
```

`PtcService` 是 `PtcRuntime` 的别名。`PtcRuntime.provide = "ptc"`，`inject = ["tools", "sandbox"]`。

`PtcRunSpec(program, cwd, timeout_ms=120000, max_output_bytes=67108864, sandbox_mode="workspace-write")`。

`PtcRunResult(value=None, logs: tuple[str, ...] = (), failure: PtcFailure | None = None, sandbox: dict)`；`ok` 为 `failure is None`；`to_dict()` 返回 JSON 友好的 `{"value", "logs", "failure", "sandbox"}`。

## 3. 配置

配置从 bundle 行 config 与 kernel `[ptc]` 表合并，kernel 表覆盖行 config（与 `attachments` 插件相同的合并模式）。同时接受 camelCase（DSH 形态）和 snake_case 别名。

| 键（camelCase / snake_case） | 默认 | 语义 |
| --- | --- | --- |
| `enabled` | `false` | 只有 true 才注册 `run_code`。 |
| `executionMode` / `execution_mode` | `"per-call"` | 目前只接受 `"per-call"`。 |
| `timeoutMs` / `timeout_ms` | `120000` | 默认程序超时（毫秒），正整数。 |
| `maxTimeoutMs` / `max_timeout_ms` | `600000` | 超时上限；更宽的值被截断。 |
| `maxOutputBytes` / `max_output_bytes` | `67108864` | logs + 返回值编码后的总字节预算。 |
| `maxPendingCalls` / `max_pending_calls` | `128` | 同时在途的内层工具调用上限。 |
| `graceMs` / `grace_ms` | `3000` | terminate 后等待多久再 kill；非负整数。 |

未知键、非布尔 `enabled`、非法 `executionMode`、非正整数的数值、负 `graceMs` 都抛 `ConfigError`。`timeoutMs` 在 `from_mapping` 中先按 `maxTimeoutMs` 截断。`PtcConfig.timeout` 返回 DSH 形状的 `{"defaultMs", "maxMs"}`。

## 4. worker 协议与执行语义

每次 `run` 启动：

```text
sys.executable -I -S -X utf8 <src/deeppyro/ptc/worker.py>
```

环境变量只保留 `PATH`、`PATHEXT`、`SYSTEMROOT`、`SYSTEMDRIVE`、`WINDIR`、`COMSPEC`、`TEMP`、`TMP`、`HOME`、`USERPROFILE`，并设置 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。cwd 使用 `spec.cwd`。

### 4.1 帧

host 先向 worker stdin 写一行：

```json
{"protocol": 1, "program": "<source>", "bindings": ["read", "grep"]}
```

worker 在 stdout 上写一行一个 JSON 对象：

| kind | 字段 | 含义 |
| --- | --- | --- |
| `log` | `stream: "stdout"\|"stderr"`, `text` | 程序 print/stderr 输出。 |
| `call` | `id: int`, `name: str`, `arguments: object` | 请求 host 执行一个工具。 |
| `done` | `value`, `encodedBytes` | 程序成功返回。 |
| `error` | `errorKind`, `message` | 程序失败。 |

host 通过 stdin 回内层调用结果：

```json
{"kind": "result", "id": 1, "ok": true, "value": "<tool text>"}
{"kind": "result", "id": 1, "ok": false, "error": {"name": "...", "code": "...", "reason": "..."}}
```

所有 JSON 都使用 UTF-8、`ensure_ascii=False`、`allow_nan=False`、紧凑分隔符。worker 的协议流固定用 `sys.__stdout__`；`sys.stdout`/`sys.stderr` 被替换成 log writer，因此程序 print 不会污染协议。

### 4.2 程序形态

程序源码被包装为：

```python
async def __ptc_main__():
    <program indented by four spaces>
```

所以程序可以写 top-level `await`，也可以 `return`。空/纯空白程序视为 `pass`，结果为 `None`。bindings 以同名 async 函数注入，同时提供 `bindings` 字典；函数支持 `await read(file_path="x")`、`await read({...})` 与 `await read(**kwargs)`。

### 4.3 输出与日志上限

- worker 侧每个流（stdout/stderr）最多保留 1 MiB；超限后追加一次 `[... output truncated by run_code ...]` 并丢弃后续内容。
- host 侧总预算为 `max_output_bytes`（logs + 返回值编码后），超过即 `output-limit`。
- host 对每次 `log` 帧累加字节；超限立即结算。返回值的 JSON 编码也计入总预算。

### 4.4 超时、取消与终止

- 超时覆盖启动 + 程序执行 + 内层工具/审批等待，默认 `timeoutMs`，上限 `maxTimeoutMs`。
- 超时或 abort 时先 `terminate()`，等待 `graceMs`，仍未退出则 `kill()`，最后关闭管道。
- `ctx.cancel` 在启动前已 set 时直接返回 `abort`，不启动进程。
- 超时返回 `PtcFailure("timeout", "run_code timed out after <ms>ms")`；取消返回 `PtcFailure("abort", ...)`。

## 5. 失败 kind

`PTC_FAILURE_KINDS` 八个值：

| kind | 触发条件 |
| --- | --- |
| `exception` | 程序抛出普通异常；worker 回 `errorKind=exception`。 |
| `timeout` | 整个 run 超时。 |
| `abort` | `ctx.cancel`。 |
| `worker-exit` | worker 未发 done 就退出、启动失败、请求帧发送失败。 |
| `invalid-output` | 返回值不是 lossless JSON（NaN、set、循环等）。 |
| `output-limit` | logs + 返回值超过 `max_output_bytes`。 |
| `protocol` | 坏 JSON、非对象帧、未知帧 kind、超过 stream limit。 |
| `sandbox-unavailable` | read-only preset 直接拒绝；`RunCodeTool` 不上报 `sandbox` 的 OS enforcement。 |

成功时 `sandbox` 固定为 `{"mode": "workspace-write", "enforcement": "none", "denial": "none"}`。

## 6. `run_code` 工具

`RunCodeTool`：`name="run_code"`、`reserved_tool=True`、`parallel_safe=False`。schema 只有两个属性：

```json
{
  "type": "object",
  "properties": {
    "program": {"type": "string", "description": "Python source. The function body may use top-level await and return."},
    "timeoutMs": {"type": "integer", "description": "Timeout for the whole program, including nested tool calls.", "minimum": 1}
  },
  "required": ["program"],
  "additionalProperties": false
}
```

行为：

- 服务缺失：`ToolError("PTC runtime is not available in this session")`（read-only 检查先于服务检查，因此 read-only 时不启动 worker）。
- read-only preset：直接返回 `ToolResult(is_error=True, text="run_code is unavailable under read-only policy")`，`meta.kind="sandbox-unavailable"`；不启动 worker。
- 成功文本：logs（rstrip 换行）与 JSON value（非 None）用两个换行连接；两者都空时 `(run_code completed with no output)`。
- 失败文本：`Error: code run failed (<kind>): <message>`；如果有 logs，追加 `\n\nCaptured output:\n<logs>`。
- 成功 meta：`{"sandbox", "logs", "value"}`；失败 meta：`{"kind", "sandbox", "logs"}`。

## 7. 内层工具 dispatch

- 可调用工具来自 `ctx.services["tool_registry"]` 的 `names()`，排除 `run_code` 与 `ask_user_question`。
- 每个 binding 调用生成新的 `call_<hex>` 子 call id；子 `ToolContext` 继承 cwd、session_id、turn、step、cancel、permission、log、services，只有 `call_id` 是新值。
- 先写 `tool/ptc-dispatch-start`：`{rootCallId, parentCallId, subCallId, name, arguments}`。
- 执行工具；异常（包括 `PermissionDenied`）转成 `ToolResult(is_error=True)`，错误码为 `PERMISSION_DENIED` 或 `TOOL_ERROR`。
- 再写 `tool/ptc-dispatch`：在 start 字段上增加 `isError`、`content:[{"type":"text","text":...}]`，可选 `meta` 与 `error:{name,code,reason}`。
- 内层 `ToolResult.text` 作为 binding 的字符串值回给程序；`isError=True` 时 worker 抛 `PtcToolCallError`，程序可以 `except` 捕获。
- 这两个记录 type 已在 `core.wire.RECORD_TYPES` 中，且不在 `SURFACE_EVENTS`，所以只进日志、不进 `Session.derive_messages()`。
- 同时在途的内层调用由 `PtcConfig.max_pending_calls` 的 `asyncio.Semaphore` 限制；超出的调用等待。

## 8. 插件与接线

`PtcPlugin`：`provide="ptc"`、`plugin_id="ptc"`、`inject=["config", "tools"]`。`setup` 总是 `ctx.provide("ptc", service)`；只有 `config.enabled` 为 true 时才 `ctx.register_tool(lambda _cfg, _workspace: [RunCodeTool()])`。

接线已全部完成（Lead）：

1. core bundle 在 `tools-builtin` 之后增加 `ptc` 行（`critical = false`，默认配置不注册工具）；
2. `AgentSession` 构造时把宿主 `ToolRegistry` 注入 `services["tool_registry"]`，内层 dispatch 从 `ToolContext.services` 取它；
3. `ToolRegistry` 允许 `reserved_tool=True` 的 `run_code` 注册（`RESERVED_TOOL_NAMES`），未标记的普通工具注册/遮蔽该名字会抛 `ToolError`；
4. `tui/runtime.py` 与 `headless.py` 的 `_plugin_services()` 注入 `ptc`，工具通过 `ctx.services["ptc"]` 取服务；
5. `~/.dpro/config.toml` 的 `[ptc] enabled = true`（或 bundle 行 config）即可让模型看到 `run_code`；默认关闭。

## 9. 安全边界（必须明确）

本实现不是 OS 沙箱。worker 子进程以当前用户权限运行，可以读写当前用户可访问的任何路径，也可以启动子进程。`PermissionGate` 对 `run_code` 的 read-only 拒绝是唯一策略边界；`sandbox` 字段固定上报 `enforcement: "none"`，表示没有 OS 级 enforcement。不要把 PTC 当作不可信代码隔离机制。

## 10. 与 DSH 的差异

- DSH 使用 Node worker / vm-script，本实现是每次调用全新 CPython 子进程。
- DSH 的 `maxPendingCalls`、`maxOutputBytes`、`timeout` 语义保留；`maxOldGenerationSizeMb`、`maxMessageBytes` 未实现。
- DSH 有 streaming logs、completion notice、持久 worker、image context；本实现没有。
- 超时与取消的终止梯度简化为 terminate → grace → kill；DSH 的 sandbox 升级/审批语义不复用。
- DSH 的 reserved name 错误文案逐字保留在 registry；`run_code` 的 description 是本实现自写，不逐字复制 DSH。
- `PtcRuntime` 不是 `api.Service` 子类（避免在无 ctx 的单测中构造），由 `PtcPlugin` 显式 `ctx.provide`。

## 11. 测试

命令：

```text
uv run pytest -o addopts= -q tests/ptc --tb=short
uv run ruff check src/deeppyro/ptc src/deeppyro/builtin/ptc.py tests/ptc
uv run ruff format --check src/deeppyro/ptc src/deeppyro/builtin/ptc.py tests/ptc
```

结果：`61 passed`（本机 Windows，Python 3.13.18）；ruff check/format 全绿。

覆盖：

- `tests/ptc/test_runtime.py`：success value、print/stderr logs、top-level await、空程序、binding 成功/可捕获错误/UNKNOWN_TOOL、程序异常、SyntaxError、timeout、abort（启动前/运行中）、worker-exit、坏协议、invalid-output、output-limit、cwd、timeout cap、max_pending_calls、缺失 worker、resolve/config 校验、registry 保留名。
- `tests/ptc/test_tool.py`：schema/flags、服务缺失、read-only、成功/空输出/logs-only、失败文本与 captured output、超时参数、dispatch 成功/错误/权限、上下文继承、排除工具。
- `tests/ptc/test_plugin.py`：默认关闭不注册、enabled 注册、bundle 行与 kernel `[ptc]` 合并/覆盖、未知键。
- `tests/ptc/test_records.py`：dispatch 记录 type 在 `RECORD_TYPES` 且不在 `SURFACE_EVENTS`；并用真实 `Session` 验证两条记录写入后 `derive_messages()` 为空。

## 12. 已知风险与未完成项

1. 多行三引号字符串作为 program 时，续行会被统一加 4 空格，可能改变字符串内容；普通多行代码不受影响。
2. 内层调用失败若程序不捕获，会变成 `exception` failure；程序可捕获 `PtcToolCallError` 后自行处理。
3. 子进程管道清理是 best-effort；Windows Proactor 下极端取消路径仍可能产生 ResourceWarning（测试未设为 error）。
4. `max_output_bytes` 只覆盖 logs 与返回值，不覆盖协议帧中 program 源码本身；program 大小远低于该预算。
5. 未做 worker 进程的内存上限；恶意/失控程序可能占满内存（本轮非目标）。
6. 默认关闭：不配置 `[ptc].enabled = true` 时模型看不到 `run_code`；TUI/headless 已接线，但没有内置开关 UI。

## 13. 最小示例

```python
# 模型调用 run_code:
# {"program": "rows = await read(file_path='a.txt'); return len(rows)"}

# 程序内:
text = await read(file_path="a.txt")
print(text[:100])
return {"chars": len(text)}
```

成功时 `ToolResult.text` 形如：

```text
<前 100 字符>

{"chars": 1234}
```
