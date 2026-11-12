# dpyro CLI / 终端前端（cli 模块契约）

> 所有者：ui-dev。契约事实源是 [INTERFACES.md](INTERFACES.md)；本文件描述 cli/tui 的实现取舍、
> 对 §7/§8/§11 的落地方式，以及从 `fixtures/` 真实样本逆向出的展示语义。
> 单一前端原则：终端交互只有一套渲染实现（`cli/render.py`），`dpyro run` 与 `dpyro chat`
> 共用 EventSink 与渲染层；`deeppyro.tui` 只是兼容别名，不产生第二套 UI。

## 1. 文件布局

| 文件 | 职责 |
|---|---|
| `src/deeppyro/cli/main.py` | **唯一** import typer 的模块：命令/选项、退出码映射、stdin prompt、SIGINT 安装 |
| `src/deeppyro/cli/app.py` | 内核装配：`build_app` 走 `deeppyro.kernel.boot`，`CliApp` 门面（session/provider/registry/permission/agents） |
| `src/deeppyro/cli/frontend.py` | `TerminalFrontend` 服务门面 + 退出码常量（0/1/2/130） |
| `src/deeppyro/cli/render.py` | **唯一**渲染层：`JsonLinesSink`（NDJSON）、`RichSink`（rich 流式）、表格/配置摘要 |
| `src/deeppyro/cli/chat.py` | 交互循环 `chat_loop`、斜杠命令、`TurnInterrupts`（Ctrl+C） |
| `src/deeppyro/cli/__init__.py`、`__main__.py` | re-export 与 `python -m deeppyro.cli` |
| `src/deeppyro/tui/__init__.py` | 兼容别名，仅 re-export `cli.main:app` 等；零实现 |
| `tests/cli/**` | 全部用 fake（无真实 LLM/磁盘会话）覆盖命令、渲染、中断、错误码 |

依赖方向：`main → app/chat/frontend`，`chat → frontend/render`，`render` 只依赖 rich。
`render.py` / `chat.py` **不 import typer，也不 import cordis**，以便将来作为 cordis 插件的前端服务复用。

## 2. 命令

```text
dpyro [--version] <command>

dpyro run [PROMPT] [-m/--model ID] [-p/--provider NAME] [--json] [--cwd DIR] [--max-steps N]
dpyro chat [-r/--resume SESSION_ID] [--cwd DIR]
dpyro sessions [-n/--limit N]          # 默认 20
dpyro config show [--json]
```

- `run`：跑**一个** turn 就退出。省略 `PROMPT` 时从管道 stdin 读取；stdin 是 TTY 或为空
  → `error: no prompt given (pass a prompt or pipe one on stdin)`，退出码 2。`--max-steps` 必须 ≥1。
- `chat`：交互式多轮。`--resume` 打开已有会话（未知 id → 2）。
- `sessions`：启动内核（只为了拿 kernel 管理的 store），列出最近会话表。
- `config show`：纯展示，**不 boot 内核**；只调用 `load_config()` + `effective_config_summary()`。
- 无参运行显示帮助并退出 2（typer `no_args_is_help`）；`--version` 输出 `dpyro 0.1.0` 退出 0。
- 所有错误信息写 stderr（rich `Console(stderr=True)`），stdout 只用于正常输出。

## 3. `--json`：NDJSON 事件流

`run --json` 时 stdout **只有一个 JSON 对象一行**（`ensure_ascii=False`、`separators=(",",":")`、
逐行 flush）；banner/footer/usage 等人类输出全部关闭，stderr 只放进程级错误。
事件类型与字段严格对齐 INTERFACES §7（也就是 `dsh-headless --json` 的形状）：

| type | 字段 | 含义 |
|---|---|---|
| `session` | `sessionId` | CLI 在 turn 前注入的第一条事件 |
| `status` | `phase`: thinking/tool/done | 阶段切换 |
| `text` | `text` | 最终回答的流式分片（可能多行） |
| `thinking` | `text` | 推理流分片 |
| `tool_call` | `callId`, `name`, `arguments` | 工具调用开始（arguments 为对象） |
| `tool_result` | `callId`, `isError`, `text` | 工具结果 |
| `usage` | `input`, `output`, `cached` | token 用量（可多次，渲染层累加） |
| `final` | `text` | 终态文本；`--json` 下原样转发、去重由消费方按 type 处理 |
| `error` | `message` | 运行错误；CLI 看到 `error` 事件最终返回 1 |

