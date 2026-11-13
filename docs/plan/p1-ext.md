# P1 扩展调研：skills / schedule / memory

调研任务 task-33 交付物。参考实现：D:/Code/deepseek-harness（DSH TypeScript 源码，下文行号均指该仓库）。目标仓库：D:/code/deeppyro（DeepPyro，纯 Python 内核 + Textual TUI）。本文只做语义调研、Python 设计与任务拆分建议，不含实现代码。

## 0. 结论速览

| feature | DSH 是否存在 | P1 建议范围 | 最大风险 |
|---|---|---|---|
| skills | 有，3 个包（skill / skill-filesystem / tool-skill） | 只读文件发现 + skill 工具 + 会话技能目录 | 无 PyYAML；目录注入需改 agent 或降级为 prompt section |
| schedule | 有，3 个包（schedule / tool-schedule / util-time） | 6 种 timing（cron 可选）+ 持久化 + 单定时器 + 4 个工具 + 宿主投递协议 | RRULE 无 python-dateutil 工作量翻倍；宿主 session/agent 投递接口缺失 |
| memory | DSH 核心无；宿主由第三方插件 dsh-mnemon 提供 | 只做 runtime memory（USER.md/MEMORY.md）+ 2 个工具 + prompt 注入 | 与 dsh-mnemon 完整协议差距大，容易被误读为要复刻 View/Strategy/Source |

三项都按「新 plugin + bundle 行」方式接入，不改 kernel/api 抽象；但 schedule 与 memory 需要少量 agent/runtime 接线（见第 6 节），这部分必须由 Lead 或对应 owner 另开任务。

## 1. 调研范围与源码坐标

