# 插件开发指南

> **状态（0.3.0）**：仓库当前实现的插件 API 是 **v3**（`deeppyro.api.API_VERSION == 3`）。v3 已按 ADR 硬切：
> 唯一挂载入口 `RuntimeContext.mount(plugin, config)`、唯一钩子 `setup(self)`、typed `inject`、typed service lookup、
> 能力清单（CapabilityManifest）、bundle v3 与 entry points。v1/v2 没有任何兼容 shim。
> 0.3.0 是 native-only 硬切：插件只面对 native schema 1 会话，headless/TUI/Web 三个入口共用同一 runtime；
> v4 会话栈、四个 0.2.0 回退开关与 `dpyro migrate` 已删除（插件 API 形状本身不变）。
> 本文描述的是**已落地**的 v3 契约；与代码冲突时以 `src/deeppyro/api/` 为准。
> 从 v2 迁到 v3 的逐项对照见 [migration-v2-v3.md](migration-v2-v3.md)。

## 0. 0.3.0 对插件作者意味着什么

v3 硬切发生在 0.2.0；0.3.0 的破坏面是**会话语义与已删除的 v4 API**，插件 API 形状不变。与插件作者直接相关的变更：

| 变更 | 状态 | 影响 |
| --- | --- | --- |
| 插件 API v3 保持不变 | 现行 | v1 `setup(ctx, config)`、v2 的写法仍然直接报 `PluginError` |
| `setup(self)` 仍是唯一 mount 形状 | 现行（0.2.0 起的 v3 契约） | 位置参数形式的 setup 在挂载时拒绝并给出迁移提示 |
| typed `inject` 与 zero-string service 查找 | 现行（0.2.0 起的 v3 契约） | 插件侧 `ctx.get/require/has` 只接受契约类；字符串抛 `TypeError` |
| 配置键 snake_case（含插件 row config） | 现行；自 0.2.0 起严格 | camelCase TOML alias 与 schema 未知键抛 `ConfigError`，见 `docs/guides/config-migration.md` |
| 会话日志格式 | native schema 1（唯一） | TUI、headless、Web 都只写 native 会话；0.2.0 的 v4 分支与 `--legacy-v4` 回退已删除 |
| `--legacy-v4` / `--native-loop` / `DEEPPYRO_NATIVE_LOOP` / `DEEPPYRO_TUI_NATIVE` | 已删除 | 这些开关不再是插件可依赖的环境；传入即 CLI 解析错误 |
| `dpyro migrate` 与会话迁移 | 已删除 | 旧 v4 会话文件不再可读；需要旧数据请在 0.2.x 上导出 |
| Web 宿主插件 | 可用（非 core bundle 行） | `deeppyro.web.plugin:WebHostPlugin`（provide `web`）由 Web 宿主 / TUI splash 显式构造并 `start()`，不占 core bundle 的 41 个插件行 |

插件包与 bundle 的文件形态（`bundle.toml` + `[[plugins]]` 行）不变。`bundle.api_version` 必须等于 3
（省略时默认 3）；其它值在加载期直接 `CompositionError`。

## 1. 快速开始：最小插件

下面这份插件与 `tests/integration/test_plugin_composition.py` 中真实运行的示例一致：一个工具 + 一个 prompt 段 + 一个斜杠命令。

```python
from deeppyro.api import Plugin
from deeppyro.core.protocols import ToolResult
from deeppyro.tools.base import BaseTool, make_parameters


class GreetTool(BaseTool):
    name = "greet"
    description = "Greet somebody by name."
    parameters = make_parameters({"who": {"type": "string"}}, required=["who"])

    async def run(self, args, ctx):
        return ToolResult(text="hello " + str(args.get("who", "world")))


class GreetPlugin(Plugin):
    plugin_id = "greet"
    version = "0.3.0"
    api_version = 3
    inject = ("tools", "prompt", "commands")   # 字符串仍允许；生产插件推荐契约类，见 §3

    def setup(self) -> None:
        self.ctx.register_tool(lambda config, workspace: GreetTool())
        self.ctx.register_prompt_section(
            "greet-hint", 45, lambda tools, prompt_ctx: "## Greeting\nAlways be polite."
        )

    def run_greet(self, host, argument):
        ...  # 命令 handler 由命令宿主调用：handler(host, argument)，host 提供命令上下文
```