约定：

- 事件按 AgentSession 发出顺序透传，CLI 只额外插入开头的 `session`。
- `tool_call.arguments` 保留 agent 给的对象形状，不在 CLI 侧做字符串化。
- `--json` 与非 `--json` 共用同一个 sink 协议（`emit/begin_turn/end_turn`），
  唯一来源是 `cli/render.py`（见 §9 偏离项）。

## 4. 退出码

| code | 含义 | 典型场景 |
|---|---|---|
| 0 | 成功 | run 正常结束；chat `/exit`/`/quit`/EOF；sessions/config show 成功；`--help/--version` |
| 1 | 运行错误 | 构建/会话/LLM/工具抛 `DeeppyroError`；NDJSON 流里出现 `error`；sessions/config 读取磁盘 `OSError` |
| 2 | 用法/配置错误 | 未知 flag、缺 prompt、`--max-steps < 1`、非法 `--cwd`、未知 provider、`--resume` 未知会话、配置 `ConfigError` |
| 130 | 用户中断 | run 执行中 Ctrl+C（进程级）；chat 仅在 SIGINT 逃出循环时（正常情况下 Ctrl+C 只中断当前 turn，进程继续） |

实现细节：`_run_impl`/`_chat_impl` 是同步 typer 回调，内部用 `asyncio.run` 跑
`_run_async`/`_chat_async`；异常映射集中在命令层，`TerminalFrontend` 只负责输出。

## 5. 交互 chat

- 斜杠命令：`/exit`、`/quit`（打印 goodbye 退出 0）、`/help`、`/new`（建新会话并换绑 agent，
  打印短 id）、`/sessions`（显示最近 20 条会话表）。未知 `/xxx` → 黄字 warning 后继续。
- 空行忽略；EOF（Ctrl+Z/Ctrl+D）→ goodbye 退出 0。
- 每个 turn：`sink.begin_turn()` → `await agent.send(text)` → 中断检查 → `sink.end_turn()` →
  `turn_usage()` 打印本 turn 的 token 增量。
- **Ctrl+C**：`_install_sigint` 把 SIGINT 路由到 `TurnInterrupts.press()`；turn 期间 `arm(agent)` 后
  press 直接调用 `agent.interrupt()`（真实 `AgentSession.interrupt` 是 async，CLI 用 `await_maybe`
  兼容并 `drain()` 等待信号处理器里 create_task 的中断请求）。`AgentSession.send` 被取消时返回
  **已交付的部分文本**而不抛异常，因此 CLI 用 `interrupts.fired`/`KeyboardInterrupt` 判定，
  打印 warning `interrupted` 后回到提示符（进程不退出）。idle 时按 Ctrl+C 只记录 `fired`，
  等当前 turn 结束后同样按中断处理。
- `chat_loop` 不直接读写 session 文件（只用 `store.list()` 展示与 `new_session()` 换会话），
  满足「TUI 只依赖 AgentSession + EventSink」。

## 6. 渲染语义（`RichSink` / `TerminalFrontend`）

- `run`（`markdown_final=False`）：`text` 分片**逐块**打印（`markup=False`，避免模型输出里的
  方括号被当标记）；`thinking` 灰色；`tool_call` 打印 `→ name args`；`tool_result` 打印
  `  ✓/✗ name + 压缩预览`（多行/超长截断，错误红/成功绿）；`error` 走 stderr 红色加粗。
- `chat`（`markdown_final=True`）：`text` 先缓冲，结束时整块按 **Markdown** 渲染，避免 rich
  在分片边界误解析；真实终端上用 Live 预览模拟流式，非 TTY（测试）只打印一次。
- `usage` 事件在 sink 内累加（`input/output/cached`），`footer`/`turn_usage` 打印
  `tokens in=X out=Y cached=Z`；`chat` 每 turn 打印增量，`run` 结束时打印一次。
