# 规格文档索引（docs/spec）

> **状态**：现行索引（0.3.0 native-only） · **最后核对**：2026-11-19 · **覆盖**：31 篇规格 + 本索引
> 总文档入口见 [docs/README.md](../README.md)；0.3.0 破坏性变更以 [docs/release/0.3.0.md](../release/0.3.0.md) 为准；
> 历史遗留项以 [docs/release/0.2.0-known-issues.md](../release/0.2.0-known-issues.md) 为准（仅追溯）。
> 权威契约：native schema 1 + ADR。与 ADR 冲突时以 ADR 为准；与 schema 冲突时以 schema 为准。

0.3.0 是 native-only 硬切：产品只读写 [native/schema.md](../native/schema.md) 的 schema 1，只有一条 native `AgentLoop`，
TUI、headless 和回环 Web UI 三个入口共用同一 runtime。v4 会话栈、v4 loop、projection、session_query、
`core.wire`、`dpyro migrate` 与四个 0.2.0 回退开关已删除，旧 `session.v4.jsonl` 不再可读。
本目录不再有“v4 过渡”“native 默认但可回退”这类状态；每篇文档只描述当前唯一路径。

## 1. 状态图例

| 状态 | 含义 | 能否作为新代码依据 |
| --- | --- | --- |
| `当前契约` | native schema 1、ADR 或经 ADR 冻结的现行契约 | ✅ 可以 |
| `现行（0.3.0）` | 已实现的 0.3.0 行为规格；以代码和测试为最终事实 | ✅ 可以（冲突时以代码为准） |
| `历史文档（非契约）` | 已移入 [docs/archive/](../archive/) 或 [docs/design/](../design/) 的计划/研究/设计草案 | ❌ 不行 |
| `历史证据` | 0.2.0 发布说明与已知问题，只用于追溯旧行为 | ❌ 不行 |

括号内是范围限定（如 `goal 域冻结`、`API v3`），不改变基础状态。没有状态头的文档视为未评审。

## 2. 唯一契约入口

| 文档 | 性质 | 状态 | 说明 |
| --- | --- | --- | --- |
| [native/schema.md](../native/schema.md) | native session schema 1（header/envelope/EventKind/Message/fold/trace/fork/校验） | `当前契约` | 唯一会话契约；native loop 只写、只读 schema 1 |
| [ADR 0001](../adr/0001-three-track-native-agent.md) | 三线（插件化 / Python 化 / AI 原生）决策记录 | `当前契约` | 0.3.0 状态更新置顶；M0–M5 历史小节保留 |
| [native/README.md](../native/README.md) | native 实现、录制 baseline 与门禁账本 | `当前契约`（状态报告，非规范正文） | 记录实现时点与测试数字；不定义 schema |

旧总接口（`INTERFACES.md`）、旧 `session.v4` 存储规格（`session.md`）、`projection.md`、`session-query.md`、
`dsh-event-catalog*.md` 已于 2026-11-19 随 v4 栈删除。图片卸载/请求预算规格（`image-budget.md`、`image-offload.md`、
`image-request.md`）同样随 v4 会话投影与 v4 agent 层删除；当前只有附件存储与 native 消息的图片 part 解析
（见 [attachments.md](attachments.md)、[llm.md](llm.md)）。旧会话不可读，也不再提供迁移命令；
需要旧数据的用户应在 0.2.x 上迁移后再升级（见 0.3.0 release note §3）。

## 3. 现行功能规格（0.3.0）

