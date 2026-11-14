# P2 执行计划：kernel 桥、hook 日志与观测链

基线：D:/code/deeppyro HEAD 6fd8e8d（P0/P1 已 push）。目标文档：
D:/dshworkspace/deeppyro-bus-alignment.md §4（P2-1..P2-4）、§7 commit 8/9、§8 验收。

## 1. 目标与边界

P2 只有两条硬验收（对齐文档 §8）：

- P2-1：一次 turn 的 `hook/invoked`、`hook/result` 可从 session 日志回放。
- P2-3：headless NDJSON 可见 hook 决策链。

边界：

- 不新增 hook 名（19 个已够），只给已有 dispatch 加观察与记录。
- 不改 `_vendor/cordis_port`；桥接在 kernel/api 或 builtin 插件里。
- hook/invoked、hook/result 的 payload 是 deeppyro 自定义（DSH 只有词汇名，
  `_dsh_src/dsh-session/lib/types/known-event-types.js:41-42` 无 writer 可抄），
  文档写明“对齐词汇名，不承诺字节级兼容”。
- 热路径（`llm/stream`、`agent/assistant-stream`）不桥接、不落日志：每帧会产生两条记录。
- 没有 agent 的 hook（approval/*、session/created、session/disposed）不写 session 日志，
  因为它们不在某个 AgentSession 里；headless/TUI 的 agent 级决策链不受影响，文档写明。

## 2. 架构

```text
HookService.dispatch (waterfall/serial/emit/bail/parallel)
  │  统一观察点 add_observer(observer)
  ├─ HookJournalPlugin  ──> AgentSession.record_hook_event → session 日志 + live sink
  └─ HookBridgePlugin   ──> ctx.emit("hook/invoked"|"hook/result", scope_thisArg, data)
                              └─ cordis listener / filter 扩展点
```

观察者是同步回调，异常被 `logger.exception` 隔离：观察失败不得改变 hook 结果。

## 3. 契约

### 3.1 HookService observer（plugin-dev / task-65）

```python
@dataclass(frozen=True, slots=True)
class HookObservation:
    kind: str          # "invoked" | "resolved"
    hook: str
    mode: str          # "waterfall" | "serial" | "emit" | "bail" | "parallel"
    scope: Mapping[str, Any] | None
    payload: Mapping[str, Any]   # 冻结后的 payload
    result: Any = None
    error: BaseException | None = None
    duration_ms: float = 0.0

def add_observer(self, observer: Callable[[HookObservation], None]) -> Callable[[], None]
```

- 每次 dispatch 恰好两条：invoked（执行前、duration_ms=0）→ resolved（finally 里）。
- resolved.result：waterfall/serial/bail 的返回值；emit/parallel 为 None。
- resolved.error：处理器抛出的异常（parallel 为 HookAggregateError）。
- 观察者异常只记日志；`_cancelled` 提前返回的 dispatch 也要成对记录。
- 五个 dispatch 方法都必须覆盖；`_dispatch_handlers` 抛错（未知 hook）不记录。

### 3.2 记录整理（plugin-dev / task-65，src/deeppyro/hooks/records.py）

常量：

```python
INVOKED_RECORD_TYPE = "hook/invoked"
RESULT_RECORD_TYPE = "hook/result"
HOT_HOOKS = frozenset({LLM_STREAM, AGENT_ASSISTANT_STREAM})
MAX_PREVIEW = 120
```

`invoked_record(obs) -> dict`：`{"hook", "mode"}` + 可选 scope `{"session","preset"}` +
可选字段（只取原始类型）：`"turn", "step", "tool", "callId", "provider", "model", "phase", "status"`。

`result_record(obs) -> dict`：`{"hook", "mode", "ok": bool, "outcome": "value"|"none"|"error",
"durationMs": int}` + `"valueType"`（非 None 且非 str 时类型名）、`"preview"`（str/JSON 截断 120）、
`"error": {"name", "message"}`、同类可选字段。键序固定，便于黄金测试。

`agent_from_payload(payload)`：payload["agent"] 有可调用 `record_hook_event` 时返回它，否则 None。
`is_journaled(hook)`：`hook not in HOT_HOOKS`。

### 3.3 session 写入（session-dev / task-67）

```python
# AgentSession
def record_hook_event(self, record_type: str, data: Mapping[str, Any]) -> int
```

- record_type 只接受 `hook/invoked`/`hook/result`，否则 SessionError。
- 写 `self._append(record_type, dict(data))`（seq 返回），再 `self._emit({"type": record_type, "seq": seq, **data})`
  让 headless NDJSON / TUI sink 立即看到（P2-3 验收）。
- 只写这二者，不改 derive/surface/压缩语义。
- `SessionQuery.hook_trace(session_id, limit=50) -> list[dict]`：按 seq 读 hook 记录，配对
  `invoked → result`（同 hook、最近未配对；无 result 的配对 result=None），返回
  `{"hook","mode","invoked": data, "result": data|None}`。给 /hooks 与测试用。

### 3.4 kernel 桥（loader-dev / task-66）

`src/deeppyro/kernel/hook_bridge.py`：

```python
EVENT_INVOKED = "hook/invoked"
EVENT_RESULT = "hook/result"

class HookEventScope:
    """cordis thisArg：携带 hook scope，实现 filter 协议扩展点。"""
    def __init__(self, scope: Mapping[str, Any] | None)
    @property scope -> Mapping[str, Any] | None
    def __filter__(self, ctx: Any) -> bool: return True   # 当前不按 listener ctx 过滤

class HookBridge:
    def __init__(self, emit: Callable[..., None])
    def __call__(self, observation: HookObservation) -> None
```

- `ctx.emit(scope_this_arg, "hook/invoked"|"hook/result", data)`；data 用 records 的整理结果，
  且跳过 HOT_HOOKS。
- builtin 插件 `HookBridgePlugin`（plugin_id="hook-bridge"，inject=["hooks"]）：
  setup 里 `self.ctx.emit` 构造 HookBridge + `ctx.effect(lambda: hooks.add_observer(bridge))`。
- 测试 `tests/kernel/test_hook_bridge.py`：假 emit 收到 (scope, name, data)；filter 协议可调用；
  观察者被注销后不再发；hot hook 不发。

### 3.5 hook journal（plugin-dev / task-65）

`HookJournalPlugin`（plugin_id="hook-journal"，inject=["hooks"]）：

- setup：`ctx.effect(lambda: hooks.add_observer(self._observe), label="hook-journal")`。
- `_observe(observation)`：跳过 hot；`agent_from_payload` 为 None 跳过；调
  `agent.record_hook_event(INVOKED/RESULT_RECORD_TYPE, record)`；异常 logger.exception 隔离。
- 计数器 `records_written` 供测试与 inspect。

### 3.6 P2-2 消费者（llm-dev / task-68）

- `src/deeppyro/builtin/prompt_extension.py`：`PromptExtensionPlugin`（plugin_id="prompt-extension",
  inject=["hooks","config"]）；config `[prompt] extension = ""`；监听 SYSTEM_PROMPT_ASSEMBLE：
  `rendered = await next_step()`，非 str 或文本为空时原样返回，否则 `rendered + "\n\n" + text`。
  文档写明这是 waterfall 后置扩展的示例消费者。
- `src/deeppyro/builtin/approval_policy.py`：`ApprovalPolicyPlugin`（plugin_id="approval-policy",
  inject=["hooks","config"]）；config `[approval] hookMode = "off"`（off|allow|deny|ask）：
  off/ask → `await next_step()`；allow/deny → 直接返回对应字符串。依据 `ApprovalRequest.tool`
  的 include/exclude 列表 `hookTools = []`（空=全部）也可加。gate 会校验返回值。
- config.py DEFAULT_TOML 增加 `[prompt]`、`[approval]` 两节（默认关闭/空文本，行为不变）。

### 3.7 观测链（tui-dev / task-69 + Lead）

- TUI `/hooks`：`HookTraceScreen`（最近 50 条配对记录：hook、mode、ok、durationMs、error/preview），
  app.py 的 `hooks_command` 读当前会话 `SessionQuery.hook_trace`（或直接 records 过滤），
  commands_builtin.py 增加 `("hooks", (), "show recent hook decisions", "/hooks", "hooks_command", False)`。
- headless NDJSON：journal 的 live emit 已经让 `{"type":"hook/invoked"|...,"hook":...}` 出现在 JSON 行流里；
  Lead 的验收测试解析 NDJSON 断言 hook 决策链。
- session_query：`hook_trace`（task-67）+ 测试；projection 订阅本轮不做（记录本身已可回放，
  等 UI 需要聚合时按 P2-4 的“消费者 ≥3 类”原则再加，plan 里写明）。

## 4. Lead 工作

- 写本 plan；bundle.toml 增加 hook-bridge（hook-observability 后）、hook-journal（其后）、
  prompt-extension、approval-policy（prompt 之后，因为要读 config 即可，不依赖 prompt 服务）；
  tests/kernel/test_bundle.py 三处顺序列表与 ACTIVE 断言同步。
- tests/integration/test_p2_bus.py：一次 Fake 模型 turn 产生 pre-step/request/model-selection/
  assemble/tool hook 的 invoked+result 记录，SessionQuery.hook_trace 可配对回放；
  headless --json 跑一条只读 turn，NDJSON 里出现 hook 记录；prompt extension 与 approval policy
  的端到端（assembled prompt 含扩展文本；approval policy allow 让需审批的工具通过）。
- 全量 pytest、ruff check/format、真实模型冒烟（headless INFO 日志 + NDJSON hook 记录）。
- 文档：docs/spec/hooks.md（observer API、bridge、journal、记录形状、hot/无 agent 排除）、
  docs/spec/session.md（hook record 结构）、docs/spec/tui.md（/hooks）、docs/spec/headless.md（NDJSON）、
  docs/spec/INTERFACES.md、README（如需）。
- commit 8：feat(kernel): hook observer bridge and session hook journal（含 65/66/67）；
  commit 9：feat(obs): hook trace in TUI and P2 consumers（含 68/69 + docs）。验证后 push。

## 5. 任务表

| 任务 | 负责人 | 写域 |
|---|---|---|
| task-65 observer + records + journal | plugin-dev | src/deeppyro/hooks/{service,records,__init__}.py、src/deeppyro/builtin/hook_journal.py、tests/hooks/、tests/builtin/test_hook_journal.py |
| task-66 kernel bridge | loader-dev | src/deeppyro/kernel/hook_bridge.py、src/deeppyro/builtin/hook_bridge.py、tests/kernel/test_hook_bridge.py、tests/builtin/test_hook_bridge.py |
| task-67 session 写入 + hook_trace | session-dev | src/deeppyro/agent/session.py、src/deeppyro/session_query/、tests/agent/、tests/session_query/ |
| task-68 P2-2 消费者 | llm-dev | src/deeppyro/builtin/{prompt_extension,approval_policy}.py、src/deeppyro/config.py、tests/builtin/ |
| task-69 TUI /hooks | tui-dev | src/deeppyro/tui/screens/hook_trace.py、src/deeppyro/tui/app.py、src/deeppyro/builtin/commands_builtin.py、tests/tui/ |

依赖：65 的 records/subscriber API 是 66/69 的合同（合同已写在 §3，可直接并行）；
67 的 `record_hook_event`/`hook_trace` 是 69 的合同；Lead 负责 bundle 行、集成测试、文档与提交。

### 5.1 验收中补的缺口（Lead，P2-2）

- 实模型冒烟发现 `[approval].hookMode/hookTools` 在真实链路上永远无法命中：
  `ApprovalRequest.tool` 从未被填充（只有测试手工构造），approval-policy 的
  `hookTools` 过滤形同虚设。修复：`src/deeppyro/tools/permissions.py` 的
  `_approve_escalation`、`check_write`、`check_command` 接收 keyword-only
  `tool: str | None` 并写入三种 ApprovalRequest；`write`/`edit` 传
  `self.name`，`pwsh`/`bash` 传 `self.name`。
- 回归：tests/builtin/test_approval_policy.py 增加 2 个真实 WriteTool 用例
  （越界写被 hook 拒绝且不弹 ask；sandbox escalation 同样被拒绝）；
  tests/tools/test_fs.py 断言 `write-outside-workspace` 与 escalation 请求的
  `tool`；tests/tools/test_shell.py 断言 `pwsh` escalation 请求的 `tool`。
- 文档：docs/spec/hooks.md §6.7、docs/spec/tools.md §4 说明 `tool` 的填充来源
  与匹配语义。

## 6. 执行记录

### 6.1 交付

| 任务 | 交付 | 验证 |
|---|---|---|
| task-65 | hooks/service.py 的 `HookObservation`/`add_observer`/`observed`；hooks/records.py；builtin/hook_journal.py | tests/hooks/test_observer.py 33、test_records.py 17、tests/builtin/test_hook_journal.py 16 |
| task-66 | kernel/hook_bridge.py（`HookEventScope` scope-first、跳过 `HOT_HOOKS`）、builtin/hook_bridge.py | tests/kernel/test_hook_bridge.py + tests/builtin/test_hook_bridge.py 22 |
| task-67 | AgentSession.`record_hook_event`（只收 hook/invoked、hook/result）；SessionQuery.`hook_trace` + builtin/session_query 透传 | tests/agent/test_hook_records.py 10、tests/session_query/test_hook_trace.py 13 |
| task-68 | builtin/prompt_extension.py（`system-prompt/assemble` 后置追加 `[prompt].extension`）；builtin/approval_policy.py（`[approval].hookMode/hookTools` → approval/request） | tests/builtin/test_prompt_extension.py 16、test_approval_policy.py 29（含 §5.1 两用例） |
| task-69 | tui/screens/hook_trace.py + `/hooks` 命令（数据源 session.hook_trace → SessionQuery.hook_trace → records 配对，最近 50 条） | tests/tui/test_hook_trace.py 13、tests/tui 383 |
| Lead | bundle.toml 五条新 row + tests/kernel/test_bundle.py 顺序/ACTIVE；`_hook_enabled` observed 守卫（9 处 agent dispatch、gate 两处、pipeline tool/call）；tests/integration/test_p2_bus.py 6 用例；全部文档 | 全量 pytest、ruff |

### 6.2 验收（对齐文档 §8）

- [x] P2-1：一次 turn 的 `hook/invoked`、`hook/result` 可从 session 日志回放。
  tests/integration/test_p2_bus.py 用真实 AgentSession 断言记录成对、scope.session
  为会话 id、热 hook（`llm/stream`、`agent/assistant-stream`）不落盘；
  live sink 与 durable log 的 seq 集合一一对应；SessionQuery.hook_trace 可配对。
- [x] P2-3：headless NDJSON 可见 hook 决策链（见 6.3 冒烟 #1，19 行 NDJSON、
  5 条 hook/invoked + 5 条 hook/result）。
- [x] 非目标守住：未新增 hook 名；未改 `_vendor/cordis_port`（架构闸门
  tests/architecture/test_import_boundaries.py 仍绿）；热 hook 不桥接不落盘；
  无 agent 的 hook（approval/*、session/created、session/disposed）不写 session
  日志。
- [ ] P2-4：结构复盘（见 §7，需评审后勾选）。

### 6.3 真实模型冒烟

- #1（P2-3 主验收）：temp `DEEPPYRO_HOME=D:/dshworkspace/.dsh-tmp/p2-smoke`，
  config 从 `C:/Users/74185/.dpro/config.toml` 复制；headless --json 只读问答。
  exit 0，19 行 NDJSON；hook/invoked 5、hook/result 5：
  `agent/pre-step`（scope {session,preset}、turn/step）、`model/selection`、
  `system-prompt/assemble`、`agent/turn-stopping`（result ok:true outcome:value
  durationMs:0 valueType:list preview:"[]"）、final "ok"。
- #2（消费者）：`[prompt].extension="SMOKE-EXTRA-RULE-42"`；
  `[approval].hookMode="deny" hookTools=["read"]`。read 的 `check_read` 恒 allow、
  不经过 gate，deny 不触发属预期（不是失败）。
- #3（approval-policy，修复前失败 → 修复后通过）：`approvalPolicy="ask"`、
  `hookMode="deny"`、`hookTools=["write"]`，让模型写 workspace 外路径。
  修复前：stderr "approval denied (headless mode): Allow this operation to run with
  danger-full-access for one call?"、exit 1（说明 hook 未答，落到 headless ask）。
  根因即 §5.1 的 `ApprovalRequest.tool` 未填充。修复后重跑：exit 0，
  `tool_result` 文本 `Error: write denied: denied by approval plugin`，
  `tools/execute` 的 hook/result error.name=PermissionDenied，turn/end completed，
  模型最终用一句中文说明写入被拒绝。
- 环境坑：PowerShell 5.1 `Set-Content -Encoding utf8` 会写 BOM，tomllib 报
  `TOMLDecodeError: Invalid statement (at line 1, column 1)`；必须
  `[System.IO.File]::WriteAllText` + `New-Object System.Text.UTF8Encoding($false)`。
  取 session 文件要过滤 `*.projections.json` 侧车，只取 `sessions/*.jsonl`。

### 6.4 验证与提交

- 全量：见 §6.5。
- 提交计划（对齐文档 §7）：commit 8 = hook observer/bridge/journal 与 session
  写入（task-65/66/67 + bundle 行）；commit 9 = 观测链与消费者（task-68/69 +
  ApprovalRequest.tool 修复 + P2 集成测试）；再一个 docs commit 收本 plan 与
  specs。全部验证后 push。

### 6.5 最终验证

- `uv run pytest -o addopts= -q`：**2733 passed in 117.42s**（P2 全部改动 +
  §5.1 修复之后）。
- `uv run ruff check src tests docs`：All checks passed。
- `uv run ruff format --check`：455 files already formatted。
- 冒烟 #3 修复后重跑：exit 0、tool_result 为
  `Error: write denied: denied by approval plugin`、turn 完成（见 6.3）。

## 7. P2-4 结构复盘

结论：**维持双总线（D1=A）**，不放开领域模块直接 import cordis。

- 现状：领域侧 HookService 是唯一 dispatch 面；cordis kernel 只通过
  builtin/hook_bridge.py 的 `HookBridge` 订阅 observer 并重发
  `hook/invoked`/`hook/result`，`HookEventScope` 做 scope-first thisArg。
- 消费者类别已 ≥3：决策（llm-retry、approval-policy、prompt-extension）、
  观测（hook-observability、TUI /hooks、headless NDJSON、SessionQuery.hook_trace）、
  桥接（HookBridge）。没有一类需要绕过 HookService。
- 合并为单总线的收益未被证据支持：现有插件全部通过 `ctx.service("hooks")` 与
  `ctx.effect` 即可工作；架构闸门
  tests/architecture/test_import_boundaries.py 保证 api/kernel 之外的模块不能
  import `_vendor`，合并会直接破坏该约束并重演框架耦合。
- 重新评估的触发条件：出现必须用 cordis filter/scope 语义才能表达的消费者、
  或 HookService dispatch 成为性能瓶颈（当前 hot hook 已排除在桥接/落盘外）、
  或需要第三方插件直接订阅 kernel 事件。届时以独立 plan 评估，不在 P2 内改。

