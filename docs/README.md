# DeepPyro 文档地图（0.3.0）

> **状态**：现行文档入口 · **版本**：0.3.0 native-only · **最后核对**：2026-11-20
>
> 本文件回答三个问题：先读什么、每类文档的权威性是什么、过时文档去哪里。具体规格状态以
> [spec/README.md](spec/README.md) 为准；发布范围与遗留项以
> [release/0.3.0.md](release/0.3.0.md) 为准。

## 1. 先读这五份

| 顺序 | 文档 | 读它来了解 |
| --- | --- | --- |
| 1 | [README.md](../README.md) | 安装、三个入口、当前能力与 0.3.0 定位 |
| 2 | [release/0.3.0.md](release/0.3.0.md) | 破坏性变更、native-only 边界、验收门禁、已知限制 |
| 3 | [adr/0001-three-track-native-agent.md](adr/0001-three-track-native-agent.md) | 为什么做 native 契约、三条主线与里程碑 |
| 4 | [native/schema.md](native/schema.md) | native session schema 1 的唯一契约正文 |
| 5 | [spec/web.md](spec/web.md) | Web 宿主：React SPA 静态产物、回环 JSON API/SSE、安全模型与打包 |

插件作者从 [plugins/README.md](plugins/README.md) 开始；从 v2 升级到 API v3 看
[plugins/migration-v2-v3.md](plugins/migration-v2-v3.md)。0.2 及更早的旧形状只保留在
[archive/](archive/) 的历史材料里，不是运行期契约。

## 2. 文档分层与权威性

| 层 | 目录 | 性质 | 冲突时 |
| --- | --- | --- | --- |
| 契约 | [native/schema.md](native/schema.md)、[adr/](adr/) | 冻结的 native schema 与架构决策 | schema / ADR 优先 |
| 现行规格 | [spec/](spec/) | 已实现行为的功能规格 | 以代码与测试为准；与契约冲突时以契约为准 |
| 操作指南 | [guides/](guides/) | 面向使用者的可执行步骤 | 以代码行为为准 |
| 历史材料 | [archive/](archive/)、[design/](design/) | 已完成研究、已被实现取代的设计草案、外部协议逆向 | 只用于追溯，不得据此扩展 |
| 发布/运维 | [release/](release/)、[ops/](ops/) | 发布说明、审计快照、数据边界 | 最新 release note 优先 |

`docs/` 下没有 `docs/plan/`。0.2.0 已删除过期计划并把仍被引用的研究移入
[archive/](archive/)；0.3.0 又删除了 v4 交互/会话/迁移规格，历史提交仍可追溯。

## 3. 目录地图

| 目录 | 内容 | 入口 |
| --- | --- | --- |
| [spec/](spec/) | 现行功能规格与状态索引 | [spec/README.md](spec/README.md) |
| [native/](native/) | schema 1 契约、实现/录制说明、覆盖与重录说明 | [native/schema.md](native/schema.md)、[native/coverage.md](native/coverage.md) |
| [guides/](guides/) | 面向使用者的操作指南：Web UI、配置迁移、bundle digest/签名、Windows 沙箱边界 | [guides/web-ui.md](guides/web-ui.md) |
| [adr/](adr/) | 架构决策记录 | [ADR 0001](adr/0001-three-track-native-agent.md) |
| [plugins/](plugins/) | 插件作者指南与 v2→v3 迁移 | [plugins/README.md](plugins/README.md) |
| [release/](release/) | 0.3.0 发布说明；0.2.0 历史账本与去品牌审计 | [release/0.3.0.md](release/0.3.0.md) |
| [ops/](ops/) | 遥测、数据与运维边界 | [ops/telemetry.md](ops/telemetry.md) |
| [design/](design/) | 0.3 迁移设计：TUI native、provider port、native host、v4 删除清单、React SPA 架构 | [design/tui-native-runtime.md](design/tui-native-runtime.md)、[design/react-web-ui.md](design/react-web-ui.md) |
| [archive/](archive/) | 历史研究/审计 + 归档索引 | [archive/README.md](archive/README.md) |

根目录还有 [README.md](../README.md) 与
[fixtures/native/README.md](../fixtures/native/README.md)（录制数据说明）。

## 4. 状态标签

每篇现行规格标题下都必须有状态块，至少包含“状态、层级、最后核对”。基础状态：

- **当前契约**：native schema 1 或经 ADR 冻结的决策；新代码可以依赖。
- **现行**：已实现、与 native 方向一致；以代码为准（可带版本/范围限定）。
- **历史文档（非契约）**：已归档，不再维护；只允许修断链和加横幅。
- **历史证据**：外部协议逆向/录制材料，不是运行期契约。

0.3.0 起不再有“v4 过渡”“native 默认但可回退”这类状态；代码和文档都只有 native 一条路径。

## 5. 维护规则

1. **新文档先定位**：会话/消息/事件形状写入 `native/`；功能规格写入 `spec/`；插件作者内容写入
   `plugins/`；发布与迁移写入 `release/`；运维边界写入 `ops/`；已完成的研究放入 `archive/`。
2. **标题下必须有状态块**：状态、层级、最后核对日期；没有状态块的规格按未评审处理。
3. **删除前先改索引**：先更新 [spec/README.md](spec/README.md) 或本文件的目录表，再 `git rm`；
   归档用 `git mv` 保留历史，并在 [archive/README.md](archive/README.md) 登记。
4. **归档不是契约**：归档文档只允许修断链和加横幅，不再更新结论；引用归档时写清“历史/非契约”。
5. **发布总表优先**：已知问题、状态和后续范围以最新 [release/](release/) 文档为准，其他文档只放摘要。

## 6. 0.3.0 已完成的文档清理

- 删除 v4 会话/交互/迁移规格与 `dpyro migrate` 指南；旧会话不再可读，会话迁移路径不存在。
  配置仍可从 0.2.x 自动升级，见 [guides/config-migration.md](guides/config-migration.md)。
- 删除 `--legacy-v4`、`--native-loop`、`DEEPPYRO_NATIVE_LOOP`、`DEEPPYRO_TUI_NATIVE` 的全部现行描述。
- `spec/web.md` 从“phase 1，不含 F1/F2”更新为已接线的启动页行为；2026-11-20 再改写为 React SPA 契约
  （静态构建产物、SPA 路由、JSON API/SSE、CSP 与 wheel 打包）。`design/nicegui-web-ui.md` 删除，
  新增 `design/react-web-ui.md` 记录 SPA 架构与取舍。
- `spec/loop.md`、`spec/events.md`、`spec/hooks.md` 只描述 native loop/typed events/fold。
- 0.2.0 的已知问题总表保留为历史账本；0.3.0 的边界写入 [release/0.3.0.md](release/0.3.0.md)。

## 7. 最近变更

| 日期 | 变更 |
| --- | --- |
| 2026-11-20 | Web UI 文档切到 React SPA：重写 `spec/web.md` 与 `guides/web-ui.md`，删除 `design/nicegui-web-ui.md`、新增 `design/react-web-ui.md`；README 补前端构建与静态产物说明。 |
| 2026-11-19 | 0.3.0 native-only：删除 v4 文档与回退/会话迁移说明；新增 Web UI 规格/指南与旧配置自动升级指南；重写本入口与 release note。 |
| 2026-11-18 | 新增 [guides/](guides/) 目录；状态标签区分 native 默认与 TUI 仍 v4；目录地图补 guides 行。 |
| 2026-11-18 | 建立本入口；删除过期计划；归档历史研究；规格全部补齐状态头。 |
