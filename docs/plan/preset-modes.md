# 内置 agent preset：Standard / PTC / Minimal / Cordis

基线：D:/code/deeppyro HEAD 9d80287（P2 已 push）。需求：用户 m12490（/preset 后接 DSH 四种
内置 agent：极简/ptc/cordis/标准；team 入口从 /preset 删除，用 /team）、m12810（英文显示名；
standard 隐藏插件创作工具；minimal 只留平台 shell；第 4 项安全边界随后单独做）。
DSH 权威：D:/dshworkspace/_dsh_src/dsh-web-app/presets/{standard,ptc,minimal,cordis}.patch.yml
与 dsh-agent-preset-registry/lib/index.js。

## 1. 目标与边界

目标：

- \`/preset\` 列出四个内置 agent preset：\`standard\`（Standard）、\`ptc\`（PTC）、\`minimal\`
  （Minimal）、\`cordis\`（Cordis）。
- \`team\` 仍是 hidden preset，只经 \`/team on\` 进入，不出现在 \`/preset\` 列表里。
- 四种都能在会话内实时切换：空闲即可切换、不重建进程、历史连续；turn 运行中拒绝。
- headless 增加 \`--preset <id>\`，未知 id 报错退出。

边界（有意不做）：

- 不复刻 DSH 的声明式插件组合（每 preset 的 \`plugins\` 行、profileContext、tool 分组）。
  deeppyro 不做 DSH 那套包组合；用 PresetSpec 的四个新字段表达差异。
- 不改 \`_vendor/cordis_port\`。
- 不实现 DSH 的 "首轮开始后 preset 锁定"（dsh-agent-preset-registry 在
  \`boundary.openTurnStartSeq !== null || lastTurn !== 0\` 时抛 agent-preset/locked）。
  deeppyro 现有语义是空闲即可切、历史连续；本计划保留并作为有意偏离记录。
- 第 4 项执行安全边界（sandbox policy/provider/approval presets）不在本计划内，随后单独做。

### 与 DSH 的映射

| DSH preset 组成 | deeppyro 表达 |
|---|---|
| persona（minimal 用 \`You are a helpful software engineer assistant.\`） | scoped 提示词段 \`minimal-persona\` |
| tool-fs / tool-bash / tool-pwsh / tool-web / tool-todo / tool-subagent / tool-ask-user | 标准内置工具集（registry 全局贡献） |
| tool-fs-search / tool-jobs / tool-schedule / tool-skill / tool-goal / present | 同上，按宿主能力注册 |
| compaction group | 默认压缩服务（与 preset 无关，不切换） |
| tool-executor-ptc（ptc preset） | \`presentation = "ptc"\`：只直接暴露 \`run_code\` + 自动 Python SDK |
| tool-plugin-manager / tool-plugin-market / tool-cordis | 5 个插件创作工具；standard/minimal 隐藏，cordis 提供 |
| skill-filesystem 的 cordis skills | \`cordis-guide\` scoped 提示词段 |
| plugin 分组里的 persistent-shell（minimal） | \`tools = ("pwsh",)\`（Windows）或 \`("bash",)\` |
| minimal 的 includeRuntimeContext=false | \`runtime_context = False\` |

## 2. 设计

### 2.1 PresetSpec 新字段

\`\`\`text
presentation: str = "native"        # 'native' 直接暴露全部工具；'ptc' 只暴露 run_code
tools: tuple[str, ...] | None       # 工具 allowlist；None = 全部
tools_exclude: tuple[str, ...] = () # allowlist / mode 过滤之后再移除
prompt_sections: tuple[str, ...] | None  # 提示词段 allowlist（按 section name）；None = 全部可见
runtime_context: bool = True        # 是否写每轮 runtime-context projection
\`\`\`

\`DEFAULT_PRESET_ID = "standard"\`（原 \`"default"\`）；保留 \`LEGACY_DEFAULT_PRESET_ID = "default"\`
作为持久化迁移别名（\`PresetsPlugin.canonical()\`，resume 旧会话时归一为 \`standard\`）。

### 2.2 四个内置 preset

| id | name | order | presentation | tools | tools_exclude | prompt_sections | runtime_context |
|---|---|---|---|---|---|---|---|
| \`standard\` | Standard | 0 | native | 全部 | 5 个插件创作工具 | 全部 | 是 |
| \`ptc\` | PTC | 10 | ptc | 全部 | 5 个插件创作工具 | 全部 + scoped tools:ptc-only/tools:sdk | 是 |
| \`minimal\` | Minimal | 20 | native | \`("pwsh",)\` / \`("bash",)\` | — | minimal-persona/tools/rules | 否 |
| \`cordis\` | Cordis | 30 | native | 全部（含插件创作工具） | — | 全部 + scoped cordis-guide | 是 |
| \`team\` | Team | 200 | native | 全部 | 5 个插件创作工具 | 全部 | 是 |

- \`standard\` 与 hidden \`team\` 由 \`builtin/presets.py\` 在 boot 时注册（revision 0，不占用
  host revision 计数器）。
- \`ptc\`/\`minimal\`/\`cordis\` 由新插件 \`builtin/agent_presets.py\`（plugin_id
  \`agent-presets\`）注册；\`minimal-persona\`、\`cordis-guide\` 两个提示词段以
  \`scope=<preset id>\` 注册。
- \`builtin/ptc.py\` 在 \`mode != "ptc"\`（新默认 native）时以 \`scope="ptc"\` 注册 run_code 与
  \`tools:ptc-only\`/\`tools:sdk\` 两段；全局 \`[ptc].mode = "ptc"\` 保留旧行为（全局注册，
  不重复注册 scoped，避免同一 scope 下重复段）。
- 插件加载顺序：\`agent-presets\` 行在 \`ptc\` 之后、\`memory\` 之前（必须晚于 presets 与 prompt）。

### 2.3 解析链（每轮/每次创建）

\`\`\`text
presets.resolve(preset, revision)
  → tools.registry_for(cwd, scope=preset, revision=revision)      # 全局 + preset-scoped 工具
  → _filter_registry: mode 过滤 → spec.tools allowlist → spec.tools_exclude
  → prompt_service.build(..., scope, revision, include=spec.prompt_sections)
  → surface = presentation=="ptc" ? registry.without(除 run_code) : None
  → AgentSession(registry=full, surface_registry=surface, runtime_context=...)
\`\`\`

- \`mode\` 过滤保持现有语义：team mode 去掉 \`subagent\`，其它 mode 去掉 \`team_*\`。
- 旧全局 \`[ptc].mode="ptc"\` 兼容：\`spec.presentation=="ptc"\` 或（全局 ptc 模式 + 标准
  preset + registry 含 run_code）时启用 surface。
- prompt allowlist 过滤后为空必须报错，不得回退到 core prompt（避免泄露全套提示词）。

### 2.4 宿主行为

- TUI：\`/preset\` picker 显示 id + 英文名 + presentation + 描述；成功提示与状态栏/welcome
  显示 display name 与 presentation；team 模式下 \`/preset\` 仍拒绝并提示先 \`/team off\`。
- TUI runtime：所有 preset id 经 \`presets.canonical()\` 归一；resume 旧记录
  \`{"preset":"default"}\` 恢复为 \`standard\`。
- headless：\`--preset\` 解析并 ensure；用 preset-scoped registry 创建 agent；
  \`SESSION_CREATED\` 的 \`preset\` 填真实 id；未知 id stderr 报错退出码 2。
- 持久化：\`agent-preset/selected\` 记录 canonical id；header \`agentPreset\` 兜底 standard。

## 3. 任务与写域

| 任务 | 负责人 | 写域 |
|---|---|---|
| T1 基础（本计划） | Lead | \`api/types.py\`、\`api/__init__.py\`、\`builtin/presets.py\` |
| T2 task-70 | plugin-dev | \`builtin/agent_presets.py\`（新）、\`builtin/ptc.py\`、\`tests/builtin/test_agent_presets.py\`、\`tests/ptc/test_plugin.py\` |
| T3 task-71 | session-dev | \`builtin/prompt_service.py\`、\`builtin/agents.py\`、\`agent/session.py\`、\`tests/agent/**\`、\`tests/builtin/**\` |
| T4 task-72 | tui-dev | \`tui/runtime.py\`、\`tui/app.py\`、\`tests/tui/**\` |
| T5 task-73 | loop-dev | \`headless.py\`、\`tui/main.py\`、\`team/manager.py\`、headless 测试 |
| 收尾 | Lead | \`bundles/core/bundle.toml\`、\`tests/kernel/test_bundle.py\`、集成测试、docs、全量验证、冒烟、commit/push |

## 4. 验收清单

- [x] PresetSpec 字段 + 常量 + `deeppyro.api` 导出
- [x] `standard` 注册、`default` 别名、team hidden 不变
- [x] `ptc`/`minimal`/`cordis` 定义与 scoped 提示词段
- [x] `ptc` preset 在 native 全局模式下仍有 run_code surface + SDK 段
- [x] registry 过滤（mode + allowlist + exclude），caller 传入 registry 同样生效
- [x] prompt allowlist（空集报错）与 runtime_context 开关
- [x] TUI `/preset` 四选、实时切换、状态栏/welcome、resume 别名迁移
- [x] headless `--preset` 四种、未知 preset 报错
- [x] 全量 `uv run pytest -o addopts= -q` 与 `ruff check/format` 全绿
- [x] 真实模型 headless 四种 preset 冒烟；TUI 连续切换截图
- [x] docs（agent-presets/ptc/tui/headless/INTERFACES/README）与 plan 执行记录

## 5. 执行记录

实现（2026-11-14，基线 9d80287）：

| 任务 | 交付 |
|---|---|
| T1 Lead | `api/types.py` 五字段 + 常量 + `__all__`；`api/__init__.py` 导出；`builtin/presets.py` 的 standard/team 定义、`canonical()` 与 `default` 别名；`bundle.toml` 的 agent-presets 行；`tests/kernel/test_bundle.py`；旧 preset 测试适配 |
| T2 task-70 plugin-dev | `builtin/agent_presets.py`（PTC/Minimal/Cordis + `minimal-persona`/`cordis-guide`）；`builtin/ptc.py` native 分支以 `scope="ptc"` 注册；`tests/builtin/test_agent_presets.py`、`tests/ptc/test_plugin.py` |
| T3 task-71 session-dev | `prompt_service.build(include=...)`（空集报错）；`agents.py` 的 allowlist/denylist、presentation surface、prompt_sections、runtime_context、`agent.presentation`；`agent/session.py` 的 `runtime_context` 开关；`tests/builtin/test_agent_preset_filters.py` |
| T4 task-72 tui-dev | `tui/runtime.py` 的 canonical/`agent_presentation`/`agent_preset_display`；`tui/app.py` 的 `/preset` picker、成功提示、welcome 与 `/status`；`tests/tui` 8 个文件 |
| T5 task-73 loop-dev | `headless.py` 的 `--preset`（`UnknownPresetError`、退出码 2、preset-scoped registry、`SESSION_CREATED` 真实 preset）；`tui/main.py` 的 `--preset`；`team/manager.py` 默认 id；`tests/tui/test_headless.py` +12 |
| Lead 收尾 | `tests/integration/test_preset_modes.py`；docs（agent-presets/ptc/tui/headless/INTERFACES/README）；全量验证；真实冒烟；TUI 截图 |

验证：

- `uv run pytest -o addopts= -q` → **2788 passed in 113.23s**；`uv run ruff check src tests docs` → All checks passed；`ruff format --check` → 458 files already formatted。
- 真实模型（deepseek/deepseek-flash，headless `--json`）四个 preset 各跑一次：exit 0、final text `ok`；`request/header.tools` 为 standard 23 个、ptc `["run_code"]`、minimal `["pwsh"]`、cordis 28 个；`system/message` 长度 19522/19113/2526/25217，标记验证 minimal 以 persona 开头且无 `## Environment`、ptc 含 `### run_code`/`class Tools(Protocol):`、cordis 含 `Extending DeepPyro with Cordis plugins`。
- 未知 preset：`uv run dpyro --headless --json --preset nope "hi"` → stderr `error: unknown agent preset: 'nope'`、退出码 2、stdout 无 JSON。
- TUI 实时切换（`run_test`，截图 `.dsh-tmp/tui_preset_{standard,picker,ptc,minimal,cordis}.png`）：standard 23 工具 → `/preset` 弹出四选 picker → `/preset ptc` surface 仅 `run_code` → `/preset minimal` 仅 `pwsh` → `/preset cordis` 28 工具；全程同一进程、不重启。

提交（origin/main）：

- `1f45222` feat(presets): add DSH-style Standard/PTC/Minimal/Cordis agent presets
- `3622b50` feat(tui): switch agent presets live with /preset
- `c207222` feat(headless): add --preset for standard/ptc/minimal/cordis
- docs 与集成测试：本文件所在 commit

偏离（有意）：

- DSH 首轮开始后锁定 preset；deeppyro 保持“空闲即可切、历史连续”（见 §1 边界与 `docs/spec/agent-presets.md` §10）。
- 状态栏 widget 的 preset 参数是权限预设，agent preset 展示落在 `/status` 行与 welcome，未改 `widgets/status_bar.py`。
- `create_schedule_session` 的 preset 未 canonical（计划任务 preset 来自持久化 spec，入口不经过 `/preset`）。
- minimal 不含 DSH 的 readImage（deeppyro 无对应工具）。
- 四个内置 preset 不可被 `/preset` 删除/覆盖（revision 不可变注册）；自定义 preset 仍可注册。
