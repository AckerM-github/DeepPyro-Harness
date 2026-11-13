# Schedule 规格

本文记录 DeepPyro P1 的 schedule 能力：七种 timing 的建模、单文件持久化、单 asyncio timer 的到期投递、`schedule` 工具（四动作）与 `schedule` 服务插件。实现参考 DSH 的 `packages/schedule/**`（schedule / tool-schedule / util-time），但按 DeepPyro 的单进程 TUI/headless 形态做了裁剪；与参考实现的差异集中在第 11 节，接线需求集中在第 12 节。

## 1. 范围

- 本层负责：任务目录（create/list/update/delete/pause/resume）、持久化与恢复、到期计算、单 timer 调度、投递回执记录。
- 本层不负责：宿主 session 的创建/唤醒与消息写入（由 TUI/headless 的 `ScheduleHost` 实现负责）、bundle 行注册、`INTERFACES.md`。
- cron 只做「类型建模 + 校验」，不可执行：create 时返回 `ScheduleInputError(code="unsupported_timing")` 并提示改用 rrule（见 §11）。
- 本层不改 `agent/session.py`、`tui/**`、`api/**`、`bundle.toml`。

## 2. 文件布局

- @src/deeppyro/schedule/__init__.py：公开 API 重导出。
- @src/deeppyro/schedule/types.py：七种 timing、三种 target、`ScheduleRecord`、`DeliveryReceipt`、`SessionCreateSpec`、常量与 JSON 编解码；`ScheduleError` / `ScheduleInputError` / `ScheduleStoreError`。
- @src/deeppyro/schedule/timeutil.py：RFC3339 UTC 毫秒解析/格式化、本地时间格式化、时区校验、首次/下一次发生时间计算。
- @src/deeppyro/schedule/rrule.py：RRULE 子集解析与展开（自研引擎，见 §11）。
- @src/deeppyro/schedule/store.py：`ScheduleStore` 单文件原子 JSON 存储、损坏隔离（quarantine）。
- @src/deeppyro/schedule/manager.py：`ScheduleManager` 门面 + `ScheduleHost` 协议 + `ScheduleConfig`。
- @src/deeppyro/tools/builtin/schedule.py：`schedule` 工具（create/list/update/delete）与管理授权 prompt section。
- @src/deeppyro/builtin/schedule.py：`SchedulePlugin`，provide `schedule` 服务并注册工具与 prompt section。
- 测试位于 @tests/schedule/（`test_store`、`test_timeutil`、`test_rrule`、`test_manager`、`test_tool`）。

## 3. 数据结构

### 3.1 timing（七种，输入字段 snake_case）

| kind | dataclass | 字段 | 语义 |
| ---- | --------- | ---- | ---- |
| after | `AfterTiming` | `after_seconds` | 一次性，now + N 秒触发，1..31536000 |
| at | `AtTiming` | `at` | 一次性，RFC3339 绝对时刻，必须在未来 |
| every | `EveryTiming` | `every_seconds` | 固定间隔，60..604800 |
| daily | `DailyTiming` | `time`、`time_zone` | 每天墙钟时间；时区空表示系统本地 |
| weekly | `WeeklyTiming` | `time`、`time_zone`、`weekdays` | 每周指定星期几（ISO 1=周一..7=周日，升序去重） |
| cron | `CronTiming` | `expression`、`time_zone` | 仅建模/校验五字段，P1 不可执行 |
| rrule | `RRuleTiming` | `rrule` | 两行文本：`DTSTART...` + `RRULE:...` |