| 文档 | 领域 | 状态 | 备注 |
| --- | --- | --- | --- |
| [tools.md](tools.md) | 工具契约 | `现行（0.3.0）` | 工具 schema 快照逐字节稳定（ADR §4）；公开插件契约见 [plugin-core.md](plugin-core.md) |
| [llm.md](llm.md) | LLM provider 契约 | `现行（0.3.0）` | 0.3.0 起只有 native provider port（OpenAI-compatible + native Anthropic） |
| [sandbox.md](sandbox.md) | 沙箱/权限执行 | `现行（0.3.0）` | Windows ACL runner；`enforcement=partial` 的限制保留 |
| [activity.md](activity.md) | 后台活动 | `现行（0.3.0）` | native 活动事实与 TUI 展示 |
| [agent-presets.md](agent-presets.md) | preset 组合 | `现行（0.3.0）` | preset v3 形状 |
| [attachments.md](attachments.md) | 附件/图片 | `现行（0.3.0）` | native session 只存附件引用 |
| [compact-blocks.md](compact-blocks.md) | 压缩块 | `现行（0.3.0）` | 与 native `compaction.done.covers` 对齐 |
| [events.md](events.md) | 内核事件目录 | `现行（0.3.0）` | 单一总线目录；24 个 implemented + 22 个 reserved |
| [frozen-distribution.md](frozen-distribution.md) | PyInstaller 打包 | `现行（0.3.0）` | core bundle 字符串行对静态分析不可见 |
| [goal.md](goal.md) | goal 域 | `现行契约（goal 域冻结）` | FROZEN M0 2026-11-16；goal 域唯一契约 |
| [headless.md](headless.md) | headless CLI | `现行（0.3.0）` | 只有 native 路径；无 v4 回退参数 |
| [hooks.md](hooks.md) | hooks 契约 | `现行（0.3.0）` | native typed `AgentHooks` + 插件 `HookService`/`EventBus` 两个面 |
| [im-plugin.md](im-plugin.md) | IM 插件 | `现行（0.3.0）` | 实现在仓库外 `dpyro-im` |
| [loop.md](loop.md) | native `AgentLoop` | `现行（0.3.0）` | run/turn/step、fold 驱动、预算/取消/checkpoint/trace |
| [mcp.md](mcp.md) | MCP client | `现行（0.3.0）` | `mcp/connected`、`mcp/disconnected` 总线事件 |
| [memory.md](memory.md) | runtime memory | `现行（0.3.0）` | SQLite FTS5 backend；无 embedding |
| [plugin-core.md](plugin-core.md) | 插件/组合契约 | `现行（0.3.0）` | 当前 `API_VERSION = 3` |
| [plugin-platform.md](plugin-platform.md) | 插件安装/打包 | `现行（0.3.0）` | `plugin.toml` 安装单元 + entry points/bundle v3 |
| [plugin-settings.md](plugin-settings.md) | settings CAS | `现行（0.3.0）` | |
| [plugins.md](plugins.md) | 插件管理/自扩展 | `现行（0.3.0）` | |
| [ptc.md](ptc.md) | PTC | `现行（0.3.0）` | 与 native loop 共用同一 AgentLoop |
| [schedule.md](schedule.md) | 定时任务 | `现行（0.3.0）` | |
| [skills.md](skills.md) | skills | `现行（0.3.0）` | 只读加载 |
| [subagent.md](subagent.md) | 子 agent | `现行（0.3.0）` | 子会话 `origin=subagent` + `parent_id`/`delegation_depth` |
| [team.md](team.md) | 团队 | `现行（0.3.0）` | native 子会话与 `plugin.team.*` 事实 |
| [tui.md](tui.md) | TUI 总览 | `现行（0.3.0）` | native 交互路径；splash F1=Web、F2/Enter=终端 |
| [tui-slots.md](tui-slots.md) | TUI slots | `现行（0.3.0）` | layer E |
| [tui-contributions.md](tui-contributions.md) | TUI 贡献 | `现行（0.3.0）` | |
| [tui-input.md](tui-input.md) | TUI 输入 | `现行（0.3.0）` | |
| [tui-widgets.md](tui-widgets.md) | TUI widgets | `现行（0.3.0）` | |
| [web.md](web.md) | Web UI 宿主 | `现行（0.3.0）` | 仅回环；TUI splash F1 或 `python -m deeppyro.web` 启动 |

## 4. 历史材料（非契约）

- [docs/archive/](../archive/)：0.1/0.2 时代的研究与已完成计划，只用于追溯；索引见 [archive/README.md](../archive/README.md)。
- [docs/design/](../design/)：0.3.0 迁移设计草案。其中 TUI native、native host、provider port 等已实现（0.3.0），
  `v4-deletion-plan.md` 是删除计划的历史记录；这些文档都不是新代码依据。
- 0.2.0 发布说明与 known issues（[docs/release/](../release/)）保留为历史证据，描述已删除机制的内容不再有效。

## 5. 维护规则

1. **新文档先定位**：会话/消息/事件形状只写进 `docs/native/`；功能规格写 `docs/spec/`；插件作者文档写 `docs/plugins/`；发布/迁移写 `docs/release/`；运维与数据边界写 `docs/ops/`；已完成研究放 `docs/archive/`。
2. **每份规格必须带状态头**：至少写明“状态、层级、最后核对”。没有状态头的文档视为未评审。
3. **代码和测试是最终事实**：0.3.0 只有 native 一条路径；文档与代码冲突时修文档，不得为了迁就文档改行为。
4. **删除前先改索引**：删除任何文件前先把本索引对应行删除或改为归档位置，避免死链。
5. **归档登记**：归档用 `git mv` 进 `docs/archive/`，并在 [archive/README.md](../archive/README.md) 登记；归档只允许修断链和加历史横幅。
6. **与 release note 同步**：破坏性变更（旧会话不可读、flags 删除、插件 API v3、Web/TUI 入口）以 [0.3.0.md](../release/0.3.0.md) 为准。

## 6. 目录统计（2026-11-19 删除 v4 文档后）

| 目录 | 文件数 | 内容 |
| --- | --- | --- |
| `docs/spec/` | 32 | 31 篇规格 + 本索引 |
| `docs/native/` | 5 | `schema.md`（契约正文）、`README.md`（实现/录制状态）、`coverage.md`、`goal.md`、`compaction.md` |
| `docs/adr/` | 1 | 三线 ADR |
| `docs/archive/` | 7 | 归档索引 + 1 篇 goal 审计 + 5 篇研究 |
| `docs/guides/` | 5 | Web UI、配置迁移、bundle digest、bundle 签名、Windows 沙箱边界 |
| `docs/design/` | 6 | 0.3.0 迁移设计（已实现或历史） |
| `docs/plugins/` | 2 | 插件作者指南 + v2→v3 迁移 |
| `docs/release/` | 4 | 0.3.0、0.2.0（历史）、0.2.0 known issues（历史）、dsh-audit（历史） |
| `docs/ops/` | 1 | 遥测/数据边界 |
| `docs/README.md` | 1 | 文档地图 |
| `docs/` 总计 | 64 | 以上全部 |