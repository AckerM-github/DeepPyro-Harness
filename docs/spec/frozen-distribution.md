# 冻结发布（PyInstaller）兼容规范

> **状态**：现行（0.3.0） · **层级**：发布/打包规格 · **最后核对**：2026-11-19

面向用 PyInstaller 打包 `dpyro.exe` 的发布者与插件作者。结论先行：

- 冻结**技术可行**，但**裸 freeze 不可用**：`src/deeppyro/bundles/core/bundle.toml` 用字符串点名插件模块，静态分析看不到它们，41 个插件行 + `[bundle]` 元数据里的模块几乎都会被裁掉，连带 `httpx`/`Pillow` 等依赖一起消失（0.3.0 起 src 已不使用 `zstandard`，但 pyproject 依赖行仍在）。
- 因此冻结版的构建输入必须**由数据生成**（`packaging/hiddenimports.py`），不能手写、不能靠 PyInstaller 的静态扫描。
- 插件代码**不进 exe**：exe 只冻结宿主，插件及其依赖留在用户目录，按目录插件形态加载。

## 1. 运行时形状

| | onedir（推荐） | onefile |
|---|---|---|
| 产物 | `dist/packaging/dpyro/dpyro.exe` + `dpyro/_internal/` | `dist/packaging/dpyro.exe` |
| 数据位置 | `_internal/`（= `sys._MEIPASS`） | 每次启动解压到临时目录（= `sys._MEIPASS`） |
| 启动代价 | 低 | 每次都要解压全部 payload（含 tzdata 的 600+ 文件） |
| 适用 | 默认发布、内网分发 | 单文件分发，可接受首启延迟 |

两者都必须是**控制台程序**（`console=True`）。TUI 需要真实控制台句柄，`--internal-*` 子进程协议需要可用的 stdin/stdout/stderr 管道；`--windowed`/`--noconsole` 会让两者同时失效。

## 2. 资源定位：`deeppyro/runtime_env.py`

冻结后 `Path(__file__)` 指向 exe 内部（onefile 下甚至是临时解压目录），包内数据必须统一走这层：

| 函数 | 源码运行 | 冻结运行 |
|---|---|---|
| `is_frozen() -> bool` | `False` | `True`（`sys.frozen`） |
| `bundle_dir() -> Path` | 包目录 `src/deeppyro` | `sys._MEIPASS`；缺失时退化为 exe 所在目录 |
| `data_path(name) -> Path` | `bundle_dir() / name` | 同上 |
| `executable() -> str` | `sys.executable` | `sys.executable`（= exe 自身） |

规则：

- 读取包内数据一律 `data_path("bundles/core/bundle.toml")` 这类相对路径，**不要**再用 `Path(__file__)` 拼。
- 用户态路径（`~/.dpro`、cwd、用户 bundle、`AGENTS.md`）不受冻结影响，继续走 `deeppyro.paths` 与 `Path.home()`；不要把它接到 `_MEIPASS` 上（那是临时目录）。
- `data_path` 不检查存在性：缺文件应在使用点报错，而不是在定位时静默返回空。

当前唯一的使用点：

- `src/deeppyro/kernel/composition.py`：`core_bundle_path()` 返回 `data_path(Path("bundles") / "core" / "bundle.toml")`，是默认 bundle 的唯一来源（`src/deeppyro/kernel/bundle.py` 在 import 期就会读它，缺文件直接抛 `CompositionError: bundle not found`）。

## 3. 子进程协议：`--internal-*` 自调

冻结后没有解释器、没有磁盘上的 `.py`，所以「再起一个自己」不能再用 `python -m ...`。两条路径改走隐藏子命令：

| 用途 | 源码运行 | 冻结运行 |
|---|---|---|
| Windows ACL 沙箱 runner | `python -m deeppyro.sandbox.windows_acl_runner ...` | `dpyro.exe --internal-sandbox-runner ...` |
| PTC worker | `python -I -S -X utf8 worker.py` | `dpyro.exe --internal-ptc-worker` |

实现要点：

