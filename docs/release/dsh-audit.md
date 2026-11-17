# DSH 去品牌审计（0.2.0）

> 目标：为 0.2.0 的去品牌门禁（ADR D5 M5：`rg -i dsh src` 接近 0）提供可复核的现状盘点与清理建议。
> 范围：本审计只统计与建议，**不改 `src` 代码**，实际清理留给后续版本。
> 基线 commit：`35b433f`；集成复核：`integration/m2-m5`（M2–M5 合流后）。

## 1. 结论摘要

- `src` 中的 dsh 引用**集中在旧会话/事件协议层**：生成的事件目录 `core/event_catalog.py` 一个文件就占 90 个字符串；
  其余多为注释对照、协议字段名与少数标识符。
- 原计划大部分命中随 **M2（v4 会话/事件移除）** 消失；但 M2 的范围只到 native loop，**v4 退出被显式推迟到 0.3**，
  因此 `core/event_catalog.py`、`core/wire.py`、`core/events.py`、`session/events.py` 仍然存在。
- 0.2.0 目标状态：`src` 零业务含义的 dsh 引用；允许保留的只有**历史 provenance 说明**（且不进入产品路径）。
- **当前未达标**：`src` 大小写不敏感命中 **379 行 / 91 个文件**（集成线实测，排除 `__pycache__`）。
  0.2.0 按 release candidate 发布，本项作为 0.3 的清理目标。

## 2. 审计方法（可复现）

### 2.1 行级计数

```powershell
# 大小写不敏感（ADR 门禁口径）
Get-ChildItem -Recurse src -File | Select-String -Pattern 'dsh' -CaseSensitive:$false |
  Measure-Object -Line
# 379 行，91 个文件（排除 __pycache__）

# 大小写敏感拆分（用于解释口径差异）
Get-ChildItem -Recurse src -File | Select-String -Pattern 'dsh' -CaseSensitive | Measure-Object -Line   # 133
Get-ChildItem -Recurse src -File | Select-String -Pattern 'DSH' -CaseSensitive | Measure-Object -Line   # 262
# 133 + 262 = 395 > 379：有 16 行同一行同时含 dsh 与 DSH（注释 + 协议名）。
```

说明：lead 任务书里“约 133 处”是**小写 `dsh` 的行数**；ADR 门禁用的是 `-i` 不区分大小写，
所以 0.2.0 的验收数字应以 **379 行** 为起点，最终目标接近 0。

### 2.2 Token 级计数（Python `tokenize`，只看 `src/**/*.py`）

用 Python 标准库 `tokenize` 扫描全部 `.py`，统计含 `dsh`（不区分大小写）的 token：

| token 类型 | 数量 | 说明 |
| --- | --- | --- |
| `COMMENT` | 54 | 注释里的对照说明/来源标注 |
| `STRING` | 262 | 含 docstring、事件 producer 路径、错误/提示文案、路径字符串 |
| `NAME` | 33 | 标识符（变量/常量名） |
| `FSTRING_MIDDLE` | 3 | f-string 文本片段 |
| **合计** | **352** | 分布在 **90** 个 `.py` 文件 |

字符串细分：其中 **94 个**是 `@deepseek-ai/dsh-<pkg>/lib/index.js:<line>` 形式的 producer 路径，
几乎全部来自生成文件 `core/event_catalog.py`。

### 2.3 其他目录（不属本审计整改范围，供 M5 门禁参考）

| 目录 | 行命中（-i） | 说明 |
| --- | --- | --- |
| `tests/` | 95 | 测试名/fixture/断言随 M2 语音重写；M5 要求 0 skip |
| `docs/` | 669 | 文档允许引用历史；`docs/spec/README.md` 已标状态 |
| `src/` | 379 | 本审计对象 |

## 3. 分类与处置建议

### A. 生成产物 / 协议数据（随 v4 移除删除）

| 位置 | 命中 | 内容 | 建议 |
| --- | --- | --- | --- |
| `src/deeppyro/core/event_catalog.py` | 93 行 / 90 字符串 | 从 DSH asar 抽取的事件目录，producer 路径 | **0.3 删除**（v4 事件目录退役；同时处理生成脚本 `scripts/extract_dsh_events.mjs` 与相关 docs） |
| `src/deeppyro/core/wire.py` | 5 行 | v4 wire 词表、`KNOWN_SESSION_EVENT_TYPES`（57 类型） | **0.3 删除**（`core.wire` 已冻结，M1 后禁止新增 import） |
| `src/deeppyro/core/events.py` | 24 行 / 8 字符串 | 94 名 bus 目录、DSH 事件名映射、`dsh_ref`/`dsh_modes` | **0.3 重写**：dotted 事件模型（native EventKind）；标识符改名 |
| `src/deeppyro/session/events.py` | 62 记录类型 | v4 记录词表（斜杠事件名） | **0.3 删除** |

### B. 注释引用（保留信息，改写措辞）

54 个注释 token，典型位置：

| 文件:行 | 内容性质 | 建议 |
| --- | --- | --- |
| `src/deeppyro/agent/options.py:17` | 与旧实现行为对齐的说明 | 改写为“旧实现/参考实现”，或删除 |
| `src/deeppyro/agent/retry.py:47,346` | 重试语义来源 | 同上；若属 ADR 保留的设计来源，注明 ADR |
| `src/deeppyro/agent/session_turn.py:235,260` | 旧 loop 对照 | M2 重写 loop 时一并删除 |
| `src/deeppyro/core/wire.py:145-146` | 词表来源注释 | M2 随文件删除 |
| `src/deeppyro/hooks/types.py:32,47,54,81,85` | hook 事件名来源 | 改写为 native 事件名（M2） |
| `src/deeppyro/sandbox/types.py:13,16,23,65` | 沙箱模式来源 | 保留语义、改措辞 |
| `src/deeppyro/paths.py:15` | `~/.dsh` vs `~/.dpro` 说明 | **保留**（解释为何不读旧目录，属迁移信息） |
| `src/deeppyro/config.py:51,55,76` | 旧配置字段对照 | M3 snake_case 迁移时删除/改写 |
| `src/deeppyro/kernel/composition.py:776` | 旧平台对照 | 改写 |
| `src/deeppyro/llm/retry.py:36,54`、`mcp/*`、`ptc/sdk.py:110,119` | 行为对照 | 逐文件改写 |