- DSH 核心源码：D:/Code/deepseek-harness/packages/skill/**、packages/schedule/**、packages/util/time/**、packages/preset/**、packages/context/**、packages/client/ui-skill、packages/client/ui-schedule。
- DSH 子系统文档（权威契约）：docs/subsystems/skills.zh.md（352 行）、docs/subsystems/schedule.zh.md（227 行）。本调研以 TypeScript 源码为准，文档只用于交叉验证。
- 宿主记忆：DSH 开源核心与 D:/Code/deepseek-harness 全仓 grep 无 mnemon / MEMORY.md / USER.md；实际记忆能力来自桌面 profile 安装的插件 C:/Users/74185/.dsh/profiles/desktop/node_modules/dsh-mnemon 及其 source/strategy/provider 子包。
- Python 现状：docs/spec/INTERFACES.md、docs/spec/plugin-core.md、docs/spec/hooks.md、docs/spec/tools.md、docs/spec/agent-presets.md、docs/spec/session.md；src/deeppyro/{agent,api,builtin,hooks,kernel,plugins,prompt,session,tools}/。

## 2. Skills

### 2.1 DSH 精确语义（源码路径 + 行号）

服务定义 packages/skill/skill/src/index.ts（805 行）：
- 21 行 SKILL_NAME 正则 /^[a-z0-9]+(?:-[a-z0-9]+)*$/；28 行 BUNDLED_SKILL_RANK = 600。
- 170 行 renderSkillContent(skill)：统一渲染块，skill 工具结果与用户显式调用共用。精确形状：<skill_content name=转义名> 后接 <skill_resources>（有 resourceBase 时给出 Base directory 或 URL，否则提示 provider 管理资源）与 <skill_instructions>正文</skill_instructions>。
- 296 行事件 skills/change（emit，listener 失败隔离，不可 veto）。
- 356 行 class SkillRegistry extends Service（ctx.skills）；390 行 registerProvider(create)；470 行 async list(options)；711 行候选 name 校验；742/758 行运行时 skill 校验。
- 分层：global 层 + 当前 scope 链（preset realm 挂载的行落入该 preset 层）；近层覆盖远层；同层内按 rank、providerOrder、localOrder 决出 winner，重复警告而不是报错。
- 摘要 SkillSummary 字段：path?、name、description、whenToUse?、invocation{modelInvocable,userInvocable}、source、provider、resourceBase?；SkillDefinition 追加 content；SkillRegistration 是运行时注册。

本地 provider packages/skill/skill-filesystem/src/index.ts（1063 行）：
- 35-39 行 rank：PROJECT_DSH=100、PROJECT_AGENTS=200、CUSTOM=300、USER_DSH=400、USER_AGENTS=500，BUNDLED=600（skill 包 28 行）。
- 57-88 行 Config：providerName 默认 filesystem、includeDefaultRoots=true、dshHome=$DSH_HOME 或 ~/.dsh、agentsHome=$DSH_AGENTS_HOME 或 ~/.agents、customSkillDirs=[]、watch=true、watchUsePolling=false、watchStabilityThresholdMs=200、watchPollIntervalMs=100、watchMaxProjects=128、watchFollowSymlinks=true、bundledSkillDir。
- 256-268 行 roots 组装顺序：项目 .dsh/skills（100，source project-dsh）< 项目 .agents/skills（200，project-agents）< customSkillDirs（300，custom）< dshHome/skills（400，user-dsh，跳过 .system）< agentsHome/skills（500，user-agents）< bundled（600，bundled）。
- 项目根为含 .git 的最近祖先目录，找不到时用 cwd。
- 发现规则：每个 root 只看直接子项，目录看 <dir>/SKILL.md，文件名以 .md 结尾的扁平文件也可；解析失败仅 warning 跳过。
- frontmatter 格式：首行必须是 ---，到下一行 --- 之间为 YAML，必须解析为 object；正文为之后内容并 trim。规范键：name 必填 kebab-case、description 必填非空、whenToUse 可选、disable-model-invocation 与 user-invocable（布尔或 1/0、true/false、yes/no、on/off，默认分别 false/true）、metadata 可选 object。旧键 disableModelInvocation/modelInvocable/userInvocable 被明确拒绝（1015-1018 行）。
- watch：chokidar 深度 1，变更时 provider invalidate 并触发 skills/change；最多 watch 128 个项目。

工具与目录 packages/skill/tool-skill/src/index.ts（429 行）：
- 36 行 kind: skill-catalog；48 行 source 形状；66/71/81 行 catalogDescriptionMaxLength 默认 500 最小 3。
- 128 行 skill 工具结果用 renderSkillContent；202 行用户显式 /skill 调用生成 user message（正文同样是 renderSkillContent）。
- 268-270 / 310-312 行 <available_skills> ... </available_skills> 目录块；281/320 行 source.kind=skill-catalog；390-404 行 catalogHistory 从最新可见消息向前扫。
- 429 行 SKILL_GESTURE = /(^|\s)\/([a-z0-9]+(?:-[a-z0-9]+)*)(?=\s|$)/g，只扫 user source 的入站消息；命中且 user-invocable 时把技能正文作为 user message 追加到本轮消息尾部，普通文本 token 保持原样。
- 会话目录：首个 agent/pre-step 发布 durable user-role <system-reminder> 目录，内容只有 model-invocable 的 name + description（空白归一、超长截断为 ...），digest 为各 [name,description] JSON 行 sha256；之后每步对比可见目录摘要，变化时追加 replace 版本；首句明确「目录只是摘要，先调用 skill 工具」。

Skill 示例：D:/Code/deepseek-harness/.agents/skills/dsh-code-review/SKILL.md 只有 name、description 两行 frontmatter，正文是 Markdown 路由说明；packages/skill/skill-office/assets/office/SKILL.md 同形。结论：实际使用中 frontmatter 极简，复杂 metadata 很少。

### 2.2 与 DSH 的最小差异（P1 裁剪）

- 不做 provider 注册表与 scope 分层：DeepPyro 的 preset scope 只服务于工具/prompt，P1 技能目录按 cwd 全局发现；预留给后续 SkillProvider 接口。
- 不做 watch 与 collect cache：每次工具调用/目录渲染重新扫描（目录规模小），配置里保留 watch=false 的占位。
- frontmatter 不引入 PyYAML：自写仅支持规范键 + 简单标量的解析器（约 30 行）；不支持 metadata 对象与多行块。若后续要完整 YAML，再由 Lead 决定加 PyYAML 依赖。
- 目录注入方式：DSH 用 durable user/message + digest 去重；DeepPyro P1 推荐 prompt section 渲染（见 2.3 选项 A），因为 agent/pre-step payload 目前不含 cwd/session 句柄，改 payload 属 agent 域。
- 显式 /skill 手势 P1 非目标（TUI 已有 /命令 命名空间），只保留 skill 工具。

### 2.3 Python 设计

新增文件：
- src/deeppyro/skills/__init__.py：导出公开类型与 SkillRegistry。
- src/deeppyro/skills/types.py：dataclass SkillSummary / SkillDefinition / SkillCatalog / SkillConfig。
- src/deeppyro/skills/frontmatter.py：parse_skill_file(path) -> SkillFile | None，错误走 errors 列表。
- src/deeppyro/skills/discovery.py：SkillRoot dataclass + discover_roots(cwd, config) + scan_root(root)。
- src/deeppyro/skills/registry.py：class SkillRegistry。
- src/deeppyro/builtin/skills.py：SkillsPlugin(Service) provide=skills，inject=[config]。
- src/deeppyro/tools/builtin/skill.py：SkillTool(BaseTool)。
- src/deeppyro/builtin/skill_tools.py：SkillToolsPlugin(Plugin)，inject=[tools, skills, prompt]，注册工具与 prompt section。
- tests/skills/{__init__,test_frontmatter,test_discovery,test_registry,test_tools}.py。
- docs/spec/skills.md。

公开 API 建议（全部 from __future__ import annotations，dataclass frozen slots）：

    SkillSource = Literal['project-dsh', 'project-agents', 'custom', 'user-dsh', 'bundled']
    SKILL_SOURCES 与 rank 常量：PROJECT_DSH_RANK=100 等，与 DSH 数值一致
    @dataclass(frozen=True, slots=True)
    class SkillSummary:
        name: str; description: str; when_to_use: str = ''
        model_invocable: bool = True; user_invocable: bool = True
        source: SkillSource = 'project-dsh'; provider: str = 'filesystem'
        path: str = ''; resource_base: str = ''
    @dataclass(frozen=True, slots=True)
    class SkillDefinition(SkillSummary):
        content: str = ''
    class SkillRegistry:
        def __init__(self, config: SkillConfig, *, roots: Sequence[SkillRoot] | None = None) -> None
        async def list(self, cwd: Path | None = None) -> list[SkillSummary]
        async def get(self, name: str, cwd: Path | None = None) -> SkillDefinition | None
        def snapshot(self, cwd: Path | None = None) -> SkillCatalog
        def render_catalog(self, cwd: Path, *, max_length: int = 500) -> str

SkillConfig（pydantic，放进 [skills] 配置，默认值）：include_default_roots=True；custom_skill_dirs=()；user_skill_dir 默认 deeppyro_home()/skills；bundled_skill_dir=''；catalog_max_entries=500；catalog_description_max_length=500；watch=False。

技能根目录映射（P1 建议）：

| rank | source | DeepPyro root |
|---|---|---|
| 100 | project-dsh | <projectRoot>/.dsh/skills |
| 200 | project-agents | <projectRoot>/.agents/skills |
| 300 | custom | [skills].custom_skill_dirs |
| 400 | user-dsh | deeppyro_home()/skills（~/.dpro/skills） |
| 600 | bundled | [skills].bundled_skill_dir 或包内 assets |

projectRoot 规则同 DSH：最近含 .git 的祖先目录，否则 cwd。「需 Lead 决策」是否兼容 ~/.agents/skills（DSH user-agents rank 500）：建议 P1 不默认扫描，留 custom_skill_dirs 兼容。

skill 工具契约（tools/builtin/skill.py）：name=skill，parallel_safe=True，parameters 为 {name: 必填 string}；description 直接沿用 DSH 文案：Load the full instructions for a skill. Call it before acting on a task that names or clearly matches a skill in the session skill catalog. execute 流程：校验 kebab-case -> 从 ctx.services.get('skills') 取注册表 -> list(cwd=ctx.cwd) 找摘要 -> 不存在则 ToolError -> 非 model_invocable 则 ToolError -> get 完整定义 -> renderSkillContent 文本返回。

renderSkillContent 输出（照抄 DSH 170 行语义）：

    <skill_content name="技能名">
    <skill_resources>
    [Base directory for this skill: 绝对路径；Resolve relative paths against it.]
    </skill_resources>
    
    <skill_instructions>
    正文
    </skill_instructions>
    </skill_content>

目录注入的两个选项：
- 选项 A（推荐 P1）：SkillToolsPlugin 注册 prompt section，名称 skills，order=35（位于 core tools=40 之前），renderer(tools, prompt_context) 用 prompt_context.cwd 调用 registry.render_catalog，输出 <system-reminder> + <available_skills> + 每行 - name: description + 使用指引。每次请求重新渲染，无 digest、无 durable 消息；system/message 投影会自动处理变化。
- 选项 B（DSH 同构，后续）：agent/pre-step hook 注入 user/message source.kind='skill-catalog'。当前 payload 见 src/deeppyro/agent/session.py:544-563，只有 messages/turn/step/signal/modelSelection/tools/agent，缺 cwd 与 session 句柄；需要把 cwd/sessionId 加入 payload 或给 AgentSession 增加只读属性。属 agent 域改动，「需 Lead 决策」。

权限与安全：技能文件只读，不触发 sandbox/approval；但技能正文会作为指令进入模型上下文，等价于用户可写 AGENTS.md，属于提示词注入面。P1 文档与工具描述必须写明来源路径；不要自动加载项目下载目录中的技能。

### 2.4 测试策略

- tests/skills/test_frontmatter.py：合法 frontmatter、缺 ---、缺 name/description、非法 name、布尔变体（1/0、yes/no、on/off）、旧键拒绝、正文 trim、扁平 .md 文件。
- tests/skills/test_discovery.py：tmp_path 造 .git 标记的项目根 + .dsh/skills + .agents/skills + custom dirs + user dir；rank 优先、同名覆盖、坏文件跳过、目录无 SKILL.md 忽略。
- tests/skills/test_registry.py：list 按 name 排序、get 不存在返回 None、大小写/非法 name、catalog 渲染截断与转义。
- tests/skills/test_tools.py：FakeSkills 注入 ToolContext.services，skill 工具正常/未知/不可模型调用三条路径；prompt section renderer 在 PromptContext(cwd=tmp_path) 下生成目录文本。全部用 pytest tmp_path，禁止依赖真实 HOME。

## 3. Schedule

### 3.1 DSH 精确语义（源码路径 + 行号）

服务 packages/schedule/schedule/src/index.ts（754 行）：
- 85/95 行配置 deliveryHistoryDays、newSessionIntervalSeconds；227 行 z.number().step(1).min(1).max(3650).default(DEFAULT_DELIVERY_HISTORY_DAYS)；229 行 newSessionIntervalSeconds min(MIN_EVERY_INTERVAL_SECONDS=60) max 604800。
- 223 行 class ScheduleService extends TypertRemoteService；352 行 deliveryLimits()；381 行 create(request, actorSessionId?, signal?)；414 list(request)；441 catalog()；451 view；461 history；495 run；520 deleteInactive；555 edit；560 行 update patch 至少一个字段。
- create 生成 id = schedule- + randomUUID；默认 target 为 {kind:new_once}；写入后 emitChanged、requestDrive、queueNotification。

类型 packages/schedule/schedule/src/types.ts（567 行）：
- 300 行 ScheduleInputErrorCode；317 行 RRuleScheduleRecord；332 行 ScheduleRecord；335-342 行 ScheduleTiming 七种：after/at/every/daily/weekly/cron/rrule（输入 snake_case：after_seconds、every_seconds、at、daily{time,time_zone}、weekly{time,time_zone,weekdays}、cron{expression,time_zone}、rrule）。
- 345 行 ScheduleSessionCreateSpec{cwd?,agentPreset?}；353-373 行 target：session{sessionId} / new_once{sessionId 预留 + create} / new{create}；397 行 receipt{scheduledAt,deliveredAt,messageId,targetSessionId}；409 行 DeliveryRecord；452 行 CatalogEntry（record + target + involvedSessionIds + status active/paused/ended + createdAt/updatedAt + executionActive/deliveryFloorEndsAt/lastDelivery）；478/490/531/545 行 create/list/patch/edit 请求。
- 约束：title 去空白非空 ≤120 字符；prompt 非空；scheduledAt 为 RFC3339 UTC 毫秒；every ≥60 秒；weekly weekdays 非空唯一升序（ISO 1=周一..7=周日）；daily/weekly time 规范化为 HH:mm:ss.SSS；cron 五字段；rrule 恰好 DTSTART + RRULE 两行。

时间引擎 packages/util/time/src（cron.ts 464 行、rrule.ts 626 行、instant.ts 286 行）：
- cron：五字段；支持 *、单值、区间、*/n 与区间步长、逗号列表；周日 0/7 规范为 0；拒绝名字/宏/秒字段/单值后步长；Vixie 语义（dom 与 dow 都受限时任一匹配；任一以 * 开头时两组都须匹配）；400 年搜索上限；无未来时点拒绝。
- rrule：严格 RFC5545 子集，引擎 rrule 2.8.1；只接受 FREQ、INTERVAL、COUNT、UNTIL、WKST、BYSECOND、BYMINUTE、BYHOUR、BYDAY、BYMONTHDAY、BYYEARDAY、BYWEEKNO、BYMONTH、BYSETPOS；拒绝 SECONDLY、多个 BYSECOND、COUNT 与 UNTIL 共存、COUNT>1000、每周期 >10080 候选、BYSETPOS >31 等；DTSTART 必须真实本地时间，TZID 时区存在，跳过不存在本地 occurrence、重叠取较早、COUNT 只按唯一时点计数；年份 0100-9999。

