# LLM provider layer (`deeppyro.llm`)

Status: implemented (owner: llm-dev). Consumed by `agent` via `core.protocols.LlmProvider`.
Streaming-only, OpenAI chat-completions wire format, no Anthropic adapter.

## 1. Public API

```python
from deeppyro.llm import LlmRegistry, OpenAICompatProvider, create_provider

create_provider(name: str, pc: ProviderConfig, *, timeout_s: float = 600.0) -> LlmProvider
registry = LlmRegistry(cfg: Config)      # lazy, cached, one provider per configured name
registry.get("deepseek")                 # -> LlmProvider; ConfigError when unknown
await registry.aclose()                  # closes every instantiated provider

provider = OpenAICompatProvider(
    name, base_url, api_key, *, model_ctx: int | None = None,
    timeout_s: float = 600.0, retry_policy: RetryPolicy | None = None,
)
async for event in provider.stream(req): ...
await provider.aclose()

policy = RetryPolicy.from_policy({"maxRetries": 5, "baseDelayMs": 500, "maxDelayMs": 10000,
                                  "jitter": 0.1, "codes": ["RATE_LIMIT", "SERVER"]})
```

Extra helpers (test/debug surface): `deeppyro.llm.sse` (`SseDecoder`, `iter_sse`,
`decode_sse_text`, `format_sse`), `deeppyro.llm.messages` (`build_chat_payload`,
`convert_messages`, `message_to_chat`, `tool_to_json`, `normalize_arguments`),
`deeppyro.llm.errors` (`LlmRequestError`, `LlmHttpError`, failure `CODE_*` constants),
`deeppyro.llm.retry` (`RetryPolicy`, `RETRYABLE_CODES`, `RETRYABLE_HTTP_STATUSES`).

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
* `model_ctx` comes from `create_provider`: the largest `contextWindow` among the
  provider's configured models (None when no model declares one).
* Timeout: `httpx.Timeout(timeout_s, connect=min(30, timeout_s))`; the client is created
  lazily on the first request and closed by `aclose()` (idempotent; a closed provider
  raises on further streams instead of silently opening a new socket).
* No prompt-cache/session affinity headers are sent yet: DSH's `x-session-affinity` /
  `prompt_cache_key` scheme is provider-specific and not required by the contract.
  `LlmRequest.session_id` is therefore currently unused (kept for a later cache-aware
  provider). OpenAI-compatible servers treat this as a plain cache miss, not an error.

## 3. Message conversion (core.wire -> chat messages)

Input is `core.wire.Message` as recorded in the session log, so the conversion mirrors
`fixtures/records/by-type/assistant_message.jsonl` / `tool_result.jsonl` shapes.

| wire Message | chat-completions message |
| --- | --- |
| `role="system"` / `role="developer"` | `{"role":"system","content":text}` (empty -> dropped) |
| `role="user"`, text only | `{"role":"user","content":text}` |
| `role="user"` with `ImagePart` | content array `[{"type":"text"},{"type":"image_url","image_url":{"url":...}}]` |
| `role="assistant"` | `{"role":"assistant","content":text_or_null,"reasoning_content":reasoning?,"tool_calls":[...]}` |
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
  provider streams, so history round-trips.
* `ImagePart.attachment` accepted forms: a ready `data:`/`http(s)://` URL, or a dict with
  `url`, or a dict with `data`/`base64` plus `mimeType`/`mediaType`. A bare attachment
  id cannot be dereferenced in this layer and is skipped (documented limitation; the log
  fixtures contain no image parts, so no real behaviour depends on it yet).
* Unsupported/unknown part types and messages that convert to empty content are dropped.

## 4. Streaming contract (INTERFACES §5.1-5.9)

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
  matching the interface snippet; `run(operation)` owns the loop.
* Retries stop as soon as content was produced: mid-stream failures never replay the
  request (the consumer has already seen deltas).

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

INTERFACES §5.8 says `ToolCallPart.arguments` (a JSON string) must be `json.loads`-ed into
`tool_calls[].function.arguments`. The OpenAI wire format requires `arguments` to remain a
*string*, and fixtures record it as stringified JSON (fixtures tool/call record), so the
literal "put an object there" reading would produce a request every OpenAI-compatible server
rejects. The agreed interpretation (confirmed with the lead) is: validate the string with
`json.loads`, then send the original bytes unchanged — no `json.dumps`, since re-encoding
alters key order/whitespace and shifts prompt-cache prefixes. Unparseable strings are a
client-side bug and fail fast with `LlmRequestError(code="BAD_REQUEST")`. Observable behaviour:
`arguments` is the same JSON text that was logged.

## 11. Known deviations / risks

* `reasoning_content` is only added when non-empty. DSH's adapter auto-enables
  `requiresReasoningContentOnAssistantMessages` for DeepSeek and then sends the field
  (possibly empty) on *every* replayed assistant message. Sending it only when present is
  accepted by DeepSeek today; if a strict server rejects replayed history, add an empty
  `reasoning_content` for DeepSeek models.
* No session-affinity/prompt-cache headers (§2) — cache hit-rate difference only.
* Anthropic-style endpoints (`web_deepseek_search_llm_request` fixtures) are out of scope.
* `create_provider` raises `ConfigError` for an unsupported `api`; `LlmRegistry.get` raises
  `ConfigError` for unknown provider names, whereas `Config.resolve()` raises `KeyError`.
  The LLM layer normalizes config mistakes to the `core.errors` hierarchy.
* Non-streaming `complete()` is intentionally not implemented (streaming-only project).

## 12. Test coverage (`uv run pytest tests/llm -q`)

* `test_sse.py`: framing, comments, multi-line data, CRLF, incremental/UTF-8-split bytes,
  flush without trailing blank line, `iter_sse`, `format_sse` round-trip.
* `test_messages.py`: system prepend/no-duplicate, developer->system, assistant
  text/reasoning/tool_calls, tool results (toolCallId + source.callId fallback), images,
  byte-for-byte argument preservation, invalid-arguments fast failure, tool schemas, payload
  fields/clamping.
* `test_retry.py`: backoff arithmetic, jitter, caps, Retry-After, retryability rules,
  exhaustion, cancellation, context manager, `from_policy` (dict + fixture policyKey +
  codes-authoritative + bad shapes), and fixture policyKey parity.
* `test_openai_compat.py` (respx-mocked, no network): plain text, reasoning deltas,
  fragmented single/multi tool calls, usage (+DeepSeek cache field), finish mapping,
  [DONE] inference, mid-stream cut, invalid JSON, wrong content-type, 500/429/401/400,
  stream error frames, empty-response retries, idempotent close, cancellation.
* `test_registry.py`: provider construction, api aliases, context-window selection,
  env api keys, caching, unknown provider, close-all ordering/errors.
* `test_fixtures.py`: real request/header tools, assistant/tool-result messages and
  byte-for-byte tool-call arguments from `fixtures/` convert to valid chat-completions JSON.
