# 原生会话日志 schema 1（Native session log schema 1）

| 项目 | 值 |
| --- | --- |
| 文档 | `docs/native/schema.md` |
| Schema 版本 | `1`（header 字段 `schema:1`） |
| 状态 | M0 冻结、M1–M2 实现完成；M3/M4 未改 schema；0.2.0 RC 以 headless 默认 native / TUI 仍 v4 交付（2026-11-18，未打 tag） |
| Owner | Lane Y（`core/`、`session/`、`agent/`、`prompt/`、`compact/`） |
| 取代 | `docs/spec/session.md`（`session.v4`） |
| 权威来源 | `docs/adr/0001-three-track-native-agent.md`（Accepted；M0–M4 完成）；研究报告 `deeppyro-native-python-agent-research.md` §3.3–3.5、§5.3、§9.3–9.5 |
| M1 落点 | `src/deeppyro/core/messages.py`、`src/deeppyro/session/schema.py`、`src/deeppyro/session/fold.py`、`src/deeppyro/observability/trace.py` |
| M2 落点 | `src/deeppyro/agent/{loop,run_state,checkpoint,budget,context_plan,events,native_provider}.py`；`session/schema.py` 增加 3 个 run 事件；headless `DEEPPYRO_NATIVE_LOOP` / `--legacy-v4` 开关（native 为默认，`--native-loop` 为弃用别名） |
| 冲突处理 | 与 ADR 冲突时以 ADR 为准；偏差在 §12 显式记录 |

## 1. 状态与范围
### 1.1 不变量
M1 起产品只写、只读 native schema 1；以下为 review 硬门槛：
1. 一种会话格式、一个消息模型、一套事件类型；不得出现第二套 wire 类型或事件目录（ADR §4 规则 5）。
2. 会话日志只记录事实：消息、状态变更、外部决定（approval/tool 结果）。token/cost/latency 明细属于 trace store（§8），不进 session log。
3. 消息以稳定 `message.id` 定位；没有按 seq 区间的 surface 替换，也没有区间投影概念。
4. 事件只分两类：`EventKind` 核心事件，以及 `plugin.<id>.<event>` 命名空间的插件事件；没有 `ignorable` 兼容开关。
5. `seq` 严格单调递增、逐条 fsync；损坏显式拒绝，不猜测、不静默修复（唯一例外是 §7.4 的悬空 tool call 派生修复）。
6. trace store 与 session log 物理分离，通过 `run_id`/`trace_id` 关联；session log 不承载高写入观测数据。

### 1.2 零兼容（ADR D2）
- 产品只读写 native schema 1。不提供 v4 reader、`V4Codec`、双写、`dpyro migrate` 命令（ADR D2/D3 删除项）。
- 旧数据 `session.v4.jsonl`（含 `.zstd`）不被产品读取；需要旧数据的用户只能使用 repo 之外的一次性导出脚本。
- 产品不扫描旧 `projectKey` 目录；只枚举 `~/.dpro/sessions/*.jsonl`。
- 读到 v4 header（`{"type":"session","version":4,...}`）必须显式失败（`SessionV4Error`，§9），不能降级、不能忽略该文件继续启动。
- schema 是硬边界：`schema != 1` 一律拒绝。破坏性变更提升 schema 号并让旧 reader 显式拒绝；schema 1 内部只允许“读取器能力增长”（新增 EventKind 由新版本读取器支持，旧读取器拒绝，不做跨版本向前兼容承诺）。

### 1.3 被删除的 v4 机制
| v4 机制 | schema 1 处理 | 理由 |
| --- | --- | --- |
| `surfaceOp`（append / `{"op":"replace","startSeq","endSeq"}`） | 删除。消息按创建顺序存在；修改用 `message.replaced`，压缩用 `compaction.done` | 稳定 id 取代区间，避免 seq 区间与消息 id 两套坐标 |
| `sourceEventSeqs` | 删除；派生只依赖事件顺序与覆盖关系 | 派生可从日志顺序完全重建，冗余校验信息只会漂移 |
| `ignorable` | 删除；未知事件只有插件命名空间一种合法形态，按命名空间跳过 | 兼容开关是 ADR 明令禁止的后门（§4 规则 3） |
| 62 个 slash 类型（`user/message`、`tool/result` 等） | 30 个 dotted `EventKind`（§5：M1 24 个 + M2 3 个 run 事件 + 3 个 `task.*` 事件） | 事件是类型而非字符串，不保留幽灵事件 |
| Header camelCase（`createdAt`、`isSeeded`、`agentPreset`、`parentSession`） | snake_case；`version` 改为 `schema`（§3） | Python-first（ADR D1/D3） |
| envelope `time` | `ts`，整数毫秒 UTC | ADR §7-2：`time` 留给时长字段 |
| assistant 消息内嵌 `stream` 快照与 `usage` | 删除；流式细节与 token 计数只进 trace store（§8） | 会话日志瘦身、隐私、回放速度（报告 §9.3） |
| tool 结果图片另写一条 user/message（`source.kind="ptc-mode"`） | 直接作为 `ImagePart` 放进 role=tool 的 `tool.result` 消息 | 一个消息模型，不再为 wire 形状绕路 |
| `fold_surface(strict=False)` 宽容模式 | 删除；reader 只有严格模式 | 容忍模式服务于修剪过的 v4 fixture，native 不需要 |

### 1.4 与当前代码的显式断裂
以下替换是有意决策，不是遗漏：
- `core/wire.py` 的 `WireModel(extra="allow")`、`Message(toolCallId,isError)`、`ToolCallPart(type="tool-call")` 不再用于会话消息；native 消息见 §6。
- `session/events.py` 的 v4 header/envelope 校验与 `header_line_dict` 顺序整体退役，由 `session/schema.py` 的 `NativeCodec` 替代。
- `session/derive.py` 的 interval 折叠、`sourceEventSeqs` 校验退役，由 `session/fold.py` 替代（§7）。
- `session/writer.py` 的 zstd frame 写入不启用（§2.5）；compact UTF-8 JSONL + LF 保留。
- `agent/session_tools.py` 中 `assistant/message` 的 `stream`/`usage`、`tool/result` 的 `surfaceOp`/`sourceEventSeqs` 字段删除；tool 图片的独立 user 消息 hack 删除。`core/wire.py` 可在 M1 的 provider adapter 内短期存在，但不得进入 session 存储或消息模型（ADR §4 规则 1）。

## 2. 物理格式
### 2.1 路径与命名
- 默认目录 `~/.dpro/sessions/`；环境变量 `DEEPPYRO_SESSION_DIR` 可覆盖（与其他 `DEEPPYRO_*` 一致）。
- 会话文件 `<sessions_dir>/<id>.jsonl`，扁平，没有每个会话的子目录（ADR D2；报告 §3.8）。
- `id` 同时是文件名主干：`^session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`，沿用 `new_session_id()` 的 `session-<uuid4>`，文件名安全。
- 不存在 `session.v4.jsonl`、`.zstd`、`meta.json`、`projectKey` 分片目录。
- `ImagePart.attachment` 可为绝对路径或 `sha256:<64hex>` 内容引用；附件存储布局不属于 schema 1 范围。