要点：

- `setup(self)` 没有参数；插件从 `self.ctx`（`PluginContext`）和 `self.config`（校验后的配置）读上下文。
- 插件实例由 kernel 的 adapter 构造，**不要自己 `new` 插件**。
- 所有通过 `ctx` 注册的东西都自动登记为 effect，fiber 卸载时撤销（工具、prompt 段、命令、provider 等）。
- 注册时抛异常会让该 row 失败：`critical=true` 的 row 中止整个 boot，外部 row 记入 `app.failures` 并继续。

## 2. 插件类契约（`deeppyro.api.Plugin`）

| 成员 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- |
| `api_version` | `ClassVar[int]` | `3` | 与 `deeppyro.api.API_VERSION` 比对；不等于 3 一律拒绝 |
| `plugin_id` | `ClassVar[str]` | `""` | row 与诊断里的稳定标识；建议 kebab-case |
| `version` | `ClassVar[str]` | `"0.0.0"` | 插件自身版本（与 API 版本无关） |
| `description` | `ClassVar[str]` | `""` | 人类可读描述 |
| `Config` | `ClassVar[type[BaseModel] \| None]` | `None` | row 的 `config` 表会校验成该 pydantic 模型；为 `None` 时 `self.config` 是普通 dict |
| `volatile` | `ClassVar[tuple[str, ...]]` | `()` | 允许运行时热更新的配置字段（`apply_plugin_config`） |
| `inject` | `ClassVar[Sequence[str \| type]]` | `()` | 依赖的服务名或契约类；挂载前解析校验 |
| `ctx` / `config` | 实例属性 | — | 由 adapter 在 `setup()` 前写好 |
| `setup()` | 方法 | — | 唯一注册入口；可同步或异步 |
| `async stop()` | 可选方法 | — | 显式清理；loader 会把它注册为 fiber disposer |
| `on_config(config)` | 可选方法 | — | 配置热更新后的回调；必须同步 |

生命周期：

1. composition 层解析 row（插件类 + `config` + patch）。
2. adapter 用 `(ctx, config)` 构造插件实例。
3. fiber 激活时调用 `setup()`；注册 contribution。
4. 运行时 `kernel.apply_plugin_config()` 只允许改 `volatile` 字段：重新校验配置 → 调用 `on_config(config)`；
   非 volatile 变更返回 `False` 并把 row 标记为 `restart-required`（内核从不重启 fiber、也不持久化配置）。
5. 卸载时调用 `stop()`（若定义）并撤销 effect。

v3 只有一个 mount 形状：`setup(self)` 唯一，v1/v2 签名已删除（ADR §7 hard cut）。
`api_version` 仍是整数门禁而不是协商协议：挂载其它版本会在 `invoke_setup` 抛 `PluginError` 并给出 v3 迁移提示。

## 3. 依赖注入与服务

### 3.1 `inject`

`inject` 的元素可以是：

- **服务名字符串**（如 `"tools"`、`"prompt"`、`"hooks"`）；
- **契约类**（`Service` 子类或已 `bind_service_type` 绑定的类，如 `CommandsPlugin`、`EventBus`、`HookService`）。

契约类在挂载时经 `deeppyro.api.inject_names()` 解析为规范服务名（`api.services.service_name()`）；类未绑定服务则
`CompositionError` 并点名该 row。规范服务名清单在 `src/deeppyro/api/services.py` 维护（以该文件为准；例如 `tools`、
`prompt`、`commands`、`llm`、`sessions`、`hooks`、`bus`、`kernel`、`settings`、`slots`、`subagent`；0.3.0 已删除
`session_query` 服务）。

### 3.2 运行期取服务

| 调用 | 返回 | 缺失时 |
| --- | --- | --- |
| `ctx.get(Contract)` | `T \| None` | 返回 `None`（契约未绑定也返回 `None`） |
| `ctx.has(Contract)` | `bool` | `False` |
| `ctx.require(Contract)` | `T` | `ServiceNotFoundError`（`ServiceTypeError` 表示契约未绑定） |

