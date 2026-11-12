# DeepPyro Harness (deeppyro)

Terminal-first **pure Python** port of DeepSeek Harness (DSH) core.

- 纯 Python，运行时不依赖 Node/Electron。
- 会话日志**逐字段兼容** DSH `session.v4.jsonl`（读取 / 追加 / 派生 / fork）。
- 内置工具语义对齐 DSH（read/write/edit/glob/grep/pwsh/bash/web/todo/jobs…）。
- 交付形态：**终端 only** —— 一次性 CLI（`dpyro run`）与交互式 TUI（`dpyro chat`）。

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
  core/     冻结的 wire 模型 + 运行时协议（共享契约，Lead 维护）
  session/  session.v4 日志：模型、append 校验、derive、fork、store
  llm/      provider 适配层（OpenAI-compatible SSE）、重试
  tools/    工具注册表、执行流水线、内置工具
  sandbox/  权限预设 / 策略闸门
  prompt/   系统提示词与运行时上下文装配
  agent/    turn/step 循环、inbox、事件流
  compact/  上下文压缩
  cli/      typer 入口（`dpyro run`、`dpyro chat`、`dpyro sessions`）
  tui/      rich 交互式终端界面
docs/spec/  契约与逆向出的 DSH 语义
tests/      单测 + 黄金对照测试
tools/      fixture 生成器（Node，只读 ~/.dsh）
fixtures/   解码后的真实会话样本
```

## 开发

```
uv sync
uv run pytest
uv run dpyro run "list python files"
uv run dpyro chat --resume <session-id>
```
