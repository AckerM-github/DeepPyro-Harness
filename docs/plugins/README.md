# 插件开发指南

> **状态（0.2.0）**：仓库当前实现的插件 API 是 **v2**（`deeppyro.api.API_VERSION == 2`）。0.2.0 计划按 ADR 硬切到 **API v3**：
> one mount、`setup(self)`、typed `inject`、能力清单（CapabilityManifest）、entry points、service typing。
> v3 仍在 M3 合流中，尚未落地；本文用 **[v2 可用]** 标注现在就能用的写法，用 **[v3 目标 / TODO(api-v3)]** 标注
> 只有 ADR 决策、尚无实现或细节未定稿的部分。两者冲突时以 `src/deeppyro/api/` 代码为准。
> 从 v2 迁到 v3 的逐项对照见 [migration-v2-v3.md](migration-v2-v3.md)。

## 0. 0.2.0 对插件作者意味着什么

0.2.0 是破坏性版本（0.x 政策允许）。与插件作者直接相关的变更：

| 变更 | 状态 | 影响 |
| --- | --- | --- |
| 插件 API 硬切 v3，无兼容 shim | 已决策（ADR D2/D5），M3 实现中 | v1 `setup(ctx, config)`、v2 的写法需要迁移 |
| `setup(self)` 成为唯一 mount 形状 | 已实现（v2 即如此） | v1 旧签名仍可用但会 `DeprecationWarning`，v3 移除 |
| typed `inject`（契约类）与 zero-string service 查找 | 部分实现（`inject` 已支持契约类） | v3 目标：调用侧不再用 `ctx.service("name")` |
| `CapabilityManifest` 能力清单 | **未实现** | 字段与校验规则未定稿，见 §4 TODO(api-v3) |
| 配置键 snake_case（含插件 row config） | 迁移中 | 0.2.0 目标；旧 camelCase 配置不再读取 |
| 会话日志只认 native schema 1 | 已实现（M1） | 插件不得读写 `session.v4.jsonl`；旧数据不兼容 |

插件包与 bundle 的文件形态（`bundle.toml` + `[[plugins]]` 行）不变，但 `bundle.api_version` 会随 API 版本升级，
当前支持到 2（大于 2 直接 `CompositionError`）。

## 1. 快速开始：最小插件 [v2 可用]

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
    version = "0.1.0"
    api_version = 2
    inject = ("tools", "prompt", "commands")   # [v3 目标] 改为契约类，见 §3

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

## 2. 插件类契约（`deeppyro.api.Plugin`）[v2 可用]

| 成员 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- |
| `api_version` | `ClassVar[int]` | `2` | 与 `deeppyro.api.API_VERSION` 比对；声明高版本会被拒绝 |
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

[v3 目标 / TODO(api-v3)] v3 会收紧为“只有一个 mount 形状”：`setup(self)` 唯一，v1 签名删除；
`api_version` 仍是整数门禁而不是协商协议（ADR D5 M3）。具体的 v1 移除时点与报错文案未定稿。

## 3. 依赖注入与服务 [v2 可用 + v3 目标]

### 3.1 `inject`

`inject` 的元素可以是：

- **服务名字符串**（如 `"tools"`、`"prompt"`、`"hooks"`）；
- **契约类**（`Service` 子类或已 `bind_service_type` 绑定的类，如 `CommandsPlugin`、`EventBus`、`HookService`）。

契约类在挂载时经 `deeppyro.api.inject_names()` 解析为规范服务名（`api.services.service_name()`）；类未绑定服务则
`CompositionError` 并点名该 row。规范服务名清单在 `src/deeppyro/api/services.py` 维护（当前 36 个，如 `tools`、
`prompt`、`commands`、`llm`、`sessions`、`session_query`、`hooks`、`bus`、`kernel`、`settings`、`slots`、`subagent`）。

### 3.2 运行期取服务 [v2 可用]

| 调用 | 返回 | 缺失时 |
| --- | --- | --- |
| `ctx.get(Contract)` | `T \| None` | 返回 `None`（契约未绑定也返回 `None`） |
| `ctx.get("name")` | `Any \| None` | 返回 `None` |
| `ctx.has("name")` | `bool` | `False` |
| `ctx.require(Contract)` | `T` | `ServiceNotFoundError`（`ServiceTypeError` 表示契约未绑定） |
| `ctx.service("name")` | `Any` | `PluginDependencyError` |

类型化快捷属性：`ctx.tools`、`ctx.prompt`、`ctx.commands`、`ctx.llm`、`ctx.presets`。`ctx.ui` 在没有 TUI 时返回
`_NoopUi`（`available=False`，所有方法返回 `False`），headless 插件无需特判。

`ctx` 未定义的属性会代理到 runtime context，所以 `ctx.on` / `ctx.once` / `ctx.emit` / `ctx.effect` / `ctx.isolate` /
`ctx.intercept` / `ctx.provide` / `ctx.plugin` / `ctx.inject` / `ctx.raw` 都可继续使用。

[v3 目标 / TODO(api-v3)] ADR 要求插件侧“零 string `.service(` 调用”，即用契约类完成依赖声明与查找。
是否同时移除 `ctx.service(str)` / `ctx.get(str)` 公共 API、`inject` 里是否还允许字符串、迁移是否给弃用期，
都留给 M3 定稿；当前不要依赖未来具体报错文案。

### 3.3 提供服务的 `Service` 子类 [v2 可用]

```python
from deeppyro.api import Service


class NotesService(Service):
    provide = "notes"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)   # 构造即注册服务，卸载即注销
        self.items = []
```

