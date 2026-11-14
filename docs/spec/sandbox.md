# DeepPyro 沙箱与权限规格（对齐 DSH 四层）

状态：已实现（无 OS 沙箱的 fail-closed、Windows ACL restricted-token runner、策略/审批/命名预设服务、工具消费、TUI/headless 接线），全量测试与真实冒烟见 §11。

## 1. 形态与范围

DeepPyro 的执行安全与 DSH 一样拆成四层，每层只负责一件事：

1. **策略层**（`sandbox/policy.py`）：当前会话的 sandbox mode 与工作区可写根。每次调用时解析，mode 切换是 log-only 的 `sandbox/mode` 记录。
2. **执行层**（`sandbox/provider.py` + `sandbox/windows_acl_runner.py`）：把 argv 包装成受 OS 约束的 argv；没有可用后端时 fail closed，绝不静默无约束执行。
3. **审批层**（`sandbox/approval.py`）：`ask` / `never` 两档策略，hook waterfall 或交互 answerer 给出决定，一次只批准一次。
4. **命名预设**（`sandbox/permission.py`）：把 sandbox mode + approval policy 打包成 read-only / workspace-write / danger-full-access，`/permission` 切换；组合不在表里时派生出只读的 `custom`。

工具侧（`tools/permissions.py`、`tools/builtin/fs.py`、`tools/builtin/shell.py`）是这些服务的消费者，负责拒绝文案、升权参数校验与结果事实（`meta["sandbox"]`）。

与 DSH 的对应关系：`dsh-sandbox-policy`、`dsh-sandbox` + `dsh-sandbox-local`、`dsh-user-approval`、`dsh-permission-presets`、`dsh-bash-sandbox` / `dsh-pwsh-sandbox` / `dsh-fs-sandbox`。

## 2. 模块布局（src/deeppyro/sandbox/）

| 文件 | 职责 |
| --- | --- |
| types.py | Preset / Approval / OUTCOMES、Decision、ApprovalRequest、PermissionPresetSpec、DEFAULT_PERMISSION_PRESETS |
| policy.py | SandboxPolicy、SandboxPolicyService；mode 解析、writable roots、runtime context 文案 |
| approval.py | ApprovalService：策略、hook、answerer、asked/decided 事件 |
| permission.py | PermissionPresetsService：命名预设、派生 custom、apply/pin_initial |
| provider.py | LocalSandboxProvider.confine、Confinement、denial/escalation 文案与失败分类 |
| windows_acl_runner.py | `python -m deeppyro.sandbox.windows_acl_runner`：restricted token + ACL + job object |
| gate.py | PermissionGate：兼容入口，把 policy/approval 服务适配成旧的 check_* / request 接口 |

`src/deeppyro/builtin/sandbox.py` 是插件装配点：`SandboxPlugin`（provide=`sandbox`）读配置、构造上述服务与 provider，并注册 `sandboxPolicy`、`approval`、`permissionPresets` 三个服务。

## 3. 策略层

- `SandboxPolicy(mode, workspace_root, additional_writable_roots)` 是每次调用解析出的不可变快照。
- `SandboxPolicyService.resolve(session, mode=None)` 的优先级：显式 mode > 会话最后一条 `sandbox/mode` 记录 > 配置默认值。
- workspace root 取 **会话 header 的 cwd**（会话不可变），配置的 `[sandbox].workspaceRoot` 只在会话没有 cwd 时兜底。这样模型不能通过改配置把写范围挪走。
- `writable_roots(session)` = workspace root + 配置的 `additionalWritableRoots` + 平台临时目录（`tempfile.gettempdir()`；另有 `/tmp`）。`read-only` 模式返回空集合。
- `sandbox_policy_text(policy)` / `approval_policy_text(policy)` 生成 runtime context 里的 `sandbox:policy` 与 `approval:policy` 两段；内容随策略变化，因此它们由 runtime context 刷新携带，而不是写进系统提示词。
- 事件：`sandbox/mode`，data=`{"mode": "<preset>"}`。`set_mode` 幂等：与当前解析值相同则不写。损坏记录抛 `SessionError("invalid sandbox/mode record: ...")`。

## 4. 审批层

- 策略只有 **ask** 与 **never** 两档（DSH 没有 always）：
  - `never`：永不交互，直接返回 rejected，既不发 `approval/asked` 也不发 `approval/decided`。
  - `ask`：先发 `approval/asked`，再走 `APPROVAL_REQUEST` waterfall；hook 没有决定时交给 answerer；answerer 不存在或抛异常按 unavailable fail closed。
