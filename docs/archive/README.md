# 历史文档归档

> **状态**：历史文档（非契约） · **最后核对**：2026-11-18
>
> 本目录保存 0.2.0 文档整理中移除自现行目录、但仍被规格或源码引用的研究、审计与计划复盘。
> 这里的内容**不再维护**，字段、路径、结论和测试数字都可能已过期；不要据此写新代码。

## 1. 归档清单

| 文档 | 原位置 | 归档原因 | 现行替代 |
| --- | --- | --- | --- |
| [goal/goal-audit.md](goal/goal-audit.md) | `docs/plan/goal-audit.md` | goal 升级线的差异审计与复盘，已完成 | [spec/goal.md](../spec/goal.md)、[ADR §6](../adr/0001-three-track-native-agent.md) |
| [research/p1-context.md](research/p1-context.md) | `docs/plan/p1-context.md` | P1 context/compaction 设计研究，已被 native ContextPlan 与 compact-blocks 规格取代 | [spec/compact-blocks.md](../spec/compact-blocks.md)、[spec/projection.md](../spec/projection.md) |
| [research/p1-ext.md](research/p1-ext.md) | `docs/plan/p1-ext.md` | 扩展点/插件化早期研究，已被 API v3 插件契约取代 | [spec/plugin-core.md](../spec/plugin-core.md)、[plugins/README.md](../plugins/README.md) |
| [research/p1-providers.md](research/p1-providers.md) | `docs/plan/p1-providers.md` | provider 分层早期研究，现行实现以 llm 规格为准 | [spec/llm.md](../spec/llm.md) |
| [research/p1-tools-runtime.md](research/p1-tools-runtime.md) | `docs/plan/p1-tools-runtime.md` | 工具运行时/附件早期研究，现行实现以 tools/attachments 规格为准 | [spec/tools.md](../spec/tools.md)、[spec/attachments.md](../spec/attachments.md) |
| [research/p2-bus.md](research/p2-bus.md) | `docs/plan/p2-bus.md` | P2 bus/hook 方案研究，v4 仍在使用但 native 已改用 typed events | [spec/events.md](../spec/events.md)、[spec/hooks.md](../spec/hooks.md) |

## 2. 引用规则

- 现行文档引用本目录时，必须标注“历史/非契约”，例如：
  `[p1-context.md（历史研究）](../archive/research/p1-context.md)`。
- 归档文档内的链接如果指向已删除的 `docs/plan/` 文件，允许只修链接，不更新结论。
- 归档文档不进入 `docs/spec/` 的状态表，也不参与“当前规格”计数。

## 3. 归档横幅模板

每篇归档文档标题下使用：

```markdown
> **历史文档（非契约）**。本文是 0.2.0 之前的研究/计划记录，保留用于追溯设计依据；
> 不再维护，字段、路径与结论可能已过期。当前入口见 [docs/README.md](../../README.md)。
```

## 4. 删除记录

2026-11-18 从 `docs/plan/` 删除的 14 篇已完成计划/任务卡（未归档，git 历史可追溯）：

- `goal-tasks/M0.md`–`M6.md`（9 张 goal 升级任务卡）
- `goal-upgrade.md`
- `p0-bus-alignment.md`
- `p1-bus.md`
- `preset-modes.md`
- `sandbox-alignment.md`

这些内容已由现行 spec、ADR 与 release note 覆盖；如需追溯，使用 `git log -- docs/plan/<file>`。
