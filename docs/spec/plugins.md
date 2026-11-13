# 插件管理与自扩展规格

本文件记录 DeepPyro 的插件管理能力：外部插件的发现、启用/禁用、校验与删除，用户 bundle 的安装/卸载，插件设置的读写，运行中插件宿主的检视，以及供 agent 调用的插件管理工具。插件清单与 bundle 的 TOML 格式和合并语义复用 @docs/spec/plugin-core.md 与 @docs/spec/INTERFACES.md 第 11 节，本文件不重复定义；设置的详细语义见 @docs/spec/plugin-settings.md。

所有能力都是普通插件：PluginManagerPlugin、SettingsPlugin、InspectPlugin 是 Service 插件，PluginToolsPlugin 是注册工具的普通 Plugin。库代码（@src/deeppyro/plugins/）不依赖 TUI，也不 boot kernel。

## 1. 文件布局

@src/deeppyro/plugins/：
- @errors.py：PluginError（继承 DeeppyroError）及 PluginNotFoundError、PluginProtectionError、PluginRemovalError、ManifestWriteError、SettingsConflict。
- @model.py：PluginSource / SOURCE_ENTRY_POINT / SOURCE_DIR / SOURCE_MANIFEST / SOURCE_LABELS、BUNDLE_STATE_* / BUNDLE_STATES、PluginInfo、ValidationResult、BundleInfo。
- @manifest_store.py：read_manifest（转调 kernel.manifest.load_manifest）、dump_manifest（手写最小 TOML 序列化）、save_user_manifest（临时文件 + fsync + os.replace 原子写）。
- @discovery.py：ENTRY_POINT_GROUP、PluginDiscovery，扫描三类外部来源并合并成行。
- @bundles.py：BundleStore，管理 ~/.dpro/bundles 下的用户 bundle。
- @settings_store.py：SettingsStore 及 mask_secrets、manifest_revision、plugin_config_schema、validate_config 等辅助函数。
- @manager.py：PluginManager，外部插件管理与 bundle 门面。
- @__init__.py：公开 API 重导出。

新增的内置插件（本层）：
- @src/deeppyro/builtin/plugin_manager.py：PluginManagerPlugin，Service，provide = plugin_manager，plugin_id = plugin-manager，包装 PluginManager。
- @src/deeppyro/builtin/settings.py：SettingsPlugin，Service，provide = settings，plugin_id = settings，包装 SettingsStore。
- @src/deeppyro/builtin/inspect.py：InspectPlugin，Service，provide = inspect，plugin_id = inspect，只读检视运行中的插件宿主。
- @src/deeppyro/tools/builtin/plugin_tools.py：四个 BaseTool（plugin_manager、plugin_inspect、settings_read、settings_write）、工厂 plugin_tools() 与注册用的 PluginToolsPlugin（plugin_id = plugin-tools）。

TUI：@src/deeppyro/tui/screens/plugins.py 提供 PluginScreen（ModalScreen[str | None]）。@src/deeppyro/tui/screens/__init__.py 不导出它，命令注册方直接从模块导入。

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

发现顺序与优先级：目录 → entry point → 清单；同一 id 先出现者胜出并记录 warning（目录 > entry point > 清单）。任何来源使用 bundle 里的 id（llm/sessions/sandbox/tools/tools-builtin/presets/prompt/prompt-core/commands/commands-builtin/agents 等，动态取自 kernel.bundle.DEFAULT_PLUGINS）都会被忽略并记录 warning，管理器永远不会改动默认 bundle 插件。清单读取失败（ManifestError/OSError）只记入 errors，其余来源继续列出。

启用状态：某外部行 enabled=True 当且仅当用户清单里有该 id 的条目且 disabled is not True（即 kernel PluginSpec.enabled()）。只出现在目录或 entry point、清单里没有条目的插件视为未启用（不会进入 kernel boot）。bundle 插件不参与此判断。

## 3. 用户 bundle 生命周期

