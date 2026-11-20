# LLM provider layer (`deeppyro.llm`)

> **状态**：现行（0.3.0） · **层级**：功能规格 · **最后核对**：2026-11-19

Status: implemented (owner: llm-dev). Streaming-only; two transports: OpenAI
chat-completions (DeepSeek, llama-server, vLLM, ...) and Anthropic Messages (P1, section 14).

**0.3.0 native-only:** `deeppyro.llm` ships only the wire-free native providers.
`create_native_provider` / `LlmRegistry.native_provider` build `core.llm.LlmProvider`
transports (OpenAI-compatible + native Anthropic) for `agent/model_call.py`; there is no
second transport layer, no v4 converter module and no v4 loop fallback. Sections below that
mention `create_provider`, `deeppyro.llm.messages`, `core.wire`, `core.protocols` or
`AgentSession` are 0.2.0 history — code and tests are authoritative.

## 1. Public API

```python
from deeppyro.llm import LlmRegistry, create_native_provider
from deeppyro.llm.native_anthropic import NativeAnthropicProvider
from deeppyro.llm.native_provider import NativeOpenAICompatProvider

create_native_provider(name: str, pc: ProviderConfig, *, timeout_s: float = 600.0) -> core.llm.LlmProvider
registry = LlmRegistry(cfg: Config)      # lazy, cached, one native provider per configured name
registry.native_provider("deepseek")     # -> core.llm.LlmProvider; ConfigError when unknown
await registry.aclose()                  # closes every instantiated provider

provider = OpenAICompatProvider(
    name, base_url, api_key, *, model_ctx: int | None = None,
    timeout_s: float = 600.0, retry_policy: RetryPolicy | None = None,
)
provider = NativeOpenAICompatProvider(   # wire-free mirror used by the native loop
    name, base_url, api_key, *, model_ctx: int | None = None,
    timeout_s: float = 600.0, retry_policy: RetryPolicy | None = None,
    attachments: object | None = None,   # optional image-attachment resolver
    requires_reasoning_content: bool = False,  # DeepSeek thinking replay (§11)
)
provider = AnthropicProvider(            # when pc.api is "anthropic-messages"/"anthropic"
    name, base_url, api_key, *, model_ctx: int | None = None,
    model_max_tokens: int | None = None, reasoning_effort: str = "off",
    timeout_s: float = 600.0, idle_timeout_s: float = 300.0,
    retry_policy: RetryPolicy | None = None,
)
provider = NativeAnthropicProvider(      # wire-free mirror used by the native loop
    name, base_url, api_key, *, model_ctx: int | None = None,
    model_max_tokens: int | None = None, reasoning_effort: str = "off",
    timeout_s: float = 600.0, idle_timeout_s: float = 300.0,
    retry_policy: RetryPolicy | None = None,
    attachments: object | None = None,   # optional image-attachment resolver
)
async for event in provider.stream(req): ...
await provider.aclose()

policy = RetryPolicy.from_policy({"maxRetries": 5, "baseDelayMs": 500, "maxDelayMs": 10000,
                                  "jitter": 0.1, "codes": ["RATE_LIMIT", "SERVER"]})
```

`create_native_provider` reads `ProviderConfig.api` and builds a native
(`core.llm.LlmProvider`) transport for every supported API:
`NativeOpenAICompatProvider` for the OpenAI-compatible aliases and
`NativeAnthropicProvider` for `anthropic-messages`/`anthropic_messages`/`anthropic`
(section 14.7). Only an unknown API raises `ConfigError` ("unsupported api"); an Anthropic
provider additionally requires an explicit `base_url` and an API key reachable through
`ProviderConfig.resolve_api_key()`, and reports a `ConfigError` naming the provider and the
missing key otherwise. `headless.boot_runtime` treats a native-provider construction failure as a
startup `ConfigError` (exit 2) and never falls back to the legacy v4 loop, which the 0.3.0 cut
removes.

Extra helpers (test/debug surface): `deeppyro.llm.sse` (`SseDecoder`, `iter_sse`,
`decode_sse_text`, `format_sse`), `deeppyro.llm.native_messages` (the wire-free message
converter used by the native transport),
`deeppyro.llm.attachments` (`image_part_url` and the attachment-ref resolver shared by both
converters), `deeppyro.llm.http` (`build_client`, `headers` — the shared httpx client/timeout
and SSE header construction), `deeppyro.llm.errors` (`LlmRequestError`, `LlmHttpError`,
failure `CODE_*` constants), `deeppyro.llm.retry` (`RetryPolicy`, `RETRYABLE_CODES`,
`RETRYABLE_HTTP_STATUSES`).

## 2. Request construction

```
POST {baseURL}/chat/completions
headers: content-type: application/json
         accept: text/event-stream
         authorization: Bearer <api key>        (only when ProviderConfig.resolve_api_key() is set)
body: {
  "model": req.model,
  "messages": [...],
  "stream": true,
  "stream_options": {"include_usage": true},
  "tools": [{...}],            # only when req.tools is non-empty
  "max_tokens": 1234,          # only when req.max_tokens is set; clamped to context window
  "temperature": 0.0           # only when req.temperature is not None
}
```

* `baseURL` is normalized by stripping the trailing slash; a baseURL that already ends in
  `/chat/completions` is used verbatim.
* `max_tokens = min(req.max_tokens, req.context_window or model_ctx)`. `max_tokens` (not
  `max_completion_tokens`) is what DeepSeek and llama-server accept.
* `model_ctx` comes from the native provider: the largest `contextWindow` among the
  provider's configured models (None when no model declares one).
