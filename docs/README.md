# DeepPyro 文档地图（0.2.0）

> **状态**：现行文档入口 · **版本**：0.2.0 release candidate · **最后核对**：2026-11-18
>
> 本文件回答三个问题：先读什么、每类文档的权威性是什么、过时文档去哪里。具体规格状态以
> [spec/README.md](spec/README.md) 为准；发布范围与遗留项以
> [release/0.2.0-known-issues.md](release/0.2.0-known-issues.md) 为准。

## 1. 先读这五份

| 顺序 | 文档 | 读它来了解 |
| --- | --- | --- |
| 1 | [README.md](../README.md) | 安装、启动、当前能力与 0.2.0 定位 |
| 2 | [release/0.2.0.md](release/0.2.0.md) | 破坏性变更、迁移步骤、验收门禁 |
| 3 | [release/0.2.0-known-issues.md](release/0.2.0-known-issues.md) | 所有已知问题、设计限制与 0.3 候选 |
| 4 | [adr/0001-three-track-native-agent.md](adr/0001-three-track-native-agent.md) | 为什么做 native 契约、三条主线与里程碑 |
| 5 | [native/schema.md](native/schema.md) | native session schema 1 的唯一契约正文 |

插件作者从 [plugins/README.md](plugins/README.md) 开始；迁移到 API v3 看
[plugins/migration-v2-v3.md](plugins/migration-v2-v3.md)。需要理解旧 v4 数据形状时，才读
[spec/INTERFACES.md](spec/INTERFACES.md) 与 [spec/session.md](spec/session.md)。

## 2. 文档分层与权威性

| 层 | 目录 | 性质 | 冲突时 |
| --- | --- | --- | --- |
| 契约 | [native/schema.md](native/schema.md)、[adr/](adr/) | 冻结的 native schema 与架构决策 | schema / ADR 优先 |
| 现行规格 | [spec/](spec/) | 已实现行为的功能规格 | 以代码与测试为准；与契约冲突时以契约为准 |
| 过渡规格 | [spec/loop.md](spec/loop.md)、[spec/events.md](spec/events.md)、[spec/hooks.md](spec/hooks.md) | headless 默认 native、TUI 仍走 v4 的并存现状 | 只用于理解现状，不得据此扩展 |
| 冻结迁移 | [spec/INTERFACES.md](spec/INTERFACES.md)、[spec/session.md](spec/session.md) | 旧 DSH v4 数据形状 | 不能作为新代码依据；0.3 删除 |
| 历史证据 | [spec/dsh-event-catalog.md](spec/dsh-event-catalog.md)、[spec/dsh-event-catalog-unresolved.md](spec/dsh-event-catalog-unresolved.md) | 外部 harness 逆向/生成材料 | 不是运行期契约；0.3 删除 |
| 历史研究 | [archive/](archive/) | 已完成的计划、审计与研究 | 只用于追溯，不再维护 |
| 发布/运维 | [release/](release/)、[ops/](ops/) | 发布说明、审计快照、数据边界 | 发布总表优先 |

`docs/` 下不再有 `docs/plan/`。2026-11-18 已删除 14 篇已完成计划/任务卡，6 篇仍被引用的研究移入
[archive/](archive/)。历史提交仍可追溯，引用这些研究的现行文档已改为归档路径。

## 3. 目录地图

| 目录 | 内容 | 入口 |
| --- | --- | --- |
| [spec/](spec/) | 38 篇规格 + 状态索引（39 个文件） | [spec/README.md](spec/README.md) |
| [native/](native/) | schema 1 契约、实现/录制说明、覆盖与重录说明 | [native/schema.md](native/schema.md)、[native/coverage.md](native/coverage.md) |
| [guides/](guides/) | 面向使用者的操作指南：native 默认化、会话迁移、配置迁移、bundle digest、Windows 沙箱边界（5 篇） | [guides/native-loop-default.md](guides/native-loop-default.md) |
| [adr/](adr/) | 架构决策记录 | [ADR 0001](adr/0001-three-track-native-agent.md) |
| [plugins/](plugins/) | 插件作者指南与 v2→v3 迁移 | [plugins/README.md](plugins/README.md) |
| [release/](release/) | 0.2.0 发布说明、已知问题总表、去品牌审计 | [release/0.2.0.md](release/0.2.0.md) |
| [ops/](ops/) | 遥测、数据与运维边界 | [ops/telemetry.md](ops/telemetry.md) |
| [design/](design/) | 0.3 迁移设计草案（TUI native、provider port、native host），不是现行契约 | [design/tui-native-migration.md](design/tui-native-migration.md) |
| [archive/](archive/) | 6 篇历史研究/审计 + 归档索引 | [archive/README.md](archive/README.md) |