- `provide` 是服务名（按仓库约定 snake_case，如 `session_query`）。
- `__init_subclass__` 会把该子类登记为这个名字的类型化契约，因此其他插件可写 `ctx.require(NotesService)`。
- `name` 是实例注册名；`intercept_config` 是宿主按层合并后的 intercept 配置（group row / preset realm，最近层优先）。
- 历史字符串注册方式仍可用：`ctx.provide("name", value, check)` 会同步登记 `type(value) -> name`，让 `ctx.get(Contract)` 也能找到。
- 工具函数：`ServiceBag`、`lookup_service`、`require_service`、`bind_service_type`、`service_name`、`contract_path` 由 `deeppyro.api` 导出。

## 4. CapabilityManifest（能力清单）[v3 目标 / 未实现]

现状（0.2.0 基线）：**仓库中没有 `CapabilityManifest` 实现**，不要按本文想象字段去写代码。当前表达能力的真实手段是：

- bundle row（`critical`、`inject`、`scope`）；
- 通过 `ctx.register_*` 实际贡献的 tool / prompt section / command / provider / preset；
- `App.row_states()` 的 `state` / `error` / `overridden` / `disabled` 等字段。

目标（ADR D5 M3）：每个插件以声明式清单报告“我依赖什么、贡献什么、有什么风险/成本”，以便能力治理、A 线消费
`risk`/`cost` 元数据、Y 线用 dataclass 表达。P 线（`api/`）拥有该契约。

**TODO(api-v3)**：以下均未定稿，M3 落地前不得虚构：

- 字段集合（至少涉及 capabilities、依赖、permission/risk、配置 schema）；
- 是否存在 `capabilities` 类属性或独立 dataclass；是否必填；内置工具/命令是否强制清单；
- 与 `inject`、bundle row、`row_states` 的关系；校验失败是 boot 失败还是告警；
- 与 M4 `eval`/权限治理的对接方式。

在此之前，插件作者可以把能力写进 `description` / README，等 M3 后再迁移到正式清单。

## 5. 扩展点 [v2 可用]

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

- **UI slots**：经 `ui` service 贡献 STATUS / CONVERSATION / PANEL / SETTINGS 内容；0.2.0 仅 append-only。
- **运行时自扩展**：`plugin_manager` 服务与 `settings`（CAS）服务；内置 `/plugins` 命令与相关工具见
  `docs/spec/plugins.md` / `docs/spec/plugin-settings.md`。
- **hooks / events**：`ctx.on(...)` 订阅 catalogued 事件（`hook/*` 等），走 kernel 的 `EventBus` / `HookService`；
  具体事件名与 payload 见 `docs/spec/hooks.md`、`docs/spec/events.md`。

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
version = "0.1.0"
api_version = 2          # 当前上限 = deeppyro.api.API_VERSION；> 2 报 CompositionError
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
- **失败分类**：`critical=true` 的 row 挂载失败 → `KernelBootError` 并 dispose 已挂 fiber；
  外部 row 默认非 critical → 记入 `app.failures`、`state=failed`，boot 继续。
- **requires**：`[bundle.requires]` 或字符串数组，操作符 `>= <= > < == = !=`；缺失/环/不支持 → `CompositionError`。
- **include**：`include = "rows.toml"` 是加载期宏，只支持 TOML/JSON、相对路径、嵌套有上限；展开后行不再可被单独 patch 寻址。

core bundle 当前 42 行。前两行示例（`kernel` 为 critical；`memory` 为非 critical、可缺省）：

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

### 6.4 加载与调试 [v2 可用]

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

## 7. 已知限制（0.2.0 仍适用）

摘自 `docs/spec/plugin-core.md` §9，并补充 0.2.0 语境：

- settings 写入下次 boot 生效（volatile 字段除外）；settings 无跨进程锁，重写用户 manifest 会丢注释。
- `bundle install` 只做结构校验；第三方代码下次 boot 才执行，无签名、无沙箱；移除 bundle 不会停掉已加载插件。
- 插件进程内运行、加载后即受信，没有 OS 级隔离；插件可以访问同进程服务。
- `api_version` 是单整数门禁，不是协商协议。
- live config 更新仅限 volatile，不重启 fiber、不写盘。
- 普通 host 风格插件类（非 `deeppyro.api.Plugin`）没有 `volatile`/`on_config`，只能 boot 时配置。
- bundle 仅 TOML（无 YAML / `!!js` 表达式）；include 只 TOML/JSON。
- group 子行不能被 patch 单独寻址；group 容器加载后静态。
- TUI slot 贡献 append-only：消息渲染后新增的 conversation row 不会被重渲染。
- 切换 preset 会重建 live agent，但不改写当前会话 header 的 `agentPreset`。
- 0.2.0 起插件只能面向 native schema 1 会话；旧 `session.v4.jsonl` 不读不写。

## 8. 参考

- `src/deeppyro/api/plugin.py`、`context.py`、`services.py`、`types.py`：本文的代码级事实来源。
- `src/deeppyro/kernel/composition.py`：bundle/row/patch 的校验与合并实现。
- `src/deeppyro/bundles/core/bundle.toml`：42 个内置插件行。
- `docs/spec/plugin-core.md`：组合内核与 v2 契约的完整规格。
- `docs/spec/plugin-platform.md`：插件包安装单元（`plugin.toml`）与 composer 层序。
- `docs/plugins/migration-v2-v3.md`：v2 → v3 迁移与 0.2.0 破坏性变更清单。