* `requires_reasoning_content` is a provider-level adapter option (model-level first, then
  provider-level) that makes the converter write `reasoning_content` even when a replayed
  assistant turn captured no reasoning; the value is an empty string, which DeepSeek
  accepts (omitting the key is an HTTP 400 in thinking mode, §11). `create_native_provider`
  enables it automatically for a provider named `deepseek` or hosted on a DeepSeek domain
  and leaves every other OpenAI-compatible endpoint alone; another endpoint can opt in
  (and DeepSeek can opt out) with the `requires_reasoning_content` key.
* Timeout: `httpx.Timeout(timeout_s, connect=min(30, timeout_s))`; the client is created
  lazily on the first request and closed by `aclose()` (idempotent; a closed provider
  raises on further streams instead of silently opening a new socket).
* No prompt-cache/session affinity headers are sent yet: DSH's `x-session-affinity` /
  `prompt_cache_key` scheme is provider-specific and not required by the contract.
  `LlmRequest.session_id` (and its native mirror `ModelRequest.session_id`) is therefore
  currently unused (kept for a later cache-aware provider). OpenAI-compatible servers treat
  this as a plain cache miss, not an error.
* The OpenAI-compatible native provider sends the identical request shape: only the request
  object differs (`core.llm.ModelRequest` instead of `LlmRequest`), so recorded request bodies
  replay unchanged.

## 3. Message conversion (native -> chat messages)

Input is `core.messages.Message` (schema 1 session log); `deeppyro.llm.native_messages`
converts it, and `deeppyro.llm.attachments` holds the image resolver. The 0.2.0 v4
converter (`deeppyro.llm.messages`) and `core.wire.Message` were deleted in 0.3.0; the
fixture shapes it mirrored are history.

| wire Message | chat-completions message |
| --- | --- |
| `role="system"` / `role="developer"` | `{"role":"system","content":text}` (empty -> dropped) |
| `role="user"`, text only | `{"role":"user","content":text}` |
| `role="user"` with `ImagePart` | content array `[{"type":"text"},{"type":"image_url","image_url":{"url":...}}]` |
| `role="assistant"` | `{"role":"assistant","content":text_or_null,"reasoning_content":reasoning_or_required_empty?,"tool_calls":[...]}` |
| `role="tool"` | `{"role":"tool","tool_call_id":toolCallId or source.callId,"content":text}` |

* `LlmRequest.system` is prepended as a system message unless the history already
  contains a `role="system"` message (then history wins; the system instruction is never
  duplicated).
* Assistant `content` is `null` when the turn contains only tool calls (OpenAI requires
  the key to be present for tool-only turns; llama-server accepts null).
* `ToolCallPart.arguments` is a JSON *string* in the log (`tool/call` record stores
  stringified JSON). The string is validated with `json.loads` but then forwarded
  **byte-for-byte**, without re-serialisation: re-encoding would reorder keys/whitespace,
  change the prompt bytes and make prompt-cache replays diverge. A string that is not valid
  JSON cannot be sent to the API at all, so `normalize_arguments` raises
  `LlmRequestError(code="BAD_REQUEST", retryable=False)` before any request is made.
* DeepSeek reasoning is replayed through `reasoning_content`; this is the same field the
  provider streams, so history round-trips. A thinking-mode server also requires the key
  on a replayed assistant turn that has tool calls but captured no reasoning, so
  `requires_reasoning_content` providers get `"reasoning_content": ""` there instead of
  omitting the key (an empty string is accepted; omission is HTTP 400). The key is only
  forced when the request carries tools.
* `ImagePart.attachment` accepted forms: a ready `data:`/`http(s)://` URL, or a dict with
  `url`, or a dict with `data`/`base64` plus `mimeType`/`mediaType`. A bare attachment id is
  dereferenced only when the converter receives an attachment resolver
  (`llm.attachments.image_part_url(..., attachments=...)`); the v4 converter and
  registry-built native providers pass none, so those parts are skipped (documented
  limitation; the log fixtures contain no bare-id image parts, so no real behaviour depends
  on it yet).
* Unsupported/unknown part types and messages that convert to empty content are dropped.

## 4. Streaming contract (historical: INTERFACES §5.1-5.9, deleted in 0.3.0)

1. `stream()` is an async generator; the last event of a normal stream is always
   `Finish` (the only exception is a failure before any content, which raises instead).
2. Text -> `TextDelta`; `reasoning_content` (DeepSeek) or `reasoning` -> `ReasoningDelta`.
3. Tool calls: one `ToolCallStart(call_id, name)` then N `ToolCallArgsDelta(call_id, delta)`.
   Argument fragments are forwarded verbatim; the consumer concatenates them. Calls are
   tracked by the SSE `index` (DSH fixture calls use index 2/3/4 because 0/1 are the
   reasoning/text blocks; OpenAI servers send 0..N — both work). Missing ids are synthesised
   with `core.ids.new_call_id()`.
4. `Usage` is emitted before `Finish` whenever the server sends it (`stream_options.include_usage`).
5. Failures before the first content event raise `LlmError(retryable=...)`; failures after
   content produce `Finish(kind="error", failure={"message","code"})` and end the stream.
6. `asyncio.CancelledError` is never swallowed or converted: it propagates out of the
   generator untouched (also true inside `RetryPolicy.run`).
7. Payload shape as in §2.
8. Message conversion as in §3 (conversion errors, e.g. invalid tool-call arguments, are
   raised before the HTTP request and are never retried).
9. Retries wrap only the acquisition of the first stream event (§6).

Success stream example (plain answer):

```
TextDelta("Hel") TextDelta("lo") Usage(...) Finish(kind="stop")
```

### 4.1 Native contract (KI-010)