根目录还有 [README.md](../README.md) 与
[fixtures/native/README.md](../fixtures/native/README.md)（录制数据说明）。

## 4. 状态标签

每篇现行/过渡规格标题下都必须有状态块，至少包含“状态、层级、最后核对”。基础状态有 8 种；括号内可加范围/里程碑限定（如 `API v3`、`M4 已扩展`），限定词不改变基础状态：

- **当前契约**：native schema 1 或经 ADR 冻结的决策；新代码可以依赖。
- **现行（0.2.0 保留）**：已实现、与 native 方向一致；以代码为准。
- **现行（过渡：v4 待移除）**：同一版本内 headless 已默认 native、TUI 仍走 v4；只用于理解现状。
- **现行（native 默认）**：headless 默认 native，`--legacy-v4` 可回退。
- **冻结：迁移参考**：只用于读旧数据或写迁移说明，0.3 删除。
- **历史证据（0.2.0 保留，0.3 删除）**：生成/逆向材料，不是运行期契约。
- **历史文档（非契约）**：已归档，不再维护。

完整状态表与每篇规格的归属见 [spec/README.md §1–§5](spec/README.md)。

## 5. 维护规则

1. **新文档先定位**：会话/消息/事件形状写入 `native/`；功能规格写入 `spec/`；插件作者内容写入
   `plugins/`；发布与迁移写入 `release/`；运维边界写入 `ops/`；已完成的研究放入 `archive/`。
2. **标题下必须有状态块**：状态、层级、最后核对日期；没有状态块的规格按未评审处理。
3. **删除前先改索引**：先更新 [spec/README.md](spec/README.md) 或本文件的目录表，再 `git rm`；
   归档用 `git mv` 保留历史，并在 [archive/README.md](archive/README.md) 登记。
4. **归档不是契约**：归档文档只允许修断链和加横幅，不再更新结论；引用归档时写清“历史/非契约”。
5. **发布总表优先**：已知问题、状态和 0.3 范围以
   [release/0.2.0-known-issues.md](release/0.2.0-known-issues.md) 为准，其他文档只放摘要。

## 6. 0.3 文档删除清单

以下内容在 0.2.0 仍被默认 v4 路径或生成脚本使用，**不能现在删**；0.3 移除 v4 时按此清单处理：

- [spec/INTERFACES.md](spec/INTERFACES.md)、[spec/session.md](spec/session.md)：v4 总接口与存储规范；
- [spec/dsh-event-catalog.md](spec/dsh-event-catalog.md)、[spec/dsh-event-catalog-unresolved.md](spec/dsh-event-catalog-unresolved.md)：
  外部 harness 事件目录；同时删除 `scripts/extract_dsh_events.mjs` 与 `core/event_catalog.py` 的生成链；
- [spec/loop.md](spec/loop.md)、[spec/events.md](spec/events.md)、[spec/hooks.md](spec/hooks.md)、
  [spec/projection.md](spec/projection.md)、[spec/session-query.md](spec/session-query.md) 中的 v4 部分：
  重写为 native loop/typed events/fold，而不是继续维护双语义；
- [release/dsh-audit.md](release/dsh-audit.md)：去品牌完成后转为历史快照或删除。

## 7. 最近变更

| 日期 | 变更 |
| --- | --- |
| 2026-11-18 | 建立本入口；删除 14 篇已完成计划；归档 6 篇历史研究；38 篇规格 + 索引全部补齐状态头；重写 spec 索引、ADR/native/release 状态与链接。 |
| 2026-11-18 | 新增 [guides/](guides/) 目录（4 篇操作指南）；状态标签区分“native 默认”与“TUI 仍 v4”；目录地图补 guides 行。 |
| 2026-11-18 | 新增 [guides/config-migration.md](guides/config-migration.md) 与 [design/](design/) 三篇 0.3 设计草案；guides 增至 5 篇；TUI 可只读查看 native 会话。 |
