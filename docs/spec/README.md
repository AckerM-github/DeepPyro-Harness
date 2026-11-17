# 规格文档索引（docs/spec）

| 项目 | 值 |
| --- | --- |
| 文档 | `docs/spec/README.md` |
| 状态 | M5 文档线重写，集成线更新（2026-11-17，集成 M1–M4）；本索引是 `docs/spec/` 的唯一状态表 |
| 权威契约 | `docs/native/schema.md`（native session schema 1）+ `docs/adr/0001-three-track-native-agent.md`（ADR） |
| 冲突处理 | 与 ADR 冲突时以 ADR 为准；与 `docs/native/schema.md` 冲突时以 schema 为准 |

本目录同时存在两类文档：**native schema 1 的当前契约**（0.2.0 的目标产品契约）与 **legacy DSH v4 规格**
（DSH `session.v4` 形状的历史记录/迁移参考；默认运行时仍是 v4，待 0.3 移除）。读者必须先确认一份文档的状态，
再决定是否把其中的字段、类型、事件名当作可依赖的契约。

## 1. 状态图例

| 状态 | 含义 | 能否作为新代码依据 |
| --- | --- | --- |
| `当前契约` | native schema 1 或经 ADR 冻结的现行契约 | ✅ 可以 |
| `现行（0.2.0 保留）` | 已实现、与 native 方向一致，0.2.0 继续有效 | ✅ 可以（以代码为准） |
| `现行（过渡：v4 待移除）` | 已实现，但仍描述 v4 wire / DSH 形状或计划在 0.3 替换的机制 | ⚠️ 只可用于理解现状，不得据此扩展 |
| `冻结：迁移参考` | 描述旧 DSH v4 数据形状，仅供读旧数据/写迁移说明 | ❌ 不行 |
| `历史证据：待删除` | DSH 逆向产物/事件目录，不是运行期契约 | ❌ 不行 |
| `历史计划（非契约）` | `docs/plan/` 下的计划卡片与复盘 | ❌ 不行 |

## 2. 唯一契约入口

| 文档 | 性质 | 状态 | 说明 |
| --- | --- | --- | --- |
| `docs/native/schema.md` | native session schema 1（header/envelope/27 EventKind/Message/fold/trace/fork/校验） | `当前契约` | 取代 `docs/spec/session.md`；native loop 只写、只读 schema 1 |
| `docs/adr/0001-three-track-native-agent.md` | 三线（插件化/Python 化/AI 原生）决策记录 | `当前契约` | M0 决策冻结；D2 零 wire 兼容、D3 保留/删除清单、D5 里程碑门禁 |
| `docs/native/README.md` | M1–M2 交付与状态账本（10 例录制、门禁数字） | `当前契约`（状态报告，非规范正文） | 记录 M1/M2 完成时点与 provenance |
| `docs/spec/INTERFACES.md` | 旧总接口文档（DSH 形状的 v4/camelCase 契约） | `冻结：迁移参考` | M0 冻结横幅；被 ADR §0 与 `docs/native/schema.md` 取代；0.2.0 未归档，计划 0.3 |
| `docs/spec/session.md` | 旧 `session.v4` 存储/派生/fork 规范 | `冻结：迁移参考` | 仅供迁移参考；被 `docs/native/schema.md` 取代 |

> 新代码唯一可依赖的会话契约是 `docs/native/schema.md`。`INTERFACES.md` 与 `session.md` 里的
> `surfaceOp`、`sourceEventSeqs`、`ignorable`、camelCase header、zstd 等均为**已删除机制**（ADR §7、schema §1.3）。

## 3. legacy DSH v4 规格（历史/待删除）

| 文档 | 状态 | 现状 | 计划 |
| --- | --- | --- | --- |
| `docs/spec/INTERFACES.md` | `冻结：迁移参考` | M0 冻结；记录 v4 记录形状与 camelCase 字段 | 0.2.0 未归档；0.3 随 v4 删除（见 KI-033） |
| `docs/spec/session.md` | `冻结：迁移参考` | v4 storage/derive/fork 仅供迁移参考 | 0.2.0 未归档；0.3 随 v4 删除（见 KI-033） |
| `docs/spec/dsh-event-catalog.md` | `历史证据：待删除` | 由 `scripts/extract_dsh_events.mjs` 生成的 DSH 事件目录（M0 artifact；非运行期契约） | 0.2.0 未删除；0.3 随 v4 删除（见 KI-033） |
| `docs/spec/dsh-event-catalog-unresolved.md` | `历史证据：待删除` | 从 DSH asar 提取的未解析证据（bus=94 internal=19 record=61 noise=2328） | 同上 |
| `docs/spec/loop.md` | `现行（过渡：M2 已交付）` | v4 loop 仍为默认；native `AgentLoop` 已实现并通过 10 例差分，走 `--native-loop` | 0.3 切换默认后重写为 `AgentLoop` 文档 |
| `docs/spec/hooks.md` | `现行（过渡：M2 部分替换）` | P1 bus 与 19 个 well-known hook 名仍用于 v4；native loop 用 typed `AgentHooks`/`events.py` | 0.3 切换后统一 |
| `docs/spec/events.md` | `现行（过渡：M2 已引入）` | v4 仍走 HookService；native loop 使用 27 个 `EventKind` 与 typed events | 0.3 切换后重写 |

## 4. 现行功能规格（0.2.0 保留）

下表文档描述已实现行为，与 native 方向一致，0.2.0 保留。凡涉及 0.3 才会替换的 v4 机制，已在“备注”列标注。

