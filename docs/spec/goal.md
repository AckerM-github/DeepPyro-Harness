# goal 契约（DSH 对齐）

> **状态**：现行契约（goal 域冻结） · **层级**：goal 域契约 · **最后核对**：2026-11-18
>
> **FROZEN 依据**：M0 取证完成（2026-11-16）。本文件是 goal 域的唯一契约权威：后续实现只能按这里的字段、状态机、事件与不变量；无证据条目一律去 §9。
> 基线：deeppyro `main` @ `0cd53af`（工作树 clean）。

## 0. 取证来源与工具

DSH 安装：`D:/deepseekharness/resources/app.asar`（Electron asar，直接 fs 读会 ENOENT）。

已解包（只读，勿改）：

| 文件 | 行数/字节 | 内容 |
|---|---|---|
| `D:/dshworkspace/_dsh_src/dsh-goal/lib/index.js` | 909 行 / 36,668 B | dsh-goal 服务实现（fold + schema + GoalService） |
| `D:/dshworkspace/_dsh_src/dsh-goal/README.zh.md` | 10,891 B | dsh-goal README |
| `D:/dshworkspace/_rd.js` | 382 行 / 29,676 B | dsh-goal-round-driver `lib/index.js`（续行驱动器） |
| `D:/dshworkspace/_rd.md` | 9,204 B | round driver README |
| `D:/dshworkspace/_toolgo.md` | 12,642 B | dsh-tool-goal README |
| `D:/dshworkspace/_toolgo_lib.js` | 375 行 / 15,904 B | dsh-tool-goal `lib/index.js`（工具 + 权限 + wrapup） |
| `D:/dshworkspace/_cmdgo.md` | 8,806 B | dsh-command-goal README |
| `D:/dshworkspace/_cmdgo_lib.js` | 188 行 / 7,225 B | dsh-command-goal `lib/index.js`（`/goal` 命令） |
| `D:/dshworkspace/_agent_loop_lib.js` | 68,412 B | dsh-agent-loop `lib/index.js`（inbox 事件 payload 在 L106-110、L199-200） |

解包器：`D:/dshworkspace/.dsh-tmp/asar_cat.mjs`（读 16 字节头 + `readUInt32LE(12)` headerSize + JSON 头 + `16+headerSize` 数据段，输出 UTF-8）。
**坑**：PowerShell 的 `>` 会把输出写成 UTF-16，必须走 cmd 才拿到 UTF-8：

```powershell
cmd /c "node D:\dshworkspace\.dsh-tmp\asar_cat.mjs dsh/node_modules/@deepseek-ai/dsh-tool-goal/lib/index.js > D:\dshworkspace\_toolgo_lib.js"
```

本文件所有 `file:line` 的行号均指上表的解包文件，不是 asar 内路径。

## 1. 已核实基线（M0 用源码复核后的修正）

| 事实 | 证据 | 结论 |
|---|---|---|
| 记录名 `goal/change`，每会话至多一个当前 goal | dsh-goal/lib/index.js:867 | 保留 |
| phase = `active`/`paused`/`blocked`/`complete` | dsh-goal/lib/index.js:42-47 | 保留 |
| 动词 `create`/`edit`/`pause`/`resume`/`complete`/`block`，另有 `clear` 墓碑 | dsh-goal/lib/index.js:34-41、:122-137 | 保留（`clear` 不是 snapshot 动词，是 tombstone） |
| 每次接受变更追加**完整快照**（version 1），日志是唯一持久权威 | dsh-goal/lib/index.js:140-161、:860-872 | 保留 |
| revision CAS `{id, revision}`，过期引用报错 | dsh-goal/lib/index.js:768 | 保留（`GOAL_STALE_REVISION`） |
| `clear` 写带 revision 的 tombstone | dsh-goal/lib/index.js:741-758 | 保留（revision = 当前 + 1） |
| activation `armed`/`disarmed` 是**进程本地**状态，不是记录字段 | dsh-goal/lib/index.js:591、:782-808；_rd.js:80-82 | 保留；`agent/created` 即 disarm（index.js:600-602） |
| 默认 `maxGoalRounds = 256`，必须正安全整数 | dsh-goal/lib/index.js:589、:595、:457 | 保留 |
| `goal/changed` 在提交后发，载荷 `{change:{operation, ref, goal?}}` | dsh-goal/lib/index.js:874-879 | 修正（模板写的是扁平 operation/ref/view） |
| `goal/activation-changed` 只在 activation 真变化时发 | dsh-goal/lib/index.js:792-807 | 保留；payload `{sessionId, goal?:{id,revision,activation}}` |
| projection 单元 `goal`、`stateVersion: 6`、需 `sessionProjections` | dsh-goal/lib/index.js:440-454、:588、:599 | 保留 |
| 工具 `get_goal`/`create_goal`/`update_goal`；action = `edit`/`pause`/`resume`/`complete`/`blocked` | _toolgo_lib.js:115-121、:263-370 | 保留（注意是 `blocked` 不是 `block`） |
| `blocked_reason` 必填非空，持久 code = `model-reported` | _toolgo_lib.js:353-358 | 保留 |
| `blockedAfterConsecutiveRounds` 默认 3，且同时是提示词数字与硬下限 | _toolgo_lib.js:114、:194-196、:354 | 保留 |
| create/edit/pause/resume 要求根 agent 当前轮存在人类直接消息 | _toolgo_lib.js:49-65、:291、:338、:346 | 保留（`source.kind === "user"`） |
| resume 拒绝持久 paused（`GOAL_TOOL_RESUME_PAUSED`） | _toolgo_lib.js:348 | 保留 |
| 自主 complete/blocked 后 `deferContext` 注入 wrapup 指令（不是硬停 turn） | _toolgo_lib.js:91-98、:359-366 | 修正（README 说「结束物理 turn」，实现是注入上下文让模型收尾） |
| driver：`drivable = active && armed`；`roundsStarted >= maxGoalRounds` 时 block `round-limit` | _rd.js:80-82、:135-141 | 保留 |
| round 消息 source = `{kind:"goal", goalId, revision, round}`，文本模板 `<goal_round>…</goal_round>` | _rd.js:11-18、:144-152 | 保留 |
| pre-step 栅栏失败：`restoreOtherClaimed`（`inbox.prepend("next-step")`）+ `{kind:"reject"}` + `requestDrive` | _rd.js:100-106、:307-316、:346-351 | 保留 |
| inbox 事件 payload：`claimed {message, turn}`；`inserted`/`discarded {message}` | _agent_loop_lib.js:106-110、:199-200 | 保留 |
| `agent/status` 在 DSH 是 `{agent, status}`，driver 只关心 `idle` | _rd.js:223-242 | 保留；deeppyro payload 是 `{"status": phase}` 且无 agent（见 §8） |
| deeppyro `user/message` 存完整 `Message`（含 `source`） | src/deeppyro/agent/session.py:1375-1379 | 保留 |
| deeppyro 每记录 append 即 flush+fsync → DSH 的 `sessions.flush` 检查点天然满足 | src/deeppyro/session/session.py:26,85；session/writer.py:108,134 | 保留（driver 的 flush 步骤可退化为 no-op） |

