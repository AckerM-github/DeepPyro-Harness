# M2a 三工具与权限

- 前置：M1
- 估算：1.5–2 人日
- 写入范围：src/deeppyro/tools/builtin/goal.py、src/deeppyro/builtin/goal.py、tests/goal/、tests/tools/
- 谨慎触碰：src/deeppyro/goal/service.py（仅补权限辅助函数）

## 目标

把单动作 goal 工具替换为 DSH 的三个工具，并实现权限门槛与 blocked 阈值。

## 子任务

### M2a-1 工具替换
- 新增 get_goal() / create_goal(objective, max_goal_rounds?) / update_goal(goal_id, revision, action, objective?, max_goal_rounds?, blocked_reason?)。
- 输入 JSON schema、输出紧凑 JSON、错误码严格按 spec §5。
- 旧 goal 工具保留 1 个 minor 作为兼容壳（内部转发），并在 description 标 DEPRECATED。

### M2a-2 权限门槛
- 仅对 root agent 的开放 turn 生效（tool 调用时通过 ToolContext.turn 判断）。
- create/edit/pause/resume 要求当前 turn 存在人类直接消息：扫描当前 turn 的 user/message，source.kind == "user"。
- subagent / 非人类来源调用被拒，错误码按 spec。
- 工具 resume 拒绝持久 paused goal（GOAL_TOOL_RESUME_PAUSED）；人类 resume 路径留给 M4。
- complete/blocked 接受当前 round。

### M2a-3 blocked 阈值
- blockedAfterConsecutiveRounds 默认 3：同一 blocker 连续 round 计数，达阈值才允许 blocked。
- 持久 blockedReason {code:"model-reported", message}，message 非空校验。

### M2a-4 配置
- 新增 [goal] 配置项（maxGoalRounds 默认 256、blockedAfterConsecutiveRounds 默认 3）的读取与默认值测试；
  配置落点按现有 config 模式（参考 src/deeppyro/config.py 与插件 config 传递方式）。

## 验收

- [ ] 工具 schema/输出/错误码与 spec §5 逐字段一致（用表驱动测试）
- [ ] 权限矩阵测试：root+人类 / root 无人类 / subagent / paused 场景
- [ ] blocked 阈值边界（2 轮拒、3 轮过）
- [ ] uv run pytest tests/goal tests/tools -q 全绿；ruff 干净

## 禁止

- 改 agent 主循环
- 实现 driver 或 /goal 命令
- 改记录 schema（M1 已定）

## 回报格式

任务ID / 改动文件 / 测试数 / 工具输出样例 / 契约偏离 / 待确认。
