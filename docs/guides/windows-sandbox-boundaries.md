# Windows 沙箱边界：enforcement=partial 到底拦什么

> 对应 KI-020（Windows 沙箱 enforcement=partial 的边界不清）。本文面向
> 使用者与排障者，说明 Windows 上本地沙箱**实际**约束了什么、依赖哪些 ACL
> 与令牌机制、以及如何验证。权威契约见
> [spec/sandbox.md](../spec/sandbox.md) §6（执行层）与 §12（已知偏离）；
> 0.2.0 已知问题总表由发布文档维护者维护，本文不改写其状态。

**一句话**：Windows 后端是 **partial enforcement**——只有**文件写**被 ACL
约束，且只允许写 workspace 与私有 temp；网络、读、进程、注册表、环境变量等
都不受沙箱约束。非 Windows 平台没有可用后端，read-only / workspace-write
直接 fail closed。危险全权模式（danger-full-access）不经过任何约束。

## 1. 两个观察口径

沙箱事实有两个独立的出口，不要混用：

| 口径 | 位置 | 内容 | 生命周期 |
| --- | --- | --- | --- |
| **宿主级事实** | capability report 的 `sandbox` 字段 | `{platform, enforcement, reason}` | 启动时按平台生成，Windows 恒为 `partial` |
| **单命令事实** | 工具结果 `meta["sandbox"]` | `{mode, enforcement, denied[, runnerFailed]}` | 每次 shell/pwsh 调用由 live `Confinement` 决定 |

宿主级事实的实现：

- `src/deeppyro/kernel/capabilities.py:228-249` —— `SandboxBoundary`（frozen
  dataclass，`to_dict()` 输出 `platform/enforcement/reason`）；
- `src/deeppyro/kernel/capabilities.py:251-277` —— `host_sandbox_boundary()`；
- `src/deeppyro/kernel/capabilities.py:505` —— `CapabilityReport.sandbox`
  （序列化在 `CapabilityReport.to_dict()`，`src/deeppyro/kernel/capabilities.py:621` 起）；
- `src/deeppyro/kernel/app.py:395` `capability_report()` /
  `src/deeppyro/kernel/app.py:403` `capability_dict()`。

Windows 上报告的取值固定为：

```json
{
  "platform": "windows",
  "enforcement": "partial",
  "reason": "the Windows ACL restricted-token backend confines file writes only: workspace and private-temp writes are limited by ACLs granted to a synthetic SID, while network access, reads, and process/registry operations are not constrained; danger-full-access runs unsandboxed"
}
```

`reason` 不是装饰：它是机器可读的边界声明，测试会断言其中同时出现
“file writes only”“ACL”“network”“not constrained”等关键词
（`tests/kernel/test_sandbox_boundary.py::test_host_sandbox_boundary_matches_the_platform`），
避免文档与代码漂移。但 `reason` 只是摘要，不是完整清单：可执行声明与覆盖
状态见 §8，明确不声明的事项见 §9。

非 Windows 上同一字段是 `{"platform": "posix", "enforcement":
"unavailable", ...}`，表示 read-only / workspace-write 无法执行。

单命令口径来自沙箱插件返回的 live `Confinement`
（`src/deeppyro/sandbox/provider.py:89-96`）：shell 工具把
`mode/enforcement` 写进结果 `meta["sandbox"]`
（后台任务同样带，`src/deeppyro/tools/builtin/shell.py:188`），前台
命令额外带 `denied` 与（runner 自身失败时）`runnerFailed`
（`src/deeppyro/tools/builtin/shell.py:245-258`）。

## 2. 被拦的东西（workspace-write）

只有一类操作被沙箱真正拦住：**文件写**。

- **workspace 内**：允许。runner 会给 workspace 目录加一条可继承 ACE，授权
  给由 workspace 路径派生的合成 SID，然后以 `WRITE_RESTRICTED` 方式启动
  受限令牌子进程；受写 SID 匹配的路径才能写。
