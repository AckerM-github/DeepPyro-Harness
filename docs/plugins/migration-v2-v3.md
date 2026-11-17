# 插件 API v2 → v3 迁移（0.2.0）

> **状态**：0.2.0 计划把插件 API 硬切到 **v3**（ADR `docs/adr/0001-three-track-native-agent.md` D2/D5/D7）。
> 仓库当前实现仍是 **v2**（`deeppyro.api.API_VERSION == 2`），M3 合流后才提供 v3。
> 本文逐项标出：现在就能改的（v2 兼容）、0.2.0 配置迁移、以及只有决策、细节未定稿的 `TODO(api-v3)`。
> 不要按 TODO 项写代码；以 `src/deeppyro/api/` 为准。

## 1. 0.2.0 破坏性变更（版本级）

| # | 变更 | 影响面 | 状态 |
| --- | --- | --- | --- |
| 1 | 会话格式只认 native schema 1，`session.v4.jsonl`（含 `.zstd`）不再读写 | 会话数据、任何读会话的插件 | M1 已实现（v4 显式拒绝） |
| 2 | 插件 API v1/v2 硬切 v3，无兼容 shim | 所有外部插件 | M3 实现中 |
| 3 | 配置键 camelCase → snake_case；旧 camelCase 配置不再读取 | `config.toml`、插件 row `config` | M3 实现中 |
| 4 | 旧 DSH 规格与事件目录归档/待删；无 v4 reader、无 `dpyro migrate`、无 dual-write | 迁移工具、事件订阅方 | M2/M5 收尾 |
| 5 | 去品牌门禁：`rg -i dsh src` 接近 0 | `src/`、测试 fixtures、事件目录 | M5 目标（当前未清理） |
| 6 | Node/TypeScript fixtures 退役，CI 不安装 Node | 测试、fixtures | M5 目标 |

范围澄清：**session schema 不属于插件 API**。v3 只覆盖插件契约（mount/inject/service typing/CapabilityManifest）；
会话模型由 native schema 1 单独拥有（ADR D4）。

## 2. 现在就能做的兼容改写（v2 即可运行）

这些改动不依赖 M3 落地，先做可以减少 0.2.0 升级时的改动量：

1. **`setup` 改为无参**：`def setup(self) -> None`，从 `self.ctx` / `self.config` 读上下文。
   v1 的 `def setup(self, ctx, config)` 目前仍被 `deeppyro.api.invoke_setup` 按签名分派，但若类里声明了
   `api_version = 2` 会收到一次性 `DeprecationWarning`；v3 删除该兼容。
2. **`inject` 尽量用契约类**：`inject = (ToolsPlugin, "prompt")` 已可用；契约类在挂载时解析为服务名
   （`inject_names` / `service_name`），未绑定的类会 `CompositionError` 并点名 row。
3. **运行期查找改用契约类**：`ctx.service("tools")` → `ctx.require(ToolsPlugin)`（必选依赖）；
   `ctx.get("tools")` → `ctx.get(ToolsPlugin)`（可选依赖）；`ctx.has("tools")` 目前只接受字符串，保持。
4. **新服务用 `Service` 子类而不要 `ctx.provide` 字符串**：

   ```python
   from deeppyro.api import Service
   
   class NotesService(Service):
       provide = "notes"

       def __init__(self, ctx, config):
           super().__init__(ctx, config)
           self.items = []
   ```

   构造即注册、卸载即注销，并自动把 `NotesService` 绑定为该名字的契约类。
5. **配置字段先改 snake_case**（插件自己的 `Config` 模型与 row `config` 一起改），见 §3 第 6 项。
6. **不要把 `deeppyro.core.wire` / `session.v4` 模型当稳定 API**：M1 之后不再新增 `core.wire` import，
   会话相关一律用 `deeppyro.core.messages` 与 `deeppyro.session.schema`。

## 3. 逐项对照表

| 场景 | v1/v2 写法（当前可用） | v3 目标 | 现在可改？ |
| --- | --- | --- | --- |
| 插件基类 | `class P(Plugin)` | 不变 | ✅ |
| 版本声明 | `api_version = 2` | `api_version = 3`（M3 落地后） | ⏳ TODO(api-v3) |
| 挂载入口 | `def setup(self)` 或 v1 `setup(self, ctx, config)` | 仅 `setup(self)` | ✅ 现在改 |
| 依赖声明 | `inject = ("tools", ContractClass)` | `inject` 使用契约类；字符串项去留未定 | ⚠️ 尽量契约类 |
| 取依赖（必选） | `ctx.service("tools")` / `ctx.require(ToolsPlugin)` | `ctx.require(ToolsPlugin)`；字符串查找移除 | ✅ 现在改 |
| 取依赖（可选） | `ctx.get("tools")` / `ctx.get(ToolsPlugin)` | `ctx.get(ToolsPlugin)` | ✅ 现在改 |
| 提供服务 | `ctx.provide("notes", value)` 或 `class Notes(Service)` | `Service` 子类 + `provide` | ✅ 推荐 Service |
| 配置热更新 | `volatile` + `on_config(config)` | 不变（ADR 未要求改） | ✅ 保持 |
| 清理 | `async def stop(self)` | 不变 | ✅ 保持 |
| 插件配置字段 | camelCase 或任意 pydantic 字段名 | snake_case | ✅ 现在改字段名 |
| 能力声明 | 无（只有 row/inject/row_states） | `CapabilityManifest` | ❌ TODO(api-v3) |
| 安装方式 | `plugin.toml` 包 / `plugins.toml` / bundle 路径 | entry points（ADR 提及，格式未定） | ❌ TODO(api-v3) |
| bundle 版本 | `api_version = 2`（默认取 API_VERSION） | `api_version = 3` | ❌ TODO(api-v3) |