### 2.2 行格式
- UTF-8、无 BOM；每行一个 JSON object；紧凑编码 `json.dumps(..., ensure_ascii=False, separators=(",",":"))`；行末 `\n`。
- 不允许空行、注释、行尾空格；最后一行必须以 `\n` 结束。
- 单行保护上限 32 MiB（reader guard，防 DoS），超过抛 `LineTooLargeError`；正常工具结果应在工具层截断。
- JSON null 策略：session log 可空字段一律省略不写 `null`；唯一例外是 header `parent_id`（fork 判别语义，必须写 `null` 或 id）。

### 2.3 Header 原子写
1. 目标目录创建临时文件 `.<id>.jsonl.tmp`。
2. 写入 header 一行，flush + `os.fsync`。
3. `os.replace` 到 `<id>.jsonl`，随后 fsync 目录（POSIX；Windows 省略目录 fsync）。
4. 之后才允许 append 事件。Header 只经此路径写一次；reader 看到“首行不是 header”或“出现第二个 header”即判定损坏。

### 2.4 Append 与 fsync
- 每个 append 调用写入一批事件，`seq` 从最后一条 +1 连续分配。
- 顺序：`write()` → `flush()` → `os.fsync(fd)`。fsync 失败视为写入失败：truncate 回调用前偏移量并抛出；已 fsync 的前缀保持有效。
- M1 默认每次 append 都 fsync（正确性优先）；批量合并 fsync 需 benchmark 支撑，不改变 schema。
- 单写者：写入进程打开会话时以 `O_EXCL` 创建 `<id>.jsonl.lock`（内容 pid + 创建毫秒），关闭时删除；第二个写者抛 `SessionLockedError`。崩溃残留锁由 M2 doctor 清理，schema 1 不自动抢占。

### 2.5 压缩决定
schema 1 不启用 zstd（采纳 ADR §7-5）：文件永远是 `.jsonl` 明文行。理由：扁平行级 append+fsync 已足够；压缩属容器层，会引入分帧/撕裂尾部语义，收益需 append 基准证明。未来若增加，只能作为独立容器重新分帧，不改变本文件的 header/envelope/EventKind。

### 2.6 读取
- 严格单遍读取：逐行 JSON 解析 → envelope 校验 → 事件层校验；任何失败立即抛出，不跳过损坏行。不提供截断尾部容忍：最后一行不完整即 `CorruptLineError`（fsync 后不会出现半行；半行只可能来自外部破坏）。
- 读取不执行工具、不调用模型、不写文件。

## 3. Header
### 3.1 字段
Header 是文件第一行，且只出现一次，是封闭对象（未知字段抛 `HeaderError`）：
| 字段 | 类型 | 必填 | 约束与语义 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定字面量 `session` |
| `schema` | integer | 是 | 固定 `1`；其他值抛 `UnsupportedSchemaError` |
| `id` | string | 是 | 见 §2.1 正则；必须等于文件名主干，否则 `HeaderError` |
| `created_at` | integer | 是 | UTC epoch 毫秒，`>=0`；会话时间线起点（不是首条事件时刻）：新建会话为创建时刻，fork 继承源 header.created_at（§10.1） |
| `cwd` | string | 是 | 初始工作目录绝对路径；写入时取 `Path.resolve()` 后的字符串 |
| `preset` | string | 是 | agent preset id，`^[a-z][a-z0-9-]{0,63}$`，如 `standard`；preset 版本在 `preset.selected` 事件 |
| `parent_id` | string 或 null | 是 | 根会话 `null`；fork 为源会话 id；subagent 为父会话 id。格式同 `id` |
| `origin` | string | 是 | 枚举：`user`（交互）、`headless`（一次性 CLI）、`subagent`（被委派）、`fork`（复制） |
| `delegation_depth` | integer | 是 | `>=0`；根用户会话 0；subagent = 父 +1；fork = 源深度 |
header 不含 `run_id`、`title`、`is_seeded`、`archived`、`updated_at`；标题是事件（`session.title`），归档/索引属于 M5 文件系统操作。

### 3.2 示例
```json
{"type":"session","schema":1,"id":"session-7f3a9c1e-2b4d-4e6f-8a90-1c2d3e4f5a6b","created_at":1763390000000,"cwd":"/home/dev/project","preset":"standard","parent_id":null,"origin":"user","delegation_depth":0}
```

### 3.3 为什么 header 没有 `run_id`
一个会话的生命周期可包含多个 Run：崩溃后 resume 开新 run、fork 后子会话继续产生新 run、同一会话后续轮次也可能属于新 run。Header 在第一次事件之前一次性写入，此时还没有 run；把 `run_id` 放进 header 要么迫使 header 重写（破坏原子写/不可变假设），要么写入后来不再正确的值。因此：
- `run_id` 放在事件 envelope（§4）：只有确实属于某次执行的事件才携带；resume 后新事件自然带新 `run_id`。同一会话内 `run_id` 唯一；fork 复制历史事件时清除被复制事件的 `run_id`/`trace_id`（§10）。

## 4. 事件 envelope
### 4.1 字段
除 header 外，每行是一个事件；envelope 是封闭对象，出现下表之外字段抛 `EventFieldError`：
| 字段 | 类型 | 必填 | 约束与语义 |
| --- | --- | --- | --- |
| `type` | string | 是 | `EventKind` 值（§5），或匹配 `^plugin\.[a-z][a-z0-9-]{0,31}\.[a-z][a-z0-9._-]{0,63}$` 的插件事件 |
| `seq` | integer | 是 | 从 1 开始、每行 +1、绝不重用；writer 分配 |
| `ts` | integer | 是 | UTC epoch 毫秒；非递减；`>= header.created_at`；writer 用 `max(now_ms(), 上一条 ts)` 消除时钟回拨；fork 复制事件原样保留源 ts |
| `data` | object | 是 | 事件载荷；形状由 `type` 决定（§5） |
| `run_id` | string | 否 | `^run_[0-9a-f]{16}$`；属于某次 Run 的事件必填（§4.2） |
| `turn_id` | string | 否 | `^turn_[0-9a-f]{16}$`；turn 内事件必填 |
| `step_id` | string | 否 | `^step_[0-9a-f]{16}$`；step 内事件必填 |
| `trace_id` | string | 否 | `^[0-9a-f]{32}$`（W3C trace id 形状）；与 trace store join 用 |
可空关联字段不写 `null`（§2.2）。