- **私有 temp 内**：允许。runner 为每个命令新建私有 temp 目录
  （`%TEMP%\deeppyro-<uuid16>`，`src/deeppyro/sandbox/provider.py:219-226`），
  给同一 SID 加 ACE，命令结束后 revoke；`TEMP`/`TMP` 环境变量在子进程里
  被指向该目录。
- **workspace 与 temp 之外的写**：拒绝（`PermissionError` / access
  denied）。包括新建、追加、覆盖、删除、重命名、改属性等文件系统写操作。
- **read-only 模式**：不授予任何写 ACE（不给写 SID），所有文件写被拒绝；
  写操作会以普通访问被拒的形式出现，而不是“策略拒绝”。

被拒时工具输出追加固定标记
`[sandbox: file access denied under <mode> mode]`
（`src/deeppyro/sandbox/provider.py:99-101`）；当前模式还有更宽目标时再追加
升权提示（`src/deeppyro/sandbox/provider.py:104-110`）。注意这依赖输出文本
匹配四条 denial 签名（`src/deeppyro/sandbox/provider.py:35-40`），因此
**命令自己打印 “access is denied” 也可能被识别成沙箱拒绝**；结果里的
`meta["sandbox"]["denied"]` 只是启发式标记，不是内核事实。

## 3. 不被拦的东西（partial 的边界）

沙箱**不**拦截以下操作，它们以当前用户的正常权限运行：

| 能力 | 是否约束 | 说明 |
| --- | --- | --- |
| 网络（TCP/UDP、DNS、代理、localhost 服务） | 否 | 没有网络命名空间/防火墙规则 |
| 读文件 | 否 | 整个文件系统按当前用户权限可读；读不经过 ACL 写 SID |
| 启动/枚举进程、创建线程 | 否 | 只有写文件受 ACL 限制 |
| 注册表写（HKCU 等） | 否 | 注册表不在文件 ACL 约束内 |
| 服务/计划任务/驱动操作 | 否 | 普通权限下能做的都能做 |
| 环境变量 | 否（TEMP/TMP 例外） | 只有 TEMP/TMP 被重定向到私有 temp |
| 剪贴板、窗口、COM 等用户态操作 | 否 | 无桌面/会话隔离 |
| 向 stdout/stderr 写数据、退出码 | 否 | 原样镜像 |
| 命名管道、共享内存等 IPC 写入 | 未验证 | 不属于文件 ACL 的写路径；没有专门断言，不能假定被拦 |
| COM/RPC 服务端写入、跨进程副作用 | 未验证 | 无用户态隔离；与剪贴板/窗口同类，未做探针验证 |

> 表中“否”表示**没有专门的约束机制**，不等于“已经被自动化测试证明不受约束”。
> 目前没有断言覆盖网络/读/进程/注册表/服务/用户态操作的“不受约束”这一侧；
> 覆盖状态见 §8，明确不声明项见 §9。

因此，“跑在沙箱里”在 Windows 上的准确含义是**文件写受限**，不是
“行为被隔离”。需要防网络外传或防读取的负载，不能依赖本地沙箱。

## 4. 依赖的 ACL / 令牌机制

partial enforcement 不是策略层模拟，而是真实的 OS 机制组合；任何一环不可用
就 fail closed（命令不执行），不会降级成无约束执行。

1. **合成写 SID**：`workspace_write_sid(workspace)`
   （`src/deeppyro/sandbox/windows_acl_runner.py:65-75`）对
   `os.path.realpath(workspace)` 的 UTF-8 字节做 sha256，取前两个
   little-endian u32 映射进 `S-1-4-<n1>-<n2>` authority。同一 workspace
   路径稳定得到同一 SID，所以 workspace 的 ACE 可以常驻复用。
2. **ACE 授权**：workspace 目录加**可继承** Modify 类 ACE
   （`_grant_dir_ace`，`src/deeppyro/sandbox/windows_acl_runner.py:463-498`），
   掩码 `GRANT_MASK = 1114454`（含 write+delete；刻意**不含**
   `WRITE_DAC`/`WRITE_OWNER`，见
   `src/deeppyro/sandbox/windows_acl_runner.py:178`）。workspace ACE 常驻；
   私有 temp 的 ACE 在命令结束时 revoke（`_revoke_dir_ace`，
   `src/deeppyro/sandbox/windows_acl_runner.py:500-533`）。
