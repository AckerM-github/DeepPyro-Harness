# Native provider port (KI-010)

Status: design note for 0.3.0. Read-only deliverable; no source or test changes made.
Scope: remove the last v4 seam from the native loop — `src/deeppyro/agent/native_provider.py` is today the only native module importing `deeppyro.core.wire` (6 imports, `agent/native_provider.py:31-36`). This note specifies the native provider contract, the translation it replaces, the HTTP implementation plan, migration order, tests, and what it does **not** unlock (deleting `core.wire`). Authority: `docs/release/0.2.0-known-issues.md` KI-010; `docs/adr/0001-three-track-native-agent.md` §4 rule 1, §6.2 (L215), `docs/native/schema.md` §6.5 (L304-305). TUI migration is a separate prerequisite, documented in `docs/design/tui-native-migration.md` (§1 L15, §4 L105-109); v4 deletion needs both.

Non-goals: no v4 agent/session removal, no TUI work, no new runtime dependencies, no wire schema changes, no prompt-cache protocol headers (out of scope, §3.5).

## 1. Target native protocol

New module `src/deeppyro/core/llm.py` (~170 lines), importable without `core.wire` and without `core.protocols` (`core/protocols.py:16` imports wire, so importing protocols loads wire transitively — see `tests/integration/test_wire_boundary.py:1-11`). It imports only `core.messages`, `core.ids`, and the standard library, so the native footprint stays clean.

```python
class StopReason(StrEnum):          # native finish reasons, str-valued for session log fidelity
    STOP = "stop"; TOOL_CALLS = "tool_calls"; LENGTH = "length"
    ERROR = "error"; ABORTED = "aborted"; CONTENT_FILTER = "content_filter"

@dataclass(frozen=True, slots=True)
class Usage:                        # same fields as today; cached/cache-write/reasoning explicit
    input_tokens: int = 0; output_tokens: int = 0; total_tokens: int = 0
    cached_input_tokens: int | None = None; cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None

@dataclass(frozen=True, slots=True)
class ToolSpec:                     # replaces v4 ToolSchema (a WireModel) on the native path
    name: str; description: str = ""; parameters: dict = field(default_factory=dict)
    output: dict | None = None

@dataclass(frozen=True, slots=True)
class ModelRequest:
    provider: str; model: str
    messages: tuple[Message, ...]              # core.messages.Message, roles/parts already validated
    system: Message | None = None
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int | None = None; temperature: float | None = None
    context_window: int | None = None; session_id: str | None = None   # session_id = prompt-cache affinity key

# stream events (one union alias StreamEvent)
@dataclass(frozen=True, slots=True)
class TextDelta: text: str; index: int = 0
@dataclass(frozen=True, slots=True)
class ReasoningDelta: text: str; index: int = 0
@dataclass(frozen=True, slots=True)
class ToolCallStart: call_id: str; name: str; index: int = 0
@dataclass(frozen=True, slots=True)
class ToolCallArgsDelta: call_id: str; delta: str; name: str | None = None; index: int = 0
@dataclass(frozen=True, slots=True)
class UsageEvent: usage: Usage
@dataclass(frozen=True, slots=True)
class Finish: stop_reason: StopReason; message: str | None = None; code: str | None = None; replay_state: dict | None = None

class LlmProvider(Protocol):        # runtime_checkable; native mirror of the v4 seam
    name: str
    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]: ...
    async def aclose(self) -> None: ...
```

Call-level result types also live in `core/llm.py`, moved from `agent/native_provider.py:50-68`: `NativeToolCall(call_id, name, arguments)` (all `str`; `arguments` is the raw provider JSON fragment join), and `ModelResponse(text, reasoning, tool_calls, finish_reason: StopReason, usage: Usage, latency_ms: int, retries: int = 0, replay_state: dict | None = None)`. `retries` is new: `AgentLoop` today hardcodes `retry_count=0` in trace records (`agent/loop.py:626`) and `TurnUsage.retries` exists unused (`src/deeppyro/observability/protocols.py`), so the port must carry real retry counts into both (AI-native trace requirement).

Assembly invariants move verbatim into a wire-free helper `src/deeppyro/agent/model_call.py` (~130 lines):

```python
async def complete_model_call(provider: LlmProvider, request: ModelRequest, *,
    cancel: asyncio.Event | None = None,
    on_text: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None) -> ModelResponse
```