- `src/deeppyro/tui/main.py` 的 `main()` **第一步**就调 `_dispatch_internal(raw)`：命中即转调 `windows_acl_runner.main(raw[1:])` 或 `ptc.worker.main()`，并在 import runtime 之前返回 —— worker 只看到裸 stdio，不会 boot 插件图，也不会触发 `migrate_legacy_layout()`。
- 两个 flag 用 `nargs=argparse.REMAINDER` + `help=argparse.SUPPRESS` 注册，所以 `dpyro --headless --internal-ptc-worker ...` 这种被 argparse 先看到的情形也能经 `_parsed_internal()` 还原并转调；`--help` 不显示它们。
- 源码安装保持旧路径不变（`tests/sandbox/test_windows_acl_runner.py` 仍断言 `[sys.executable, "-m", "deeppyro.sandbox.windows_acl_runner"]`）。
- 默认权限预设 `workspace-write` 在 Windows 上必走 runner，因此**这是冻结版 shell/后台任务的生死线**：flag 一旦不通，所有命令都拿不到结果。

## 4. 构建输入（`packaging/`）

| 文件 | 作用 |
|---|---|
| `packaging/hiddenimports.py` | 从 bundle 数据生成 `hiddenimports` 与 `datas`（纯 stdlib，不 import 宿主） |
| `packaging/launcher.py` | PyInstaller 入口脚本：`sys.exit(main())` |
| `packaging/deeppyro.spec` | `DEEPPYRO_FREEZE_MODE=onedir 或 onefile` 同一份 spec |
| `packaging/build.ps1` | 唯一受支持入口：生成输入 → 构建 → 自检 |

`hiddenimports` 的来源（顺序即优先级）：

1. `src/deeppyro/bundles/core/bundle.toml` 的全部 `plugin` 行（含 `include` 与内联 `rows` 递归）；
2. 用户 bundle 目录（`$DEEPPYRO_HOME/bundles` 或 `~/.dpro/bundles`）里的 `bundle.toml` —— 让构建与出包机器一致；
3. `DYNAMIC_HIDDENIMPORTS`：`textual.drivers.windows_driver`、`textual.drivers.linux_driver`（textual 在 `App.get_driver_class()` 方法体内按平台导入）、`tzdata`（`zoneinfo` 用 `importlib.resources` 找库，不是 import 依赖）、`deeppyro.ptc.worker`、`deeppyro.sandbox.windows_acl_runner`（隐藏 flag 才导入）；
4. 仅当 `DEEPPYRO_FREEZE_PLUGINS=1` 才扫 `$DEEPPYRO_PLUGINS_DIR` / `~/.dpro/plugins` 的 `plugin.toml`（**默认关**：exe 出包后插件应继续外挂安装）。

`datas` 三件，落地位置即 `_MEIPASS` 相对路径：`bundles/core/bundle.toml` → `bundles/core`、`certifi/cacert.pem` → `certifi`、`tzdata/zoneinfo` → `tzdata/zoneinfo`；另加 `copy_metadata("deeppyro-harness")` 让 `importlib.metadata.version("deeppyro-harness")` 在冻结后仍能返回版本（否则 `dpyro --version` 打印 `0.0.0`）。

`excludes` 见 `packaging/hiddenimports.py` 的 `EXCLUDES`（numpy/pandas/tkinter/pytest/IPython/PyQt… 宿主从不使用）。

## 5. 插件契约（运行在冻结宿主里）

| 要求 | 原因 | 检查点 |
|---|---|---|
| 纯 Python | 冻结宿主只能加载源码 | 不要编译 C 扩展进插件目录 |
| 依赖放插件自己的 `_vendor/` | exe 里没有 pip，也没有宿主的依赖树 | `deeppyro.plugins.metadata` 的 `_vendor/cpXY` → `_vendor` 候选 |
| import 期零 IO | boot 阶段插件目录只读、HOME 可能被策略限制 | 模块顶层不要读写文件/连网 |
| 只依赖 `deeppyro.api` 与 `deeppyro.core.protocols` | 其他模块可能被冻结裁掉，也可能不向后兼容 | 见 `docs/spec/plugins.md` 的 API 面 |
| 用户数据走 `Path.home()` / `deeppyro.paths` | `Path.home()` 不依赖 `__file__`，冻结后仍正确 | 不要拼 `_MEIPASS` |
| 目录插件（`~/.dpro/plugins/<id>/plugin.toml`） | 冻结后 entry point 恒空，目录是唯一可靠来源 | `dpyro plugin list` 能列出 |

