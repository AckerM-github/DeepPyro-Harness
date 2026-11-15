# M3a 观察设施：Session.subscribe 与 inbox 总线事件

- 前置：M1
- 估算：1–1.5 人日
- 写入范围：src/deeppyro/session/session.py、src/deeppyro/session/store.py、
  src/deeppyro/agent/session.py（仅 _inbox_event 区域）、src/deeppyro/core/events.py、
  tests/session/、tests/agent/、tests/core/
- 不改 driver（M3b 才写）

## 目标

补两处驱动所需的观察设施：
1. 记录级订阅（替代 DSH 的 session/event），不发新记录、不发新总线名；
2. agent/inbox/inserted|claimed|discarded 上总线（当前只发 UI sink）。

## 子任务

### M3a-1 Session.subscribe
- Session 增加 subscribe(cb) / unsubscribe：每次 append 成功后回调 (record_type, data, seq)；返回取消函数。
- 回调异常必须隔离（记录日志，不影响 append/会话）。
- 无订阅者时行为与性能与基线一致（不得为订阅引入额外写盘）。
- 提供 sessions 服务层包装（按 session_id 注册）供 driver 使用。

### M3a-2 inbox 事件上总线
- AgentSession._inbox_event 在保留 UI sink 的同时 hooks.emit 同名事件。
- payload 严格对齐 spec §6（inserted/discarded {message}，claimed {message, turn}）。
- 顺序：先 append agent/inbox/spliced 记录，再 emit 总线事件。
- 注意 claimed payload 需要 turn，检查 Inbox.claim 调用链是否需要传 turn 或由 AgentSession 补齐。

### M3a-3 catalog 更新
- core/events.py 的 _PRODUCERS 增加三个 inbox 事件（producer agent.session）；
  如 catalog 是自动生成则更新生成脚本后重新生成 core/event_catalog.py。
- 三个事件状态从 reserved 升 implemented，测试断言 catalog 一致（无 producer 缺失、mode 不冲突）。

### M3a-4 测试
- tests/session/test_session_subscribe.py：回调顺序、异常隔离、取消、无订阅零变化。
- tests/agent/test_inbox_events.py：三事件 payload、顺序（spliced 记录先于总线事件）、claim 带 turn。
- tests/core/test_events.py：catalog 一致性。

## 验收

- [ ] uv run pytest tests/session tests/agent tests/core -q 全绿
- [ ] 事件 payload 与 spec §6 逐字段一致
- [ ] 无订阅者回归：基线会话事件序列不变
- [ ] ruff 干净

## 禁止

- 新增记录名或 session/event 总线名
- 修改 inbox 的 splice/claim 语义
- 提前写 driver

## 回报格式

任务ID / 改动文件 / payload 对照表 / 测试数 / 契约偏离 / 待确认。