It preserves every current adapter rule (`agent/native_provider.py:136-244`): finish required; unknown/error finish → `ProviderStreamError`; tool-call name required; args joined, default `"{}"`; `finish_reason` forced to `tool_calls` iff calls exist and finish differed; tool_calls-finish with no calls → error; empty assistant message → error; cancel before/during → `RunCancelledError`; usage last-wins; latency measured around the call; provider `call_id` matched before stream index with `new_call_id()` fallback (`core.ids`); `asyncio.CancelledError` re-raised.

Error taxonomy (reusing `llm/errors.py`, which is wire-free): `LlmError`/`LlmRequestError` (`retryable`, `code`, `retry_after`, `failure()`) is raised by the transport before the first content token and is the only class `RetryPolicy` sees; mid-stream failures become `Finish(StopReason.ERROR, code, message)` and surface as `ProviderStreamError` in the loop (unchanged behavior, KI-010 boundary); cancellation is `asyncio.CancelledError` at the provider and `RunCancelledError` at the loop (`agent/errors.py:1-27`). Unknown finish, truncated stream and empty message stay `ProviderStreamError` — no new exception classes.

## 2. Mapping today → native

| What `native_provider.py` translates today | Current (v4/wire) landing | Native landing |
|---|---|---|
| role (system/developer/user/assistant/tool) | `to_wire_message`: `WireMessage.role` Literal | `ModelRequest.messages[i].role` (`core.messages` already validates the role/part matrix); chat payload role mapping in `llm/native_messages.py`: developer/system → `system` (first, only if no system-role message), user/assistant/tool verbatim |
| `TextPart` | `WireTextPart(text)` | user: `{"type":"text"}` part (or plain string when no images); assistant: `content` string; system: string; tool: string |
| `ReasoningPart` | `WireReasoningPart(text)` | assistant-only `reasoning_content` field; never sent for other roles (same as `llm/messages.py:203-253`) |
| `ToolCallPart(id,name,arguments)` | `WireToolCallPart` | assistant `tool_calls[{id,type:"function",function:{name,arguments}}]`; `arguments` passed through unmodified after a JSON-parse check (byte stability, `llm/messages.py:72-90`) |
| `ImagePart(attachment, media_type)` | `WireImagePart(attachment)` — **media_type is dropped today** (`native_provider.py:88`) | `llm/attachments.py` resolves attachment ref → data URL / http(s) passthrough; new path may pass `part.media_type` as fallback (strict improvement, same wire bytes) |
| `Message.id`, `source`, `tool_call_id`, `is_error` | wire `id`, `source=None`, `toolCallId`, `isError` | ids/sources stay native; provider payload uses `tool_call_id` for tool role and text content; ids included only in `prompt_hash` payload (they are today too, via `model_dump`) |
| 6 finish kinds (`FINISH_KIND_TO_NATIVE`, `build_request`) | `FinishKind` Literal, adapter map L38-45 | `StopReason` StrEnum; provider SSE `finish_reason` map copied from `llm/openai_compat.py:65-75` (stop/end_turn, tool_calls/tool_use/function_call, length/max_tokens, content_filter, error → StopReason) |
| usage | v4 `Usage(input,output,total,cached_input_tokens,cache_write_tokens,reasoning_tokens)` | same dataclass moved to `core/llm.py`; `core/protocols` re-exports it so 15+ v4 importers and `agent/budget.py` are untouched |
| request options | `LlmRequest(provider,model,messages,system,tools,max_tokens,temperature,context_window,session_id)` | `ModelRequest` fields 1:1; `tools` become `ToolSpec`; `context_window` still clips `max_tokens` (`llm/messages.py:269-287`) |
| `ContextPlan` (`agent/context_plan.py:1-134`) | messages native, but `tools: tuple[ToolSchema,...]` where `ToolSchema` is a WireModel (`core/protocols.py:26`) | `ContextPlan.tools: tuple[ToolSpec,...]`; token estimation unchanged (Text/Reasoning/ToolCall name+args/Image 256) |

The one-time observable change: `AgentLoop._prompt_payload` (`agent/loop.py:559-568`) currently serializes wire `model_dump(mode="json")`; the native serializer will dump `Message.to_dict()` (`core/messages.py`) plus `ToolSpec` JSON, changing `prompt_hash` values once. Only the hash **format** is asserted (`tests/native/test_loop.py:542`, `tests/agent/test_loop_usage.py:119`; `tests/observability/test_telemetry.py:224-235` is the v4 plugin path, not the loop), and `tests/fixtures/native/synthetic/mcp.json:338,356` carry literal `sha256:aaaa…` values — document as intentional, keep `HASH_RE`.