`core.llm` defines the wire-free mirror of this contract: `StopReason` (StrEnum with
`stop/tool_calls/length/error/aborted/content_filter`), `Usage`, `ToolSpec`,
`ModelRequest`, the same delta/usage/finish event dataclasses plus
`RetryScheduled`, `ModelResponse`, `NativeToolCall` and the `LlmProvider` Protocol.
Native providers (`NativeOpenAICompatProvider`, `NativeAnthropicProvider`) emit identical
event sequences, with `Finish(kind="tool-calls")` becoming
`Finish(stop_reason=StopReason.TOOL_CALLS)`.
`agent/model_call.py` adapts one stream to one `ModelResponse` and preserves every v4
adapter invariant (finish required, tool-call names, argument joining, forced `tool_calls`,
empty-message rejection, cancellation and usage last-wins); it also counts
`RetryScheduled` events into `ModelResponse.retries`.

## 5. SSE decoding

`SseDecoder` is incremental and transport-agnostic:

* `data:` fields only; `event:`/`id:` are surfaced on `SseEvent` but not required;
  comment lines (`: ping`) and `retry:` are ignored; multiple `data:` lines in one event
  are joined with `\n`.
* Line endings: `\n`, `\r\n` (a lone `\r` is supported as a terminator when followed by
  more input; blank-line framing is otherwise required).
* UTF-8 is decoded with a stateful incremental decoder, so a multi-byte character split
  across TCP reads is never corrupted.
* `flush()` dispatches a trailing event that was not followed by a blank line (some
  servers omit the final separator).
* `{"error": {...}}` frames are turned into `LlmRequestError` with a normalized `code`.

## 6. Retry policy

`RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=30.0, jitter=0.1)`:

* delay after failure n (1-based) = `min(base_delay * 2**(n-1) * (1 + jitter*random()), max_delay)`;
  a larger `Retry-After` (delta-seconds or HTTP date) overrides the computed delay, still
  capped by `max_delay`.
* `max_attempts` counts total attempts (1 = never retry).
* Only retryable `LlmError`s are retried: statuses `{408,409,425,429,500,502,503,504}`
  (plus any other 5xx), network/timeout transport errors, and failure codes
  `{EMPTY_RESPONSE, RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT}` — exactly the retryable set
  pinned by `fixtures/records/by-type/llm_retry.jsonl` policyKey
  `["normal",5,["EMPTY_RESPONSE","RATE_LIMIT","SERVER","TIMEOUT","TRANSPORT"],500,10000,0.1]`
  (the fixture records 5 *retries*, i.e. 6 attempts; the Python default of 3 total attempts
  is the contract default).
* `RetryPolicy.from_policy(policy)` rebuilds a recorded DSH policy for replay/integration
  tests. It accepts the dict shape `{maxRetries, baseDelayMs, maxDelayMs, jitter, codes}`
  (snake_case aliases tolerated) and the raw policyKey as a JSON string or parsed sequence
  `[mode, maxRetries, codes, baseDelayMs, maxDelayMs, jitter]`. `maxRetries + 1` becomes
  `max_attempts`; milliseconds become seconds; `codes` becomes `RetryPolicy.retryable_codes`,
  which then **replaces** the per-exception `retryable` flag so replays retry exactly what
  the recorded policy would have retried. Missing keys keep the dataclass defaults;
  wrong value types raise `TypeError` and out-of-range/length/JSON errors raise `ValueError`.
* 4xx other than 408/409/425/429 are never retried (400/401/403/404/422).
* `RetryPolicy` is also an async context manager (`async with RetryPolicy(...) as retry`),
  matching the interface snippet; `run(operation, *, on_retry=None)` owns the loop.
  `on_retry(attempt, exception, delay)` is called for every retry that will be slept on, so
  the native provider can emit `RetryScheduled(attempt, code, delay_s)` before sleeping.
  `agent/model_call.py` counts those events into `ModelResponse.retries`; the native loop
  stores the count in `ModelCallRecord.retry_count` and `TurnUsage.retries` (previously
  hardcoded to 0).
* Retries stop as soon as content was produced: mid-stream failures never replay the
  request (the consumer has already seen deltas).

### 6.1 Builtin retry consumer (`deeppyro.builtin.llm_retry`, P0-2)

The retry loop lives in the agent loop (`agent/model_call.py` for native; the 0.2.0
0.2.0 v4 `AgentSession._stream_step` was deleted); the builtin
`llm-retry` plugin supplies the policy and the `agent/request-error` decision:

    [[plugins]]
    id = "llm-retry"
    plugin = "deeppyro.builtin.llm_retry:LlmRetryPlugin"

* The plugin injects `hooks` and `config`, provides the `retryPolicy` service, and
  listens on the `agent/request-error` waterfall. Its handler returns
  `{"kind": "retry"}` when the payload carries a policy and the signal is not
  aborted; otherwise it calls `next()` so a later listener can decide.
* The session builds the payload `{agent, turn, step, provider, failure,
  retryPolicy, signal}`, where `retryPolicy` is the service value (None when the
  plugin is disabled). Only that decision triggers `decide_retry`; the session
  then appends `llm/retry` (and `llm/retry-started` after the delay) and repeats
  the same request. Without the plugin no policy exists, so nothing retries and
  pre-P0 behaviour is unchanged.
* Config table (top-level, parsed while the plugin boots; invalid values raise
  `ConfigError` instead of silently disabling recovery):

      [retry]
      mode = "normal"          # or "always"
      maxRetries = 5
      initialDelayMs = 500
      maxDelayMs = 10000
      jitterRatio = 0.1
      retryableCodes = ["EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT"]

  Missing keys fall back to the DSH defaults. `mode = "normal"` becomes
  `RetryPolicy(max_attempts=maxRetries+1, base_delay=initialDelayMs/1000,
  max_delay=maxDelayMs/1000, jitter=jitterRatio, retryable_codes=...)`; an absent
  `[retry]` table means `RetryPolicy()` (3 attempts). `mode = "always"` returns a
  mapping instead because `RetryPolicy` cannot express an unbounded retry budget.
