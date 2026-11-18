# 插件 API v2 → v3 迁移（0.2.0 历史契约，0.3.0 仍适用）

> **状态**：v3 已落地（M3，`deeppyro.api.API_VERSION == 3`），按 ADR
> `docs/adr/0001-three-track-native-agent.md` D2/D5/D7 **硬切，无兼容 shim**。
> v1/v2 插件在挂载时直接 `PluginError`；本文是逐项迁移对照。
> 0.3.0 没有再次硬切插件 API：本文的 v3 契约仍是现行契约，但会话侧已 native-only
> （v4 会话栈、`dpyro migrate` 与四个回退开关均已删除）。
> 事实来源：`src/deeppyro/api/`、`src/deeppyro/kernel/`。与文档冲突时以代码为准。

## 1. 0.2.0 破坏性变更（版本级）

| # | 变更 | 影响面 | 状态 |
| --- | --- | --- | --- |
| 1 | 插件 API v1/v2 硬切 v3，无兼容 shim | 所有外部插件 | ✅ M3 已实现 |
| 2 | 唯一挂载入口 `RuntimeContext.mount(plugin, config)`，唯一钩子 `setup(self)` | 插件启动代码 | ✅ M3 已实现 |
| 3 | `ctx.get/require/has` 只接受契约类，字符串抛 `TypeError`；`ctx.service(str)` 删除 | 运行期取服务的插件 | ✅ M3 已实现 |
| 4 | `PluginContext` 封闭，不再把未知属性代理到 runtime context | 依赖 `ctx.on/emit/effect` 的插件 | ✅ M3 已实现 |
| 5 | `CapabilityManifest` 成为插件契约的一部分 | 能力治理、审计、成本 | ✅ M3 已实现 |
| 6 | `bundle.api_version` 必须为 3 | 所有 bundle | ✅ M3 已实现 |
| 7 | 配置 canonical 键改 snake_case | `config.toml`、插件 `Config` | ✅ 0.2.0 起严格：camelCase alias 与 schema 未知键抛 `ConfigError`，迁移见 `docs/guides/config-migration.md` |
| 8 | 会话日志新增 native schema 1；headless 自 0.2.0 起默认 native，TUI 交互仍写 v4（0.2.0 时点状态） | 读会话的插件 | ✅ 0.3.0 收口：只有 native schema 1，v4 格式、`dpyro migrate` 与回退开关已删除 |
| 9 | 去品牌门禁（`rg -i dsh src` 接近 0） | `src/`、测试 fixtures | ⚠️ 0.2.0 时点未达标：377 行 / 94 文件（0.3.0 随 v4 清理继续收敛，最终品牌审计由 lead 负责） |
| 10 | Node/TypeScript fixtures 退役，CI 不安装 Node | 测试、fixtures | ⚠️ 部分完成：合成 fixtures 已入库（46 文件），真实录制基线仍为未跟踪的 26.6MB |

范围澄清：**session schema 不属于插件 API**。v3 只覆盖插件契约
（mount/inject/service typing/CapabilityManifest）；会话模型由 native schema 1 单独拥有（ADR D4）。

## 2. 逐项对照表

| 场景 | v1/v2 写法 | v3 写法 |
| --- | --- | --- |
| 插件基类 | `class P(Plugin)` | 不变 |
| 版本声明 | `api_version = 2` | `api_version = 3`（默认值即 3） |
| 挂载入口 | `setup(self)` 或 v1 `setup(self, ctx, config)` | 仅 `setup(self)`；位置参数形式被拒绝 |
| 依赖声明 | `inject = ("tools", ContractClass)` | 契约类优先；字符串项仍允许但不再推荐 |
| 取依赖（必选） | `ctx.service("tools")` / `ctx.require(ToolsPlugin)` | `ctx.require(ToolsPlugin)` |
| 取依赖（可选） | `ctx.get("tools")` / `ctx.get(ToolsPlugin)` | `ctx.get(ToolsPlugin)` |
| 判断依赖 | `ctx.has("tools")` | `ctx.has(ToolsPlugin)` |
| 提供服务 | `ctx.provide("notes", value)` 或 `class Notes(Service)` | `Service` 子类 + `provide` |
| 配置热更新 | `volatile` + `on_config(config)` | 不变 |
| 清理 | `async def stop(self)` | 不变 |
| 插件配置字段 | camelCase 或任意 pydantic 字段名 | snake_case（核心 `[agent]`/`[providers]` 表自 0.2.0 起严格；插件自有配置模型仍自定义字段） |
| 能力声明 | 无 | `capability_manifest()` / 类属性 |
| bundle 版本 | `api_version = 2` | `api_version = 3`（省略时默认 3） |
| 安装方式 | `plugin.toml` 包 / `plugins.toml` / bundle 路径 | 三者保留；新增 entry points 发现 |

## 3. 最小迁移步骤

1. **盘点入口**：列出 `setup` 签名、`inject`、`ctx.service/get/has` 调用、`ctx.provide` 调用、`Config` 字段名。
2. **改挂载入口**：`def setup(self)`（可 async），从 `self.ctx` / `self.config` 读上下文。
3. **改依赖**：`inject` 尽量用契约类；`ctx.require(Contract)` / `ctx.get(Contract)` / `ctx.has(Contract)`。
   找不到契约类的第三方服务仍可保留字符串 `inject`，但运行期查找只能走 `ctx.raw`。