## 3. OpenAI-compatible / DeepSeek HTTP path

### 3.1 Modules and reuse
| New/edited | Purpose |
|---|---|
| `src/deeppyro/llm/native_provider.py` (~400) | `class NativeOpenAICompatProvider`: `stream(ModelRequest)`, `aclose()`, `list_models()`; mirrors `llm/openai_compat.py` |
| `src/deeppyro/llm/native_messages.py` (~210) | `Message`/`ToolSpec` → OpenAI chat payload; strict subset of `llm/messages.py:183-287` |
| `src/deeppyro/llm/attachments.py` (~120) | extracted wire-free image resolution from `llm/messages.py:93-180`; used by both message converters |
| `src/deeppyro/llm/http.py` (~70) | shared `build_client(timeout_s)` + `headers(api_key)`; `openai_compat.py:123-139` switches to it (small diff) so auth/TLS/timeout is not duplicated |
| reused unchanged | `llm/sse.py` (144, SSE decode), `llm/errors.py` (301, codes + `http_error`), `llm/retry.py` (189, `RetryPolicy`), `config.py` `ProviderConfig.resolve_api_key` (`config.py:75-81`) |

No new dependency: `httpx` is already the transport; `respx` is already a test dependency in `tests/llm`.

### 3.2 Request
POST `{base_url}/chat/completions` (verbatim if base_url already ends `/chat/completions`, `llm/openai_compat.py:116-121`), headers content-type json, accept `text/event-stream`, bearer key only when configured. Body: `{model, messages, stream: true, stream_options: {include_usage: true}, tools?, max_tokens?, temperature?}`; `max_tokens` clipped to `request.context_window or model_ctx` exactly as `llm/messages.py:269-287`; `tools` from `ToolSpec.parameters`, empty → `{"type":"object","properties":{}}`; system prompt becomes a leading `role=system` message only if none present. `session_id` is carried for trace/hash and reserved for future affinity (§3.5).

### 3.3 SSE/stream parsing
Split `aiter_bytes()` with `llm/sse.py:iter_sse`; skip empty `data`; `[DONE]` ends; malformed JSON → protocol error; `chunk["error"]` → normalized `LlmRequestError`. Delta extraction mirrors `llm/openai_compat.py:326-368`: `reasoning_content` then `reasoning` → `ReasoningDelta`; `content` → `TextDelta`; `tool_calls` indexed by `index` (fallback count), raw `id` overrides a pre-issued `new_call_id()`, `function.name` concatenated (name once set), `function.arguments` forwarded verbatim as `ToolCallArgsDelta`; `ToolCallStart` emitted once when name or args first appear. `finish_reason` = last non-null string. Usage chunk → `UsageEvent` before `Finish`; final `Finish` is always last.

### 3.4 Retries, timeouts, errors
Retry policy wraps only acquisition of the first stream event (`llm/openai_compat.py:176-201`; `docs/spec/llm.md` §retry; INTERFACES §5.9). `RetryPolicy` (max 3 attempts, exponential backoff) retries `{EMPTY_RESPONSE, RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT}`; 429/5xx map through `llm/errors.py:http_error` (retryable, `retry_after` honored). A failure before the first content token raises `LlmRequestError(retryable=True)`; after content it becomes `Finish(ERROR, code, message)`. Timeouts: `httpx.Timeout(timeout_s, connect=min(30.0, timeout_s))`, default 600 s from `create_provider` (`llm/registry.py:59-91`). Non-SSE content-type → protocol error, no retry (matches `llm/errors.py` codes; taxonomy table in `docs/spec/llm.md:225-255`).

### 3.5 Prompt-cache fields
Read `usage.prompt_tokens_details.cached_tokens`, else DeepSeek `prompt_cache_hit_tokens` (same as `llm/openai_compat.py:_usage_event`); populate `Usage.cached_input_tokens` → `RunResult.cached_input_tokens` (`agent/loop.py:127,327`), `TurnUsage.cached_tokens`, `ModelCallRecord.cached_input_tokens` (`agent/loop.py:628`), headless NDJSON `{"cached": …}` (`headless.py:1008-1016`) — no behavior change expected. Do **not** add `prompt_cache_key`/`x-session-affinity` headers in this port; `session_id` is already plumbed as the natural future key (`docs/spec/llm.md:53-75`).

