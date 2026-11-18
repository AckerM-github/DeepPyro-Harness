# DeepPyro Harness (deeppyro)

**纯 Python、原生（native-only）的本地 agent harness**：同一个 native 运行时驱动终端 TUI、headless 与
仅回环的 Web UI。0.3.0 删除了旧 v4 会话栈与全部兼容开关，插件化、Python 化、AI 原生是三条产品主线。

> **0.3.0 状态**：native-only 硬切完成。TUI、headless、Web 三条入口都读写 schema 1 的
> `~/.dpro/sessions/<id>.jsonl`；`--legacy-v4`、`--native-loop`、`DEEPPYRO_NATIVE_LOOP`、
> `DEEPPYRO_TUI_NATIVE` 与 `dpyro migrate` 均已删除。旧 v4 会话不再可读，升级前请保留备份。
> 设计原因见 [ADR 0001](docs/adr/0001-three-track-native-agent.md)，逐项状态见
> [0.3.0 release note](docs/release/0.3.0.md)。

## 三条主线

| 线 | 目标 | 落地位置 |
| --- | --- | --- |
| **Python 化** | 一种会话格式、一个消息模型、一套事件类型；dataclass/Protocol/StrEnum/TaskGroup，不复刻 TypeScript 形状 | `src/deeppyro/core/messages.py`、`session/schema.py`、`session/fold.py`、`agent/loop.py` |
| **插件化** | 一切能力经插件贡献；无参 `setup(self)`、typed `inject`/`ctx.get`、声明式 bundle、能力清单 | `src/deeppyro/api/`、`kernel/`、`plugins/`、`bundles/` |
| **AI 原生** | 会话只记事实；token/时延/重试进本地 trace；可回放、可断点续跑、可评测 | `observability/`、`context/`、`eval/`、`memory/` |

## 三个入口，一个运行时

| 入口 | 用途 | 启动方式 |
| --- | --- | --- |
| **TUI** | 日常交互、会话管理、team/subagent、goal、hooks、PTC | `dpyro`（默认终端） |
| **Web UI** | 浏览器外壳；只监听 `127.0.0.1`，单页无构建 | TUI 启动页按 **F1**，或 `python -m deeppyro.web` |
| **headless** | 脚本/CI；人类摘要或 NDJSON | `dpyro --headless "..."` |

启动页：**F1 = Web，F2 / Enter = 终端**。Web 进程由宿主显式启动，不监听时只是一个惰性插件；
页面只负责发命令、订阅 SSE、回放 transcript，所有会话状态都在 append-only 日志里。

```powershell
uv sync
uv run dpyro                       # TUI
uv run dpyro --headless "总结这个仓库的目录结构"
uv run python -m deeppyro.web --port 0 --cwd .
```

Web 默认绑定随机回环端口并打印带一次性 bearer token 的 URL；`--host` 只接受 `127.0.0.1`/`localhost`，没有 TLS、没有远程绑定选项。详见 [Web UI 指南](docs/guides/web-ui.md)。

## native session 与 trace

- **schema 1**：封闭 header + 30 个核心 `EventKind`（dotted 小写名），插件事件用
  `plugin.<id>.<event>` 命名空间。消息以稳定 `message.id` 定位；没有 `surfaceOp`、
  `sourceEventSeqs`、`ignorable`。
- **只追加、逐条 fsync**：`seq` 严格单调；header 原子写；损坏显式拒绝；单写者用 `O_EXCL` 锁。
- **fold 派生**：请求消息由纯函数 fold 得到；fork 复制固定事件子集并重排 `seq`。
- **trace 与 session 物理分离**：trace 默认写 `~/.dpro/traces/<run_id>.jsonl`，只记 `run`/
  `model_call`/`tool_call` 的 hash、token 计数、大小、时延与 id，不存 prompt/消息/工具输出正文。
- **usage** 写 `~/.dpro/usage/usage.jsonl`；只有显式配置 `observability.otlp` 才导出 OTLP/HTTP(JSON)。

完整契约见 [docs/native/schema.md](docs/native/schema.md)。

## 快速开始