## 2. 记录 schema

### 2.1 `goal/change` data

两条互斥形状（`kind` 必须存在且为 `"goal/change"`，`version` 必须为 `1`；其它值 → 回放硬失败）：

**A. 快照（create / edit / pause / resume / complete / block）** —— 恰好 7 个键：`kind, version, operation, goal, roundsStarted, createdAt, updatedAt`（_dsh-goal L140-161）

```ts
{
  kind: "goal/change",
  version: 1,
  operation: "create" | "edit" | "pause" | "resume" | "complete" | "block",
  goal: {
    id: string,                 // 非空；create 时为 `goal-${randomUUID()}`（index.js:648）
    revision: number,           // 正安全整数；每次变更 +1
    objective: string,          // 非空且首尾无空白（trim 后原样存）
    phase: "active" | "paused" | "blocked" | "complete",
    maxGoalRounds: number,      // 正安全整数
    blockedReason?: { code: string, message: string }  // 仅 phase === "blocked" 时存在
  },
  roundsStarted: number,        // 非负安全整数
  createdAt: number,            // 非负安全整数（毫秒）
  updatedAt: number             // 非负安全整数且 >= createdAt
}
```

- `goal` 的键集随 phase 严格校验：`blocked` 必须含 `blockedReason`，其余三个 phase **不允许**出现该键（index.js:93-94）。
- `blockedReason` 必须恰好两个键 `code`+`message`；`code` 匹 `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`（lower-kebab）；`message` 非空且首尾无空白（index.js:78-80）。

**B. 墓碑（clear）** —— 恰好 5 个键：`kind, version, operation, cleared, clearedAt`（index.js:122-137）

```ts
{
  kind: "goal/change",
  version: 1,
  operation: "clear",
  cleared: { id: string, revision: number },   // 恰好两个键；revision 为正安全整数
  clearedAt: number                             // 非负安全整数
}
```

`cleared.revision` 必须等于被清除快照的 `revision + 1`，`clearedAt >= 该快照 updatedAt`（index.js:236-239、:741-755）。

### 2.2 记录不变式

- **revision**：非 `create` 的每次变更必须 `next.revision === current.revision + 1`（index.js:174-176）；`create` 必须 `revision === 1`（index.js:248）。
- **时间戳**：非 `create` 变更必须保留 `createdAt` 与 `roundsStarted`，且 `updatedAt` 非递减（index.js:183）；新时间戳取 `max(Date.now(), 当前 updatedAt)` 以抵抗时钟回拨（index.js:837）。
- **phase 转换表**（index.js:184-213、:640-758）：

  | 动词 | 允许的前置 phase | 结果 phase | activation 副作用 |
  |---|---|---|---|
  | `create` | 无当前 goal，或当前 `complete` | `active` | `armed` |
  | `edit` | 任意（不改 phase） | 不变 | 保持当前 activation |
  | `pause` | `active` | `paused` | `disarmed` |
  | `resume` | `active`（且 disarmed）/ `paused` / `blocked` | `active` | `armed` |
  | `complete` | `active` / `paused` / `blocked` | `complete` | `disarmed` |
  | `block` | `active` | `blocked` | `disarmed` |
  | `clear` | 任意（需当前 goal） | 无 goal | `disarmed` |