### 3.6 Wiring and secrets
Only `ProviderConfig.resolve_api_key` (`config.py:75-81`) reads inline/env secrets; the native provider receives the resolved string, never reads env. Construction stays behind the plugin: add `create_native_provider(name, ProviderConfig, *, timeout_s=600.0)` to `llm/registry.py` reusing the `_OPENAI_COMPAT_APIS` predicate (`llm/registry.py:28-43`), and extend `builtin/llm.py`: `LlmPlugin.native_provider(name)` plus `add_native_provider(name, factory, *, plugin_id="", description="")` mirroring `add_provider` (`builtin/llm.py:39-58`) so third-party plugins can supply native providers and headless never imports the concrete class ("everything is a plugin"). Scope: `_OPENAI_COMPAT_APIS` only. If `api` is Anthropic (`llm/registry.py:46-53`), `native_provider` returns `None` (or raises a typed unsupported error) and the native path falls back to the legacy v4 loop with a session/trace fact; otherwise the native loop would silently lose Anthropic support it has today through the adapter.

### 3.7 Retry accounting
Extend `RetryPolicy.run(operation)` with optional `on_retry: Callable[[int, LlmError], None] | None` (~6 lines in `llm/retry.py`, named kwargs keep v4 callers intact). The native provider emits a `RetryScheduled(attempt, code, delay_s)` stream event before sleeping; `complete_model_call` counts them into `ModelResponse.retries`, and `AgentLoop` writes `ModelCallRecord.retry_count` (`agent/loop.py:626`) and `TurnUsage.retries` instead of the hardcoded 0.

## 4. Call-site migration order

1. `core/llm.py` types + unit tests; add it (and `agent/model_call.py`) to the wire-free footprint in `tests/integration/test_wire_boundary.py:26-39`.
2. `llm/attachments.py`, `llm/http.py`, `llm/native_messages.py`, `llm/native_provider.py` + respx tests against recorded SSE fixtures.
3. `agent/model_call.py` ported from the adapter invariants + tests; `core/protocols.py` re-exports `Usage` from `core/llm.py` (v4 importers untouched, wire import L16 stays until v4 exit).
4. `agent/context_plan.py` uses `ToolSpec`; `AgentLoop` constructor takes `LlmProvider` (the new one), drops `build_request`/`NativeProviderAdapter` imports (`agent/loop.py:59-62,171`), builds `ModelRequest`, uses the native `_prompt_payload` serializer (L559-568), keeps `_await_cancellable` (L535-557) and every existing error/finalize path (L296-312); populate `retries` in trace/usage.
5. `headless.py`: at `boot_runtime` (`headless.py:688-726`) build the native provider for the native path only; legacy `--legacy-v4` keeps the v4 `LlmProvider`; `team`/`subagent` managers (`headless.py:776-798`) stay on the v4 provider until they migrate; close both objects in `runtime.aclose` (`headless.py:667`).
6. Port `tests/native/replay.py` `ReplayProvider` (344 lines) to the native protocol, converting the same recorded JSONL (SSE chunks → native events); fixtures under `tests/fixtures/native/recordings/` stay byte-identical, so the 10 recorded differential cases (`tests/native/test_differential.py`) stay green.
7. Delete `src/deeppyro/agent/native_provider.py`; shrink `AGENT_WIRE_ALLOWLIST` (`tests/integration/test_wire_boundary.py:43-55`) and the explicit seam assertion (L111-121); no other production file changes.
8. TUI (`tui/runtime.py:88,110`) and v4 exit stay out of this milestone; TUI provider selection flips when the TUI migration lands.

What remains on v4 `LlmProvider`/`LlmRequest`/`StreamEvent` until v4 deletion: `compact/compactor.py`, `team/manager.py`, `subagent/manager.py`, `builtin/session_title.py`, `eval/live.py`, `tui/runtime.py` and the v4 agent path; plus `ToolSchema` for the `tools.registry`/v4 request path. Keep them compiling unchanged.

The 7 synthetic differential cases (`image, approval, compaction, mcp, ptc, subagent, handoff`) are provider-agnostic: `tests/native/synthetic.py` drives `session.derive`+`fold` directly and never constructs `AgentLoop` or an `LlmProvider`. They stay green untouched **iff** step 4 does not alter `session/schema.py` event shapes and fixtures under `tests/fixtures/native/synthetic/*.json` stay byte-identical. Add a CI guard that `git diff --exit-code tests/fixtures/native/` is empty for the port PR.