3. **受限令牌**：`CreateRestrictedToken`
   （`src/deeppyro/sandbox/windows_acl_runner.py:574-608`；令牌组装在
   `_build_token` :600-608），标志
   `RESTRICTED_TOKEN_FLAGS = 13` = `DISABLE_MAX_PRIVILEGE |
   LUA_TOKEN | WRITE_RESTRICTED`；restricting SIDs 为“当前登录 SID、World
   SID、合成写 SID”（read-only 模式没有写 SID）。`WRITE_RESTRICTED` 使写
   访问检查只认可 restricting SID 的写授权——这是“workspace 外写被拒”的
   真正来源。
4. **默认 DACL 补丁**：受限令牌的 default DACL 会追加写 SID 的
   `FILE_ALL_ACCESS`（`_set_token_default_dacl_grant`，
   `src/deeppyro/sandbox/windows_acl_runner.py:535-572`），保证子进程新创建
   的对象（文件、管道等）对同一 SID 可访问。
5. **普通访问检查仍生效**：受限令牌是“叠加限制”，normal SID 的访问检查
   不会消失。**workspace 目录必须对当前用户（或 Authenticated Users）可写**，
   否则即使沙箱本来允许，workspace 内写也会被普通 ACL 拒绝。
6. **进程生命周期**：子进程通过 `CreateProcessAsUserW(CREATE_SUSPENDED)`
   启动，先加入 `KILL_ON_JOB_CLOSE` 的 job object 再 resume
   （`_create_kill_on_close_job` :610-621、`_spawn_restricted` :663-721，
   `src/deeppyro/sandbox/windows_acl_runner.py`），
   父进程退出/被杀时后台子进程随之被杀。stdin/stdout/stderr 句柄继承。
7. **fail closed**：runner 任何失败都写 stderr
   `windows-acl-run: <detail>`、退出码 127
   （`src/deeppyro/sandbox/windows_acl_runner.py:1-20,38,133-152`），
   且失败发生在启动子进程之前。provider 把 `(127, "windows-acl-run: ")`
   注册为 runner-failure 规则（`src/deeppyro/sandbox/provider.py:214`），
   工具据此区分“命令失败”和“沙箱自身失败”。

由此带来的直接推论：

- workspace 的 ACE 是**常驻**的，同一个 workspace 会留下
  `S-1-4-...` 的继承 ACE。多个 workspace 各自有不同 SID；删除 workspace
  后 ACE 随目录消失，不会影响其他目录。
- ACL 操作要求当前进程对 workspace 有 `WRITE_DAC`；普通用户在自己目录里
  默认满足，系统目录/只读共享上不满足 → runner 失败，命令不执行。
- 受限令牌去掉管理员特权（`DISABLE_MAX_PRIVILEGE`），命令不能提权；
  它仍以**当前用户身份**运行，所以用户能做的非文件写操作（见 §3）都能做。

## 5. 前置条件与已知限制

| 限制 | 表现 |
| --- | --- |
| 非 Windows | `read-only`/`workspace-write` 抛 `SandboxUnavailableError`，命令不执行（`src/deeppyro/sandbox/provider.py:179-180`） |
| `additionalWritableRoots` 非空 | 抛 `SandboxUnsupportedPolicyError`（`src/deeppyro/sandbox/provider.py:181-185`）；Windows ACL 后端只能表达一个 workspace (+私有 temp) |
| workspace 不可写 | workspace 内写也被拒；runner 可能因无法设置 ACE 而直接失败（exit 127） |
| 私有 temp | 每命令新建，命令结束后由消费方删除（`src/deeppyro/tools/builtin/shell.py:108-111`）；ACL 在 runner 退出时 revoke |
| 网络/读/进程/注册表 | 不受约束（partial 的定义） |
| 审批 | 只有 once/deny；没有 always（配置会拒绝 `always`） |
| 后台任务 | 后台 shell 也走 confinement；任务结束时清理 temp |

## 6. 如何验证

### 6.1 断言 report 事实