- `final` 与已流式打印的文本相同则不重复打印（`--json` 仍原样转发 `final` 事件）。
- 所有人类输出 `soft_wrap=True` 或窄表布局，保证 80 列终端不截断 URL/apiKey/模型列表。

## 7. sessions 表与 id 短写

表列：`时间 | 标题 | 轮数 | id`（时间来自 `createdAt` int 毫秒 → `%Y-%m-%d %H:%M`；
标题空则 `-`；轮数来自 `SessionInfo.turns`；`--limit` 截断，空表打印 `no sessions found`）。

短 id：真实 id 形如 `session-<uuid>`（fixtures 的 `session-*.jsonl` 与 header `id` 字段一致）。
字面「前 8 位」永远是 `session-`，没有区分度，因此 `short_session_id` 先去掉 `session-` 前缀再取
前 8 位（例如 `session-1f620f7f-... → 1f620f7f`）。这是展示层约定，不是契约字段。

## 8. config show 与打码

`effective_config_summary` 返回 `configPath/configExists/sessionDir/agent{...}/providers{...}`；
每个 provider 含 `api/baseURL/apiKeyEnv/apiKey/apiKeySource/models`。打码规则
`mask_secret`：`None`/`""`→`-`（渲染时），长度 ≤8 → `***`，否则保留前 4 + `…` + 后 4
（用 unicode 省略号，避免与 key 字符混淆）。`apiKeySource` 为 `config`、`env:<VAR>` 或 `None`，
用于说明 key 来自配置文件还是环境变量。`--json` 输出同一 summary 的单行紧凑 JSON。
`config show` 不启动内核，因此未配置 provider 时也能看配置（`cfg.resolve()` 失败 →
`ConfigError` → 退出 2）。

## 9. 装配：kernel boot 与同步/异步桥

`build_app(...)` 默认路径（INTERFACES §11）：

```python
kernel = await boot(config)                 # config=None 读 config.toml；测试可注入 Config
services = {name: kernel.service(name) for name in
            ("frontend", "llm", "sessions", "sandbox", "tools", "agents")}
ui = frontend or getattr(services["frontend"], "frontend", services["frontend"])
```

- 每次命令按 workspace 取：`sandbox.gate(cwd, ask=make_terminal_ask(ui))`、
  `tools.registry_for(cwd)`、`llm.provider(name)`、`sessions.store`、`agents.create(...)`。
- `CliApp` 暴露 `new_session/open_session/list_sessions/create_agent`，并保存 `kernel_app`；
  `aclose()` 幂等、`await kernel.stop()`，任何构建失败都会先 stop 再抛（避免内核泄漏）。
- 真实内核服务 `frontend` 是 `FrontendPlugin`，其 `.frontend` 才是 `TerminalFrontend`，
  故 build_app 做一次解包（`getattr(..., "frontend", service)`）；测试覆盖该分支。
- typer 回调是同步的，而 kernel/AgentSession 是 async：命令层 `asyncio.run(_run_async(...))`；
  `CliApp.create_agent` 用 `agents.options(**overrides)` + `agents.create(...)`。
- **兼容假件**：`await_maybe`（app/chat 各一份）对 awaitable 才 await，同步测试假件原样返回；
  因此同一命令层既能跑真实内核，也能在 domain 模块未落地时用 fake 测试。
- 会话目录、配置路径来自 `load_config()` / `session_dir(cfg)`（`DEEPPYRO_CONFIG`、
  `DEEPPYRO_SESSION_DIR` 可覆盖，测试与 smoke 均用临时目录）。

真实 boot 探针已验证（临时目录 + 默认配置）：六服务齐全、`AgentSession.busy=False`、
`send/interrupt` 为 coroutine function、`await agent.interrupt()` 正常、`await app.aclose()`
连续两次幂等、`await list_session_infos_async()` 返回新建会话。未跑真实 LLM 请求。

## 10. 测试策略（`tests/cli`，109 个）

全部 fake，不依赖 session/llm/tools/agent 的实现：

- `fakes.py`：`FakeSession/FakeInfo/FakeStore/FakeAgent/AsyncFakeAgent/FakeApp/Installer`、
  `make_terminal_frontend()`（StringIO + no_color，固定宽度 100）、`LineReader`。
  `Installer` 替换 `cli.main.build_app/load_config/list_session_infos` 三个 seam。