- outcomes：`allowed-once`、`rejected`、`cancelled`、`unavailable`。hook 返回值接受 `allow`/`deny`/`cancel` 别名以及四个 outcome 本身；无法识别按 unavailable。
- `ApprovalService.request(...)` 的顺序：
  1. signal 已 set → `cancelled`，不跑 hook；
  2. policy=never → `rejected`；
  3. emit `approval/asked` `{id, toolName[, callId][, reason]}`；
  4. waterfall `approval/request`，payload `{id, request, approval, agent, policy, signal}`，fallback `"unavailable"`；
  5. 未决定时 answerer；无 answerer → `unavailable`；
  6. emit `approval/decided` `{id, outcome}`。
- 事件：`approval/policy` 记录 `{"policy": "ask"|"never"}`，`set_policy` 幂等。
- 一次批准只对一次调用有效；没有 "always allow"，也没有 gate 级放宽。

## 5. 命名预设

默认表（`DEFAULT_PERMISSION_PRESETS`）：

| preset | sandbox mode | approval policy |
| --- | --- | --- |
| read-only | read-only | ask |
| workspace-write | workspace-write | ask |
| danger-full-access | danger-full-access | never |

- `PermissionPresetsService.apply(session, name)`：
  1. 未知 name 报 `unknown preset "<name>" (available: read-only, workspace-write, danger-full-access)`；
  2. `custom` 不可选；
  3. 当前 preset 与 name 相同则**不写任何记录**（净零切换）；
  4. 否则写 `permission/preset`；
  5. 然后只对发生变化的旋钮写 `sandbox/mode` 与/或 `approval/policy`。
- `derive(session)` 先看最后一条 `permission/preset` 是否仍与当前两个旋钮匹配，再匹配任意同一组合的 preset，最后返回 `custom`。
- `pin_initial(session)` 在会话创建时补齐缺失的初始记录（mode、policy、preset），保证回放与 runtime context 有稳定起点。
- 环境变量/配置不能直接绕过命名表把任意组合写进会话；`custom` 只读。

## 6. 执行层

### 6.1 confine 契约

`LocalSandboxProvider.confine(argv, policy, *, signal=None, temp_root=None)` 返回 `Confinement` 或抛错，**绝不返回未约束的 argv 作为降级**。

- `danger-full-access`：原 argv，`enforcement="full"`。
- 非 Windows：`SandboxUnavailableError(mode, "no POSIX sandbox backend is available")`，工具把命令判为未执行。
- `additional_writable_roots` 非空：`SandboxUnsupportedPolicyError`（Windows ACL 后端无法表达额外根；改用 danger-full-access 或删除额外根）。
- Windows：包装成
  `python -m deeppyro.sandbox.windows_acl_runner --workspace <root> --temp <private> --mode <mode> -- <argv...>`，
  `enforcement="partial"`、四条 denial 签名、runner-failure 规则 `(exit 127, "windows-acl-run: ")`，并返回私有 `temp_root` 供调用方清理。

### 6.2 Windows ACL runner

- 生成 workspace 专属 restricting SID（`S-1-4-...`），给 workspace 与私有 temp 加 ACE；用 `CreateRestrictedToken`（WRITE_RESTRICTED）启动子进程。
- 只有 workspace 与私有 temp 可写；workspace 外的写返回 `PermissionError`/access denied。
- read-only 模式不给任何写 ACE，所有写被拒绝。
- 子进程继承 stdin/stdout/stderr；退出码原样镜像；job object `KILL_ON_JOB_CLOSE` 保证父进程退出时杀掉后台子进程。
- 失败一律 fail closed：stderr 写 `windows-acl-run: <detail>`，退出 127，不启动目标命令。
- temp root 在命令结束后由消费方清理；workspace ACE 常驻，temp ACE 在退出时 revoke。

### 6.3 结果事实与标记

- `Confinement` 携带 `denial_signatures` 与 `runner_failure_rules`；消费方用 `matches_denial(output, signatures)` / `classify_runner_failure(exit_code, output, rules)` 分类。
- runner 自身失败：输出追加 DSH 原文
  `[sandbox: the sandbox runner itself failed under <mode> mode — the command did not run; this is a sandbox problem, not a command failure]`，
  `runnerFailed=True`，这种情况下**不**把命令失败归咎于命令本身。
- 命令被沙箱拒绝：输出追加 `[sandbox: file access denied under <mode> mode]`；若当前 mode 还有更宽的目标，再追加
  `[sandbox: escalation available - retry this exact <subject> once with sandbox_permissions (the narrowest wider mode that suffices) + justification; the approval prompt asks the user]`。
- 工具结果的 `meta["sandbox"]`：`{"mode","enforcement","denied"}`，runner 失败时额外 `runnerFailed=True`。

## 7. 升权闭环

