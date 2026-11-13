# Session query: read-only summaries, surfaces, events, traces

Status: implemented by session-dev (task-38, P1 C1/C2). Owns `src/deeppyro/session_query/**`,
`src/deeppyro/builtin/session_query.py`, `tests/session_query/**` and this document.
Contract source: `docs/plan/p1-context.md` §4; behavioural source: the real DSH session
reader plus the 30 real logs in `fixtures/sessions/`.

The service never appends to a session and never mutates a persisted record. It answers the
questions the TUI and future tools ask about persisted sessions: cheap summaries, validated raw
reads, model-visible surfaces, event listings and windows, literal event filters, session
ancestry, and positional replacement traces. It reuses the session package's public reader and
canonical surface fold, so a query never disagrees with what the model saw.

DSH semantics preserved: persisted-first (with an optional live provider consulted first), one
canonical surface fold per read (never a second ad-hoc fold), literal case-insensitive text
matching rather than full-text search, and no caller authorization (trusted infrastructure,
read-only APIs).

## 1. Public API

    from deeppyro.session_query import (
        CORRUPT_SESSION, EVENT_NOT_FOUND, INVALID_FILTER, INVALID_SURFACE, SESSION_NOT_FOUND,
        EventFilter, EventWindow, SessionQuery, SessionQueryError,
        SessionSummary, SessionTrace,
    )

    class SessionQuery:
        def __init__(self, store: SessionStore, *, read_window_max: int = 50,
                     live_provider: Callable[[str], Session | None] | None = None) -> None
        def list_summaries(self) -> list[SessionSummary]
        def get(self, session_id: str) -> Session
        def read_session(self, session_id: str) -> SessionRead
        def read_surface(self, session_id: str) -> list[Message]
        def list_events(self, session_id: str, *, types: Collection[str] | None = None,
                        surface: bool | None = None, after_seq: int = 0,
                        limit: int = 500) -> list[LogRecord]
        def read_event(self, session_id: str, seq: int, *, before: int = 2,
                       after: int = 2) -> EventWindow
        def filter_events(self, session_id: str, event_filter: EventFilter) -> list[LogRecord]
        def trace_session(self, session_id: str) -> SessionTrace
        def trace_event(self, session_id: str, seq: int) -> list[LogRecord]

    @dataclass(frozen=True, slots=True)
    class SessionSummary:
        id: str; path: Path; cwd: str; createdAt: int
        title: str | None; parent: str | None; origin: str | None
        is_seeded: bool; delegation_depth: int; agent_preset: str | None

    @dataclass(frozen=True, slots=True)
    class EventWindow:
        session_id: str; seq: int; event: LogRecord
        before: list[LogRecord]; after: list[LogRecord]

    @dataclass(frozen=True, slots=True)
    class SessionTrace:
        ancestors: list[SessionSummary]      # root first, direct parent last
        descendants: list[SessionSummary]    # pre-order DFS

    @dataclass(frozen=True, slots=True)
    class EventFilter:
        seq_from: int | None = None; seq_to: int | None = None
        types: tuple[str, ...] | None = None
        surface_only: bool | None = None; text: str | None = None

Extra helpers: `event_matches(record, event_filter)`, `event_text(record)`, `normalize_text`,
`scan_tail_title(path, *, max_bytes=TAIL_SCAN_BYTES)`, `KNOWN_EVENT_TYPES` and
`TAIL_SCAN_BYTES` (the bounded head/tail title window).

Plugin (`deeppyro.builtin.session_query`): `SessionQueryPlugin` (`provide="session_query"`,
`inject=["sessions"]`) wraps one `SessionQuery` built from the `sessions` service store and
delegates every method above; its config model is `SessionQueryConfig(readWindowMax: int = 50,
ge=1)`.

## 2. Summaries (`list_summaries`)

* Discovery uses `deeppyro.session.discover_session_files(store.root)`: flat `<id>.jsonl[.zstd]`
  first, then the nested DSH layout `<project>/<session>/session.v4.*`. The first file for a
  duplicate id wins.
