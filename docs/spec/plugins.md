# 外部插件管理规格

本文件记录 @src/deeppyro/plugins 与 @src/deeppyro/tui/screens/plugins.py 的语义：只管理「我们源码之外」的插件，负责发现、启用/禁用、校验与删除，不 boot kernel，也不安装 Python 包。插件清单的 TOML 格式与合并语义完全复用 @docs/spec/INTERFACES.md 第 11 节（@src/deeppyro/kernel/manifest.py），本文件不重复定义清单格式。

## 1. 文件布局

@src/deeppyro/plugins/：
- @errors.py：PluginError（继承 DeeppyroError）及 PluginNotFoundError、PluginProtectionError、PluginRemovalError、ManifestWriteError。
- @model.py：PluginSource / SOURCE_ENTRY_POINT / SOURCE_DIR / SOURCE_MANIFEST / SOURCE_LABELS、PluginInfo、ValidationResult。
- @manifest_store.py：read_manifest（转调 kernel.manifest.load_manifest）、dump_manifest（手写最小 TOML 序列化）、save_user_manifest（临时文件 + os.replace 原子写）。
- @discovery.py：ENTRY_POINT_GROUP、PluginDiscovery，扫描三类来源并合并成行。
- @manager.py：PluginManager，list/refresh/validate/enable/disable/remove。
- @__init__.py：公开 API 重导出。

@src/deeppyro/tui/screens/plugins.py：PluginScreen（ModalScreen[str | None]）。@src/deeppyro/tui/screens/__init__.py 不导出它，命令注册方直接从模块导入。

## 2. 三类外部来源

(a) Python entry point：组名 deeppyro.plugins。用 importlib.metadata.entry_points() 枚举，优先调用其 .select(group=...)，兼容旧 dict 形式与可迭代形式。行 id 为 entry point 名字；value 必须是 module:Class 字符串（也允许裸 module，但只有 load() 后才能解析出类）；version 取 distribution.version，origin 形如 dist-name==version，description 取 metadata 的 Summary。value 为空时该行带 error，不抛异常。

(b) 用户插件目录：默认为 ~/.dpro/plugins/（deeppyro.paths.plugins_dir()；DEEPPYRO_PLUGINS_DIR 可覆盖；显式传入 manifest 时默认用 manifest 同级的 plugins/）。每个插件一个子目录 <name>/，必须含 plugin.toml：

    name = "Demo Plugin"
    version = "0.1.0"
    description = "a demo plugin"
    module = "demo_plugin"
    class = "DemoPlugin"
    provides = ["demo"]

name/version/description/class 必须是字符串；module 必须是非空字符串；解析目标为 module:Class，若 class 为空则 module 本身必须已含冒号。id 恒为目录名（额外顶层键忽略）。provides 是可选扩展键，仅用于展示。没有 plugin.toml 的子目录直接忽略；TOML 语法错误、字段类型错误或无法解析目标时生成带 error 的行而不是抛异常。扫描时默认把插件目录插入 sys.path 头部（inject_sys_path=True），以便 validate 能 import 该目录里的模块。

(c) 用户清单：~/.dpro/plugins.toml 的 [[plugins]] 条目（deeppyro.paths.plugins_manifest_path()；DEEPPYRO_PLUGINS_MANIFEST 可覆盖）。完全复用 kernel.manifest 的 load_manifest / PluginSpec / enabled() 语义；只有当一个 id 没有在 (a)(b) 中出现时，清单条目本身才会作为 source=manifest 的行展示（name 即 module:Class）。清单是启用状态的唯一存储层：对 (a)(b) 的插件写 disabled 标志，插入新条目时用 id + name=module:Class。

发现顺序与优先级：目录 → entry point → 清单；同一 id 先出现者胜出并记录 warning（目录 > entry point > 清单）。任何来源使用 bundle 里的 id（llm/sessions/sandbox/tools/agents，动态取自 kernel.bundle.DEFAULT_PLUGINS）都会被忽略并记录 warning，管理器永远不会改动默认 bundle 插件。清单读取失败（ManifestError/OSError）只记入 errors，其余来源继续列出。

启用状态：某行 enabled=True 当且仅当用户清单里有该 id 的条目且 disabled is not True（即 kernel PluginSpec.enabled()）。只出现在目录或 entry point、清单里没有条目的插件视为未启用（不会进入 kernel boot）。bundle 插件不参与此判断。

## 3. 数据模型