* Tests: `tests/builtin/test_llm_retry.py` (policy construction, hook decisions,
  session integration incl. always mode and exhausted retries) and the retry
  decision tests in `tests/agent/test_retry.py`.

## 7. Failure taxonomy

| situation | code | retryable | surfaced as |
| --- | --- | --- | --- |
| HTTP 429 | `RATE_LIMIT` | yes | raise before content / Finish(error) after |
| HTTP 408 | `TIMEOUT` | yes | as above |
| HTTP 5xx | `SERVER` | yes | as above |
| HTTP 401/403 | `AUTH` | no | raise |
| HTTP 404 | `NOT_FOUND` | no | raise |
| other 4xx | `BAD_REQUEST` | no | raise |
| invalid tool-call arguments in history | `BAD_REQUEST` | no | raise before the request |
| connect/read error | `TRANSPORT` / `TIMEOUT` | yes | as above |
| non-SSE content-type | `PROTOCOL` | no | raise |
| invalid JSON / non-object SSE data | `PROTOCOL` | no | raise; Finish(error) if content already seen |
| stream ends without finish_reason and without [DONE] | `EMPTY_RESPONSE` if no content, else `PROTOCOL` | yes | raise / Finish(error) |
| `finish_reason` present but zero content events | `EMPTY_RESPONSE` | yes | raise (retried) |
| mid-stream error frame after content | normalized code | — | `Finish(kind="error")` |

`Finish(kind="error").failure` is always `{"message": str, "code": str}`, the same shape the
DSH log records for `assistant/attempt` finish errors.

## 8. `finish_reason` mapping

`stop`/`end_turn` -> `stop`; `tool_calls`/`tool_use`/`function_call` -> `tool-calls`;
`length`/`max_tokens` -> `length`; `content_filter` -> `content-filter`;
`error` -> `error`; anything unknown -> `stop`. If the server sends `[DONE]` without a
finish_reason, `tool-calls` is inferred when tool calls were streamed, else `stop`.

## 9. Usage mapping

`prompt_tokens` -> `input_tokens`, `completion_tokens` -> `output_tokens`,
`total_tokens` (or the sum when absent). `cached_input_tokens` comes from
`prompt_tokens_details.cached_tokens` or DeepSeek's `prompt_cache_hit_tokens`; otherwise
None. (The session log stores camelCase `inputTokens/outputTokens/cacheReadTokens` in
`assistant/message.usage`; that mapping belongs to the session/agent workstream.)

## 10. Contract interpretation: tool-call arguments stay a JSON string

The historical INTERFACES §5.8 said `ToolCallPart.arguments` (a JSON string) must be `json.loads`-ed into
`tool_calls[].function.arguments`. The OpenAI wire format requires `arguments` to remain a
*string*, and fixtures record it as stringified JSON (fixtures tool/call record), so the
literal "put an object there" reading would produce a request every OpenAI-compatible server
rejects. The agreed interpretation (confirmed with the lead) is: validate the string with
`json.loads`, then send the original bytes unchanged — no `json.dumps`, since re-encoding
alters key order/whitespace and shifts prompt-cache prefixes. Unparseable strings are a
client-side bug and fail fast with `LlmRequestError(code="BAD_REQUEST")`. Observable behaviour:
`arguments` is the same JSON text that was logged.

## 11. Known deviations / risks

* KI-010 moved the native loop off the v4 wire request: `AgentLoop._prompt_payload` now
  serializes native `Message.to_dict()` / `ToolSpec.to_dict()` instead of
  `model_dump(mode="json")`. The `prompt_hash` format (`sha256:<64 hex>`) and the hashing
  algorithm are unchanged, but the hash **values change once**; traces recorded before and
  after the port cannot be joined by prompt hash. No fixture pins the value.

* `reasoning_content` replay used to be added only when non-empty, which made a DeepSeek
  thinking-mode tool loop fail with HTTP 400 ("The `reasoning_content` in the thinking mode
  must be passed back to the API") whenever one tool-call turn captured no reasoning (the
  second sequential tool call in a turn commonly has none). The replay now follows DSH's
  `requiresReasoningContentOnAssistantMessages` behaviour: `requires_reasoning_content`
  providers write the key on every replayed tool-call assistant message, empty when no
  reasoning was captured; see §2/§3 and `tests/llm/test_native_messages.py`.
* No session-affinity/prompt-cache headers (§2) — cache hit-rate difference only.
* Anthropic-compatible endpoints are supported by `AnthropicProvider` (section 14); the
  `web_deepseek_search_llm_request` fixture uses an Anthropic-shaped *web search tool*
  payload that is still out of scope (no server-side tools in P1).
* Anthropic history reasoning is **not** replayed with a thinking signature: thinking is
  enabled only for the live call, and every replayed assistant turn is sent without a
  thinking block. Multi-turn thinking/tool loops may therefore be rejected by the server
  (risk R1 in `docs/archive/research/p1-providers.md`); the core `Finish`/`LlmRequest` contract has no
  channel for signatures, so enabling this needs a core extension (deferred).
* Anthropic providers must configure `base_url` explicitly. `ProviderConfig.base_url` has a
  DeepSeek-flavoured default, so provider construction raises `ConfigError` (with
  `https://api.anthropic.com` as the hint) instead of silently calling the wrong host.
* Anthropic system/developer messages are always folded into the top-level `system` string;
  DSH's in-history system snapshots (`in_history_system`) are not implemented in P1.