- **哪些字段由哪个动词写**：`create` 写全部字段并置 `roundsStarted=0`；`edit` 只写 `objective`/`maxGoalRounds`（禁止改 phase 与 blockedReason，index.js:186）；`pause`/`complete`/`block` 只改 phase（block 额外写 blockedReason）；`resume` 清空 blockedReason（`withPhase` 不复制，index.js:809-818）；`roundsStarted` 只由 fold 观察 goal-round 的 `user/message` 递增（index.js:274-280），不写成长更新之外的值。
- **round 准入**：带 goal source 的 `user/message` 只有在其 `round === roundsStarted + 1` 且 `round <= maxGoalRounds`、goal 处于 `active` 且 id/revision 匹配时才被接受，并令 `roundsStarted = round`（index.js:274-280）。
- **fold 的失败语义**：projection 首次遇到非法自有事件即记 `failure: "goal replay failed at session event <seq>: <msg>"` 并冻结在最后一个合法视图；宿主侧 `get` 直接抛 `Error(failure)`，而客户端视图保持旧值（index.js:423-438、:776-780）。

### 2.3 旧格式兼容（deeppyro 特有）

deeppyro v0.1 已写过的旧记录（`src/deeppyro/goal/service.py:155/167`）形状是 `{"goal": "<字符串>"}` 或 `{"goal": null}`，与 DSH 快照不兼容，且 `tests/test_goal.py` 与真实 `~/.dpro/sessions` 中存在此类历史。

- 读到 `{"goal": <string>}`：内存折叠为一个**合成的 v1 快照**——`id = "legacy"`、`revision = 1`、`objective = <string>`、`phase = "active"`、`maxGoalRounds = 默认值`、`roundsStarted = 0`，`createdAt/updatedAt` 取该记录的 `time`；`activation` 视为 `disarmed`（旧记录没有 activation 概念）。
- 读到 `{"goal": null}`：等价于无当前 goal（不合成墓碑）。
- **是否回写：否**。旧记录只读兼容，下一次任意合法变更会写全新 v1 快照（revision 从 1 起），不修改旧行。
- 测试要求：旧记录 → 折叠出期望视图；旧记录之后写新快照 → 折叠结果取新快照；`{"goal": null}` 之后 `create` 成功；损坏的旧值（如空字符串）→ 视为无 goal，不抛。

## 3. 状态机与 CAS

- **CAS**：所有变更（除 `create`/`clear` 需先读当前）都接收 `{id, revision}`；`state === null` → `GOAL_NOT_FOUND`；id/revision 不匹配 → `GOAL_STALE_REVISION`（index.js:765-770）。`clear` 内部以当前视图构造 ref，同样走 CAS 前置检查。
- **活 agent 校验**：`ctx.agents.get(agent.id) !== agent` → `GOAL_AGENT_NOT_LIVE`（index.js:772-774）。
- **动词 × phase 矩阵与错误码**（index.js:640-758、:820-830）：

| 动词 | 当前 phase | 结果 | 失败错误码 |
|---|---|---|---|
| `create` | 无 goal | active/armed, revision 1 | — |
| `create` | `complete` | active/armed, revision 1（替换旧 goal） | — |
| `create` | `active`/`paused`/`blocked` | 不变 | `GOAL_ALREADY_EXISTS`（`goal "<id>" already exists with phase "<phase>"`） |
| `edit` | 任意（ref 必须匹配） | phase 不变，revision+1 | 两个替换字段都缺失 → `GOAL_INVALID_EDIT`；`objective` 空 → `GOAL_INVALID_OBJECTIVE`；`maxGoalRounds` 非正安全整数 → `GOAL_INVALID_MAX_ROUNDS` |
| `pause` | `active` | paused/disarmed | 其它 phase → `GOAL_INVALID_TRANSITION` |
| `resume` | `paused`/`blocked` | active/armed | 其它 phase（含 `complete`）→ `GOAL_INVALID_TRANSITION` |
| `resume` | `active` 且 disarmed | active/armed | `active` 且已 armed → `GOAL_INVALID_TRANSITION`（`already active and armed`） |
| `resume` | 任意 resumable | — | `roundsStarted >= maxGoalRounds` → `GOAL_INVALID_TRANSITION`（`exhausted <n> goal rounds; increase maxGoalRounds before resuming`） |
| `complete` | `active`/`paused`/`blocked` | complete/disarmed | 已 `complete` → `GOAL_INVALID_TRANSITION` |
| `block` | `active` | blocked/disarmed + blockedReason | 其它 phase → `GOAL_INVALID_TRANSITION`；reason 非法 → `GOAL_INVALID_BLOCK_REASON` |
| `clear` | 任意（需当前 goal） | 无 goal，写墓碑，disarmed | 无当前 goal → `GOAL_NOT_FOUND` |