### 4.2 关联字段要求
| 事件类 | run_id | turn_id | step_id | trace_id |
| --- | --- | --- | --- | --- |
| `turn.started` / `turn.ended` | 必填 | 必填 | 禁止 | 必填 |
| `step.started` / `step.ended` | 必填 | 必填 | 必填 | 必填 |
| `assistant.message` / `tool.call` / `tool.result` | 必填 | 必填 | 必填 | 必填 |
| `approval.requested` / `approval.decided` | 必填 | 必填 | 必填 | 必填 |
| `user.message` / `system.message` / `developer.message` | 可选 | turn 内必填 | 禁止 | 可选 |
| 状态/元数据事件（`*.selected`、`session.title`、`goal.changed`、`todo.written`、`deliverable.presented`、`plan.mode`、`workspace.cwd_changed`、`sandbox.mode`、`permission.policy`） | 可选 | 可选 | 可选 | 可选 |
| `run.started` / `run.checkpoint` / `run.finished` | 必填 | 禁止 | 禁止 | 必填 |
| `message.replaced` / `compaction.done` | 可选 | 可选 | 可选 | 可选 |
| 插件事件 | 可选 | 可选 | 可选 | 可选 |
同一 `turn_id`/`step_id`/`run_id` 在文件内只由一条 `turn.started`/`step.started`/`run.started` 引入；turn_id/step_id 在一份日志内唯一，不复用。

### 4.3 事件顺序
- `seq` 顺序即事实发生顺序；fold 只按 seq 单遍推进。
- turn/step 必须成对闭合：`turn.started` 后只允许同一 `turn_id` 的事件，直到 `turn.ended`；step 同理。
- 消息事件与对应 tool 调用必须在同一 step 内（`tool.call` 与 `tool.result` 共享 turn_id + step_id）。
- `message.replaced`/`compaction.done` 只能引用此前已 fold 进视图的消息 id；允许出现在任何位置的事件：`user.message`（queued inbox）、状态/元数据事件、插件事件、目标已存在时的 `message.replaced`。

### 4.4 未知事件策略
1. `type` 在 `EventKind` 中 → 核心事件，按 §5 校验 `data`。
2. `type` 匹配插件命名空间 → 插件事件：只校验 envelope（seq/ts/data 类型）与总行大小；`data` 内容不校验；持久化、fold 跳过、不影响派生消息。插件 id 即命名空间第二段，不再另设 `plugin` 字段（与报告 §3.4 草案措辞的差异见 §12）。
3. 其他未知 `type` → `UnknownEventError`，读取失败。
插件命名空间格式非法（段数不足、含大写、以数字开头等）→ `PluginNamespaceError`。插件缺失时其事件仍作为未知插件事件跳过（核心不需要该插件即可读取会话）。

### 4.5 为什么 `run_id` 在事件上
Run 是执行归属，不是会话身份（§3.3）。事件级 `run_id` 让“哪次执行产生了这条事实”可查询、可回放，并支持 resume/fork 产生的多 run 共存。ADR §7-3 已锁定该决定。

## 5. EventKind 目录
### 5.1 命名规则
- `<domain>.<object>` 或 `<domain>.<verb_past>`，全小写、点分隔（ADR §7-1）；禁止 `/`、禁止 camelCase。
- 名词性事实用名词短语（`tool.call`、`tool.result`、`user.message`）；状态转移用过去分词（`turn.started`、`goal.changed`）。
- Python 侧 `class EventKind(StrEnum)`，成员名为值大写替换（`TOOL_CALL = "tool.call"`）。EventKind 只包含真正实现的类型（M1 24 个；M2 增加 `run.started`/`run.checkpoint`/`run.finished` 后 27 个；task 接线再增加 `task.state`/`task.handoff`/`task.handoff.result`，共 30 个），不预留幽灵成员（报告 §3.5）。新增事件须同时改本文件、`EventKind` 与 golden fixtures。

### 5.2 核心事件（M1 24 个）
| EventKind | data 形状 | 生产者 | fold 效果 |
| --- | --- | --- | --- |
| `turn.started` | `{}` | agent turn 循环 | 打开 turn；之后事件必须属于该 turn |
| `turn.ended` | `{reason: TurnReason}` | agent turn 循环 | 关闭 turn，记录结束原因（§5.3） |
| `step.started` | `{}` | agent turn 循环 | 打开 step |
| `step.ended` | `{}` | agent turn 循环 | 关闭 step |
| `user.message` | `{message: Message}` | TUI/headless 输入、queued inbox | 追加一条 user 消息 |
| `system.message` | `{message: Message}` | prompt 层（系统提示快照） | 替换会话 system 块（last-wins，最多一条生效），不进入对话消息序列 |
| `developer.message` | `{message: Message}` | 能力/工具变更通知 | 追加一条 developer 消息 |
| `assistant.message` | `{message: Message, finish_reason: FinishReason}` | agent turn 循环 | 追加一条 assistant 消息；每个 step 至多一条 |
| `tool.call` | `{call_id, name, arguments}` | tools pipeline（审批前） | 不产生消息；与同 step assistant 消息中的 ToolCallPart 配对 |
| `tool.result` | `{call_id, message: Message, error_code?}` | tools pipeline | 追加一条 role=tool 消息（`message.tool_call_id == call_id`） |
| `approval.requested` | `{approval_id, tool_call_id, tool_name, reason?}` | approval pipeline | 记录待决审批（不产生消息） |
| `approval.decided` | `{approval_id, outcome}` | approval pipeline | 关闭该审批；outcome 见 §5.3 |
| `message.replaced` | `{message_id, message: Message}` | 编辑/脱敏/修正 | 原位替换 content/is_error（id/role/created_at 不变） |
| `compaction.done` | `{compaction_id, covers: [message_id], message: Message}` | compact 模块 | 用汇总消息替换 covers 覆盖的消息（§7.3） |
| `model.selected` | `{provider, model}` | agent/TUI | last-wins 会话状态 |
| `preset.selected` | `{preset, mode, revision?, previous?}` | agent/TUI | last-wins；mode 取 default 或 team |
| `permission.policy` | `{policy}` | approval pipeline | last-wins；policy 取 ask 或 never |
| `sandbox.mode` | `{mode}` | sandbox | last-wins；mode 取 read-only、workspace-write、danger-full-access |
| `workspace.cwd_changed` | `{cwd}` | working-directory 服务 | last-wins；resume 时覆盖 header.cwd |
| `session.title` | `{title, source}` | title 服务 | last-wins；source 取 user 或 llm |
| `goal.changed` | `{goal: Goal 或 null, rounds_started: integer}` | goal 服务 | last-wins；`null` 清除当前 goal；`rounds_started` 对应 `GoalState.rounds_started` |
| `plan.mode` | `{mode: string}` | plan 功能（M1 仅读） | last-wins；取值集合由 prompt/agent 配置 lane 定义，schema 只要求非空字符串 |
| `todo.written` | `{todos: [{content, status}]}` | `todo_write` 工具 | 整表替换；status 取 pending、in_progress、completed |
| `deliverable.presented` | `{files: [{path, description?}]}` | `present` 工具 | 追加到 deliverables 列表 |