要求：Python 3.12+、[uv](https://docs.astral.sh/uv/)、Windows/macOS/Linux。

```powershell
uv sync

# 首次运行会生成 ~/.dpro/config.toml
uv run dpyro

# headless：一次性执行，适合脚本/CI
uv run dpyro --headless "总结这个仓库的目录结构"
uv run dpyro --headless --json "列出三个可改进点" | ConvertFrom-Json

# 管道输入
Get-Content README.md | uv run dpyro --headless "提炼三条要点"
```

最小 provider 配置（snake_case；camelCase 键直接报错）：

```toml
[agent]
provider = "deepseek"
model = "deepseek-chat"

[providers.deepseek]
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
```

默认配置指向本地 OpenAI 兼容端点（`http://127.0.0.1:8000/v1`）。没有本地模型时，按上面的示例改
`~/.dpro/config.toml`，并设置 `DEEPSEEK_API_KEY`（或用 `DEEPPYRO_CONFIG` 指向另一份配置）。

### CLI 参考

`dpyro [prompt ...]`：无参数进入 Textual TUI；带 prompt 或 `--headless` 时一次性执行。

| 参数 | 说明 |
| --- | --- |
| `--headless` | 无 UI 执行；prompt 从位置参数或 stdin 读取 |
| `--json` | 输出 NDJSON 事件流（`session`/`step`/`status`/`text`/`tool_call`/`tool_result`/`usage`/`final`/`error`） |
| `--provider` / `--model` | 覆盖配置文件中的 provider/model |
| `--preset` | agent preset（内置 `standard`、`ptc`、`minimal`、`cordis`） |
| `--permission-mode` | `read-only`、`workspace-write`、`danger-full-access` |
| `--approval-policy` | `ask`、`never` |
| `--cwd` | 指定工作目录 |
| `--max-steps` | 单次请求的 step 上限 |
| `--resume ID` | 继续一个 native 会话 |
| `--version` | 打印 `deeppyro-harness <版本>` |

退出码：`0` 成功、`1` 运行时错误、`2` 参数/配置错误、`130` 中断。

插件包管理（boot 之前分派）：

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

## 配置与数据目录

| 路径（`~/.dpro/` 下） | 说明 |
| --- | --- |
| `config.toml` | 用户配置；首次启动生成 |
| `sessions/` | native 会话日志：schema 1 的 `<session-id>.jsonl` |
| `traces/` | 本地 trace store（hash/计数/时延，不含正文） |
| `usage/` | 本地 usage 账本 `usage.jsonl` |
| `schedules.json` | 定时任务 |
| `memory/` | runtime memory（`memories.json`、`USER.md`、`MEMORY.md`） |
| `attachments/` | 附件存储（PNG + manifest） |
| `plugins.toml`、`plugins/` | 插件 manifest 与包目录 |
| `history.jsonl` | TUI 输入历史 |

环境变量覆盖：`DEEPPYRO_HOME`、`DEEPPYRO_CONFIG`、`DEEPPYRO_SESSION_DIR`、`DEEPPYRO_TRACES_DIR`、
`DEEPPYRO_USAGE_DIR`、`DEEPPYRO_HISTORY`、`DEEPPYRO_PLUGINS_MANIFEST`、`DEEPPYRO_PLUGINS_DIR`。

## 开发

```powershell
uv sync
uv run pytest                                   # 全量测试
uv run dpyro                                    # 本地跑 TUI
uv run dpyro --headless --provider local "hi"   # 本地端点冒烟
```

### 四道门禁（与 CI 相同）

| # | 命令 | 说明 |
| --- | --- | --- |
| 1 | `uv run ruff check src tests` | lint |
| 2 | `uv run ruff format --check src tests` | 格式检查 |
| 3 | `uv run python scripts/typecheck.py` | pyright 棘轮：只允许缩基线，不得新增错误 |
| 4 | `uv run pytest` | 全量测试 |

CI（`.github/workflows/ci.yml`）在 Windows + Python 3.12/3.13 上跑同样四道门禁，不安装 Node。
native 差分/replay 在 `.github/workflows/native.yml` 中阻塞运行。

> 0.3.0 发布门禁的实测数字（测试数、pyright 基线、耗时）见
> [docs/release/0.3.0.md](docs/release/0.3.0.md) §4。

### 测试 fixtures

- native 基线 fixtures 已入库：`fixtures/native/`（10 例真实录制，17 次请求、7 次工具调用）与
  `tests/fixtures/native/golden-session.jsonl`（decode→fold→encode 逐字节一致）。
- 合成差分矩阵覆盖 image、approval、compaction、MCP、PTC、subagent、handoff 七类形状；
  这些是形状覆盖，不替代真实录制。
- CI 不运行 Node；旧的录制生成器已随 v4 删除。

## 插件与组合

- 一切能力都是插件行。core bundle（`src/deeppyro/bundles/core/bundle.toml`）当前 **41 个插件行**
  （另有 `[bundle]` 元数据），`critical=true` 的行失败会阻止 boot；外部行失败进入 `app.failures`，不中断 boot。
- 一个最小插件（API v3；完整指南见 `docs/plugins/README.md`）：

```python
from pydantic import BaseModel

from deeppyro.api import Plugin


class GreetConfig(BaseModel):
    greeting: str = "hello"


class GreetPlugin(Plugin):
    plugin_id = "greet"
    version = "0.3.0"
    api_version = 3
    inject = ("commands",)
    Config = GreetConfig

    def setup(self) -> None:
        self.ctx.register_command("greet", self.run_greet, summary="say hi")

    def run_greet(self, host, argument: str) -> None:
        ...  # handler(host, argument)
```

- 插件可以用 `inject` 声明依赖（服务名字符串或契约类，如 `CommandsPlugin`），用
  `self.ctx.require(SomeService)` / `self.ctx.get(SomeService)` 取 typed service；
  `Service` 子类通过 `provide` 注册新服务。
- bundle 声明：`[bundle]` 元数据 + `[[plugins]]` 行（`id`、`plugin`、`config`、
  `before/after`、`critical`、`group`、`requires` 等）；层序为 core bundle → 用户 bundles →
  显式 bundle → `plugins.toml`，同 `id` 的 patch 语义见 `docs/spec/plugin-core.md` §3。
- 插件作者指南：`docs/plugins/README.md`；v2→v3 迁移：`docs/plugins/migration-v2-v3.md`。

## 已知限制（0.3.0）

> 逐项清单、边界与证据见 [0.3.0 release note](docs/release/0.3.0.md) §5。

- **native-only 是硬切**：旧 `session.v4.jsonl`（含 `.zstd`）不再读取，也没有迁移命令；需要旧对话请从备份恢复。
- **插件 API v3 是硬切**：v1/v2 插件、带位置参数的 `setup`、`api_version < 3` 的 bundle 在加载期被拒绝；
  host 级只接受契约类（`App.get(Contract)` / `App.require(Contract)`）。
- **配置严格 snake_case**：`load_config` 对 camelCase 键与 schema 表内未知键直接抛 `ConfigError`；
  CLI 首次启动会把 0.2.x 旧配置原地升级并写 `.bak` 备份（见 `docs/guides/config-migration.md`）。
  顶层插件 section 与文档化的 adapter 选项是仅有的开放边界。
- **Web UI 是本地单用户外壳**：只绑定回环、明文 HTTP、无 TLS、单 attached session、无审批 UI；
  SSE journal 在进程内，重启后靠 transcript 回放。
- **观测默认本地**：usage/trace 只写本机，不含正文；OTLP 导出默认关闭。
- **memory 仍是 SQLite FTS5**：有 `memory_search`/`memory_add`，无 embedding/向量检索。
- **Windows 沙箱**：`enforcement=partial`（受限 token/ACL），不是完整 OS 级隔离。
- **合成差分不是录制**：七类覆盖证明两侧形状一致，真实 provider 行为仍以录制基线为准。
- **插件平台边界**：bundle 签名只证明共享密钥持有者签发过字节；没有 OS 沙箱/进程内隔离；
  group 子行只能改既有子行；TUI slot 默认 append-only，替换需显式 opt-in。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| `docs/README.md` | **文档总入口**：阅读顺序、权威性分层、目录地图、状态标签 |
| `docs/native/schema.md` | **当前会话契约**：header/envelope/EventKind/Message/fold/trace/fork |
| `docs/native/README.md` | native 交付、录制基线、验证与状态账本 |
| `docs/spec/web.md` | Web 宿主规格：路由、SSE、安全模型 |
| `docs/guides/web-ui.md` | Web UI 使用与安全边界 |
| `docs/spec/README.md` | 全部规格的状态索引 |
| `docs/plugins/README.md` | 插件作者指南（模板、setup、typed inject、bundle、扩展点） |
| `docs/release/0.3.0.md` | 0.3.0 release note：破坏性变更、验收门禁、已知限制 |
| `docs/adr/0001-three-track-native-agent.md` | 三线决策记录（权威） |

## 命名

| 项目 | 值 |
| --- | --- |
| PyPI 包 | `deeppyro-harness` |
| Python 包/import | `deeppyro` |
| CLI | `dpyro`、`deeppyro` |
| 用户配置目录 | `~/.dpro` |
| 源码布局 | `src/deeppyro/` |
