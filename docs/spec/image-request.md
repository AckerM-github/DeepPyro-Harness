# Image request assembly (agent loop, P1 stage 3)

Status: implemented in `src/deeppyro/agent/images.py` and wired into
`AgentSession._turn_body` (`src/deeppyro/agent/session.py`). Tests:
`tests/agent/test_images.py` (pure planning) and
`tests/agent/test_agent_images.py` (loop integration).

## Problem

Stage 2a (see [image-offload.md](image-offload.md)) makes `derive_messages`
apply durable `image/offload` records: an offloaded occurrence becomes the
"no local path" placeholder text, everything else stays an attachment reference.
That is exactly what the durable session log wants -- image bytes never enter the
log -- but a provider request needs either an inline data URL or block-text, never
a bare reference.

Stage 3 bridges the two directions for each provider request:

1. fold the surface and apply the existing offloads (stage 2a);
2. expand resolvable references to inline data URLs **in memory only**;
3. measure every image occurrence against the route `ImageBudget`;
4. if the budget is exceeded, choose the oldest occurrences to offload, persist
   **one** durable `image/offload` record with their targets, and hand the
   provider the messages where those occurrences are already placeholders.

The durable log therefore still contains only references; a replay under a
different budget derives different request messages, but the durable history is
unchanged.

## Pipeline (one per step)

`plan_request_images(records, *, attachments, budget=None)`:

1. `surface_messages_with_seq(records)` -> `[(record_seq, message)]` from
   `deeppyro.session.fold_surface` + `event_message`, applying
   `project_image_offloads` (or an explicit `offloads` mapping) through
   `apply_image_offloads`.
2. `expand_image_attachments(messages, attachments)`: copies each message and
   replaces `ImagePart.attachment` refs that resolve through
   `deeppyro.llm.messages.image_part_url(part, attachments=resolver)` with
   `{"url": "data:<mime>;base64,<payload>"}`. Inputs are never mutated.
   Unresolvable refs, ready `data:`/`http(s):` URLs and dict shapes with
   `url`/`data`/`base64` are left unchanged. Duplicate occurrences of one
   reference are resolved (bytes read) once per call.
