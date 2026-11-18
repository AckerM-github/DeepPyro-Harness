# Headless 入口

> **状态**：现行（0.3.0 native-only） · **层级**：Headless 规格 · **唯一运行时代码**：`src/deeppyro/headless.py`
> **关联契约**：[native/schema.md](../native/schema.md)（schema 1 会话）、[loop.md](loop.md)（AgentLoop）、[events.md](events.md)（总线目录）
> **最后核对**：2026-11-19

## 1. 定位

Headless 是 TUI 的非交互对应物：执行**恰好一个 turn** 后退出，结果写 stdout（人类可读）或
NDJSON（`--json`）。它与 TUI 共用同一入口和同一运行时：`dpyro` 无参数启动全屏 TUI，
`dpyro PROMPT` 或 `dpyro --headless` 进入 headless（`src/deeppyro/tui/main.py:129`）。

0.3.0 起 headless 只驱动 native 运行时（`AgentLoop` + schema 1 会话）。旧 v4 headless 路径已被删除，
`headless.py` 头部注释即写明这一点；没有第二套后端。

- 不 import `deeppyro.tui`：headless 只 import `api`/`runtime`/`headless` 自身模块。
- 旧 0.2.0 的 `--legacy-v4` / `--native-loop` 与 `DEEPPYRO_NATIVE_LOOP` 已全部移除。
  现在传入这些参数是 **argparse 未知参数**，按 CLI 约定以退出码 **2** 结束；没有任何回退开关。
- 旧 v4 会话（`session.v4.jsonl`）不再可读；headless 不接受迁移，也不会隐式转换。

## 2. CLI 与输入

入口 `dpyro`（`src/deeppyro/tui/main.py`）；headless 相关参数：

| 参数 | 语义 |
|---|---|
| `PROMPT...`（位置参数） | 一次性提示词；存在时隐含 `--headless`（`main.py:23`） |
| `--headless` | 执行一个 turn 后退出 |
| `--json` | 事件以 NDJSON 输出；stdout 只有事件行 |
| `--provider` / `--model` | 覆盖配置中的 provider / model |
| `--preset` | agent preset：`standard`、`ptc`、`minimal`、`cordis` |
| `--permission-mode` | `read-only` / `workspace-write` / `danger-full-access` |
| `--approval-policy` | `ask` / `never` |
| `--cwd` | workspace 目录 |
| `--max-steps` | 每个 turn 的工具步数上限 |
| `--resume ID` | **仅 TUI**；headless 会打印提示到 stderr 后忽略（`main.py:151-152`） |

无提示词时从 stdin 读入（`read_stdin_prompt`，`headless.py:106`）；读到的内容为空时
打印 `error: empty prompt` 并以 **2** 退出（`tui/main.py:157-159`）。

## 3. 模块布局

| 模块 / 符号 | 职责 |
|---|---|
| `read_stdin_prompt`（:106） | 阻塞读 stdin，拼成单个 prompt |
| `resolve_workspace`（:133） | 解析 `--cwd` / 配置，得到 workspace `Path` |
| `compact_text`（:119） | 人类可读输出里的单行折叠 |
| `JsonLinesSink`（:289） | `--json` 输出；每行一个 `ensure_ascii=False` 的 JSON 对象，`flush` 后立即可见 |
| `RichSink`（:322） | 人类可读输出（header / 步骤 / 尾部摘要） |
| `HeadlessRuntime`（:474） | headless 的运行时上下文：kernel、服务、tools、permission、jobs/team/subagent |
| `boot_runtime`（:578） | 装配：load_config → 解析 provider/model → preset → kernel.boot → 领域服务 → tools/PermissionGate → team/subagent（存在才装配） |
| `_resolve_native_provider`（:561） | 从 provider 工厂注册表解析 native `LlmProvider`；不直接 import 具体 transport |
| `_NativeSinkHooks`（:780） | `NullAgentHooks` 子类：把 typed AgentHooks 事件映射为 headless 事件 |
| `run_once`（:697） | 建 native 会话 → `pin_permission` → 装配 `NativeAgentHost` → `host.send(prompt)` → final/usage → 返回退出码 |

