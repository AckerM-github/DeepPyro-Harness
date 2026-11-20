# Web UI 使用指南（本地回环宿主）

> **状态**：现行（0.3.0 · React SPA） · **层级**：操作指南 · **最后核对**：2026-11-20
> 规格：[spec/web.md](../spec/web.md)。本指南覆盖启动（含 TUI splash F1）、界面、命令与设置、
> 审批、打包与排错。

## 1. 启动

前提：Python 环境已安装（仓库自带 `.venv`）。Web 宿主只依赖 FastAPI + uvicorn，由 `uv sync`
安装；浏览器端是已构建好的 React/Vite SPA（产物已提交在 `src/deeppyro/web/ui/static/`），
运行不需要 Node.js。只有修改 `web/` 下的前端源码时才需要 Node.js 20.19+ 与 pnpm（见 §7）。

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

conversation-first 工作台（左栏会话、中栏对话、右侧详情默认收起）：

- **Sidebar（最左）**：品牌区（原创半面罩机器标 + DeepPyro）、**New session** 按钮、
  会话搜索框、会话列表（标题、turn 数、preset、相对时间、active 白点，行尾 `⋯` 菜单提供
  **Rename / Archive**；双击标题也可改名）、底部的 **Show archived** 与 **Load more**、
  用户栏与 Settings 入口。会话列表分页加载，归档会话默认隐藏，勾选后以
  `Archived · N turns` 显示。
- **Stage（主区）**：顶栏只有会话标题、非 live 时的连接状态、运行中的 `Working…`，
  以及 Reload / Session details 两个图标；中间是消息流（用户消息右侧气泡；助手消息无头像、
  无时间戳；推理与工具调用收进 `Used N tools` / `Working…` 折叠过程层，展开后是按工具
  语义化的卡片：命令用终端卡、改文件显示 Before/After、搜索按文件分组、读文件带行号）；
  底部是 composer 栈：审批条（`Permission needed`）、排队条（`Queued · N`）、计划进度条、
  composer 卡片（模型菜单、权限 preset、上下文用量、发送/Stop 按钮）。空会话显示
  `What should we build?` 与工作区建议。
- **Session details（右侧，默认关闭）**：点顶栏 info 图标打开覆盖式抽屉，显示模型/Provider/
  preset/工作区、上下文用量进度、本会话 token 用量、权限 preset 与 Manage permissions 入口；
  点 scrim、关闭按钮或 `Esc` 收起。

窄屏（<768px）时侧栏收进抽屉（顶栏菜单按钮打开），Session details 变成全屏抽屉；
composer 仍保留模型与权限菜单。

加载态、空态、错误态都来自真实 API；没有 mock 数据。断线时顶栏出现 reconnecting/offline，
重连后按 `lastEventSeq` / timeline 的 `journal_seq` 续订，不重复渲染历史。

`Ctrl/Cmd+K` 打开**快速切换器**：默认列出会话与 Actions（New session / Settings）；输入以
`/` 开头才显示命令列表，命令直接执行，不再有参数步骤。composer 左侧的命令图标打开同一个
切换器。

- **真实执行**：`/new`、`/resume`、`/sessions`、`/rename`、`/archive`、`/unarchive`、
  `/permission`、`/status`、`/plan`、`/goal`、`/cwd`、`/plugins`、`/help`。
- **导航**：`/settings`、`/panel`。
- **明确拒绝**：`/clear` 不执行——transcript 是持久会话日志且总会重放，输入它会提示用
  `/new` 或侧栏新建会话。
- **只读/说明**：`/provider`、`/model`、`/preset` 无参显示当前值；带参跳设置并提示
  “只影响下一 turn”。
- **未接 Web 宿主**：`/compact`、`/jobs`、`/team`、`/thinking`、`/hooks` 返回明确错误，
  不假装成功。团队模式请用 TUI/headless。

每个 native 会话在**创建时**把 cwd 写进会话 header，运行中不改写。要换目录：

1. 点侧栏 **New session**、或按 `Ctrl/Cmd+N` 打开 **New session** 弹窗：**Folder** 行
   显示当前工作区，点 **Choose…** 打开目录选择器（只列目录，可上溯、可切 Home/Workspace 根）；
   也可以直接从 **Recent folders** 选最近用过的目录。填可选 **Title**、选 **Preset** 后点
   **Start session**；留空目录则使用宿主启动时的 `--cwd`（默认当前目录）。
2. 或在 composer 输入 `/cwd D:\work\project`：等价地新建并切换到该目录的会话。
3. `/cwd` 不带参数只显示当前会话的目录，不会改变它。

旧会话不会被删除或改写，仍留在侧栏；新会话继承当前 provider/model/preset 与权限策略。
目录不存在或不可读时，选择器与弹窗内显示服务端校验错误，不会静默失败。

会话行菜单的 **Rename**（或双击标题）可直接改标题，等价于 `/rename`，只改会话 header，
不重写历史。

归档/取消归档都从会话行尾的 `⋯` 菜单触发。归档 active 会话时后端先创建替代会话再归档，
UI 自动跳到替代会话；取消归档后可在 **Show archived** 结果里看到它恢复，刷新页面状态保持。
触屏/窄屏下 `⋯` 按钮常显，不依赖 hover。

## 4. 设置

设置是覆盖层（侧栏 Settings 或 `Ctrl/Cmd+,` 打开，Esc 关闭），不是独立路由；打开时从
`GET /api/ui/settings` 读取当前配置，分区：

