# DeepPyro Harness (deeppyro)

Terminal-first **pure Python** port of DeepSeek Harness (DSH) core.

- 纯 Python，运行时不依赖 Node/Electron。
- 会话日志**逐字段兼容** DSH `session.v4.jsonl`（读取 / 追加 / 派生 / fork）。
- 内置工具语义对齐 DSH（read/write/edit/glob/grep/pwsh/bash/web/todo/jobs…）。
- 交付形态：**终端 only** —— 一次性 CLI（`dpyro run`）与交互式 TUI（`dpyro chat`）。
- 内核直接 vendor 了 [cordis](https://github.com/cordiverse/cordis) 的 Python 移植（`src/deeppyro/_vendor/cordis_port`，MIT）：服务、依赖注入、事件总线、fiber 生命周期都与 DSH 同构；领域模块不 import cordis，只有 `kernel/` 负责装配。

## 命名

| 用途 | 值 |
|---|---|
| 仓库目录 | `D:\code\deeppyro` |
| 发行名 (PyPI) | `deeppyro-harness` |
| import 名 | `deeppyro` |
| CLI 命令 | `dpyro` / `deeppyro` |
| 配置目录 | `%APPDATA%\deeppyro` |

## 布局

```
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
  cli/      唯一的终端前端：typer 入口 + rich 渲染 + 交互循环（`dpyro run/chat/sessions/config`）
  tui/      兼容别名（零实现，re-export `deeppyro.cli`）
docs/spec/  契约与逆向出的 DSH 语义
tests/      单测 + 黄金对照测试
tools/      fixture 生成器（Node，只读 ~/.dsh）
fixtures/   解码后的真实会话样本
```

## 开发

```powershell
uv sync
uv run pytest

# 首次运行会自动生成 %APPDATA%\deeppyro\config.toml（默认 provider: local）
uv run dpyro config show
uv run dpyro run "列出 pyproject.toml 里的依赖"
uv run dpyro run --json "1+1=?"        # NDJSON 事件流
uv run dpyro chat                      # 交互式 TUI；/new /sessions /exit
uv run dpyro chat --resume <id>
uv run dpyro sessions
```

插件清单（可选）：`%APPDATA%\deeppyro\plugins.toml`，用 `[[plugins]]` + `id/disabled/config` 覆盖内置
插件（`frontend / llm / sessions / sandbox / tools / agents`），也支持 `module:Class` 自带插件。