PluginInfo（frozen dataclass，slots）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | 外部插件的稳定标识（目录名 / entry point 名 / 清单 id） |
| name | str | 展示名（plugin.toml 的 name；entry point 用 id） |
| version | str | 版本，未知为空串 |
| source | entry-point / dir / manifest | 来源 |
| origin | str | 来源位置：plugin.toml 路径 / dist==version / 清单路径 |
| description | str | 说明，未知为空串 |
| enabled | bool | 清单 disabled 语义下的启用状态 |
| error | str / None | 该行内部结构错误（未安装/不可读/格式错），与校验错误无关 |
| provides | tuple[str, ...] | 目录 provides 键或 validate 得到的服务名 |
| target | str / None | module:Class；entry point 为裸 module 时由 validate 填充 |

ValidationResult：id、ok、target、error、provides。validate 永不抛异常（未知 id 除外），失败信息放在 ok=False + error。

## 4. PluginManager API

    PluginManager(
        *,
        manifest_path: str | Path | None = None,
        plugins_dir: str | Path | None = None,
        entry_points: Callable[[], Any] | None = None,
        bundle_ids: Iterable[str] | None = None,
        inject_sys_path: bool = True,
    )

manifest_path 缺省为 kernel.bundle.DEFAULT_MANIFEST_PATH，plugins_dir 缺省为其父目录下的 plugins/，bundle_ids 缺省取 DEFAULT_PLUGINS 的 id，entry_points 缺省动态调用 importlib.metadata.entry_points()。构造时立即 refresh()。测试通过显式 path 与假 entry_points 工厂完全隔离真实用户目录。

只读属性：manifest_path、plugins_dir、bundle_ids、errors、warnings。list() 返回当前行副本；get(id) 返回单行或 None。

refresh()：重新扫描三类来源，返回新行列表；不写盘。

validate(id)：不 boot kernel 的导入校验。先解析 target（entry point 裸 module 则调用 entry.load() 后取类的 module:qualname，含 <locals> 的类拒绝），然后 importlib.import_module + kernel.manifest.resolve_plugin_name，最后从插件类的 provide 类属性（str 或 list/tuple）提取 provides。成功时把 target/provides 写回缓存行并清空 error；失败返回 ok=False 与原始异常文本。未知 id 抛 PluginNotFoundError。

enable(id)：
- 先拒绝 bundle id（PluginProtectionError）。
- 已启用则幂等返回当前行。
- 先 validate，失败抛 PluginError（不写清单）。
- 严格读取清单（ManifestError/OSError → PluginError），绝不覆盖不可读文件。
- 若已有该 id 条目：清 disabled（None），保留原 name/config/before/after；否则追加 PluginSpec(id=id, name=target)。
- 原子写回后 refresh，返回新行。

disable(id)：bundle 保护与已禁用幂等同上；读取清单后把 disabled 置 True（保留 name/config/before/after）；清单里没有条目但能解析 target 时追加 disabled=true 条目；随后 refresh。

remove(id)：bundle 保护。entry point 来源抛 PluginRemovalError（提示用 pip uninstall，本工具不做 pip 操作）。目录来源先确认待删目录 resolve 后严格位于 plugins_dir 之内（root 本身与外部路径拒绝），先删除清单条目再 shutil.rmtree；删除失败抛 PluginRemovalError。manifest 来源删除清单条目（必须存在，否则 PluginRemovalError）。最后 refresh。

清单写盘（@manifest_store.py）：dump_manifest 只输出 [[plugins]] 表，字段按 id、name、disabled、config、before、after 顺序，无值字段省略；config 以内联表输出；字符串用 JSON 转义（TOML basic string 子集）；不支持的值类型抛 ManifestWriteError。save_user_manifest 在目标同目录创建命名临时文件，写完 flush + os.fsync 后 os.replace；内容为空时删除清单文件；失败清理临时文件并抛 ManifestWriteError。读取统一走 kernel load_manifest，因此 merge/别名/错误文本与 kernel 一致。

错误类型：PluginError 及其子类均为 DeeppyroError 子类，调用方可按 PluginError 统一捕获。

## 5. PluginScreen

    PluginScreen(manager: PluginManager | None = None, *, name=None, id=None, classes=None)

manager 省略时延迟构造真实的 PluginManager（读真实用户目录）；测试传入假 manager。屏幕自身不含发现逻辑，只调用 manager。

布局：Vertical#plugin-dialog，内含标题、DataTable#plugin-table、详情 Static#plugin-detail、按键提示；表格列固定为 名称/版本/来源/状态/说明。状态列：行有 error 时显示「错误」，否则 enabled 显示「启用」，否则「禁用」。说明列优先显示 error，其次 description，最后 -。空列表时详情区提示三类来源。

