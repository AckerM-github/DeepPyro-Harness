# 插件设置规格

本文件详细描述 DeepPyro 的插件设置（SettingsStore / SettingsPlugin）语义：如何从 composition 中读取生效配置、如何用 revision + CAS 安全写入用户清单、secret 掩码、pydantic 校验、原子写与已知限制。PluginManager、bundle 生命周期与 agent 工具的总览见 @docs/spec/plugins.md；插件/清单格式见 @docs/spec/plugin-core.md 与 @docs/spec/INTERFACES.md 第 11 节。

## 1. 数据流

插件配置的生效值来自 kernel 的 composition：core bundle 的 row config 是最底层，用户 bundle 与用户清单（~/.dpro/plugins.toml 的 [[plugins]]）依次 patch。对同一 row id，config 是整体替换而不是深合并（kernel composition 语义）。SettingsStore 读取「该插件当前生效的完整配置」，把调用方的 patch 深合并进去，再把合并后的完整配置作为一个 [[plugins]] 行写回用户清单。因此：

- 写入方只需提供要改的键（patch），不必知道 bundle 默认值。
- 写入后 manifest 行保存完整配置；以后 bundle 默认值再变化时，该插件仍使用 manifest 里的完整值（manifest 是最后一个 patch 层）。
- 默认改动不会热更新到运行中的 Service，下一次 boot 才生效；声明在插件类 volatile 里的字段可以由 SettingsPlugin 在运行中 apply（见第 10 节）。

## 2. 公开 API

    SettingsStore(
        *,
        manifest_path: str | Path | None = None,
        plugins_dir: str | Path | None = None,
        bundle_paths: Sequence[str | Path] = (),
    )

manifest_path 缺省为 deeppyro.paths.plugins_manifest_path()（~/.dpro/plugins.toml）；plugins_dir 缺省为 deeppyro.paths.plugins_dir()，显式给 manifest_path 时缺省为其父目录的 plugins/；bundle_paths 追加到 composition（用于测试或显式 bundle）。

属性：manifest_path、plugins_dir、revision。

方法：

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| describe(plugin_id) | dict | schema、来源、掩码后的生效值、revision 与 apply 信息；找不到插件抛 PluginNotFoundError |
| read(plugin_id) | dict | describe 的读取子集 |
| write(plugin_id, patch, expected_revision) | dict | 校验 + CAS + 原子写入；冲突抛 SettingsConflict，校验失败抛 PluginConfigError；返回字段含 volatile_fields/volatile_changed |
| effective_config(plugin_id) | dict | 未掩码的完整生效配置，仅供进程内 live apply 等内部调用，不得进入 agent 工具输出 |

SettingsPlugin（@src/deeppyro/builtin/settings.py）是 Service，provide = settings，plugin_id = settings，version = 0.1.0，构造时创建 SettingsStore()；属性 revision 与方法 describe/read/write 委托 store。它不注入其它服务，方法调用时按需读取磁盘与 composition；write 在 volatile 变更时通过 ctx.get("kernel") 调用 kernel 服务的 apply_plugin_config（见第 10 节）。

## 3. revision 与 CAS

revision = sha256(用户清单文件的原始字节).hexdigest() 的前 16 个十六进制字符。清单不存在时按空字节计算（e3b0c44298fc1c14），因此空清单与首次写入前 revision 稳定一致。

写入协议：
1. 调用 describe/read 得到当前 revision 与掩码值。
2. 修改需要的字段，把 revision 原样传给 write 的 expected_revision。
3. write 先比较 expected_revision 与当前 revision；不一致立即抛 SettingsConflict，不读取插件、不写盘。
4. 写入成功后返回新 revision，供下一次写入使用。

任何对清单的成功写入（包括 PluginManager.enable/disable、BundleStore.set_enabled）都会改变 revision，因为它们都会重写同一文件；调用方遇到 SettingsConflict 应重新 read 后重试。这里没有乐观重试或自动合并，CAS 语义是显式的。

## 4. 生效配置的定位

write/describe/read 每次调用都重新构造 default composition（core bundle → 显式 bundle_paths → 自动发现的 ~/.dpro/bundles → 用户清单），因此看到的是磁盘当前状态。

- 命中 PluginRow 时：当前值 = row.config or {}，target = row.plugin，origin = row.origin，config_source 根据 origin 前缀归类：manifest: 为 manifest，bundle: 为 bundle，否则 default（core 行 origin 为 bundle:core）。
- 未命中 composition 时：回退到 PluginManager 解析外部来源（目录 / entry point / 清单）的 target；能找到 target 时仍可用 schema 校验并写入新 manifest 行，但没有生效值（values 为空）。
- 清单解析失败：在读取 composition 之前先严格读取清单，失败抛 PluginError（cannot read plugin manifest ...），避免把文件损坏误报成未知插件。

