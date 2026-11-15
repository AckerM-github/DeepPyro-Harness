# deeppyro goal 升级到 DSH 水平：实施计划

> 交接给编码 agent 的全景文档。任务卡在 docs/plan/goal-tasks/，契约在 docs/spec/goal.md。
> 基线：deeppyro HEAD 3281f1d（2026-11-15），工作区 clean。派发前 agent 必须重新自检 HEAD 与 clean。

## 1. 目标与非目标

**目标**：把 deeppyro 的 goal（当前 = 日志里一句话 + 提示词一段 + 单动作工具）升级到 DSH 形态：
生命周期状态机（phase）、CAS revision、activation 开关、自动续行 round、模型工具、人类命令、总线事件与最小投影。

**非目标（本轮不做）**：
- Web UI / HTTP 外壳（DSH 的 goal 客户端面板不做）；
- 多 goal 并发（DSH 也是每会话一个）；
- 等待子 agent（DSH waitForChildren）——v1 记录为 documented divergence；
- 新的记录名、新的 wire 词表、新 provider。

## 2. 现状 vs DSH 差距

| 维度 | deeppyro 现状 | DSH | 差距 |
|---|---|---|---|
| 记录 | goal/change 写 {goal: str\|null}（src/deeppyro/goal/service.py:22） | goal/change 版本 1 完整快照 + clear 墓碑 + revision | data schema 升 v2，旧格式只读兼容 |
| 生命周期 | 只有有/无 | phase active/paused/blocked/complete；动词 create/edit/pause/resume/complete/block/clear | 全缺 |
| CAS | 无 | {id, revision} 过期引用报错 | 全缺 |
| activation | 无 | armed/disarmed 进程本地；会话开始边界必 disarm | 全缺 |
| 自动续行 | 无 | round driver：idle 触发 → 预留 round → followup <goal_round> → pre-step 栅栏 → 消耗 round | 全缺（最大块） |
| 模型工具 | 单个 goal 工具（读/设/清） | get_goal / create_goal / update_goal + 权限门槛 + 策略文本 | 需替换 + 权限 |
| 人类面 | 无 | /goal 命令 | 全缺 |
| 投影 | 无 | projection 单元 goal v6 | 缺（可先只做 TUI 视图） |
| 总线 | 只发 goal/changed {session, goal}（service.py:188）；goal/activation-changed 仅 reserved | goal/changed 带 operation/ref/view；activation-changed 边界转发 | payload + 新事件 |
| 驱动依赖 | agent/status(emit)、agent/error、agent/pre-step(waterfall) 已有；inbox 三事件只到 UI sink；无 session 记录观察 API | 全有 | 需补两处观察设施 |
| 测试 | tests/test_goal.py 5 例 | — | 新增约 60–90 例 |

## 3. 目标契约摘要

权威版本：docs/spec/goal.md（M0 产出后冻结）。摘要：

**记录**（goal/change data v2，字段以 M0 源码取证为准）：

```json
{ "goal": { "id": "<goal id>", "revision": 1, "objective": "…", "phase": "active",
            "roundsStarted": 0, "maxGoalRounds": 256,
            "blockedReason": { "code": "model-reported", "message": "…" } },
  "version": 1 }
```

clear 写带 revision 的墓碑（goal: null + revision）。旧会话读到 {goal: "<string>"} 只在内存折叠为 active 视图，不回写。

**总线**：goal/changed（operation + 快照/ref + view）、goal/activation-changed；
agent/inbox/inserted|claimed|discarded 由 reserved 升 implemented（payload 对齐 DSH：inserted/discarded {message}，claimed {message, turn}）。

**驱动不变量**（全部可测）：
1. 只有真正进入 step 的 goal 来源 user/message 推进 round；人类消息/陈旧预留不推进；
2. round 消息 source = {kind:"goal", goalId, revision, round}，开独立 request series；
3. agent/pre-step 栅栏：phase 必须 claimed、!stale、内容与 source 完全一致、goal 仍 active+armed、round == roundsStarted+1；失败则恢复其他 claimed 消息并 reject；
4. pause（initiator != agent 且 running）→ cancel({keepInbox:true})；resume/fork 后保持 disarmed 直到显式 resume；
5. round 用尽 → block code round-limit；followup 失败 → block queue-failed。

## 4. 里程碑

