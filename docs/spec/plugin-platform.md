# 插件平台：安装单元、装配顺序与诊断

本文件定义 DeepPyro 的插件平台契约中与「外部插件包」有关的部分：唯一的安装单元是什么、`plugin.toml` 怎么写、插件包如何参与组合、依赖放在哪里、失败如何分类、`dpyro plugin` 子命令做什么。组合层（bundle/patch/清单）的合并语义见 @docs/spec/plugin-core.md；管理器的启用/禁用/删除语义见 @docs/spec/plugins.md；本文件只补充「包格式 + 装配 + 诊断」，不重复定义合并算法。

本层当前覆盖 P0（安装单元、启动期激活、插件包组合层、`dpyro plugin list/info/doctor/path`、`PluginContext.plugin_dir`）与 P1（uv 安装器与 `add/remove/update/enable/disable/allow-version`）。P2（TUI 贡献面 screen/ui 服务）、P3（冻结打包）见 §9。

## 1. 唯一安装单元

一个插件 = 一个目录，位于插件根下（`deeppyro.paths.plugins_dir()`，默认 `~/.dpro/plugins/`，`DEEPPYRO_PLUGINS_DIR` 可覆盖）：

    <plugins_dir>/<id>/
        plugin.toml          唯一权威元数据（必需）
        <python package>/    代码，`module` 指向它
        _vendor/cp313/       依赖（可选，见 §4）
        bundle.toml          组合层（可选，见 §5）
        ui.toml              TUI 声明式贡献面（可选，P2 生效）
        pyproject.toml       发布者用于 uv lock（可选，运行期不读）
        uv.lock              发布者锁文件（可选，运行期不读）

目录名即插件 id，必须与 `[package].id` 一致；不一致视为 `metadata-invalid`。没有 `plugin.toml` 的子目录被忽略（用户 bundle、笔记目录不受影响）。

安装单元只有这一种。entry point 只用于「已经在解释器里装好」的包（开发期），不是发布形态：冻结成 exe 之后 `importlib.metadata` 与 entry point 都可能失效。

## 2. `plugin.toml`

### 2.1 v2（分节，推荐）

    [package]
    id = "dpyro-im"                 # 必须等于目录名
    name = "DeepPyro IM"
    version = "0.1.0"
    description = "QQ/Discord bridge"
    module = "dpyro_im"             # 也可以是 "dpyro_im.plugin:ImPlugin"
    class = "ImPlugin"
    api_version = 2                 # 必填，整数，不得大于宿主 API_VERSION
    provides = ["im"]               # 可选，仅用于展示

    [compat]
    dpyro = ">=0.1,<0.2"            # 逗号分隔的约束列表
    python = ">=3.12,<3.14"
    dpyro_exempt_versions = []      # 精确版本豁免（对齐 DSH 的 exact-version 豁免）

    [deps]
    vendor = "_vendor"              # 默认值
    requires = ["websockets>=15"]   # add/update 用它重建 _vendor；doctor 也校验

    [ui]
    slots = ["status", "panel"]     # 未知槽位在注册期报错；声明期只做白名单校验
    package = ""

    [bundle]
    patch = "bundle.toml"

| 节 | 键 | 语义 |
| --- | --- | --- |
| `[package]` | `id` | 必须是目录名；缺省时取目录名 |
| | `module` / `class` | 目标类；`module` 支持 `pkg.mod:Class` 简写 |
| | `api_version` | 宿主契约版本，超界即 `version-incompat` |
| | `provides` | 展示用服务名列表 |
| `[compat]` | `dpyro` / `python` | 分别与宿主版本、解释器版本比对 |
| `[deps]` | `vendor` / `requires` | 依赖目录名、声明的依赖 |
| `[ui]` | `slots` / `package` | 声明式 UI 入口（P2） |
| `[bundle]` | `patch` | 包内组合层文件名（默认 `bundle.toml`） |

未知键不报错，原样保留在 `PluginMetadata.raw`，便于后续格式扩展。UTF-8 BOM 被容忍（Windows 编辑器常写 BOM）。`plugin.toml` 语法错误、字段类型错误、`id` 与目录名不一致、缺少 `module`/`class` 都归入 `metadata-invalid`。

### 2.2 旧版扁平格式（继续支持）

    name = "Demo Plugin"
    version = "0.1.0"
    description = "a demo plugin"
    module = "demo_plugin"
    class = "DemoPlugin"
    provides = ["demo"]

