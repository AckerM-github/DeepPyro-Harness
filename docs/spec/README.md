# 规格文档索引（docs/spec）

> **状态**：0.2.0 现行索引 · **最后核对**：2026-11-18 · **覆盖**：38 篇规格 + 本索引
>
> 本索引是 `docs/spec/` 的唯一状态表。总文档入口见 [docs/README.md](../README.md)；
> 发布范围与遗留项见 [0.2.0-known-issues.md](../release/0.2.0-known-issues.md)。
> 权威契约：native schema 1 + ADR。与 ADR 冲突时以 ADR 为准；与 schema 冲突时以 schema 为准。

本目录同时存在三类文档：**native schema 1 的当前契约**、**headless 已默认 native / TUI 仍走 v4 的现行与过渡规格**、
**只用于迁移或历史追溯的冻结材料**。读任何一篇之前先看标题下的状态块，再决定能否依赖其中的字段、
类型和事件名。

## 1. 状态图例

| 状态 | 含义 | 能否作为新代码依据 |
| --- | --- | --- |
| `当前契约` | native schema 1 或经 ADR 冻结的现行契约 | ✅ 可以 |
| `现行（0.2.0 保留）` | 已实现、与 native 方向一致，0.2.0 继续有效 | ✅ 可以（以代码为准） |
| `现行（过渡：v4 待移除）` | 已实现，但仍描述 v4 wire / DSH 形状或计划在 0.3 替换的机制 | ⚠️ 只可用于理解现状，不得据此扩展 |
| `现行（native 默认）` | headless 默认 native，`--legacy-v4` 回退；TUI 交互仍 v4 | ⚠️ 两条路径行为不同 |
| `现行（native opt-in）` | 0.2.0 仅 TUI 交互路径：默认 v4，显式开关走 native | ⚠️ 默认路径与 native 路径行为不同 |
| `冻结：迁移参考` | 描述旧 DSH v4 数据形状，仅供读旧数据/写迁移说明 | ❌ 不行 |
| `历史证据（0.2.0 保留，0.3 删除）` | DSH 逆向产物/事件目录，不是运行期契约 | ❌ 不行 |
| `历史文档（非契约）` | 已移入 [docs/archive/](../archive/) 的计划/研究 | ❌ 不行 |

括号内是范围/里程碑限定（如 `API v3`、`M3 已调整`、`M4 已扩展`、`native 默认`），不改变基础状态；`goal.md` 的 `现行契约（goal 域冻结）` 表示该域按 FROZEN 规则维护。

## 2. 唯一契约入口

| 文档 | 性质 | 状态 | 说明 |
| --- | --- | --- | --- |
| [native/schema.md](../native/schema.md) | native session schema 1（header/envelope/27 EventKind/Message/fold/trace/fork/校验） | `当前契约` | 取代 `spec/session.md`；native loop 只写、只读 schema 1 |
| [ADR 0001](../adr/0001-three-track-native-agent.md) | 三线（插件化/Python 化/AI 原生）决策记录 | `当前契约` | M0–M5 完成；M0 决策冻结；D2 零 wire 兼容、D3 保留/删除清单、D5 里程碑门禁 |
| [native/README.md](../native/README.md) | M0–M4 交付、录制 provenance 与门禁账本 | `当前契约`（状态报告，非规范正文） | 记录实现时点与测试数字；不定义 schema |
| [spec/INTERFACES.md](INTERFACES.md) | 旧总接口文档（DSH 形状的 v4/camelCase 契约） | `冻结：迁移参考` | 冻结横幅；被 ADR §0 与 native schema 取代；0.3 删除 |
| [spec/session.md](session.md) | 旧 `session.v4` 存储/派生/fork 规范 | `冻结：迁移参考` | 仅供迁移参考；被 native schema 取代；0.3 删除 |

> 新代码唯一可依赖的会话契约是 [native/schema.md](../native/schema.md)。`INTERFACES.md` 与 `session.md` 里的
> `surfaceOp`、`sourceEventSeqs`、`ignorable`、camelCase header、zstd 等均为**已删除机制**（ADR §7、schema §1.3）。

## 3. 现行功能规格（0.2.0 保留）

下表文档描述已实现行为，与 native 方向一致，0.2.0 保留。凡涉及 0.3 才会替换的 v4 机制，已在“备注”列标注。