用户 bundle 安装到 ~/.dpro/bundles/<bundle_id>/，必须含 bundle.toml（格式与校验见 @docs/spec/plugin-core.md）。kernel 默认合成顺序为：core bundle → 显式 bundle_paths → 自动发现的用户 bundle（按目录名排序）→ extra_bundle_paths → 用户清单；全部 bundle 收集后按 (order, id) 排序再依次 apply。因此用户 bundle 的 order 决定其覆盖层级，而不是安装顺序。

BundleStore 构造：

    BundleStore(*, manifest_path=None, root=None, bundle_ids=None)

manifest_path 缺省为 ~/.dpro/plugins.toml；root 缺省为 kernel.composition.user_bundles_dir()；bundle_ids 缺省取 kernel.bundle.DEFAULT_PLUGINS 的 id（受保护的 core 行）。

方法：

| 方法 | 行为 |
| --- | --- |
| list() | 返回 tuple[BundleInfo, ...]，包含无法解析的 bundle（state=failed，error 带 CompositionError 文本） |
| install(source, *, force=False) | 校验 bundle.toml 后复制整棵目录到 root/<id>/，返回 state=loaded、restart_required=True 的信息 |
| set_enabled(bundle_id, enabled) | 在用户清单里为 bundle 的所有 row id 写 disabled 补丁，返回新信息；restart_required 表示清单是否实际变化 |
| remove(bundle_id) | 删除已安装目录，并先清掉它独占的清单行，返回 state=removed |

install 细节：source 可以是 bundle 目录或直接指向 bundle.toml。先用 kernel.composition.load_bundle 校验（api_version <= API_VERSION、order 为 int、plugins 为数组、每行有 plugin target、拒绝未知键）。bundle id 来自 [bundle].id，必须是单个路径组件：strip 后非空，不能是 . 或 ..，不能含 / 、反斜杠、冒号或 NUL；否则 PluginError。目标已存在且 force=False 时抛 PluginError；force=True 先删除旧目录。复制在 root 下的临时目录完成（tempfile.mkdtemp + shutil.copytree），随后 os.replace 原子替换，失败清理临时目录。复制成功后再 load_bundle 一次确认目标可解析。

set_enabled 细节：bundle 的每个 row id 在用户清单里写 disabled 补丁。禁用时：已有条目则替换为 disabled=True（保留 name/config/before/after），没有条目则追加只有 id 与 disabled=True 的条目。启用时：已有条目则清掉 disabled，若该条目除 id 与 disabled 外没有 name/config/before/after 则整条删除（恢复 bundle 默认），没有条目则无需写。若 bundle 的任何 row id 属于 core bundle 的 id，抛 PluginProtectionError（不允许禁用 core 行）。写盘走 save_user_manifest，restart_required 仅在清单内容变化时为 True。

remove 细节：先读取 bundle 得到 row ids；对每个 id 判断是否被其它已安装 bundle 也提供，只删除「不属于 core、也不被其它 bundle 提供」的清单行；必须先写清单再删目录，否则下次 boot 会遇到未知 id 的补丁行而报错。随后 shutil.rmtree 删除 root/<id>（失败抛 PluginRemovalError）。解析失败的 bundle 目录也可以按目录名删除。remove 不删除插件目录、不做 pip uninstall、也不会停止已加载的插件：需要重启进程。

覆盖与状态：多个 bundle 提供同一 row id 时，composition 之后 apply 者胜出。BundleStore 按 (order, bundle.id) 判定 winner（完全并列时按目录名），非 winner 的冲突 row 记入 BundleInfo.overridden_rows。state 取值优先级：failed（bundle.toml 无法解析）> disabled（bundle 所有 row 都被清单禁用）> overridden（bundle 有 row 被覆盖）> loaded。enabled 为 True 表示 bundle 至少有一行且并非所有行都被禁用。

错误：BundleStore 的失败都抛 PluginError 的子类；不会静默改写清单。

## 4. 数据模型

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