冻结宿主里 `importlib.metadata.entry_points(group="deeppyro.plugins")` 只会看到 exe 内 `copy_metadata` 带进来的分发，**外挂插件不可能出现在其中**；目录扫描与 `~/.dpro/plugins.toml` 清单仍照常工作，`plugins_dir` 会被插入 `sys.path` 头部，因此插件目录里的模块与 `_vendor/` 可正常 import。

## 6. 已知限制

- **entry point 插件在冻结版恒空**：这不是 bug，是发布形态的必然结果（见 `docs/spec/plugins.md` 的三种来源）。插件作者请提供目录包。
- `importlib.resources` 与 `pkgutil` 在仓库中零使用；新增代码也不要引入，包内数据一律走 `runtime_env.data_path`。
- PTC：冻结版 `run_code` 通过 `--internal-ptc-worker` 自调，worker 由 exe 自己解释执行；若发布者裁剪了 PTC 行（`critical=false`），该路径自然不生效。
- 时区：`tzdata` 以数据形式随包落地；缺失时 `deeppyro.schedule` 会报 `ScheduleInputError("invalid_time_zone", ...)`（schedule 行本身非 critical）。
- onefile：`_MEIPASS` 每次启动都是新临时目录，不要在其中写状态；日志/会话必须落在 `~/.dpro`。

## 7. 构建与自检

```powershell
# 先看构建输入（离线可用，不装 PyInstaller 也行）
.\packaging\build.ps1 -DryRun

# onedir（推荐）
.\packaging\build.ps1 -Clean

# onefile
.\packaging\build.ps1 -Mode onefile -Clean

# 把当前机器上已安装的插件也冻进 exe（不推荐，仅特殊分发）
.\packaging\build.ps1 -IncludePlugins
```

`build.ps1` 依次做：打印生成的输入 → 检查 PyInstaller 是否可用（缺失时只在 `-DryRun` 下成功退出）→ `python -m PyInstaller --noconfirm --distpath dist\packaging --workpath build\packaging packaging\deeppyro.spec` → 校验 onedir 的 `_internal` 里三件数据 → 自检。

自检项（任一失败即非零退出）：

1. `dpyro.exe --version` 输出 `deeppyro-harness <version>`；
2. `dpyro.exe plugin list` 退出码 0；
3. `dpyro.exe --internal-sandbox-runner --bogus` 退出码 **127**（证明沙箱自调穿过了冻结边界；若被 TUI 吞掉会得到别的码）；
4. 一次最小 headless 回合（需要已配置 provider，默认只告警，`-StrictSelfTest` 才致命）。

## 8. 验收清单

- `packaging/hiddenimports.py` 的 `missing_core_modules()` 为空，`core_bundle_modules()` 覆盖 `bundle.toml` 的每一行（`tests/architecture/test_frozen_hiddenimports.py` 守卫）。
- 新增任何包内数据文件时，同时改 `data_files()` 并让读取点走 `data_path()`。
- 任何「再起一个自己」的新代码，必须走 `--internal-*` 协议并在 `tui/main.py` 分流，不许依赖 `sys.argv[0]`。
- 任何新的动态导入（`importlib.import_module`、字符串 target、方法体内导入）都要进 `DYNAMIC_HIDDENIMPORTS`。
- 构建产物必须是控制台程序；`--windowed` 一律视为回归。

## 9. 变更点索引

| 文件 | 改动 |
|---|---|
| `src/deeppyro/runtime_env.py` | 新增：`is_frozen`/`bundle_dir`/`data_path`/`executable` 与两个内部 flag 常量 |
| `src/deeppyro/kernel/composition.py` | `core_bundle_path()` 走 `data_path()` |
| `src/deeppyro/sandbox/windows_acl_runner.py` | `self_command()` 冻结时返回内部 flag |
| `src/deeppyro/ptc/runtime.py` | `uses_internal_worker()`/`worker_argv()`，冻结时自调 |
| `src/deeppyro/tui/main.py` | `_dispatch_internal()`/`_parsed_internal()` 与两个隐藏参数 |
| `packaging/` | spec、launcher、构建输入生成器、构建脚本 |
| `tests/test_frozen_paths.py` | 伪造 `sys.frozen`/`sys._MEIPASS` 的冻结分支测试 |
| `tests/architecture/test_frozen_hiddenimports.py` | 生成器覆盖 core bundle 全部行的架构门 |