3. Measure each `ImagePart` of the expanded messages ((`_inline_length`):
   - expanded/inline data URL: length of the base64 payload after the comma;
   - `{"data"|"base64": ...}` dict: string length;
   - unresolved reference dict with `bytes`: `base64_length(bytes)`;
   - bare id / `http(s)` URL / unknown: 0.
4. `required = required_image_offload(messages, budget, version_bytes=...)`
   from `deeppyro.llm.image_budget` (the stricter of the count route, the file
   byte route and the inline byte route; already-offloaded occurrences are
   skipped by the projection because they are text by then).
5. If `required > 0`: select the oldest `required` occurrences in model order,
   grouped per record seq -> `{"seq": int, "imageIndexes": [int, ...]}`
   (content-list positions, exactly what stage 2a's `apply_image_offloads`
   interprets). The returned messages keep inline data URLs for everything else
   and take the placeholder **text** for selected occurrences from the durable
   reference (not from the expanded URL), so the model sees the attachment id /
   display name rather than a base64 blob.
6. `ImageRequestPlan.record_data()` returns `{targets: [...]}` (or `{}`).

## Session integration

`AgentSession._turn_body` replaces the direct request build with
`_plan_request_images()`:

```python
request = self._build_request(self._plan_request_images())
request = await self._request_hook(request, turn, step)
```

`_plan_request_images()`:

- reads `self._records()`;
- `attachments = self._services.get("attachments")` (the `attachments` service
  mounted by the builtin plugin, see `src/deeppyro/builtin/attachments.py`);
- `budget = self._services.get("image_budget")` if present, else
  `getattr(self._provider, "image_budget", None)`, else the module default
  `ImageBudget()`;
- appends `image/offload` **once** when `plan.record_data()` is non-empty;
- returns `plan.messages` to the request builder.

The append happens after the step's prompt/runtime-context/request records
(`system/message`, `user/message`, `request/header`, `request/context`) and
after automatic compaction, but before `assistant/message` (the model has not
streamed yet). The target seqs are unchanged by the append, so the record is
valid for later derivations.

No constructor argument was added: the budget override rides on the existing
`services` dict (and, as a fallback, on the provider object), which keeps the
public `AgentSession` signature stable.

## Durable record shape

```json
{
  "type": "image/offload",
  "seq": 17,
  "time": 1763000000000,
  "data": {"targets": [{"seq": 3, "imageIndexes": [0, 1]}]}
}
```

- `image/offload` is a projection event, not a surface event: it must not carry
  `surfaceOp` or `sourceEventSeqs` (`Session.append` rejects them on
  non-surface types), and it never becomes a surface node.
- `seq` is the seq of a durable `user/message` node that is still on the
  surface; `imageIndexes` are positions in that message's `content` list.
- One record per request that needs an offload, covering all selected targets.
  Multiple records for the same node are allowed and union (idempotent
  projection); the planner always emits a single record.
- Targets whose node is no longer on the surface are ignored by the projection.

## Semantics and invariants

- **Durable log stays base64-free.** Only `seq`/index targets are persisted; the
  data URLs exist solely in the `LlmRequest.messages` handed to the provider.
- **Idempotent**: once the record is appended, the next derivation already shows
  the placeholders, so the next plan measures fewer images and does not write a
  second record for the same occurrence.
- **Oldest first**: selection is in model order (surface order), matching DSH's
  "oldest occurrence(s) must be offloaded" policy. Grouping preserves content
  order within a node.
- **Placeholder identity**: the text is the stage-2a no-path variant
  `[image omitted to fit request image limits; <identity>. No local normalized image path is available; ask the user to attach it again if needed.]`,
  where `identity` is the durable attachment id (or `JSON.stringify(name) + " (" + attachmentId + ")"`).
- **Single measurement source**: `version_bytes` returns the exact base64 length
  of the bytes that go on the wire, so the count/byte prefix selection matches
  what the provider receives.
- **No attachments service -> no change**: text-only hosts, SDK tests and
  `attachments=None` keep the old behaviour: references are passed through and
  nothing is persisted. This also means malformed/legacy sessions without an
  attachment store never fail here.

## Attachment resolver adaptation

`deeppyro.llm.messages.image_part_url` duck-types the resolver
(`read_image_bytes(ref)` or `image_host_path(ref)`, optional
`lookup(attachment_id)`), and the wire stores either a bare attachment id string
or the reference dict from the log. The real `AttachmentStore` methods are typed
(`ImageAttachmentRef`), so `agent.images` wraps the store in a small adapter:
it calls the store with the wire-shaped ref first and, if that raises, maps the
ref through `lookup(attachmentId)` and retries once. Resolvers that already accept
wire-shaped refs keep getting exactly that object. Failures degrade to "unresolved"
so one broken attachment can never fail a request.
`llm/messages.py` signatures and payload shapes are untouched.

## Deviations / P1 limits

- **Proactive budgeting, not error-driven.** The request is measured before it is
  sent; the `IMAGE_OFFLOAD_REQUIRED` error/retry path from DSH is out of scope,
  so an underestimated remote (http) image simply costs 0 in this planner.
- **No local normalized path variant.** P1 has no files API and no TUI upload, so
  only the no-path placeholder is produced (`normalizedAccessText` variant not
  implemented).
- **Inline data URL only.** No files route, no provider URL upload, no attachment
  serving endpoint; `http(s)` URLs are forwarded unchanged and count as 0 bytes.
- **Provider budget is not yet surfaced.** `OpenAICompatProvider` does not expose
  `image_budget`; until `llm` route config does (or a host passes
  `services["image_budget"]`), the `ImageBudget()` defaults apply to every route.
- **Legacy fallback.** Without an attachment service the durable refs are sent to
  the provider as-is, exactly as before stage 3; hosts are expected to mount the
  builtin `attachments` service in normal operation.
- **Image bytes are read once per plan per unique reference** (not cached across
  steps); the attachment store is expected to be fast/local.
- **Compaction and images are independent**: compaction summaries never include
  image payloads; offloaded targets in compacted regions disappear with their
  nodes and are ignored.

## Out of scope (later stages)

- TUI drag/drop, clipboard and IM image intake; files API and attachment serving.
- Provider route constants (150M-vs-20M byte budgets etc.) wired from
  `[llm]` config; DSH's `IMAGE_OFFLOAD_REQUIRED` retry/compaction loop.
- Image lifecycle GC and attachment deletion.

## Test coverage

- `tests/agent/test_images.py`: seq pairing, stage-2a offload application,
  explicit offload mapping, off-surface targets, expansion copying/no mutation,
  duplicate-ref single read, typed-store lookup adapter, ready URLs, unresolvable
  refs, budget fit/trigger/grouping/oldest-first, declared-byte fallback,
  idempotency, strict malformed record propagation, JSON-native payloads,
  base64-free record data.
- `tests/agent/test_agent_images.py`: request carries data URLs while the log
  keeps refs; budget exceeded -> exactly one `image/offload` record with correct
  `seq`/`imageIndexes`, appended after `request/context` and before
  `assistant/message`; placeholder text for the offloaded image and inline URL for
  the retained one; no duplicate record across tool steps; no attachments service
  -> refs untouched; provider `image_budget` fallback.
