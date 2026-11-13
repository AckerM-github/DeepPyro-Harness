# Image request budgets and attachment resolution (`deeppyro.llm`)

Status: implemented (owner: llm-dev, task-50 / P1 image stage 2b). Pure functions; no I/O,
no network, no import of `deeppyro.attachments`.

This module pair supplies the two provider-side pieces of image support:

* `deeppyro.llm.image_budget` -- how many oldest images a request must offload before it
  fits a route's count/byte budget (a direct port of DSH `dsh-llm/lib/index.js`).
* `deeppyro.llm.messages.image_part_url` -- resolving a durable attachment reference to an
  inline data URL at request time, without ever storing base64 in the session log.

The pieces that consume them live elsewhere: session projection replaces offloaded
`ImagePart`s with the placeholder text (`deeppyro.session`), and request assembly appends
`image/offload` records before deriving messages again (`agent/session.py`).

Source of truth for the algorithm: DSH `_dsh_src/dsh-llm/lib/index.js:647-649`
(`base64Length`), `:692-709` (`offloadedImagePrefixCount`), `:720-728`
(`requiredImageOffload`); `:581-585` (`offloadedImageText`) is *not* implemented here
because the placeholder belongs to the projection layer.

## 1. `ImageBudget`

```python
from deeppyro.llm.image_budget import ImageBudget

@dataclass(frozen=True, slots=True)
class ImageBudget:
    max_images: int = 600                    # DEFAULT_MAX_IMAGES
    max_bytes: int = 134_217_728             # 128 MiB, files route
    max_inline_bytes: int = 20_971_520       # 20 MiB, inline/base64 route
    offload_byte_quantum: int = 67_108_864   # 64 MiB removal step, files route
    inline_offload_byte_quantum: int = 10_485_760  # 10 MiB removal step, inline route
    offload_count_quantum: int = 20          # image-count removal step
```

All defaults are the DSH defaults (`dsh-llm-deepseek/lib/types/defaults.js`). `__post_init__`
validates the three removal quanta:

| field | must be | must not exceed |
| --- | --- | --- |
| `offload_byte_quantum` | `>= 1` | `max_bytes` |
| `inline_offload_byte_quantum` | `>= 1` | `max_inline_bytes` |
| `offload_count_quantum` | `>= 1` | `max_images` |

Violations raise `ValueError` (`"must be >= 1"` / `"must not exceed"`). A quantum larger
than its own limit would make every removal overshoot the budget, so it is rejected rather
than clamped. The dataclass is frozen and slotted: a budget is a value object.

## 2. `base64_length(n)`

```python
def base64_length(n: int) -> int:
    return 4 * ((n + 2) // 3)
```

Base64 encoding of `n` raw bytes, including padding: 0->0, 1->4, 2->4, 3->4, 4->8, 7->12,
100->136. Non-int values (including `bool`) raise `TypeError`; negative lengths raise
`ValueError`.
These are exactly the lengths an inline `image_url` payload adds to the request.

## 3. `offloaded_image_prefix_count(lengths, budget)`

`lengths` are represented byte lengths of the retained image occurrences, **oldest first**.
Only the files route limits are applied here (`max_images`, `max_bytes`,
`offload_byte_quantum`, `offload_count_quantum`); `required_image_offload` applies the
inline route separately.

```text
total        = sum(lengths)
excess_count = max(0, len(lengths) - max_images)
excess_bytes = max(0, total - max_bytes)
if both are zero: return 0
remove_count = min(len, ceil(excess_count / count_quantum) * count_quantum)
remove_bytes = ceil(excess_bytes / byte_quantum) * byte_quantum   # 0 when no byte excess
walk oldest first, removing; stop before adding an image when
    count >= remove_count  and  (remove_bytes == 0 or bytes_met)
where bytes_met = removed_bytes >= remove_bytes   if byte_quantum == 1
                  removed_bytes >  remove_bytes   otherwise
```