原则：**注释只保留“为什么这样设计”的知识，不保留“某个外部实现怎么做的”引用**；
需要保留来源时指向 `docs/adr/0001-three-track-native-agent.md`。

### C. 标识符（改名，涉及行为/测试）

33 个 NAME token：

- `src/deeppyro/core/events.py`：`dsh_ref`、`dsh_modes` → `legacy_ref`/`legacy_modes`（M2 删除该文件时自然消失）。
- `src/deeppyro/skills/{__init__,discovery,types}.py`：`PROJECT_DSH_RANK`、`USER_DSH_RANK`、`SOURCE_PROJECT_DSH`、
  `SOURCE_USER_DSH` → 改为中性名（如 `PROJECT_LEGACY_RANK` / `SOURCE_PROJECT_LEGACY`）。
  注意：这些常量参与 skill 排序/来源判定，改名要同步改测试（`tests/skills/test_discovery.py` 等 15 处命中）。
- 其余标识符随 A/B 类文件删除。

### D. 普通字符串（docstring / 文案 / 路径）

除生成目录外约 170 个字符串 token，集中在：

| 文件 | 字符串命中 | 内容 | 建议 |
| --- | --- | --- | --- |
| `src/deeppyro/sandbox/policy.py` | 9 | 模式说明/来源 | 改写文案 |
| `src/deeppyro/skills/types.py` | 9 | rank/source 名 | 随标识符一起改 |
| `src/deeppyro/llm/image_budget.py` | 7 | 图片预算对照说明 | 改写 |
| `src/deeppyro/attachments/types.py` | 5 | 附件类型说明 | 改写 |
| `src/deeppyro/session/layout.py` | 5 | 路径说明 | 改写 |
| `src/deeppyro/projection/registry.py`、`prompt/projection.py` | 4+4 | projection 对照 | 改写 |
| `src/deeppyro/sandbox/provider.py`、`session_query/query.py`、`skills/__init__.py` | 4+4+4 | 说明/文案 | 改写 |

### E. 测试 fixture（不在本审计整改，M5 门禁要求 0 skip）

- `tests/` 共 95 行命中，主要是 skill rank 常量、MCP 对齐测试、core events 词表测试。
- 旧 DSH 会话 fixtures 由 `tools/gen_fixtures.mjs` 生成；M5 要求 CI 无 Node、无 skip，
  这批 fixture 与生成器随 v4 移除一并删除（native fixtures 已由 `fixtures/native/` 接管）。

## 4. 建议的 0.2.0 处置

| 类别 | 0.2.0 保留？ | 处置 | 责任线 |
| --- | --- | --- | --- |
| A 生成产物/协议数据 | 否 | 随 0.3 删除 v4 时移除；0.2.0 暂留 | Y（0.3） |
| B 注释引用 | 否（迁移说明除外） | 改写为中性表述或删除 | 各 lane 随代码重写 |
| C 标识符 | 否 | 改名 + 同步测试 | Y（M2）/ M4（skills） |
| D 普通字符串 | 否 | 逐文件改写文案 | 各 lane |
| E 测试 fixture | 否 | 删除旧 fixtures/生成器，保留 native fixtures | M5 集成 |
| `paths.py:15` 旧目录说明 | **是** | 保留一句“不读取旧目录”的迁移说明 | 集成 owner 决定 |

目标不是“grep 到 0 就结束”，而是：**产品路径不依赖任何外部 harness 的命名、字段或事件目录**。

## 5. 落地清单（交给集成 owner）

1. 0.3 删除 v4 后重跑第 2 节命令，确认 `core/{wire,events,event_catalog}.py`、`session/events.py` 已删除/重写。
2. 批量改写剩余注释/docstring（约 54 注释 + 170 非生成字符串），每批跑四道门禁。
3. 重命名 skills rank 常量并同步 `tests/skills/*`。
4. 删除 `tools/gen_fixtures.mjs` 与旧 fixtures；确认 `pytest` 0 skip。
5. 门禁记账：在 CI 或发布 checklist 中加入 `rg -i dsh src` 计数，只允许下降；
   0.2.0 发布前目标 **< 10 行**（仅剩迁移说明类注释），理想为 0。

## 6. 不做的事（明确边界）

- 本 M5 不改 `src` 代码（只改 `pyproject.toml` 与 `src/deeppyro/__init__.py` 的版本号）。
- 0.2.0 不删除 `core/event_catalog.py`、不改 `core/wire.py`：v4 退出已推迟到 0.3，提前删除会破坏默认 TUI/headless 路径。
- 不批量改测试与 fixtures：等 M2 的会话/事件替换落地后再改，避免重复返工。
- `docs/` 中的历史引用不要求清零；由 `docs/spec/README.md` 标注状态即可。

## 7. 参考

- `docs/adr/0001-three-track-native-agent.md` D2/D3/D5（零 wire 兼容、保留设计资产、M5 去品牌门禁）。
- `docs/spec/README.md`：legacy 规格的冻结/待删状态。
- `docs/release/0.2.0.md`：发布门禁与已知限制。