* Anthropic images are converted only when they are inline `data:` URLs; remote URLs and bare
  attachment ids are skipped (no Files API).
* native provider construction raises `ConfigError` for an unsupported `api`; `LlmRegistry.native_provider` raises
  `ConfigError` for unknown provider names, whereas `Config.resolve()` raises `KeyError`.
  The LLM layer normalizes config mistakes to the `core.errors` hierarchy.
* Non-streaming `complete()` is intentionally not implemented (streaming-only project).
* Catalog discovery is metadata only: it never writes `config.toml`, never widens the
  runtime model check, and emits no `models-updated` event (P1). Selecting a discovered
  model is the caller's job (TUI writes the session `modelSelection` record).
* `OpenAICompatProvider.list_models` sends the request even without an API key, deviating
  from the design note in `docs/archive/research/p1-providers.md` ("return [] without a key"): local
  llama-server has no auth and is the main discovery use case. Auth-gated endpoints answer
  401/403, which surfaces as `LlmHttpError` and falls back to the configured catalog in the
  registry.
* Discovery treats 3xx as failures (redirects disabled) instead of following them, so a
  misconfigured base URL (missing `/v1`, http->https, login redirect) reports the HTTP error
  instead of silently fetching an HTML page.

## 12. Test coverage (`uv run pytest tests/llm -q`)

* `test_sse.py`: framing, comments, multi-line data, CRLF, incremental/UTF-8-split bytes,
  flush without trailing blank line, `iter_sse`, `format_sse` round-trip.
* `test_messages.py`: system prepend/no-duplicate, developer->system, assistant
  text/reasoning/tool_calls, tool results (toolCallId + source.callId fallback), images,
  byte-for-byte argument preservation, invalid-arguments fast failure, tool schemas, payload
  fields/clamping.
* `test_retry.py`: backoff arithmetic, jitter, caps, Retry-After, retryability rules,
  exhaustion, cancellation, context manager, `on_retry` reporting (attempt/code/delay),
  `from_policy` (dict + fixture policyKey + codes-authoritative + bad shapes), and fixture
  policyKey parity.
* Native transport (KI-010): `test_native_messages.py` (request-body goldens for
  `core.messages`/`ToolSpec` conversion, image passthrough, argument preservation),
  `test_native_provider.py` (respx + inline SSE: text/reasoning/fragmented tool calls, both
  cache-usage fields, finish mapping, mid-stream cuts, retries with `RetryScheduled`,
  non-SSE content types, 401/400, listing, idempotent close, cancellation),
  `test_native_anthropic.py` (httpx.MockTransport: text/tool/thinking streams, split
  `input_json_delta` arguments, usage+cache fields, stop-reason mapping, image input, 429
  retries with `RetryScheduled`, 4xx classification, malformed SSE, cancellation), and
  `tests/agent/test_model_call.py` (one test per adapter invariant).
* `test_openai_compat.py` (respx-mocked, no network): plain text, reasoning deltas,
  fragmented single/multi tool calls, usage (+DeepSeek cache field), finish mapping,
  [DONE] inference, mid-stream cut, invalid JSON, wrong content-type, 500/429/401/400,
  stream error frames, empty-response retries, idempotent close, cancellation; plus
  `list_models` listing parsing/headers, 405->[], keyless requests, 401 surfacing, HTML
  payload rejection.
* `test_registry.py`: provider construction, OpenAI/Anthropic api dispatch (v4 and native),
  native Anthropic `base_url`/API-key requirements, `max_tokens`/`reasoning_effort` adapter
  options, context-window selection, env api keys, caching, unknown provider, close-all
  ordering/errors; plus catalog discovery:
  configured snapshot, listing merge/precedence, TTL cache, refresh bypass, failure fallback
  with last-known-good, 404 empty means configured, providers without listings, `model_info`,
  aclose clearing the cache.
* `test_catalog.py`: badge/model info serialization, configured catalog extras, listing
  parser aliases and validation, merge precedence, protocol errors.
* `test_discovery.py`: URL normalization for both API shapes, `fetch_listing` headers/timeouts/
  redirect refusal/status classification/oversized or non-JSON payloads, `CatalogCache`
  TTL/failure-backoff/clear semantics.
* `test_inspect_models.py`: boot-level `inspect.providers()` rows carry configured models,
  discovered models after `list_models`, and inspection stays defensive when a service lacks
  `catalog_snapshot`.
* `test_fixtures.py`: real request/header tools, assistant/tool-result messages and
  byte-for-byte tool-call arguments from `fixtures/` convert to valid chat-completions JSON.
* `test_errors.py`: Anthropic error-info parsing, status/type -> code/retryable table,
  context-window detection, Retry-After seconds/HTTP-date, failure payload shape.
* `test_messages_anthropic.py`: system folding, assistant text/thinking/tool_use conversion,
  reasoning-off drop, tool-result pairing and validation errors, image data URLs, adjacent
  merging, payload fields and `max_tokens` precedence, effort/thinking switch.
* `test_anthropic.py` (respx-mocked, no network): URL normalization, text/thinking streams,
  fragmented and full `tool_use` inputs, usage split across message_start/message_delta,
  stop-reason mapping including unknown, missing `message_stop` (before/after content),
  empty responses, 401/402/429/5xx/context-window HTTP errors with Retry-After, stream
  error events, invalid JSON/tool args, idempotent close, cancellation, `list_models`.

## 13. Model catalog discovery (`list_models` / `model_info`)

P1 scope: discovery is **metadata only**. It never edits `config.toml` and never relaxes the
runtime model check (`Config.resolve` / agent model validation are unchanged); the TUI/CLI may
use the list to offer candidates, and a selected model is written to the session's
`modelSelection` record by the agent layer (A3), not here.

### 13.1 Public surface