```python
import asyncio

from deeppyro.config import Config  # 或以你的配置加载函数构造
from deeppyro.kernel.app import create_app
from deeppyro.kernel.capabilities import host_sandbox_boundary

print(host_sandbox_boundary().to_dict())  # 不启动 kernel 也能看平台事实

async def main() -> None:
    config = Config.model_validate({"agent": {"provider": "local", "model": "m"}})
    app = await create_app(config)
    print(app.capability_dict()["sandbox"])

asyncio.run(main())
```

Windows 上期望 `platform == "windows"`、`enforcement == "partial"`、
`reason` 非空且包含“file writes only”“network”“not constrained”。

### 6.2 直接探测 provider 的边界（推荐）

```python
import shutil
import subprocess
from pathlib import Path

from deeppyro.sandbox.policy import SandboxPolicy
from deeppyro.sandbox.provider import LocalSandboxProvider

workspace = (Path.cwd() / "probe-workspace").resolve()
workspace.mkdir(exist_ok=True)
policy = SandboxPolicy(mode="workspace-write", workspace_root=workspace)
provider = LocalSandboxProvider()

# 1) workspace 内可写
confinement = provider.confine(["cmd", "/c", "echo inside> inside.txt"], policy)
print("enforcement:", confinement.enforcement)  # partial
run = subprocess.run(confinement.argv, capture_output=True, text=True, cwd=workspace)
print("inside exit:", run.returncode)

# 2) workspace 外不可写
outside = (workspace.parent / "probe-outside").resolve()
outside.mkdir(exist_ok=True)
confinement = provider.confine(["cmd", "/c", f'echo nope> "{outside / "nope.txt"}"'], policy)
run = subprocess.run(confinement.argv, capture_output=True, text=True, cwd=workspace)
print("outside exit:", run.returncode, run.stdout + run.stderr)  # access denied

# 3) 网络/读不受限（示例：localhost 连接或读任意可读文件）
confinement = provider.confine(["cmd", "/c", "type C:\Windows\win.ini > nul"], policy)
run = subprocess.run(confinement.argv, capture_output=True, text=True, cwd=workspace)
print("read exit:", run.returncode)  # 0：读没有被沙箱拦截

shutil.rmtree(confinement.temp_root, ignore_errors=True)  # 清理私有 temp
```

预期：`enforcement == "partial"`；workspace 内 `exit 0`；workspace 外
失败并出现 access denied 文本；workspace 外读取成功（证明“只拦写、不拦读”）。

这个探针只验证“写被拦、读没被拦”。网络、进程、注册表在 §3 的否定声明
**没有**自动化断言（见 §8），不要把它外推成“其它通道也已被测试证明”。

### 6.3 看 workspace 上的 ACL 残留

```powershell
icacls path\to\probe-workspace
```

应能看到一条 `S-1-4-<n1>-<n2>` 的 ACE（继承 flags 类似 `(OI)(CI)`，权限串
反映 write+delete，具体显示随 icacls 版本而异）。对比
`windows_acl_runner.workspace_write_sid(workspace)` 的返回值可以确认是
同一 SID。

### 6.4 跑测试

```powershell
.venv\Scripts\python.exe -m pytest -q --no-header -p no:cacheprovider ^
  tests/kernel/test_sandbox_boundary.py ^
  tests/sandbox/test_windows_acl_runner.py ^
  tests/tools/test_shell.py tests/tools/test_fs.py
```

`tests/kernel/test_sandbox_boundary.py` 断言 Windows report 的
`enforcement == "partial"`、`reason` 关键词与 report 的默认值/registry/boot
序列化；runner/shell 测试覆盖 workspace/temp/outside 写、read-only、退出码镜像、
stdio、fail-closed、marker 与 `runnerFailed`。逐条声明到具体 node id 的映射见
§8；**§8 中标为“仅手工探针”或“代码事实（无自动断言）”的声明不在这条命令的
覆盖范围内**。

### 6.5 区分三种失败