时间语义：
- 所有持久化时刻是 RFC3339 UTC 毫秒字符串（`2026-11-14T02:01:00.000Z`）。
- daily/weekly/rrule 按墙钟时间计算：不存在的本地时间（DST 跳过）被跳过，重复的本地时间取较早的 instant（`fold=0`）。
- weekly 的 `weekdays` 必须非空、唯一升序，否则 `invalid_rule`。
- rrule 支持 FREQ DAILY/WEEKLY/MONTHLY/YEARLY、INTERVAL、COUNT、UNTIL、WKST、BYDAY（含序号）、BYMONTHDAY、BYMONTH、BYHOUR、BYMINUTE、单个 BYSECOND；拒绝 sub-daily FREQ、BYYEARDAY、BYWEEKNO、BYSETPOS、多个 BYSECOND、COUNT+UNTIL 组合。展开上界 100 年。

### 3.2 target（三种）

- `SessionTarget(session_id)`：投递回已存在的会话（默认值为创建者会话，见 §11）。
- `NewOnceTarget(session_id="", create=SessionCreateSpec())`：首次投递时新建一个会话（会话 id 预留字段，宿主回填）。
- `NewTarget(create=SessionCreateSpec())`：每次 occurrence 新建会话。
- `SessionCreateSpec(cwd="", agent_preset="")`。

### 3.3 task 与回执

`ScheduleRecord` 字段：`id`（`schedule-<uuid4 hex>`）、`title`（去空白，≤120 字符）、`prompt`、`kind`、`timing`、`scheduled_at`、`status`（`active` / `paused` / `ended`）、`target`、`created_at`、`updated_at`、`last_delivery`、`delivery_history`。

`DeliveryReceipt` 字段：`scheduled_at`、`delivered_at`、`message_id=""`、`target_session_id=""`。宿主投递时可以返回自定义 receipt；返回 `None` 表示已投递但无回执，manager 会按当前扫描时刻合成一条。

工具与 inspect 面输出 camelCase task model（`task_model()`）：`id`、`title`、`prompt`、`kind`、`timing`、`target`、`status`、`scheduledAt`、`scheduledAtLocal`、`timeZone`、`createdAt`、`updatedAt`、`lastDelivery`、`deliveryHistory`。

## 4. 持久化

- 路径：`deeppyro_home()/schedules.json`（默认 `~/.dpro/schedules.json`，测试用 `DEEPPYRO_HOME` 隔离）。
- 形状：`{"version": 1, "tasks": {"<id>": <record json>}}`；record 的 timing/target 用 `kind` 区分。
- 原子写：同目录 `NamedTemporaryFile` + `flush` + `fsync` + `os.replace`，失败清理临时文件并抛 `ScheduleStoreError`。
- 读取：文件缺失视为空；JSON 损坏、版本不符、单条 record 非法都抛 `ScheduleStoreError`。
- 损坏恢复：`ScheduleManager` 构造时捕获 `ScheduleStoreError`，把原文件重命名为 `schedules.json.corrupt-YYYYmmdd-HHMMSS`（`ScheduleStore.quarantine()`）后以空目录启动，并记录 warning；隔离失败只记录日志，不阻塞启动。
- 单进程假设：没有跨进程锁；TUI 单实例可接受，headless 并发运行同一 `DEEPPYRO_HOME` 需人工避免。

## 5. 定时器与投递语义

`ScheduleManager` 每个实例只运行一个 asyncio timer task（名字 `deeppyro-schedule-timer`）：