BundleInfo（frozen dataclass，slots）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | str | bundle id；failed 时取目录名 |
| name / version | str | bundle.toml 的元数据，failed 时为空串 |
| api_version / order | int | bundle.toml 元数据，failed 时为 0 |
| critical | bool | bundle.toml 的 critical |
| path | str | 实际 bundle.toml 路径（字符串） |
| enabled | bool | 至少一行且并非全部被清单禁用 |
| state | str | loaded / failed / disabled / overridden / removed 之一 |
| error | str | failed 时的解析错误文本，否则空串 |
| row_ids | tuple[str, ...] | bundle 内的插件 row id，按文件顺序 |
| disabled_rows | tuple[str, ...] | 被用户清单禁用的 row id |
| overridden_rows | tuple[str, ...] | 被其它 bundle 覆盖的 row id |
| restart_required | bool | list() 恒为 False；install/set_enabled/remove 的结果表示是否必须重启生效 |

状态常量：BUNDLE_STATE_LOADED、BUNDLE_STATE_FAILED、BUNDLE_STATE_DISABLED、BUNDLE_STATE_OVERRIDDEN、BUNDLE_STATE_REMOVED；BUNDLE_STATES 为按展示顺序排列的 tuple。

## 5. PluginManager API

    PluginManager(
        *,
        manifest_path: str | Path | None = None,
        plugins_dir: str | Path | None = None,
        entry_points: Callable[[], Any] | None = None,
        bundle_ids: Iterable[str] | None = None,
        inject_sys_path: bool = True,
        bundles_root: str | Path | None = None,
    )

manifest_path 缺省为 kernel.bundle.DEFAULT_MANIFEST_PATH，plugins_dir 缺省为其父目录下的 plugins/，bundle_ids 缺省取 DEFAULT_PLUGINS 的 id，entry_points 缺省动态调用 importlib.metadata.entry_points()，bundles_root 缺省为 ~/.dpro/bundles。构造时立即 refresh()。测试通过显式 path 与假 entry_points 工厂完全隔离真实用户目录。

只读属性：manifest_path、plugins_dir、bundle_ids、bundles_root、errors、warnings。list() 返回当前行副本；get(id) 返回单行或 None。

refresh()：重新扫描三类外部来源与 bundle 状态，返回新行列表；不写盘。

validate(id)：不 boot kernel 的导入校验。先解析 target（entry point 裸 module 则调用 entry.load() 后取类的 module:qualname，含 <locals> 的类拒绝），然后 importlib.import_module + kernel.manifest.resolve_plugin_name，最后从插件类的 provide 类属性（str 或 list/tuple）提取 provides。成功时把 target/provides 写回缓存行并清空 error；失败返回 ok=False 与原始异常文本。未知 id 抛 PluginNotFoundError。

enable(id)：先拒绝 bundle id（PluginProtectionError）；已启用则幂等返回；先 validate，失败抛 PluginError（不写清单）；严格读取清单（ManifestError/OSError → PluginError），不覆盖不可读文件；已有该 id 条目则清 disabled 并保留 name/config/before/after，否则追加 PluginSpec(id=id, name=target)；原子写回后 refresh。

disable(id)：bundle 保护与已禁用幂等同上；读取清单后把 disabled 置 True（保留其余字段）；清单里没有条目但能解析 target 时追加 disabled=true 条目；随后 refresh。

remove(id)：bundle 保护。entry point 来源抛 PluginRemovalError（提示用 pip uninstall，本工具不做 pip 操作）。目录来源先确认待删目录 resolve 后严格位于 plugins_dir 之内（root 本身与外部路径拒绝），先删除清单条目再 shutil.rmtree；删除失败抛 PluginRemovalError。manifest 来源删除清单条目（必须存在，否则 PluginRemovalError）。最后 refresh。

bundle 门面（委托 BundleStore）：list_bundles()、install_bundle(source, *, force=False)、remove_bundle(bundle_id)、set_bundle(bundle_id, enabled)。

清单写盘（@manifest_store.py）：dump_manifest 只输出 [[plugins]] 表，字段按 id、name、disabled、config、before、after 顺序，无值字段省略；config 以内联表输出；字符串用 JSON 转义（TOML basic string 子集）；不支持的值类型抛 ManifestWriteError。save_user_manifest 在目标同目录创建命名临时文件，写完 flush + os.fsync 后 os.replace；内容为空时删除清单文件；失败清理临时文件并抛 ManifestWriteError。读取统一走 kernel load_manifest，因此 merge/别名/错误文本与 kernel 一致。