`_denying_ask`（:273）：headless 没有审批 UI，需要 `ask` 的请求一律拒绝，并把
`approval denied (headless mode): ...` 写到 stderr。

## 4. 事件（`--json` NDJSON）

stdout 的事件是**扁平 JSON 对象**，一行一个。顺序与内容：

1. `{"type": "session", "sessionId": "<id>"}`（`headless.py:861`，总在其它事件之前）。
2. typed hook 事件（`_NativeSinkHooks`）：
   - `text`：`{"type": "text", "text": "..."}`（`text_delta`）
   - `thinking`：`{"type": "thinking", "text": "..."}`（`reasoning_delta`；人类可读模式的 marker 为 `· thinking`）
   - `tool_call`：`{"type": "tool_call", ...}`（`tool_call_started`）
   - `tool_result`：`{"type": "tool_result", ...}`（`tool_call_finished`）
3. 终止事件：`final`（最终助手文本）、`usage`（`input` / `output` / `cached` 三个键）、`error`（出错时）。
4. `--json` 模式下 stdout 只有以上事件行；诊断信息走 stderr。

run 期间另外通过 HookService 发 `session/created` 与 `session/disposed` 总线事件
（`_emit_hook`，`headless.py:764`；发射点 :855 / :977），供插件观察；0.3.0 没有 v4 hook journal
记录，也没有其它生命周期事件流。

## 5. 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常运行结束（包括输出过 `error` 事件但最终成功的情形） |
| 1 | 运行失败；若 sink 已观察到错误（`sink.saw_error`，`headless.py:909`）则为 1 |
| 2 | 用法错误：空 prompt、未知/已移除参数、配置/preset/model 解析失败 |
| 130 | Ctrl-C 中断（`KeyboardInterrupt`） |

## 6. 运行时能力

- **jobs**：`run_once` 结束时取消本 runtime 持有的后台 job（`_cancel_jobs`，:741），不留孤儿。
- **subagent**：可装配 subagent 服务；子会话 `origin=subagent`，`delegation_depth+1`；headless 的
  提问通道是 deny（`_denying_ask`），因此子代理无法弹审批。
- **team**：若配置启用 team preset，headless 装配 team 服务（`boot_runtime` 内可选，缺服务不崩溃）；
  团队事实写 native 会话与 `plugin.team.*`。
- **preset**：`standard`（含 PTC 的完整模式）、`ptc`、`minimal`、`cordis` 由 agent-presets 插件提供；
  未知 preset 打印错误退出 2。
- **permissions**：`pin_permission` 把本次运行的 permission preset 与 approval policy 固定进
  PermissionGate；CLI 覆盖优先于配置。
- **goal / compaction / trace**：goal 由 NativeRoundDriver 在 native 会话的 `goal.changed` 事实之上
  推进；compaction 由 NativeCompactService 手动触发（`/compact` 在 TUI），headless 不自动压缩；
  trace/usage 走 `TraceExportSink` 与 `UsageSink`，不在 stdout 额外造数。

## 7. 验证入口

`pytest tests/tui/test_headless.py`（headless 行为、退出码、NDJSON 事件形状、permission 拒绝）。
CLI 参数解析的单元测试在 `tests/tui/test_main_entry.py`。

## 8. 维护注意

- 改事件形状必须同时改 `tests/tui/test_headless.py` 与本文档；不要在没有对应 typed hook 时虚构事件。
- 已移除的开关不要"加回兼容"：0.3.0 的约定是移除即报错（argparse 未知参数 → 2），不是静默回退。
- 会话格式变更见 [native/schema.md](../native/schema.md)；loop 语义见 [loop.md](loop.md)。