存储 packages/schedule/schedule/src/storage.ts：133-136 行 domain {name:schedule, version:2, compatibleVersions:[1], tables:{tasks}}；JSON 路径 <DSH_HOME>/storages/schedule.json；SQLite 表 u_schedule_tasks 只存 JSON 文本。

运行时 packages/schedule/schedule/src/runtime.ts（473 行）：30 行最小投递间隔；66 行单 timer 串行；200-225 行到期扫描与下次唤醒；233 行 deliverDue；288 行 nextAdmissionAt（lastDelivery 锚点 + interval，new 目标用更大间隔）；353 行 send：createUserMessage 且 source {kind:'schedule'}，followup 后等 session flush；369 行 settle；466 行 waterfall schedule/resolve-agent 解析目标 Agent；停机后只投递最近一次错过的 occurrence，规则耗尽转 ended；同一已有 session 的多个到期任务合并为一条消息；崩溃可能重复投递。

工具 packages/schedule/tool-schedule/src/index.ts（442 行）：管理授权句 MANAGEMENT_AUTHORIZATION 在 192-194 行；四个工具描述 196-207；title 帮助 209-210；rrule 帮助 212-226；TARGET_CHOICES 228；targetHelp 236-249；PATCH_FIELDS 252；modelTask 268（去掉 involvedSessionIds/createdAt/executionActive/deliveryFloorEndsAt，lastDelivery 只留 3 字段，追加 scheduledAtLocal）；runForCaller 299-318（delegationDepth>0 返回 subagent_session，其余 internal_error）；targetFor 320-338（this_session -> session；new_once/new -> create{cwd, agentPreset}）；工具注册 372/394/417/431；system prompt section tool:schedule 仅当 create/edit 可见时输出管理授权句。