错误类型：PluginError 及其子类均为 DeeppyroError 子类，调用方可按 PluginError 统一捕获。

## 6. 设置服务：SettingsStore 与 SettingsPlugin

SettingsStore 负责插件配置的描述、读取与带 CAS 的写入；SettingsPlugin 把它暴露为 cordis 服务（provide = settings），供 agent 工具和其它插件使用。详细规则见 @docs/spec/plugin-settings.md，这里给出要点。

    SettingsStore(*, manifest_path=None, plugins_dir=None, bundle_paths=())

属性：manifest_path、plugins_dir、revision。revision = sha256(用户清单原始字节).hexdigest() 的前 16 个十六进制字符；清单不存在时按空字节计算，因此首次写入前后的 revision 都能比较。

describe(plugin_id)：返回 dict，字段为 plugin_id、target、origin、config_source（bundle / manifest / default / none）、critical、disabled、schema（插件类 Config 的 model_json_schema，无 pydantic Config 时为 None）、values（掩码后的生效配置）、revision、apply（恒为 next-boot）、volatile_fields（生效配置的顶层键排序，提示需要重启）、error（解析插件类失败时的文本）。

read(plugin_id)：返回 plugin_id、target、schema、values、revision、apply、volatile_fields、error。

write(plugin_id, patch, expected_revision)：
1. patch 必须是 Mapping；expected_revision 与当前 revision 不一致时抛 SettingsConflict（stale）。
2. 用 default composition（core bundle + 用户 bundle + 用户清单）定位该插件的 PluginRow，取其生效 config 作为当前值；找不到时再经 PluginManager 按外部来源解析 target。
3. 对 patch 做递归深合并（嵌套 dict 逐层合并，其它值整体替换）。patch 里值为字符串 *** 的 secret 字段会被丢弃，因此调用方可以原样回传 read 的结果而不会覆盖真实 secret。
4. 若插件类声明了 pydantic Config，用 model_validate 校验合并后的完整配置；失败抛 PluginConfigError，只包含 loc 与 msg，不回显输入值。
5. 读取用户清单，找到该 id 的最后一条 [[plugins]]：把 config 替换为合并后的完整配置；没有条目则追加 PluginSpec(id=plugin_id, name=target, config=合并结果)。写全量配置是必要的，因为同 id 的 manifest patch 对 config 是整体替换而不是深合并。
6. 原子写回，返回 plugin_id、revision（新值）、values（掩码）、changed（顶层变化键排序）、restart_required=True、apply=next-boot。

掩码规则：字段名（大小写不敏感）包含 key、token、secret、password 中任意一个子串即视为 secret，递归处理嵌套 dict/list，非 None 值替换为 ***。注意这是启发式：maxTokens 因包含 token 会被掩码（展示为 ***，写回时保持原值），而叫 credential 的字段不会被掩码。

SettingsPlugin：provide = settings，plugin_id = settings，version = 0.1.0，inject = []。构造时创建 SettingsStore()；属性 revision；方法 describe/read/write 直接委托。

## 7. 检视服务：InspectPlugin

InspectPlugin（provide = inspect，plugin_id = inspect，version = 0.1.0，inject = []）只读访问当前进程的 cordis 宿主，不 import kernel.app。所有方法在依赖服务缺失时抛 PluginDependencyError。

| 方法 | 返回 |
| --- | --- |
| services() | 按 name 排序的 [{name, state, provider}]；state 为 active / unavailable（Impl.check 返回 False 或抛异常）/ pending（注册服务的 fiber 未激活）；provider 为服务实例的类的 module.qualname |
| plugins() | composition 行与运行时 registry 合并后的 [{id, plugin, state, critical, origin, error}]；disabled 行 state=disabled；未挂载 state=unmounted；fiber 状态失败时 state=failed 且 error 汇总失败文本；composition 中不存在的 runtime 行以 plugin 空串、origin 空串追加 |
| tools(workspace=None) | [{name, plugin_id, scope, parallel_safe}]；逐个调用 ToolContribution.factory 实例化工具，返回多工具时展开；实例化失败的行带 error 而不是抛异常 |
| prompt_sections() | [{name, order, plugin_id, scope}]，已按 (order, name) 排序 |
| commands() | [{name, plugin_id, summary, aliases}] |
| providers() | [{name}]；合并 llm.provider_names() 与 config.value.providers 的键，避免只列已实例化 provider |
| config_schema(plugin_id) | 委托 SettingsStore.describe，返回 plugin_id、target、schema、revision、apply |

