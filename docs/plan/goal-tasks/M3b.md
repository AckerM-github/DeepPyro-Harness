# M3b 轮次驱动：round driver、pre-step 栅栏、opt-in 挂载

- 前置：M3a（观察设施）+ M2a（工具，便于端到端验证）；M2b 建议已合入
- 估算：3–4.5 人日
- 写入范围（新增为主）：src/deeppyro/goal/round_driver.py、src/deeppyro/builtin/goal_round_driver.py、
  src/deeppyro/builtin/agents.py（on_created/live_agents）、src/deeppyro/bundles/core/bundle.toml、
  tests/goal/test_round_driver.py、tests/goal/test_fence.py、tests/integration/（若目录存在）
- **风险最高卡：闸门/竞态必须单独 review**

## 目标

实现 DSH 同构的 round driver：idle 时自动为 active+armed 的 goal 续行一轮；
pre-step 栅栏保证只有被预留的 round 消息真正进入 step；默认关闭（opt-in），未启用零行为变化。

## 子任务

### M3b-1 驱动状态机（goal/round_driver.py）
- 状态：idle / reserving / queued / claimed / admitted / cancelled。
- drivable(goal) = phase active && activation armed。
- readyToDrive = 插件就绪 && 未 stopping && agent 仍活 && agent 空闲 && !competingQueued。
- drive()：先确认记录已持久（deeppyro append 即 fsync，spec 已记录该假设）；
  roundsStarted >= maxGoalRounds → block code round-limit；
  否则 round = roundsStarted+1，构造 <goal_round> 文本（原文取自 spec §7），
  followup(message, source={kind:"goal", goalId, revision, round})；
  失败 → block code queue-failed。
- requestDrive 单飞；drive 串行；同一 session 同时只允许一个预留。

### M3b-2 事件接线
- 监听：agent/status（空闲触发 requestDrive）、agent/error（disarm）、agent/disposed（清状态）、
  goal/changed（needsCheckpoint，pause 且 running 且 initiator != agent → cancel keepInbox）、
  agent/inbox/inserted（非本轮预留 → competingQueued=true + stale=true）、
  agent/inbox/claimed（匹配 → claimed）、agent/inbox/discarded（匹配 → cancelled）、
  Session.subscribe 的 user/message（匹配 → admitted，推进 roundsStarted 由 M1 fold 规则负责）、
  turn/end（max-tokens → disarm；aborted → 按 claimed/admitted 处理）。

### M3b-3 pre-step 栅栏
- 监听 agent/pre-step：若提交消息含 source.kind == "goal"，执行 spec §7.5 校验清单。
- 失败：其余 claimed 消息用 agent.inbox.prepend 恢复到 next-step → 标记 stale → requestDrive → 返回 reject。
- 通过：继续原 fallback；abort 或后续 reject 时恢复其他消息。
- 必须处理“人类消息与预留同批 claim”的恢复顺序。

### M3b-4 挂载与 opt-in
- AgentsPlugin 增加 on_created 回调 / live_agents()，供 driver 绑定 session→agent；只驱动 root agent（排除 team/subagent）。
- 新增 builtin/goal_round_driver.py 插件，bundle.toml 增加行，但**默认配置 driver=false**；
  开启方式（[goal] driver=true 或等价）写进 README 与 spec。
- 未启用时不得注册任何 driver 监听、不得发任何事件（测试断言）。

### M3b-5 测试
- 单测（假 agent/inbox/clock）：15 条不变量（ready 闸门、预留、stale、pause cancel、round-limit、queue-failed、
  error/disarm、resume 重驱、单飞）。
- 集成（真 AgentSession + fake provider）：一轮全链路（followup → pre-step 栅栏 → user/message → round+1 → turn/end → 下一轮）；
  陈旧预留拒绝且消息不丢；人类消息插入竞争；round 上限；重启 rehydrate 后保持 disarmed。
- 未启用回归：与基线会话记录序列完全一致。

## 验收

- [ ] spec §7 的 8 组不变量每条至少一个测试
- [ ] 栅栏失败场景下，被 claim 的人类消息 100% 恢复（无丢失断言）
- [ ] 未启用 driver 时行为零变化（记录级 diff）
- [ ] uv run pytest tests/goal tests/agent -q 全绿；ruff 干净

## 禁止

- 把 driver 逻辑写进 AgentSession 主循环
- 默认开启 driver
- 新增记录名/总线名绕过 M3a
- 凭记忆写 <goal_round> 模板（以 spec 原文为准）

## 回报格式

任务ID / 改动文件 / 不变量测试映射表 / 集成测试结论 / 竞态分析说明 / 待确认。