- 非法转换的统一文案：`cannot <operation> goal "<id>" from phase "<phase>"; expected <a> or <b>`，错误码 `GOAL_INVALID_TRANSITION`（index.js:828-830）。
- `create` 的 objective 空白 → `GOAL_INVALID_OBJECTIVE`；`maxGoalRounds` 非正安全整数 → `GOAL_INVALID_MAX_ROUNDS`（index.js:456-464）。
- **clear 语义**：写墓碑而不是删除历史；`cleared.revision = 当前 revision + 1`；`clearedAt = max(now, 当前 updatedAt)`；fold 应用后 `goal`, `roundsStarted`, `createdAt`, `updatedAt` 全部清空，但 `seenGoalIds` 保留（因此同一 id 不能复活，index.js:233-249）。

## 4. activation

- **存储位置**：服务内的进程本地 `WeakMap<session, {activation: "armed"|"disarmed", pendingActivation?: {offset, activation}}>`，**不是**记录字段（index.js:591、:782-791）。
- **提交握手**：`commit()` 先记 `pendingActivation = {offset: agent.session.seq, activation}`，再 `session.append("goal/change", change)`；`session/event` 回调里若 `SessionSeq(offset) === event.seq` 则应用该 activation，否则用 `disarmed`（index.js:603-608、:860-872）。这保证「快照落盘成功才武装」，且重放时不会凭空 arm。
- **自动 disarm 的边界**：
  - `agent/created` → `disarmed`（index.js:600-602）。
  - driver 启动时对 `ctx.agents.list()` 中每个 agent 调 `disarm`（_rd.js:357）。
  - driver 在 `agent/error`、`turn/end`（`max-tokens`，或 `aborted` 且 `reason.kind === "disposed"`）、未知失败分支、拆装阶段都调 `disarm`（_rd.js:211-213、:273-285、:358-377）。
  - `pause`/`complete`/`block`/`clear` 后 activation 直接落 `disarmed`。
- **resume 的条件与副作用**：phase ∈ {active(disarmed), paused, blocked} 且 `roundsStarted < maxGoalRounds`；提交一个 `resume` 快照并把 activation 置 `armed`（index.js:691-704）。
- **`goal/activation-changed` payload**：仅当 activation 真的变化时发；`{sessionId: string, goal?: {id, revision, activation}}`——无当前 goal 时只有 `sessionId`（index.js:792-807）。

## 5. 工具契约

三个工具共享同一份输出：`{ goal: null }` 或 `{ goal: { id, revision, objective, phase, roundsStarted, maxGoalRounds, blockedReason? }, activation }`，以 `JSON.stringify` 作为单个 text block 返回（_toolgo_lib.js:220-245）。

### 5.1 `get_goal()`

- 入参：`{}`。输出：见上。无 goal → `{"goal": null}`。
- 权限：需要「精确活跃调用 agent + 开放轮次」（`GOAL_TOOL_AGENT_REQUIRED` / `GOAL_TOOL_DRIVER_REQUIRED`，_toolgo_lib.js:8-35）。
- presentCall：`{card:"generic", title:"Read current goal", kind:"read"}`。

### 5.2 `create_goal(objective, max_goal_rounds?)`

- 入参：`objective` 必填 string；`max_goal_rounds` 可选 number（正安全整数）。
- 校验：objective trim 后非空；`maxGoalRounds` 缺省用配置默认（工具侧无默认上限，服务侧默认 256）。
- 人类轮次要求：必须满足 direct-human 授权，否则 `GOAL_TOOL_AUTHORITY_REQUIRED`（`this goal operation requires a direct human turn on a top-level agent`，_toolgo_lib.js:62-65）。
- 已有 goal：由服务判定——非 `complete` 的当前 goal → `GOAL_ALREADY_EXISTS`。
- 副作用：服务提交 `create` 快照并 arm。

### 5.3 `update_goal(goal_id, revision, action, objective?, max_goal_rounds?, blocked_reason?)`

- `action ∈ {edit, pause, resume, complete, blocked}`（_toolgo_lib.js:115-121）。
- `goal_id` 非空且首尾无空白、`revision` 正安全整数，否则 `GOAL_TOOL_INVALID_UPDATE`（_toolgo_lib.js:212-218）。
- 各 action 前置条件：

  | action | 授权 | 其它前置 |
  |---|---|---|
  | `edit` | direct-human | 至少一个有效替换值（`objective !== ""`、`max_goal_rounds !== 0`），否则服务报 `GOAL_INVALID_EDIT` |
  | `pause` | direct-human | 服务侧 phase = active |
  | `resume` | direct-human | 命中持久 `paused` → **`GOAL_TOOL_RESUME_PAUSED`**（模型不许恢复 paused，必须由用户路径恢复） |
  | `complete` | direct-human **或** 当前 goal round | 服务侧非 complete |
  | `blocked` | direct-human **或** 当前 goal round | `blocked_reason` 必填非空（否则 `GOAL_TOOL_INVALID_UPDATE`）；goal-round 授权且 `roundsStarted < blockedAfterConsecutiveRounds` → `GOAL_TOOL_BLOCK_THRESHOLD` |