| 文档 | 领域 | 状态 | 备注 |
| --- | --- | --- | --- |
| `tools.md` | 工具契约 | `现行（0.2.0 保留）` | 工具 schema 快照逐字节稳定（ADR §4） |
| `llm.md` | LLM 流式契约 | `现行（0.2.0 保留）` | 2 个 transport；native 请求消息由 fold 派生 |
| `sandbox.md` | 沙箱/权限执行 | `现行（0.2.0 保留）` | Windows ACL runner；`enforcement=partial` 的限制保留 |
| `activity.md` | 后台活动 | `现行（0.2.0 保留）` | |
| `agent-presets.md` | preset 组合 | `现行（M3 已调整）` | preset v3 形状已随 API v3 落地 |
| `attachments.md` | 附件/图片 | `现行（0.2.0 保留）` | |
| `compact-blocks.md` | 压缩块 | `现行（0.2.0 保留）` | 与 native `compaction.done.covers` 对齐（ADR §7-4） |
| `frozen-distribution.md` | PyInstaller 打包 | `现行（0.2.0 保留）` | core bundle 字符串行对静态分析不可见 |
| `goal.md` | goal 域 | `现行契约（goal 域冻结）` | FROZEN M0 2026-11-16；goal 域唯一契约 |
| `headless.md` | headless CLI | `现行（native opt-in）` | 参数/退出码/NDJSON 形状保持；`--native-loop` 走 `AgentLoop`，默认仍 v4 |
| `im-plugin.md` | IM 插件 | `现行（0.2.0 保留）` | 实现在仓库外 `dpyro-im` |
| `image-budget.md` | 图片预算 | `现行（0.2.0 保留）` | |
| `image-offload.md` | 图片卸载 | `现行（0.2.0 保留）` | |
| `image-request.md` | 图片请求 | `现行（0.2.0 保留）` | |
| `mcp.md` | MCP client | `现行（0.2.0 保留）` | 对齐 `dsh-mcp-client` 资源语义 |
| `memory.md` | runtime memory | `现行（M4 已扩展）` | SQLite FTS5 backend 已落地；未暴露为工具、未自动写入 |
| `plugin-core.md` | 插件/组合契约 | `现行（M3 已重写为 API v3）` | 当前 `API_VERSION = 3`；v1/v2 硬切 |
| `plugin-platform.md` | 插件安装/打包 | `现行（M3 已调整）` | `plugin.toml` 安装单元 + entry points/bundle v3 |
| `plugin-settings.md` | settings CAS | `现行（0.2.0 保留）` | |
| `plugins.md` | 插件管理/自扩展 | `现行（0.2.0 保留）` | |
| `projection.md` | projection | `现行（0.2.0 保留）` | |
| `ptc.md` | PTC | `现行（0.2.0 保留）` | 对齐默认 native |
| `schedule.md` | 定时任务 | `现行（0.2.0 保留）` | P1 |
| `session-query.md` | 会话查询 | `现行（0.2.0 保留）` | |
| `skills.md` | skills | `现行（0.2.0 保留）` | P1 只读 |
| `subagent.md` | 子 agent | `现行（M4 已扩展）` | supervisor + 2 workers 已落地；未接真实 subagent 生命周期 |
| `tui.md` | TUI 总览 | `现行（0.2.0 保留）` | |
| `tui-slots.md` | TUI slots | `现行（0.2.0 保留）` | layer E 已实现 |
| `tui-contributions.md` | TUI 贡献 | `现行（0.2.0 保留）` | P2 |
| `tui-input.md` | TUI 输入 | `现行（0.2.0 保留）` | |
| `tui-widgets.md` | TUI widgets | `现行（0.2.0 保留）` | |

## 5. 历史计划文档（docs/plan，非契约）

- `docs/plan/goal-upgrade.md`、`docs/plan/goal-audit.md`、`docs/plan/goal-tasks/M0–M6.md`：goal 升级计划的实施卡片与复盘（已完成；**不是**本 ADR 的 M0–M5 里程碑）。
- `docs/plan/p0-bus-alignment.md`、`docs/plan/p1-*.md`、`docs/plan/p2-bus.md`、`docs/plan/preset-modes.md`、`docs/plan/sandbox-alignment.md`：P0–P2 阶段计划，历史记录，随对应实现归档。
- 注意：`docs/plan/goal-tasks/M5.md` 是旧 goal 升级线的 M5（projection/events/README/loop.md），**不是** 0.2.0 的 M5 发布里程碑（后者见 `docs/adr/0001-three-track-native-agent.md` §D5 与 `docs/release/0.2.0.md`）。

## 6. 维护规则

1. **新文档先定位**：会话/消息/事件形状只写进 `docs/native/`；功能规格写 `docs/spec/`；插件作者文档写 `docs/plugins/`；发布/迁移写 `docs/release/`；运维与数据边界写 `docs/ops/`。
2. **每份规格必须带状态头**：至少写明“状态、与 native schema 1 的关系、owner、取代/被取代关系”。没有状态头的文档视为未评审。
3. **冻结流程**：被取代的文档在标题下加冻结横幅，并在本索引 §3 登记；v4 运行时移除后再按 §3 计划删除。
4. **删除前先改索引**：删除任何文件前先把本索引对应行改为“已删除/归档位置”，避免出现死链。
5. **与 release note 同步**：破坏性变更（v4 不再支持、config snake_case、插件 API v3、DSH 路径退役）以 `docs/release/0.2.0.md` 为准，本索引只记录文档状态。

## 7. 目录统计（集成线，M1–M5）

| 目录 | 文件数 | 内容 |
| --- | --- | --- |
| `docs/spec/` | 38 | 现行功能规格 + legacy DSH v4 规格（本索引覆盖全部） |
| `docs/native/` | 2 | `schema.md`（契约正文）、`README.md`（M1–M2 状态） |
| `docs/adr/` | 1 | 三线 ADR |
| `docs/plan/` | 20 | 历史计划卡片与复盘（非契约） |