The asymmetric byte test is DSH's inclusive-only-at-quantum-one rule: with an exact
accounting quantum (`1`) reaching the target removes that prefix; with any coarser quantum
the removal must strictly pass the rounded target, so trailing quanta are not left half
consumed. The count test is always inclusive. The result is capped by the number of
occurrences (you cannot remove more than exist). Invalid lengths (non-int, `bool`,
negative) raise `ValueError`.

Examples:

| lengths | budget | result | why |
| --- | --- | --- | --- |
| `[4, 4]` | `max_images=2, max_bytes=8` | 0 | fits |
| `[5,5,5,5]` | `max_images=2` (count quantum 1) | 2 | count excess 2 |
| `[1]*30` | `max_images=20`, count quantum 20 | 20 | excess 10 rounds up to 20 |
| `[6, 6]` | `max_bytes=10`, byte quantum 1 | 1 | 6 >= 2 |
| `[10, 10]` | `max_bytes=10`, byte quantum 10 | 2 | 10 > 10 is false |
| `[100, 1, 1]` | `max_bytes=2`, byte quantum 1 | 1 | oldest first |
| `[4,4,4,4]` | `max_images=2, max_bytes=10` | 2 | count and bytes both met |

## 4. `required_image_offload(messages, budget, *, version_bytes=None)`

Returns how many additional oldest images the request must offload so it fits, or `0`
when it already fits. A positive result is a request to append an `image/offload` record
and rebuild the request -- never permission to silently drop an image.

Collection rules, in message order (the surface order used to build the request):

1. Walk every content part; only `type == "image"` parts count.
2. Skip occurrences already projected out: `offloaded is True` on a reference dict, or
   `model_extra["offloaded"] is True` on a pydantic `ImagePart` (core's `WireModel`
   allows extra fields, so the durable flag survives validation).
3. Length:
   * with `version_bytes`, its return value is used **as-is** and must be a non-negative
     `int` (otherwise `ValueError`). The callback is the caller's declaration of the exact
     represented request length of one occurrence; it receives the original `ImagePart`.
   * without it, a reference dict must carry `attachment["bytes"]` (the durable raw size)
     and the inline representation length `base64_length(bytes)` is used. A bare
     attachment id has no byte size, so it raises `ValueError` telling the caller to pass
     `version_bytes`.
4. Compute the inline route (`max_images`, `max_inline_bytes`, inline quantum) and the
   files route (`max_images`, `max_bytes`, files quantum) over the same lengths and return
   the larger count.

The dual route is deliberate: P1 only sends inline base64, but `ImageBudget` carries both
DSH caps, and returning the stricter of the two keeps either configured cap enforced. With
base64 lengths and the default budgets the inline cap (20 MiB) is far tighter than the files
cap (128 MiB), so in practice the inline route decides. The returned count is the prefix
length only; it is stable for identical history and identical budget, which is what makes
offload records deterministic across process restarts.

## 5. `image_part_url(part, *, attachments=None)`

```python
def image_part_url(part: ImagePart | dict[str, Any], *, attachments: Any = None) -> str | None
```

Precedence for the `attachment` value:

| attachment | result |
| --- | --- |
| string starting with `data:`, `http://`, `https://` | forwarded unchanged |
| dict with non-empty `url` | forwarded unchanged |
| dict with non-empty `data` or `base64` | `data:{mimeType|mediaType|image/png};base64,<data>` |
| dict reference + resolver | bytes read through resolver -> data URL |
| bare string id + resolver | optional `lookup(id)` -> ref, then bytes -> data URL |
| bare string id without resolver | `None` (old behavior: the part is skipped) |
| resolver missing/raising/empty bytes | `None` |

Resolver protocol (duck-typed; the attachments service implements it, but tests use fakes):

* `lookup(attachment_id) -> ref | None` -- optional, maps a bare durable id to its reference;
  exceptions are swallowed and treated as "no mapping".
