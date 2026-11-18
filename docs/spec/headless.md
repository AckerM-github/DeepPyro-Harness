# headless 一次性模式（deeppyro/headless.py）

> **状态**：现行（native 默认） · **层级**：CLI 功能规格 · **最后核对**：2026-11-18
> 0.2.0 起 `run_once` 默认走 native schema 1 + `AgentLoop`；`--legacy-v4` 回退 v4，`--native-loop` 为弃用别名。
> 契约入口：[native/schema.md](../native/schema.md)；操作指南：[guides/native-loop-default.md](../guides/native-loop-default.md)。

> 所有者：Lead。native 路径（默认）以 [native/schema.md](../native/schema.md) 为准；`--legacy-v4` 的 v4 路径
> 以 [INTERFACES.md](INTERFACES.md)（迁移参考）为事实源。本文件记录 headless 的 API、事件与取舍。
> 交互式产品形态见 [tui.md](tui.md)。

## 1. 定位

`dpyro` 无参数启动 Textual TUI（唯一交互式形态）；headless 是同一套领域层/内核之上的
一次性脚本路径，供管道、CI、自动化调用。它**自包含**：不 import `deeppyro.tui`、
不 import 已删除的 `deeppyro.cli`。

    dpyro --headless "prompt" [--model M] [--provider P] [--preset P] [--json] [--cwd DIR]
        [--max-steps N] [--permission-mode MODE] [--approval-policy POLICY]
        [--legacy-v4 | --native-loop(deprecated)]
    echo "prompt" | dpyro --headless --json

路径选择（只影响 `run_once`）：默认 native schema 1 + `AgentLoop`；`--legacy-v4`（或
`DEEPPYRO_NATIVE_LOOP=0|false|no|off`）回退 v4；`--native-loop` 仍被接受但已弃用（会打
`DeprecationWarning`），等价于默认。`--legacy-v4` 与 `--native-loop` 互斥（argparse 退出码 2）。
native 路径不创建 v4 session-service bindings，`--max-steps` 映射为 native budget；细节见
[native/schema.md §5.5](../native/schema.md)。

参数为空（或 stdin 为空）→ 退出码 2；`--max-steps 0` → 2；`--preset` 未知 → stderr
`error: unknown agent preset: '<id>'`、退出码 2（在 boot 建 agent 之前失败）。
`--permission-mode` 只接受 `read-only`/`workspace-write`/`danger-full-access`，
`--approval-policy` 只接受 `ask`/`never`（argparse `choices`，非法值由 argparse 以退出码 2 拒绝）。

## 2. 模块结构

| 名称 | 说明 |
|---|---|
| `read_stdin_prompt()` | stdin 非 tty 时读取全部内容；OSError → "" |
| `resolve_workspace(path=None)` | 解析并校验工作目录；不存在 → ConfigError |
| `JsonLinesSink` | NDJSON：每个事件一行 compact JSON（`ensure_ascii=False`），emit 即 flush |
| `RichSink` | rich 终端渲染：流式 text、dim thinking、工具调用/结果预览、usage/final/error |
| `HeadlessRuntime` | boot 后服务门面：`create_agent(session, sink, max_steps=)`、`pin_permission(session)`、`aclose()`；携带 activity/jobs/team/subagent 服务，以及解析后的 `preset`/`preset_spec`/`tools` 与可选的 `permission_mode`/`approval_policy` CLI 覆盖 |
| `boot_runtime(...)` | load_config → 校验 provider/model → 解析 agent preset（`--preset`，默认 `standard`；别名 `default` 归一）→ `kernel.boot` → 取 llm/sessions/sandbox/tools/agents 五服务（外加 activity/jobs/team/subagent）→ 建 workspace 级 ToolRegistry/PermissionGate → 配置 team 与 subagent 服务；boot 前校验 `--permission-mode`/`--approval-policy`（非法 → ConfigError → 退出码 2） |
| `run_once(prompt, ...)->int` | 一次性执行入口，返回退出码；按 preset 建 scoped registry、写 session header 的 `agentPreset`；建 session 后立即 `pin_permission`（先 `pin_initial` 再应用 CLI 覆盖） |

## 3. 事件语义

- `run_once` 先建 Session，然后前端**恰好发一次** `{"type":"session","sessionId":...}`；
  `AgentSession` 永不发 session 事件。
- 其余事件全部来自 `AgentSession` 注入的 sink：`status`/`step`/`text`/`thinking`/`tool_call`/
  `tool_result`/`usage`/`final`/`error`，以及 hook journal 的 `hook/invoked`/`hook/result`。
  `step` 是 UI-only hint（紧跟 durable `step/start`，不写记录）；落盘顺序仍是「先 append 记录、后 emit 事件」。