### 5.3 data 子结构
- **TurnReason**（`turn.ended.data.reason`，按 `kind` 判别）：`completed`；`aborted`（附加 `by` 取 user、provider、disposed，可选 `message`）；`error`（`message`，可选 `code`）；`max_steps`（`limit`）；`max_tokens`（可选 `limit`）；`blocked`（`message`）。未完成任务的中断语义见 §7.4。**FinishReason**：取值为 stop、tool_calls、length、error、aborted、content_filter。
- **Goal**：字段对齐现有 `GoalSnapshot`（`src/deeppyro/goal/types.py`），仅 camelCase 转 snake_case：`{id: string, revision: integer >= 1, objective: string, phase: active 或 paused 或 blocked 或 complete, max_goal_rounds: integer, blocked_reason?: {code: string, message: string}}`。phase 使用现有 `Phase` 字面量（active/paused/blocked/complete）；`max_goal_rounds` 必填，默认 256（`DEFAULT_MAX_GOAL_ROUNDS`）；`blocked_reason` 仅 phase=blocked 时出现。**ApprovalOutcome**：取值为 allowed、rejected、cancelled、unavailable。
- **Message** 见 §6；事件中的 Message 与 §6 JSON 形状完全一致。

### 5.4 预留事件（M3/M4，尚未实现）
M2 已实现 3 个 `run.*` 事件（§5.5），它们不再属于本表。下表只保留尚未实现的 M3/M4 名称；当前 reader 把它们当未知类型拒绝。没有幽灵成员，此处只提前冻结命名：
| EventKind | 里程碑 | 何时发出 | data 计划形状 |
| --- | --- | --- | --- |
| `capability.granted` | M3 | 能力/权限授予后 | `{capability, tool_call_id?, scope?}` |
| `capability.denied` | M3 | 能力/权限拒绝时 | `{capability, tool_call_id?, reason}` |
| `schedule.changed` | M3 | 定时任务增删改 | `{schedule_id, operation, ...}` |
| `mcp.server.connected` / `mcp.server.disconnected` | M3 | MCP 连接状态变化 | `{server, transport}` |
| `subagent.started` / `subagent.finished` | M4 | 派生子代理/子代理结束 | `{child_session_id, label?}` / `{child_session_id, status}` |
| `team.member.changed` / `team.task.changed` / `team.message.queued` / `team.message.delivered` | M4 | 团队协作状态变化 | 沿用 v4 语义、改 snake_case |

### 5.5 M2 run 事件（已实现，2026-11-17）
M2 把 §5.4 预留的 3 个 `run.*` 名称加入 `EventKind`（27 个成员）；随后 Task 接线再加入 3 个 `task.*`（共 30 个）。`run.*` 是执行轨迹（run 生命周期、断点、累计预算），不是对话事实：`fold()` 跳过它们，不产生消息、不改 `SessionState`。写入者必须是 `NativeSession`，禁止双写 v4。

关联字段（§4.2）：`run.*` 必须同时带 `run_id` 与 `trace_id`，禁止 `turn_id`/`step_id`（checkpoint 内的当前 turn/step 放在 data 中）。`_OrderTracker` 不约束 run 事件，因此它们可以写在 turn 之外、turn 之间或 step 内。

| EventKind | data 形状（闭合） | 写入时机 |
| --- | --- | --- |
| `run.started` | `{}` | 每个 run 的第一条事件，早于该 run 的第一个 `turn.started` |
| `run.checkpoint` | 见下 | step 开始后、派发有副作用工具前、每批工具结果写回后、run 终止前 |
| `run.finished` | `{status, reason?}` | 该 run 的最后一条 session 事件 |

**run.finished**：`status` 必填，取 completed、failed、cancelled；`reason` 可选，为 §5.3 的 TurnReason，且与该 run 的 `turn.ended.reason` 语义一致（例如 `max_steps` 预算超限、`error`+code）。

**run.checkpoint**（闭合字段集）：
- 必填：`last_seq`（integer >= 0，checkpoint 事件自身的 seq）、`status`（running、completed、failed、cancelled）、`budgets`、`tool_calls`、`approvals`。
- 可选：`last_message_id`（Message id）、`turn_id`、`step_id`（checkpoint 时刻打开的 turn/step）。
- `budgets` 闭合对象，5 个字段全部必填：`input_tokens`、`output_tokens`、`cost_usd`、`tool_calls`、`wall_ms`；前四项 integer >= 0，`cost_usd` 为有限非负数（接受 int 或 float）。
- `tool_calls`：已派发工具数组，每项闭合 `{call_id, name, state, idempotency_key?}`；`state` 取 pending、running、completed、failed；`idempotency_key` 形如 `^idem:[0-9a-f]{64}$`（§10.3）。
- `approvals`：审批数组，每项闭合 `{approval_id, tool_call_id, state}`，`state` 为非空字符串。
- 这是唯一允许出现在 session log 的预算数字（§8.5 D-13）：仍是累计值，不含单次调用 token/latency、prompt 正文或消息副本。

M2 loop 的典型 run 事件序：`run.started` → `turn.started` → `user.message` →（每个 step）`step.started` → `run.checkpoint` → `assistant.message` →（有工具时）`tool.call`×N → `run.checkpoint`（pending）→ 执行 → `tool.result`×N → `run.checkpoint`（completed/failed）→ `step.ended` → … → `turn.ended` → `run.checkpoint`（终态）→ `run.finished`。kill/崩溃允许留下未闭合 step/turn，resume 按 §10.2 修复（`aborted`/`by=disposed`）。

断点 sidecar：M2 在 `DEEPPYRO_CHECKPOINTS_DIR` 或 `~/.dpro/checkpoints/<session_id>.json` 额外写原子 JSON 快照（临时文件 + `os.replace`），内容为 `{version, session_id, run_id, trace_id, written_at, state, checkpoint}`；`state` 对齐 checkpoint data（run_id/trace_id/status/last_seq/usage/budget/turn_id/step_id/last_message_id）。sidecar 只是加速恢复的派生缓存，session log 中的 `run.checkpoint` 事件才是权威；两者不一致时以 session log 为准。resume 只使用与当前 `run_id` 匹配的最后一条 checkpoint（§10.2）。

幂等键（§10.3）：`"idem:" + sha256(f"{session_id}\n{call_id}\n{args_hash}")` 的小写 hex；同一调用重试复用同一 key，不产生新 `tool.call` 事件。

## 6. Native Message model
### 6.1 模型定义
消息模型位于 `src/deeppyro/core/messages.py`，是冻结 slots dataclass；热路径不做 pydantic 校验，pydantic 只在边界（配置、工具参数、会话 JSON 解析）。（报告 §3.3）
```python
PartType = Literal["text", "reasoning", "tool_call", "image"]
Role = Literal["system", "user", "assistant", "tool", "developer"]
@dataclass(frozen=True, slots=True)
class TextPart:
    type: Literal["text"] = "text"
    text: str
@dataclass(frozen=True, slots=True)
class ReasoningPart:
    type: Literal["reasoning"] = "reasoning"
    text: str
@dataclass(frozen=True, slots=True)
class ToolCallPart:
    type: Literal["tool_call"] = "tool_call"
    id: str            # call_<16hex>
    name: str
    arguments: str     # 原始 JSON 字符串，不解析
@dataclass(frozen=True, slots=True)
class ImagePart:
    type: Literal["image"] = "image"
    attachment: str    # 绝对路径或 sha256:<64hex>
    media_type: str    # image/png, image/jpeg, image/webp, image/gif
ContentPart = TextPart | ReasoningPart | ToolCallPart | ImagePart
@dataclass(frozen=True, slots=True)
class Message:
    id: str
    role: Role
    content: tuple[ContentPart, ...]
    created_at: int
    source: MessageSource
    tool_call_id: str | None = None
    is_error: bool = False
```