* `ModelInfo` (frozen dataclass): `provider`, `id`, `name`, `description`,
  `context_window`, `max_tokens`, `input_modalities`, `badge`; `.label` (`name or id`) and
  `.to_dict()` with camelCase keys. `provider`/`id`/`name`/`inputModalities` are always
  present; `description`/`contextWindow`/`maxTokens`/`badge` appear only when set, so the
  payload stays cheap over the inspect JSON.
* `ModelBadge(text, tone)`: `text` is a string or a `language -> string` mapping
  (`.label(language="en")` resolves it), `tone` is one of `neutral`/`info`/`success`/
  `warning` (invalid values normalize to `neutral`).
* `parse_model_listing(provider, payload, *, source="")` accepts a bare JSON list,
  `{"data": [...]}` (OpenAI), `{"models": {...}}` (Anthropic) and `{"models": [...]}`.
  Entries without a non-empty string `id` are dropped; duplicate ids keep the first.
  Aliases: name `name`/`display_name`/`displayName`; context `contextWindow`,
  `context_window`, `context_length`, `max_input_tokens`, `maxInputTokens`, `limit.context`;
  max output `maxTokens`, `max_tokens`, `maxOutputTokens`, `max_output_tokens`,
  `max_completion_tokens`, `maxCompletionTokens`, `top_provider.max_completion_tokens`,
  `limit.output`; modalities `inputModalities`/`input_modalities` (only `text`/`image`
  kept, default `("text",)`). Non-listing payloads raise
  `LlmRequestError(code="PROTOCOL")` with the `source` in the message.
* `catalog_from_config(provider, pc)` builds the configured catalog from `ProviderConfig`,
  including snake_case adapter options `max_tokens`/`input_modalities` from
  `ModelConfig.model_extra` (config validation rejects their camelCase spellings since 0.2.0).
* `merge_catalogs(base, overlay)`: the configured order is the spine; a listing entry with the
  same id overrides display fields (`name`, `description`, `badge`) and fills missing
  `context_window`/`max_tokens`/modalities from config; listing-only ids are appended.
* `LlmRegistry.list_models(provider, *, refresh=False)`,
  `LlmRegistry.model_info(provider, model_id)`, `LlmRegistry.configured_models(provider)`,
  `LlmRegistry.catalog_snapshot(provider)` (synchronous, used by inspect) and
  `LlmRegistry.last_catalog_error`.
* `OpenAICompatProvider.list_models() -> list[ModelInfo]` issues a separate GET (never part of
  the stream); 404/405 means "no listing endpoint" and yields `[]`.
* `builtin.llm.LlmPlugin.list_models/model_info/catalog_snapshot` forward to the registry.
  Plugin-contributed factories are asked directly (their `list_models()` may return a list or
  an awaitable) and contribute `[]` when they provide none.
* `builtin.inspect.providers()` rows gain `"models": [info.to_dict(), ...]` from
  `catalog_snapshot`, so rendering never triggers network I/O.

### 13.2 URLs

* OpenAI-compatible: `<base>/models` after stripping trailing `/` and a pasted
  `/chat/completions` or `/completions` suffix; a base already ending in `/models` is used
  as-is.
* Anthropic: `<origin>/v1/models?limit=1000` — accepts `https://api.anthropic.com`, `.../v1`,
  `.../v1/messages`, `.../messages`, `.../v1/models`, with optional trailing `/`.
* `fetch_listing` uses a 5 s timeout (interactive metadata, not the 600 s stream timeout),
  refuses redirects (`follow_redirects=False`), caps the payload at 4 MiB and raises
  `LlmRequestError` for timeouts (`TIMEOUT`, retryable), transport errors (`TRANSPORT`,
  retryable), non-2xx statuses (shared `http_error` classification including
  `Retry-After`) and non-JSON bodies (`PROTOCOL`).

### 13.3 Cache / fallback policy

`CatalogCache` keeps a TTL cache (default `600 s`) per provider plus the last error:

* a fresh listing is reused without a request; `refresh=True` bypasses it;
* on failure the registry records the error and serves the last good listing when one exists
  (backing off for another TTL), otherwise the configured catalog;
* an empty listing is not cached — an endpoint answering 200 with nothing must not hide
  models added later during the TTL;
* concurrent calls use a per-provider `asyncio.Lock` (single flight); only unknown provider
  names raise `ConfigError` from `list_models`, every other failure degrades;
* no `models-updated` event is emitted in P1; consumers refresh on demand (e.g. when opening a
  model picker).
## 14. Anthropic Messages provider (`NativeAnthropicProvider`, P1 B1/B2)

`create_native_provider` builds this provider for `ProviderConfig.api` in
`{"anthropic-messages", "anthropic_messages", "anthropic"}`. It implements B1/B2 of
`docs/archive/research/p1-providers.md`; B3/B4 (voice/TUI polish, non-streaming helpers) remain out of
scope. Lead decisions applied here: thinking signatures are not replayed, `base_url` must be
explicit, and model discovery stays metadata-only (section 13).

### 14.1 Construction, endpoint and headers

* `base_url` is mandatory and must be set explicitly; a missing value raises `ConfigError`
  mentioning `https://api.anthropic.com` (no auto-detection).
* `model_max_tokens` and `reasoning_effort` come from config adapter options, model-level first and
  provider-level as fallback: `max_tokens` and
  `reasoning_effort` (snake_case only since 0.2.0; camelCase spellings are rejected at config
  validation). The effort must be one of `off`, `low`, `medium`,
  `high`, `max`; an invalid value fails in the constructor (`ConfigError`).
* `messages_url`: a base already ending in `/messages` is used verbatim; a base ending in
  `/v1` gets `/messages`; otherwise `/v1/messages` is appended.
