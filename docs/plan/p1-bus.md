# P1 总线语义执行方案（deeppyro-bus-alignment.md §P1）

- 输入：`D:/dshworkspace/deeppyro-bus-alignment.md` §P1-1..P1-5、§7 commit 4–7
- 前置：P0 全部验收通过（见 `docs/plan/p0-bus-alignment.md`）
- 原则：语义对齐 DSH cordis（`D:/dshworkspace/_cordis_events.ts`），一次只改一类语义，每步可独立回滚

---

## 1. P1-1 翻转 waterfall 极性（对齐 DSH）

DSH 语义（`_cordis_events.ts:246-261`）：listener 收到 `next` 作为最后参数；**不调用 next() 即否决**后续链（含内置行为），返回值即最外层 listener 的返回值；多次调用 next() 抛错。

现状（`src/deeppyro/hooks/service.py`）：返回非 None 短路；返回 None 且未调用 next 时服务自动续链（即"不调 next 也续链"）。这是权威语义的反向。

改动：
- `_waterfall`：listener 返回 None 且未调用 next → 视为 **veto**，返回 `None`（不再自动续链）；显式调用 `next()` → 返回下游结果；返回非 None → 短路（保持不变）。
- fallback 仍表示"链尾行为"：无 listener 或所有 listener 都调用 next 时执行。
- 同步 `docs/spec/hooks.md` §2.1 与 §7（Differences from DSH 改为"已对齐"），补一段迁移说明：现有 listener 若只想观察、不拦截，必须显式 `await next()`。

迁移面：
- in-tree listener 全部显式调用 next（P0-2 的 llm-retry 已按新极性写；工具三件套的内置行为在 pipeline 的 fallback 里，不受影响）。
- 约 23 个 `tests/hooks/test_service.py` 用例 + 少量 agent/tools 集成测试需按新语义更新；禁止放宽断言掩盖回归。
- 验收：veto（不调 next 且返回 None）、短路（返回决策）、续链（调用 next）三条路径各有测试；一个 listener veto 后下游 listener 与 fallback 都不执行。

## 2. P1-2 补齐原语：bail / parallel / once / global

- `bail`：同步版 serial —— 按序调用，遇到第一个非 null/false/undefined 返回值停止并返回它。
- `parallel`：`Promise.allSettled` 语义 —— 并发执行全部 listener，聚合错误（DSH `AggregateError`）；deeppyro 用 `asyncio.gather(return_exceptions=True)` 聚合为 `HookAggregateError`。
- `once`：listener 只触发一次（DSH EventOptions 无 once，但 deeppyro 作为便利原语补；默认不启用）。
- `global`：DSH `EventOptions {prepend, global}` 的 global 语义（跨 scope 可见）；与 P1-4 的 scope 过滤一起实现。
- 可选：dev 模式按 `HOOK_MODES` 校验调用方式（waterfall hook 被 emit 调用等直接报错）。

## 3. P1-3 payload 冻结与取消检查

- 派发前对 payload 做**递归只读冻结**（`types.MappingProxyType` + tuple，对照 DSH `deepFreeze`），listener 只能返回新值，不能就地改 payload；对应测试：修改 payload 抛 `TypeError`。
- waterfall 每一跳检查 `payload["signal"]`（存在时）：已 abort → 直接返回/抛 `AbortedError`，不再进入下游 listener。
- 注意：工具 hook payload 里的 `call`/`agent` 是活对象，冻结只做浅层字段容器（dict/list）递归，自定义对象保持引用，文档写明边界。

## 4. P1-4 scoped handlers

- `hooks.on(name, handler, *, scope=None, global_=False)`：scope 形如 `{"agent": agent_id}` / `{"layer": "..."}` / `{"preset": preset_id}`。
- `waterfall`/`serial`/`emit` 增加可选 `scope=` 参数；派发时只调用 global listener 与 scope 匹配的 listener。
- 验收：Lead 与 subagent 同时注册同名监听器，互不串台（一个 team preset 会话与一个 default 会话并行跑，各自只收到自己的事件）。
- payload 里的 `agent` 字段（P0-4 已铺路）作为默认 scope 来源：`scope={"agent": payload["agent"].id}`。

## 5. P1-5 按消费者补 hook（准入：有 in-tree 消费者 + 有测试）

| hook | 模式 | 消费者 | 备注 |
| --- | --- | --- | --- |
| `system-prompt/assemble` | waterfall | prompt 服务 / 插件追加 section | payload `{agent, turn, step, sections}` |
| `llm/stream` | emit | TUI/headless 观测 | payload `{frame, provider, model}`；与 agent/assistant-stream 区分（provider 层） |
| `tool/call` | emit | 观测/job 记账 | 在 tool/call 记录落盘后发 |
| `session/created` | emit | projections/session-query 缓存失效 | payload `{sessionId, cwd}` |
| `session/disposed` | emit | 资源回收 | payload `{sessionId}` |
| `approval/request` | waterfall | approval 插件 | 返回 allow/deny/ask/cancel |
| `approval/asked` | emit | 观测 | 已发起审批 |
| `approval/decided` | emit | 观测/审计 | 审批结论 |
| `model/selection` | waterfall | 按 preset 改模型 | payload `{agent, provider, model}` |
| `agent/error` | emit | 观测/重试统计 | payload `{agent, error, phase}` |

每个 hook 必须同时交付：常量（hooks/types.py，加入 HOOK_NAMES/HOOK_MODES）、payload 文档（docs/spec/hooks.md §4/§5）、至少一个 in-tree 消费者或观测接线、至少一个测试。没有消费者的 hook 不放开。

## 6. 提交序列（对齐文档 §7 的 4–7）

1. `feat(hooks): align waterfall polarity with cordis (no-next = veto)` + docs
2. `feat(hooks): frozen payloads, cancellation checks, bail and parallel`
3. `feat(hooks): scoped handlers and global listeners`
4. `feat(hooks): consumer-driven hook events`（每个 hook 单独 commit 亦可）

## 7. 验收

- 极性：veto/short-circuit/next 三路径测试；全量绿。
- 冻结：listener 就地改 payload 失败；signal 中止不再派发。
- scope：Lead/subagent 不串台测试。
- P1-5：每个 hook ≥1 消费者 + ≥1 测试。
- 全量 `uv run pytest -o addopts= -q`、`ruff check src tests docs`、`ruff format --check` 全绿；真实 TUI 冒烟各一次。