### 6.2 JSON 形状
字段与 dataclass 一一对应、snake_case；可空字段省略（`is_error=false` 省略）：
| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `id` | string | 是 | `^msg_[0-9a-f]{16}$`；派生修复消息 `^msg_missing_[0-9a-f]{16}$`；会话内唯一 |
| `role` | string | 是 | system、user、assistant、tool、developer |
| `content` | array | 是 | 至少 1 个 part；按 role 限制（§6.3） |
| `created_at` | integer | 是 | 消息创建毫秒；消息事件中必须等于 envelope `ts`（fork 复制事件保留原 ts，等式继续成立）；`message.replaced` 必须保留原值 |
| `tool_call_id` | string | 条件 | role=tool 必填且等于对应 `call_id`；其他 role 禁止 |
| `is_error` | boolean | 否 | 默认 false；只对 role=tool 有意义 |
| `source` | object | 是 | `{kind: SourceKind, ...}`，见 §6.4 |

### 6.3 Part 与 role 约束
| part type | 字段 | 允许的 role |
| --- | --- | --- |
| `text` | `text: string` | 全部 |
| `reasoning` | `text: string` | assistant |
| `tool_call` | `id, name, arguments` | assistant |
| `image` | `attachment, media_type` | user、tool |
system/developer 只允许 text；tool 只允许 text/image（tool 结果图片直接内嵌，不再另写 user 消息）；assistant 的 `tool_call` part 与同 step 的 `tool.call` 事件按 `id` 一一对应。

### 6.4 MessageSource
`source.kind` 判别，未知 kind 拒绝：
| kind | 必填附加字段 | 语义 |
| --- | --- | --- |
| `user` | — | 人类输入 |
| `model` | `provider: string, model: string` | 模型生成 |
| `tool` | `call_id: string` | 工具结果 |
| `system` | — | 系统提示快照 |
| `goal` | `goal_id: string, revision: integer, round: integer` | goal 驱动的用户输入；引用 `GoalSnapshot.id`（§5.3） |
| `compaction` | `compaction_id: string` | 压缩汇总消息 |
| `repair` | `reason: string` | 派生修复消息（§7.4） |
| `plugin` | `plugin: string` | 插件插入的消息 |
不允许任意 `metadata` 字典；新来源必须新增 kind 并写入本表。

### 6.5 Provider 边界
- `llm/` 层在调用前后做形状转换：native Message/Part 与 OpenAI/Anthropic 请求和响应互转；`tool_call.arguments` 保持原始 JSON 字符串，provider adapter 负责流式增量拼接，组装完成后一次性写入 `assistant.message`。
- LLM 层内部流事件（`TextDelta`/`ToolCallArgsDelta`/`Usage`/`Finish`）只用于内存状态与 trace，不写 session log（§8）；session 层不 import provider 类型，provider 层不 import session 存储类型。
- M2 实现：`src/deeppyro/agent/native_provider.py` 是当前唯一新增的 `core.wire` importer（ADR §4 规则 1 的已记录 seam），只做 native↔wire 形状转换；M3 provider 移植后删除该 seam。

## 7. 折叠与派生规则
### 7.1 输入输出
`fold(header, events) -> SessionView`：纯函数、确定性、不读时钟、不做 I/O。`SessionView` 至少包含：`messages: tuple[Message, ...]`（按创建顺序）；`system: Message 或 None`（当前生效系统块）；`state`（model、preset、permission_policy、sandbox_mode、cwd、title、goal、plan_mode、todos、deliverables）；`pending_approvals`、`last_seq`、`last_message_id`；`plugin_events`（原样保留供 UI/插件使用，不参与派生）；`issues`（严格模式下为空，只有 §7.4 的悬空 tool call 修复会记录 issue）。

### 7.2 单遍推进
1. 校验 header 与 envelope（§9 失败即抛）。
2. 按 `seq` 顺序应用事件：消息事件追加；`message.replaced` 原位替换；`compaction.done` 覆盖；`system.message` 替换系统块；`tool.call` 只做配对登记；状态事件 last-wins；插件事件跳过。
3. 折叠结束后做配对检查（§7.4），再执行悬空修复。
4. 派生 provider 请求：system 块（若有）→ messages 序列；tool call/result 必须成对，否则请求不合法。

### 7.3 `message.replaced` 与 `compaction.done`
- `message.replaced`：`message.id` 必须等于 `message_id`；只能改 `content` 与 `is_error`；role/created_at/tool_call_id/source 不变；位置不变；目标消息必须已在视图中。用于编辑、脱敏、工具结果修正。
- `compaction.done`：`covers` 为非空消息 id 数组，按折叠顺序排列、在视图中连续（相邻之间没有被未覆盖消息隔开）、每个 id 都存在且未被本事件覆盖；与 `message.id` 互异。折叠时删除 covers 中全部消息，并在第一条被覆盖消息的位置插入 `message`（role=user、source.kind=compaction）。之后消息位置不变。covers 不含 seq；seq 由 writer 分配，不参与派生（ADR §7-4）。
- 同一消息只能被覆盖一次；被覆盖后被 `message.replaced` 或第二个 `compaction.done` 引用 → `MessageRefError`，避免“复活已删消息”的歧义。

### 7.4 Tool call/result 配对
- 每个 assistant 消息中的 `ToolCallPart` 发出后必须有一条 `tool.call` 事件，`call_id` 相同且同一 step；`tool.call.arguments` 是该 part 的原始 JSON 字符串（不再解析、不再改序）。
- 每个 `tool.call` 必须恰有一条 `tool.result`；`tool.result.message.role == "tool"` 且 `tool_call_id == call_id`；`call_id` 全会话唯一。
- 正常结束：`turn.ended.reason.kind == "completed"` 时不允许未配对的 `tool.call`（`ToolPairingError`）。
- 中断/崩溃：允许 `aborted`/`error` 的 turn 内存在未配对 call。fold 为每个未配对 call 派生修复消息：role=tool、`tool_call_id=call_id`、`is_error=true`、`id="msg_missing_<call_id 的 16hex>"`、`source={kind:"repair", reason:"missing-tool-result"}`，text 为 `tool result was never recorded: the turn stopped before the tool finished`。修复消息不写入日志，只存在于派生视图，保证下一次请求合法。
- 工具重试不产生新的 `tool.call` 事件：重试属一次 call 的内部行为，重试次数与结果大小进 trace（§8）。

### 7.5 assistant 流式消息
- 会话日志不保存 token 增量流；一个 step 只在结束时写一条 `assistant.message`，携带最终 content 与 `finish_reason`。
- 被中断时：已产出的内容写一条 `assistant.message`，`finish_reason="aborted"`；若中断发生在任何内容产生之前则不写该事件。
- 参数未拼完的 tool call 不产生 `tool.call` 事件；已完整拼出的 call 才登记；中断 turn 的未完成调用按 §7.4 修复。