v3 已移除字符串形式：`ctx.get("name")` / `ctx.has("name")` 抛 `TypeError`；`ctx.service(...)` 不存在。
宿主级同样只接受契约类：`App.get(Contract)` / `App.require(Contract)`；过渡期的 `App.service(name)` /
`App.get(str)` / `App.has_service(name)` 已按 KI-035 删除（回归见 `tests/api/test_services.py`）。

类型化快捷属性：`ctx.tools`、`ctx.prompt`、`ctx.commands`、`ctx.llm`、`ctx.presets`。`ctx.ui` 在没有 TUI 时返回
`_NoopUi`（`available=False`，所有方法返回 `False`），headless 插件无需特判。

`PluginContext` 在 v3 是封闭的：未定义属性直接 `AttributeError`，不再代理到 runtime context。
需要 kernel 级操作时显式走 `ctx.raw`（host 集成路径），不要依赖 `ctx.on` / `ctx.emit` / `ctx.effect` 等隐式转发。
插件自身可用的注册入口是 §5 的 `register_*` 与 `provide` / `mount`。

### 3.3 提供服务的 `Service` 子类

```python
from deeppyro.api import Service


class NotesService(Service):
    provide = "notes"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)   # 构造即注册服务，卸载即注销
        self.items = []
```

- `provide` 是服务名（按仓库约定 snake_case，如 `user_questions`、`working_directory`）。
- `__init_subclass__` 会把该子类登记为这个名字的类型化契约，因此其他插件可写 `ctx.require(NotesService)`。
- `name` 是实例注册名；`intercept_config` 是宿主按层合并后的 intercept 配置（group row / preset realm，最近层优先）。
- 历史字符串注册方式仍可用：`ctx.provide("name", value, check)` 会同步登记 `type(value) -> name`，让 `ctx.get(Contract)` 也能找到。
- 工具函数：`ServiceBag`、`lookup_service`、`require_service`、`bind_service_type`、`service_name`、`contract_path` 由 `deeppyro.api` 导出。

## 4. CapabilityManifest（能力清单）

v3 已实现 `src/deeppyro/api/capabilities.py`。每个 `api.Plugin` 都有 `capability_manifest()`，默认由
`build_manifest(self)` 从类属性派生；可覆写该方法补充工具/成本等细节。字段（frozen slots dataclass）：

| 字段 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- |
| `plugin_id` | `str` | 类名 | 稳定标识，不能为空 |
| `version` | `str` | `"0.0.0"` | 插件版本 |
| `services_provided` | `tuple[str, ...]` | `()` | 由 `provide` 派生 |
| `services_required` | `tuple[str, ...]` | `()` | 由 `inject` 派生 |
| `tools` | `tuple[ToolCapability, ...]` | `()` | 工具名、`risk`、`idempotent`、`parallel_safe`、`token_cost` |
| `hooks` | `tuple[str, ...]` | `()` | 订阅/发出的事件或 hook 名 |
| `permissions` | `tuple[str, ...]` | `()` | 宿主权限，如 `tools:write` |
| `sandbox` | `str \| None` | `None` | `workspace-write` / `danger-full-access` |
| `risk` | `RiskLevel` | `low` | `low` / `medium` / `high` |
| `idempotent` | `bool` | `True` | 挂载/setup 是否可安全重放 |
| `token_cost` | `TokenCost \| None` | `None` | 每百万 token 的 input/output/cached 价格 |
| `context_window` | `int \| None` | `None` | 插件假设的上下文窗口 |
| `metadata` | `Mapping[str, Any]` | `{}` | 附加信息（默认含 module/qualname） |

插件类上的对应声明处为 `risk`、`idempotent`、`permissions`、`sandbox`、`token_cost`、`context_window`；
工具可用 `BaseTool.risk` / `BaseTool.idempotent` 标注，`tool_capability(tool)` 会折叠为 `ToolCapability`。
这些治理字段**不进入** model-facing tool schema——43 个内置工具的 schema 快照保持不变。
`CapabilityManifest.to_dict()` 可供 inspect/工具输出；`UsageService`（`builtin/usage.py`）随 core bundle 的
`usage` 行挂载，记录每轮用量（native loop 通过 `UsageSink` 调用 `record_turn`）。

## 5. 扩展点

全部经 `PluginContext` 注册，返回值是 disposer，并自动登记 effect：

