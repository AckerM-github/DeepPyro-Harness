# DeepPyro Harness (deeppyro)

Terminal-first **pure Python** port of DeepSeek Harness (DSH) core.

- 纯 Python，运行时不依赖 Node/Electron。
- 会话日志**逐字段兼容** DSH `session.v4.jsonl`（读取 / 追加 / 派生 / fork）。
- 内置工具对齐 DSH 语义：read/write/edit/glob/grep/pwsh/bash/web_search/web_fetch/todo_write/present，
  以及 P1 的 working_directory/skill/schedule/mnemon_runtime_memory/ask_user_question、
  compress/search_context/decompress 和 subagent/team_*/job_* 工具。
- 产品形态：**只有一套终端前端** —— Textual 全屏 TUI（`dpyro`，唯一交互形态）+
  headless 一次性模式（`dpyro --headless "prompt"`，支持 `--json` NDJSON，供脚本/CI）。
  不做 Web UI。
- 启动页是 FIGlet `ansi_shadow` 像素字标（火色 / 冰蓝渐变、逐行揭示）；每个用户 turn 的**思考与
  工具调用融合成一个 agent 过程**：运行中对话里只保留一条滚动 header（当前一句，或正在执行的工具），
  step/tool 明细默认收起；turn 结束 header 变为一行摘要。点击 header 展开/收起明细，点击 step 行或
  `/thinking` 打开全屏按 step 查看完整 reasoning，点击工具行展开完整参数与结果（reasoning 来自已落盘的
  assistant/message，不另存格式）。
- 会话设置全部走 slash 命令：`/policy` 切权限预设（read-only / workspace-write /
  danger-full-access）、`/approval` 切审批策略（ask / never / always）、`/cwd` 切工作目录，
  切换立即作用于当前会话（并写入 `permission/preset`、`approval/policy`、
  `working-directory/change` 记录）。