- `blocked` 持久化的 code 固定为 `model-reported`，message = `blocked_reason`（_toolgo_lib.js:355-358）。
- 被忽略字段不得改变状态：非 `edit` 传 `objective`/`max_goal_rounds`、非 `blocked` 传 `blocked_reason` 一律忽略（_toolgo_lib.js:337-358）。
- **终局 wrapup**：当授权来源是 goal-round（自主轮）且 action 为 `complete`/`blocked`，工具调用 `exec.deferContext(...)` 注入一条 `source = {kind:"tool-goal", form:"notice", summary: boundContextSummary("<action>: <objective>")}` 的用户消息，内容为 `<goal_complete>`/`<goal_blocked>` 段落（_toolgo_lib.js:91-98、:359-366）。文本要求模型「现在写给用户的收尾消息，只报告本会话证据能支持的内容，不要再调用工具」。
- 错误码全集（工具层）：`GOAL_TOOL_AUTHORITY_REQUIRED`、`GOAL_TOOL_AGENT_REQUIRED`、`GOAL_TOOL_DRIVER_REQUIRED`、`GOAL_TOOL_INVALID_UPDATE`、`GOAL_TOOL_RESUME_PAUSED`、`GOAL_TOOL_BLOCK_THRESHOLD`；服务层错误码见 §3。

### 5.4 策略文本

系统提示词段落名 `tool:goal`，顺序取 `TOOL_GOAL` 段位（_toolgo_lib.js:258-262）。原文（`${blockedAfter}` 取配置值，默认 3）：

```markdown
create_goal may infer goal intent from a direct human request in any language. After session resume or fork, an active goal is disarmed: when a human asks to continue or resume in any wording or language, use update_goal action resume to rearm it. Mark complete only when the objective is actually achieved. Mark blocked only after the same blocking condition persists for at least 3 consecutive goal rounds, and report that concrete condition in blocked_reason; difficulty, uncertainty, or useful remaining work is not blocked.
```

来源：_toolgo_lib.js:194-196；配置项 `blockedAfterConsecutiveRounds`（正安全整数，默认 3，_toolgo_lib.js:114、:198-202）。工具描述原文：`create_goal` = "Create a persisted goal that keeps this session working across automatic continuation rounds. Use it when the direct human request is a long-running objective, even if the user did not say \"goal\"; not for single-turn work."；`get_goal` = "Read the current session goal, including the id and revision that update_goal requires."（_toolgo_lib.js:122-123）。

## 6. 总线事件 payload

`goal/change` 是**记录**不是总线事件（`src/deeppyro/core/wire.py:167` 已在记录词表）。总线侧只有下面这些：

| 事件 | mode | payload 精确字段 | 何时发 | 作用域 |
|---|---|---|---|---|
| `goal/changed` | emit | `{ change: { operation, ref: {id, revision}, goal?: GoalView } }`；`GoalView = {id, revision, objective, phase, roundsStarted, createdAt, updatedAt, activation, blockedReason?}` | 每次 `session.append("goal/change")` 成功后 | **agent 作用域**（`agentEvents(ctx, agent).emit`，_dsh-goal L874-879） |
| `goal/activation-changed` | emit | `{ sessionId: string, goal?: {id, revision, activation} }` | activation 真变化时（含 disarm） | **context 全局**（`ctx.emit`，L792-807） |
| `agent/inbox/inserted` | emit | `{ message: Message }` | splice 后有 inserted（`discardRemoved` 之外的插入也算） | agent 作用域（L199-200） |
| `agent/inbox/claimed` | emit | `{ message: Message, turn: number }` | `inbox.claim(target, turn)` 每取出一个 | agent 作用域（L106-110） |
| `agent/inbox/discarded` | emit | `{ message: Message }` | splice 且 `discardRemoved` 为真时，逐个 removed | agent 作用域（L199） |
| `agent/status` | emit | `{ agent: Agent, status: "idle" \| "running" }` | 状态迁移 | agent 作用域（driver 只消费 `idle`，_rd.js:223-242） |

补充：`agent/inbox/spliced` 是**记录**（`session.append`），不是总线事件（L199）。`goal/changed` 的 `goal` 字段是 `this.view(...)` 的产物（goal 全字段 + `roundsStarted`/`createdAt`/`updatedAt`/`activation`），无当前 goal 时省略（L873-879、L882-891）。

## 7. round driver 不变量（逐条对照 _rd.js）