扁平格式没有 `[package]` 节时 `api_version` 缺省为宿主 `API_VERSION`，因此升级后已有的插件目录照常加载；一旦使用分节格式，`api_version` 必须显式声明。

## 3. 启动期激活

`deeppyro.kernel.app.create_app()` 在构建组合之前调用 `deeppyro.plugins.bootstrap.prepare()`：

1. 扫描插件根下每个含 `plugin.toml` 的目录，解析元数据并做 `[compat]` 校验（不 import 插件代码，不抛异常）；
2. 按 id 排序，依次把 `<dir>` 与其 vendor 目录插入 `sys.path` 头部（已存在则跳过，幂等）；
3. 校验失败的包只记录失败分类，不阻断其它包。

这消除了旧实现中「只有 plugin-manager 挂载后才可 import 插件模块」的顺序耦合：任何组合行（包括清单里直接写的 `module:Class`）都能在解析时 import。

清单层的某一行若指向已删除的目录、或 `module:Class` 无法 import，该行降级为 `state=failed` 并在 `App.row_states()` 的 `error` 里给出原因，其余行继续启动（不再整机 brick）；`doctor` 把这类行报为 `manifest-dangling`。

`create_app(activate_plugins=False)` 供自管 `sys.path` 的嵌入方关闭该步骤。

## 4. 依赖与 `_vendor`

插件依赖必须落在插件包内部，不写宿主的 site-packages：

    <dir>/_vendor/cp313/     按解释器 tag 分目录（推荐）
    <dir>/_vendor/           不带 tag 的兜底目录

查找顺序 `_vendor/cp<major><minor>` → `_vendor`。加载器只把存在的目录加入 `sys.path`，不创建、不修改。

- 依赖安装由 uv 完成（`dpyro plugin add` / `update`）：`uv pip install --python <host python> --target <dir>/_vendor/cp313 -r requirements.txt`。
- `[deps].requires` 参与安装：`add` 在预检通过后把它导出成 `requirements.txt` 交给 uv（预检只拦截语法非法的 requirement），`--no-deps` 跳过该步骤；`update` 同样按它重建 `_vendor`。发布者提供了 `pyproject.toml` / `uv.lock` 时才改用 `uv lock --upgrade` + `uv export`。
- 若 `_vendor/cpXY` 里存在与宿主 tag 不同的二进制扩展（`.pyd`/`.so`），`doctor` 报 `abi-mismatch`。
- 所有插件共享同一进程的 `sys.modules`：两个包 vendor 同名不同版本的顶层包属于未定义行为；`add` 预检与 `doctor` 都对全量包做跨包比对并报 `dep-conflict`。

## 5. 组合层：插件包自带的 `bundle.toml`

插件包可以携带自己的组合层，格式与普通 bundle 完全一致（`[bundle]` 表 + `[[plugins]]` 行），因此天然获得「新 id 插入、已有 id 整键覆盖」的语义，无需第二套补丁格式。

`deeppyro.kernel.composition.default_composition()` 收集层为：

1. 内置 core bundle（`src/deeppyro/bundles/core/bundle.toml`）；
2. 显式 `bundle_paths`；
3. 自动发现的用户 bundle（`~/.dpro/bundles/*/bundle.toml`）；
4. **插件包 bundle.toml**（`<plugins_dir>/*/bundle.toml`，按目录名排序）；
5. `extra_bundle_paths`；
6. 用户清单 `~/.dpro/plugins.toml`（补丁层，最后应用）。

前五层收集完成后按 `(order, id)` 排序再依次 apply，缺文件等于没有该层，坏文件记入 `composition.warnings` 而不阻断 boot；清单层失败同样只记录。核心行可以覆盖 config、可以禁用，但不能改变实现类（`cannot change class`），也不能从组合里删除。

## 6. 失败分类

插件包内的失败归入六类之一，每类带一句可执行的补救建议；另有一类清单悬挂诊断 `manifest-dangling`（不是包内失败，但同样让 `doctor` 失败）：