- 内核直接 vendor 了 [cordis](https://github.com/cordiverse/cordis) 的 Python 移植
  （`src/deeppyro/_vendor/cordis_port`，MIT）：服务、依赖注入、事件总线、fiber 生命周期
  都与 DSH 同构；领域模块不 import cordis，只有 `kernel/` 负责装配。
- **一切皆插件**：DSH 式组合内核——bundle 用 `bundle.toml` 声明插件行，多层 patch 支持
  按 id 覆盖 config（整体替换）、`before/after` 插入、`disabled/critical/isolate/intercept`；
  还支持 group 行（`group = true` + 子行）、`include` 外部行文件、`[bundle].requires`
  依赖拓扑排序，以及 active/failed/disabled/overridden/restart-required 活状态。
  core bundle 只有 32 行（kernel 运行时 + LLM、会话、沙箱、工具、提示词、命令、agent、
  activity 活动栏、jobs 后台任务、subagent 委派、team 团队、插件管理、设置、inspect、TUI 槽位，
  以及 P1 的 session-query、compress、schedule、working-directory、user-questions、attachments、
  memory、skills、projections），其余能力都由插件贡献。插件可注册工具、提示词段、
  slash 命令、LLM provider、agent preset（带 revision 与 per-preset realm 子插件），以及
  TUI 状态栏项 / 对话行 / 面板 / 设置页。
- **活动栏与多任务**：对话区上方一行活动栏（plan / jobs / agents，空段隐藏）；待办计划来自
  `todo_write`（`/plan` 查看）；后台任务由 jobs 服务托管，shell `run_in_background` 立即返回
  job id，`/jobs` 或 `job_output` / `job_kill` / `job_list` 管理，headless 退出时自动取消；
  team 服务支持最多 8 个成员、共享任务板、成员消息与独立子会话（`/team on` 进入 Team 模式、
  `/team off` 退出，`/team` 打开面板、`/team board` 打开任务板；agent 侧有 `team_*` 工具），
  记录按 DSH 的 `team/*`、`subagent/*` 形状落盘。
- **默认 subagent，Team 是显式模式**：默认单 Agent 直接持有 `subagent` 工具，把独立子任务委派给
  一次性子会话（结果回传，不进入 Team 协作层）；`/team on` 在当前会话进入内置 team preset 后，Lead
  获得 `team_*` 工具（spawn / 任务板 / 消息）并收起 `subagent`，Team 成员子会话两者都没有，避免嵌套。
  `/team off` 回到进入前的 preset（仍有 live 成员时拒绝），两种模式的子会话都按 DSH 形状写
  `subagent/*`、`team/*` 记录。
- **P1 运行时能力**：会话查询（`session_query` 服务：摘要/事件窗口/trace）、模型自压缩
  （`compress` / `search_context` / `decompress`，block 侧车文件按 session 寻址）、定时任务
  （`schedule` 工具 + TUI timer + `~/.dpro/schedules.json`）、只读 skills 目录（`skill` 工具 +
  提示词目录段）、runtime memory（`mnemon_runtime_memory` 工具，USER.md/MEMORY.md 投影，
  容量硬上限）、图片附件（Pillow 校验/归一化 + inline 预算 + `image/offload` 投影）、
  per-session 投影注册表与 checkpoint sidecar、Anthropic Messages provider 与模型目录发现
  （`/model` 候选合并配置模型与远端目录，contextWindow 自动回退）、PTC `run_code`
  （默认关闭的 Python 程序化工具调用：程序内 `await` 当前 session 的工具，内层调用只写
  `tool/ptc-dispatch*` 日志、不进模型可见 surface）。
- **运行时自扩展**：`/plugins` 界面与 `plugin_manager` / `plugin_inspect` /
  `settings_read` / `settings_write` / `plugin_create` 五个工具让 agent 自己安装/脚手架
  外部 bundle、启停插件、读改插件配置、查看 live 组合（安装/脚手架会执行或生成第三方代码，
  需人工批准；声明为 volatile 的字段可经 kernel 服务即时生效，其余 next-boot 生效）。

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
      builtin/  core 插件行（llm/sessions/session_query/sandbox/tools/prompt/commands/agents/
                activity/jobs/subagent/team/presets/plugin_manager/settings/inspect，以及
                compress/schedule/working_directory/user_questions/attachments/memory/skills/projections）
      bundles/core/bundle.toml  core bundle 的 32 个插件行（首行 kernel 运行时）
      jobs/     后台任务服务（JobHandle/JobResult、job/registered|settled）
      subagent/ 一次性子智能体委派（默认模式的 subagent 工具、子会话、subagent/* 记录）
      team/     子智能体团队与共享任务板（成员、消息、子会话、team/* 记录）
      attachments/  图片附件校验/归一化/限额与 manifest
      memory/   运行时记忆存储与 USER.md/MEMORY.md 投影
      projection/ per-session 投影注册表与 checkpoint sidecar
      schedule/ 定时任务存储、rrule/cron 解析与 timer manager
      session_query/ 只读会话摘要/事件窗口/trace
      skills/   skills 目录发现、frontmatter 与目录渲染
      user_questions/ ask_user_question 服务与 handler 串行化
      working_directory/ 会话级工作目录服务与 working_directory 工具
      core/     冻结的 wire 模型 + 运行时协议（共享契约）
      session/  session.v4 日志：模型、append 校验、derive、fork、store
      llm/      provider 适配层（OpenAI-compatible SSE、Anthropic Messages）、模型目录、图片预算、重试
      tools/    工具注册表、执行流水线、内置工具
      ptc/      最小 Python PTC transport（run_code 保留工具、worker 协议、内层 dispatch，默认关闭）
      sandbox/  权限预设 / 策略闸门
      prompt/   系统提示词与运行时上下文装配
      agent/    turn/step 循环、inbox、事件流
      compact/  上下文压缩、compress block/ref sidecar 与检索工具
      tui/      Textual 全屏 TUI（唯一前端：app/widgets/screens/slots/commands/sink/runtime）
      plugins/  外部插件发现与管理（entry point / 插件目录 / 用户清单 / bundle 安装与设置）
      headless.py  一次性 headless（run_once / NDJSON+rich sink，自包含）
      paths.py  用户目录解析（~/.dpro、DEEPPYRO_HOME）与旧数据迁移
    docs/spec/  契约与逆向出的 DSH 语义（INTERFACES / tui / headless / session / llm / tools / loop /
                plugins / agent-presets / subagent / activity / hooks / skills / schedule / memory /
                session-query / compact-blocks / attachments / image-budget / image-offload /
                image-request / projection / ptc）
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

TUI 里输入 `/help` 查看 slash 命令（`/new /resume /provider /model /preset /policy /approval /cwd
/status /plan /jobs /team /thinking /compact /clear /plugins /panel /exit`）；`/provider`、`/model`、
`/policy`、`/approval`、`/cwd`、`/team` 等无参时打开选择器或面板，带参数的切换立即生效（turn
运行中拒绝）。`/team on` 在当前会话进入 Team 模式、`/team off` 回到进入前的 preset，Team 模式
只给 Lead 开放 `team_*` 工具并隐藏 `subagent`；
`Ctrl+C` 在 turn 中取消当前 turn，空闲时清空输入，再按退出；`Ctrl+D` 退出。

### 配置与数据目录

所有用户级状态集中在一个目录：默认 `~/.dpro`（Windows 为 `C:\Users\<user>\.dpro`），
结构对齐 DSH 的 `~/.dsh`：

| 路径 | 内容 |
|---|---|
| `config.toml` | provider / model / agent 配置（首次运行自动生成） |
| `sessions/` | `session.v4.jsonl` 会话日志；`sessions/blocks/` 为 compress block/ref 侧车文件 |
| `schedules.json` | 定时任务（version 1 原子写；timer 只在 TUI 进程运行） |
| `memory/` | runtime memory 的 `memories.json` 与 `USER.md`/`MEMORY.md` 投影 |
| `attachments/` | 图片附件 PNG 与 `manifest.jsonl` |
| `plugins.toml` | 用户插件清单 |
| `plugins/` | 外部插件目录 |
| `history.jsonl` | TUI 输入历史 |

- `DEEPPYRO_HOME` 覆盖整个目录；`DEEPPYRO_CONFIG` / `DEEPPYRO_SESSION_DIR` / `DEEPPYRO_HISTORY` /
  `DEEPPYRO_PLUGINS_MANIFEST` / `DEEPPYRO_PLUGINS_DIR` 覆盖单项路径。
- 旧版本散落在 `%APPDATA%\deeppyro` / `%LOCALAPPDATA%\deeppyro` 的数据，首次运行会自动复制到
  `~/.dpro`（只补缺失文件、不覆盖已有文件、不删除旧目录）。
- 在 TUI 里用 `/provider` 和 `/model` 切换 provider 与模型，选择会写回 config.toml 的 `[agent]`
  段（保留其余内容与注释）；`/resume` 会按日志里的 `model/selection` 记录恢复 provider/model。
- API key 两种写法：`apiKey = "sk-..."` 直接写在 provider 段；或 `apiKeyEnv = "DEEPSEEK_API_KEY"`
  写环境变量名（注意 `apiKeyEnv` 的值是变量名，不是 key 本身）。缺失 key 时 `/provider` 选择器会标注
  missing API key。

### 插件与组合

- core bundle：`src/deeppyro/bundles/core/bundle.toml`（32 行，首行 `kernel` 运行时服务，`critical = true`）。
- 外部 bundle：`~/.dpro/bundles/<id>/bundle.toml` + Python 文件；`/plugins` 或
  `plugin_manager` 工具可安装/启停/卸载（安装只做结构校验，下次 boot 执行第三方代码）。
- 其他插件来源：entry point 组 `deeppyro.plugins`、`~/.dpro/plugins/<id>/plugin.toml`、
  用户清单 `~/.dpro/plugins.toml` 的 `[[plugins]]` 行。
- 插件可贡献：工具、提示词段（带 preset scope + revision）、slash 命令、LLM provider、
  agent preset（`/preset` 切换，带 revision 与 per-preset realm 子插件）、TUI 状态栏项 /
  对话行 / 面板（`/panel`）/ 设置页；UI 也可以在 `ui.toml` 里用 `module:function`
  声明式注册（`~/.dpro/bundles/<id>/ui.toml`，支持 theme token 与 locale）。
- 组合语义：group 行给子行建独立 cordis context；`include` 支持 JSON/TOML；bundle 依赖用
  `requires` 拓扑排序；`App.row_states()` 暴露 active/failed/disabled/overridden/
  restart-required 状态；patch 层把被覆盖的行标为 overridden。
- 配置：`settings` 服务 + `/plugins` 与 `settings_*` 工具；写入带 revision CAS 与 secret
  掩码；插件类声明 `volatile` 的字段由 `kernel` 服务 live apply（其余 next-boot 生效）。
- 已知边界：插件进程内运行、加载即信任（无 OS 隔离）；目录来源插件需在 `sys.path` 中或先加载
  plugin-manager 行；外部行失败只降级记录，不中断 boot。

### 测试 fixtures 含个人会话，故意不入库

真实 DSH 会话日志属于个人数据，仓库里不含 `fixtures/`（已 gitignore）。需要跑黄金对照 /
记录类型回归测试时，在本机生成：

    node tools/gen_fixtures.mjs    # 读 %USERPROFILE%\.dsh\sessions，写 ../fixtures/
    # 可用 DSH_SESSIONS / DEEPPYRO_FIXTURES 覆盖输入、输出目录

缺少 fixtures 时，相关测试会自动 skip，其余测试照常运行。