| 方法 | 签名（当前实现） | 说明 |
| --- | --- | --- |
| `register_tool` | `(factory, *, scope=None)` | `factory(config, workspace) -> tool 或 tool 列表` |
| `register_prompt_section` | `(name, order, renderer, *, scope=None)` | `renderer(tools, prompt_context) -> str`；按 `(order, name)` 排序 |
| `register_preset` | `(preset)` | 注册 `PresetSpec`（id/name/description/order/model?/instructions） |
| `register_command` | `(name, handler, *, summary="", usage="", aliases=())` | `handler(host, argument)`；`host` 由命令层提供 |
| `register_provider` | `(name, factory, *, description="")` | `factory(llm_service) -> LlmProvider` |

`scope` 是 preset id 作用域（realm）：在该作用域里注册的贡献只对该 preset 的 agent 可见，未指定 scope 的贡献
会被钉到当前 realm 的 revision，避免泄漏到其它 preset（`PluginContext.contribution_scope` 实现）。

其它能力通道：

- **UI slots**：经 `ui` service 贡献 STATUS / CONVERSATION / PANEL / SETTINGS 内容；默认 append-only，显式替换/覆盖见 §7。
- **运行时自扩展**：`plugin_manager` 服务与 `settings`（CAS）服务；内置 `/plugins` 命令与相关工具见
  `docs/spec/plugins.md` / `docs/spec/plugin-settings.md`。
- **hooks / events**：`ctx.on(...)` 订阅 catalogued 事件（如 `tools/pre-execute`、`tools/execute`、
  `tools/post-execute`、`approval/request`、`command/executed`、`goal/changed`、`session/created`），走 kernel 的
  `EventBus` / `HookService`；声明清单见 `src/deeppyro/core/events.py`。native loop 自身的生命周期用 typed
  `AgentHooks` 观察（由宿主构造 `AgentLoop` 时传入，不经过 EventBus）。详见 `docs/spec/hooks.md`、`docs/spec/events.md`。

## 6. bundle：目录、声明与组合

### 6.1 目录

```text
my-bundle/
  bundle.toml
  greet.py            # 插件模块；row.plugin 可写相对路径 "greet.py:GreetPlugin"
```

### 6.2 `bundle.toml` 示例

```toml
[bundle]
id = "greet-bundle"
name = "Greet bundle"
version = "0.3.0"
api_version = 3          # 必须等于 deeppyro.api.API_VERSION；其它值报 CompositionError
order = 50               # 同层用户 bundle 按 (order, id) 应用
critical = false         # 外部 bundle 默认 false：失败只记 app.failures，不中止 boot

[[plugins]]
id = "greet"
plugin = "greet.py:GreetPlugin"
config = { greeting = "hello" }

[[plugins]]
id = "greet-extra"
plugin = "greet.py:GreetPlugin"
config = { greeting = "hi" }
after = "greet"
before = "tools-builtin"
critical = false

# 组：一行声明多个子行；组没有自己的 fiber，disable 组会跳过全部子行
[[plugins]]
id = "greet-group"
group = true
[[plugins.rows]]
id = "greet-inner"
plugin = "greet.py:GreetPlugin"
```

`[[plugins]]` 行支持的键（未知键直接拒绝）：`id`、`plugin`、`name`、`config`、`disabled`、`before`、`after`、
`critical`、`isolate`、`intercept`、`group`、`rows`、`include`。

### 6.3 组合规则

- **层序**：打包的 core bundle（`src/deeppyro/bundles/core/bundle.toml`，`order=0`、`critical=true`）→
  用户 bundle `~/.dpro/bundles/<id>/bundle.toml`（按 `order`,`id`）→ 插件包内的 `plugins/<id>/bundle.toml` →
  显式 `boot(bundle_paths=[...])` → 用户 patch manifest `~/.dpro/plugins.toml`。core 永远第一。
- **patch 语义**：同 `id` 的行覆盖：`config` **整体替换**（不做深合并）、`disabled`/`critical` 覆盖、
  `isolate`/`intercept` 合并、插件类不可更换；被覆盖行标 `overridden`。