| 分类 | 含义 | 补救 |
| --- | --- | --- |
| `metadata-invalid` | `plugin.toml` 缺失/语法错误/字段类型错误/id 不一致/缺 `module` 或 `class`/`bundle.toml` 无法解析 | 修 `plugin.toml` |
| `import-failed` | 声明的 `module:Class` 无法 import | `doctor` 看 traceback |
| `dep-missing` | `[deps].requires` 在 vendor 目录里找不到或版本不满足 | `dpyro plugin update <id>` 重建 `_vendor`，或 `dpyro plugin add <id>` 重装 |
| `dep-conflict` | 两个包 vendor 了同名不同版本 | 卸载其一 |
| `abi-mismatch` | vendor 里的二进制扩展不是当前解释器 tag | 按当前 Python 重建 `_vendor` |
| `version-incompat` | `[compat].dpyro` / `[compat].python` / `api_version` 不接受当前宿主 | 升级插件或宿主，或用 `dpyro plugin allow-version <id> <version>` 豁免 |
| `manifest-dangling` | 用户清单里的行指向不存在的包目录，或其中的 `module:Class` 无法 import | 从清单删掉该行，或重新 `dpyro plugin add <id>` |

## 7. `dpyro plugin`

    dpyro plugin list [--json]
    dpyro plugin info <id>
    dpyro plugin doctor [<id>] [--json]
    dpyro plugin path
    dpyro plugin add <registry spec | dir | wheel/tarball | git url> [--force] [--no-deps] [--offline] [--json]
    dpyro plugin remove <id> [--force] [--json]
    dpyro plugin update <id> [--offline] [--json]
    dpyro plugin enable <id> [--json]
    dpyro plugin disable <id> [--json]
    dpyro plugin allow-version <id> <version> [--json]

- `list`：表格（id / version / api / provides / vendor tag / status）；有异常时追加一行 `unhealthy: ...`。
- `info`：输出该包的完整元数据 JSON（含 `raw`）。
- `doctor`：逐包输出 `ok` 或 `FAIL` + 每类失败的说明与补救建议；同时检查用户清单里的行是否仍指向可安装、可 import 的包（`manifest-dangling`，退出码 `2`）；`--json` 输出机器可读结果（清单问题额外带 `manifestDangling: true`）。
- `path`：打印插件根目录。
- `add`：三段式。预检（源解析、`plugin.toml` 校验、`check_compat`、包名/版本合法性、目标是否已存在、磁盘可写、与已装包的 `_vendor` 版本冲突，并临时把包目录与 vendor 插入 `sys.path` 实际 import `module:Class`、要求它是 `deeppyro.api.Plugin` 子类）不过则退出码 `2`，不落任何文件；安装（目录拷贝或归档安全解压到临时目录，必要时用 uv 安装 `[deps].requires`）任一步失败则回滚并退出码 `3`；依赖安装完与包目录原子落位后各复验一次 `module:Class` 可导入（失败则回滚并退出码 `3`），全部通过后才用 `PluginManager` 把 id 写入清单，确有新增/改动时退出码 `4`。`--force` 允许替换同名包，`--no-deps` 跳过依赖安装（缺依赖降级为 warning），`--offline` 透传给 uv。
- `remove`：删除包目录、清单条目与 `__pycache__`；被其它包的 `[deps].requires` 或 `bundle.toml` 的 `[bundle].requires` 依赖时拒绝并列出依赖者（退出码 `2`），`--force` 可越过。若包目录已丢失但清单仍残留该 id，`remove` 会幂等删行（报告与退出码体现象限变化）。
- `update`：有 `pyproject.toml` 的包走 `uv lock --upgrade` + `uv export` + `uv pip install`，否则按 `[deps].requires` 重建 vendor；先在新目录构建，成功后原子替换旧 `_vendor`，失败恢复 `uv.lock` 与旧 vendor；打印 `包名: 旧版本 -> 新版本` 的 diff，确有变化时退出码 `4`。
- `enable` / `disable`：薄封装管理器写清单的 `disabled` 标记；清单字节变化时退出码 `4`，幂等时 `0`。
- `allow-version`：把精确版本追加进 `[compat].dpyro_exempt_versions`；写前备份 `plugin.toml.bak`，写后重新 `load_metadata` 校验，失败还原原文；改动时退出码 `4`。
- 所有动词都支持 `--json`；失败时输出 `{ok: false, kind, error, problems?, stderrTail?}`。

退出码：`0` 成功（含幂等无改动）；`1` 用法错误（未知 id/未知动词）；`2` 预检或诊断失败（没有任何文件被改动）；`3` 安装/升级失败且已回滚（`--json` 会说明回滚是否完整）；`4` 需要重启才能生效。