### 3.2 与 DSH 的最小差异（P1 裁剪）

- 目标：session / new_once / new 三种目标保留；P1 至少实现 session 与 new_once，new 目标可延后（需宿主批量新建会话与限速）。
- 时间：after/at/every/daily/weekly 必做；rrule 必做（schedule_create 的唯一 timing 入口）；cron 可延后（工具参数只有 rrule，DSH 模型工具同样只暴露 rrule）。
- 持久化：DeepPyro 不用 SQLite 领域表，改为 ~/.dpro/schedules.json 单文件；保留 version 与 v1 兼容字段名，便于以后迁移。
- 回执：保留 lastDelivery + deliveryHistory（默认 200 条/30 天）；不做 pendingDelivery 的跨重启恢复（P1 单进程 TUI，重启后按 scheduledAt 重新扫描；文档注明可能重复投递）。
- 管理变更通知：DSH 向除操作者外的相关 Agent 排队通知；P1 只做 TUI 状态栏/通知，非目标。

### 3.3 Python 设计

依赖决策「需 Lead 决策」：RRULE 建议引入 python-dateutil>=2.9（纯 Python，提供 dateutil.rrule 与 tz），在适配层实现同 DSH 的严格子集校验；若不允许新依赖，则 P1 只支持 after/at/every/daily/weekly + COUNT=1 的一次性 rrule，完整 rrule 延后（工作量约 600-900 行且 DST 边界易错）。cron 引擎自写（约 200 行），不依赖第三方。时区使用标准库 zoneinfo.ZoneInfo。

新增文件：
- src/deeppyro/schedule/__init__.py：导出 ScheduleService、类型、错误。
- src/deeppyro/schedule/types.py：timing/target/record/receipt/catalog dataclass 与 ScheduleInputError。
- src/deeppyro/schedule/timeutil.py：RFC3339 UTC 毫秒解析与格式化、本地时间格式化（format_schedule_local_time）、时区校验。
- src/deeppyro/schedule/rrule.py：validate_rrule + next_occurrence（dateutil 适配层）。
- src/deeppyro/schedule/cron.py：parse_cron + cron_next（Vixie 语义）。
- src/deeppyro/schedule/store.py：ScheduleStore 原子 JSON 存储。
- src/deeppyro/schedule/engine.py：单 asyncio timer、到期扫描、投递与推进。
- src/deeppyro/schedule/service.py：ScheduleService 门面 + ScheduleHost 协议。
- src/deeppyro/builtin/schedule.py：SchedulePlugin(Service) provide=schedule，inject=[config, sessions]（host 由 runtime 注入）。
- src/deeppyro/tools/builtin/schedule.py：ScheduleTool/ListTool/ViewTool/EditTool + ScheduleToolsPlugin。
- tests/schedule/{__init__,test_timeutil,test_rrule,test_cron,test_store,test_service,test_tools}.py；docs/spec/schedule.md。