- 只在 `workspace-write` / `read-only` 下、且目标比当前模式严格更宽时允许：`read-only → workspace-write|danger-full-access`，`workspace-write → danger-full-access`；同模式是 no-op，反向/未知目标报错。
- 工具 schema 只有在执行器宣称支持沙箱时才出现 `sandbox_permissions`（enum：workspace-write / danger-full-access）与 `justification`；二者必须成对，缺 justification 报 `invalid escalation: sandbox_permissions requires a justification`。
- 升权请求走 `ApprovalService`（kind=`sandbox-escalation`），一次一审批；拒绝原文：
  `the user rejected escalating this <subject> to "<mode>"; it stays denied, so stop and explain instead of working around it`；
  取消原文：`approval for escalating to "<mode>" was cancelled`。
- fs fence：`write` / `edit` 先 canonicalize 再按 `writable_roots` 判定，写前重新 canonicalize 缓解 TOCTOU；读不限制。拒绝时消息含 denial marker，模式可升时再含 escalation hint。
- shell：`check_command` 恒允许（由 OS 层实际拒绝），随后 `confine` 再 spawn；禁止在 confine 失败时降级执行。

## 8. 配置

`config.toml` / `DEFAULT_TOML`：

```toml
[sandbox]
mode = "workspace-write"          # read-only | workspace-write | danger-full-access
workspaceRoot = ""                # 可选；会话无 cwd 时兜底
additionalWritableRoots = []

[approval]
policy = "ask"                    # ask | never
hookMode = "off"                  # approval-policy 插件
hookTools = []

[permission]
defaultPreset = "workspace-write" # 新会话 pin_initial 的默认命名预设
```

旧的 `[agent].permissionPreset` / `[agent].approvalPolicy` 已删除；`Approval` 类型不再接受 `always`（配置校验会拒绝）。

## 9. TUI 与 headless

- TUI：`/permission [preset]` 无参报告当前预设并打开选择器（`custom` 只读标记），带参切换；状态栏显示 permission 行。`/policy`、`/approval` 已删除。切换要求无运行中 turn；gate 动态从日志解析，下一轮生效。
- headless：`--permission-mode read-only|workspace-write|danger-full-access` 与 `--approval-policy ask|never` 可分别覆盖；`boot_runtime` 在加载配置前校验，非法值 `ConfigError("invalid permission preset: ...")` / `ConfigError("invalid approval policy: ...")`。会话创建后 `pin_permission` 先 `pin_initial`，再按 CLI 覆盖依次 `apply` 与 `set_policy`。
- 子会话（subagent / team 成员）继承父会话的两个旋钮；spawn 时通过 `policy_service.set_mode` 与 `approval_service.set_policy` 写入子会话日志。

## 10. 事件

| 事件 | data | 说明 |
| --- | --- | --- |
| sandbox/mode | `{mode}` | 策略层 mode 切换，log-only |
| approval/policy | `{policy}` | 审批策略切换，log-only |
| permission/preset | `{preset}` | 命名预设切换，log-only |
| approval/asked | `{id, toolName[, callId][, reason]}` | ask 策略下发起一次询问 |
| approval/decided | `{id, outcome}` | allowed-once / rejected / cancelled / unavailable |

## 11. 测试与验证

- `tests/sandbox/test_services.py`：policy/approval/permission 服务、事件幂等、custom 派生、records 顺序。
- `tests/sandbox/test_windows_acl_runner.py`：runner 的 workspace/temp/outside、退出码镜像、read-only、stdio、失败 fail-closed。
- `tests/tools/test_sandbox.py`：gate 的 preset/approval 组合、check_write/check_command、升权校验、hook/answerer 路径。
- `tests/tools/test_fs.py`、`test_shell.py`、`test_shell_jobs.py`：拒绝文案、升权、marker、runner failure、后台任务、temp 清理。
- `tests/tui/test_app.py`、`test_headless.py`：/permission、--permission-mode/--approval-policy、pin_initial 与覆盖。
- 真实冒烟：headless `--json --permission-mode read-only` 下写被拒且输出含 marker；danger-full-access 下可写；`--approval-policy never` 下升权被拒；三种事件按序落盘。

## 12. 已知偏离与限制

- Windows 后端是 **partial** enforcement：只约束文件写，网络与读不限制；POSIX 后端尚未实现（read-only/workspace-write 在非 Windows 上 fail closed）。
- workspace 目录必须对当前用户/Authenticated Users 可写：restricted token 的 normal access check 仍要求 normal SID 有写权限，否则 workspace 内也会被拒。
- `additionalWritableRoots` 在 Windows ACL 后端上不支持（报 SANDBOX_UNSUPPORTED_POLICY）。
- 私有 temp root 每个命令新建，命令结束清理；后台任务在结束时清理。
- 审批没有 always；交互界面只有 once/deny。