- `start()`：幂等；创建 timer task。`stop()`：置停止标志、唤醒 timer、`wait_for` 最多 5 秒，超时则 cancel；幂等，未启动调用安全。
- 睡眠时长 = 距离最早 active `scheduled_at` 的秒数，**上限 60 秒**（扫描兜底，吸收系统休眠/时钟跳变）；无 host 或目录为空时按 `scan_interval_seconds` 轮询。
- 每轮唤醒在锁内做一次到期扫描：`status == active` 且 `scheduled_at <= now` 的任务按 `(scheduled_at, id)` 排序逐个投递。
- 一次性任务（after/at）投递后置 `ended`；循环任务投递后推进到「严格晚于当前时刻」的第一个 occurrence（rrule/every/daily/weekly）。进程停机/机器休眠导致错过多次 occurrence 时**只补投一次**，然后直接推进到未来，避免补投风暴。
- 投递失败（host 抛异常）不推进 `scheduled_at`，任务保持 active，下一次扫描（最早 `scan_interval_seconds`，且不超过 60 秒）重试。
- 目标 session 不存在（`host.session_exists()` 为 False）时任务直接置 `ended`，不投递、不重试。
- 投递后追加 `delivery_history`（按 `delivery_history_days` 和 `delivery_history_records` 双重裁剪），更新 `last_delivery`、`updated_at` 并持久化；持久化失败只记录日志，不中断 timer。
- 精度：定时精度受 asyncio 事件循环与 60 秒扫描上限影响，实际投递延迟通常 < 1 秒（空闲循环下）；不保证毫秒级。
- 重复投递风险：重启后按 `scheduled_at` 重新扫描，崩溃瞬间可能重复投递一次（与 DSH 相同，P1 接受并在 §11 注明）。
- 恢复：启动时从 store 加载全部任务；`paused` 任务不参与扫描，`ended` 任务只保留记录。

## 6. ScheduleHost 协议

`ScheduleHost` 是 runtime 与 manager 之间的唯一桥（`runtime_checkable` Protocol）：

- `async session_exists(session_id: str) -> bool`：目标 session 是否仍可投递。
- `async delegation_depth(session_id: str) -> int`：会话委派深度；工具用它拒绝子会话。
- `async deliver(task: ScheduleRecord, message: str) -> DeliveryReceipt | None`：写入消息并返回回执；`None` 表示成功但无回执；抛异常表示失败可重试。

`NullScheduleHost` 供测试与未接线时使用：`session_exists` 返回 False、深度 0、永不投递。`manager.configure_host(host)` 可在运行期替换 host 并唤醒 timer；`manager.host` 可读。

消息文本由 `manager.render_message(record)` 生成：`Scheduled task: <title> (occurrence <本地时间>)\n\n<prompt>`。宿主负责把它包成带 `source = {kind: "schedule", ...}` 的用户消息（需 Lead 在 `agent/session.py` 提供投递 API，见 §12）。

## 7. ScheduleManager 公开 API

- `ScheduleManager(store=None, *, host=None, config=None, now_fn=None)`：`now_fn` 返回 aware/naive datetime，测试注入 fake clock；默认 `datetime.now(UTC)`。
- `configure_host(host) -> None`、`host` 属性、`records` 属性（按 scheduled_at,id 排序的元组）。
- `async start()` / `async stop()`。
- `async tick(*, now=None) -> list[DeliveryReceipt]`：确定性触发一次扫描（测试主入口）。
- `async create(request: Mapping, *, actor_session_id=None) -> ScheduleRecord`：request 支持 timing 对象或 shorthand（`rrule` / `cron` / `weekly` / `daily` / `at` / `every_seconds` / `after_seconds`）或 `kind`。
- `async list(*, active=None, limit=100, offset=0) -> dict`：`{tasks, total, limit, offset}`；limit 1..500。
- `async view(task_id) -> ScheduleRecord | None`、`async delete(task_id) -> bool`。
- `async update(task_id, patch, *, expected_updated_at=None, actor_session_id=None) -> ScheduleRecord`：支持 title/prompt/timing/rrule/target/active；expectedUpdatedAt 做 CAS，冲突抛 `schedule_conflict`；空 patch 抛 `invalid_selector`。
- `task_model(record) -> dict`、`render_message(record) -> str`、`next_wakeup() -> str | None`（同步）、`async delegation_depth(session_id) -> int`（host 缺失/异常时 0）。

配置（`[schedule]` 表，`ScheduleConfig.from_config` 读取）：`delivery_history_days=30`、`delivery_history_records=200`、`new_session_interval_seconds=60`（仅保存，P1 未强制，见 §11）、`scan_interval_seconds=5`、`default_time_zone=""`（空 = 系统本地）。