## 5. Test strategy, risks, change map

### 5.1 Tests (all offline; no network)
- `tests/llm/test_native_messages.py` (~200): request-body goldens — system prepend, developer role, text/reasoning, image data URL from bare attachment + dict refs, tool calls with raw arguments, max_tokens clipping, empty parameters object.
- `tests/llm/test_native_provider.py` (~450) using `respx` + recorded SSE fixtures `tests/fixtures/native/llm/*.sse` (~12 files): text/stop; reasoning+text; fragmented tool_calls with late `id`; tool_calls without content; usage with `prompt_tokens_details.cached_tokens` and with DeepSeek `prompt_cache_hit_tokens`; mid-stream `error` frame; 429 then success; 500 then success; timeout then success; non-SSE content type; truncated stream (no finish/`[DONE]`); empty stream; 401 mapping. Retry assertions count attempts and delays using `RetryPolicy(sleep=fake_sleep)` (`llm/retry.py:41-65`) and `on_retry`.
- `tests/agent/test_model_call.py` (~180): every assembly invariant from §1 (finish required, unknown finish, tool name missing, args join default, finish inference both ways, empty message, cancel-before/during, usage last-wins, latency, retry count).
- Native loop tests: update `tests/native/test_loop.py` provider fakes to the native protocol; assert `cached_input_tokens` still flows to `RunResult`/usage sink/trace and `retry_count` is populated; keep `HASH_RE` assertions, refresh the one-time hash expectation.
- Differential: ported `ReplayProvider`; run `tests/native/test_differential.py`; keep `tests/native/test_differential_synthetic.py` unchanged and green.
- Boundary: `tests/integration/test_wire_boundary.py` — footprint `core/llm.py`, `agent/model_call.py`, `llm/native_*.py`; allowlist loses `native_provider.py`; extend the AST scanner test so no native module imports `core.protocols` (transitive wire).
- Commands: `.venv\Scripts\python.exe -m pytest tests/llm tests/native tests/integration/test_wire_boundary.py`, then full `pytest`; `ruff check src tests`; `ruff format --check src tests`; `scripts/typecheck.py` (pyright baseline 79 errors, ratchet must stay 0 new).

### 5.2 Risks
| Risk | Mitigation |
|---|---|
| `prompt_hash` values change once (`agent/loop.py:559-568`) | Documented intentional change; tests assert format only; note in changelog; hash algorithm unchanged |
| Anthropic configured provider loses native loop | Explicit fallback to `--legacy-v4` loop with a log/session fact; config test; follow-up native Anthropic transport |
| Adapter invariants drift while moving to `agent/model_call.py` | Port invariant tests first, byte-for-byte expectations; differential recordings as backstop |
| Image parity (`media_type` dropped today at `native_provider.py:88`) | Extract `llm/attachments.py` from `llm/messages.py:93-180` with existing tests; native path may use `part.media_type` fallback, same bytes |
| HTTP duplication / secret handling | Shared `llm/http.py`; `ProviderConfig.resolve_api_key` remains the only secret reader; `llm/openai_compat.py` refactor covered by existing tests |
| Retry accounting churn in shared `RetryPolicy` | Optional keyword-only `on_retry` hook, default None; v4 tests unchanged |
| Plugin boundary erosion (headless imports concrete class) | Construction only via `LlmPlugin.native_provider/add_native_provider`; test that `headless.py` imports no `llm.native_provider` |
| Typecheck ratchet | New modules pyright-clean; run `scripts/typecheck.py` per step |