- **Provider & models**：provider/model 选择、Base URL、API key（只写不读，显示 `api_key_set`）、
  温度、max tokens、连接测试；保存后校验并原子写 `~/.dpro/config.toml`，生成编号 `.bak`。
- **Agent behavior / Permissions**：默认 preset、权限模式、审批策略；运行中会话直接在
  composer 的权限菜单（或 Session details 抽屉）调整，下一 turn 生效。
- **Commands**：25 个 core command 的真实注册表视图。
- **Plugins**：运行时插件行、状态、插件设置 schema 表单（live 字段热应用，restart 字段标记重启）。
- **Sessions / Web host / About**：会话计数、回环地址、版本与 Python/平台信息。

写 config 不会热改已启动的 `KernelConfig`；页面明确提示新 turn/重启后完全生效。

## 5. 工具审批

需要更高权限的工具（例如写 workspace 之外的路径）会让 turn 挂起：

1. composer 上方出现审批条 `Permission needed · <动作>`：摘要、目标路径/原因，以及
   **Allow once** / **Allow for this session** / **Deny** 三个按钮。
2. 等待中的工具卡保持 **Running**，不会误显示 missing-result 错误。
3. **Allow once** 只放行这一次；**Allow for this session** 对同一会话的同一工具自动放行，
   直到会话 detach 或宿主重启（放行集合只在内存里）；**Deny** 让工具得到拒绝并收尾。
4. 超时（默认 300 秒）或 turn 结束会清理 pending，不会永久卡住。

审批决策只通过本机 API 发送，不经过模型。

## 6. 快捷键

| 键 | 作用 |
| --- | --- |
| `Ctrl/Cmd+K` | 快速切换器（会话/Actions；输入 `/` 时列命令） |
| `Ctrl/Cmd+,` | 设置 |
| `Ctrl/Cmd+N` | 新建会话 |
| `Enter` | 发送（turn 未结束时进入队列） |
| `Shift+Enter` | 换行 |
| `Esc` | 关闭抽屉/切换器/设置/新建会话弹窗 |
| `Ctrl-C`（终端） | 停止宿主进程 |

Stop 按钮向宿主发 `POST /interrupt`，由 `NativeAgentHost.interrupt()` 取消当前 turn；
不会杀 socket。队列里排队的消息会一起丢弃（响应里的 `dropped`）。turn 未结束时 `Enter`
发送的消息进入队列（`Queued · N` 条），可逐条删除；上限 8 条，满后返回 429。

## 7. 打包说明

- 服务器是 FastAPI + uvicorn，只绑回环；浏览器端是 React + TypeScript + Vite 构建的 SPA，
  产物提交在 `src/deeppyro/web/ui/static/`（`index.html` + 哈希命名的 `assets/`），同源提供、
  无 CDN、无外部字体。
- wheel 打包：`[tool.hatch.build.targets.wheel] packages = ["src/deeppyro"]` 自动收录
  `web/ui/static` 与 `web/ui/assets`；`tests/web/test_frontend.py` 会断言产物存在且 shell 引用可解析。
- PyInstaller 等冻结打包目前只收集 bundle/certifi/tzdata，**需要额外收集
  `src/deeppyro/web/ui/static/`**（否则浏览器拿到 503 “Web UI is not built”）；这是已知打包 TODO。
- 修改前端后必须 `cd web && pnpm install && pnpm build` 并提交产物，否则宿主继续服务旧 bundle。
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
| 429 turn queue is full | 当前 turn 未结束且已排 8 条；等完成或 Stop；队列条里也可逐条删除 |
| 503 the web session host is not running | 宿主正在关闭/未启动；重启 `python -m deeppyro.web` |
| 审批条不消失 | 点 Allow once / Allow for this session / Deny；或等 300 秒超时 |
| 模型菜单切换后当前 turn 没变 | 会话级模型切换从下一 turn 生效；当前 turn 流式中会提示 deferred |
| 发送后消息没有立刻出现 | turn 未结束时消息进入队列，composer 上方出现 `Queued · N`；可删除或等当前 turn 结束 |
| 找不到 Commands / Plugins 导航 | 已从侧栏移除：命令走 `Ctrl/Cmd+K`（输入 `/`）或 composer 内直接输入；Plugins 在 Settings 分区 |
| 设置保存后当前会话没变 | agent/provider/permission 从下一 turn 生效，streaming 时等当前 turn 结束（响应 `live.deferred=true`）；web port 等配置仍需重启宿主 |
| 新建会话在 `sessions/` 里找不到文件 | 未发首条消息的会话是 deferred draft，只存在内存；发送后才写 `<id>.jsonl`，未使用就切换/归档会直接丢弃 |
| 新建会话的目录不是想要的 | cwd 在会话创建时固定；用 New session 弹窗或 `/cwd <path>` 新建，旧会话保留 |
| 状态点显示 reconnecting / offline | SPA 的 EventSource 断开后按 800ms 起指数退避（上限 10s）自动重连；`offline` 表示浏览器报告离线。持续无法恢复时确认宿主进程还在、端口未变 |
| 断线后自动重连 | 正常：客户端用 `?after=<lastEventSeq>` 从 seq 续传，不重复渲染历史 |

## 9. 验证

    .venv\Scripts\python.exe -m pytest tests/web -q
    .venv\Scripts\python.exe -m ruff check src/deeppyro/web tests/web
    .venv\Scripts\python.exe -m ruff format --check src/deeppyro/web tests/web

安全边界、路由与事件契约见 [spec/web.md](../spec/web.md)。
