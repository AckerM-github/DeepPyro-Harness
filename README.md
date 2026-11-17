# DeepPyro Harness (deeppyro)

Terminal-first **pure Python** agent harness。0.2.0 引入 DeepPyro 自己定义的 native 会话/事件/追踪模型，
并把“插件化、Python 化、AI 原生”作为三条主线；旧 v4 路径仍暂时保留，切换状态见下方说明与“已知限制”。

> **0.2.0 状态**：M1（native 契约 + 观测骨架）、M2（native loop/checkpoint）、
> M3（插件 API v3 + typed services）、M4（eval/memory/multi-agent）已完成并合流，M5 完成发布文档与 0.2.0 版本号。
> native loop 目前是 headless 的显式开关（`DEEPPYRO_NATIVE_LOOP=1` / `--native-loop`），TUI 与默认 headless
> 仍走 v4；0.2.0 是 release candidate，不是“native-only”最终切换。状态以 `docs/spec/README.md` 为准。

## 0.2.0 的三条主线

| 线 | 目标 | 落地位置 |
| --- | --- | --- |
| **Python 化（Y）** | 一种会话格式、一个消息模型、一套事件类型；用 dataclass/Protocol/StrEnum/TaskGroup 表达，不再复刻 TypeScript 形状 | `src/deeppyro/core/messages.py`、`session/schema.py`、`session/fold.py`、`agent/loop.py` |
| **插件化（P）** | 一切能力经插件贡献；一种 mount 形状（`setup(self)`）、typed `inject`/`ctx.get`、声明的 bundle、能力清单 | `src/deeppyro/api/`、`kernel/`、`plugins/`、`bundles/` |
| **AI 原生（A）** | 会话日志只记录事实，token/时延/重试进本地 trace；可回放、可断点续跑、可评测 | `src/deeppyro/observability/trace.py`、`context/`、`eval/`、`memory/`（M2–M4 已交付） |

## 与外部 harness 的关系：零 wire 兼容

- **native schema 1 是 native loop 的唯一契约。** 该路径只读写 `~/.dpro/sessions/<session-id>.jsonl`（详见
  `docs/native/schema.md`）。旧的 `session.v4.jsonl`（含 `.zstd`）不会被 native reader 接受（`SessionV4Error`），
  不降级、不 dual-write，也不提供 `dpyro migrate` 命令。默认 v4 运行时仍暂时保留，见“已知限制”。
- **配置以 snake_case 为准。** 模型属性、dump 与文档使用 `base_url`、`api_key_env`、`context_window`、
  `max_tokens`、`session_dir` 等名字；0.2.0 为兼容旧文件仍接受 camelCase 别名的**读取**，写入与新增配置
  一律 snake_case，0.3 再移除别名。
- **旧 DSH 规格归档。** `docs/spec/INTERFACES.md`、`docs/spec/session.md`、DSH 事件目录等已冻结/标记待删除；
  状态见 `docs/spec/README.md`。
- **保留的是设计资产，不是协议。** 一切皆插件、声明式 bundle、Service 生命周期、durable-log-first、
  能力贡献、最小权限与可回放这些设计继续保留（ADR D3）。

## native session 与 trace

走 native loop 时，会话事实只有一个来源：`~/.dpro/sessions/<session-id>.jsonl`（可用 `DEEPPYRO_SESSIONS_DIR` 覆盖）。

- **schema 1**：文件首行是封闭 header（`type`、`schema=1`、`id`、`created_at`、`cwd`、`preset`…），之后每行是
  `EventKind` 事件。当前核心事件 27 个，使用 dotted 小写命名（如 `turn.started`、`tool.call`、`compaction.done`）；
  插件事件使用 `plugin.<id>.<event>` 命名空间。
- **只追加、逐条 fsync**：`seq` 严格单调递增；header 原子写；损坏显式拒绝，不做“尽力而为”的修复；
  单写者用 `O_EXCL` 锁。消息以稳定 `message.id` 定位，没有 `surfaceOp`/`sourceEventSeqs`/`ignorable`。
- **fold 派生**：请求消息由纯函数 fold 得到（`session/fold.py`），相同 header+events 必然得到相同视图；
  fork 复制固定事件子集并重排 `seq`，保留 `ts` 与 `message.created_at`。