- **group 子行 patch**：组的结构由声明它的 bundle 拥有；patch 层用**限定地址**改单个子行——把行 `id` 写成
  `"<group-id>/<child-id>"`，嵌套组继续用 `/` 分段（如 `"outer/inner/deep"`）。子行 patch 只允许改 `config`
  （整体替换）、`disabled`、`critical`，以及沿用的 `name`/`isolate`/`intercept` 合并规则；不能新增/删除/重排
  子行、不能把子行变成组、不能带 `before`/`after`、不能更换插件类，地址解析不到也抛 `CompositionError`。
  顶层 id 精确匹配优先，所以顶层 id 本身含 `/` 时仍按顶层行处理。plain 子行 id 只有在整个组合里唯一时才能被
  settings / 行状态按短 id 找到，重复时必须用限定地址（歧义时按未知插件报错，不会猜）。
- **group 子行 patch 的落盘位置**：不重写声明组的 `bundle.toml`。settings 写入落到**用户 patch manifest** 的
  一行 `id = "group/child"`，由 `manifest_store` 的行级编辑保留注释、块顺序与未知键，无变化时字节不变；
  `~/.dpro/plugins.toml` 因此始终是唯一被改动的文件。
- **失败分类**：`critical=true` 的 row 挂载失败 → `KernelBootError` 并 dispose 已挂 fiber；
  外部 row 默认非 critical → 记入 `app.failures`、`state=failed`，boot 继续。
- **requires**：`[bundle.requires]` 或字符串数组，操作符 `>= <= > < == = !=`；缺失/环/不支持 → `CompositionError`。
- **include**：`include = "rows.toml"` 是加载期宏，只支持 TOML/JSON、相对路径、嵌套有上限；展开后行不再可被单独 patch 寻址。

core bundle 当前 **41 个 `[[plugins]]` 行 + `[bundle]` 元数据**（`version = "0.3.0"`；`[bundle]` 不是插件行）。
Web 宿主插件 `WebHostPlugin`（provide `web`）不在这 41 行内，由 Web 宿主 / TUI splash 显式构造并 `start()`。
前两行示例（`kernel` 为 critical；`memory` 为非 critical、可缺省）：

```toml
[[plugins]]
id = "kernel"
plugin = "deeppyro.kernel.runtime:KernelRuntime"
critical = true

[[plugins]]
id = "memory"
plugin = "deeppyro.builtin.memory:MemoryPlugin"
critical = false
```

### 6.4 加载与调试

```python
from deeppyro.kernel import boot

app = await boot(config, bundle_paths=["/path/to/my-bundle"])
print(app.row_states())          # id/plugin/state/runtime_state/origin/error/overridden/...
await app.dispose()
```

命令行：`dpyro plugin list` / `info <id>` / `doctor [id]` / `path` / `enable` / `disable` / `add` / `remove` /
`update` / `allow-version`（均支持全局 `--json`）。

### 6.5 测试你的插件

仓库内的范例是 `tests/integration/test_plugin_composition.py`：把 bundle 写进临时目录，`boot(cfg, bundle_paths=[...])`，
再断言工具可调用、prompt 段被渲染、斜杠命令存在、LLM provider 注册成功。外部插件建议照此写集成测试，
不要只测插件类本身。

## 7. 已知限制（0.3.0 仍适用）

摘自 `docs/spec/plugin-core.md` §9，并补充 0.3.0 语境。0.2.0 时代的已知问题（KI-019/KI-021）保留在
[`docs/release/0.2.0-known-issues.md`](../release/0.2.0-known-issues.md)，仅作历史；当前版本范围与已知问题关闭情况见
[`docs/release/0.3.0.md`](../release/0.3.0.md)：

- settings 写入在跨进程文件锁内做 CAS+merge（Windows `msvcrt` / POSIX `fcntl`，默认 5s 超时，超时抛 `SettingsLockTimeout`）；
  用户 manifest 采用行级编辑，保留注释、键顺序与未知键（无变化时字节不变）。仍有“需重启才生效”的字段。
  实现见 `src/deeppyro/plugins/file_lock.py` 与 `plugins/manifest_store.py`。