| 现象 | 含义 | 处理 |
| --- | --- | --- |
| 输出含 `[sandbox: file access denied under <mode> mode]` | 命令被沙箱 ACL 拒绝（写 workspace 外） | 改写到 workspace 内；或按提示申请一次升权 |
| 输出含 `[sandbox: the sandbox runner itself failed under <mode> mode — the command did not run; this is a sandbox problem, not a command failure]`，`meta["sandbox"]["runnerFailed"] == true`，exit 127 | 沙箱基础设施失败（ACL/令牌/子进程创建），命令**没有运行** | 检查 workspace 是否存在且当前用户可写/可改 ACL；不要把它当命令 bug |
| 普通非零退出码、无 marker | 命令自身失败 | 按命令错误处理 |

## 7. 实现位置与测试映射

| 主题 | 位置 |
| --- | --- |
| confine 契约与 enforcement 取值 | `src/deeppyro/sandbox/provider.py:166-216` |
| Windows runner 入口/fail closed | `src/deeppyro/sandbox/windows_acl_runner.py:1-20,133-152` |
| 合成写 SID | `src/deeppyro/sandbox/windows_acl_runner.py:65-75` |
| ACE 授予/回收 | `src/deeppyro/sandbox/windows_acl_runner.py:463-533` |
| 受限令牌与默认 DACL | `src/deeppyro/sandbox/windows_acl_runner.py:535-608` |
| job object / 子进程启动 | `src/deeppyro/sandbox/windows_acl_runner.py:610-621,663-721` |
| 工具结果事实与 marker | `src/deeppyro/tools/builtin/shell.py:84-111,188,245-258` |
| report 的 sandbox 事实 | `src/deeppyro/kernel/capabilities.py:228-277,505`、`src/deeppyro/kernel/app.py:395-406` |
| 测试 | `tests/kernel/test_sandbox_boundary.py`、`tests/sandbox/test_windows_acl_runner.py`、`tests/tools/test_shell.py`、`tests/tools/test_fs.py` |

## 8. 声明与可执行证据矩阵

把本文的每条边界声明映射到具体证据。**覆盖状态**只有三种，不要混写：

- **自动**：有 pytest node 在跑，可作为门禁；
- **仅手工探针**：只能按 §6.2/§6.3 人工验证，没有自动断言；
- **代码事实**：由实现直接决定，但没有测试断言，改动时需人工复核。