### 7.6 未知/插件事件、fork 与无 interval surface
- 插件事件只参与“存在性”记录；fold 不解释 data，也不因插件缺失而失败。
- 只有 append 与按 id 的原位替换；不存在重放区间、影子节点、surface 节点概念，派生位置永远等于消息首次出现的位置。
- fork 复制固定事件子集（§10）：只重编号 seq，ts/created_at 原样保留；子会话 fold 结果与源会话在被复制点上逐条一致（消息 id、created_at、content 相同，仅 seq 不同）。

### 7.7 派生与确定性
- 请求消息 = 系统块（若有）+ 视图消息（按位置）；developer 消息按位置参与对话。
- 相同 header + events 必须产生逐字节相同的 SessionView 与 provider 请求消息（消息 id、created_at 都来自日志）。禁止在 fold 中读 `now()`、随机数、环境变量。
- 损坏或非法顺序不产生“尽力而为”的部分视图，直接失败。

## 8. Trace/run 关联
### 8.1 存储位置与格式
- trace store 默认 `~/.dpro/traces/<run_id>.jsonl`（每个 Run 一个文件；`DEEPPYRO_TRACES_DIR` 可覆盖），compact UTF-8 JSONL、LF，fsync 策略由实现决定（可批量）。
- trace 记录允许显式 null（便于 SQL/Parquet 导出的列对齐），session log 不写 null；trace 不存 prompt 正文、消息正文、工具输出正文，只存 hash、大小、id、计数与耗时。

### 8.2 M1 记录类型
| type | 必填字段 | 说明 |
| --- | --- | --- |
| `run` | `run_id, trace_id, session_id, started_at, provider, model` | 文件首行，记录 Run 身份 |
| `model_call` | `trace_id, span_id, run_id, turn_id, step_id, provider, model, prompt_hash, input_tokens, output_tokens, latency_ms, finish_reason, retry_count, ts` | 每次 provider 调用一条；可选 `cached_input_tokens`、`reasoning_tokens`、`cost_usd`、`context_message_ids`、`error_code` |
| `tool_call` | `trace_id, span_id, run_id, turn_id, step_id, call_id, tool, args_hash, result_size, is_error, retry_count, latency_ms, ts` | 每次工具执行一条；可选 `capability`、`risk`、`error_code`、`idempotency_key` |
- `span_id`：`^span_[0-9a-f]{16}$`；`args_hash`/`prompt_hash`：`sha256:<64hex>`（对规范化后的输入做 SHA-256，规范化规则由 `observability/trace.py` 固定并写测试）。
- `result_size`：整数字节数（工具输出 UTF-8 字节长度）。
- M2 增加 `span` 记录（turn/step 级 parent span）；M1 不需要。

### 8.3 示例
```json
{"type":"run","run_id":"run_0123456789abcdef","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","session_id":"session-7f3a9c1e-2b4d-4e6f-8a90-1c2d3e4f5a6b","started_at":1763390001000,"provider":"deepseek","model":"deepseek-chat"}
{"type":"model_call","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"span_0123456789abcdef","run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","step_id":"step_0123456789abcdef","provider":"deepseek","model":"deepseek-chat","prompt_hash":"sha256:1111111111111111111111111111111111111111111111111111111111111111","input_tokens":1024,"output_tokens":64,"latency_ms":1830,"finish_reason":"tool_calls","retry_count":0,"ts":1763390001500}
{"type":"tool_call","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"span_ffeeddccbbaa9988","run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","step_id":"step_0123456789abcdef","call_id":"call_0123456789abcdef","tool":"read","args_hash":"sha256:2222222222222222222222222222222222222222222222222222222222222222","result_size":42,"is_error":false,"retry_count":0,"latency_ms":7,"ts":1763390002100}
```

### 8.4 关联规则
- session 事件上的 `trace_id` 与 trace 文件中的 `trace_id` 相同；`run_id` 与文件名/`run_id` 字段相同。两者在 envelope 上冗余，避免回放时先加载 trace 才能关联。
- 一次 `assistant.message` 若由多次 provider 尝试产生，trace 有对应多条 `model_call`；session 只有一条最终消息。
- 工具重试：session 有一次 `tool.call` 与一条 `tool.result`；trace 有多条 `tool_call`（`retry_count` 递增）。

### 8.5 session log 禁止项
以下内容不得出现在 session 事件的 data 中（review 直接拒绝）：prompt 全文（消息本身除外）、provider 原始响应、原始流；单次调用的 `input_tokens`/`output_tokens`/`cached_tokens`/`cost`/`latency`；工具输出的完整大对象（完整内容进工具自己的结果消息，trace 只存 `result_size`）；堆栈、调试日志、随机 id（§6 定义的消息/call id 除外）。
唯一例外：M2 的 `run.checkpoint` 可携带累计预算数字（`budgets`），因为断点续跑必须知道剩余预算；该记录仍不含 prompt 正文与单次调用明细（§12 D-13）。

### 8.6 OpenTelemetry 对齐
导出 OTLP 时映射到 GenAI semantic conventions（报告 §9.4.1）：`provider` → `gen_ai.provider.name`，`model` → `gen_ai.request.model` / `gen_ai.response.model`，`input_tokens` → `gen_ai.usage.input_tokens`，`output_tokens` → `gen_ai.usage.output_tokens`，`finish_reason` → `gen_ai.response.finish_reasons`。不要自创语义字段名；映射表由 `observability/trace.py` 维护。

### 8.7 采样与保留
session log 全量、长期保留（可回放/可 fork）；trace store 高写入、可采样、默认短期（报告 §9.3）。采样只影响 trace 完整性，不影响 session 事实与 fold。