1. **drivable**：`goal !== undefined && goal.phase === "active" && goal.activation === "armed"`（_rd.js:80-82）。
2. **readyToDrive**：`ctx.fiber.state === 2`（ACTIVE）**且** `!state.stopping` **且** `ctx.agents.get(agent.id) === agent`（同一个活实例）**且** `agent.status === "idle"` **且** `!state.competingQueued`（_rd.js:84-86）。
3. **drive() 步骤顺序**（_rd.js:108-174）：① `readyToDrive` 失败即返回；② `needsCheckpoint` → `await ctx.sessions.flush(agent.session)`，失败则 `disarm` 并返回，成功后用 `readyAfterCheckpoint` 再验；③ 若存在 `attempt`：清 `attempt`、置 `needsCheckpoint = true`、`requested = true`，**本轮直接返回**（强制「一轮结束 → 落盘 → 下一轮」）；④ 取 `currentGoal`，不 drivable 即返回；⑤ 若存在 `subagents` 服务：`await Promise.race([subagents.waitForChildren(agent), unloading.promise])` 并再验 ready；⑥ `roundsStarted >= maxGoalRounds` → `block({code:"round-limit", message:"Goal reached its configured limit of <n> rounds."})`；⑦ `round = roundsStarted + 1`，用 `renderGoalRoundPrompt(goal, round)` 造 `createUserMessage({content, source:{kind:"goal", goalId, revision, round}})`；⑧ 置 `attempt = {goalId, revision, round, messageId, content, phase:"queued", cancelled:false, stale:false}`；⑨ `agent.followup(message)`；抛错 → 清 attempt 并在「latest 仍是同 ref 的 active+armed」时 `block({code:"queue-failed"})`。
4. **预留与状态迁移**：`queued` →（inbox/claimed，且 `sameQueued(content, source, attempt)`）`claimed` →（`user/message` 且 `event.data.id === attempt.messageId`）`admitted`；`discarded` 命中同一消息 → `attempt.cancelled = true`；`inserted` 命中 next-turn 且不是自己的 round → `competingQueued = true`，若 attempt 处于 `queued` 则 `attempt.stale = true`（_rd.js:249-264、:265-288）。
5. **pre-step 栅栏校验清单**（_rd.js:290-356）：先找 `messages` 中第一条 `source.kind === "goal" && round > 0` 的消息作为 submitted；`validReservation` 要求「fiber ACTIVE && !stopping && attempt 存在 && `attempt.phase === "claimed"` && `!attempt.stale` && `sameQueued(content, source, attempt)` && goal 存在且 `goal.id === source.goalId` && `goal.revision === source.revision` && `goal.phase === "active"` && `goal.activation === "armed"` && `source.round === goal.roundsStarted + 1`」。无效 → `attempt.stale = true`、清 attempt、`restoreOtherClaimed(agent, messages, submitted.id)`、`requestDrive`、返回 `{kind:"reject"}`。
6. **失败路径**：`followup` 抛错 → `queue-failed`（见 3⑨）；flush 抛错 → `disarm`；round 用尽 → `round-limit`；`agent/error` → `disarm`；`agent/disposed` → 删除该 agent 的 state；`goal/changed` 的 `operation === "pause"` 且 agent 正在 running 且 `currentInitiator() !== agent` → `agent.cancel({kind:"user"},{keepInbox:true})`；`turn/end` 的 `max-tokens`、或 `aborted` 且 `reason.kind === "disposed"` → `disarm`；其余 `aborted`：attempt 处于 `claimed`/`admitted` → 标 `cancelled`，否则 `disarm`；pre-step 里 `next()` 返回 `{kind:"reject"}` → 清 attempt 并在同 ref active+armed 时 `block({code:"prompt-rejected"})`；卸载期 `stopping = true` + `disarm` + 对 running 的 attempt 发 `agent.cancel({kind:"parent"})` 并等 idle（_rd.js:210-378）。
7. **幂等与单飞**：`requestDrive` 用 `state.requested` + `state.run` 合并触发，循环 `while (requested && !stopping)` 在 `ctx.agents.withoutInitiator(...)` 内跑；`run` 结束时若又有 `requested` 再开一轮（_rd.js:176-209）。
8. **turn 停止条件**：只有 `turn/end` 的 `max-tokens` 与 `aborted(reason.kind === "disposed")` 会 `disarm`；`aborted` 的其它理由不 disarm，而是把 attempt 标 `cancelled` 让 `idle` 分支结算（`pause` + `inbox.remove`）。`complete`/`blocked` 的自主轮**不硬停 turn**，而是由工具注入 wrapup 上下文让模型收尾（§5.3）。
9. **resume/fork**：driver 装载时对 `ctx.agents.list()` 全部 `disarm`（_rd.js:357）；goal 服务在 `agent/created` 也置 `disarmed`（_dsh-goal L600-602）⇒ 会话恢复/分叉后 active goal 一律 disarmed，必须由人类 `resume` 重新 arm。

## 8. deeppyro 映射

