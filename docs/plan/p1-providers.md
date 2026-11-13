# P1 调研：模型目录（listModels）与 Anthropic provider

> 状态：调研/设计文档（P1 实现前）。作者：llm-dev（task-34）。
> 范围：只做设计与拆分建议，不改任何代码。DSH 结论来自 D:\dshworkspace\_dsh_src\ 下已解包的编译产物（只有 lib/*.js 与 README.md，没有 src/*.ts），行号指 lib/index.js；README 行号指对应包 README.md。
> 目标仓库：D:\code\deeppyro（DeepPyro Harness，DSH 的纯 Python 重写），现有 llm 模块见 docs/spec/llm.md。

## 0. 一句话结论

- **(A) listModels 不是「再多一个 HTTP 调用」**，而是一套分层契约：listModels 只提供「可选择的目录条目」{provider,id,name,description?,inputModalities?,badge?}；上下文窗口、输出上限、reasoning 档位由 resolveModelInfo 单独提供；运行时探测（discovery）另有一套 registerModelDiscovery/discoverModels 与凭据作用域缓存。DSH 里它是**程序 API + 事件**（llm/models-updated），没有 TUI/CLI 列表命令。
- **(B) DSH 没有独立 Anthropic 适配器包**；Anthropic Messages 的一等实现就是 dsh-llm-deepseek（DeepSeek 的 Anthropic 兼容端点），dsh-llm-pi-ai 通过 anthropic-messages 协议支持第三方 Anthropic 目录。Python 版应以 dsh-llm-deepseek 的 serialize/translate/error 语义为蓝本，而不是以 pi-ai 的第三方目录为蓝本。
- 最大风险不在协议解析，而在 **thinking signature 的跨轮回放**：core 的 Finish 只有 kind/failure，assistant/message.message.source 由 agent/session.py 固定写 {kind,provider,model}，所以 Anthropic 的带签名 thinking blocks 当前**无处可存**。P1 建议先默认 reasoning_effort=off（不发 thinking、历史 reasoning 块不转 thinking），把「开启 thinking 的多轮工具调用」列为需要 core 扩展的后续项。

---

## 1. Feature A：listModels（模型目录 / 模型发现）

### 1.1 DSH 的真实位置与语义

#### 1.1.1 运行时 API 与事件

| 位置 | 内容 |
| --- | --- |
| D:\dshworkspace\_dsh_src\dsh-llm\lib\index.js:2086-2106 | LlmRuntime.listModels(provider)：registration(provider) → adapter.listModels(provider) → 逐条校验后返回。 |
| 同文件:1721-1723 | LlmAdapter.listModels(_provider) 基类默认 Promise.resolve([])（「没有 GUI 模型」）。 |
| 同文件:1908 | listProviders() 按注册顺序返回 {...provider} 路由。 |
| 同文件:608-693 | resolveModelInfo(provider, model, signal) → normalizeModelInfo：校验 provider/id 匹配、name 非空、context.contextWindow 正整数（否则 INVALID_MODEL_CONTEXT）、systemPromptUpdate 只允许 'in-history'（缺失=只读开头 system，否则 INVALID_MODEL_INFO）、toolUpdate ∈ {'in-history','addition-only'}、defaultMaxTokens 正整数（INVALID_MODEL_MAX_TOKENS）、reasoning.efforts 每项 id/name 非空且不重复、defaultEffort 必须在列（INVALID_MODEL_REASONING）。 |
| 同文件:446-499 | registerModelDiscovery(settingsNs, discover)（重复 DUPLICATE_DISCOVERY，空 ns INVALID_DISCOVERY）；discoverModels(settingsNs, request, signal)：无注册 NO_DISCOVERY，provider/baseURL 皆空 INVALID_DISCOVERY，返回值去重并只保留 {id(必填非空),name?,contextWindow?,maxTokens?,inputModalities?}；远端拒绝包成 RemoteError('llm/model-discovery-rejected', ...)。 |
| D:\dshworkspace\_dsh_src\dsh-llm\lib\typert.host.js:235-236 | 远程 API：async listModels(provider: string): Promise<LlmModelInfo[]>。 |
| 同文件:706,722-724 | 事件 'llm/models-updated'(provider)：「A provider's advertised models or display metadata changed; consumers reload listModels()」。 |
| D:\dshworkspace\_dsh_src\dsh-llm\README.md:28 | listModels 是 catalog-driven GUI 的可选模型来源；core 解析/流式仍接受未列出 id；GUI 要求 catalog 成员资格才能选择/提交；面向 GUI 的 adapter **必须**实现 listModels；条目可带 badge（文本可单字面量或按小写 language id 翻译，必须含 en；tone ∈ neutral/info/success/warning），resolveModelInfo 不含 badge。 |
| 同文件:59 | ctx.llm.listProviders() 按注册顺序。 |
| 同文件:88-90 | LlmConfigurableProvider 的 error/defaultBaseURL/nativeAuth/baseURLEnv；reasoning effort 在 provider I/O 前按精确模型校验；adapter 配置的输出上限在请求省略时物化。 |
| 同文件:95 | 稳定失败码：NO_ADAPTER、MISSING_CREDENTIAL、AUTH、RATE_LIMIT、CONTEXT_WINDOW_EXCEEDED、QUOTA、ACCOUNT_QUOTA、INVALID_CREDENTIAL；消费者按 code 路由不看 message。 |

**结论**：DSH 没有 TUI /models 命令，也没有 CLI 子命令。listModels 的消费者只有两处：GUI（经 typert 远程调用，typert.host.js:235）和 dsh-tool-subagent\lib\index.js:199（(await llm.listModels(provider.id)).filter(route.model===model.id) 枚举子代理可选模型）。

#### 1.1.2 官方 DeepSeek adapter 的目录实现（网络探测 + 缓存 + 合并）

文件：D:\dshworkspace\_dsh_src\dsh-llm-deepseek\lib\index.js

| 位置 | 语义 |
| --- | --- |
| :19-31 | DEFAULT_MODELS=[{id:'deepseek-flash',name:'DeepSeek-V4.1-Flash',contextWindow:1048576,inputModalities:['text','image'],systemPromptUpdate:'in-history',toolUpdate:'addition-only'},{id:'deepseek-v4-pro',name:'DeepSeek-V4-Pro',description:'Stronger agentic coding...',contextWindow:1048576}]。 |
| :17,36-56 | OFFICIAL_CONTEXT_WINDOW=1048576；DEFAULT_CONTEXT_WINDOW=1e6；DEFAULT_MAX_TOKENS=256000；DEFAULT_STREAM_IDLE_TIMEOUT_MS=300000；图片 offload 常量（20MiB/64MiB/10MiB/20）等。 |
| :300-318, :360 | catalog schema：id 必填；name/description/contextWindow(>=1)/maxTokens(>=1)/imagePixelBudget/imageMaxBytes/systemPromptUpdate('in-history')/toolUpdate(in-history 或 addition-only)/inputModalities（默认 ['text']，至少 1 个）；resolveModels 逐项校验重复 id、空 name、正整数、图片限制只对 image 模型。 |
| :525 | modelInfo()：配置命中 → catalogModelInfo（provider,id,name:name??id,description?,inputModalities:model.inputModalities??['text'],badge?）+ context.contextWindow(配置 ?? 1e6) + defaultMaxTokens(配置 ?? connection.maxTokens) + systemPromptUpdate/toolUpdate + reasoning{efforts:off/low/high/max, defaultEffort}。 |
| :2117 | DeepSeekAdapter.listModels(provider) → this.dependencies.discoverModels?.(provider) ?? []。 |
| :2469 | requestModelListing()：GET、redirect:'error'、accept: application/json、TIMEOUT(默认 5000ms)/ABORTED/TRANSPORT/HTTP_<status>/INVALID_RESPONSE。 |
| :2433 | parseModelListing()：body 必须是 {data:[...]}；逐条取 id（非空 string，去重）、name、client_requirements[product].min_version（不满足 → {kind:'upgrade-required'}）、api_capabilities.anthropic_messages（缺失 → unsupported）、output_modalities 必须含 text、input_modalities ∈ {text,image}、context_window/max_output_tokens 正整数、system_prompt_update、tool_update；badge 来自 client_metadata[product].badge（文本 1-12 grapheme、单行、无控制/双向字符；tone 默认 neutral）。 |
| :2516-2663 | ModelListingCache：scope = endpoint + sha256(credential)；首次 view() 等待加载，之后后台刷新（modelsMaxAgeMs 默认 600000）；失败**不覆盖** listing（last-known-good），只置 failing + warn；deepEqualJson 有变化才 notify。 |
| :2753-2755 | listing 默认 modelsTimeoutMs=5000、modelsMaxAgeMs=600000。 |
| :2687 | ctx.emit('llm/models-updated', provider)。 |
| :2832-2899 | resolveListedCatalog()：listing 条目叠在内置 catalog 上；maxTokens=min(routeMaxTokens,max_output_tokens)；leading-only 移除继承的 in-history；text-only 丢弃 image 限制；未出现在 listing 的内置模型保留。 |
| :2906 | refuseListedModel()：upgrade-required → CLIENT_UPGRADE_REQUIRED，unsupported → MODEL_UNSUPPORTED。 |
| README.md:53,119,121 | 省略 models 时公布 deepseek-flash（text+image，1,048,576）与 text-only deepseek-v4-pro；显式 models 完全替换默认；未列出 id 仍可作 text-only 路由；GUI 选择需目录成员，已保存选择在目录项消失后仍可提交；provider 插件拥有目录可用性，transport 提供 requestModelListing/credential-scoped ModelListingCache/resolveListedCatalog/refuseListedModel。 |

#### 1.1.3 pi-ai 的多协议发现实现（可作为「通用 HTTP 目录」蓝本）

文件：D:\dshworkspace\_dsh_src\dsh-llm-pi-ai\lib\index.js

- :1927-1944 listModels(provider)：resolveApiKey 抛 MISSING_CREDENTIAL → 返回 []；否则返回已装目录 models.getModels(provider).map(m => ({provider,id,name,inputModalities:[...m.input]}))。
- :2273-2285 LISTABLE_PROTOCOLS={'anthropic-messages','openai-completions','openai-responses'}（Azure/Codex 不探测）。
- :2286-2289 ANTHROPIC_VERSION='2023-06-01'、ANTHROPIC_MODEL_LIMIT=1000。
- :2297 MAX_RESPONSE_BYTES=4MiB（先查 content-length，再累计拒绝）。
- :2317-2321 listingUrl(baseURL, api)：OpenAI → {base}/models；anthropic-messages → 去末尾 '/'，若以 '/v1' 结尾先去掉，再 {root}/v1/models?limit=1000（**只有 listing URL 规范化 /v1，模型请求仍用原 baseURL**）。
- :2374-2402 readListing(body)：优先 data 数组，否则 models 对象；id = key ?? entry.id（缺失跳过）；name = entry.name ?? display_name ?? displayName ?? id；contextWindow = contextWindow ?? context_window ?? context_length ?? max_input_tokens ?? limit.context；maxTokens = maxOutputTokens ?? max_output_tokens ?? maxTokens ?? max_tokens ?? limit.output ?? top_provider.max_completion_tokens；容量只接受正整数。
- :2412-2416 usableProbeKey 拒绝空/不可放 header 的 key（INVALID_CREDENTIAL）。
- :2427-2437 discoverModels：已装目录优先本地返回；无 baseURL → DISCOVERY_FAILED；无可用协议 → DISCOVERY_UNSUPPORTED。
- :2454-2463 GET 失败 → DISCOVERY_FAILED（abort → ABORTED）；非 2xx → DISCOVERY_FAILED（401/403 提示检查 key）。

#### 1.1.4 与 DSH 的最小差异（P1 建议接受）

1. 不做 badge 的按语言翻译（TUI 是纯文本；badge 只存原始 text 与 tone）。
2. 不做 client_requirements.min_version 版本门槛与 CLIENT_UPGRADE_REQUIRED（无客户端版本概念）。
3. 不做 Files/图片能力联动（P1 无 image offload）。
4. 目录缓存只在内存（TTL 600s，last-known-good），不落盘、不后台刷新；refresh=True 时同步刷新。
5. 不 emit llm/models-updated（Python 侧无远程客户端；TUI 在选择器打开或 /model 时现取）。
6. 只实现两种 listing 解析：OpenAI 兼容 {base}/models 与 Anthropic 兼容 {root}/v1/models?limit=1000；不做 models 对象形式以外的第三方目录协议。
7. **LlmProvider Protocol 不新增方法**（会破坏 tests/integration/fake_provider.py 的 ScriptedProvider）；目录能力用独立 runtime_checkable Protocol。

### 1.2 Python 版设计

#### 1.2.1 新增模块

**src/deeppyro/llm/catalog.py**（新）

```python
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

BadgeTone = Literal["neutral", "info", "success", "warning"]

@dataclass(frozen=True, slots=True)
class ModelBadge:
    text: str | Mapping[str, str]
    tone: BadgeTone = "neutral"

@dataclass(frozen=True, slots=True)
class ModelInfo:
    provider: str
    id: str
    name: str = ""
    description: str = ""
    context_window: int | None = None      # listing 或配置；listModels 契约里可选
    max_tokens: int | None = None
    input_modalities: tuple[str, ...] = ("text",)
    badge: ModelBadge | None = None

@runtime_checkable
class ModelCatalog(Protocol):
    async def list_models(self) -> list[ModelInfo]: ...

def catalog_from_config(provider: str, pc: ProviderConfig) -> list[ModelInfo]:
    """配置里的 models 是目录的下限回退（DSH：未列出的 id 仍可路由，但 GUI 需要目录成员）。"""

def parse_model_listing(provider: str, payload: Any, *, source: str = "") -> list[ModelInfo]:
    """容忍 OpenAI/Anthropic 两种 listing 形状；非法条目丢弃，整体形状非法抛 LlmRequestError(PROTOCOL)。"""
```

实现要点：
- parse_model_listing 复用 pi-ai readListing 的别名表（context_length/max_input_tokens/limit.context、max_output_tokens/max_completion_tokens/limit.output），并对 Anthropic 官方 listing 的 data[] 与 pi-ai 的 models{} 两种容器都接受。
- 条目校验：id 非空 string 否则丢弃；name 回退 id；input_modalities 只保留 text/image，默认 ("text",)；容量只接受正整数；重复 id 后者丢弃。
- badge：仅当 {text,tone} 形状合法时保留；tone 不在四种之一时归一为 neutral（DSH 在 GUI 层校验，这里放宽为归一，文档记录）。

**src/deeppyro/llm/discovery.py**（新，纯函数 + 缓存，无 IO）

```python
DEFAULT_CATALOG_TIMEOUT_S = 5.0
DEFAULT_CATALOG_TTL_S = 600.0
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MODEL_LIMIT = 1000
MAX_LISTING_BYTES = 4 * 1024 * 1024
OPENAI_LISTING_SUFFIX = "/models"

def openai_listing_url(base_url: str) -> str
def anthropic_listing_url(base_url: str) -> str
async def fetch_listing(client: httpx.AsyncClient, url: str, *, headers: Mapping[str, str],
                        provider: str, timeout_s: float = DEFAULT_CATALOG_TIMEOUT_S) -> Any
```

fetch_listing 语义对齐 pi-ai :2454-2463 与 deepseek :2469：follow_redirects=False；非 2xx → LlmRequestError(code=AUTH/…)；超 4MiB → PROTOCOL；JSON 解析失败 → PROTOCOL；传输错误 → TRANSPORT；CancelledError 原样。

#### 1.2.2 改动现有模块

**src/deeppyro/llm/openai_compat.py**：新增方法（不改 stream() 行为）

```python
    async def list_models(self) -> list[ModelInfo]:
        """GET {base}/models；无 API key 时返回 []（DSH pi-ai 语义），失败抛 LlmRequestError。"""
```

- URL：self._base_url.rstrip('/')，若以 /chat/completions 结尾则去掉再 + /models（listing 与推理 base 共享路径前缀，不套用 pi-ai 的 /v1 规范化，因为 OpenAI 兼容本地服务通常就是 http://127.0.0.1:8000/v1）。
- 头：Authorization: Bearer <key>（仅当有 key）、Accept: application/json；复用 _ensure_client，超时用 DEFAULT_CATALOG_TIMEOUT_S（不是 600s 流超时）。
- 404/405 说明服务端不支持 listing → 返回 []（不抛），其余错误抛 LlmRequestError，由 registry 决定回退。

**src/deeppyro/llm/anthropic.py**（新，见 §2.2）：async def list_models()：GET {root}/v1/models?limit=1000，头 x-api-key / anthropic-version，解析同上；无 key → []。

**src/deeppyro/llm/registry.py**：新增

```python
class LlmRegistry:
    async def list_models(self, provider: str, *, refresh: bool = False) -> list[ModelInfo]:
        """目录优先；未知 provider → ConfigError；无目录能力 → catalog_from_config()。"""

    async def model_info(self, provider: str, model_id: str) -> ModelInfo | None:
        """先查目录，再退回配置里的 models 条目。"""
```

- 缓存键 = provider 名；每个 provider 一个 asyncio.Lock 单飞（避免 /model 连按时重复打网络）。
- 失败策略（建议）：若已有缓存 → 返回缓存（last-known-good）并记 last_catalog_error 属性；无缓存 → 返回 catalog_from_config() 并记错误；只有未知 provider 才抛 ConfigError。这样 TUI 启动/选择器永不因网络失败崩溃。
- 新增 list_providers() 返回 [{"name","description"}]（与 builtin/inspect 现有形状一致；DSH 的 LlmProviderInfo 还含 defaultBaseURL/nativeAuth，P1 不做）。

**src/deeppyro/builtin/llm.py**（llm 服务接线点）：新增

```python
    async def list_models(self, provider: str, *, refresh: bool = False) -> list[ModelInfo]:
        """配置 provider → registry.list_models；工厂 provider → 若工厂对象有 list_models 则用，否则空列表。"""

    async def model_info(self, provider: str, model_id: str) -> ModelInfo | None
```

stop() 保持只 registry.aclose()（工厂 provider 的关闭由插件自己负责，add_provider 不接管）。

**src/deeppyro/builtin/inspect.py:415-435**：providers() 每行增加 "models": [{"id","name","contextWindow","inputModalities"}]；保留 name/description 不变（向后兼容）。目录对 inspect 是可选增强：list_models 失败时留空数组，不让 inspect 报错。

**src/deeppyro/tui/runtime.py**：
- 新增 async def list_models(self, provider: str | None = None, *, refresh: bool = False) -> tuple[list[ModelInfo], list[ModelInfo]]：返回（配置内、仅目录）；或更简单 async def model_catalog(self) -> dict[str, list[ModelInfo]]。
- _validate_model（:401-433 内部）放宽为「配置 models ∪ 目录 models」。
- _context_window（:41-49）增加第三级回退：目录条目的 context_window，最后才 DEFAULT_CONTEXT_WINDOW。
- _apply_selection 与 model/selection 记录不变；选择目录外模型时仍写 [agent] provider/model（save_agent_selection 只写两行、不校验 models，已确认 config.py:186-233 可写）。

**src/deeppyro/tui/app.py:774-856（/model）**：
- choices 从 runtime.model_choices() 扩展为 config ∪ catalog（去重，provider/id 显示）；对 discovered-only 条目在描述里标 (not in config)。
- 选择 discovered-only 模型后必须提示：下次非交互启动 headless._resolve_model 只认配置模型 → 若不放宽 headless，就需要用户把模型写进 [providers.X].models。P1 建议同时放宽 headless，并把「持久化到 config.toml」列为决策点（本仓库 save_agent_selection 能原子改写 tomllib，但新增 models 行会破坏注释/格式，建议不自动写）。
- 网络调用在 TUI 里必须是 async（现有命令处理器是 async，可直接 await；不得用 asyncio.run）。

**src/deeppyro/headless.py:108-127、:423-436**：
- _resolve_model 在「配置里没有」时回退 await services['llm'].model_info(provider, model)，用其 context_window；仍取不到才抛 ConfigError。
- 启动路径不因目录网络失败而失败（用 config fallback，不阻塞 boot；P1 只在非交互 headless 对显式 --model 做校验时查询）。

#### 1.2.3 记录/事件/配置/权限

- **wire 记录**：不新增任何 record type。DSH 的 listModels 不写 session 记录，目录是 runtime 状态。model/selection 记录保持不变（tui/runtime.py:562-564）。
- **事件**：P1 不引入 llm/models-updated；若 Lead 希望 TUI 后台刷新，再在 builtin/llm.py 用 cordis ctx.emit("llm/models-updated", provider)（本仓库已有 ctx.emit 通道，见 api/context.py:112-113，但 llm 服务当前没有消费者）。
- **配置**：ProviderConfig.extra="allow" 与 ModelConfig.extra="allow"（config.py:19-41）已可承载 inputModalities/maxTokens/reasoningEffort/systemPromptUpdate/toolUpdate；P1 读取端用 model_extra 容错，**不改 config.py**。目录 TTL/超时先用常量（600s/5s），不暴露配置项。若 Lead 希望 TUI 持久化目录模型，建议后续在 config.py 加 providers.X.models[*].inputModalities 等正式字段（需 Lead 决策）。
- **权限/审批**：目录查询不经过工具系统、不触发审批；只受 resolve_api_key() 结果影响。任何错误消息/日志不得包含 API key 或 listing 原始 body（只放截断摘要）。

### 1.3 测试策略（Feature A）

| 文件 | 用例 |
| --- | --- |
| tests\llm\test_catalog.py | parse_model_listing：OpenAI {data:[...]}、pi-ai {models:{...}}、别名（context_length/limit.context/max_output_tokens）、name 回退 id、重复 id 丢弃、非法容量/模态丢弃、badge 归一、整体形状非法 → PROTOCOL；catalog_from_config：name 回退、inputModalities 默认 text、contextWindow。 |
| tests\llm\test_discovery.py | openai_listing_url 三种 base（含 /v1、含 /chat/completions、带尾斜杠）；anthropic_listing_url（/v1 规范化、?limit=1000）；fetch_listing 的 401/404/网络错误/超大 body/坏 JSON。respx，不打外网。 |
| tests\llm\test_registry.py（扩展） | list_models 缓存命中不重复请求、refresh=True 强制、失败保留 last-known-good、无缓存回退 config、未知 provider ConfigError、model_info。 |
| tests\llm\test_openai_compat.py（扩展） | GET /models respx：Authorization、无 key 返回 []、404→[]、401→AUTH。 |
| tests\llm\test_anthropic.py（见 Feature B） | Anthropic listing URL/头/解析。 |
| tests\builtin\test_llm.py | llm 服务 list_models 对配置 provider/工厂 provider 的分派；inspect.providers() 含 models 行；失败不回退成异常。 |
| tests\tui\test_runtime_models.py | fake LLM 服务返回目录 → model_choices 合并、_validate_model 接受 discovered-only、_context_window 回退；选择后 model/selection 记录。 |
| tests\test_headless.py（若已有，否则 tests\integration） | --model 为 discovered-only 时用目录 contextWindow 启动；目录失败时仍按配置报错。 |

**fixtures**：无需新增 fixtures/ 文件（该目录只读）。可把 2 个 listing 样本内联在测试里；tests\llm\test_fixtures.py 继续复用 fixtures/records/by-type/request_header.jsonl 的 66 个 tools 校验 tool_to_anthropic。

### 1.4 实施拆分建议（Feature A）

| 任务 | 标题 | 依赖 | 写域 | 预估 |
| --- | --- | --- | --- | --- |
| A1 | llm 目录类型与 listing 解析/缓存 | — | src\deeppyro\llm\catalog.py、src\deeppyro\llm\discovery.py、tests\llm\test_catalog.py、tests\llm\test_discovery.py | 新增 ~320 行 + 测试 ~280 行 |
| A2 | provider.list_models + registry/builtin 服务接线 + inspect | A1 | src\deeppyro\llm\openai_compat.py、src\deeppyro\llm\registry.py、src\deeppyro\builtin\llm.py、src\deeppyro\builtin\inspect.py、tests\llm\test_registry.py、tests\builtin\test_llm.py | ~200 行 + 测试 ~180 行 |
| A3 | TUI/headless 选择器接入与 contextWindow 回退 | A2 | src\deeppyro\tui\runtime.py、src\deeppyro\tui\app.py、src\deeppyro\headless.py、tests\tui\test_runtime_models.py、tests\test_headless.py | ~250 行 + 测试 ~200 行 |
| A4 | docs/spec/llm.md 目录章节 + 本计划回填实测 | A1-A3 | docs\spec\llm.md | ~120 行文档 |

A3 建议由 tui-dev 做（写域属于 tui/headless），A1/A2/A4 由 llm-dev 做；A2 与 A3 之间需要一次接口对齐（list_models/model_info 签名与失败语义），建议 A2 完成后 Lead 在 task 里贴最终签名再启动 A3。

---

## 2. Feature B：Anthropic provider（Messages API）

### 2.1 DSH 的真实语义

**没有独立的 anthropic 包**。全仓搜索结论：
- 一等实现：D:\dshworkspace\_dsh_src\dsh-llm-deepseek\lib\index.js（DeepSeek 官方 Anthropic 兼容端点，base 默认 https://api.deepseek.com/anthropic）。
- 第三方 Anthropic：dsh-llm-pi-ai 的 'anthropic-messages' 兼容项（lib\index.js:10 import anthropicMessagesApi；:455 兼容表；:838 注册；:2449-2452 discovery 头；:2273 可探测协议）。
- 搜索型副产物：dsh-web-search-deepseek 也走 https://api.deepseek.com/anthropic/v1 + /messages + anthropic-version: 2023-06-01。

#### 2.1.1 请求（serialize，:1603-1745）

- URL：/v1/messages（baseURL 已以 /v1 结尾则复用）；**所有请求拒绝重定向**（redirect:'error'）。
- 头：anthropic-version: 2023-06-01；API key → x-api-key；账号 token → x-dsh-auth-token（**没有 Bearer**）；带 file id 时 anthropic-beta: files-api-2025-04-14。
- body：{model, stream:true, messages, max_tokens: options.maxTokens ?? model.maxTokens ?? connection.maxTokens(默认 256000), thinking:{type: effort==='off'?'disabled':'enabled'}, effort!=='off' ? output_config:{effort} : {}, system?, temperature?, stop_sequences?, tools:[{name,description,input_schema:tool.parameters, defer_loading?}]}。
- reasoning effort ∈ {'off','low','high','max'}（默认 high；thinking disabled 时只允许 off，否则 UNSUPPORTED_REASONING_EFFORT）。
- 内容块：text 非空才保留；user/tool_result 的 reasoning/tool-call 块丢弃；image → {type:'image',source:{type:'base64',media_type,data}} 或 {type:'file',file_id}，且前面插一条确定性 handle 文本。
- system：普通模式取**最新** system 文本作顶层 system（与 options.system 用 '\n\n' 连接）；模型声明 systemPromptUpdate:'in-history' 时，历史中后续 system 快照作为 role:'system' 消息压在其前一条 user/tool-result turn 之后（flushSystemUpdates 要求 messages.at(-1).role==='user'，否则 UNSUPPORTED_CONTENT）。
- tool 消息 → role:'user' + content:[{type:'tool_result',tool_use_id, content:[...], is_error?}]；相邻同 role 合并 content。
- assistant：text→{type:'text'}（空丢弃）；reasoning→{type:'thinking',thinking, signature?}（仅 replay 有效且同 model）；tool-call→{type:'tool_use',id,name,input:JSON.parse(arguments)（解析失败或非对象 → {})}。
- 配对校验：同一 assistant 内重复 id → INVALID_REQUEST；tool_result 无匹配 call、call 未立即有 result、历史以未解决 tool 结尾 → INVALID_REQUEST。

#### 2.1.2 流式（translate，:1941-2032）

- parseSse @1800：SSE 框 → JSON；解析失败 MALFORMED_RESPONSE；frame.event 与 event.type 不一致 MALFORMED_RESPONSE；event.type==='error' → providerError。
- message_start：message.usage 合并（input_tokens/output_tokens/cache_read_input_tokens/cache_creation_input_tokens）；重复 message_start 出错。
- content_block_start：text → {type:'text'}；thinking → {type:'reasoning'} + replay {type:'reasoning',signature?}；tool_use → {type:'tool-call',id,name,arguments:JSON.stringify(input)}；**其它块类型 UNSUPPORTED_CONTENT**。先发 block-start（{index,blockType}），初始 text 非空立即发 text-delta；tool_use 立刻发一条 tool-call-delta{index,id,name,argumentsDelta:''}。
- content_block_delta：text_delta→text-delta；thinking_delta→reasoning-delta（累积 replay text）；signature_delta→只累积 signature，**不产 chunk**；input_json_delta→tool-call-delta{index,id,argumentsDelta:partial_json}（**原始碎片**，不 JSON.parse）；不匹配 MALFORMED_RESPONSE。
- content_block_stop→block-end {index,block:{...content}}，tool-call 用累积 json 覆盖 arguments。
- message_delta：delta.stop_reason 映射 end_turn|stop_sequence→stop、tool_use→tool-calls、max_tokens→max-tokens（DSH kind；Python core 只有 'length'，映射为 length）、未知 MALFORMED_RESPONSE；event.usage 合并。
- message_stop：要求 reason 存在且所有 block 已 closed；**空内容 + stop → EMPTY_RESPONSE**；非 max-tokens 时每个 tool-call arguments 必须是合法 JSON 对象否则 MALFORMED_RESPONSE；totalTokens = input+output+cacheRead+cacheWrite；**先 yield usage 再 yield finish**，finish 带 replayState:{response:{kind:'deepseek-messages',version:1,model},blocks:[replay...]}。
- 流在 message_stop 前结束 → STREAM_CLOSED（**没有 [DONE] 依赖**，message_stop 是唯一终止标记）。
- generate() @2147 用 idleWatchdog（DEFAULT_STREAM_IDLE_TIMEOUT_MS=300000）包 stream；异常归一化：timeout→TIMEOUT、signal aborted→ABORTED、LlmError 原样、其它→TRANSPORT。

#### 2.1.3 错误分层（providerError，:1769-1791）

| 输入 | code | 可重试 |
| --- | --- | --- |
| status 401/403；type authentication_error/permission_error | AUTH | 否 |
| 配额型；status 402 | QUOTA | 否 |
| status 429；type rate_limit_error | RATE_LIMIT | 是 |
| 文本命中 context window 超限 | CONTEXT_WINDOW_EXCEEDED | 否 |
| status 400/413；type invalid_request_error | INVALID_REQUEST | 否 |
| status>=500；type api_error/overloaded_error | SERVER | 是 |
| 其它有 status | HTTP_<status> | 仅 408/409/425/429/5xx |
| 无 status | SERVER | 是 |
| 传输失败 / 调用方取消 | TRANSPORT / ABORTED | 否 / 否 |
| 流空闲超时 | TIMEOUT(MESSAGES_IDLE) | 是 |

retry-after 头支持秒数与 HTTP-date → providerRetryAfterMs；request-id 头 → ProviderRequestId。

#### 2.1.4 目录发现（Anthropic 官方/DeepSeek）

GET {origin}/models（base 变成 origin 根），parseModelListing @2433：要求 {data:[...]}；api_capabilities.anthropic_messages 缺失 → 该模型 unsupported；input_modalities ∈ {text,image}；context_window/max_output_tokens 正整数；client_metadata[product].badge；client_requirements[product].min_version 版本门槛。缓存/合并/refusal 同 §1.1.2。

#### 2.1.5 与 DSH 的最小差异（P1 建议）

1. **不做 thinking signature 持久化**：core Finish(kind, failure) 与 agent/session.py:1130-1135 的 source={kind,provider,model} 都不携带 replay 元数据 → P1 默认 reasoning_effort='off'，不发 thinking、历史 ReasoningPart 转请求时直接丢弃（thinking disabled 时 Anthropic 不接受 thinking 块）。开启 thinking 的单轮可支持，但**多轮工具调用不可靠**，列为需 core 扩展的后续项（见 §3 风险 R1）。
2. 不做 in-history system 快照（Python 历史只有一个 system 消息位置；LlmRequest.system 顶层发送即可）；ModelConfig 的 systemPromptUpdate 只记录不改变行为。
3. 不做 Files API / image offload / anthropic-beta 文件头；图片仅支持 inline base64（ImagePart 的 data URL 或 {data,mimeType}），裸 attachment id 跳过（与现有 OpenAI provider 的 docs/spec/llm.md §11 一致）。
4. 不做 prompt caching（cache_control）、不做 prompt_cache_key/session affinity。
5. 不做 pause_turn 续跑（罕见；P1 映射 stop 并记 warning；需 Lead 决策是否改为 error）。
6. 不做原生 OAuth/native auth（只 apiKey/apiKeyEnv）。
7. 不做非流式 complete()。
8. 未知 content block 类型：DSH 抛 UNSUPPORTED_CONTENT；P1 抛 PROTOCOL（非重试）并在 docs 列出。

### 2.2 Python 版设计

#### 2.2.1 新增模块

**src/deeppyro/llm/messages_anthropic.py**（新）

```python
def tool_to_anthropic(tool: ToolSchema) -> dict[str, Any]:
    """{name, description, input_schema: tool.parameters}；defer_loading 不做。"""

def build_anthropic_payload(
    req: LlmRequest,
    *,
    default_max_tokens: int,
    reasoning_effort: str = "off",
    model_max_tokens: int | None = None,
    in_history_system: bool = False,
) -> dict[str, Any]:
    """返回可直接 POST 的 body；转换错误抛 LlmRequestError(CODE_BAD_REQUEST, retryable=False)。"""

def convert_anthropic_messages(
    messages: Sequence[Message],
    system: str | None,
    *,
    in_history_system: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """返回 (messages, system)。相邻同 role 合并；tool 结果合入 user；assistant tool_use 配对校验。"""
```

规则表（P1）：

| core Message/Part | Anthropic |
| --- | --- |
| role=system 第一个 + req.system | 顶层 system 字符串（'\n\n' 连接）；后续 system 违反 P1 策略 → 丢弃并记 warning（不做 in-history） |
| role=developer | 当 system 处理（与 OpenAI 转换一致） |
| role=user + TextPart | {role:'user',content:[{type:'text',text}]} |
| role=user + ImagePart(data URL) | {type:'image',source:{type:'base64',media_type,data}} |
| role=user + ImagePart(裸 attachment id) | 跳过该 part；若消息空则丢弃整条 |
| role=assistant + TextPart | {type:'text'}（trim 后空则丢） |
| role=assistant + ReasoningPart | reasoning_effort=='off' → 丢弃；否则 {type:'thinking',thinking}（无 signature，见 R1） |
| role=assistant + ToolCallPart | {type:'tool_use',id,name,input: json.loads(arguments)}；解析失败/非 dict → {} |
| role=tool + ToolResult | 合入相邻 role:'user' 的 content:[{type:'tool_result',tool_use_id, content:[{type:'text',text}], is_error?}] |
| 空 assistant 消息 | 跳过（Anthropic 拒绝空 content） |

配对校验（发出请求前，抛 LlmRequestError(CODE_BAD_REQUEST)）：assistant 内重复 tool_use.id；tool_result 无对应 call；call 未紧跟 result；历史以未解决 tool 结尾。

payload 字段：model、max_tokens（req.max_tokens → model_max_tokens → default_max_tokens；Anthropic 必填）、stream:true、messages、system?、temperature?（透传，thinking 时服务端忽略）、tools?（空列表不加）、thinking={type: disabled|enabled} 与开启时 output_config:{effort}。context_window 超限不预检（由服务端返回 CONTEXT_WINDOW_EXCEEDED，与 DSH 一致）。

**src/deeppyro/llm/anthropic.py**（新）

```python
class AnthropicProvider:
    name: str

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        *,
        model_ctx: int | None = None,
        model_max_tokens: int | None = None,
        reasoning_effort: str = "off",
        timeout_s: float = 600.0,
        idle_timeout_s: float = 300.0,
        retry_policy: RetryPolicy | None = None,
    ) -> None: ...

    @property
    def messages_url(self) -> str: ...   # base 以 /v1 结尾复用，否则 + /v1/messages

    async def stream(self, req: LlmRequest) -> AsyncIterator[StreamEvent]: ...
    async def list_models(self) -> list[ModelInfo]: ...
    async def aclose(self) -> None: ...
```

stream() 与 OpenAICompatProvider 同构，可复用 RetryPolicy 与错误工厂：
1. payload = build_anthropic_payload(...)（转换错误在**任何 HTTP 之前**抛，不消耗重试预算）。
2. async with retry_policy as retry: → first = await retry.run(fetch_first)，fetch_first 新建 attempt 生成器并 anext；空流 → EMPTY_RESPONSE（retryable）。
3. yield first 后 async for 余下事件。
4. _attempt()：client.stream("POST", messages_url, json=payload, headers=...)；status>=400 → 读 body 归一化（§2.1.3 表）；content-type 非 text/event-stream → PROTOCOL。
5. _translate()：iter_sse(resp.aiter_bytes())；逐事件翻译（表见下）；message_stop 终止；无 message_stop 即 EOF → 有内容 Finish(error, STREAM_CLOSED) / 无内容 LlmRequestError(STREAM_CLOSED, retryable=True)。
6. timeout_s 用 httpx 总超时 + read=idle_timeout_s（httpx read timeout 按单次读操作计，正好近似 DSH idleWatchdog）；读超时 → TIMEOUT(MESSAGES_IDLE) retryable。
7. CancelledError 不捕获；已产内容后的任何 LlmRequestError → Finish(kind='error', failure)；httpx.HTTPError → TRANSPORT/TIMEOUT。
8. 结尾统一 Finish（stop|tool-calls|length|content-filter|error|aborted），Usage 必须在 Finish 之前。

事件映射：

| Anthropic 事件 | core 事件 |
| --- | --- |
| message_start usage | 暂存（不立即 yield；DSH 在 finish 前统一 yield） |
| content_block_start(text) 初始 text 非空 | TextDelta(text) |
| content_block_start(thinking) 初始 thinking 非空 | ReasoningDelta(text) |
| content_block_start(tool_use) | ToolCallStart(id, name)；若 input 非空 → ToolCallArgsDelta(id, json.dumps(input)) |
| content_block_delta.text_delta | TextDelta(delta.text) |
| content_block_delta.thinking_delta | ReasoningDelta(delta.thinking) |
| content_block_delta.signature_delta | 无（P1 丢弃） |
| content_block_delta.input_json_delta | ToolCallArgsDelta(id, delta.partial_json)（原样） |
| content_block_stop | 无（tool_use 时记录累积 args 以便 finish 校验） |
| message_delta.delta.stop_reason | 记录，等 message_stop |
| message_delta.usage | 合并 usage |
| message_stop | Usage(...) 然后 Finish(kind) |
| SSE event:error 或 {"type":"error"} | 按 §2.1.3 归一化 code；有内容 → Finish(error)，否则抛（可重试则退避） |

finish_reason 映射：end_turn|stop_sequence → stop；tool_use → tool-calls；max_tokens → length；refusal → content-filter；pause_turn → stop（+warning）；未知 → PROTOCOL 不重试。

Usage 映射（与现有 OpenAI provider 语义对齐：input_tokens 含缓存命中部分）：
input_tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens；cached_input_tokens = cache_read_input_tokens；output_tokens 原样；total_tokens = input_tokens + output_tokens。docs 里注明这与 DSH 的拆分字段不同（DSH 分开存 cacheRead/cacheWrite）。

#### 2.2.2 改动现有模块

**src/deeppyro/llm/errors.py**：新增常量 CODE_QUOTA="QUOTA"、CODE_CONTEXT_WINDOW_EXCEEDED="CONTEXT_WINDOW_EXCEEDED"、CODE_STREAM_CLOSED="STREAM_CLOSED"（CODE_PROTOCOL 保留给格式/SSE 错误）；RETRYABLE_CODES 仍为 {EMPTY_RESPONSE,RATE_LIMIT,SERVER,TIMEOUT,TRANSPORT}（QUOTA/CONTEXT 不在其中；STREAM_CLOSED 的「无内容」分支直接设 retryable=True，与现有 PROTOCOL 分支一致）。

**src/deeppyro/llm/registry.py**：create_provider 变 dispatcher：

```python
_ANTHROPIC_APIS = frozenset({"anthropic-messages", "anthropic", "anthropic_messages"})
if api in _ANTHROPIC_APIS:
    return AnthropicProvider(
        name,
        _anthropic_base_url(pc),
        pc.resolve_api_key(),
        model_ctx=_context_window(pc),
        model_max_tokens=_max_tokens(pc),
        reasoning_effort=_reasoning_effort(pc),
        timeout_s=timeout_s,
    )
```

_anthropic_base_url(pc)：pc.baseURL 若仍是 openai-completions 的默认值 https://api.deepseek.com/v1 则改用 https://api.deepseek.com/anthropic，否则原样（**需 Lead 决策**：这条「默认值嗅探」不优雅；替代方案是要求配置显式写 baseURL，或由 Lead 在 config.py 加 per-api 默认值）。
_max_tokens/_reasoning_effort：读 pc.models[*].model_extra（maxTokens/max_tokens/reasoningEffort/reasoning_effort），模型级优先，缺省 None/'off'。

**src/deeppyro/llm/__init__.py**：re-export AnthropicProvider、ModelInfo/ModelBadge/ModelCatalog、build_anthropic_payload/convert_anthropic_messages/tool_to_anthropic；__all__ 保持排序。

**docs/spec/llm.md**：新增 §13 Anthropic Messages request/stream/error/usage mapping、§14 目录发现、§15 差异与风险（把 §11 的「无 Anthropic」改成指向 §13）。

**src/deeppyro/builtin/llm.py**：无需为 Anthropic 特改（create_provider 已分派）；只需 A2 的 list_models 转发。

#### 2.2.3 记录/事件/配置/权限

- **wire 记录**：不新增 record type。Anthropic 的 assistant/message 仍写现有形状；**replayState 不写入**（P1）。若未来支持 thinking，建议：assistant/message.message.source.replayState = {response:{kind:"anthropic-messages",version:1,model},blocks:[{type:"reasoning",signature}|{type:"text"}]}（wire.Message.source 是 dict，session/derive.py:224-247 用 Message.model_validate(payload) 会保留 extra，可回放）。但写入点在 agent/session.py:1121-1146（loop 写域），且 llm 层要把 signature 交给它 → 需要 core Finish 增加字段或新增 StreamEvent 类型（**需 Lead 决策，P1 非目标**）。
- **配置**：[providers.X] api = "anthropic-messages"、baseURL = "https://api.deepseek.com/anthropic"、apiKeyEnv = "DEEPSEEK_API_KEY"；模型能力字段走 ModelConfig.model_extra（maxTokens/reasoningEffort/inputModalities/systemPromptUpdate/toolUpdate），P1 不改 config.py。DEFAULT_TOML 是否增加 anthropic provider 由 Lead 决定（config.py 属 Lead 写域）。
- **权限/审批**：不触发工具审批；api key 只进请求头；错误消息不含 key。
- **重试兼容**：复用 RetryPolicy，retryable 由 provider 归一化后的 LlmRequestError 决定；429 的 retry-after 用 _parse_retry_after 写入 retry_after（OpenAICompatProvider 已有该 helper，建议提到 llm/retry.py 或 llm/errors.py 共享）。

### 2.3 测试策略（Feature B）

| 文件 | 用例 |
| --- | --- |
| tests\llm\test_messages_anthropic.py | system 顶层拼接；多个 role=system 时的 P1 丢弃策略；developer→system；text 空丢；reasoning off 丢弃 / on 转 thinking；tool_use input 解析与非法→{}；tool_result 合入 user + is_error；相邻同 role 合并；重复 tool_use id / 未配对 result / 未解决 tool 结尾 → BAD_REQUEST；图片 data URL 与裸 id；payload：max_tokens 顺序（req→model→default）、thinking disabled、output_config.effort、temperature、tools.input_schema、空 tools 不加；用 fixtures request_header.jsonl 的 66 tools 跑 tool_to_anthropic。 |
| tests\llm\test_anthropic.py | respx mock SSE（复用 tests\llm\helpers.py，新增 anthropic_sse/anthropic_event 辅助）：纯文本（message_start → content_block_start/text_delta → content_block_stop → message_delta end_turn → message_stop）事件序列 = TextDelta…Usage…Finish(stop)；thinking deltas → ReasoningDelta；tool_use 分片（start+2×input_json_delta → ToolCallStart + 2×ArgsDelta 原样、finish=tool-calls）；max_tokens→length；未知 stop_reason → PROTOCOL 且不重试；空内容 → EMPTY_RESPONSE 重试（成功后一次调用）；message_stop 前断流（有内容→Finish(error, STREAM_CLOSED) 且 call_count=1；无内容→重试耗尽）；event:error overloaded → SERVER 重试；429 + retry-after: 7 → sleeps=[7.0]；401→AUTH 不重试；400 prompt too long→CONTEXT_WINDOW_EXCEEDED 不重试；529→SERVER；usage 缓存字段合并；请求头（x-api-key/anthropic-version/无 Bearer）与 body（stream/max_tokens/thinking）；取消传播（hanging body + task.cancel → CancelledError）；aclose() 后 stream 抛 closed。 |
| tests\llm\test_registry.py（扩展） | api="anthropic-messages" → AnthropicProvider、base URL 处理、model_max_tokens/reasoning_effort 从 model_extra 读取、未知 api 仍 ConfigError。 |
| tests\llm\test_errors.py（新或并入） | 状态码→code→retryable 表；retry-after 秒数与 HTTP-date；failures dict 形状 {message,code}。 |
| tests\llm\test_fixtures.py（扩展） | tool_result/assistant fixture → Anthropic 转换不抛、tool_use 配对成立。 |

无需真实网络；需要一个可选的「真端点冒烟脚本」（不纳入 pytest，或 pytest.mark.skipif(not os.environ.get('DEEPSEEK_API_KEY'))），P1 建议只手工验证 https://api.deepseek.com/anthropic 与本地 llama-server（后者不是 Anthropic 协议，跳过）。

### 2.4 实施拆分建议（Feature B）

| 任务 | 标题 | 依赖 | 写域 | 预估 |
| --- | --- | --- | --- | --- |
| B1 | Anthropic 消息转换 + 错误码/错误归一化 | — | src\deeppyro\llm\messages_anthropic.py、src\deeppyro\llm\errors.py、tests\llm\test_messages_anthropic.py、tests\llm\test_errors.py | ~350 行 + 测试 ~300 行 |
| B2 | AnthropicProvider 流式 + create_provider 分派 + re-export | B1 | src\deeppyro\llm\anthropic.py、src\deeppyro\llm\registry.py、src\deeppyro\llm\__init__.py、tests\llm\test_anthropic.py、tests\llm\test_registry.py | ~500 行 + 测试 ~450 行 |
| B3 | Anthropic/OpenAI 目录发现接线（list_models + B 的 listing） | B2, A1 | src\deeppyro\llm\anthropic.py、src\deeppyro\llm\catalog.py、tests\llm\test_anthropic.py | ~120 行 + 测试 ~120 行 |
| B4 | docs/spec/llm.md Anthropic 章节 + 手工真端点冒烟 | B2, B3 | docs\spec\llm.md | ~150 行文档 |

B1 可与 A1 并行（写域不重叠）；B2 必须等 B1；A2 与 B2 都要改 registry.py → **串行**（建议 A2 先完，或把 registry 的改动集中在一次 task 里，避免写冲突）。

---

## 3. 风险与非目标

### 3.1 风险

- **R1（高）：thinking signature 跨轮回放无处可存**。core Finish 无 replay 字段；agent/session.py:1130-1135 固定 source；wire.Message 虽 extra="allow" 且 session/derive.py:224-247 能保留 source，但 llm 层没有通道把 signature 送到 session 写入。影响：开启 thinking 的多轮工具调用（Anthropic 要求原样回传带 signature 的 thinking 块）会 400。P1 规避：默认 off + 历史 reasoning 丢弃；若 Lead 要 P1 就支持，需先扩展 core Finish（新增可选 replay: dict）或新增 ReplayMetadata StreamEvent —— **需 Lead 决策**。
- **R2（中）：Anthropic 网关只接受 user/assistant 两种 role**。DSH 的 in-history system 快照使用 role:'system' 消息（DeepSeek 端点接受），通用 Anthropic 网关会拒绝。P1 不做 in-history，风险低；若未来做，需按网关探测或改为「合入前一条 user 消息」。
- **R3（中）：配置默认 baseURL 与 api 不匹配**。ProviderConfig.baseURL 默认是 https://api.deepseek.com/v1（OpenAI 形状）；api="anthropic-messages" 但用户漏写 baseURL 会打到错误端点。P1 的「默认值嗅探」很脆弱，建议 Lead 决定：(a) config.py 按 api 给默认值；(b) 强制显式 baseURL 并在 create_provider 报 ConfigError。**需 Lead 决策**。
- **R4（中）：目录模型无法持久选择**。TUI 选择 discovered-only 模型后只写 [agent] provider/model，下次启动 headless._resolve_model 只认配置 models（除非 A3 放宽）。同时若把模型写回 [providers.X].models 会破坏 TOML 注释/行式重写。P1 建议：放宽运行时校验 + UI 提示用户手工加配置，不自动改 config.toml。
- **R5（中）：目录网络请求会拖慢 TUI/启动**。必须：5s 超时、失败回退配置、last-known-good；TUI 打开选择器时才查询（启动不做），headless 只在显式模型校验时查询。
- **R6（低-中）：retry-after 与 RetryPolicy 的职责边界**。现有 _parse_retry_after 在 openai_compat.py 内；Anthropic 复用需提取到共享模块，否则两处漂移。
- **R7（低）：usage 语义差异**。OpenAI prompt_tokens 含缓存命中；Anthropic input_tokens 不含 cached（需相加）；DSH 分开存 cacheRead/cacheWrite。文档必须写清映射，测试覆盖两种服务端字段。
- **R8（低）：LlmRequest 无 reasoning effort 字段**。P1 只能从 ModelConfig.model_extra 读取；/model 切换模型不会切换 effort。若 Lead 要按请求传 effort，需 core 扩展 LlmRequest（**需 Lead 决策**）。
- **R9（低）：重复/并发的目录刷新**。registry 用 per-provider asyncio.Lock 单飞；aclose() 时需同时取消进行中的 listing 请求，避免关闭后 leak（用 asyncio.gather）。

### 3.2 非目标（P1 明确不做）

- Anthropic 之外的新协议（Bedrock/Vertex/OpenAI Responses）。
- Files API、图片 offload、anthropic-beta。
- prompt caching / cache_control / session affinity / prompt_cache_key。
- OAuth/native auth、x-dsh-auth-token、登录流程。
- 目录的版本门槛（CLIENT_UPGRADE_REQUIRED）、badge 多语言翻译、catalog 持久化到磁盘。
- llm/models-updated 推送事件与后台刷新（先按需查询）。
- 非流式 complete()。
- 把目录能力加进 LlmProvider Protocol（避免破坏现有 fake provider）。

---

## 4. Lead 必读结论（6 条）

1. **listModels 的真实契约是分层的**：listModels 只给 {provider,id,name,description?,inputModalities?,badge?}，**不含 contextWindow**；context/defaultMaxTokens/reasoning/systemPromptUpdate 在 resolveModelInfo。P1 若只在 ModelInfo 里塞 contextWindow 供 TUI 用可以（超集），但**不要**把 contextWindow 当成 listModels 的契约去要求 provider 必填。证据：dsh-llm\lib\index.js:2086-2106、:608-693；README.md:28。
2. **DSH 没有 /models 命令**，listModels 是程序 API（typert.host.js:235）+ llm/models-updated 事件（:722-724）；消费者只有 GUI 和 subagent 工具（dsh-tool-subagent\lib\index.js:199）。Python 侧若做 TUI 选择器，是**新增消费方**，不是复刻命令。
3. **Anthropic 的蓝本是 dsh-llm-deepseek，不是 pi-ai 第三方目录**：请求 POST /v1/messages、x-api-key / anthropic-version: 2023-06-01、thinking{type}+output_config.effort、tool_result 走 user 消息、message_stop 为唯一终止、STREAM_CLOSED/EMPTY_RESPONSE 区分、429/5xx 可重试而 400/401/402/403/413 不可重试。证据：dsh-llm-deepseek\lib\index.js:1603-1745、:1941-2032、:1769-1791。
4. **最大阻塞是 thinking signature**：core Finish 无 replay 通道、agent/session.py:1121-1146 写死 source。P1 请批准「默认 reasoning_effort=off、历史 reasoning 丢弃、thinking 多轮列为 core 扩展后再做」；否则 B2 需要先改 core（Lead 写域）。
5. **配置有两个待决策点**：(a) ProviderConfig.baseURL 默认值是 OpenAI 形状，api="anthropic-messages" 时如何取默认端点（嗅探 vs 显式必填 vs config.py 加 per-api 默认）；(b) 目录发现出的模型能否被 /model 选中并持久化（放宽运行时校验 vs 自动写 config.toml）。两者都涉及 config.py/Lead 写域，需在 A2/A3/B2 开工前定。
6. **拆分建议：A1/A2（llm-dev）→ A3（tui-dev，等 A2 接口）与 B1（llm-dev，可与 A1 并行）→ B2（llm-dev，registry 与 A2 串行）→ B3/B4（llm-dev）**。写冲突点只有 src\deeppyro\llm\registry.py（A2/B2）、docs\spec\llm.md（A4/B4）与 src\deeppyro\builtin\inspect.py（A2），建议每个文件同一时间只归一个 task。