### 5.3 File-by-file change map (approx. lines; "+" new/changed, "−" removed)
| File | Change | Size |
|---|---|---|
| `src/deeppyro/core/llm.py` | new: StopReason/Usage/ToolSpec/ModelRequest/events/Protocol/ModelResponse/NativeToolCall | +170 |
| `src/deeppyro/agent/model_call.py` | new: `complete_model_call` adapter invariants | +130 |
| `src/deeppyro/llm/native_messages.py` | new: core.messages → chat payload | +210 |
| `src/deeppyro/llm/native_provider.py` | new: SSE/HTTP native provider | +400 |
| `src/deeppyro/llm/attachments.py` | new: extracted image resolution | +120 |
| `src/deeppyro/llm/http.py` | new: shared client/headers | +70 |
| `src/deeppyro/agent/native_provider.py` | delete seam | −260 |
| `src/deeppyro/agent/loop.py` | native request/payload, retries, imports | +35/−25 |
| `src/deeppyro/agent/context_plan.py` | ToolSpec instead of ToolSchema | +10/−10 |
| `src/deeppyro/core/protocols.py` | re-export Usage from core.llm | +2/−2 |
| `src/deeppyro/llm/registry.py` | `create_native_provider` | +25 |
| `src/deeppyro/llm/retry.py` | optional `on_retry` hook | +6 |
| `src/deeppyro/llm/openai_compat.py` | use `llm/http.py` | +8/−20 |
| `src/deeppyro/llm/messages.py` | use `llm/attachments.py` | +5/−90 |
| `src/deeppyro/builtin/llm.py` | native provider service methods | +30 |
| `src/deeppyro/headless.py` | select native provider on native path; close it | +25 |
| `tests/native/replay.py` | ReplayProvider → native protocol | +80/−60 |
| `tests/integration/test_wire_boundary.py` | footprint/allowlist/seam guard | +10/−8 |
| `tests/llm/test_native_messages.py` | new | +200 |
| `tests/llm/test_native_provider.py` | new | +450 |
| `tests/agent/test_model_call.py` | new | +180 |
| `tests/native/test_loop.py`, `test_headless_native.py` | fakes/expectations | +60/−30 |
| `tests/fixtures/native/llm/*.sse` | new recorded fixtures | ~12 files |

Net source ≈ +810/−405; tests/fixtures ≈ +980/−90. No new runtime dependencies.

## 6. Can `core.wire` be deleted after this port?

**No.** The port removes `agent/native_provider.py` (6 imports), leaving **38 files / 39 import statements** still importing `deeppyro.core.wire` in `src` (today: 39 files / 45 statements — measured by `grep -rn "from deeppyro.core.wire" src`; KI-010 records the same 39/45, ADR §6.2 L215 tracks the seam itself). Grouped:

- **v4 session stack, 14 files / 15 imports**: `session/writer.py:23`, `store.py:19`, `session.py:13`, `reader.py:20`, `metadata.py:20`, `fork.py:16`, `events.py:17`, `derive.py:17`, `image_projection.py:26`; `session_query/filters.py:15`, `query.py:27`, `types.py:15`; `projection/registry.py:33`; `migrate.py:46,47` (v4 reader).
- **v4 agent surface, 8 files / 8 imports**: `agent/images.py:26`, `inbox.py:31`, `request.py:17`, `session_records.py:9`, `session_state.py:16`, `session_stream.py:25`, `session_support.py:13`, `session_tools.py:20`.
- **v4 capabilities, 11 files / 11 imports**: `compact/tokens.py:7`, `surface.py:25`, `refs.py:23`, `compactor.py:35`, `compress_tool.py:38`; `goal/round_driver.py:56`; `team/manager.py:27`; `subagent/manager.py:25`; `builtin/inspect.py:22`; `builtin/session_title.py:31`; `prompt/projection.py:25`.
- **llm v4 converters, 3 files / 3 imports**: `llm/messages.py:30`, `llm/messages_anthropic.py:31`, `llm/image_budget.py:37` (needed by `AnthropicProvider` and v4 budget path).
- **re-exports, 2 files / 2 imports**: `core/__init__.py:36`, `core/protocols.py:16` (`LlmRequest` carries wire `Message`; imported by 59 src files mostly for Tool types).
- **tests** (outside the src count): `tests/native/synthetic.py`, the 10-recording differential fixtures/replay, and ~24 test modules importing v4 stream/Usage types.

The native footprint itself is already wire-free (12 modules, `tests/integration/test_wire_boundary.py:26-39`); everything above is the v4 track that survives 0.3.0. Deleting `core.wire` additionally requires: (a) v4 session format retirement (`session/events.py`, `derive.py`, readers/writers, `migrate.py`), (b) v4 capability port (compact/team/subagent/goal/title/inspect/projection), (c) v4 LLM converters removal after `AnthropicProvider`+v4 loop deletion, (d) dropping `core.protocols` wire re-export and the 57 DSH record types (`core/wire.py`), (e) TUI migration first (`docs/design/tui-native-migration.md:15,105-109`). KI-010 stays open after this port with the reduced, enumerated scope above; the port is a prerequisite, not the exit.