`--json` 既可放在动词前也可放在动词后。TUI 的 `/plugins` 屏与 `dpyro plugin doctor` 共用 `deeppyro.plugins.cli.diagnose()`，两者显示同一份健康状态（失败类别与补救建议）；TUI 健康扫描还会传入 `known_slots=KNOWN_SLOT_KINDS`，因此 `[ui].slots` 的未知槽位会直接标红。

## 8. Python API

@src/deeppyro/plugins/metadata.py：

- `load_metadata(source, *, plugin_id=None, known_slots=None) -> PluginMetadata`、`parse_metadata(document, ...)`；
- `PluginMetadata`（`id/name/version/description/module/class_name/api_version/directory/provides/compat/deps/ui/bundle/raw`，属性 `target`、`toml_path`、`with_directory()`）；
- `check_compat(...) -> list[MetadataError]`、`satisfies(version, spec)`、`python_tag()`、`vendor_candidates()`、`find_vendor_dir()`；
- `MetadataError(kind, message, *, plugin_id, remediation)` 与 `REMEDIATION`、`FAILURE_KINDS`、`describe_failure()`。

@src/deeppyro/plugins/bootstrap.py：

- `scan_plugin_packages(root=None) -> list[PluginPackage]`（按 id 排序，坏包以 `errors` 呈现）；
- `PluginPackage`（`loadable`、`target`、`provides`、`error`、`failure_kind()`）；
- `prepare(root=None) -> Activation`、`activate(packages, *, path=None)`、`activation_paths()`、`load_package(id, root=None)`。

@src/deeppyro/plugins/install.py：

- `add(source, *, root=None, force=False, no_deps=False, offline=False, runner=None, manager=None, timeout=None) -> InstallReport`、`remove(...) -> RemoveReport`、`update(...) -> UpdateReport`、`set_enabled(...) -> StateReport`、`allow_version(...) -> AllowVersionReport`；
- `PreflightFailure`（`problems`）、`InstallFailure`（`rolled_back`、`stderr_tail`）、`UnknownPlugin`、`InstallerError`；
- uv 通过 `Runner = Callable[[UvCommand], UvResult]` 注入（测试不需要真实 uv 或网络），`DPYRO_UV` 覆盖可执行文件路径；`run_uv` 默认超时 600s，stderr 只保留最后 200 行；
- 诊断辅助 `vendor_versions()`、`vendor_conflict_map()`、`requirement_errors()`、`customized_core()`、`dependents_of()`、`target_errors()`（导入校验）、`manifest_issues()`（`manifest-dangling`）。

插件内可读 `PluginContext.plugin_dir`（`Path | None`）：包式安装时返回包目录，in-tree 插件返回 `None`。

## 9. 与 DSH 的对应、偏离与后续

对应：单一安装单元、`plugin.toml` 作为唯一权威元数据、`[compat]` 对齐 `engines.dsh`、整键覆盖的补丁层、启动审计（doctor）、「预检不过不安装」的 P1 安装器。

有意偏离：`_vendor/`（Python 没有 per-package 解析，依赖必须落到宿主进程可见目录）；不做 profile 多环境（用 `DEEPPYRO_HOME` 替代）；entry point 不是发布形态。

已实现（按里程碑）：

- P1：`add/remove/update/enable/disable/allow-version`、uv 安装后端（`--offline`/`--no-deps`、`DPYRO_UV`、注入式 runner）、预检不过零安装、临时目录 + `os.replace` 原子落位与失败回滚、`_vendor` 快照 diff、跨包 `dep-conflict` 检测、`allow-version` 精确版本豁免、`add` 的 `module:Class` 导入校验（预检与落位后复验）、`doctor` 的 `manifest-dangling`、清单坏行启动降级（`state=failed`）。
- P2：`ui.toml` 声明（含 `[[screens]]`）与 `screen` 槽位、Textual-free `ui` 服务（线程安全动作队列，TUI 每 0.1s drain）、`/plugins` 屏健康状态（复用 `diagnose()`）。接线与红线见 `docs/spec/tui-contributions.md`。
- P3：`packaging/`（spec + launcher + `hiddenimports` 生成器 + `build.ps1`）、`--internal-sandbox-runner` 与 `--internal-ptc-worker` 自调子命令、`src/deeppyro/runtime_env.py` 资源定位、`docs/spec/frozen-distribution.md`（插件代码不冻结、依赖走 `_vendor/`、插件目录只读）。

未实现（按里程碑）：

- 无。P1 已交付；后续扩展仍需先更新本文件。
