# 配置迁移：camelCase → snake_case（0.2.0–0.3.0）

0.2.0 起 DeepPyro 的配置校验**始终严格**：配置文件与直接构造配置模型（`Config.model_validate`、
`load_config`）使用同一套规则。0.1 的兼容读取（camelCase alias）已删除，旧写法会直接失败并指出
替代键，而不是静默忽略。

0.3.0 起，命令行启动会在读取配置前自动升级旧文件（`migrate_legacy_config`）：重命名 schema 字段、
把 `[agent]` 的权限键搬到 `[permission]` / `[approval]`，并写一份 `.bak` 备份。`load_config` 本身
仍然只接受 snake_case；自动迁移只发生在 CLI 入口，库调用方需要显式调用迁移函数。

## 发生了什么

| 变化 | 行为 |
| --- | --- |
| 7 个 v4 camelCase 别名移除 | 遇到即抛 `ConfigError`，信息包含完整路径与 snake_case 替代键 |
| schema 表内未知键 | 抛 `ConfigError`（`unknown configuration key ...`） |
| `extra="allow"` 收窄 | 只保留两个开放边界，见下文「例外」 |
| `strict_config` / `strictConfig` 开关移除 | 校验本就始终严格；该键会报错提示删除 |
| `populate_by_name` 移除 | 构造参数也只接受 snake_case |
| `lint_config(document)` | 纯报告函数，返回问题字符串列表；不告警、不抛错、不读环境变量 |

错误示例（`load_config` / `Config.model_validate` 均会产生）：

```text
ConfigError: configuration key 'agent.maxTokens' is not supported; use 'agent.max_tokens'
ConfigError: unknown configuration key 'agent.timeoutMs' for AgentConfig
ConfigError: configuration key 'strict_config' is no longer supported:
  configuration validation is always strict as of 0.2.0; remove it
ConfigError: configuration key 'providers.local.models[0].inputModalities' is not supported;
  use 'providers.local.models[0].input_modalities'
```

一个文件有多个问题时，`load_config` 会一次列出：

```text
ConfigError: invalid configuration: configuration key 'agent.maxTokens' is not supported; use
'agent.max_tokens'; configuration key 'providers.local.baseURL' is not supported; use
'providers.local.base_url'
```

## 映射表

### 配置模型字段

| 0.1 camelCase | 0.2.0+ canonical | 位置 |
| --- | --- | --- |
| `contextWindow` | `context_window` | `[providers.X.models[*]]`、`ModelProfile` |
| `baseURL` | `base_url` | `[providers.X]` |
| `apiKeyEnv` | `api_key_env` | `[providers.X]` |
| `apiKey` | `api_key` | `[providers.X]` |
| `maxTokens` | `max_tokens` | `[agent]`、`[providers.X]`、`[providers.X.models[*]]` |
| `maxSteps` | `max_steps` | `[agent]` |
| `sessionDir` | 已删除 | 顶层；改用 `DEEPPYRO_SESSION_DIR` |
| `permissionPreset` | `[permission] defaultPreset` | `[agent]` → 独立表 |
| `approvalPolicy` | `[approval] policy` | `[agent]` → 独立表 |

### adapter 选项（provider/catalog 读取的 model_extra）

`[providers.X]` 允许 `max_tokens`、`reasoning_effort`；`[providers.X.models[*]]` 额外允许
`input_modalities`。camelCase 拼写不再被接受：

| 0.1 camelCase | 0.2.0+ snake_case |
| --- | --- |
| `maxTokens` / `maxOutputTokens` / `maxCompletionTokens` | `max_tokens` |
| `reasoningEffort` | `reasoning_effort` |
| `inputModalities` | `input_modalities` |

### 已删除的开关

`strict_config = true`（或 `strictConfig`）删除即可；配置校验始终严格。`DEEPPYRO_STRICT_CONFIG`
环境变量仍存在，但只影响 **preset model 校验**（默认 `ConfigWarning`，置 1 时 `ConfigError`）。

## 迁移前 / 后

迁移前（0.2 及更早，0.3 启动时自动迁移）：

```toml
[agent]
provider = "deepseek"
model = "deepseek-chat"
maxTokens = 8192
maxSteps = 20
permissionPreset = "workspace-write"
approvalPolicy = "ask"

[providers.deepseek]
baseURL = "https://api.deepseek.com/v1"
apiKeyEnv = "DEEPSEEK_API_KEY"
models = [{ id = "deepseek-chat", contextWindow = 65536, maxTokens = 8192 }]
```

迁移后（0.3.0）：

```toml
[agent]
provider = "deepseek"
model = "deepseek-chat"
max_tokens = 8192
max_steps = 20

[providers.deepseek]
api = "openai-completions"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
models = [{ id = "deepseek-chat", context_window = 65536, max_tokens = 8192 }]

[permission]
defaultPreset = "workspace-write"

[approval]
policy = "ask"
```

## 例外：仍然是开放边界的部分

1. **顶层插件 section** 原样保留，插件自己校验自己的选项名（camelCase 也可以）。例如：
   `[sandbox]` 的 `additionalWritableRoots`、`[retry]` 的 `maxRetries`/`initialDelayMs`、
   `[approval]` 的 `hookMode`/`hookTools`、`[title]` 的 `maxChars`、`[[mcp.servers]]` 的
   `serverName`/`toolCallTimeoutMs`、`[permission]` 的 `defaultPreset`，以及任意自定义插件 table。
   这些键不会出现在配置文件 schema 错误里。
2. **adapter 选项**（见上表）以 snake_case 存入 `model_extra`，由 provider/catalog 层读取。
3. **插件自有配置模型**（row `config`、`[attachments]`、`[ptc]` 等）不属于本迁移：它们由各自插件定义，
   可以继续接受 camelCase 别名。

## 迁移步骤

1. 直接运行 `uv run dpyro`：CLI 入口会调用 `migrate_legacy_config()`，把旧配置原地升级并写
   `<config>.bak`（已有备份时用 `.bak.1`、`.bak.2`……）。成功后新增一行 `config: migrated ...` 提示。
2. 如果文件里有无法映射的未知键，迁移会在写盘前报 `ConfigError`，原文件保持不变；按报错逐个修，
   或对照映射表手工改写。
3. 库调用方不会被隐式改写：显式调用 `migrate_legacy_config(path)`，或直接按新 schema 构造配置。
   需要报告而不是失败时用 `lint_config(tomllib.loads(text))`。
4. 删除 `strict_config` 键；确认没有脚本依赖 `DEEPPYRO_STRICT_CONFIG` 改变配置加载行为。
5. 插件 section 不动；以后新增配置一律 snake_case。

## 参考

- `src/deeppyro/config.py`：`_REMOVED_ALIASES`、`_ADAPTER_ALIASES`、`migrate_legacy_config`、`lint_config`、`load_config`
- `tests/api/test_config_lint.py`：错误文本与严格性回归测试
- `tests/api/test_config_migration.py`：旧配置升级、幂等与 `.bak` 回归测试
- `tests/test_config_v3.py`：snake_case canonical、camelCase 拒绝