| ID | 名称 | 产出 | 估算 | 前置 |
|---|---|---|---|---|
| M0 | 契约冻结 | docs/spec/goal.md（精确 schema/错误码/payload/不变量 + 证据行号） | 0.5–1d | 无 |
| M1 | 状态机与记录 | GoalService v2、CAS、fold、activation | 2–3d | M0 |
| M2a | 三工具与权限 | get_goal/create_goal/update_goal、权限门槛、blocked 阈值 | 1.5–2d | M1 |
| M2b | turn 内结束与策略文本 | ToolContext.conclude_turn、策略段、wrapup 降级 | 0.5–1d | M2a |
| M3a | 观察设施 | Session.subscribe、inbox 三事件上总线、catalog 状态 | 1–1.5d | M1 |
| M3b | 轮次驱动 | round driver、pre-step 栅栏、opt-in 挂载 | 3–4.5d | M3a + M2a |
| M4 | 人类面 | /goal 命令、TUI goal 行 | 1–2d | M2a,M3b |
| M5 | 投影与文档 | 最小 projection/TUI 口径、events.md、README、loop.md | 1d | M4 |
| M6 | 全量验收 | 集成测试、DSH fixture 回放、兼容审计 | 2–3d | M5 |
| 合计 | | | 13–19 人日 | |

MVP = M0+M1+M2a+M2b（4–6d，无自动续行）；完整 = +M3a+M3b（10–13d）。

## 5. 依赖图

```text
M0 契约冻结
 └─> M1 状态机/记录/activation
      ├─> M2a 三工具+权限 ─> M2b turn 内结束+策略文本
      └─> M3a 观察设施 ─> M3b 轮次驱动+栅栏+挂载
                              ├─> M4 /goal + TUI
                              └─> M5 投影/文档
M6 全量验收（最后）
```

## 6. 关键决策

| ID | 决策 | 理由 |
|---|---|---|
| D1 | 不新增记录名，仍用 goal/change，data 升级为快照 | 保持 wire 62 记录名兼容；DSH 也只用一个记录 |
| D2 | driver 做成独立插件（builtin/goal_round_driver.py + goal/round_driver.py），不改 AgentSession 主循环 | 与 DSH 同构；loop 保持纯净、可单测 |
| D3 | driver 默认不挂 bundle 行（opt-in） | 自动续行 = 无人值守烧 token；DSH 也是装了 driver 才跑 |
| D4 | 旧 {goal: str} 只读兼容，不回写 | 已发布 v0.1 会话不能丢 goal，也不能被静默改写 |
| D5 | v1 不做 waitForChildren | 避免把 subagent 生命周期耦合进 driver；列为 documented divergence |
| D6 | 记录观察用 Session.subscribe 进程内 API，不新增 session/event 记录或总线名 | 不扩大 wire/事件面；DSH 的 session/event 是服务内部通道 |

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 旧 goal/change 格式破坏 | 老会话打开崩/丢 goal | M1 fold 兼容 + fixture 测试 |
| pre-step 栅栏竞态 | round 消息被误当人类输入 / 人类消息被吞 | M3b 严格照 DSH：competingQueued/stale + claimed 校验 + inbox.prepend 恢复；M6 专测 |
| 默认开启 driver | 用户不知情烧 token | D3；未挂载行为零变化测试 |
| conclude_turn 改 loop | 影响所有工具行为 | 回调默认空，显式请求才结束；全量回归 |
| DSH 字段凭记忆写错 | 契约错、返工 | M0 必须先取证；spec 里无证据条目进 unresolved |
| 与 compaction 交互 | round 指令被压缩遮蔽 | 与 DSH 一致，文档说明 |

## 8. 建议 commit 序列

1. docs(goal): freeze DSH goal contract（M0）
2. feat(goal): snapshot records, revision CAS, strict replay（M1）
3. feat(goal): get_goal/create_goal/update_goal + policy（M2a）
4. feat(agent): tool-requested turn conclusion（M2b）
5. feat(session): record subscription + inbox bus events（M3a）
6. feat(goal): round driver and pre-step reservation fence（M3b）
7. feat(goal): opt-in driver wiring（M3b）
8. feat(cli): /goal command and TUI goal line（M4）
9. docs(goal): projection, events, README（M5）
10. test(goal): full acceptance（M6）

## 9. 验收口径

- 每卡：uv run pytest <本卡测试目录> -q 全绿；uv run ruff check src tests 干净。
- M6：uv run pytest -q 全量；DSH fixture 回放；events.md 与 core/event_catalog.py 一致；
  未挂 driver 的会话行为与基线逐记录 diff 无变化。
- 契约偏离：任何与 docs/spec/goal.md 不同的实现必须写进回报并等你裁决，不允许静默偏离。

## 10. 派发方式

见 README.md。核心：一次一张卡 + 对应 prompts/*.md；M0 先冻结契约；M3b 单独 review。