describe 字段：plugin_id、target、origin、config_source、critical、disabled、schema、values（掩码）、revision、apply（恒为 next-boot）、volatile_fields（插件类 volatile ClassVar 声明的可热更新字段名，排序去重）、error（resolve_row 失败文本）。read 字段：plugin_id、target、schema、values、revision、apply、volatile_fields、error。

## 5. schema

若插件类声明了 Config（必须是 pydantic BaseModel 子类），schema = Config.model_json_schema()；否则 schema = None。schema 只描述结构，不包含 secret 的实际值。

契约见 @src/deeppyro/api/plugin.py：插件类属性 Config，kernel 在 boot 时用 model_validate(row.config or {}) 构造插件实例的 config。SettingsStore 用同一个类做离线校验，因此设置写入与 boot 校验结果一致。

## 6. 掩码规则

字段名（大小写不敏感）包含 key、token、secret、password 中任意子串即视为 secret：

- 递归处理 dict 与其嵌套值，list/tuple 递归处理元素。
- 值为 None 时保持 None，其它值替换为 MASKED_VALUE（***）。
- read/describe/write 的返回值都只包含掩码后的值。

写回时 secret 的处理：patch 中值为 *** 的 secret 字段会被丢弃（_strip_masked），因此调用方可以把 read 的结果整体作为 patch 回传而不会覆盖真实 secret。如果 secret 在 patch 中缺失，同样保持原值。要真正修改 secret，必须显式传新明文。

启发式的已知偏差：maxTokens 因含 token 被掩码，属于可接受的误判；credential、privateKey 这种不含上述子串的字段不会被掩码。掩码只影响展示与回传，不影响落盘值，也没有加密存储。

## 7. 写入算法

write(plugin_id, patch, expected_revision)：
1. patch 必须是 Mapping（否则 PluginError：settings patch must be a JSON object）。
2. revision 不匹配抛 SettingsConflict，消息包含 expected 与 current。
3. 定位 PluginRow / target（见第 4 节）；当前值 current = row.config or {}。
4. merged = deep_merge(current, strip_masked(patch))。深合并规则：两边都是 dict 时递归，其它情况用 patch 值替换；list 整体替换。
5. 若有 Config，model_validate(merged)；失败抛 PluginConfigError。
6. 读取用户清单，取该 id 的最后一条 [[plugins]]（kernel 合并语义是最后一条生效）；存在则 replace(config=merged)，不存在则追加 PluginSpec(id=plugin_id, name=target, config=merged)。
7. save_user_manifest 原子写回。返回 plugin_id、新 revision、values（掩码）、changed（顶层键中 old != new 的键排序）、volatile_fields（声明值）、volatile_changed（changed 中属于 volatile 的键）、restart_required=True、apply=next-boot。这是 SettingsStore 层面的结果；SettingsPlugin 会按 kernel 能力改写 restart_required 与 apply，见第 10 节。

写入示例（清单片段）：

    [[plugins]]
    id = "llm"
    config = { defaultModel = "local-model", maxTokens = 8192 }

    [[plugins]]
    id = "demo-plugin"
    name = "tests.builtin.demo_plugins:DemoPlugin"
    config = { apiKey = "hunter2", model = "local", maxTokens = 4096, temperature = 0.7 }

注意最后一条同 id 行才生效；SettingsStore 总是改写最后一条。

## 8. 校验与错误

校验：仅当插件类有 pydantic Config 时执行 model_validate。错误信息只包含 loc 与 msg，最多 8 条，不回显输入值，避免把 secret 写进日志或工具输出。

| 错误 | 触发条件 |
| --- | --- |
| PluginNotFoundError | 插件既不在 composition 中，也无法通过 PluginManager 解析 |
| SettingsConflict | expected_revision 与当前 revision 不一致（SettingsConflict 同时是 ValueError） |
| PluginConfigError | pydantic 校验失败 |
| PluginError | patch 不是 Mapping、无 target 可写、清单读写失败等 |
| ManifestWriteError | 清单序列化或原子写失败（PluginError 子类） |

## 9. 原子写与清单格式

写入统一走 @src/deeppyro/plugins/manifest_store.py 的 save_user_manifest：