## 8. 工具契约

只有一个工具 `schedule`（`parallel_safe = False`），`action` 枚举四动作：

| action | 必填参数 | 可选参数 | 行为 |
| ------ | -------- | -------- | ---- |
| create | title、prompt、rrule | target | 创建任务，返回 task model JSON；meta `{scheduleId, status}` |
| list | 无 | active、limit、offset | 返回分页 JSON；meta `{count, total}` |
| update | id | title、prompt、rrule、target、active、expectedUpdatedAt | 修改/暂停/恢复，CAS 冲突返回 `schedule_conflict` |
| delete | id | 无 | 删除并返回 `{"deleted": id}`，不存在返回 `unknown_task` |

参数与错误约定：

- 缺参/类型错误由 `tools/args` 辅助函数抛 `ToolError`（如 `title is required`）。
- `ScheduleInputError` 映射为 `ToolError("<code>: <message>")`，错误码包括 `invalid_rule`、`invalid_selector`、`not_future`、`unsupported_timing`、`unknown_task`、`schedule_conflict`、`invalid_prompt`；其他 `ScheduleError` 映射为 `schedule failed: ...`。
- 服务缺失（`ToolContext.services` 无 `schedule`）抛 `ToolError("schedule service is not available in this session")`。
- 子会话拒绝：执行前查 `service.delegation_depth(session_id)`，>0 抛 `ToolError("subagent_session: scheduled tasks are not available in child sessions")`；探测异常按 0 处理，绝不因宿主故障拒绝调用。需要 Lead 在 `api/types.py` 的 `CHILD_DISABLED_TOOLS` 追加 `schedule` 做双保险。
- 时间输入只暴露 `rrule`（两行 DTSTART/RRULE，帮助文本见 `RRULE_HELP`），与 DSH 模型工具一致；after/at/every 等由内部 API 或测试使用。
- 管理授权：prompt section `tool:schedule`（order 45，core rules=50 之前）注册 `MANAGEMENT_AUTHORIZATION` 原文；当 `visible_tools` 非空且不含 `schedule` 时返回空串，由 prompt 服务跳过。

## 9. SchedulePlugin

`src/deeppyro/builtin/schedule.py`：

- `SchedulePlugin(Service)`：`provide = "schedule"`、`plugin_id = "schedule"`、`inject = ["config"]`；构造时用 `[schedule]` 配置（或行内 config）建 `ScheduleManager`，`setup()` 注册 `schedule_tools()` 与 `tool:schedule` prompt section；`schedule_manager` 属性暴露 manager 供 runtime 接线。
- 不启动 timer：`start()/stop()` 由 runtime/headless 接线（见 §12）。
- 不注册 inspect/TUI 面；bundle 行由 Lead 添加（建议 `schedule` 在 sessions 之后、tools 之前或之后均可，`schedule-tools` 可合并进本插件，见 §12）。

## 10. 测试

- `tests/schedule/test_store.py`：缺失/损坏/版本/原子写/quarantine/路径。
- `tests/schedule/test_timeutil.py`：RFC3339、本地时间、DST 跳过、daily/weekly 首次与下一次；occurrence_time_zone（含 rrule TZID 解析）。
- `tests/schedule/test_rrule.py`：DAILY/WEEKLY/MONTHLY/YEARLY、COUNT/UNTIL/INTERVAL/BYDAY/BYMONTHDAY、TZID 墙钟、DST 不存在时间跳过、first_after、非法规则；与 `dateutil.rrule` 交叉校验（仅测试使用）。
- `tests/schedule/test_manager.py`：create/target/list/分页/pause/CAS/delete、一次性投递、循环 catch-up 单次投递、失败重试不推进、session 消失转 ended、无 host、持久化恢复、损坏隔离、timer start/stop（真实短 sleep）。
- `tests/schedule/test_tool.py`：四动作正常与错误码、参数校验、子会话拒绝、服务缺失、授权 section 可见性、工具元数据。