## 9. 验证与拒绝规则
以下错误类为提议（当前代码只确立了 `deeppyro.core.errors.SessionError` 基类与英文消息风格）。M1 在 `src/deeppyro/session/errors.py` 定义，全部继承 `SessionError`：
| 情况 | 错误类 | 错误消息模板 |
| --- | --- | --- |
| 行不是合法 JSON | `CorruptLineError` | `session line {line} is not valid JSON: {detail}` |
| 空行 | `CorruptLineError` | `session line {line} is blank` |
| v4 header | `SessionV4Error` | `session format v4 is not supported; export it with the standalone migration script and start a schema 1 session: {path}` |
| `schema` 非 1 | `UnsupportedSchemaError` | `unsupported session schema {schema!r}: this build reads schema 1 only` |
| header 缺字段 | `HeaderError` | `session header lacks required field {name!r}` |
| header 字段类型/值非法 | `HeaderError` | `session header field {name!r} has invalid value {value!r}` |
| header 未知字段 | `HeaderError` | `session header has unknown field {name!r}` |
| 第二个 header 或 header 不在首行 | `HeaderError` | `session has more than one header line` / `session does not start with a header line` |
| header.id 与文件名不一致 | `HeaderError` | `session id {id!r} does not match file name {name!r}` |
| seq 重复/跳号/回退 | `SequenceError` | `event seq {seq} is out of order: expected {expected}` |
| ts 回退或早于 header | `TimestampError` | `event seq {seq} has ts {ts} before the previous ts {previous}` |
| 未知核心事件类型 | `UnknownEventError` | `unknown event type {type!r} at seq {seq}: plugin events must use the plugin.<id>.<event> namespace` |
| 插件命名空间非法 | `PluginNamespaceError` | `invalid plugin event type {type!r}: expected plugin.<id>.<event>` |
| data 不是 object | `EventFieldError` | `event {type!r} at seq {seq} has data of type {kind}, expected object` |
| envelope 未知/缺失关联字段 | `EventFieldError` | `event {type!r} at seq {seq} has unknown field {name!r}` / `event {type!r} at seq {seq} requires field {name!r}` |
| 消息结构非法（role/part/tool_call_id） | `MessageError` | `event {type!r} at seq {seq} message is invalid: {detail}` |
| replaced/compaction 引用未知或已覆盖消息 | `MessageRefError` | `event {type!r} at seq {seq} references unknown message id {message_id!r}` |
| `compaction.done.covers` 不连续/含自身 | `MessageRefError` | `compaction at seq {seq} covers a non-contiguous range: {detail}` |
| tool call/result 未配对 | `ToolPairingError` | `tool.call {call_id!r} at seq {seq} has no matching tool.result` |
| tool.result 无对应 call / call_id 重复 | `ToolPairingError` | `tool.result at seq {seq} has no matching tool.call {call_id!r}` / `call id {call_id!r} is used more than once` |
| turn/step 层级或顺序错误 | `OrderingError` | `event {type!r} at seq {seq} is not allowed {where}: {detail}` |
| 行超过 32 MiB | `LineTooLargeError` | `session line {line} is larger than {limit} bytes` |
| 会话已被写入进程锁定 | `SessionLockedError` | `session {id!r} is locked by pid {pid}` |
严格性：读取阶段（`NativeCodec.decode`）与写入阶段（`NativeCodec.append` 前置断言）共用同一校验器；任何失败都不产生部分视图。

## 10. Fork 与 resume
### 10.1 Fork
- 前置：源会话不在 open turn/step 中；源文件完整可读。
- 新会话 id 必须新生成；header：`parent_id = 源 id`、`origin = "fork"`、`delegation_depth = 源 delegation_depth`、`created_at = 源 header.created_at`（时间线连续，保证复制事件 ts >= header.created_at；fork 执行时刻由复制后新事件 ts 表示）、cwd/preset 继承源 header。
- Fork 在写入前整段复制事实（ADR §7-7：copy，不做 lazy 引用）：
  - 保留：消息事件、`message.replaced`、`compaction.done`、状态事件、`session.title`、`goal.changed`、`todo.written`、`deliverable.presented`、plugin.* 事件、已闭合 turn/step 的事件、已闭合配对（requested+decided）的 approval。
  - 丢弃：`run.*`（M2）、未闭合 approval、当前 open turn/step（存在则报错）。
  - 重写：seq 从 1 重新连续分配；**ts 与 message.created_at 原样保留**（日志记录事实发生时间，fork 不伪造历史；§6.2 等式在复制事件上继续成立，复制事件早于 fork 执行时刻是预期语义）；清除复制事件的 `run_id`/`trace_id`；保留 `turn_id`/`step_id`/`call_id`/`message.id`（消息身份与派生结果可比较）。新事件 ts = `max(now_ms(), 上一条 ts)`（§4.1），确保仍非递减。Fork 不是文件系统 copy：子会话不引用源文件，源会话之后的变化不影响子会话。

### 10.2 Resume
1. 加写锁（§2.4）。
2. 读取并校验 header + 全部事件，fold 得 `SessionView`（最后一条是未完成 turn 时按 §7.4 修复派生视图）。
3. 确定新 `run_id`、新 `trace_id`；写 `run.started`（M2）与 `turn.started`，继续追加事件。
4. 若存在 `run.checkpoint`（M2）：以最后一条与当前 `run_id` 匹配的 checkpoint 恢复 run 状态，再重放其后的 session 事件；不允许用旧 run 的 checkpoint 恢复新 run。run 事件与断点 sidecar 的细节见 §5.5；sidecar 只是加速缓存，与 session log 冲突时以 session log 为准。
5. Resume 不修改历史事件、不重写 header、不重置 seq。

### 10.3 有副作用工具的幂等键（M2 契约，M1 预留）
- 每个 `tool.call` 派发前计算：`idempotency_key = "idem:" + sha256(f"{session_id}\n{call_id}\n{args_hash}")`（hex 小写），记录在 trace `tool_call.idempotency_key` 与 checkpoint。
- 有副作用的工具必须接受该键并在外部系统去重；重试复用同一键，不产生新的 `tool.call` 事件。
- 幂等键算法变更需改本文件并写 golden fixture（跨 resume 必须稳定）。

### 10.4 Checkpoint 内容（M2，reserved）
见 §5.4 `run.checkpoint`：必须包含 `last_seq`、最后消息 id、当前 turn/step、RunStatus、累计预算、每个已派发工具的状态与幂等键、待决 approval。不得包含 prompt 正文、单次调用 token/latency 明细、消息内容副本。