## 8. Agent 面向工具

@src/deeppyro/tools/builtin/plugin_tools.py 导出四个 BaseTool（parallel_safe 均为 False，因为安装/改配置会改磁盘），以及工厂 plugin_tools() 和 PluginToolsPlugin。

| 工具名 | 参数 | 行为 |
| --- | --- | --- |
| plugin_manager | action = list / install / set / remove；source、id、enabled、force | list 返回 bundles 与外部插件列表；install 需要 source；set 需要 id 且 enabled 必须是 bool；remove 需要 id；未知 action 或参数类型错误返回 is_error |
| plugin_inspect | what = services / plugins / tools / prompts / commands / providers；workspace 可选 | 分派到 InspectPlugin 对应方法，结果 JSON 序列化（dataclass 自动转 dict）；未知 what 返回 is_error |
| settings_read | plugin_id | 调用 settings.read，返回掩码后的 JSON |
| settings_write | plugin_id、patch（object）、expected_revision | 校验 patch 是 dict、revision 是 str 后调用 settings.write；CAS 冲突与校验错误以 is_error 文本返回 |

服务解析：工具只从 ToolContext.services 里按键取 plugin_manager、inspect、settings 三个服务；缺失时返回固定的 is_error 文本（... service is not available in this session; plugin tools are disabled），不 import 全局单例。工具内部异常统一转成 ToolResult(is_error=True) 并带前缀，不向 agent 抛出 traceback。

安全提示：plugin_manager 的描述明确写出 install 会把第三方 bundle 复制到用户目录并在下一次 boot 执行其代码，因此该调用需要人工批准；工具不提供任何签名校验或沙箱。

PluginToolsPlugin：普通 Plugin（不是 Service），plugin_id = plugin-tools，version = 0.1.0，inject = [tools]；setup 调用 ctx.register_tool(lambda _cfg, _workspace: plugin_tools()) 注册四个工具。它不修改 builtin_tools()，是否暴露给会话由 bundle 行与 runtime 接线决定。

## 9. 内置 bundle 行与集成点

Layer D 的四个内置插件需要作为 core bundle 的 [[plugins]] 行。截至本层实现完成时 @src/deeppyro/bundles/core/bundle.toml 已包含这些行（由 Lead 维护），目标路径为：

| id | plugin | 依赖 |
| --- | --- | --- |
| plugin-manager | deeppyro.builtin.plugin_manager:PluginManagerPlugin | 无（inject 为空） |
| settings | deeppyro.builtin.settings:SettingsPlugin | 无 |
| inspect | deeppyro.builtin.inspect:InspectPlugin | 无（方法调用时才取其它服务） |
| plugin-tools | deeppyro.tools.builtin.plugin_tools:PluginToolsPlugin | tools（必须排在 tools 行之后） |

集成点（由 Lead 接线）：
- TUI runtime 需要把 plugin_manager、settings、inspect 三个服务实例放进 AgentSession 的 services dict（services 的键就是工具查找用的名字），agents.create 已支持 services 参数。
- 管理界面：@src/deeppyro/tui/commands.py 的 /plugins 命令由 Lead 改为 push PluginScreen（例如 await app.push_screen(PluginScreen()) 或 push_screen_wait 后忽略 None 结果），本层不修改 app.py/commands.py。
- 目录来源插件的可导入性：PluginManagerPlugin 构造时创建 PluginManager 并 refresh，会把插件目录插入 sys.path。若希望 boot 期间直接导入目录插件，plugin-manager 行应排在依赖它的外部行之前；外部 bundle 的 order 应大于 core 的 0。

## 10. PluginScreen

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

