# 第 4 项：执行安全边界严格对齐 DSH（S0–S6）

> 目标（用户 m12810 / m12949 / m13516）：在 preset 四内置之后，把「PermissionGate ≠ OS sandbox」
> 这一段按 DSH 四层语义严格对齐：策略层、执行层、工具消费者、审批 + 命名权限预设。
> 只保留 `/permission`，删除 `/policy`、`/approval` 与审批值 `always`。
> 事实源：`D:/dshworkspace/_dsh_src/{dsh-sandbox-policy,dsh-sandbox-local,dsh-user-approval,dsh-permission-presets,dsh-tool-pwsh,dsh-bash-sandbox,dsh-fs-sandbox}`。

## 1. DSH 四层与 deeppyro 映射

| 层 | DSH | deeppyro |
| --- | --- | --- |
| 策略 | `dsh-sandbox-policy`：每次调用解析 `{mode, workspaceRoot, additionalWritableRoots}`，会话内切换写 log-only `sandbox/mode` | `sandbox/policy.py`：`SandboxPolicyService`（resolve/workspace_root/last_mode/set_mode；writable roots = workspace + additional + temp；`sandbox_policy_text`/`approval_policy_text`） |
| 审批 | `dsh-user-approval`：`ask|never`，无 answerer fail closed，写 `approval/asked|decided` | `sandbox/approval.py`：`ApprovalService`（policy/last_policy/effective_policy/set_policy/request；never 在 waterfall 前 rejected；ask 先 asked 再 `approval/request` waterfall 再 decided） |
| 预设 | `dsh-permission-presets`：命名 preset = sandbox mode + approval 打包，`/permission` 切换，只写变化旋钮，custom 派生只读 | `sandbox/permission.py`：`PermissionPresetsService`（names/catalog/get/derive/current/is_custom/apply/pin_initial；三内置 preset；`permission/preset` 事件） |
| 执行 | `dsh-sandbox-local`：`confine(argv)` 或 fail closed；enforcement full/partial；Windows restricted token | `sandbox/provider.py` + `sandbox/windows_acl_runner.py`：`LocalSandboxProvider.confine`；danger 原样；Windows read-only/workspace-write 走 ACL runner；非 Windows fail closed |
| 消费者 | bash/pwsh deny-only executor + fs-sandbox canonicalize-then-contain | `tools/builtin/shell.py` confine + marker；`tools/permissions.py` check_write 按 writable roots 直接拒 + 显式升权审批 |

## 2. 关键决策

- `/permission` 成为一等命令，打包切换 `sandbox/mode` + `approval/policy`（必要时写 `permission/preset`）；
  `/policy`、`/approval` 命令与用法串全部删除。
- 审批值删除 `always`：只有 `ask` / `never`；`never` 在 hook 之前直接 rejected（prepended listener 不能在其上放行）。
  交互审批只有「一次性允许」与「拒绝」，ApprovalScreen 删除 always 按钮/快捷键。
- 配置迁移：`[agent] permissionPreset/approvalPolicy` → `[sandbox] mode/additionalWritableRoots`、
  `[approval] policy`、`[permission] defaultPreset`；headless 新增 `--permission-mode` / `--approval-policy`。
- 写入不再「越界即弹审批」：`check_write` 按策略直接允许/拒绝；审批只用于工具显式
  `sandbox_permissions` 升权（一次一审批，只能更宽）。拒绝文案与 DSH 原文一致。
- shell 命令 `check_command` 恒允许，由 `confine()` 在 OS 层约束；runner 失败与 denial 用 DSH marker
  区分（`[sandbox: the sandbox runner itself failed ...]` vs `[sandbox: file access denied under <mode> mode]` + escalation hint）。
- 子会话权限继承父会话：`team` / `subagent` 用 `policy_service.set_mode` + `approval_service.set_policy`，
  gate 仍按会话日志动态解析，因此继承顺序不敏感。