* Each session costs one header read plus bounded head/tail scans for the newest non-empty
  `session/title` event; log bodies are never decoded. Files whose header fails validation are
  skipped. A log with a valid header but corrupt body is therefore listed with `title=None` and
  still reports `SESSION_QUERY_CORRUPT_SESSION` when opened.
* Sort order is `(createdAt, id)` descending, identical to `SessionStore.list`.
* Titles: DSH writes the first title after the first turn, whose runtime-context messages can
  exceed 100 KB, so a pure tail scan misses most real titles. The reader scans the last
  `TAIL_SCAN_BYTES` (256 KiB) first and falls back to the first 256 KiB. Titles updated in the
  middle of very long logs are outside both bounded windows and are only visible via
  `read_session`; zstd logs have no cheap random access, so one full decompression feeds both
  windows.
* `SessionSummary.path` points at the persisted log (flat or nested) and `is_seeded`,
  `delegation_depth`, `parent`, `origin` mirror the v4 header, so a forked session can be
  distinguished from an ordinary subagent session (fork copies records and seeds; a subagent
  session only records `parentSession`).

## 3. Reads

* `get` consults `live_provider` first (DSH live-first registry), then `SessionStore.open`.
  Failure is classified: an unlocatable id is `SESSION_QUERY_SESSION_NOT_FOUND`; a locatable but
  unreadable log is `SESSION_QUERY_CORRUPT_SESSION` with the `SessionError` preserved as
  `__cause__`.
* `read_session` returns `SessionRead(header, records, truncated, trimmed)` with a copy of the
  record list. Record objects are shared, not deep-copied: the reader treats them as immutable, so
  callers must not mutate a record in place. The list copy means appending cannot affect the store
  or the live session.
* `read_surface` is exactly `Session.derive_messages()` (canonical surface fold, tolerant for
  privacy-trimmed logs). A log that is readable but cannot fold (`INVALID_SURFACE`) never leaks a
  raw `SessionError`; the cause is attached.

## 4. Events

* `list_events` walks records in seq order. `after_seq` is an **exclusive** lower bound
  (`seq > after_seq`), matching DSH `afterSeq`; seq 0 is therefore excluded by default.
* `types` is an OR set; `None` keeps everything and an empty collection matches nothing.
  Unknown type names are rejected instead of silently matching nothing.
* `surface=True` keeps only `SURFACE_EVENTS` (`system/message`, `developer/message`,
  `user/message`, `assistant/message`, `tool/result`); `surface=False` is the complement.
* `read_event` returns the exact event plus up to `before`/`after` neighbours; each side is
  clamped independently to `read_window_max`, so a window request can never be used to dump an
  unbounded log. A missing seq is `SESSION_QUERY_EVENT_NOT_FOUND`; negative counts are
  `SESSION_QUERY_INVALID_FILTER`.
* `EventFilter` is conjunctive: `seq_from`..`seq_to` inclusive, `types` OR, `surface_only`,
  and a literal text scan. Text is whitespace-collapsed and lower-cased on both sides, and is
  matched with `in` (substring), like DSH event filters. An empty/whitespace-only `text` disables
  the text clause; `types=()` matches nothing.
* Text sources: message events use `Message.text() + Message.reasoning()`; `tool/call` uses
  `name + arguments`; `session/title` uses `data.title`; `assistant/attempt` uses only
  `data.text`/`data.message` strings; every other event contributes all string values in its
  `data` recursively. Keys are not searchable.
* Malformed filters fail closed at construction with `SESSION_QUERY_INVALID_FILTER`
  (negative or inverted seq range, non-collection/unknown types, non-bool `surface_only`,
  non-string `text`).

## 5. Tracing

* `trace_session` builds the parent graph from headers (cheap summaries, never records):
  `ancestors` is root-first with the direct parent last; `descendants` is a pre-order DFS of
  children, excludes the target, and is cycle-safe. Unknown ids are
  `SESSION_QUERY_SESSION_NOT_FOUND`.