| 文档 | 领域 | 状态 | 备注 |
| --- | --- | --- | --- |
| [tools.md](tools.md) | 工具契约 | `现行（0.2.0 保留）` | 工具 schema 快照逐字节稳定（ADR §4）；公开插件契约见 [plugin-core.md](plugin-core.md) |
| [llm.md](llm.md) | LLM 流式契约 | `现行（0.2.0 保留）` | 2 个 transport；native 请求消息由 fold 派生 |
| [sandbox.md](sandbox.md) | 沙箱/权限执行 | `现行（0.2.0 保留）` | Windows ACL runner；`enforcement=partial` 的限制保留 |
| [activity.md](activity.md) | 后台活动 | `现行（0.2.0 保留）` | |
| [agent-presets.md](agent-presets.md) | preset 组合 | `现行（M3 已调整）` | preset v3 形状已随 API v3 落地 |
| [attachments.md](attachments.md) | 附件/图片 | `现行（0.2.0 保留）` | |
| [compact-blocks.md](compact-blocks.md) | 压缩块 | `现行（0.2.0 保留）` | 与 native `compaction.done.covers` 对齐（ADR §7-4） |
| [frozen-distribution.md](frozen-distribution.md) | PyInstaller 打包 | `现行（0.2.0 保留）` | core bundle 字符串行对静态分析不可见 |
| [goal.md](goal.md) | goal 域 | `现行契约（goal 域冻结）` | FROZEN M0 2026-11-16；goal 域唯一契约 |
| [headless.md](headless.md) | headless CLI | `现行（native 默认）` | 参数/退出码/NDJSON 形状保持；默认走 `AgentLoop`，`--legacy-v4` 回退，`--native-loop` 为弃用别名 |
| [im-plugin.md](im-plugin.md) | IM 插件 | `现行（0.2.0 保留）` | 实现在仓库外 `dpyro-im` |
| [image-budget.md](image-budget.md) | 图片预算 | `现行（0.2.0 保留）` | |
| [image-offload.md](image-offload.md) | 图片卸载 | `现行（0.2.0 保留）` | |
| [image-request.md](image-request.md) | 图片请求 | `现行（0.2.0 保留）` | |
| [mcp.md](mcp.md) | MCP client | `现行（0.2.0 保留）` | 对齐 `dsh-mcp-client` 资源语义 |
| [memory.md](memory.md) | runtime memory | `现行（M4 已扩展）` | SQLite FTS5 backend 已落地；`memory_search`/`memory_add` 按 preset 只读/可写注册，无 embedding |
| [plugin-core.md](plugin-core.md) | 插件/组合契约 | `现行（API v3）` | 当前 `API_VERSION = 3`；v1/v2 硬切 |
| [plugin-platform.md](plugin-platform.md) | 插件安装/打包 | `现行（API v3）` | `plugin.toml` 安装单元 + entry points/bundle v3 |
| [plugin-settings.md](plugin-settings.md) | settings CAS | `现行（0.2.0 保留）` | |
| [plugins.md](plugins.md) | 插件管理/自扩展 | `现行（0.2.0 保留）` | |
| [ptc.md](ptc.md) | PTC | `现行（0.2.0 保留）` | 对齐默认 native |
| [schedule.md](schedule.md) | 定时任务 | `现行（0.2.0 保留）` | P1 |
| [skills.md](skills.md) | skills | `现行（0.2.0 保留）` | P1 只读 |
| [subagent.md](subagent.md) | 子 agent | `现行（M4 已扩展）` | supervisor + 2 workers 已落地；结果与 supervisor 汇总携带 usage |
| [tui.md](tui.md) | TUI 总览 | `现行（native opt-in）` | 交互 TUI 仍走 v4（默认），不读取 native 会话；native 仅 headless `run_once` |
| [tui-slots.md](tui-slots.md) | TUI slots | `现行（0.2.0 保留）` | layer E 已实现 |
| [tui-contributions.md](tui-contributions.md) | TUI 贡献 | `现行（0.2.0 保留）` | P2 |
| [tui-input.md](tui-input.md) | TUI 输入 | `现行（0.2.0 保留）` | |
| [tui-widgets.md](tui-widgets.md) | TUI widgets | `现行（0.2.0 保留）` | |

## 4. 过渡规格（headless 默认 native，TUI 仍 v4）