按键（Binding，全部 show=False）：

| 键 | action | 行为 |
| --- | --- | --- |
| e | enable | 对当前行调用 manager.enable；失败 notify severity=error，不崩溃 |
| d | disable | 对当前行调用 manager.disable；失败同样 notify |
| r | refresh | manager.refresh 重载表格；manager.errors 非空时 warning notify |
| i | details | manager.validate，详情区显示标识/名称/版本/来源/状态/说明/提供/目标/校验结果 |
| Esc | close | dismiss(None) |

无选中行时 e/d/i 只发 warning 通知。所有 manager 异常在屏幕内捕获并转 App.notify，异常不会穿透事件循环。enable/disable 后保留原选中 id 并刷新详情。

样式限制：Screen 的 CSS 是独立样式表，无法引用 app CSS 里新增的变量（例如 theme.tcss 的 $bg/$yellow 会触发 UnresolvedVariableError），因此 PluginScreen 的 CSS 只使用标准 Textual 主题变量（$accent/$surface/$text-muted），这些名字会继承 app 对同名标准变量的覆盖；主题中缺少的自定义 token（$bg、$green、$yellow、$red、$cyan、半透明背景）不会生效。若需与其他屏幕完全一致的配色，由 kernel/TUI 主题拥有者在 theme.tcss 增加 PluginScreen 块与自定义变量，本模块不改 theme.tcss。

命令接线：@src/deeppyro/tui/commands.py 目前 /plugins 指向 app.show_plugins()（列出已加载 kernel 插件）。管理界面的接线方（Lead）应改为 push PluginScreen（例如 await app.push_screen(PluginScreen())，或 push_screen_wait 后忽略 None 结果）；本模块不修改 app.py/commands.py。

## 6. 测试

命令：@uv run pytest tests/plugins tests/kernel -q；ruff：@uv run ruff check src/deeppyro/plugins src/deeppyro/tui/screens/plugins.py tests/plugins。

当前结果：tests/plugins 62 passed + tests/kernel 18 passed（tests/plugins 覆盖 @tests/plugins/test_manifest_store.py 11、@tests/plugins/test_discovery.py 19、@tests/plugins/test_manager.py 19、@tests/plugins/test_screen.py 13），ruff 无告警。tui 相关用例用 App.run_test 注入假 manager，不触碰真实用户目录；所有目录/entry point 用例在 tmp_path 构造，conftest 在用例结束后恢复 sys.path 并清理新导入模块。

覆盖要点：三类来源字段与优先级、坏 TOML/坏 value 的行级错误、bundle id 过滤、清单 enabled 语义、原子写 round-trip（含 unicode/转义/嵌套 config/空清单删文件/无 .tmp 残留）、enable/disable 幂等与配置保留、读写失败不覆盖清单、bundle 保护、未知 id、目录删除边界、entry point 不卸载、validate 成功/失败、屏幕渲染/按键/通知/关闭/空列表/默认 manager 注入。

## 7. 与 DSH 的差异与已知风险

- 目录插件可导入性：plugin.toml 指向的模块位于插件目录，正常 kernel boot 进程不会自动把该目录加入 sys.path。PluginManager 在扫描时把目录插入当前进程 sys.path，因此 validate/enable 可用；但新进程启动 dpyro 并直接 boot kernel 时，kernel 只能按 module:Class 解析目标，需要 PYTHONPATH 或先经管理器注册路径。entry point 插件属于已安装发行版，无此问题。详见 @docs/spec/tui.md 的启动流程。
- 清单重写会丢失注释、未知顶层键和原始排版，只保留 [[plugins]] 的语义内容（id/name/config/disabled/before/after）。这是最小 TOML 序列化的刻意取舍，避免引入 TOML 写库依赖。
- 不提供安装、升级、pip uninstall 或版本选择；entry point 来源只能由 pip 管理，remove 明确拒绝。
- remove 目录时若 kernel 正在运行，已加载的插件实例不会停止（管理器不 boot/stop kernel）；需要重启进程完成卸载。
- 清单写入没有跨进程锁，假定单 TUI 进程独占用户清单。
- 用户清单读取失败只降级为 errors 展示；此时 enable/disable 会拒绝写盘，避免覆盖原文件。
- 裸 module 的 entry point 必须 load() 后才能得到 module:Class；该步骤在管理器进程内执行，等价于信任已安装插件代码（与 kernel 启动一致）。