| # | 声明 | 证据 | 覆盖状态 |
| --- | --- | --- | --- |
| 1 | Windows 宿主级 report 为 `platform=windows`、`enforcement=partial`，`reason` 含 file writes only / ACL / network / not constrained 关键词 | `tests/kernel/test_sandbox_boundary.py::test_host_sandbox_boundary_matches_the_platform` | 自动（Windows） |
| 2 | report 默认值、registry 生成、boot 后 `capability_dict()` 都携带同一 sandbox 事实 | 同文件 `::test_capability_report_defaults_to_the_host_boundary`、`::test_registry_report_carries_the_sandbox_boundary`、`::test_booted_capability_dict_exposes_the_boundary` | 自动 |
| 3 | workspace 内文件写允许，workspace 外文件写被拒 | `tests/sandbox/test_windows_acl_runner.py::test_workspace_write_confines_child_writes` | 自动（Windows） |
| 4 | read-only 拒绝所有文件写，且不改动 workspace ACL | 同文件 `::test_read_only_denies_all_writes`、`::test_read_only_does_not_touch_workspace_acl` | 自动 |
| 5 | workspace 的写 ACE 常驻可复用；私有 temp 的 ACE 每条命令结束撤销 | 同文件 `::test_workspace_grant_is_standing_and_temp_grant_is_revoked` | 自动 |
| 6 | runner 自身失败时 fail closed：命令不执行、stderr 以 `windows-acl-run:` 开头、退出码 127 | 同文件 `::test_cli_failure_exits_127_with_prefixed_stderr`、`::test_main_failure_is_fail_closed` | 自动 |
| 7 | 子进程退出码镜像、stdio 继承、合成写 SID 稳定且按 workspace 区分 | 同文件 `::test_child_exit_code_is_mirrored`、`::test_child_stdio_is_inherited`、`::test_workspace_write_sid_is_stable_and_well_formed`、`::test_workspace_write_sid_differs_per_workspace` | 自动 |
| 8 | 工具层：denial marker 启发式、`denied` 标记、`runnerFailed` 区分、私有 temp 清理、read-only 走 runner | `tests/tools/test_shell.py::test_pwsh_marks_a_sandbox_denial_and_offers_escalation`、`::test_pwsh_reports_a_runner_failure_as_a_sandbox_problem`、`::test_pwsh_removes_the_private_temp_root_after_the_run`、`::test_pwsh_runs_in_read_only_mode_through_the_runner` | 自动 |
| 9 | 网络、读文件、进程、注册表、服务、用户态操作**不受沙箱约束** | §6.2 探针第 3 步只演示“读”没被拦；无专门断言 | 仅手工探针（声明，未自动覆盖） |
| 10 | danger-full-access 完全不经过沙箱 | `src/deeppyro/sandbox/provider.py:177-178` 直接返回 `enforcement="full"` 的 `Confinement` | 代码事实（无自动断言） |
| 11 | `additionalWritableRoots` 非空时拒绝执行 | `src/deeppyro/sandbox/provider.py:181-185` 抛 `SandboxUnsupportedPolicyError` | 代码事实（无自动断言） |
| 12 | 非 Windows 平台 read-only / workspace-write 在启动命令前 fail closed | `src/deeppyro/sandbox/provider.py:179-180`；`test_sandbox_boundary.py` 非 Windows 分支只断言 report 文案含 fail closed | 部分自动（仅 report 文案）；本仓库 CI 无非 Windows runner，执行路径未验证 |
| 13 | 命令自己打印 “access is denied” 也可能被标记为沙箱拒绝 | `src/deeppyro/tools/builtin/shell.py:100-105` 的文本匹配启发式 | 已知局限（无测试，行为故意保持） |
| 14 | 本文**不**声称的隔离保证（VM/网络/身份/插件进程内） | 见 §9 | 非声明 |

## 9. 明确不声明的事项（non-claims）

以下都是**有意不声明**的能力。改动沙箱后端或发布说明时，不得把它们写成已提供：

- **不是 VM / 容器 / OS 级隔离**，也不切换登录会话或用户身份：受限令牌子进程
  仍以**当前用户**身份运行，只是叠加了写限制 SID（§4.3）。
- **不是“防恶意代码”的安全边界**：partial 后端下，被执行命令仍拥有当前用户的
  网络、读文件、进程、注册表、服务与用户态权限（§3）。
- **不约束非文件通道**：命名管道/共享内存等 IPC、COM/RPC 服务端写入、剪贴板、
  窗口/桌面均未验证被拦（§3 脚注），不能假定“写不了文件就出不去”。
- **不防数据外泄、不防读取**：只限制文件写路径。需要这些保证的负载必须使用
  真正的 OS/虚拟化隔离，这不在 0.2.0 范围内。
- **不是插件隔离**：插件在 kernel 进程内受信运行（KI-021）；沙箱只约束
  shell/pwsh 启动的子进程，不约束进程内的插件代码。
- **不支持 additionalWritableRoots**：Windows 后端只能表达一个 workspace
  加每命令私有 temp（§5）。
- **不提供跨命令/跨会话隔离**：私有 temp 每命令一个；workspace 写 SID 的 ACE
  常驻是设计（同一 workspace 复用），不是泄漏。
- **审批没有 always**：只有 once/deny（§5）。
- **非 Windows 没有本地后端**：read-only/workspace-write 在启动命令前 fail
  closed，不会降级为无约束执行。
- **danger-full-access 有意完全不沙箱**：这是显式选择，不是遗漏。
- **本文不改变 KI-020 状态**：KI-020 仍是 `accepted`/partial。把
  `enforcement` 提升到 `full` 需要新的后端实现与 ADR，不能靠改
  文档或加测试断言完成。

与 release 文档的关系：KI-020 的关闭状态由
`docs/release/0.2.0-known-issues.md` 维护者更新；本文只提供边界说明、验证
方法与 §8/§9 的可执行声明清单，不改变已知问题表。
