# M2b turn 内结束能力与策略文本

- 前置：M2a
- 估算：0.5–1 人日
- 写入范围：src/deeppyro/core/protocols.py（ToolContext）、src/deeppyro/agent/session.py（仅工具批后结束 turn 的接线）、
  src/deeppyro/builtin/goal.py（策略段注册）、tests/agent/、tests/goal/
- **本卡动 loop，是回归高风险卡：必须全量跑 tests/agent**

## 目标

支持 DSH 语义：工具在自主 complete/blocked 后可以要求在本 step 后结束物理 turn；
并注册 tool:goal 系统提示词策略段。

## 子任务

### M2b-1 ToolContext 结束回调
- ToolContext 增加 conclude_turn 能力（默认为 None，仅 AgentSession 注入）。
- 工具只能“请求”结束，不能直接改 turn 状态。

### M2b-2 AgentSession 接线
- _run_tools 返回后检查是否有 conclude_turn 请求；有则在本 step 结束后关闭 turn（reason 按 spec：completed/blocked），
  不再发起下一次模型请求。
- 无请求时行为与基线完全一致（回归 fixtures 逐记录比对）。
- 多个工具同时请求时取第一个，其余忽略并记录告警。

### M2b-3 策略文本与 wrapup
- 注册 tool:goal 段落，文本按 spec §5.4（DSH 固定文本）；数值与配置一致（blockedAfterConsecutiveRounds）。
- 自主 complete/blocked 成功后：v1 降级为“结束 turn + 记录 wrapup 待办”（deeppyro 无 additionalContexts），
  在 spec 里把该偏差写进 documented divergence。

### M2b-4 测试
- tests/agent/test_turn_conclude.py：请求结束 → 无额外模型调用、turn/end reason 正确、未请求不受影响。
- 回归：tests/agent 全绿。

## 验收

- [ ] 请求结束的用例无第二次模型调用（用 fake provider 计数断言）
- [ ] 未请求结束的基线行为逐记录不变
- [ ] 策略文本包含 spec 规定的阈值与措辞
- [ ] uv run pytest tests/agent tests/goal -q 全绿；ruff 干净

## 禁止

- 改 step/turn 状态机其它语义
- 引入 additionalContexts 机制（v1 不做）
- 让工具直接写日志或关 turn

## 回报格式

任务ID / 改动文件 / 回归结论（tests/agent 全绿证据）/ 契约偏离（wrapup 降级说明）/ 待确认。