| 项 | DSH | deeppyro 落点（已核实） | 差距 |
|---|---|---|---|
| 记录名 | `goal/change` | 已在记录词表 `src/deeppyro/core/wire.py:167`；常量 `src/deeppyro/goal/service.py:22 GOAL_EVENT` | v0.1 data 是 `{goal: str \| null}`，需按 §2.3 兼容旧格式 |
| 服务 | `ctx.goals` | `src/deeppyro/goal/service.py:63 class GoalService`，`provide="goal"`（`src/deeppyro/builtin/goal.py`），现有 API：`configure/bind/unbind/get/set/clear/context_text/_publish` | 无 revision/CAS/phase/rounds、无 activation、无 fold 校验 |
| 工具 | `get_goal`/`create_goal`/`update_goal` | 现只有单个 `goal` 工具（`src/deeppyro/tools/builtin/goal.py`，参数 `text?`/`clear?`） | 需替换为三工具 + 授权 + 阈值 + wrapup |
| 提示词 | 段名 `tool:goal`，正文含阈值数字 | `src/deeppyro/builtin/goal.py:73 ctx.register_prompt_section("goal", 35, self._render)` | 段名与正文都要按 §5.4 改（order 35 保留） |
| 驱动挂载 | 独立包 `dsh-goal-round-driver`（opt-in） | 需新建 `src/deeppyro/builtin/goal_round_driver.py` + `bundles/core/bundle.toml` 行（默认关，`[goal] driver=true` 才启用） | 全新 |
| 记录观察 | `session/event`（session, event） | `src/deeppyro/session/session.py:96 add_append_observer(observer: Callable[[Session, LogRecord], None])`，`:113 observer(self, record)` | 无 `Session.subscribe`；用 observer 适配即可（D6 决策：不新增名字） |
| idle 触发 | `agent/status {agent, status}` | `src/deeppyro/agent/session.py:504 await self._hooks.emit(AGENT_STATUS, {"status": phase}, scope=self._hook_scope())`，phase 只有 `running`(:484)/`idle`(:492) | payload **没有 agent/session 引用**；M3a 需把 `agent`（或 session id）加进 payload 或改由 driver 按 session 反查 |
| inbox 事件 | `agent/inbox/{inserted,claimed,discarded}` | `src/deeppyro/agent/inbox.py:37-40` 常量；`:142 Inbox(log, *, emit)`；`agent/session.py:289` 注入 `self._inbox_event`，`:1236 _inbox_event` 只 `self._emit(event)` 到 sink | 需同时 `hooks.emit` 到总线（M3a）；`claimed` 的 `turn` 字段 deeppyro 已有 `claim(target, turn)` |
| turn/end reasons | `max-tokens`、`aborted{reason:{kind:"disposed"}}` | `src/deeppyro/agent/session.py:671 max-tokens`、`:1630 aborted{reason:{kind}}`、`_abort_turn("disposed")` :547/:555；另有 `completed`/`max-steps`/`error`/`blocked{pre-step}` | 语义可映射，无需改记录 |
| 活实例/根/发起者 | `agents.get/turnBoundaryOf/roots/currentInitiator/withoutInitiator` | deeppyro `AgentsPlugin`/`AgentSession` 无这些 API（`busy`/`turn`/`step`/`inbox` 有） | 需 M3a 提供最小等价物（`is_live`、`is_root`、`turn_boundary`、`initiator`），或 driver 降级为单 agent 语义（§9） |
| 终局 wrapup | `exec.deferContext(message)` | deeppyro 工具无 `deferContext`；可用 `agent.inject(...)`（next-step，不唤醒）近似 | 语义不完全等价（DSH 在**当前 step 后**注入并结束轮次） |
| flush 检查点 | `sessions.flush(session)` | deeppyro 每条 append 即 flush+fsync（`src/deeppyro/session/session.py:26,85`；`session/writer.py:108,134`） | 天然满足，driver 的 flush 可退化为 no-op |
| subagent 等待 | `subagents.waitForChildren(agent)` | `src/deeppyro/builtin/team.py`/`subagent` 服务无同名 API | M4 前按「无 subagents 服务」分支处理 |

## 9. UNRESOLVED

1. **initiator/根 agent 语义**：DSH 用 `currentInitiator()` + `roots()` 判定「人类直接请求」；deeppyro 没有 initiator 概念。M3a 需要决定：新增 `AgentSession.initiator`（`source.kind === "user"` 的入站消息即设）还是用「当前 turn 内存在 user/message 且非 plugin 注入」近似。**影响 §5 的全部授权判定，必须先定。**
2. **`agent/status` payload 扩展**：模板 §8 提到「deeppyro agent/status 的 done 相位」，实际只有 `running`/`idle`。M3a 要么在 payload 加 `agent`/`session`，要么在 driver 侧用 scope 里的 `session` 反查 agent（driver 需要 agent 对象才能 followup/cancel）。
3. **`deferContext` 等价物**：deeppyro 的 `agent.inject(text, source=...)` 是 next-step 且「不唤醒」；DSH 的 `deferContext` 是「本 step 结束后作为上下文注入并结束自主轮」。M3b 需确认用 inject 是否足以让模型收尾，或需要新增工具结果附带上下文。
4. **driver 默认开关**：模板 §8 写「独立插件 + bundle.toml opt-in 行」。建议默认 **关**（`[goal] driver = false`），因为自动续行会消耗真实 token；需用户确认。
5. **completion 自主轮的 turn 结束**：DSH 说「结束物理 turn」，实现是注入 wrapup 让模型写最终消息后自然结束。deeppyro 若用 inject，模型可能继续工具调用。M3b 需决定是否在 wrapup 后强制 `_close_turn`（会与「human 变更不停止 turn」区分）。
6. **旧格式写入方的处置**：`src/deeppyro/goal/service.py:188` 现在 `emit("goal/changed", {"session": session_id, "goal": goal})`；新 payload 是 `{change:{...}}`。是否有外部消费者依赖旧 payload？仓内检索仅 `tests/test_goal.py`。M2 一并改测试即可（无兼容负担），但需用户确认「bus payload 不保留旧形状」。
7. **section 名**：`register_prompt_section("goal", 35, ...)` 与 DSH 的 `tool:goal` 不同名。改名会改变 prompt 段落集合（可能影响既有测试快照），建议保留 `goal` 名、只对齐正文（需确认）。
8. **`maxGoalRounds` 上限**：DSH 只要求「正安全整数」，无上限；deeppyro 是否要加上限（如 1e6）防止选手误配？建议不加，按契约一致。