4. **改服务提供**：字符串 `ctx.provide` → `Service` 子类（`ctx.provide` 仍可作为历史注册方式）。
5. **补能力清单**：声明 `risk` / `idempotent` / `permissions` / `sandbox` / `token_cost` / `context_window`，
   或覆写 `capability_manifest()` 列出工具与 hook。
6. **改配置**：`Config` 字段与 row `config` 键改 snake_case；同步配置文件与文档。
7. **改 bundle**：`bundle.api_version = 3`。
8. **去掉 v4 假设**：若读会话日志，改 `deeppyro.session.schema` + `deeppyro.session.fold`；
   不得依赖 `surfaceOp`、`sourceEventSeqs`、`ignorable`、62 种 v4 记录类型等机制。
9. **回归**：用 `boot(cfg, bundle_paths=[...])` 起临时应用，跑插件自己的集成测试，再跑 `dpyro plugin doctor`。

## 4. 代码对照

### 4.1 插件类

```python
from deeppyro.api import Plugin, RiskLevel
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.builtin.prompt_service import PromptPlugin


class GreetPlugin(Plugin):
    plugin_id = "greet"
    version = "0.3.0"
    api_version = 3
    inject = (ToolsPlugin, PromptPlugin)
    risk = RiskLevel.LOW
    permissions = ("tools:write",)

    def setup(self) -> None:
        self.ctx.register_tool(lambda config, workspace: GreetTool())
```

### 4.2 提供服务

```python
from deeppyro.api import Service


class NotesService(Service):
    provide = "notes"

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.items = []
```

构造即注册、卸载即注销；`__init_subclass__` 把 `NotesService` 绑定为该名字的契约类，
其他插件可写 `ctx.require(NotesService)`。`inject` 解析与运行期查找统一经
`deeppyro.api.services.service_name()`。

### 4.3 能力清单

```python
from dataclasses import replace

from deeppyro.api import CapabilityManifest, Plugin, RiskLevel, TokenCost, ToolCapability


class GreetPlugin(Plugin):
    ...
    risk = RiskLevel.MEDIUM
    idempotent = True
    sandbox = "workspace-write"
    token_cost = TokenCost(input_per_mtok=0.1, output_per_mtok=0.4)

    def capability_manifest(self) -> CapabilityManifest:
        manifest = super().capability_manifest()
        return replace(manifest, hooks=("tools/pre-execute",), tools=(ToolCapability(name="greet"),))
```

字段全集见 `docs/plugins/README.md` §4。治理字段不进入 model-facing tool schema。

## 5. 错误与排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `PluginError ... setup with positional parameters` | 仍是 v1/v2 签名 | 改成 `setup(self)`，从 `self.ctx`/`self.config` 读 |
| `PluginError ... targets api_version N` | `api_version != 3` | 声明 3 |
| `TypeError: PluginContext.get() is typed ...` | 传了字符串 | 传契约类 |
| `AttributeError: PluginContext has no attribute 'on'` | 依赖隐式转发 | 用 `ctx.raw` 或显式服务 |
| `ServiceTypeError` | 契约类未绑定服务名 | 让提供方用 `Service` 子类或 `bind_service_type` |
| `ServiceNotFoundError` | 服务未注册/插件未加载 | 检查 `inject` 与 `app.row_states()` |
| `CompositionError: ... api_version` | bundle 版本不是 3 | 改 `bundle.toml` |
| 插件挂载失败但 boot 继续 | row 非 critical | 查 `app.failures`、`row_states()` 的 `error` |

## 6. 兼容承诺

- 0.2.0 硬切 v3，不为 v2 提供 shim；0.x 版本允许破坏性变更。
- 不希望迁移的插件可以继续锁定 `deeppyro-harness` 0.1.x；0.2.0 不保证 0.1.x 插件可加载。
- 0.2.0 已移除 camelCase 配置 alias 并始终严格校验（迁移见 `docs/guides/config-migration.md`）；v4 会话读写路径在 0.3.0 已删除，当前只有 native schema 1。
- `UsageService` 已挂入 core bundle（`usage` 行），每轮记账写入 `~/.dpro/usage/usage.jsonl`；能力清单仍可供 inspect/工具读取。

## 7. 参考

- `docs/adr/0001-three-track-native-agent.md`：D2（零 wire 兼容、v1/v2 硬切 v3）、D4（契约归属）、D5（M3 门禁）。
- `src/deeppyro/api/{plugin,context,services,capabilities,version}.py`：v3 事实来源。
- `src/deeppyro/kernel/{runtime,composition,bundle_schema}.py`：mount 与 bundle 校验。
- `docs/spec/plugin-core.md`：v3 契约与组合语义规格。
- `docs/plugins/README.md`：插件作者指南（模板、扩展点、bundle 示例）。
- `docs/release/0.2.0.md`：版本级 release note（破坏性变更、门禁、已知限制）。
