# headless 一次性模式（deeppyro/headless.py）

> 所有者：Lead。契约事实源是 [INTERFACES.md](INTERFACES.md)；本文件记录 headless 路径的
> API、事件与取舍。交互式产品形态见 [tui.md](tui.md)。

## 1. 定位

`dpyro` 无参数启动 Textual TUI（唯一交互式形态）；headless 是同一套领域层/内核之上的
一次性脚本路径，供管道、CI、自动化调用。它**自包含**：不 import `deeppyro.tui`、
不 import 已删除的 `deeppyro.cli`。

    dpyro --headless "prompt" [--model M] [--provider P] [--json] [--cwd DIR] [--max-steps N]
    echo "prompt" | dpyro --headless --json

参数为空（或 stdin 为空）→ 退出码 2；`--max-steps 0` → 2。

## 2. 模块结构

| 名称 | 说明 |
|---|---|
| `read_stdin_prompt()` | stdin 非 tty 时读取全部内容；OSError → "" |
| `resolve_workspace(path=None)` | 解析并校验工作目录；不存在 → ConfigError |
| `JsonLinesSink` | NDJSON：每个事件一行 compact JSON（`ensure_ascii=False`），emit 即 flush |
| `RichSink` | rich 终端渲染：流式 text、dim thinking、工具调用/结果预览、usage/final/error |
| `HeadlessRuntime` | boot 后服务门面：`create_agent(session, sink, max_steps=)`、`aclose()`；携带 activity/jobs/team/subagent 服务 |
| `boot_runtime(...)` | load_config → 校验 provider/model → `kernel.boot` → 取 llm/sessions/sandbox/tools/agents 五服务（外加 activity/jobs/team/subagent）→ 建 workspace 级 ToolRegistry/PermissionGate → 配置 team 与 subagent 服务 |
| `run_once(prompt, ...)->int` | 一次性执行入口，返回退出码 |

## 3. 事件语义

- `run_once` 先建 Session，然后前端**恰好发一次** `{"type":"session","sessionId":...}`；
  `AgentSession` 永不发 session 事件。
- 其余事件全部来自 `AgentSession` 注入的 sink：`status`/`step`/`text`/`thinking`/`tool_call`/
  `tool_result`/`usage`/`final`/`error`。`step` 是 UI-only hint（紧跟 durable `step/start`，不写记录）；
  落盘顺序仍是「先 append 记录、后 emit 事件」。
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
  `team_*` 工具只在 session 的 agentPreset 为 `team` 时可见；headless 不自动切换 preset。
- 默认 vs Team：默认单 Agent + `subagent` 委派；Team 是显式模式（TUI 用 `/team on`，
  headless 需预先以 team preset 创建/恢复会话，不支持会话内切换）。两者记录都按 DSH 的
  `subagent/*`、`team/*` 形状落盘。

## 6. 审批

headless 没有交互式审批 UI：`boot_runtime` 默认用 `_denying_ask`（一律拒绝并 stderr 提示
`approval denied (headless mode): ...`）。需要审批的工具调用在 headless 下会被拒绝，
脚本应使用 workspace 内路径或 `approval = "never"` 的显式策略。

## 7. 测试

`tests/tui/test_headless.py`（17 个，假 runtime 注入，无真实 LLM/磁盘会话）：
stdin 读取、两种 sink、`run_once` 的空/非法参数、流式 turn、json NDJSON 行类型、
agent 失败、KeyboardInterrupt、ConfigError、workspace 校验、退出时取消后台任务、绑定 team 会话。