公开 API 建议：

    after_seconds / every_seconds / at: datetime / daily(time, time_zone) / weekly(time, time_zone, weekdays)
    Timing = AfterTiming | AtTiming | EveryTiming | DailyTiming | WeeklyTiming | CronTiming | RRuleTiming
    Target = SessionTarget(session_id) | NewOnceTarget(session_id, create) | NewTarget(create)
    @dataclass(frozen=True, slots=True)
    class ScheduleRecord:
        id: str; title: str; prompt: str; scheduled_at: str; kind: str; timing: Timing
        created_at: str = ''; updated_at: str = ''
    @dataclass(frozen=True, slots=True)
    class ScheduleCatalogEntry:
        record: ScheduleRecord; target: Target; status: str
        involved_session_ids: tuple[str, ...] = (); last_delivery: DeliveryReceipt | None = None
        delivery_history: tuple[DeliveryReceipt, ...] = ()
    class ScheduleService:
        def __init__(self, *, store: ScheduleStore, host: ScheduleHost | None = None) -> None
        def configure_host(self, host: ScheduleHost) -> None
        async def start(self) -> None; async def stop(self) -> None
        async def create(self, request: ScheduleCreateRequest, actor_session_id: str | None = None) -> ScheduleCatalogEntry
        async def list(self, *, active: bool | None = None, limit: int = 100, offset: int = 0) -> SchedulePage
        async def catalog(self) -> list[ScheduleCatalogEntry]
        async def view(self, task_id: str) -> ScheduleCatalogEntry | None
        async def history(self, task_id: str, *, limit: int = 50, before: str | None = None) -> DeliveryHistory
        async def edit(self, request: ScheduleEditRequest) -> ScheduleCatalogEntry | None
        async def delete_inactive(self) -> int
        async def run(self, task_id: str) -> ScheduleCatalogEntry | None
        def delivery_limits(self) -> dict[str, int]

    class ScheduleHost(Protocol):
        async def session_exists(self, session_id: str) -> bool
        async def is_archived(self, session_id: str) -> bool
        async def delegation_depth(self, session_id: str) -> int
        async def ensure_session(self, target: Target) -> Any
        async def deliver(self, session: Any, message: Any, task: ScheduleCatalogEntry) -> DeliveryAck

配置项 [schedule]（Config.extra allow 已是现状，无需改核心 config.py）：delivery_history_days=30、delivery_history_records=200、new_session_interval_seconds=60、scan_interval_seconds=5、default_time_zone=''（空表示跟随系统本地时区）。

持久化与记录类型：
- 任务存储：deeppyro_home()/schedules.json，形状 {version:1, tasks:{任务 id: CatalogEntry 的 JSON}}；原子写（tmp + os.replace）；单进程假设，无跨进程锁（与插件清单同一风险，文档注明）。
- 投递消息：宿主以 user/message 写入目标会话，message.source = {kind:'schedule', form:'reminder', scheduleId, scheduledAt, occurrenceAt, taskTitle}；RECORD_TYPES 已含 schedule/change，可另在 actor session 追加 {kind:'create'|'edit'|'delete'|'run', taskId, scheduledAt}，P1 可选。
- 数据校验：session v4 events.py 只要求 user/message source.kind 非空，schedule source 合法；不需要新增 wire 类型。

工具契约（tools/builtin/schedule.py，全部 parallel_safe=False）：
- schedule_create：title 必填（≤120，用户语言动作+对象）、prompt 必填、rrule 必填（帮助文本照抄 DSH 212-226 行）、target 必填 enum this_session/new_once/new；返回 modelTask 形状 JSON（含 scheduledAtLocal）。
- schedule_list：active?、limit?、offset?；schedule_view：id 必填；schedule_edit：id、action update|delete、title?/prompt?/rrule?/target?/active?/expectedUpdatedAt?（CAS）。
- 错误：invalid_prompt、invalid_selector、invalid_rule、invalid_time_zone、not_future、time_out_of_range、frequency_too_high、subagent_session、internal_error；工具只声明对应 code，其余转 internal_error。
- 子会话限制：P1 应在 api/types.py 的 CHILD_DISABLED_TOOLS 中追加 schedule_* 工具名（「需 Lead 决策」，api 属 Lead 域），工具执行时再经 ScheduleHost.delegation_depth 防御一次。
- 管理授权：注册 prompt section tool:schedule，order=45（core rules=50 之前），仅当本 scope 可见 schedule_create 或 schedule_edit 时渲染 DSH 原文：Only create or modify scheduled tasks at the user's explicit request; prior explicit user authorization for the running scheduled task remains valid.

宿主投递接线（最大工作量，见第 6 节）：TUI/headless 目前各自持有单 agent 与 SessionStore（src/deeppyro/tui/runtime.py、src/deeppyro/headless.py）。ScheduleHost 需要：
- session 目标：找到运行时当前 agent 或按 session_id 激活 agent，然后调用 AgentSession 的投递 API。现有 followup(text) 只构造 source=user 的消息（src/deeppyro/agent/session.py:297-307）；需要新增 followup_message(message: Message, target=TARGET_NEXT_TURN) 或 followup(text, source=...)，否则只能降级为普通用户文本（丢失 schedule source，不推荐）。此项改 agent/session.py，「需 Lead 决策」。
- new_once/new 目标：用 SessionsPlugin.create 或 SessionStore.create 建会话，再按 TuiRuntime._attach 的同样参数（provider/registry/options/permission/sink/services/preset）创建 AgentSession，后台任务执行 agent.send(prompt)，并把事件转发到 TUI 通知。P1 可先只支持 session 目标，new_once 延后。
- 投递框架文本：DSH 有 renderReminderFraming/renderRecurringReminderBatchFraming；P1 沿用固定前缀，例如 Scheduled task <title> (occurrence <local time>): 后接 prompt，避免与普通消息混淆。

### 3.4 测试策略

- test_timeutil.py：RFC3339 解析/格式化、四位年份、毫秒精度、不存在本地时间拒绝、DST 重叠取较早（用 zoneinfo America/New_York）。
- test_rrule.py：合法最小规则、COUNT=1、TZID、拒绝 SECONDLY/COUNT+UNTIL/非法字段/坏 DTSTART、DTSTART 过去仍取未来首个 occurrence。
- test_cron.py：五字段、*/n、区间列表、dom+dow Vixie 语义、拒绝秒/名字/宏。
- test_store.py：round-trip、原子写无临时文件残留、坏 JSON 拒绝、版本兼容。
- test_service.py：注入 fake clock（engine 接受 now_fn 便于测试）；create/list/view/edit CAS 冲突；after/every 触发一次投递；停机后只补最近一次；规则耗尽转 ended；同 session 批量合并；expectedUpdatedAt 冲突返回 schedule_conflict。
- test_tools.py：FakeToolContext（services 含 schedule 服务）；四个工具正常/错误码；子会话拒绝；输出 JSON 可解析。