- 同目录创建命名临时文件（前缀为清单文件名，后缀 .tmp），写入后 flush + os.fsync，再 os.replace 覆盖目标；任何失败都会清理临时文件。
- 内容为空时删除清单文件而不是留下空文件。
- 序列化只输出 [[plugins]] 表，字段顺序 id、name、disabled、config、before、after，无值字段省略；config 用 TOML 内联表；字符串用 JSON 转义。
- 因此写入会丢失清单里的注释、未知顶层键与原始排版；只保留 [[plugins]] 的语义内容。
- 没有跨进程锁：假定同一时间只有一个进程写用户清单。

## 10. 生效时机：volatile 与 live apply

SettingsStore 只负责磁盘上的配置 patch。SettingsPlugin 把结果暴露为 cordis 服务 settings，agent 工具 settings_read/settings_write 从 ToolContext.services 里取它。默认情况下 boot 时 kernel 按 composition 重新读取 manifest 并 model_validate，新配置在启动完成后才进入插件实例，因此 store 层 describe 的 apply 恒为 next-boot。

插件类可以声明可热更新字段：@src/deeppyro/api/plugin.py 的 Plugin 有 ClassVar volatile: tuple[str, ...]（默认空）。describe/read 的 volatile_fields 就是这个声明（排序、去重），不是「生效配置的顶层键」。SettingsPlugin.write 的流程：

1. 委托 SettingsStore.write 完成 CAS、深合并、pydantic 校验与原子写盘；store 的 restart_required 仍为 True、apply 仍为 next-boot。
2. 若本次 changed 里有插件类声明的 volatile 字段，用 SettingsStore.effective_config(plugin_id) 取未掩码的完整配置，再通过 ctx.get("kernel") 取 kernel 服务（KernelRuntime，boot 时默认加载），调用 apply_plugin_config(plugin_id, config) -> bool。
3. kernel 侧只应用 volatile 字段：合并、pydantic 校验、更新插件实例的 config 与 ctx.config，可选调用插件的 on_config 回调；非 volatile 字段变化、未知或未挂载插件、校验失败都返回 False 并标记 restart-required。
4. apply 返回 True 时 applied_live=True；若 changed 全部是 volatile，则 restart_required=False、apply=live；若仍有非 volatile 变更，则 restart_required=True、apply=next-boot。kernel 服务缺失、返回 False 或抛异常时回退 applied_live=False、restart_required=True，配置已经落盘且异常不外抛。

SettingsPlugin.write 的返回字段：plugin_id、revision、values（掩码）、changed、volatile_fields、volatile_changed、restart_required、apply、applied_live。settings_write 工具把这些字段原样序列化给 agent；effective_config 是内部方法，绝不能进入工具输出，因为它的值未掩码。

## 11. 已知限制

- 热更新仅限插件类声明在 volatile 里的字段，且需要 kernel 服务可用；未声明 volatile、非 volatile 变更、kernel 缺失或拒绝时仍只在下一次 boot 生效。不提供回滚、历史或 diff（changed 只是本次顶层键变化）。
- 只有 pydantic Config 才有 schema/结构校验；无 Config 的插件可写入任意键，boot 时也不会被校验。
- secret 判定是字段名启发式，存在误判/漏判；没有加密、密钥管理或审计。
- 写入的是完整配置而非最小 patch；bundle 默认值后续变化不会自动进入已写过的 manifest 行。
- composition 包含的 extra_plugins 无法被 describe 定位出生效值；只有 PluginManager 能解析 target 时才能写入。
- 清单重写丢失注释与未知键；没有文件锁。
- 设置写入不会触发 plugin 重新加载或 boot 校验；错误配置要到下次 boot 才可能暴露（写入时尽量通过 pydantic 提前拦截）。

## 12. 测试

- @tests/plugins/test_settings_store.py：schema/masked/critical/disabled/volatile_fields 声明、volatile_changed、effective_config、revision、read、write 深合并与 secret 保留、CAS 冲突、pydantic 校验失败不改文件、未知插件、掩码回写、目录插件首次写入追加条目、llm 不写清单、disabled 行、保留其它行、revision 变化、mask_secrets 递归、无 Config、patch 非 Mapping、坏清单。
- @tests/builtin/test_settings.py：boot kernel 后 describe/write/read 端到端、SettingsConflict、PluginConfigError、secret 保留与清单 round-trip、volatile live apply（FakeKernel 成功/失败/拒绝、混合变更、真实 kernel 下断言 app.mount_record(...).instance.config）。

命令：@uv run pytest tests/plugins tests/builtin -o addopts= -q。
