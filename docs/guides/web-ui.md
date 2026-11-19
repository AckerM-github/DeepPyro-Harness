# Web UI 使用指南（本地回环宿主）

> **状态**：现行（0.3.0 · NiceGUI） · **层级**：操作指南 · **最后核对**：2026-11-19
> 规格：[spec/web.md](../spec/web.md)。本指南覆盖启动（含 TUI splash F1）、界面、命令与设置、
> 审批、打包与排错。

## 1. 启动

前提：Python 环境已安装（仓库自带 `.venv`）。Web 宿主依赖 FastAPI + uvicorn + NiceGUI 4，
由 `uv sync` 安装。

两种进入方式，启动的都是同一个 loopback Web 宿主：

1. **TUI splash**：运行 `dpyro`，提示 `F1 web · F2 terminal`；按 **F1** 后本进程
   直接转为 Web 宿主，打印带 token 的 URL 并尝试用默认浏览器打开（浏览器不可用时
   只打印 URL，不影响宿主运行）。**F2 / Enter** 进入终端 TUI。
   `dpyro --resume <id>` 配合 F1 时会把该 native 会话挂到 Web 宿主。
   TUI 运行时装配失败时 splash 不会关闭：错误行会显示在入口提示上方，仍可按 F1
   直接启动独立的 Web 宿主。
2. **直接 CLI**：

       .venv\Scripts\python.exe -m deeppyro.web --port 0 --cwd .
       # 或指定要恢复的 native 会话：
       .venv\Scripts\python.exe -m deeppyro.web --port 0 --cwd . --session <id>

可选 `--provider P --model M` 用于新会话；`--host` 默认且只允许回环值。启动后终端打印：

    Deeppyro Web 0.3.0
    URL: http://127.0.0.1:54321/?token=<随机令牌>
    Localhost only: bound to 127.0.0.1, never exposed on the network.
    Session: <session-id>
    Press Ctrl-C to stop.

把 URL 原样粘贴到本机浏览器。首次打开会校验 token、设置 HttpOnly cookie，并 303 到不带 token
的干净地址；之后刷新不需要再带 token。保持终端进程运行，Ctrl-C 优雅停止（返回 130）。

## 2. 界面

三栏结构（Discord 密度的原创布局，由 NiceGUI 元素树渲染）：

- **Sidebar（最左）**：顶部品牌区（原创半面罩机器标 + DeepPyro + 工作目录）、搜索、
  Sessions / Commands / Plugins 导航、native 会话列表（标题、turn 数、preset、相对时间、
  active 白点）、归档区、底部用户栏（头像与 Settings 入口）。
- **Stage（主区）**：当前会话标题、preset/model/cwd、运行条、消息流（用户/助手/推理/
  markdown/代码块）、工具卡（PENDING/DONE/ERROR + 参数 + 结果）、审批卡、composer。
- **Inspector（右侧）**：session id、model/provider、preset、turns、权限/审批策略、cwd、
  context 进度、usage（input/output/cached）、runtime 计数、快捷键。

窄屏（≤900px）时侧栏与 inspector 折叠，顶部 52px 顶栏保留品牌、New session、Commands 与 Settings。

加载态、空态、错误态都来自真实 API；没有 mock 数据。断线时连接点变为 offline，重连后按
`lastEventSeq` / timeline 的 `journal_seq` 续订，不重复渲染历史。

## 3. 命令（命令面板与 slash）

按 `Ctrl/Cmd+K` 打开命令面板，或在 composer 输入 `/help`。面板分三组：
Commands（25 个 core command）、Sessions（搜索/跳转）、Settings（分区）。

- **真实执行**：`/new`、`/resume`、`/sessions`、`/rename`、`/archive`、`/unarchive`、
  `/permission`、`/status`、`/plan`、`/goal`、`/cwd`、`/plugins`、`/help`、`/clear`。
- **导航**：`/settings`、`/panel`。
- **只读/说明**：`/provider`、`/model`、`/preset` 无参显示当前值；带参跳设置并提示
  “只影响新会话”。
- **未接 Web 宿主**：`/compact`、`/jobs`、`/team`、`/thinking`、`/hooks` 返回明确错误，
  不假装成功。团队模式请用 TUI/headless。

在 composer 输入以 `/` 开头的文本会走命令，不会发给模型。

### 工作目录（cwd）

每个 native 会话在**创建时**把 cwd 写进会话 header，运行中不改写。要换目录：

1. 点侧栏 SESSIONS 的 **+**、窄屏顶栏的 **+**，或按 `Ctrl/Cmd+N` 打开 **New session** 弹窗；
   输入绝对路径，或从 **Recent workspaces** 选一个最近目录，然后 **Start session**。
   留空则使用宿主启动时的 `--cwd`（默认当前目录）。
2. 或在 composer 输入 `/cwd D:\work\project`：等价地新建并切换到该目录的会话。
3. `/cwd` 不带参数只显示当前会话的目录，不会改变它。