* `read_image_bytes(ref) -> bytes` **or** `image_host_path(ref) -> Path` -- at least one;
  `read_image_bytes` wins when both exist. Non-bytes results and empty bytes degrade to
  `None`.
* MIME for resolved bytes: `mediaType` then `mimeType` on the reference, else
  `image/png`.

Every resolver failure degrades to `None` so a broken attachment can never become a provider
exception; the part is skipped exactly like an unresolvable bare id. Cancellation
(`asyncio.CancelledError`, a `BaseException`) is not swallowed. Resolution happens in
memory only: the input is a reference, the bytes are read from the attachment store's own
copy, and the data URL exists only for the duration of payload construction -- the durable
session log still stores references, never base64.

`message_to_chat` / `convert_messages` / `build_chat_payload` keep their existing
signatures and currently call `image_part_url(part)` without a resolver; see section 7.

## 6. Tests

`uv run pytest -o addopts= -q tests/llm/test_image_budget.py tests/llm/test_messages.py`

* `tests/llm/test_image_budget.py`: defaults, frozen, quantum validation, `base64_length`
  boundaries and type errors, prefix-count count/byte excesses, both quantum boundary rules,
  oldest-prefix ordering, cap at length, invalid lengths, `required_image_offload` zero and
  non-zero cases, oldest-first across messages, dict bytes -> base64, inline and files
  routes each triggering alone, `version_bytes` direct semantics and validation, skipping
  offloaded pydantic and dict parts, missing bytes error.
* `tests/llm/test_messages.py`: reference dict + resolver, bare id through `lookup`, bare id
  read directly, host-path-only resolver, declared MIME, plain dict part, lookup/read/host
  failures degrading to `None`, inline data and ready URLs bypassing the resolver, empty
  bytes, no resolver (unchanged legacy behavior).

There are no real image fixtures in `fixtures/`; all resolver tests use synthetic bytes.

## 7. Deviations and open risks

* **`version_bytes` semantics.** DSH's callback returns the *raw* version bytes and DSH
  applies `base64_length` itself when `budget.representation === "base64"`. This port uses
  the callback's return value directly as the represented request length (the task contract:
  the caller knows which route it is building). A caller that passes raw sizes for an inline
  route will under-count by the base64 expansion (~4/3 plus padding).
* **Single call covers two routes with one length set.** DSH calls `requiredImageOffload`
  once per route with route-specific `versionBytes`; here one length list feeds both the
  inline and files caps and the stricter count wins. With base64 lengths the files cap
  compares base64 bytes against a raw cap, which can offload earlier than strictly necessary
  -- conservative, never under-budget.
* **No resolver plumbing in request builders yet.** `message_to_chat`, `convert_messages`
  and `build_chat_payload` do not accept an attachments resolver, and `LlmRequest` has no
  attachments field, so a provider cannot resolve durable ids on its own today. Stage 3
  (request assembly) must either pass the resolver through these helpers or expand
  references before conversion; this module deliberately keeps the signature extension
  local to `image_part_url` per task-50's write scope.
* **No placeholder text.** `offloadedImageText` (DSH `:581-585`) belongs to session
  projection, which replaces the part with a `TextPart`; this module only counts.
* **Not re-exported from `deeppyro.llm`.** `llm/__init__.py` is outside task-50's write
  scope, so import from `deeppyro.llm.image_budget` / `deeppyro.llm.messages` for now.
* **`offloaded` relies on extra fields.** `core.wire.ImagePart` has no `offloaded` field;
  the projection layer must set it as an extra (allowed by `WireModel`) for the skip rule to
  see it. A plain dict part can carry it directly.
* **Attachment store is duck-typed.** No import of `deeppyro.attachments` (avoids a
  circular dependency and lets stage 2b land before stage 1 merges); the resolver shape is
  the contract, and tests use fakes.