## 4. Memory

### 4.1 事实：DSH 核心没有记忆子系统

证据：
- D:/Code/deepseek-harness/packages、apps、docs 全量 rg mnemon|MEMORY.md|USER.md 无匹配；packages 下无 memory/mnemon 目录。
- D:/dshworkspace/_dsh_src 70+ 包同样只有 in-memory 字样（会话存储/缓存）。
- 实际记忆由桌面 profile 的第三方插件 dsh-mnemon 0.5.42 提供，其 cordis.patch.yml 以普通 bundle 行装配，不修改 DSH 内核。

dsh-mnemon 的协议规模（供判断 P1 边界）：
- Source：source-runtime（USER.md/MEMORY.md、修订、本地存储）、source-documents（Markdown 检索/归档）、source-memory-spaces（Provider-backed 长期记忆，默认 Mnemon Native 本地 SQLite）、source-skills（实验性自进化技能）。
- Strategy：default-three-tier（常驻 runtime + 按需 documents + 按需 memory-spaces）、general、auto-capture、light-context、scoped。
- Core 每轮生成唯一不可变 View（projection + routes + readGrants + actionOffers），Host 固定到一轮并控制工具访问；模型工具 17 个（mnemon_view_route、mnemon_view_action、mnemon_memory_bodies、mnemon_recall、mnemon_related、mnemon_status、mnemon_document_search/create/manage、mnemon_runtime_memory、mnemon_remember/link/forget、mnemon_memory_body_create/update/merge）。
- 宿主注入：user/message source.kind=mnemon、form=memory-snapshot，每轮 step/start 后、请求前提交；最多 4 个快照/65536 字节；旧快照用 developer/message（content=[]、source {kind:'dsh-mnemon', form:'memory-snapshot-retired'}）+ surfaceOp replace 退休；Python session.md:162/165 已观察到真实形状。
- 本机数据格式（~/.mnemon）：runtime/memories.json {version:1, entries:[{content, created_at, updated_at, target:user|memory, importance:critical|normal|low}]}；USER.md/MEMORY.md 是投影；documents/{active,archived}+index.json；memory-spaces 用 SQLite；skills/ledger.json。
- 安全准则：记忆是数据不是指令；历史记忆永不低于当前指令。

### 4.2 与 DSH 的最小差异（P1 裁剪）

P1 只做 Runtime Memory（USER.md / MEMORY.md），复刻数据格式与语义，不做 View/Strategy/Source 协议、Documents 检索、Memory Spaces/Provider、Skills 自进化。这些作为 Phase 2/3 的接口占位（SkillProvider 之外再留 MemorySource Protocol 即可），避免把 dsh-mnemon 数万行协议塞进内核。

### 4.3 Python 设计

新增文件：
- src/deeppyro/memory/__init__.py：导出 MemoryService、类型、错误。
- src/deeppyro/memory/types.py：MemoryEntry、MemoryReceipt、MemorySnapshot、MemoryConfig。
- src/deeppyro/memory/store.py：MemoryStore 原子 JSON + 两个投影文件。
- src/deeppyro/memory/service.py：MemoryService。
- src/deeppyro/builtin/memory.py：MemoryPlugin(Service) provide=memory，inject=[config, prompt]。
- src/deeppyro/tools/builtin/memory.py：MnemonicRuntimeMemoryTool + 可选 MnemonStatusTool。
- tests/memory/{__init__,test_store,test_service,test_tools}.py；docs/spec/memory.md。

公开 API 建议：

    MemoryTarget = Literal['user', 'memory']; MemoryImportance = Literal['critical', 'normal', 'low']
    @dataclass(frozen=True, slots=True)
    class MemoryEntry:
        id: str; content: str; target: MemoryTarget; importance: MemoryImportance = 'normal'
        created_at: str = ''; updated_at: str = ''
    @dataclass(frozen=True, slots=True)
    class MemoryReceipt:
        id: str; action: str; target: MemoryTarget; revision: int; entry: MemoryEntry | None = None
    class MemoryService:
        def __init__(self, *, store: MemoryStore) -> None
        def entries(self, target: MemoryTarget) -> list[MemoryEntry]
        def snapshot(self, target: MemoryTarget) -> MemorySnapshot
        def remember(self, *, target: MemoryTarget, content: str, importance: MemoryImportance = 'normal') -> MemoryReceipt
        def replace(self, *, target: MemoryTarget, entry_id: str, content: str, importance: MemoryImportance | None = None) -> MemoryReceipt
        def forget(self, *, target: MemoryTarget, entry_id: str) -> MemoryReceipt
        def status(self) -> dict[str, Any]  # 只读计数、revision、路径

存储：deeppyro_home()/memory/memories.json（形状照抄 ~/.mnemon/runtime/memories.json，追加 id 与 revision）；USER.md/MEMORY.md 由 store 在每次写后原子重建（一条一行、条目间空行或 § 分隔；与宿主投影保持同样可读性）。容量建议「需 Lead 决策」：每个文件最多 200 条或 16384 字节，超限拒绝写入并返回明确错误（DSH 有类似上下限，但数值未在本机文档中给出）。

注入：选项 A（推荐 P1）prompt section 名称 memory-runtime，order=12（core identity=10 与 environment=20 之间），renderer 渲染 MNEMON RUNTIME MEMORY SNAPSHOT 头 + USER.md/MEMORY.md 两个 <runtime-memory-file name=...> 块；空文件不输出。所有变更下一轮生效，无需 session 事件。
选项 B（DSH 同构，后续）：agent/pre-step 注入 user/message source {kind:'dsh-mnemon', form:'memory-snapshot'}，旧快照用 developer/message replace 退休。与 skills 选项 B 同样需要 agent/pre-step payload 或 AgentSession 公开属性；且 retirement 需要插件能 append session 记录，「需 Lead 决策」。