| 文档 | 状态 | 现状 | 0.3 计划 |
| --- | --- | --- | --- |
| [loop.md](loop.md) | `现行（过渡：v4 待移除）` | headless 已默认 `AgentLoop`；v4 loop 仍服务 TUI 交互路径，`--legacy-v4` 可回退 | v4 退出后重写为 `AgentLoop` 文档 |
| [events.md](events.md) | `现行（过渡：v4 待移除）` | v4 仍走 HookService；native loop 使用 27 个 `EventKind` 与 typed events | 切换后重写 |
| [hooks.md](hooks.md) | `现行（过渡：v4 待移除）` | P1 bus 与 19 个 well-known hook 名仍用于 v4；native loop 用 typed `AgentHooks`/`events.py` | 切换后统一，删除 cordis 语义描述 |
| [projection.md](projection.md) | `现行（过渡：v4 待移除）` | v4 的 projection/checkpoint 仍服务 TUI 会话；native 使用 `session/fold.py` 的纯 fold | v4 退出后重写或移植 native fold |
| [session-query.md](session-query.md) | `现行（过渡：v4 待移除）` | v4 的只读 summary/surface/event/trace 查询仍服务 TUI 会话；native 尚无等价 query 层 | 随 v4 重写或移植 |

## 5. 冻结与历史证据（0.2.0 保留，0.3 删除）

| 文档 | 状态 | 现状 | 计划 |
| --- | --- | --- | --- |
| [INTERFACES.md](INTERFACES.md) | `冻结：迁移参考` | M0 冻结；记录 v4 记录形状与 camelCase 字段 | 0.3 随 v4 删除 |
| [session.md](session.md) | `冻结：迁移参考` | v4 storage/derive/fork 仅供迁移参考 | 0.3 随 v4 删除 |
| [dsh-event-catalog.md](dsh-event-catalog.md) | `历史证据（0.2.0 保留，0.3 删除）` | 由 `scripts/extract_dsh_events.mjs` 生成的 DSH 事件目录（M0 artifact；非运行期契约） | 0.3 随 v4 删除 |
| [dsh-event-catalog-unresolved.md](dsh-event-catalog-unresolved.md) | `历史证据（0.2.0 保留，0.3 删除）` | 从 DSH asar 提取的未解析证据（bus=94 internal=19 record=61 noise=2328） | 同上 |

## 6. 历史研究归档（docs/archive，非契约）

2026-11-18 文档整理时，把 6 篇仍被现行规格或源码引用的计划/研究移入 [docs/archive/](../archive/)：

- [goal-audit.md](../archive/goal/goal-audit.md)：goal 升级线差异审计；
- [p1-context.md](../archive/research/p1-context.md)、[p1-ext.md](../archive/research/p1-ext.md)、
  [p1-providers.md](../archive/research/p1-providers.md)、[p1-tools-runtime.md](../archive/research/p1-tools-runtime.md)、
  [p2-bus.md](../archive/research/p2-bus.md)：P1/P2 研究。

另有 14 篇已完成计划/任务卡已从 `docs/plan/` 删除（9 张 goal-tasks 卡、goal-upgrade、p0-bus-alignment、
p1-bus、preset-modes、sandbox-alignment）。删除清单与追溯方式见 [docs/archive/README.md §4](../archive/README.md)。

## 7. 维护规则

1. **新文档先定位**：会话/消息/事件形状只写进 `docs/native/`；功能规格写 `docs/spec/`；插件作者文档写 `docs/plugins/`；发布/迁移写 `docs/release/`；运维与数据边界写 `docs/ops/`；已完成研究放 `docs/archive/`。
2. **每份规格必须带状态头**：至少写明“状态、层级、最后核对”。没有状态头的文档视为未评审。
3. **冻结流程**：被取代的文档在标题下加冻结横幅，并在本索引 §5 登记；v4 运行时移除后再按 §5 计划删除。
4. **删除前先改索引**：删除任何文件前先把本索引对应行改为“已删除/归档位置”，避免出现死链。
5. **与 release note 同步**：破坏性变更（v4 不再支持、config snake_case、插件 API v3、DSH 路径退役）以 [0.2.0.md](../release/0.2.0.md) 为准，本索引只记录文档状态。

## 8. 目录统计（2026-11-18 整理后）

| 目录 | 文件数 | 内容 |
| --- | --- | --- |
| `docs/spec/` | 39 | 38 篇规格 + 本索引（29 现行 + 5 过渡 + 4 冻结/历史） |
| `docs/native/` | 3 | `schema.md`（契约正文）、`README.md`（实现与录制状态）、`coverage.md`（差分覆盖与重录） |
| `docs/adr/` | 1 | 三线 ADR |
| `docs/archive/` | 7 | 6 篇历史文档 + 归档索引 |
| `docs/guides/` | 4 | native 默认化、会话迁移、bundle digest、Windows 沙箱边界 |
| `docs/` 总计 | 61 | spec/native/adr/guides/plugins/release/ops/archive + 文档地图 |
