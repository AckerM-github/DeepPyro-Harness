# Image offload projection (`deeppyro.session.image_projection`)

Status: implemented (P1 image stage 2a, owner: loop-dev). Pure, read-only
projection over durable session records. It never writes the session log and
never touches image bytes: it only decides which model-visible message content
blocks render as text placeholders instead of image blocks.

Modules:

| module | contents |
| --- | --- |
| `src/deeppyro/session/image_projection.py` | placeholder text constants, `image_identity`, `offloaded_image_text`, `project_image_offloads`, `apply_image_offloads` |
| `src/deeppyro/session/derive.py` | applies the projection inside `derive_messages()` |
| `tests/session/test_image_projection.py` | 35 tests: text parity, apply/project semantics, derive integration, real-session round trip |

Related docs: `docs/spec/session.md` (log, surface fold, trimmed sessions),
`docs/plan/p1-tools-runtime.md` section 4.4 (stage 2 design),
`docs/spec/llm.md` (wire image parts sent to providers).

## 1. Why this is a projection, not a message mutation

The durable log never stores image bytes or provider URLs. A user message
contains an `ImagePart` whose `attachment` is an `ImageAttachmentRef`
(`attachmentId`, `mediaType`, `bytes`, `width`, `height`, optional `name`).
DeepSeek routes cap how many images a single request may carry, so a request may
have to omit the oldest image occurrences. That decision is durable but is *not*
part of the visible conversation: it is recorded as a separate
`image/offload` record and replayed by a pure interpreter when messages are
derived for the model. Consequences:

- replaying the same log under a different provider budget can offload a
  different prefix without rewriting history;
- re-attaching the image (or a later provider accepting it) needs no session
  rewrite: the durable message is intact, only the projection changes;
- the TUI / exporter can still show "an image was here" because the durable
  message still carries the image part.

`image/offload` is a known projection event
(`deeppyro.session.events.PROJECTION_EVENTS`) and is never a surface node:
`fold_surface()` returns no node for it and
`plan_surface_transition()` returns `(SKIP, None, None)`.

## 2. Durable record shape

```json
{
  "type": "image/offload",
  "seq": 42,
  "time": 1763000000000,
  "data": {
    "targets": [
      {"seq": 17, "imageIndexes": [1]},
      {"seq": 23, "imageIndexes": [0, 2]}
    ]
  }
}
```

- `targets` is an array of `{seq, imageIndexes}` entries.
- `seq` is the session seq of an earlier surface message node (normally a
  `user/message`) whose content carries image parts.
- `imageIndexes` are indexes into that message's `content` array; only
  indexes that point at an `ImagePart` have an effect, other indexes are
  ignored (a message may change shape only through a surface replace, and a
  stale index must not corrupt derivation).
- Every record may name several targets, and several records may name the same
  target seq; index sets are **unioned**, so re-applying is idempotent.
- A target seq that is no longer on the folded surface (replaced or absent) is
  simply never looked up; it is not an error.

`Session.append` accepts `image/offload` without payload validation (the log
is a faithful record of what happened). Validation therefore belongs to
derivation: `strict=True` is the default and is what `Session.derive_messages()`
uses for complete logs; `strict=False` skips malformed records and is used for
trimmed fixture logs.

## 3. Public API

```python
from deeppyro.session.image_projection import (
    IMAGE_OFFLOAD_EVENT,          # "image/offload"
    OFFLOADED_IMAGE_PREFIX,       # "[image omitted to fit request image limits; "
    OFFLOADED_IMAGE_SUFFIX,       # " No local normalized image path is available; ..."
    image_identity,               # (ref: Mapping[str, Any]) -> str
    offloaded_image_text,         # (ref: Mapping[str, Any]) -> str
    project_image_offloads,       # (records, *, strict=True) -> dict[int, set[int]]
    apply_image_offloads,         # (message, indexes: AbstractSet[int]) -> Message
)
```

`project_image_offloads` returns `{target_seq: {content_index, ...}}` and is
purely structural: it looks only at `record.type` and `record.data`.
`apply_image_offloads` returns a **new** `Message` (`model_copy`); the input
message is never mutated, so derived messages remain safe to cache/share.

`derive_messages(records, *, strict=True)` performs, in order:

1. fold the surface (append/replace semantics unchanged);
2. `project_image_offloads(records, strict=strict)`;
3. for each surface node in order: build its `Message`
   (`event_message`) and, when that node's seq has offloads, pass it through
   `apply_image_offloads`. Nodes that derive to `None` are skipped as before.

No other derivation behaviour changed.