## 3. 交付清单

- 新增：`src/deeppyro/sandbox/{policy,approval,permission,provider,windows_acl_runner}.py`；
  重写 `sandbox/{types,gate,__init__}.py`、`builtin/sandbox.py`、`tools/permissions.py`。
- 消费者：`tools/builtin/shell.py`（confine/meta/marker/背景任务/temp 清理）、`tools/builtin/fs.py`（经 check_write）、
  `agent/session.py`（bind_session、runtime context 两段文本）、`prompt/{prompt,runtime_context}.py`。
- 面：`tui/{app,runtime,main}.py` 的 `/permission`、状态栏、审批模态；`headless.py` 的 pin_permission 与两个 CLI 旗标；
  `subagent/manager.py`、`team/manager.py` 的权限继承。
- 测试：`tests/sandbox/`（services + Windows runner）、`tests/tools/test_sandbox.py` 重写、
  shell/fs/headless/tui/hook/approval/integration 全量适配。
- 文档：新建 `docs/spec/sandbox.md`；更新 `INTERFACES`、`tools`、`tui`、`tui-input`、`tui-widgets`、`headless`、
  `hooks`、`README`。

## 4. 验收（已完成）

- 全量 `uv run pytest -o addopts= -q` → **2843 passed**；`ruff check src tests docs` 与
  `ruff format --check` 全绿。
- headless 真实模型（deepseek/deepseek-flash，临时 home，用后删除）：
  - `--permission-mode read-only` 写 workspace：`write` 被直接拒，消息含 denial marker + escalation hint；
    模型尝试升权 → `the user rejected escalating this write to "workspace-write"; ...`。
  - `--permission-mode danger-full-access` 写 workspace：成功，文件内容正确。
  - read-only 下 pwsh 写文件：ACL runner 在 OS 层拒绝（`Access ... denied`），输出带
    `[sandbox: file access denied under read-only mode]` 与 escalation hint。
  - workspace-write 下 pwsh 写文件：runner 放行，文件生成。
  - `--approval-policy never`：会话记录出现 `approval/policy {"policy":"never"}`，
    升权路径按 fail closed 拒绝。
  - 会话记录初始三条 `sandbox/mode`/`approval/policy`/`permission/preset`；切换后只写变化的旋钮；
    runtime context 出现 `sandbox:policy` 与 `approval:policy` 两段（read-only 文案与 ask/never 文案）。
- TUI `run_test`：`/permission` picker 列出 read-only/workspace-write/danger-full-access（current 标记），
  `/permission read-only` 同进程切换后状态栏 `read-only/ask`；切 danger-full-access 后 `danger-full-access/never`；
  记录顺序与 `pin_initial` + 变化旋钮一致。

## 5. 已知偏离

- POSIX 没有本地 sandbox 后端：非 `danger-full-access` 一律 `SandboxUnavailableError`（fail closed），
  不静默无约束执行。
- Windows runner 只实现 read-only / workspace-write，enforcement 恒为 `partial`（DSH 同层语义已保留）。
- `additionalWritableRoots` 暂不进入 runner：非空时 `SandboxUnsupportedPolicyError`。
- workspace 必须对当前用户/Authenticated Users 可写（restricted token 的双重 access check 需要 normal 通过）。
- `/permission` 切换不重建 agent，也不改 system prompt；策略文本随下一轮 runtime context 快照刷新。
- 审批 escalate 只覆盖工具显式升权；不存在「越界写自动弹审批」路径（与 DSH 一致）。

## 6. 提交序列

1. `feat(sandbox)`：types/policy/approval/permission/provider/windows runner + `tests/sandbox`。
2. `feat(tools,tui,headless)`：gate/shell/fs/permissions 消费者、/permission、ApprovalScreen、headless 旗标、team/subagent 继承。
3. `docs`：sandbox.md、INTERFACES/tools/tui/headless/hooks/README、本文件。