## 11. 最小完整 JSONL 示例
header + 一轮带工具调用的 turn + 最终 assistant + `message.replaced` + `compaction.done`（每行都是合法 JSON）：
```jsonl
{"type":"session","schema":1,"id":"session-7f3a9c1e-2b4d-4e6f-8a90-1c2d3e4f5a6b","created_at":1763390000000,"cwd":"/home/dev/project","preset":"standard","parent_id":null,"origin":"user","delegation_depth":0}
{"type":"turn.started","seq":1,"ts":1763390001000,"run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","data":{}}
{"type":"step.started","seq":2,"ts":1763390001000,"run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","step_id":"step_0123456789abcdef","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","data":{}}
{"type":"user.message","seq":3,"ts":1763390001000,"run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","data":{"message":{"id":"msg_0000000000000011","role":"user","content":[{"type":"text","text":"Read a.py and summarize it."}],"created_at":1763390001000,"source":{"kind":"user"}}}}
{"type":"assistant.message","seq":4,"ts":1763390002000,"run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","step_id":"step_0123456789abcdef","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","data":{"message":{"id":"msg_0000000000000012","role":"assistant","content":[{"type":"text","text":"I will read the file."},{"type":"tool_call","id":"call_0123456789abcdef","name":"read","arguments":"{\"path\":\"a.py\"}"}],"created_at":1763390002000,"source":{"kind":"model","provider":"deepseek","model":"deepseek-chat"}},"finish_reason":"tool_calls"}}
{"type":"tool.call","seq":5,"ts":1763390002100,"run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","step_id":"step_0123456789abcdef","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","data":{"call_id":"call_0123456789abcdef","name":"read","arguments":"{\"path\":\"a.py\"}"}}
{"type":"tool.result","seq":6,"ts":1763390002200,"run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","step_id":"step_0123456789abcdef","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","data":{"call_id":"call_0123456789abcdef","message":{"id":"msg_0000000000000013","role":"tool","content":[{"type":"text","text":"print('hi')"}],"created_at":1763390002200,"tool_call_id":"call_0123456789abcdef","source":{"kind":"tool","call_id":"call_0123456789abcdef"}}}}
{"type":"assistant.message","seq":7,"ts":1763390003000,"run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","step_id":"step_0123456789abcdef","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","data":{"message":{"id":"msg_0000000000000014","role":"assistant","content":[{"type":"text","text":"a.py prints hi."}],"created_at":1763390003000,"source":{"kind":"model","provider":"deepseek","model":"deepseek-chat"}},"finish_reason":"stop"}}
{"type":"step.ended","seq":8,"ts":1763390003000,"run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","step_id":"step_0123456789abcdef","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","data":{}}
{"type":"turn.ended","seq":9,"ts":1763390003100,"run_id":"run_0123456789abcdef","turn_id":"turn_0123456789abcdea","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","data":{"reason":{"kind":"completed"}}}
{"type":"message.replaced","seq":10,"ts":1763390004000,"data":{"message_id":"msg_0000000000000014","message":{"id":"msg_0000000000000014","role":"assistant","content":[{"type":"text","text":"a.py prints a greeting."}],"created_at":1763390003000,"source":{"kind":"model","provider":"deepseek","model":"deepseek-chat"}}}}
{"type":"compaction.done","seq":11,"ts":1763390005000,"data":{"compaction_id":"cmp_0123456789abcdef","covers":["msg_0000000000000011","msg_0000000000000012","msg_0000000000000013","msg_0000000000000014"],"message":{"id":"msg_0000000000000015","role":"user","content":[{"type":"text","text":"Earlier: the user asked to read a.py; the agent read it and summarized."}],"created_at":1763390005000,"source":{"kind":"compaction","compaction_id":"cmp_0123456789abcdef"}}}}
```

## 12. M1 实现细节（已锁定默认值）
ADR §7 锁定的 8 项按推荐项执行，不再讨论：`ts` 毫秒（`time` 留给时长）、dotted lowercase 事件名、header 无 `run_id`（run_id 在事件）、`compaction.done.covers` 只含 message id、schema 1 不启用 zstd、插件事件 `plugin.<id>.<event>` 且无 `ignorable`、fork 为 copy + 新 seq + `parent_id`、API v3 不属于 session schema。以下 D-1..D-15 是 M1 实现默认值；变更需在 M1 PR 记录并同步本文件：
| # | 议题 | 建议 | 状态/理由 |
| --- | --- | --- | --- |
| D-1 | turn_id/step_id 位置 | 放 envelope，不重复放 data | 与报告 §3.4 示意示例（放 data）不同；按单一事实来源选 envelope |
| D-2 | 工具结果图片 | 直接进 role=tool 消息的 ImagePart | 删除 v4 的独立 user/message 绕行（§1.3） |
| D-3 | assistant 流/usage | 不入 session log，只进 trace | 报告 §9.3；M1 起 assistant.message.data 无 stream/usage |
| D-4 | trace 文件粒度 | 每个 Run 一个 `<run_id>.jsonl` | 便于按 run 采样/清理；SQLite/OTLP 可作为 TraceSink 可替换实现 |
| D-5 | checkpoint 预算数字 | 允许 run.checkpoint.budgets 累计值；禁止单次调用 token/latency | 断点续跑需要；属 §8.5 唯一例外 |
| D-6 | ImagePart.media_type | 新增字段（当前 wire.ImagePart 只有 attachment） | provider adapter 需要媒体类型；有意断裂（§1.4） |
| D-7 | 错误类与模块 | session/errors.py 中提议的类，继承现有 SessionError | 当前只确立基类与英文消息风格（§9） |
| D-8 | message id 生成器 | M1 沿用 `msg_<16hex>`；schema 只约束格式 | 更换 ULID 需单独决策，不影响本 schema |
| D-9 | 单写者锁 | `<id>.jsonl.lock` + `O_EXCL` | 不依赖单进程假设；崩溃残留锁由 M2 doctor 处理 |
| D-10 | message.replaced 是否可改 is_error | 允许（只对 tool 消息有意义） | 工具结果可被修正；role/id/created_at 仍不可变 |
| D-11 | 插件事件命名空间即插件 id | 采纳；不再另设 `plugin` 字段 | 与报告 §3.4 草案措辞不同，按 ADR §7-6 的具体化记录 |
| D-12 | `system.message` 是否为消息事件 | 不进对话消息序列，仅替换系统块 | 当前 v4 也把 system 视为 surface 节点；native 避免重复系统块 |
| D-13 | Goal 对象字段名 | 对齐现有 `GoalSnapshot`：`id`、`max_goal_rounds`、phase 取 `complete`（snake_case 序列化）；事件与消息 source 引用仍用 `goal_id` | 避免运行时适配层；完整取值见 §5.3 |
| D-14 | origin/delegation_depth 必填与扩展 | 保留并必填；origin 在 v4 仅 `subagent` 基础上扩为 user/headless/subagent/fork；删除 `is_seeded`（用 origin=subagent 且 delegation_depth>=1 表达） | 运行时 header 已有两者且 subagent manager 写 origin=subagent；user/headless 入口需显式赋值，属有意增强 |
| D-15 | seq 基址与跳号 | 从 1 开始、严格连续；删除 v4 trimmed 容忍 | 现有 v4 writer 从 0 开始（session.py last_seq=-1 -> 0），reader 允许首 seq 跳号并标 trimmed；native 有意断裂，读取缺行即报错 |

## 13. 引用与 M1 验收
- ADR：`docs/adr/0001-three-track-native-agent.md`（D2 零兼容、D3 保留/删除清单、D4 所有权、D5 里程碑、§7 已锁定决策、§4 合规规则）；M0 交付物：本文件 + ADR + `fixtures/native/` 的 10 个录制用例（lead 汇总，M0 不写代码）。
- 研究报告：`deeppyro-native-python-agent-research.md` §3.3（Message dataclass）、§3.4（schema 1 草案）、§3.5（EventKind）、§5.3（M1 范围与门禁）、§9.3–9.5（存储分层、trace/replay、持久化 Run）；被取代：`docs/spec/session.md`（session.v4 的存储/派生/fork 旧契约）、`docs/spec/INTERFACES.md` §0 旧不变量（只用于理解迁移前语义，不再是实现依据）。
- M1 验收（对应 ADR D5）：(1) native 会话往返：编码→解码→编码逐字节稳定，fold 结果与 golden fixture 一致；(2) v4 被显式拒绝（`SessionV4Error`），产品不枚举/不接受旧路径；(3) 工具 schema 字节级不变（本 schema 不触碰 `ToolSchema.parameters`）；(4) trace 冒烟：一次会话至少产生一条 model_call 与一条 tool_call，可按 run_id 与 session 事件 join。