- `test_main.py`(9)：`--help`/`--version`/无参/未知命令→2。
- `test_run.py`(20)：NDJSON 事件顺序与 stdout 纯净、非 json 流式+footer、`error` 事件→1、
  ConfigError→2、DeeppyroError→1、KeyboardInterrupt→130、`--max-steps` 透传、缺 cwd→2、
  同步与异步 AgentSession 两种形态。
- `test_chat.py`(25)：`/exit /quit /new /sessions /help`、未知命令、空行、EOF、
  Ctrl+C 中断后继续、`TurnInterrupts`（arm/disarm/fired/drain 异步）、resume 未知/已存在。
- `test_sessions.py`(6)、`test_config_show.py`(7)：表/limit/空表/错误码、打码与 `--json`、
  provider 来源标记、真实 TOML 集成。
- `test_render.py`(25)：mask/compact/时间/短 id、JsonLinesSink 行格式、RichSink 去重/usage 累加/
  markdown 只刷一次、空表、配置摘要。
- `test_app.py`(17)：`resolve_awaitable/await_maybe`、六服务解析、frontend 解包、provider/model
  覆盖、失败时 stop、create_agent 委托、`make_terminal_ask`、`list_session_infos` 与异常清理。

运行：`uv run pytest tests/cli -q`（exit 0）；`uv run ruff check src/deeppyro/cli src/deeppyro/tui tests/cli`
（All checks passed, line-length=110）；`uv run python -c "import deeppyro.cli, deeppyro.tui"`。

## 11. 从 fixtures 逆向出的语义（影响展示）

- 会话文件名两种：`session-<uuid>.jsonl` 与裸 `<uuid>.jsonl`；header 首行
  `{type:"session", version, id, createdAt(ms), cwd, agentPreset?, isSeeded?, delegationDepth?}`。
  CLI 只依赖 `SessionInfo.id`，不解析文件名。
- 标题记录 `type:"title", data.title` 会随会话多次出现（重命名/模型生成），**后写覆盖先写**；
  sessions 表展示 `SessionInfo.title`（store 已折叠），而不是第一条 title。
- 轮数：`turn/start` 记录数（store 折叠为 `SessionInfo.turns`）；`recordCount` 是全部记录数，
  表中不展示。
- 时间戳全部 int 毫秒（`createdAt`），渲染层统一格式化，不做时区换算（本地时间）。
- `permission/preset`、`sandbox/mode`、`approval/policy` 记录说明权限状态是日志中的 durable 状态；
  CLI 只通过 `sandbox.gate(workspace, ask=...)` 请求批准，不自己改这些记录。

## 12. 与 INTERFACES 的偏离 / 已确认扩展

1. **Sink 来源**：`JsonLinesSink/RichSink` 唯一实现在 `cli/render.py`，agent 只接收注入的
   `EventSink`（§7 已定案；`agent/sinks.py` 的重复实现已删除）。
2. **`--json`（config show）与 `--version`**：§8 未列出，属于向后兼容扩展（不改变已有命令行为）。
3. **sessions 短 id**：§8 写「id 前 8 位」，实际取去掉 `session-` 前缀后的前 8 位（见 §7）。
4. **`make_terminal_ask`**：权限 ask 回调按 `async (ApprovalRequest) -> bool` 实现；非交互 stdin
   直接拒绝并 warning。
5. **async AgentSession**：真实 `AgentSession.send/interrupt` 是 async（§7 未标注）；CLI 用
   `await_maybe` 兼容 sync/async 假件，建议 §7 补充说明。
6. **config show 不 boot kernel**：纯展示命令只 load 配置，避免为一个表启动服务图。

## 13. 已知风险 / 未验证

- 真实 LLM 请求链路未在 CLI 端到端跑过（需要可用 provider/key）；只验证到 boot + create_agent +
  interrupt + aclose。
- Windows 真实 TTY 的 Ctrl+C 未做人工端到端验证；信号路径由单测（`_install_sigint`、
  `TurnInterrupts`）覆盖。
- domain 模块（session/tools/agent）仍在演进，boot 服务名/方法签名若变化，`build_app` 需同步。