关于 bundle：`bundle.api_version` 当前默认取 `API_VERSION`，且大于 `API_VERSION` 直接 `CompositionError`。
因此 v3 落地时，**所有显式写了 `api_version = 2` 的 bundle 必须同步改 3**；M3 是否提供自动重写/是否给弃用期
尚未定稿 → `TODO(api-v3)`。

## 4. 迁移步骤（外部插件作者）

1. **盘点入口**：列出 `setup` 签名、`inject`、`ctx.service/get` 调用、`ctx.provide` 调用、`Config` 字段名。
2. **改挂载入口**：`setup(self)`；需要异步初始化就写 `async def setup(self)`（v2 起支持）。
3. **改依赖**：能换成契约类的全部换；找不到契约类的服务名，先记录在插件 README，等 M3 的 service typing 方案。
4. **改服务提供**：字符串 `ctx.provide` → `Service` 子类（保留 `name` 兼容期）。
5. **改配置**：`Config` 字段与 row `config` 键改 snake_case；同步配置文件与文档。
6. **去掉 v4 假设**：插件若读会话日志，改为 native schema 1 的 `session/schema.py` + `session/fold.py`；
   不得依赖 `surfaceOp`、`sourceEventSeqs`、`ignorable`、62 种 v4 记录类型等已删除机制。
7. **等 M3 落地后**补 `api_version = 3`、`CapabilityManifest`、entry point 安装声明（见 §6 TODO 清单）。
8. **回归**：用 `boot(cfg, bundle_paths=[...])` 起临时应用，跑插件自己的集成测试；再跑 `dpyro plugin doctor`。

## 5. 验证与排错

```python
from deeppyro.kernel import boot

app = await boot(config, bundle_paths=["/path/to/your-bundle"])
states = app.row_states()        # state/error/overridden/disabled/critical ...
await app.dispose()
```

- 插件挂载失败但 boot 还在：该 row 非 critical，查 `app.failures` 与 `row_states()` 的 `error`。
- 依赖缺失：`ctx.require(Contract)` 抛 `ServiceNotFoundError`；契约类未绑定抛 `ServiceTypeError`。
- 插件 API 版本不匹配：声明高于 `deeppyro.api.API_VERSION` 会被拒绝（当前即 2）；错误文案以代码为准。
- bundle 解析失败：`CompositionError` 会带 `bundle:<path>` 前缀，检查未知键、`api_version`、`requires` 与 include。

## 6. TODO(api-v3) 汇总（M3 定稿前不得实现/文档化）

- `api_version = 3` 的声明方式与拒绝文案；v1 兼容移除的确切版本。
- `inject` 是否保留字符串项；`ctx.service(str)` / `ctx.get(str)` 是否移除、是否有弃用期。
- `CapabilityManifest`：字段集合、是否必填、与 `inject`/bundle row/`row_states` 的关系、校验失败语义。
- entry points 的组名、发现规则、与现有 `plugin.toml` 安装单元的并存/替换关系。
- `bundle.api_version` 升级策略（硬切？自动改写？弃用期？）。
- service typing：是否要求所有服务都是 `Service` 子类、是否移除 `ctx.provide(str)`。
- 与 M4（eval/memory/multi-agent）的能力对接方式（`risk`/`cost` 元数据消费）。

## 7. 时间线与兼容承诺

- ADR 已锁定：**0.2.0 硬切 v3，不为 v2 提供 shim**；迁移说明就是本文。0.x 版本允许破坏性变更。
- 不希望迁移的插件可以继续锁定 `deeppyro-harness` 0.1.x；0.2.0 不保证 0.1.x 插件可加载。
- 若 M3 未能按计划合流，0.2.0 不应发布（release note 中列为发布阻塞项）；届时以实际 `API_VERSION` 为准，
  本文相应条目顺延。

## 8. 参考

- `docs/adr/0001-three-track-native-agent.md`：D2（零 wire 兼容、v1/v2 硬切 v3）、D4（契约归属）、D5（M3 门禁）。
- `src/deeppyro/api/plugin.py` / `context.py` / `services.py` / `types.py`：v2 事实来源。
- `docs/spec/plugin-core.md`：v2 契约与组合语义规格。
- `docs/plugins/README.md`：插件作者指南（模板、扩展点、bundle 示例）。
- `docs/release/0.2.0.md`：版本级 release note（破坏性变更、门禁、已知限制）。