## 4. Placeholder text: byte parity with DSH

The placeholder is DSH's *no-access* variant, copied byte for byte from
`@deepseek-ai/dsh-llm/lib/index.js` (`imageIdentity` /
`offloadedImageText` called without an access path):

```text
[image omitted to fit request image limits; <identity>. No local normalized image path is available; ask the user to attach it again if needed.]
```

Identity rules (`image_identity`):

| ref | identity |
| --- | --- |
| `{"attachmentId": "att-1"}` | `att-1` |
| `{"attachmentId": "att-1", "name": "photo.png"}` | `"photo.png" (att-1)` |
| `{"attachmentId": "att-1", "name": "截图.png"}` | `"截图.png" (att-1)` |
| `{"attachmentId": "att-1", "name": null}` | `att-1` |
| `{"attachmentId": "att-1", "name": ""}` | `att-1` |

The display name is quoted with JSON rules, so embedded quotes and control
characters are escaped exactly as JS `JSON.stringify` does. Python's
`json.dumps(..., ensure_ascii=False)` matches `JSON.stringify` for non-ASCII
names (both keep the characters raw); plain `json.dumps` would emit
`\uXXXX` escapes and break byte parity.

A bare string attachment (legacy/foreign log, no ref object) falls back to
`{"attachmentId": str(attachment)}` so derivation stays total.

## 5. Strict and tolerant modes

Strict mode rejects, with
`SessionError("invalid image/offload record at seq <seq>: <detail>")`:

- `data.targets` missing or not an array;
- a target that is not an object;
- `seq` missing, negative, or not an integer (booleans are not integers here);
- `imageIndexes` missing or not an array, or an element that is negative/not
  an integer.

Tolerant mode skips the offending record (earlier and later valid records still
apply), which keeps trimmed session files readable. It never raises for a bad
`image/offload` payload.

## 6. Limits of stage 2a (not implemented here)

This stage is the pure interpreter only. The following remain outside it; the
upgrade path is additive and does not change this module's public API:

- **attachment store**: durable attachment records and the resolver from
  `attachmentId` to local normalized image bytes;
- **offload *selection***: request image budget accounting, oldest-first
  selection, the `IMAGE_OFFLOAD_REQUIRED` error and the compaction/retry loop
  that writes `image/offload` records;
- **access-path variant**: DSH can append "path: ..." instead of the no-path
  sentence when a normalized local file is available. Today only the no-path
  constant is implemented; `offloaded_image_text(ref)` is the seam for a future
  `access` argument, matching DSH's optional parameter;
- **writer**: `AgentSession` does not yet emit `image/offload` records;
  nothing in the current loop needs it;
- **request serialization**: `deeppyro.llm.messages` converts image parts to
  provider payloads; it does not consult this projection yet (it already skips
  attachment ids it cannot resolve).

## 7. Evidence and tests

No fixture session contains an `image/offload` record (grep over
`fixtures/sessions` and `fixtures/records`: zero hits), so the text
constants and the record shape were reverse-engineered from the DSH sources:
`dsh-commands/lib/typert.host.js` (`ImageOffloadTarget`,
`SessionEventMap['image/offload']`), `dsh-session/lib/known-event-types.js`
(`MESSAGE_PROJECTION_EVENT_TYPES`), and `dsh-llm/lib/index.js`
(`imageIdentity`, `offloadedImageText`). Because the projection is a no-op
when no record is present, all existing fixture-derived behaviour is unchanged.

`tests/session/test_image_projection.py` covers: exact constants; identity
without a name; JSON quoting (including embedded quotes and non-ASCII names);
missing `attachmentId`; apply-selected-only; input not mutated; bare-string
fallback; out-of-range/non-image indexes; project single/union/multi-target/empty;
strict rejection of 10 malformed shapes; tolerant skipping; derive integration
(single node, several messages, off-surface target, non-image index, duplicate
record idempotence, not-a-surface-node); real `SessionStore` append/reopen
round trip and strict/tolerant derive.

## 8. Deliberate deviations from DSH

- **Empty / null names collapse to the attachment id.** DSH tests only
  `ref.name === undefined`; an empty string would render as `"" (att-1)`.
  The task contract for this port requires empty/None names to be treated as
  absent, so the port normalizes them. This is the only text deviation.
- **Malformed payloads are a strict derivation error** instead of being trusted
  from the writer; trimmed logs can opt into tolerant mode.
- **Unknown keys in `data` are ignored** (forward-compatible with future DSH
  fields), matching the repo-wide `WireModel(extra="allow")` policy.