样式限制：Screen 的 CSS 是独立样式表，无法引用 app CSS 里新增的变量（例如 theme.tcss 的 $bg/$yellow 会触发 UnresolvedVariableError），因此 PluginScreen 的 CSS 只使用标准 Textual 主题变量（$accent/$surface/$text-muted），这些名字会继承 app 对同名标准变量的覆盖；主题中缺少的自定义 token（$bg、$green、$yellow、$red、$cyan、半透明背景）不会生效。若需与其他屏幕完全一致的配色，由主题拥有者在 theme.tcss 增加 PluginScreen 块与自定义变量，本模块不改 theme.tcss。

## 11. 测试

命令：@uv run pytest tests/plugins tests/tools tests/builtin tests/kernel -o addopts= -q；全量 tui 测试 @uv run pytest tests/tui -o addopts= -q。ruff：@uv run ruff check src/deeppyro/plugins src/deeppyro/builtin/plugin_manager.py src/deeppyro/builtin/settings.py src/deeppyro/builtin/inspect.py src/deeppyro/tools/builtin/plugin_tools.py tests/plugins tests/builtin tests/tools/test_plugin_tools.py。

当前结果：tests/plugins + tests/tools + tests/builtin + tests/kernel 共 253 passed（其中 tests/plugins 99、tests/builtin 12、tests/tools 的 plugin tools 25、tests/kernel 18）；tests/tui 156 passed；scoped ruff 无告警。

覆盖要点：三类来源字段与优先级、坏 TOML/坏 value 的行级错误、bundle id 过滤、清单 enabled 语义、原子写 round-trip（含 unicode/转义/嵌套 config/空清单删文件/无 .tmp 残留）、enable/disable 幂等与配置保留、读写失败不覆盖清单、bundle 保护、未知 id、目录删除边界、entry point 不卸载、validate 成功/失败、屏幕渲染/按键/通知/关闭/空列表/默认 manager 注入；bundle 安装/覆盖/force/非法 id/路径穿越/顺序覆盖/共享 row 清理/失败状态；settings schema/掩码/CAS/secret 保留/深合并/pydantic 校验；inspect services/plugins/tools/prompts/commands/providers 与失败行；四个 agent 工具的缺服务/参数校验/异常转 is_error。

## 12. 与 DSH 差异与已知风险

- 设置改动只落盘，不热更新：describe 的 apply 恒为 next-boot，restart_required=True；运行中的 Service 不会重读配置。
- 只有声明了 pydantic Config 的插件才有 JSON schema；否则 describe 的 schema 为 null，write 只做合并与落盘（无结构校验）。
- secret 判定是按字段名的启发式，存在误判（maxTokens 被掩码）和漏判（credential 不掩码）；没有独立密钥库或加密存储，掩码只影响 read/describe 的展示。
- 清单重写会丢失注释、未知顶层键和原始排版，只保留 [[plugins]] 的语义内容（id/name/config/disabled/before/after）。这是最小 TOML 序列化的刻意取舍，避免引入 TOML 写库依赖。
- settings/inspect 通过 default composition 读取生效视图；通过 extra_plugins 挂载、且不在用户清单/composition 里的插件，config_schema 走 PluginManager 兜底解析 target，但没有生效 config 行。
- 用户 bundle 安装只做结构校验，不签名、不沙箱；下一次 boot 会执行其中代码，因此 install 属于需要人工批准的高风险操作。
- inspect 读取失败 fiber 的错误文本用到了 vendor cordis 的私有属性（fiber._error），vendor 升级时可能失效。
- 删除 bundle 或目录插件不会停止当前进程里已加载的插件；需要重启。
- 清单写入没有跨进程锁，假定单 TUI 进程独占用户清单。
- 目录插件可导入性：正常 boot 进程不会自动把插件目录加入 sys.path；PluginManager 扫描时插入当前进程 sys.path，因此 validate/enable 可用。新进程直接 boot kernel 时，需 PYTHONPATH 或让 plugin-manager 行先加载；entry point 插件属于已安装发行版，无此问题。
- 不提供安装、升级、pip uninstall 或版本选择；entry point 来源只能由 pip 管理，remove 明确拒绝。
- 用户清单读取失败时 settings 抛 PluginError、外部插件 enable/disable 拒绝写盘，避免覆盖原文件。