- hook journal 记录同样走这个 sink：NDJSON 行带 `type`（`hook/invoked`|`hook/result`）、
  `seq`、`hook`、`mode` 等字段，因此一次 turn 的 hook 决策链（含 `ok`/`durationMs`/
  `error`）可直接从 stdout 解析；热 hook（`llm/stream`、`agent/assistant-stream`）不落日志。
  `approval/*` 与 session 生命周期 hook 没有 agent，不会出现在该行流里（见 hooks.md §2.11）。
- `--json`：stdout 只有 NDJSON；错误信息走 stderr；`saw_error` 时退出码 1。
- 非 json：先打印 `session <label>  provider/model  cwd ...` 头，成功后打印
  `session <label> · tokens in=... out=... cached=...` 尾。

## 4. 退出码

| 码 | 含义 |
|---|---|
| 0 | turn 完成 |
| 1 | 运行错误（`DeeppyroError`/`OSError`/`saw_error`） |
| 2 | 参数或配置错误（空 prompt、`max_steps < 1`、`ConfigError`） |
| 130 | 用户中断（KeyboardInterrupt → `agent.interrupt()`） |

## 5. 后台任务、subagent 与团队

- 后台任务：headless 可调用 `job_*` 工具与 `shell run_in_background`；`run_once` 在关闭 agent 后、
  返回前调用 `jobs.cancel_owner(session_id)`，一次性进程不会留下后台子进程。
- subagent：boot 时把 subagent 服务 `configure(..., ask=_denying_ask)`，创建 session 后 `bind_session(session)`，
  退出时 `aclose()` 取消在途子会话；默认模式下 `subagent` 工具可用（子会话审批一律拒绝）。
- 团队：team 服务同样 `configure`/`bind_session`，退出时 `aclose()` 关闭所有成员子会话；
  `team_*` 工具只在 session 的 agentPreset 为 `team` 时可见。headless 用 `--preset team` 以 team
  preset 启动，也支持 `standard`/`ptc`/`minimal`/`cordis`；它没有会话内 `/preset` 切换，仍不支持
  在运行中切 preset。headless 总是新建会话，因此不调用 `TeamService.restore()`；团队冷恢复只发生在
  TUI `/resume` 打开 team 模式会话时。
- 默认 vs Team：默认单 Agent + `subagent` 委派；Team 是显式模式（TUI 用 `/team on`，
  headless 用 `--preset team` 启动）。两者记录都按 DSH 的 `subagent/*`、`team/*` 形状落盘。

## 6. 权限与审批

- 会话创建后 `pin_permission` 先写初始 `sandbox/mode`、`approval/policy`、`permission/preset`
  三条记录，再按需应用 `--permission-mode` / `--approval-policy`（走 `PermissionPresetsService.apply`
  与 `ApprovalService.set_policy`，只写变化的旋钮）。
- 审批走 `ApprovalService`：`--approval-policy never` 时任何审批请求在 waterfall 前直接被
  rejected（不发 `approval/asked`/`approval/decided`）；`ask` 策略下 headless 没有交互式
  answerer，`ApprovalService` 返回 `unavailable`，gate 的 decision reason 为
  `no interactive approval handler is available`（fail closed）。
- 工具侧一次性升权（`sandbox_permissions` + `justification`）在 headless 下因此默认失败；
  脚本应使用 workspace 内路径，或用 `--permission-mode danger-full-access` 显式放开。
- 文件写入按 `[sandbox] mode` 与 writable_roots（workspace + additional + temp）直接判定，
  被拒时消息带 `[sandbox: file access denied under <mode> mode]`；shell 命令按策略 confine
  （`danger-full-access` 原样执行，Windows 其余模式走 ACL runner，非 Windows fail closed），
  输出尾部的 denial/runner-failure marker 与 TUI 一致（见 sandbox.md §7）。

## 7. 测试

`tests/tui/test_headless.py`（45 个，假 runtime 注入，无真实 LLM/磁盘会话）：
stdin 读取、两种 sink、`run_once` 的空/非法参数、流式 turn、json NDJSON 行类型、
agent 失败、KeyboardInterrupt、ConfigError、workspace 校验、退出时取消后台任务、绑定 team 会话、
`--preset` 解析/别名/未知 preset 退出码 2、preset-scoped registry 与 `session/created` 的真实 preset；
`--permission-mode`/`--approval-policy` 的 parser 透传、boot 校验（非法 → ConfigError）与
`pin_permission` 的初始/覆盖行为。