* Headers: `content-type: application/json`, `accept: text/event-stream`,
  `anthropic-version: 2023-06-01`, and `x-api-key: <key>` when a key is configured. There is
  deliberately no `Authorization: Bearer` header.
* Timeout: `httpx.Timeout(timeout_s, connect=min(30, timeout_s), read=idle_timeout_s)`.
  The read timeout is the *idle* timeout for a streaming body (default 300 s), so a stalled
  stream fails as `TIMEOUT` instead of hanging for the whole request budget.
* `list_models()` issues `GET <origin>/v1/models?limit=1000` with the same version/key
  headers; 404/405 means the endpoint is unavailable and yields `[]`.

### 14.2 Request body

```
POST {messages_url}
headers: content-type: application/json, accept: text/event-stream,
         anthropic-version: 2023-06-01, x-api-key: <key>   (key optional)
body: {
  "model": req.model,
  "max_tokens": req.max_tokens or model_max_tokens or 8192,   # mandatory for Anthropic
  "stream": true,
  "messages": [...],                 # user/assistant alternating, tool_result in user
  "system": "...",                   # only when a system/developer message exists
  "temperature": 0.0,                # only when req.temperature is not None
  "tools": [{"name", "description", "input_schema": {...}}],  # only when non-empty
  "thinking": {"type": "disabled"} | {"type": "enabled"},
  "output_config": {"effort": "low" | "medium" | "high" | "max"}  # only when enabled
}
```

`max_tokens` precedence is request -> `model_max_tokens` -> 8192; the context window is not
pre-checked (the server's context-window error is classified as
`CONTEXT_WINDOW_EXCEEDED`). No `stream_options`/`[DONE]` sentinel is used: Anthropic streams
typed events and terminates with `message_stop`.

### 14.3 Message conversion (native `core.messages` -> Anthropic messages)

| wire Message | Anthropic |
| --- | --- |
| `role="system"` / `role="developer"` | folded into the top-level `system` string (empty -> dropped) |
| `role="user"` text | `{"type":"text","text":...}` inside a user message |
| `role="user"` with `ImagePart` | `{"type":"image","source":{"type":"base64","media_type","data"}}` for `data:` URLs; other forms skipped |
| `role="assistant"` text | `{"type":"text","text":...}` |
| `role="assistant"` reasoning | `{"type":"thinking","thinking":...}` only when `reasoning_effort != "off"`, without signature |
| `role="assistant"` tool calls | `{"type":"tool_use","id","name","input": json.loads(arguments) or {}}` |
| `role="tool"` | `{"type":"tool_result","tool_use_id": toolCallId or source.callId, "content":[{"type":"text"}], "is_error"?: true}` inside a user message |

Rules:

* System texts are collected in encounter order after `LlmRequest.system` and joined with
  `"\n\n"`. P1 never replays system messages inside history.
* Adjacent messages with the same role are merged (Anthropic requires alternating roles;
  several tool results become one user message).