## 10. 证据索引

| 条目 | 证据 |
|---|---|
| 快照 7 键、tombstone 5 键 | `_dsh_src/dsh-goal/lib/index.js:140-161`、`:122-137` |
| phase 枚举 / 动词枚举 | `lib/index.js:42-47`、`:34-41` |
| revision +1 与不可变字段 | `lib/index.js:174-183` |
| create 前置（revision 1 / active / 0 rounds / 无当前或当前 complete / id 未见过） | `lib/index.js:247-249` |
| clear 墓碑 revision = 当前+1 | `lib/index.js:741-758` |
| round 准入 fold | `lib/index.js:274-280` |
| projection 定义与 stateVersion 6 | `lib/index.js:440-454`；状态 schema `:365-383` |
| replay 失败冻结 | `lib/index.js:423-438`、`:776-780` |
| 服务 inject / 默认 256 | `lib/index.js:588-596` |
| commit 握手 + goal/changed | `lib/index.js:860-879` |
| view 形状（含 activation） | `lib/index.js:882-891` |
| activation 变化事件 | `lib/index.js:792-807` |
| 错误码：GOAL_INVALID_OBJECTIVE / MAX_ROUNDS / BLOCK_REASON | `lib/index.js:456-482` |
| 错误码：GOAL_ALREADY_EXISTS / INVALID_EDIT / INVALID_TRANSITION | `lib/index.js:644`、`:666`、`:828-830` |
| 错误码：GOAL_NOT_FOUND / STALE_REVISION / AGENT_NOT_LIVE | `lib/index.js:765-774` |
| resume 的两条拒绝（已 armed / 轮次耗尽） | `lib/index.js:691-704` |
| 工具三件套 + schema | `_toolgo_lib.js:263-370`、`:124-195` |
| 工具权限（agent/driver/direct-human） | `_toolgo_lib.js:8-35`、`:49-65` |
| 阈值与提示词文本 | `_toolgo_lib.js:114`、`:194-202` |
| GOAL_TOOL_RESUME_PAUSED | `_toolgo_lib.js:348` |
| blocked_reason 必填 + model-reported | `_toolgo_lib.js:353-358` |
| GOAL_TOOL_BLOCK_THRESHOLD | `_toolgo_lib.js:354` |
| wrapup 文本与 deferContext | `_toolgo_lib.js:91-98`、`:359-366` |
| round 提示词模板 | `_rd.js:11-18` |
| drivable / readyToDrive | `_rd.js:80-86` |
| drive 顺序与 round-limit / queue-failed | `_rd.js:108-174` |
| restoreOtherClaimed | `_rd.js:100-106` |
| 单飞 requestDrive | `_rd.js:176-209` |
| 生命周期事件绑定 | `_rd.js:210-288` |
| validReservation | `_rd.js:290-293` |
| pre-step 栅栏与 prompt-rejected | `_rd.js:295-356` |
| 卸载 disarm / parent cancel | `_rd.js:357-377` |
| `/goal` 语法与渲染 | `_cmdgo_lib.js:19-35`、`:59-77`、`:108-172` |
| `/goal` 注册（attachments: true） | `_cmdgo_lib.js:174-185` |
| inbox payload（claimed/inserted/discarded） | `_agent_loop_lib.js:106-110`、`:199-200` |
| deeppyro 记录词表含 goal/change | `src/deeppyro/core/wire.py:167` |
| deeppyro 现有 goal 服务 | `src/deeppyro/goal/service.py:22`、`:63`、`:145-190` |
| deeppyro 记录写入点（旧形状） | `src/deeppyro/goal/service.py:155`、`:167`、`:188` |
| deeppyro 总线目录 goal/changed | `src/deeppyro/core/events.py:84` |
| deeppyro append observer | `src/deeppyro/session/session.py:96`、`:113` |
| deeppyro agent/status payload | `src/deeppyro/agent/session.py:497-504`、`:484`、`:492` |
| deeppyro inbox 事件常量 | `src/deeppyro/agent/inbox.py:37-40`、`:142`、`:195-232` |
| deeppyro inbox → sink | `src/deeppyro/agent/session.py:289`、`:1236-1241` |
| deeppyro turn/end reasons | `src/deeppyro/agent/session.py:576-693`、`:1567`、`:1630`、`:1650` |
| deeppyro cli/工具现状 | `src/deeppyro/tools/builtin/goal.py`；`src/deeppyro/builtin/goal.py:73` |
