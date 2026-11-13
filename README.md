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
- 会话区上方的**控制栏**用三个下拉直接切换权限预设（read-only / workspace-write /
  danger-full-access）、审批策略（ask / never / always）与工作目录，切换立即作用于当前会话
  （并写入 `permission/preset`、`approval/policy`、`working-directory/change` 记录）。
- 内核直接 vendor 了 [cordis](https://github.com/cordiverse/cordis) 的 Python 移植
  （`src/deeppyro/_vendor/cordis_port`，MIT）：服务、依赖注入、事件总线、fiber 生命周期
  都与 DSH 同构；领域模块不 import cordis，只有 `kernel/` 负责装配。
- **一切皆插件**：DSH 式组合内核——bundle 用 `bundle.toml` 声明插件行，多层 patch 支持
  按 id 覆盖 config（整体替换）、`before/after` 插入、`disabled/critical/isolate/intercept`；
  core bundle 只有 16 行（LLM、会话、沙箱、工具、提示词、命令、agent、插件管理、设置、
  inspect、TUI 槽位），其余能力都由插件贡献。插件可注册工具、提示词段、slash 命令、LLM
  provider、agent preset，以及 TUI 状态栏项 / 对话行 / 面板。
- **运行时自扩展**：`/plugins` 界面与 `plugin_manager` / `plugin_inspect` /
  `settings_read` / `settings_write` 四个工具让 agent 自己安装/启停外部 bundle、读改插件配置、
  查看 live 组合（安装会执行第三方代码，需人工批准；设置 next-boot 生效）。

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
      kernel/   cordis 服务图、composition/bundle/patch、boot（唯一装配层）
      api/      插件契约（Plugin/Service/PluginContext/注册表/preset spec）
      builtin/  core 插件行（llm/sessions/sandbox/tools/prompt/commands/agents/presets/
                plugin_manager/settings/inspect）
      bundles/core/bundle.toml  core bundle 的 16 个插件行
      core/     冻结的 wire 模型 + 运行时协议（共享契约）
      session/  session.v4 日志：模型、append 校验、derive、fork、store
      llm/      provider 适配层（OpenAI-compatible SSE）、重试
      tools/    工具注册表、执行流水线、内置工具
      sandbox/  权限预设 / 策略闸门
      prompt/   系统提示词与运行时上下文装配
      agent/    turn/step 循环、inbox、事件流
      compact/  上下文压缩
      tui/      Textual 全屏 TUI（唯一前端：app/widgets/screens/slots/commands/sink/runtime）
      plugins/  外部插件发现与管理（entry point / 插件目录 / 用户清单 / bundle 安装与设置）
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

TUI 里输入 `/help` 查看 slash 命令（`/new /resume /model /preset /status /compact /clear /plugins /panel /exit`）；
会话区上方控制栏的权限预设 / 审批策略 / 工作目录三个下拉同样立即生效（turn 运行中禁用）；
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

### 插件与组合

- core bundle：`src/deeppyro/bundles/core/bundle.toml`（16 行，`critical = true`）。
- 外部 bundle：`~/.dpro/bundles/<id>/bundle.toml` + Python 文件；`/plugins` 或
  `plugin_manager` 工具可安装/启停/卸载（安装只做结构校验，下次 boot 执行第三方代码）。
- 其他插件来源：entry point 组 `deeppyro.plugins`、`~/.dpro/plugins/<id>/plugin.toml`、
  用户清单 `~/.dpro/plugins.toml` 的 `[[plugins]]` 行。
- 插件可贡献：工具、提示词段（带 preset scope）、slash 命令、LLM provider、agent preset
  （`/preset` 切换）、TUI 状态栏项 / 对话行 / 面板（`/panel`）/ 设置页。
- 配置：`settings` 服务 + `/plugins` 与 `settings_*` 工具；写入带 revision CAS 与 secret
  掩码，next-boot 生效。
- 已知边界：插件进程内运行、加载即信任（无 OS 隔离）；目录来源插件需在 `sys.path` 中或先加载
  plugin-manager 行；外部行失败只降级记录，不中断 boot。

### 测试 fixtures 含个人会话，故意不入库

真实 DSH 会话日志属于个人数据，仓库里不含 `fixtures/`（已 gitignore）。需要跑黄金对照 /
记录类型回归测试时，在本机生成：

    node tools/gen_fixtures.mjs    # 读 %USERPROFILE%\.dsh\sessions，写 ../fixtures/
    # 可用 DSH_SESSIONS / DEEPPYRO_FIXTURES 覆盖输入、输出目录

缺少 fixtures 时，相关测试会自动 skip，其余测试照常运行。