- `bundle install` 只做结构校验，安装与运行都没有 OS 沙箱；第三方代码下次 boot 才执行；移除 bundle 不会停掉已加载插件。
  可选 `[bundle].digest = "sha256:…"` 按磁盘原始字节钉住 bundle（见 `docs/guides/bundle-digest.md`）；
  可选 `[bundle].signature = "hmac-sha256:…"` 用**共享密钥**做 HMAC-SHA256 完整性/真实性校验，
  密钥来自 `DEEPPYRO_BUNDLE_SIGNING_KEY` 或 `DEEPPYRO_BUNDLE_SIGNING_KEY_FILE`：声明了签名却缺密钥、
  签名格式非法或字节不匹配都会在 boot 拒绝启动，未声明签名照常启动（见 `docs/guides/bundle-signing.md`）。
  签名不是身份/PKI，也不是 OS 沙箱，只证明“持有同一共享密钥的某一方签发过这些字节”。
- 插件进程内运行、加载后即受信，没有 OS 级隔离；插件可以访问同进程服务。
- `api_version` 是单整数门禁，不是协商协议。
- live config 更新仅限 volatile，不重启 fiber、不写盘。
- 普通 host 风格插件类（非 `deeppyro.api.Plugin`）没有 `volatile`/`on_config`，只能 boot 时配置。
- bundle 仅 TOML（无 YAML / `!!js` 表达式）；include 只 TOML/JSON。
- group 子行可用限定地址（`parent/child`）patch 单个子行的 `config`/`disabled`/`critical`，但不重写声明组的
  bundle：不能新增/删除/重排子行、不能换插件类、不能加子行；`include` 展开的行仍不可单独寻址；plain 子行 id
  在多个组里重复时必须用限定地址。组的挂载点与子行列表在加载后仍然静态。
- TUI slot 贡献默认 append-only：重复 `(kind, id)` 抛 `SlotError`（错误文本含双方 plugin id）。需要更新或替换
  已有贡献时必须显式传 `on_conflict`：`SlotConflict.REPLACE` 只允许更新本插件自己的槽（跨插件 `REPLACE`
  报错并提示改用 `OVERRIDE`）；`SlotConflict.OVERRIDE` 是唯一能替换别的插件贡献的策略，胜出贡献的
  `replaced_plugin_id` 记录原 owner，`registry.overrides()` 返回跨插件替换历史（`SlotOverride`），
  `clear()` 同时清空该历史。消息渲染后新增的 conversation row 仍不会被重渲染。
- 切换 preset 会重建 live agent，但不改写当前会话 header：TUI `SessionLayer` 在应用前后快照并断言 header
  不变（违规抛 `DeeppyroError` 并回滚切换），只有新会话或显式 fork 才写入新的 `agentPreset`。
- preset 的 `model` 在注册（`add()`）与应用（`resolve()`）时按 `[providers]` 配置校验：默认
  `ConfigWarning`，`DEEPPYRO_STRICT_CONFIG` 下为 `ConfigError`；未知 provider/model 不再等下一次请求才失败。
  配置加载本身自 0.2.0 起始终严格，这个开关只影响 preset-model 校验。
- 会话只有 native schema 1（`~/.dpro/sessions/<id>.jsonl`）：headless、TUI、Web 共用同一 runtime；旧 v4 会话
  不再可读，也没有 `dpyro migrate`（需要旧数据请在 0.2.x 上导出）。插件不得导入 `deeppyro.core.wire`、
  `deeppyro.session.store`、`deeppyro.projections` 等已删除模块。

## 8. 参考

- `src/deeppyro/api/plugin.py`、`context.py`、`services.py`、`types.py`：本文的代码级事实来源。
- `src/deeppyro/kernel/composition.py`：bundle/row/patch 的校验与合并实现。
- `src/deeppyro/bundles/core/bundle.toml`：41 个内置插件行 + `[bundle]` 元数据。
- `docs/spec/plugin-core.md`：组合内核与 v3 契约的完整规格。
- `docs/spec/plugin-platform.md`：插件包安装单元（`plugin.toml`）与 composer 层序。
- `src/deeppyro/web/plugin.py`：Web 宿主插件（provide `web`，宿主显式 `start()`；不在 core bundle 行内）。
- `docs/release/0.3.0.md`：0.3.0 native-only 硬切与删除清单。
- `docs/plugins/migration-v2-v3.md`：v2 → v3 迁移与 0.2.0 破坏性变更清单（历史契约，当前仍适用）。