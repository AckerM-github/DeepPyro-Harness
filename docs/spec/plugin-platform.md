# 插件平台：安装单元、装配顺序与诊断

本文件定义 DeepPyro 的插件平台契约中与「外部插件包」有关的部分：唯一的安装单元是什么、`plugin.toml` 怎么写、插件包如何参与组合、依赖放在哪里、失败如何分类、`dpyro plugin` 子命令做什么。组合层（bundle/patch/清单）的合并语义见 @docs/spec/plugin-core.md；管理器的启用/禁用/删除语义见 @docs/spec/plugins.md；本文件只补充「包格式 + 装配 + 诊断」，不重复定义合并算法。

本层当前覆盖 P0（安装单元、启动期激活、插件包组合层、`dpyro plugin list/info/doctor/path`、`PluginContext.plugin_dir`）。P1（uv 安装器）、P2（TUI 贡献面 screen/ui 服务）、P3（冻结打包）见 §9。

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
    api_version = 1                 # 必填，整数，不得大于宿主 API_VERSION
    provides = ["im"]               # 可选，仅用于展示

    [compat]
    dpyro = ">=0.1,<0.2"            # 逗号分隔的约束列表
    python = ">=3.12,<3.14"
    dpyro_exempt_versions = []      # 精确版本豁免（对齐 DSH 的 exact-version 豁免）

    [deps]
    vendor = "_vendor"              # 默认值
    requires = ["websockets>=15"]   # 仅用于 doctor 校验，不参与安装

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

`create_app(activate_plugins=False)` 供自管 `sys.path` 的嵌入方关闭该步骤。

## 4. 依赖与 `_vendor`

插件依赖必须落在插件包内部，不写宿主的 site-packages：

    <dir>/_vendor/cp313/     按解释器 tag 分目录（推荐）
    <dir>/_vendor/           不带 tag 的兜底目录

查找顺序 `_vendor/cp<major><minor>` → `_vendor`。加载器只把存在的目录加入 `sys.path`，不创建、不修改。

- 依赖安装由 uv 完成（P1 提供 `dpyro plugin add`）：`uv pip install --python 3.13 --target <dir>/_vendor/cp313 -r requirements.txt`。
- `[deps].requires` 不参与安装，只用于 `doctor` 校验（`dep-missing`）。
- 若 `_vendor/cpXY` 里存在与宿主 tag 不同的二进制扩展（`.pyd`/`.so`），`doctor` 报 `abi-mismatch`。
- 所有插件共享同一进程的 `sys.modules`：两个包 vendor 同名不同版本的顶层包属于未定义行为，`doctor` 的目标是把它显式化（P1 做跨包比对）。

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

所有失败都归入六类之一，每类带一句可执行的补救建议：

| 分类 | 含义 | 补救 |
| --- | --- | --- |
| `metadata-invalid` | `plugin.toml` 缺失/语法错误/字段类型错误/id 不一致/缺 `module` 或 `class`/`bundle.toml` 无法解析 | 修 `plugin.toml` |
| `import-failed` | 声明的 `module:Class` 无法 import | `doctor` 看 traceback |
| `dep-missing` | `[deps].requires` 在 vendor 目录里找不到 | `dpyro plugin add <id>`（P1）或重建 `_vendor` |
| `dep-conflict` | 两个包 vendor 了同名不同版本 | 卸载其一 |
| `abi-mismatch` | vendor 里的二进制扩展不是当前解释器 tag | 按当前 Python 重建 `_vendor` |
| `version-incompat` | `[compat].dpyro` / `[compat].python` / `api_version` 不接受当前宿主 | 升级插件或宿主，或用 `allow-version` 豁免（P1） |

## 7. `dpyro plugin`

    dpyro plugin list [--json]
    dpyro plugin info <id>
    dpyro plugin doctor [<id>] [--json]
    dpyro plugin path

- `list`：表格（id / version / api / provides / vendor tag / status）；有异常时追加一行 `unhealthy: ...`。
- `info`：输出该包的完整元数据 JSON（含 `raw`）。
- `doctor`：逐包输出 `ok` 或 `FAIL` + 每类失败的说明与补救建议；`--json` 输出机器可读结果。
- `path`：打印插件根目录。

退出码：`0` 成功；`1` 用法错误（未知 id/未知动词）；`2` 诊断失败（有包不健康）；`3` 安装失败且已回滚（P1）；`4` 需要重启才能生效（P1）。

`--json` 既可放在动词前也可放在动词后。TUI 的 `/plugins` 屏仍走 `PluginManager`；P2 会把同一份诊断接进该屏，使 CLI 与 TUI 显示一致的健康状态。

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

插件内可读 `PluginContext.plugin_dir`（`Path | None`）：包式安装时返回包目录，in-tree 插件返回 `None`。

## 9. 与 DSH 的对应、偏离与后续

对应：单一安装单元、`plugin.toml` 作为唯一权威元数据、`[compat]` 对齐 `engines.dsh`、整键覆盖的补丁层、启动审计（doctor）、「预检不过不安装」的 P1 安装器。

有意偏离：`_vendor/`（Python 没有 per-package 解析，依赖必须落到宿主进程可见目录）；不做 profile 多环境（用 `DEEPPYRO_HOME` 替代）；entry point 不是发布形态。

未实现（按里程碑）：

- P1：`add/remove/update/enable/disable/allow-version`，uv 安装后端、快照回滚、跨包依赖冲突检测。
- P2：`ui.toml` 声明与 `screen` 槽位、Textual-free `ui` 服务（动作队列）、`/plugins` 屏健康状态。
- P3：PyInstaller spec、`--internal-*` 子命令、hiddenimports、插件目录只读约束。