旧会话不会被删除或改写，仍留在侧栏；新会话继承当前 provider/model/preset 与权限策略。
目录不存在或不是目录时弹窗/命令会显示服务端校验错误。

## 4. 设置

设置是全屏路由 `/settings`（侧栏 Settings 或 `Ctrl/Cmd+,`），分区：

- **Provider & models**：provider/model 选择、Base URL、API key（只写不读，显示 `api_key_set`）、
  温度、max tokens、连接测试；保存后校验并原子写 `~/.dpro/config.toml`，生成编号 `.bak`。
- **Agent behavior / Permissions**：默认 preset、权限模式、审批策略；运行中会话也可通过
  `/permission` 调整。
- **Commands**：25 个 core command 的真实注册表视图。
- **Plugins**：运行时插件行、状态、插件设置 schema 表单（live 字段热应用，restart 字段标记重启）。
- **Sessions / Web host / About**：会话计数、回环地址、版本与 Python/平台信息。

写 config 不会热改已启动的 `KernelConfig`；页面明确提示新 turn/重启后完全生效。

## 5. 工具审批

需要更高权限的工具（例如写 workspace 之外的路径）会让 turn 挂起：

1. 页面出现审批卡：工具名、摘要、目标路径/原因、**Allow once** / **Deny**。
2. 工具卡保持 **PENDING**，不会误显示 missing-result 错误。
3. 点 Allow 后工具继续执行；Deny 后工具得到拒绝并收尾。
4. 超时（默认 300 秒）或 turn 结束会清理 pending，不会永久卡住。

审批决策只通过本机 API 发送，不经过模型。

## 6. 快捷键

| 键 | 作用 |
| --- | --- |
| `Ctrl/Cmd+K` 或 `?` | 命令面板 |
| `Ctrl/Cmd+,` | 设置 |
| `Ctrl/Cmd+N` | 新建会话（选择工作目录） |
| `Enter` | 发送 |
| `Shift+Enter` | 换行 |
| `Esc` | 关闭面板/设置/新建会话弹窗 |
| `Ctrl-C`（终端） | 停止宿主进程 |

Stop 按钮向宿主发 `POST /interrupt`，由 `NativeAgentHost.interrupt()` 取消当前 turn；
不会杀 socket。队列里排队的消息会一起丢弃（响应里的 `dropped`）。

## 7. 打包说明

- 服务器是 FastAPI + uvicorn + NiceGUI 4，只绑回环；页面是 Python 元素树，浏览器侧由
  NiceGUI 自带的 Quasar/Vue 渲染，无前端构建步骤、无 CDN。
- PyInstaller 等单文件打包需收集 `src/deeppyro/web/ui/assets/`（`style.css`、`chat.css`、
  `settings.css`、`cyborg-mark.svg`、`InterVariable.woff2`），并让 NiceGUI 的静态资源随包分发。
  FastAPI/uvicorn/NiceGUI 都是普通 Python 依赖。
- 打包后行为不变：启动打印随机 token 的 loopback URL；token 每进程不同，重启后旧 URL 失效。
- 只绑回环且不提供 TLS，请勿把端口转发到局域网/公网。

## 8. 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| 页面 401 | URL 缺 `?token=...` 或 token 来自旧进程；用当前 banner 的 URL 重开 |
| 403 unexpected Host/Origin | 用了非回环主机名/端口/其他来源；从 `127.0.0.1:<port>` 打开 |
| 409 session is already open | 同一 native 会话被另一个进程打开；先关闭它（死进程锁会自动回收） |
| 409 team mode is not available | web 运行时拒绝 team 预设；团队请用 TUI/headless |
| 409 stale settings revision | 设置被另一个窗口改过；点 Reload 后重试 |
| 413 payload too large | 消息超过 100000 字符或 body 超过 1 MiB |
| 429 turn queue is full | 当前 turn 未结束且已排 8 条；等完成或 Stop |
| 503 the web session host is not running | 宿主正在关闭/未启动；重启 `python -m deeppyro.web` |
| 审批卡一直 PENDING | 点 Allow once / Deny；或等 300 秒超时 |
| 设置保存后当前会话没变 | 运行中的 KernelConfig 不热重载；开新会话或重启宿主 |
| 新建会话的目录不是想要的 | cwd 在会话创建时固定；用 New session 弹窗或 `/cwd <path>` 新建，旧会话保留 |
| 短暂显示 “Connection lost.” | 宿主 Socket.IO 心跳窗口为 15 秒（ping 12s / timeout 6s）；首屏卡顿不应再触发。若持续出现，确认宿主进程还在、端口未变 |
| 断线后自动重连 | 正常：EventSource 带 `Last-Event-ID` 或 `after=`，从 seq 续传 |

## 9. 验证

    .venv\Scripts\python.exe -m pytest tests/web -q
    .venv\Scripts\python.exe -m ruff check src/deeppyro/web tests/web
    .venv\Scripts\python.exe -m ruff format --check src/deeppyro/web tests/web

安全边界、路由与事件契约见 [spec/web.md](../spec/web.md)。
