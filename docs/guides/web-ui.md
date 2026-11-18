# Web UI 使用指南（本地回环宿主）

> **状态**：现行（phase 1） · **层级**：操作指南 · **最后核对**：2026-11-18
> 规格：[spec/web.md](../spec/web.md)。本指南覆盖启动、界面、F1/F2 接线计划、打包与排错。
> 注意：本任务未修改 `src/deeppyro/tui/`，F1/F2 splash 由 lead 在后续集成步骤接线。

## 1. 启动

前提：Python 环境已安装（仓库自带 `.venv`），无额外依赖。

    .venv\Scripts\python.exe -m deeppyro.web --port 0 --cwd .
    # 或指定要恢复的 native 会话：
    .venv\Scripts\python.exe -m deeppyro.web --port 0 --cwd . --session <id>

可选 `--provider P --model M` 用于新会话的模型覆盖；`--host` 默认且只允许回环值。
启动后终端打印（示意）：

    Deeppyro Web 0.1.0
    URL: http://127.0.0.1:54321/?token=<随机令牌>
    Localhost only: bound to 127.0.0.1, never exposed on the network.
    Session: <session-id>
    Press Ctrl-C to stop.

把 URL 原样粘贴到本机浏览器（URL 自带 token；不要只打开 `http://127.0.0.1:<port>/`，
缺 token 会 401）。在页面打开期间保持终端进程运行，Ctrl-C 优雅停止。

## 2. 界面

- 左侧栏：native 会话列表（标题、turn 数、短 id、`read-only`/`damaged` 标记）、
  “New session” 按钮、连接状态点（offline/connecting/ready/live/thinking/retry/offline）。
- 顶栏：当前会话标题与完整 session id、模型、预设、context（已用/窗口）、token 用量。
- 主区：transcript（从 native facts 回放，用户/助手/系统消息，thinking 折叠块，
  工具调用及其结果）；流式文本、工具活动实时更新。
- 底部：输入框、Send、Stop；顶部横幅固定显示 “Localhost only”。

## 3. 快捷键

| 键 | 作用 |
| --- | --- |
| `Ctrl+Enter`（或 `Cmd+Enter`） | 发送消息 |
| `Esc` | 清空输入框并清除提示 |
| `Ctrl-C`（终端） | 停止宿主进程 |

Stop 按钮向宿主发 `POST /interrupt`，由 `NativeAgentHost.interrupt()` 取消当前 turn；
不会杀 socket。队列里排队的消息会一起丢弃（响应里的 `dropped`）。

## 4. F1 / F2 splash 接线计划（后续集成，不在本任务）

TUI 启动 splash 现在仍是 “press any key”。目标形态：F1 = Web，F2 = TUI。届时只需在 TUI
已 boot 好 kernel 的位置加一次调用（TUI 侧改动由 lead 做，本包已提供稳定 API）：

    from deeppyro.web.host import WebHost

    host = await WebHost.start(kernel_app=self.kernel_app, port=0, cwd=self.cwd)
    host.print_banner()                 # 打印带 token 的 URL
    # 可选：webbrowser.open(host.url)
    # 用户选择 F2 时直接不启动；F1 时直到退出再：
    await host.stop()

不用传 `config`/`services`（给了 kernel 就会复用同一个插件内核）；`port=0` 自动选空闲
回环端口。若希望走插件缝（bundle 行由 lead 添加，本任务未改 `bundles/core/bundle.toml`）：

    [[plugins]]
    id = "web"
    plugin = "deeppyro.web.plugin:WebHostPlugin"

    # 可选配置（不会在 setup 期间自动监听）：
    # config = { port = 0, cwd = ".", autostart = false }

然后 `plugin = app.get(WebHostPlugin)`；`host = await plugin.start()`；
`await plugin.stop()`。`WebHostPlugin.setup()` 只注册路由注册表、不监听，符合插件惰性。

## 5. 打包说明

- 服务器只用标准库（asyncio streams、json、hmac、secrets、http 相关工具），没有
  aiohttp/flask/uvicorn/fastapi；`httpx` 只在测试使用（既有 dev 依赖）。
- 前端是 `src/deeppyro/web/assets.py` 中的字符串常量（HTML/CSS/JS），无构建步骤、
  无 CDN、无外部字体，因此单文件可执行体（PyInstaller 等）无需收集静态资产目录。
- 打包后行为不变：启动时打印随机 token 的 loopback URL；token 每进程不同，重启后旧 URL
  失效（这是设计，不是缺陷）。
- 由于只绑回环且不提供 TLS，请勿把该端口转发到局域网/公网；产品定位是“本机 Web 外壳”。

## 6. 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| 页面 401 | URL 缺 `?token=...` 或 token 来自旧进程；用当前 banner 打印的 URL 重开 |
| 403 unexpected Host/Origin | 用了非回环主机名/端口/其他来源；从打印的 `127.0.0.1:<port>` URL 打开 |
| 403 cross-site request refused | 浏览器扩展/代理改写了 Origin 或 Sec-Fetch-Site；本机直连 |
| 409 session is already open | 同一 native 会话被另一个进程（TUI/headless）打开；先关闭它 |
| 409 team mode is not available | native 运行时尚未移植 team 预设，换单 agent 预设 |
| 413 payload too large | 消息超过 100000 字符或 body 超过 1 MiB |
| 429 turn queue is full | 当前 turn 未结束且已排 8 条；等完成或 Stop |
| 503 the web session host is not running | 宿主正在关闭/未启动；重启 `python -m deeppyro.web` |
| Stop 提示 nothing to interrupt | 没有进行中的 turn（正常） |
| 工具调用被拒绝 | phase 1 没有审批 UI，需要审批的工具一律被拒绝；这是已知限制 |
| 断线后自动重连 | 正常：EventSource 带 `Last-Event-ID`，宿主从 seq 续传；transcript 会补全历史 |

## 7. 验证

    .venv\Scripts\python.exe -m pytest tests/web -q
    .venv\Scripts\python.exe -m ruff check src/deeppyro/web tests/web
    .venv\Scripts\python.exe -m ruff format --check src/deeppyro/web tests/web

安全边界、路由与事件契约见 [spec/web.md](../spec/web.md)。
