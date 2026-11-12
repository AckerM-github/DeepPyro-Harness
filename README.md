# DeepPyro Harness (deeppyro)

Terminal-first **pure Python** port of DeepSeek Harness (DSH) core.

- 纯 Python，运行时不依赖 Node/Electron。
- 会话日志**逐字段兼容** DSH `session.v4.jsonl`（读取 / 追加 / 派生 / fork）。
- 内置工具对齐 DSH 语义：read/write/edit/glob/grep/pwsh/bash/web_search/web_fetch/todo_write/present。
- 产品形态：**只有一套终端前端** —— Textual 全屏 TUI（`dpyro`，唯一交互形态）+
  headless 一次性模式（`dpyro --headless "prompt"`，支持 `--json` NDJSON，供脚本/CI）。
  不做 Web UI。
- 启动页是 FIGlet `ansi_shadow` 像素字标（火色 / 冰蓝渐变、逐行揭示）；思考过程用单行滚动 ticker
  实时展示最新一句，回答一开始就折叠成一行，点击可展开全文。
- 内核直接 vendor 了 [cordis](https://github.com/cordiverse/cordis) 的 Python 移植
  （`src/deeppyro/_vendor/cordis_port`，MIT）：服务、依赖注入、事件总线、fiber 生命周期
  都与 DSH 同构；领域模块不 import cordis，只有 `kernel/` 负责装配。

## 命名

| 用途 | 值 |
|---|---|
| 仓库目录 | `D:\code\deeppyro` |
| 发行名 (PyPI) | `deeppyro-harness` |
| import 名 | `deeppyro` |
| CLI 命令 | `dpyro` / `deeppyro`（同一个 TUI 入口） |
| 配置目录 | `~/.dpro`（Windows: `C:\Users\<user>\.dpro`，单目录存放全部用户级状态） |

## 布局

    src/deeppyro/
      _vendor/cordis_port/  vendored cordis Python 内核（MIT，不改动）
      kernel/   cordis 服务图、插件清单、boot（唯一装配层）
      core/     冻结的 wire 模型 + 运行时协议（共享契约）
      session/  session.v4 日志：模型、append 校验、derive、fork、store
      llm/      provider 适配层（OpenAI-compatible SSE）、重试
      tools/    工具注册表、执行流水线、内置工具
      sandbox/  权限预设 / 策略闸门
      prompt/   系统提示词与运行时上下文装配
      agent/    turn/step 循环、inbox、事件流
      compact/  上下文压缩
      tui/      Textual 全屏 TUI（唯一前端：app/widgets/screens/commands/sink/runtime）
      headless.py  一次性 headless（run_once / NDJSON+rich sink，自包含）
      paths.py  用户目录解析（~/.dpro、DEEPPYRO_HOME）与旧数据迁移
    docs/spec/  契约与逆向出的 DSH 语义（INTERFACES / tui / headless / session / llm / tools / loop / plugins）
    tests/      单测 + 黄金对照测试
    tools/      fixture 生成器（Node，只读 ~/.dsh）

## 开发

    uv sync
    uv run pytest

    # 首次运行会在 ~/.dpro 生成 config.toml（默认 provider: local）
    uv run dpyro                                  # 打开 TUI（唯一交互形态）
    uv run dpyro --headless "列出 pyproject.toml 里的依赖"
    uv run dpyro --headless --json "1+1=?"        # NDJSON 事件流
    echo "1+1=?" | uv run dpyro --headless --json # stdin 管道

TUI 里输入 `/help` 查看 slash 命令（`/new /resume /model /status /compact /clear /plugins /exit`）；
`Ctrl+C` 在 turn 中取消当前 turn，空闲时清空输入，再按退出；`Ctrl+D` 退出。

### 配置与数据目录

所有用户级状态集中在一个目录：默认 `~/.dpro`（Windows 为 `C:\Users\<user>\.dpro`），
结构对齐 DSH 的 `~/.dsh`：

| 路径 | 内容 |
|---|---|
| `config.toml` | provider / model / agent 配置（首次运行自动生成） |
| `sessions/` | `session.v4.jsonl` 会话日志 |
| `plugins.toml` | 用户插件清单 |
| `plugins/` | 外部插件目录 |
| `history.jsonl` | TUI 输入历史 |

- `DEEPPYRO_HOME` 覆盖整个目录；`DEEPPYRO_CONFIG` / `DEEPPYRO_SESSION_DIR` / `DEEPPYRO_HISTORY` /
  `DEEPPYRO_PLUGINS_MANIFEST` / `DEEPPYRO_PLUGINS_DIR` 覆盖单项路径。
- 旧版本散落在 `%APPDATA%\deeppyro` / `%LOCALAPPDATA%\deeppyro` 的数据，首次运行会自动复制到
  `~/.dpro`（只补缺失文件、不覆盖已有文件、不删除旧目录）。

插件清单（可选）：`~/.dpro/plugins.toml`，用 `[[plugins]]` + `id/disabled/config`
覆盖内置插件（`llm / sessions / sandbox / tools / agents`），也支持 `module:Class` 自带插件；
TUI 的 `/plugins` 管理外部插件（entry point / 用户目录 plugin.toml / 用户清单）。

### 测试 fixtures 含个人会话，故意不入库

真实 DSH 会话日志属于个人数据，仓库里不含 `fixtures/`（已 gitignore）。需要跑黄金对照 /
记录类型回归测试时，在本机生成：

    node tools/gen_fixtures.mjs    # 读 %USERPROFILE%\.dsh\sessions，写 ../fixtures/
    # 可用 DSH_SESSIONS / DEEPPYRO_FIXTURES 覆盖输入、输出目录

缺少 fixtures 时，相关测试会自动 skip，其余测试照常运行。