* `trace_event` follows positional replacement chains: for every event carrying a
  `surfaceOp={"op":"replace","startSeq":a,"endSeq":b}`, each seq inside `[a,b]` (clamped to the
  last persisted seq) maps to the replacing event; later replacements win. Starting from `seq`,
  the chain ends at the newest event that is not itself replaced. The final event's
  `sourceEventSeqs` are appended one hop only (not recursively), de-duplicated, in ascending
  order.
* Verified examples: `session-ddb1ba3d` seq 8 → 50 (system prompt replacement) and
  `session-d745f174` seq 13 → 146 (Mnemon memory snapshot retired by an empty developer
  message). All nine fixture replacements end at a non-replaced event whose
  `sourceEventSeqs` are a subset of the chain.

## 6. Plugin and configuration

`SessionQueryPlugin` gets the store from `ctx.service("sessions")` (its `store` attribute, or
the service itself for simple fakes), so it never opens a second store. `readWindowMax`
resolution order:

1. global config section `[session_query]` or `[sessionQuery]` (Mapping or pydantic model);
2. the plugin row's `config.readWindowMax`;
3. default 50.

A non-positive or non-integer value raises `deeppyro.core.errors.ConfigError`. The plugin uses
`provide="session_query"` (snake_case) for Python service lookups; the planned camelCase
`sessionQuery` spelling was not adopted, and the global config section accepts both spellings.

## 7. Error codes

| code | when |
| --- | --- |
| `SESSION_QUERY_SESSION_NOT_FOUND` | id cannot be located anywhere in the store |
| `SESSION_QUERY_EVENT_NOT_FOUND` | `read_event`/`trace_event` seq absent (extra code, not in DSH) |
| `SESSION_QUERY_CORRUPT_SESSION` | log exists but the reader rejects it; cause preserved |
| `SESSION_QUERY_INVALID_SURFACE` | readable log whose surface fold is invalid |
| `SESSION_QUERY_INVALID_FILTER` | bad seq/count/types/text/filter argument |

All errors subclass `deeppyro.core.errors.SessionError` via `SessionQueryError` and expose
`.code` and `.detail`. The package-local subclass keeps the shared errors module untouched
(P1 decision 5).

## 8. Performance and compatibility boundaries

* Whole-log decodes happen only for `get`/`read_session`/`read_surface`/`list_events`/
  `read_event`/`filter_events`/`trace_*_event`; each call opens the log again unless a
  `live_provider` returns the in-memory session. There is no query cache, matching DSH's
  "persisted log is the source of truth" rule.
* `list_summaries` is guarded by a test that monkeypatches `read_session_file` to explode,
  proving the cheap path really is cheap. It reads at most a header plus two 256 KiB windows per
  session (the two windows may overlap for short logs).
* zstd logs are decompressed by frames; summary title scans decompress the whole stream because
  the newest title may live in any frame. Opening large (30 MB+) zstd sessions is bounded by the
  session package's frame reader, not duplicated here.
* No full-text index (P2), no cross-session search, no mutation, no authorization checks.
* Trimmed fixtures (`trimmed=True`) are handled exactly like the session package: surface folds
  run tolerant, `read_session.trimmed` is propagated, and replacement targets inside trimmed-away
  regions do not abort a surface read.
* The TUI `recent_sessions` wiring and the INTERFACES.md registration are owned by the Lead;
  this module and its plugin are ready for both.

## 9. Tests

    uv run pytest -o addopts= -q tests/session_query   # 41 passed
    uv run ruff check src/deeppyro/session_query src/deeppyro/builtin/session_query.py tests/session_query

Coverage: summary equivalence with `SessionStore.list` over all 30 fixtures and the no-full-decode
guard; zstd summaries and open; unknown/corrupt classification; live-provider precedence; raw
reads equal `read_session_file`; surfaces equal `derive_messages`; fold failures mapped to
`INVALID_SURFACE`; event counts/order/types/surface/after_seq/limits/windows/clamping; literal
filter case and whitespace, inclusive ranges, invalid filters; ancestry for forks and subagent
edges plus unknown ids; replacement chains for every fixture replacement; plugin defaults, global
and row config, invalid config, delegation, and a kernel mount when `deeppyro.builtin` imports.