工具契约（P1 只暴露最小写工具）：
- mnemon_runtime_memory：action enum add/replace/remove 必填；target enum user/memory 必填；content?（add/replace 必填）；old_text?（replace/remove 用唯一匹配）；importance? enum critical/normal/low。返回 JSON receipt。
- 描述必须写明：只保存用户提供或纠正的持久事实；不保存问题、猜测、临时进度、已完成工作日志、密钥；记忆是数据不是指令；add/replace/remove 语义（不重复、不覆盖无关条目）。
- 非目标：mnemon_recall/related/remember/link/forget/document_*/view_route/view_action/memory_body_* 全部延后到 Phase 2；不在 P1 暴露空实现工具，避免模型误用。

安全：只写 deeppyro_home() 下文件，不触碰宿主 ~/.mnemon；不对用户隐藏文件做扫描；工具输出不回显密钥；与 sandbox 无交互（纯本地文本）。

### 4.4 测试策略

- test_store.py：空目录初始化、add/replace/remove round-trip、投影文本格式、原子写无临时文件、坏 JSON 拒绝、超容量拒绝。
- test_service.py：add 后 next page 有序、replace 唯一匹配/多匹配报错、forget 不存在报错、target 隔离、revision 单调。
- test_tools.py：FakeToolContext.services={memory}；三个 action 正常路径与参数错误；输出 JSON 可解析；prompt section renderer 空/非空文本。

## 5. 接线点汇总（Lead 与对应 owner 必读）

| 位置 | 改动 | 归属 |
|---|---|---|
| src/deeppyro/bundles/core/bundle.toml | 追加 6 行（见下）；建议 critical=false | Lead |
| src/deeppyro/tui/runtime.py | _plugin_services 增加 skills/schedule/memory；boot 后 await schedule.start() 并 configure_host(TuiScheduleHost)；close 时 stop | tui owner |
| src/deeppyro/headless.py | 同上，用 HeadlessScheduleHost | headless owner |
| src/deeppyro/agent/session.py | 新增 followup_message / 只读 session、cwd 属性（若选选项 B 还需 pre-step payload 增加 cwd/sessionId） | agent owner |
| src/deeppyro/api/types.py | CHILD_DISABLED_TOOLS 增加 schedule_* 工具名（子会话不可用） | Lead |
| src/deeppyro/paths.py | 可选新增 memory_dir()/schedules_path()/skills_dir() 便于测试；不加强制 | Lead |
| docs/spec/INTERFACES.md | §11 core bundle 服务表追加 skills/schedule/memory 与工具 | Lead |

建议 bundle 行（追加在 inspect 之后；位置可调，但 skills 必须在 tools/prompt 之后、skill-tools 之前，schedule 在 sessions 之后，memory 在 prompt 之后）：

    [[plugins]]
    id = "skills"
    plugin = "deeppyro.builtin.skills:SkillsPlugin"
    critical = false

    [[plugins]]
    id = "skill-tools"
    plugin = "deeppyro.builtin.skill_tools:SkillToolsPlugin"
    critical = false

    [[plugins]]
    id = "schedule"
    plugin = "deeppyro.builtin.schedule:SchedulePlugin"
    critical = false

    [[plugins]]
    id = "schedule-tools"
    plugin = "deeppyro.tools.builtin.schedule:ScheduleToolsPlugin"
    critical = false

    [[plugins]]
    id = "memory"
    plugin = "deeppyro.builtin.memory:MemoryPlugin"
    critical = false

    [[plugins]]
    id = "memory-tools"
    plugin = "deeppyro.tools.builtin.memory:MemoryToolsPlugin"
    critical = false

或用 ctx.register_tool/register_prompt_section 把工具并入服务插件以减少行数；建议保留独立行，便于用户按需禁用工具暴露。

prompt section order 建议：memory-runtime=12、skills 目录=35、tool:schedule=45；core 现有 identity=10/environment=20/instructions=30/tools=40/rules=50 不变，按 (order,name) 排序。

## 6. 建议 shared task 拆分