* Tool-call `input` is an **object** here (unlike OpenAI's string `arguments`). Arguments are
  parsed with `json.loads`; unparseable JSON or a non-object value becomes `{}` so a bad
  replay cannot fail the whole turn.
* Call/result pairing is validated before the request and raises
  `LlmRequestError(code="BAD_REQUEST")` (never retried): duplicate `tool_use` ids, a
  `tool_result` without a pending call, an assistant tool call that is not immediately
  followed by its results, and a conversation ending with unresolved calls. An empty
  conversation is rejected the same way.
* Tool schemas: `{"name","description","input_schema": parameters or {"type":"object","properties":{}}}`.

### 14.4 Stream event mapping

| Anthropic event | core event |
| --- | --- |
| `message_start` | nothing; start usage captured |
| `content_block_start` `text` (non-empty) | `TextDelta` |
| `content_block_start` `thinking` (non-empty) | `ReasoningDelta` |
| `content_block_start` `tool_use` | `ToolCallStart(id, name)`, then `ToolCallArgsDelta` for a non-empty initial input object |
| `content_block_delta` `text_delta` | `TextDelta` |
| `content_block_delta` `thinking_delta` | `ReasoningDelta` |
| `content_block_delta` `signature_delta` | dropped (P1 does not replay signatures) |
| `content_block_delta` `input_json_delta` | `ToolCallArgsDelta` (fragment verbatim) |
| `content_block_stop` | nothing (tool input validated at end of turn) |
| `message_delta` | records `stop_reason`, merges usage |
| `message_stop` | `Usage` (when seen) then `Finish` |
| `ping`, unknown top-level events, unknown delta types | ignored (forward compatibility) |
| unknown content-block type or unknown `stop_reason` | `PROTOCOL` (fail closed) |

Stop reasons: `end_turn`/`stop_sequence` -> `stop`; `tool_use` -> `tool-calls`;
`max_tokens` -> `length`; `refusal` -> `content-filter`; `pause_turn` -> `stop` (P1 has no
continuation loop); anything else -> `PROTOCOL`.

Termination rules:

* a completed tool_use input must parse to a JSON object unless `stop_reason == "max_tokens"`
  (a truncated call is reported as `length`, not a protocol failure);
* EOF without `message_stop` before any content raises
  `LlmRequestError(code="STREAM_CLOSED", retryable=True)` (retried); after content it becomes
  `Finish(kind="error", failure={"code":"STREAM_CLOSED"})`;
* a stream that finishes with `stop`/`tool-calls` but emitted no content event raises
  `EMPTY_RESPONSE` (retryable);
* `Finish` is always the last event of a normal stream; `Finish(kind="error")` ends a stream
  that failed after content.

### 14.5 Usage

`message_start.message.usage` and `message_delta.usage` are merged (higher value wins for
cumulative counters). Anthropic reports cache reads/writes separately, so:

```
input_tokens  = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
output_tokens = output_tokens
total_tokens  = input_tokens + output_tokens
cached_input_tokens = cache_read_input_tokens
```

`Usage` is emitted before `Finish` only when the stream reported usage at all.

### 14.6 Errors and retries

| situation | code | retryable |
| --- | --- | --- |
| 401 `authentication_error` / 403 `permission_error` | `AUTH` | no |
| 402 | `QUOTA` | no |
| 429 `rate_limit_error` (Retry-After honoured) | `RATE_LIMIT` | yes |
| 400/413 `invalid_request_error` | `BAD_REQUEST` | no |
| context-window phrasing (`prompt is too long`, `context window`, ...) | `CONTEXT_WINDOW_EXCEEDED` | no |
| 5xx, `overloaded_error`, `api_error` | `SERVER` | yes |
| stream `error` event (no HTTP status) | classified by type, else `SERVER` | yes |
| connect/read failure | `TRANSPORT` / `TIMEOUT` | yes |

Classification lives in `deeppyro.llm.errors` (`anthropic_error`, `anthropic_error_info`,
`is_context_window_message`, shared `parse_retry_after`) and mirrors
`docs/archive/research/p1-providers.md` section 2.2.4. An explicit Anthropic `error.type` wins over the
HTTP status (an `invalid_request_error` on a 5xx is a user error). Retries use the same
`RetryPolicy` as the OpenAI transport: only first-event acquisition is retried, content
never replays, `CancelledError` propagates untouched, and `Retry-After` (seconds or HTTP
date) overrides the computed backoff.

### 14.7 Native transport (`NativeAnthropicProvider`, KI-010)

The 0.3.0 native-only cut needs the same Anthropic Messages transport behind the wire-free
`core.llm.LlmProvider` contract (no `core.wire`/`core.protocols` imports — both are deleted).
`NativeAnthropicProvider` lives in `deeppyro.llm.native_anthropic` and is built by
`registry.create_native_provider` for the same API aliases; `builtin/llm.LlmPlugin.native_provider`
exposes it, and `headless.boot_runtime` raises `ConfigError` when it cannot be constructed
(never downgrades to the legacy v4 loop).

Shared with `AnthropicProvider`: endpoint rule (`messages_url`), headers
(`anthropic-version`, `x-api-key`, no Bearer), `httpx.Timeout(..., read=idle_timeout_s)`,
`list_models` (404/405 -> `[]`), request body (section 14.2), message/block conversion semantics
(section 14.3: system folding, adjacent-role merge, strict `tool_use`/`tool_result` pairing,
`input_schema`, block-shaped tool input), stream event mapping and stop reasons (section 14.4),
usage folding (section 14.5) and error classification/retryability (section 14.6).

Differences from the historical v4 provider:

* Request messages come from `core.messages`: `convert_messages(messages, system, ...)` folds
  `ModelRequest.system` and history system/developer text into the top-level `system` string;
  assistant `ToolCallPart` -> `{"type":"tool_use","id","name","input"}`; tool messages ->
  user `tool_result` blocks using `tool_call_id` + `is_error`; `ImagePart` -> inline base64
  images resolved via `image_part_url(..., default_media_type=part.media_type)` (bare attachment
  ids work when an `attachments` resolver is injected, otherwise the image is skipped).
* Events are `core.llm` types (`TextDelta`, `ReasoningDelta`, `ToolCallStart`,
  `ToolCallArgsDelta`, `UsageEvent`, `Finish`); a normal stream ends with `UsageEvent` (when
  the stream reported usage) then `Finish(stop_reason=...)`. A stream that fails after producing
  content ends with `Finish(StopReason.ERROR, message=..., code=...)`.
* Empty responses, cut streams and malformed frames use the same retryable
  `EMPTY_RESPONSE`/`STREAM_CLOSED`/`PROTOCOL` classification as `NativeOpenAICompatProvider`;
  retries emit `RetryScheduled(attempt, code, delay_s)` before backoff, and only first-event
  acquisition is retried.
* Usage maps into `core.llm.Usage` with `cached_input_tokens = cache_read_input_tokens` and
  `cache_write_tokens = cache_creation_input_tokens` (in addition to the v4 aggregate formula in
  14.5). `latency_ms` and the retry count stay in the caller (`agent/model_call.py`).
* `aclose()` is idempotent and closes the lazy `httpx.AsyncClient`; a stream cancelled by the
  consumer propagates `CancelledError` and closes the response context.
* Construction requires an explicit `base_url` and an API key (inline or via `api_key_env`);
  failures name the provider and the missing key and are raised while the kernel boots.

### 14.8 Known deviations / risks (Anthropic, P1)

* **Thinking signatures are not replayed** (Lead decision). With the default
  `reasoning_effort="off"`, history reasoning is dropped and `thinking: {"type":"disabled"}`
  is sent. With thinking enabled, live thinking is still streamed as `ReasoningDelta`, but a
  replayed assistant turn carries a thinking block without its signature; single-turn answers
  work, multi-turn thinking/tool loops may be rejected (risk R1). Enabling this needs a core
  `Finish`/`LlmRequest` extension (deferred).
* Only inline base64 images are supported; remote URLs and bare attachment ids are skipped.
* No in-history system snapshots (all instructions are top-level), no Files API, no
  server-side tools and no beta headers.
* `pause_turn` maps to a normal `stop`; there is no automatic continuation.
* Empty tool input can yield `ToolCallStart` with no `ToolCallArgsDelta`; consumers already
  default missing arguments to `"{}"`.
* The provider never sends `stream_options`; usage arrives in Anthropic's native events.