命令：`uv run pytest tests/schedule -o addopts= -q`、`uv run ruff check src/deeppyro/schedule src/deeppyro/tools/builtin/schedule.py src/deeppyro/builtin/schedule.py tests/schedule`。

## 11. 与 DSH 的差异 / 已知限制

1. `cron` 只建模与校验，执行一律返回 `unsupported_timing`（P1 允许延后；文档与工具帮助都提示用 rrule）。
2. rrule 使用自研纯 Python 子集引擎（`rrule.py`），不是 `python-dateutil` 适配层；测试用 dateutil 交叉校验子集行为。新增 `python-dateutil>=2.9` 与 `tzdata>=2024.1` 依赖（后者用于 Windows 命名时区）。
3. 工具是单个 `schedule` 工具 + `action` 枚举（create/list/update/delete），DSH 是 `schedule_create/list/view/edit` 多工具；工具 create 只暴露 `rrule` timing（DSH 模型工具同样只暴露 rrule）。
4. `target` 省略时默认投递给创建者会话（`this_session`）；DSH service 默认 `new_once`，DSH 工具要求 target 必填。
5. `new_session_interval_seconds` 仅保存未强制：多个 new 目标同时到期时不限速；同一 session 的多个到期任务也未合并成一条消息（DSH runtime 有此优化）。
6. 没有 `pendingDelivery` 跨重启恢复：重启后按 `scheduled_at` 重新扫描，崩溃可能重复投递一次；没有 remote API、没有 `run` 立即执行、没有 `catalog/history` 独立接口（工具 list/view 覆盖 P1 需求）。
7. 不实现 delivery 并发控制/批量合并/管理通知广播；单进程、无跨进程锁。
8. 工具 update 的 timing 输入同样只有 rrule（可改规则），after/at/every/daily/weekly 只可由内部 API 创建。
9. 无时区数据时的降级：tzdata 缺失时命名时区返回 `invalid_time_zone` 并附安装 tzdata 的提示；UTC 始终可用。

## 12. 接线状态（Lead / runtime owner）

1. ✅ `src/deeppyro/bundles/core/bundle.toml`：`schedule` 行已加（critical=false），紧跟 `compress`；工具注册在 SchedulePlugin 内，无独立 `schedule-tools` 行。
2. ✅ TUI：`src/deeppyro/tui/runtime.py` 的 `start_schedule()` 在 boot 后 `configure_host(_TuiScheduleHost(self))` 并 `await start()`；`app.on_mount` 调用；`runtime.close()` 先 `stop_schedule()`（stop + 取消后台 agent 任务）。headless 为一次性进程，P1 不启动 timer：schedule 工具可创建任务并存盘，但不自动投递（timer 在 TUI 进程下次启动后恢复）。
3. ✅ `_TuiScheduleHost`：`session_exists` 认当前会话或 store.open 成功；`delegation_depth` 恒 0；`deliver` 对 SessionTarget 当前会话走 `schedule_notice` + `session.followup(source={kind:"schedule",taskId,occurrence})`，其它会话打开后建静默 agent 投递并跟踪关闭；NewOnce 复用首次创建的会话，New 每次新建；回执 `target_session_id` 为实际会话。
4. ✅ `AgentSession.followup(text, *, source=...)` 已存在，用于投递（空闲时自动起 turn）。
5. ✅ `CHILD_DISABLED_TOOLS` 已含 `schedule`。
6. ✅ `docs/spec/INTERFACES.md` §11.4 已登记 `schedule` 服务与工具。
7. ⏸ `paths.py` 未新增 `schedules_path()`：`DefaultScheduleStore` 已用 `default_schedules_path()`，暂无其它调用方，暂不公开。