- **trace 与 session 物理分离**：trace store 默认写 `~/.dpro/traces/<run_id>.jsonl`（`DEEPPYRO_TRACES_DIR` 可覆盖），
  只记录 `run`/`model_call`/`tool_call` 的 hash、token 计数、大小、时延与 id，**不存 prompt 正文、消息正文、工具输出正文**；
  两者通过 `run_id`/`trace_id` 关联（`docs/native/schema.md` §8）。
- **M1/M2 已交付**：native message/session/fold/trace 模块、10 例真实录制 fixtures、native `AgentLoop`
  （ContextPlan、budget/cancel、checkpoint/resume、typed trace）已入库并通过门禁。headless 可用
  `--native-loop` 显式切换；TUI 与未加开关的 headless 仍走 v4，完整切换留给后续版本。

## 快速开始

要求：Python 3.12+、[uv](https://docs.astral.sh/uv/)、Windows/macOS/Linux。

```powershell
uv sync

# 初始化配置（首次运行会生成 ~/.dpro/config.toml）
uv run dpyro

# headless：一次性执行，适合脚本/CI
uv run dpyro --headless "总结这个仓库的目录结构"
uv run dpyro --headless --json "列出三个可改进点" | ConvertFrom-Json

# 管道输入
Get-Content README.md | uv run dpyro --headless "提炼三条要点"
```

配置默认写在 `~/.dpro/config.toml`。最小 provider 配置（snake_case；旧 camelCase 键在 0.2.0 仍可读取）：

```toml
[agent]
provider = "deepseek"
model = "deepseek-chat"

[providers.deepseek]
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
```

默认配置指向本地 OpenAI 兼容端点（`http://127.0.0.1:8000/v1`）。没有本地模型时，请按上面的示例改
`~/.dpro/config.toml`，并设置 `DEEPSEEK_API_KEY`（或以 `DEEPPYRO_CONFIG` 指向另一份配置）。

### CLI 参考

`dpyro [prompt ...]`：无参数进入 Textual 全屏 TUI；带 prompt 或 `--headless` 时一次性执行。

| 参数 | 说明 |
| --- | --- |
| `--headless` | 无 UI 执行；prompt 从位置参数或 stdin 读取 |
| `--json` | 输出 NDJSON 事件流（`session`/`step`/`status`/`text`/`tool_call`/`tool_result`/`usage`/`final`/`error` 等） |
| `--provider` / `--model` | 覆盖配置文件中的 provider/model |
| `--preset` | agent preset（内置 `standard`、`ptc`、`minimal`、`cordis`） |
| `--permission-mode` | `read-only`、`workspace-write`、`danger-full-access` |
| `--approval-policy` | `ask`、`never` |
| `--cwd` | 指定工作目录 |
| `--max-steps` | 单次请求的 step 上限 |
| `--native-loop` | headless 使用 native `AgentLoop`（等价于 `DEEPPYRO_NATIVE_LOOP=1`；默认仍为 v4） |
| `--version` | 打印 `deeppyro-harness <版本>` |

退出码：`0` 成功、`1` 运行时错误、`2` 参数/配置错误、`130` 中断。

插件包管理（在 boot 之前分派）：

```powershell
uv run dpyro plugin list
uv run dpyro plugin info <plugin-id>
uv run dpyro plugin doctor [plugin-id]
uv run dpyro plugin add <目录|wheel|registry spec|git URL> [--force] [--no-deps] [--offline]
uv run dpyro plugin remove <plugin-id> [--force]
uv run dpyro plugin update <plugin-id> [--offline]
uv run dpyro plugin enable|disable <plugin-id>
uv run dpyro plugin allow-version <plugin-id> <version>
```

产品形态只有终端：Textual TUI + headless，不做 Web UI。

## 配置与数据目录

| 路径（`~/.dpro/` 下） | 说明 |
| --- | --- |
| `config.toml` | 用户配置；首次启动生成 |
| `sessions/` | 会话日志。`--native-loop` 路径写 schema 1 的 `<session-id>.jsonl`；默认 v4 路径仍写 `session.v4.jsonl`，两者不混用 |
| `traces/` | 本地 trace store（hash/计数/时延，不含正文） |
| `schedules.json` | 定时任务 |
| `memory/` | runtime memory（`memories.json`、`USER.md`、`MEMORY.md`） |
| `attachments/` | 附件存储（PNG + manifest） |
| `plugins.toml`、`plugins/` | 插件 manifest 与包目录 |
| `history.jsonl` | TUI 输入历史 |

环境变量覆盖：`DEEPPYRO_HOME`、`DEEPPYRO_CONFIG`、`DEEPPYRO_SESSIONS_DIR`、`DEEPPYRO_TRACES_DIR`、
`DEEPPYRO_HISTORY`、`DEEPPYRO_PLUGINS_MANIFEST`、`DEEPPYRO_PLUGINS_DIR`。

## 开发

```powershell
uv sync
uv run pytest                                   # 全量测试
uv run dpyro                                    # 本地跑 TUI
uv run dpyro --headless --provider local "hi"   # 本地端点冒烟

# 只跑 M1 native 契约相关测试
uv run --no-sync pytest tests/core/test_messages.py tests/session tests/observability -q
```

### 四道门禁（与 CI 相同）

提交前必须全绿：

| # | 命令 | 说明 |
| --- | --- | --- |
| 1 | `uv run ruff check src tests` | lint |
| 2 | `uv run ruff format --check src tests` | 格式检查 |
| 3 | `uv run python scripts/typecheck.py` | pyright 棘轮：只允许缩基线，不得新增错误 |
| 4 | `uv run pytest` | 全量测试 |

CI（`.github/workflows/ci.yml`）在 Windows + Python 3.12/3.13 上跑同样四道门禁，**不安装 Node**。
pyright 基线在 `pyright-baseline.json`，`scripts/typecheck.py --write` 只在清理基线时使用。

> 带本机旧 `fixtures/` 时全量 pytest 为 **3690 passed / 0 failed / 0 skipped**（2026-11-17 集成门禁）。
> 干净 checkout 中 `tests/session/test_reader.py` 会在 `fixtures/native/manifest.json` 缺失时优雅 skip，
> 不再在收集期失败；若要跑满旧 fixture 用例，仍需从本机 DSH 数据生成未入库的 `fixtures/`。

### 测试 fixtures

- native 契约的基线 fixtures 已入库：`fixtures/native/`（10 例真实录制，17 次请求、7 次工具调用）与
  `tests/fixtures/native/golden-session.jsonl`（decode→fold→encode 逐字节一致）。
- `tools/gen_fixtures.mjs` 是迁移期保留的旧 DSH 录制生成器（读 `%USERPROFILE%\.dsh/sessions`）。CI 不运行 Node；
  该脚本及其产出随 v4 线格式移除一并退役。旧 `fixtures/manifest.json` 故意不入库：干净 checkout 中
  `tests/session/test_reader.py` 用 `manifest.json` 存在性守卫跳过读旧 fixture 的用例，不再在导入期失败。
- native 的正式基线是已入库的 `fixtures/native/` 与 `tests/fixtures/native/golden-session.jsonl`；
  离线 replay/eval 在 `.github/workflows/native.yml` 非阻塞运行。

## 插件与组合

- 一切能力都是插件行。core bundle（`src/deeppyro/bundles/core/bundle.toml`）当前 42 行，
  `critical=true`；外部行失败进入 `app.failures`，不中断 boot。
- 一个最小插件（当前 API v3；完整指南见 `docs/plugins/README.md`）：

```python
from pydantic import BaseModel

from deeppyro.api import Plugin


class GreetConfig(BaseModel):
    greeting: str = "hello"


class GreetPlugin(Plugin):
    plugin_id = "greet"
    version = "0.1.0"
    api_version = 3
    inject = ("commands",)
    Config = GreetConfig

    def setup(self) -> None:
        self.ctx.register_command("greet", self.run_greet, summary="say hi")

    def run_greet(self, host, argument: str) -> None:
        ...  # handler(host, argument)：host 由命令宿主提供，具体 API 见命令层
```

- 插件可以用 `inject` 声明依赖（服务名字符串或契约类，如 `CommandsPlugin`），用
  `self.ctx.require(SomeService)` / `self.ctx.get(SomeService)` 取 typed service；
  `Service` 子类通过 `provide` 注册新服务。
- bundle 声明（`<bundle>/bundle.toml`）：`[bundle]` 元数据 + `[[plugins]]` 行（`id`、`plugin`、`config`、
  `before/after`、`critical`、`group`、`requires` 等）；层序为 core bundle → 用户 bundles → 显式 bundle →
  `plugins.toml`，同 `id` 的 patch 语义见 `docs/spec/plugin-core.md` §3。
- 插件作者完整指南：`docs/plugins/README.md`；v2→v3 迁移：`docs/plugins/migration-v2-v3.md`。

## 已知限制（0.2.0）

> 完整逐项清单、状态与证据见 [docs/release/0.2.0-known-issues.md](docs/release/0.2.0-known-issues.md)；本节只是发布摘要，冲突以总表为准。

- **native loop 仍是 opt-in**：只有 headless 的 `--native-loop` / `DEEPPYRO_NATIVE_LOOP=1` 走 native schema 1
  与 `AgentLoop`；TUI 和默认 headless 仍走 v4，v4 provider/`core.wire` 尚未删除。这是 0.2.0 最重要的
  发布限制，完整切换排在后续版本。
- **两种会话并存**：native 路径写 `<session-id>.jsonl`，v4 路径仍写 `session.v4.jsonl`；两者不 dual-write、
  不互相读取。备份与迁移时必须区分。
- **插件 API v3 是硬切**：v1/v2 插件、带位置参数的 `setup`、显式 `api_version < 3` 的 bundle 会在加载期
  被拒绝；host 级 `App.service(name)` 等字符串 helper 仅作为弃用的 embedder/test 接口保留。
- **配置兼容窗口**：模型属性/文档已 snake_case，但旧 camelCase TOML 键在 0.2.0 仍可读取（别名）；
  移除别名是 0.3 的 breaking change。
- **无遥测**：0.2.0 不实现任何遥测/上报；本地 trace 是唯一观测数据。约定见 `docs/ops/telemetry.md`。
- **trace 只有 hash 与计数**，不含正文；不实现 OTLP 导出。
- **memory/eval 未闭环**：SQLite FTS long-term memory 未暴露为工具、也未自动写入；eval 只有 fixture/scripted
  runner，没有接 native `AgentSession` 的 live runner；supervisor 未接真实 subagent 生命周期。
- **Windows 沙箱**：`enforcement=partial`（受限 token/ACL），不是完整 OS 级隔离。
- **native 覆盖缺口**：image/approval/compaction/MCP/PTC/subagents/multi-agent handoff 仍未进入 native 差分基线。
- **去品牌未清零**：`src` 仍有约 379 行 `dsh` 命中（大小写不敏感），清理建议见 `docs/release/dsh-audit.md`。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| `docs/native/schema.md` | **当前会话契约**：schema 1 的 header/envelope/EventKind/Message/fold/trace/fork |
| `docs/native/README.md` | native M1–M2 交付与状态账本 |
| `docs/spec/README.md` | 全部规格的状态索引（当前契约 vs legacy DSH v4 vs 待删除） |
| `docs/plugins/README.md` | 插件作者指南（模板、setup、typed inject、bundle、扩展点） |
| `docs/plugins/migration-v2-v3.md` | 插件 API v2→v3 迁移与 0.2.0 破坏性变更 |
| `docs/release/0.2.0.md` | 0.2.0 release note：breaking changes、迁移步骤、门禁、已知限制 |
| `docs/release/0.2.0-known-issues.md` | **0.2.0 已知问题唯一总表**：S1/S2/S3、证据、目标版本、已解决条目 |
| `docs/release/dsh-audit.md` | DSH 去品牌审计：`src` 中 dsh 引用的分类与清理建议 |
| `docs/ops/telemetry.md` | 遥测边界：默认无上报、opt-in 约定 |
| `docs/adr/0001-three-track-native-agent.md` | 三线决策记录（权威） |

## 命名

| 项目 | 值 |
| --- | --- |
| PyPI 包 | `deeppyro-harness` |
| Python 包/import | `deeppyro` |
| CLI | `dpyro`、`deeppyro` |
| 用户配置目录 | `~/.dpro` |
| 源码布局 | `src/deeppyro/` |