| 任务 | 标题 | 依赖 | 写域 | 预估 |
|---|---|---|---|---|
| T1 | skills 核心：类型、frontmatter、发现、注册表 | 无 | src/deeppyro/skills/**、tests/skills/** | 500-700 行 |
| T2 | skill 工具 + 技能目录 prompt section + 插件行 | T1 | src/deeppyro/builtin/skills.py、builtin/skill_tools.py、tools/builtin/skill.py、tests/skills/test_tools.py | 350-450 行 |
| T3 | schedule 时间基础：types/timeutil/rrule/cron | 依赖决策（python-dateutil） | src/deeppyro/schedule/{__init__,types,errors,timeutil,rrule,cron}.py、tests/schedule/test_time*.py | 700-1000 行 |
| T4 | schedule 存储 + 服务 + 引擎 + 插件 | T3 | src/deeppyro/schedule/{store,engine,service}.py、builtin/schedule.py、tests/schedule/test_service.py | 700-900 行 |
| T5 | schedule 四工具 + 授权 prompt section | T4 | src/deeppyro/tools/builtin/schedule.py、tests/schedule/test_tools.py | 350-450 行 |
| T6 | memory 存储 + 服务 + 插件 + 注入 | 无（可与 T1 并行） | src/deeppyro/memory/**、src/deeppyro/builtin/memory.py、tests/memory/** | 400-550 行 |
| T7 | memory 工具 + 状态工具 | T6 | src/deeppyro/tools/builtin/memory.py、tests/memory/test_tools.py | 250-350 行 |
| T8 | 宿主接线：bundle.toml、TUI/headless services、ScheduleHost、agent 投递 API、子会话工具过滤 | T2/T5/T7 | bundles/core/bundle.toml、tui/runtime.py、headless.py、agent/session.py、api/types.py、tests/tui+integration | 400-600 行 |

建议按 T1+T6 并行先行（互不依赖），T2/T7 随后；T3 的依赖决策必须在 T3 开始前由 Lead 拍板；T4/T5 串行；T8 最后统一接线并在 TUI 手工验收。每个任务完成标准沿用仓库惯例：scoped pytest 全绿 + scoped ruff check/format 干净 + docs/spec/<area>.md 更新。

## 7. 风险与非目标

风险：
1. RRULE 是最大技术风险：DSH 在 rrule 2.8.1 之上还有约 600 行严格校验与 DST 语义；不允许 python-dateutil 时 P1 应缩小为 after/at/every/daily/weekly。
2. schedule 宿主投递是最大集成风险：DeepPyro 的 TUI/headless 都是单 agent 单会话，新增「按 session_id 找/建 agent 并投递带 source 的消息」需要改 agent 与 runtime；若只做 session 目标可显著降风险。
3. skills 提示词注入：项目内技能文件等价用户可写指令，P1 不做信任分级；文档必须提示只扫描受信目录。
4. memory 与宿主 dsh-mnemon 文件格式相同但目录不同（~/.dpro/memory vs ~/.mnemon），不要互相读写，避免版本漂移。
5. 无跨进程锁：schedules.json、memories.json 都是单进程假设；与插件清单、config 写同样风险，TUI 单实例可接受，headless 并发运行需文档警告。
6. bundle critical=true 会让新插件加载失败拖垮启动；新行显式 critical=false。

非目标（P1）：
- skills 的 provider 注册表/scope 分层/watch/cache/显式 /skill 手势/office 内置技能包。
- schedule 的 cron、new 目标并发限速、跨重启 pendingDelivery 恢复、管理通知广播、Remote API（DeepPyro 无 Remote 层）。
- memory 的 View/Strategy/Source 协议、Documents 检索、Memory Spaces/Provider、自进化技能、Git 同步、分支过滤。

## 8. Lead 必读结论

1. skills 可以先做只读发现 + skill 工具 + prompt section 目录，全部在插件层完成，不动 agent loop；DSH 原生的 durable skill-catalog 消息需要 agent/pre-step payload 增加 cwd/session 句柄，建议放 Phase 2。
2. frontmatter 不要默认加 PyYAML：实际 SKILL.md 只有 name/description，自写 30 行解析器足够；若 Lead 选择完整 YAML 兼容，需要改 pyproject.toml（本任务禁止，但下游任务需授权）。
3. schedule 建议引入 python-dateutil 作为唯一新依赖（需 Lead 改 pyproject），在适配层严格复刻 DSH 的 RFC5545 子集；否则 P1 只做 5 种简单 timing + 一次性 COUNT=1。
4. schedule 的值不在于工具而在于宿主投递：必须新增 ScheduleHost、AgentSession.followup_message（带 source）以及 TUI/headless 的 schedule.start/stop 接线；没有 T8 就只有能创建却不会触发的任务。
5. schedule 持久化建议 ~/.dpro/schedules.json（version 1，原子写，单进程假设）；工具必须带 MANAGEMENT_AUTHORIZATION、拒绝子会话（CHILD_DISABLED_TOOLS + host 深度双保险）。
6. memory 不要按 dsh-mnemon 全量实现：P1 只做 runtime memory（memories.json + USER.md/MEMORY.md 投影）+ mnemon_runtime_memory 工具 + prompt section 注入；Documents 与 Memory Spaces 留 Phase 2 接口。
7. 三项新功能都通过 bundle 行接入且 critical=false；需要 Lead 在 T8 统一改 bundle.toml、TUI/headless、agent/session.py、api/types.py，并把新服务加入 _plugin_services 的 services dict，否则插件拿不到服务。
8. 建议先 T1+T6（互不依赖）并行，再 T3（依赖决策后），T8 最后；每个任务保持 scoped pytest + scoped ruff 的完成标准，避免并发大改动互相覆盖。

## 9. 附：调研中确认的 Python 现状（便于实施）

- Plugin 契约：src/deeppyro/api/plugin.py；Service 子类 provide 即注册服务；inject 为 list[str]；PluginContext 提供 register_tool(factory, scope=)、register_prompt_section(name, order, renderer, scope=)、register_command、register_provider、register_preset，均随 fiber 自动注销（src/deeppyro/api/context.py）。
- 工具：ToolPlugin.add(factory) 与 ctx.register_tool 都经 ToolsPlugin 目录；registry_for(workspace, scope, revision) 支持 preset scope；BaseTool 自带 name/description/parameters/parallel_safe（src/deeppyro/tools/base.py、builtin/tools.py）。
- ToolContext（src/deeppyro/core/protocols.py）：cwd、session_id、turn、step、call_id、cancel、log、permission、services dict；工具从 ctx.services 解析服务，团队工具即此模式（src/deeppyro/tools/builtin/team.py:34-38）。
- hooks：src/deeppyro/hooks/types.py 固定 9 个 hook，无通用自定义事件；因此 skills/memory 的 durable 注入必须复用 agent/pre-step（payload 见 src/deeppyro/agent/session.py:544-563），否则只能 prompt section。
- session：Session.append 支持 surfaceOp='append' 与 replace 对象、sourceEventSeqs；user/message 的 data 是 message 本身；developer/message 必须 role=developer，可用于退休空占位（src/deeppyro/session/events.py:287-358；docs/spec/session.md:162/165）。
- 插件清单/配置：kernel.manifest 与 plugins.manifest_store 已有 load_manifest/dump_manifest/save_user_manifest 与原子写；配置保存用 config.save_agent_selection 的 tmp+os.replace 模式（src/deeppyro/config.py）。
- 测试基线：pytest asyncio_mode=auto，addopts=-q，测试目录均带 __init__.py；tmp_path + monkeypatch 是标准手法；ruff line-length=110、target py312、_vendor 排除。
