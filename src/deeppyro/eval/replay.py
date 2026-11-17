"""Deterministic offline replay of the recorded native fixtures.

The fixtures under fixtures/native record real provider streams.  Replay never
talks to a network: it parses each JSONL recording, re-assembles assistant
responses from the recorded SSE chunks, and checks that the reassembled content,
tool calls, finish reason and usage match the recorded aggregate response.  The
manifest hashes are verified too, so an accidental fixture edit fails loudly.

The report is a pure function of the fixture bytes: cases are sorted by case id,
every check has a stable name and failure details list at most the first three
problems, which keeps CI output reproducible.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from deeppyro.eval.errors import ReplayError
from deeppyro.llm.sse import decode_sse_text

MANIFEST_FILENAME = "manifest.json"
RECORDINGS_DIRNAME = "recordings"
DONE_PAYLOAD = "[DONE]"
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
KNOWN_LINE_TYPES = frozenset(
    {
        "case",
        "llm.request",
        "llm.response.start",
        "llm.sse",
        "llm.response.end",
        "tool.call",
        "tool.result",
        "case.end",
    }
)
CASE_CHECK_NAMES = (
    "case-header",
    "line-types",
    "requests-unique",
    "requests-paired",
    "request-bodies",
    "response-status",
    "sse-parse",
    "sse-done",
    "assembled-content",
    "assembled-tool-calls",
    "finish-reason",
    "usage",
    "tool-results",
    "case-counts",
    "case-status",
)
MANIFEST_CHECK_NAMES = (
    "manifest",
    "manifest-shape",
    "manifest-integrity",
    "manifest-counts",
    "manifest-totals",
    "manifest-files",
    "manifest-duplicates",
)


@dataclass(frozen=True, slots=True)
class ReplayCheck:
    """One named replay assertion."""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ReplayCaseResult:
    """Replay result for a single recording file."""

    case_id: str
    file: str
    status: str
    requests: int
    tool_calls: int
    checks: tuple[ReplayCheck, ...]

    @property
    def passed(self) -> bool:
        """True when the recording ended cleanly and every check passed."""
        return self.status == "ok" and all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> tuple[ReplayCheck, ...]:
        """The checks that failed, in declaration order."""
        return tuple(check for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "case_id": self.case_id,
            "file": self.file,
            "status": self.status,
            "requests": self.requests,
            "tool_calls": self.tool_calls,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """Aggregate replay report over every recorded fixture case."""

    cases: tuple[ReplayCaseResult, ...] = ()
    checks: tuple[ReplayCheck, ...] = ()

    def __post_init__(self) -> None:
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ReplayError("replay report contains duplicate case ids")
        if case_ids != sorted(case_ids):
            raise ReplayError("replay report cases must be sorted by case id")
        names = [check.name for check in self.checks]
        if len(set(names)) != len(names):
            raise ReplayError("replay report contains duplicate check names")

    @property
    def total(self) -> int:
        """Number of replayed cases."""
        return len(self.cases)

    @property
    def passed_count(self) -> int:
        """Number of cases whose replay passed."""
        return sum(1 for case in self.cases if case.passed)

    @property
    def failed_count(self) -> int:
        """Number of cases whose replay failed."""
        return self.total - self.passed_count

    @property
    def all_passed(self) -> bool:
        """True when every report check and every case passed."""
        return all(check.passed for check in self.checks) and self.failed_count == 0

    @property
    def failed_case_ids(self) -> tuple[str, ...]:
        """Case ids whose replay failed, sorted."""
        return tuple(case.case_id for case in self.cases if not case.passed)

    def failure_summary(self) -> tuple[str, ...]:
        """One deterministic line per failing check and failing case."""
        lines = [f"manifest: {check.name}: {check.detail}" for check in self.checks if not check.passed]
        for case in self.cases:
            for check in case.failed_checks:
                lines.append(f"{case.case_id}: {check.name}: {check.detail}")
        return tuple(lines)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "cases": [case.to_dict() for case in self.cases],
            "checks": [check.to_dict() for check in self.checks],
            "total": self.total,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "all_passed": self.all_passed,
            "failed_case_ids": list(self.failed_case_ids),
        }


@dataclass(frozen=True, slots=True)
class _AssembledCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class _AssembledStream:
    content: str
    reasoning: str
    calls: tuple[_AssembledCall, ...]
    finish_reason: str | None
    usage: dict[str, int] | None
    done: bool
    chunks: int


@dataclass(slots=True)
class _PendingRequest:
    request_id: str
    body: dict[str, object] | None = None
    start: dict[str, object] | None = None
    end: dict[str, object] | None = None
    sse: list[str] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)


def _mapping(value: object, subject: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReplayError(f"{subject} must be a JSON object")
    mapping: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ReplayError(f"{subject} must use string keys")
        mapping[key] = item
    return mapping


def _sequence(value: object, subject: str) -> list[object]:
    if not isinstance(value, list):
        raise ReplayError(f"{subject} must be a JSON array")
    return list(value)


def _text(value: object, subject: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReplayError(f"{subject} must be a non-empty string")
    return value


def _integer(value: object, subject: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ReplayError(f"{subject} must be an integer")
    return value


def _read_json_lines(path: Path) -> tuple[dict[str, object], ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReplayError(f"{path.name}: cannot read recording ({exc})") from exc
    lines: list[dict[str, object]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            raise ReplayError(f"{path.name}:{number}: blank line in recording")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReplayError(f"{path.name}:{number}: invalid JSON ({exc.msg})") from exc
        lines.append(_mapping(parsed, f"{path.name}:{number}"))
    if not lines:
        raise ReplayError(f"{path.name}: recording is empty")
    return tuple(lines)


def _assemble_stream(raw_lines: Sequence[str], subject: str) -> _AssembledStream:
    document = "\n\n".join(raw_lines)
    try:
        events = decode_sse_text(document)
    except Exception as exc:
        raise ReplayError(f"{subject}: cannot decode SSE document ({exc})") from exc
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    done = False
    chunks = 0
    for event in events:
        if event.data == DONE_PAYLOAD:
            done = True
            continue
        try:
            payload = json.loads(event.data)
        except json.JSONDecodeError as exc:
            raise ReplayError(f"{subject}: invalid SSE JSON payload ({exc.msg})") from exc
        chunk = _mapping(payload, f"{subject} SSE chunk")
        chunks += 1
        if chunk.get("usage") is not None:
            raw_usage = _mapping(chunk["usage"], f"{subject} usage")
            parsed_usage: dict[str, int] = {}
            for key in USAGE_KEYS:
                if raw_usage.get(key) is not None:
                    parsed_usage[key] = _integer(raw_usage[key], f"{subject} usage {key}")
            usage = parsed_usage
        for choice_value in _sequence(chunk.get("choices", []), f"{subject} choices"):
            choice = _mapping(choice_value, f"{subject} choice")
            reason = choice.get("finish_reason")
            if isinstance(reason, str) and reason:
                finish_reason = reason
            delta_value = choice.get("delta")
            if delta_value is None:
                continue
            delta = _mapping(delta_value, f"{subject} delta")
            content = delta.get("content")
            if isinstance(content, str):
                content_parts.append(content)
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str):
                reasoning_parts.append(reasoning)
            for fragment_value in _sequence(delta.get("tool_calls", []), f"{subject} tool_calls"):
                fragment = _mapping(fragment_value, f"{subject} tool_call fragment")
                index = _integer(fragment.get("index"), f"{subject} tool_call index")
                slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                call_id = fragment.get("id")
                if isinstance(call_id, str) and call_id:
                    slot["id"] = call_id
                function_value = fragment.get("function")
                if function_value is None:
                    continue
                function = _mapping(function_value, f"{subject} tool_call function")
                name = function.get("name")
                if isinstance(name, str) and name:
                    slot["name"] = name
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    slot["arguments"] += arguments
    ordered = tuple(
        _AssembledCall(
            call_id=calls[index]["id"], name=calls[index]["name"], arguments=calls[index]["arguments"]
        )
        for index in sorted(calls)
    )
    return _AssembledStream(
        content="".join(content_parts),
        reasoning="".join(reasoning_parts),
        calls=ordered,
        finish_reason=finish_reason,
        usage=usage,
        done=done,
        chunks=chunks,
    )


def _aggregate(name: str, failures: dict[str, list[str]]) -> ReplayCheck:
    details = failures.get(name, [])
    detail = "; ".join(details[:3])
    if len(details) > 3:
        detail += f"; (+{len(details) - 3} more)"
    return ReplayCheck(name=name, passed=not details, detail=detail)


def _check_request_body(body: dict[str, object], subject: str, fail: Callable[[str, str], None]) -> None:
    messages_value = body.get("messages")
    try:
        messages = _sequence(messages_value, f"{subject} messages")
    except ReplayError as exc:
        fail("request-bodies", str(exc))
        return
    if not messages:
        fail("request-bodies", f"{subject}: request has no messages")
    for index, message_value in enumerate(messages):
        try:
            message = _mapping(message_value, f"{subject} message {index}")
        except ReplayError as exc:
            fail("request-bodies", str(exc))
            continue
        if not isinstance(message.get("role"), str):
            fail("request-bodies", f"{subject}: message {index} has no role")
    if not isinstance(body.get("model"), str) or not body.get("model"):
        fail("request-bodies", f"{subject}: request has no model")
    if body.get("stream") is not True:
        fail("request-bodies", f"{subject}: recorded request did not stream")


def _compare_calls(
    observed: Sequence[dict[str, object]],
    assembled: Sequence[_AssembledCall],
    subject: str,
    fail: Callable[[str, str], None],
) -> None:
    if len(observed) != len(assembled):
        fail(
            "assembled-tool-calls",
            f"{subject}: recorded {len(observed)} tool calls but stream has {len(assembled)}",
        )
        return
    for index, (recorded, expected) in enumerate(zip(observed, assembled, strict=True)):
        call_id = recorded.get("call_id")
        if expected.call_id and isinstance(call_id, str) and call_id != expected.call_id:
            fail("assembled-tool-calls", f"{subject}: call {index} id {call_id} != {expected.call_id}")
        name = recorded.get("name")
        if name != expected.name:
            fail("assembled-tool-calls", f"{subject}: call {index} name {name!r} != {expected.name!r}")
        arguments = recorded.get("arguments")
        if not isinstance(arguments, str):
            fail("assembled-tool-calls", f"{subject}: call {index} arguments are not a string")
            continue
        try:
            recorded_arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            fail("assembled-tool-calls", f"{subject}: call {index} arguments are invalid JSON ({exc.msg})")
            continue
        try:
            assembled_arguments = json.loads(expected.arguments) if expected.arguments else {}
        except json.JSONDecodeError as exc:
            fail("assembled-tool-calls", f"{subject}: streamed arguments are invalid JSON ({exc.msg})")
            continue
        if recorded_arguments != assembled_arguments:
            fail("assembled-tool-calls", f"{subject}: call {index} arguments do not match the stream")


def _compare_usage(
    expected_value: object,
    assembled: dict[str, int] | None,
    subject: str,
    fail: Callable[[str, str], None],
) -> None:
    if expected_value is None:
        fail("usage", f"{subject}: response.end has no usage")
        return
    try:
        expected = _mapping(expected_value, f"{subject} usage")
    except ReplayError as exc:
        fail("usage", str(exc))
        return
    if assembled is None:
        fail("usage", f"{subject}: stream has no usage chunk")
        return
    for key in USAGE_KEYS:
        expected_count = expected.get(key)
        if expected_count != assembled.get(key):
            fail("usage", f"{subject}: usage {key} {expected_count!r} != {assembled.get(key)!r}")


def replay_recording(path: str | Path) -> ReplayCaseResult:
    """Replay one recorded fixture file and return its structured result.

    Args:
        path: Path to a single fixtures/native/recordings/*.jsonl file.

    Returns:
        A ReplayCaseResult whose checks describe JSONL shape, request pairing,
        SSE reassembly and case-end counters.

    Raises:
        ReplayError: Only when the path is missing; malformed recordings are
            reported as failed checks instead of exceptions.
    """
    recording_path = Path(path)
    file_label = recording_path.name
    failures: dict[str, list[str]] = {}

    def fail(name: str, detail: str) -> None:
        failures.setdefault(name, []).append(f"{file_label}: {detail}")

    try:
        lines = _read_json_lines(recording_path)
    except ReplayError as exc:
        return ReplayCaseResult(
            case_id=recording_path.stem,
            file=file_label,
            status="error",
            requests=0,
            tool_calls=0,
            checks=(ReplayCheck("jsonl", False, str(exc)),),
        )

    header: dict[str, object] | None = None
    trailer: dict[str, object] | None = None
    requests: dict[str, _PendingRequest] = {}
    results: dict[str, dict[str, object]] = {}
    for number, line in enumerate(lines, start=1):
        subject = f"line {number}"
        kind = line.get("type")
        if not isinstance(kind, str) or kind not in KNOWN_LINE_TYPES:
            fail("line-types", f"{subject}: unknown line type {kind!r}")
            continue
        if kind == "case":
            if header is not None:
                fail("case-header", f"{subject}: duplicate case line")
            header = line
            continue
        if kind == "case.end":
            if trailer is not None:
                fail("case-header", f"{subject}: duplicate case.end line")
            trailer = line
            continue
        if kind == "llm.request":
            try:
                request_id = _text(line.get("request_id"), f"{subject} request_id")
            except ReplayError as exc:
                fail("requests-paired", str(exc))
                continue
            if request_id in requests:
                fail("requests-unique", f"{subject}: duplicate request {request_id}")
                continue
            record = _PendingRequest(request_id=request_id)
            body_value = line.get("body")
            if body_value is None:
                fail("request-bodies", f"{subject}: request {request_id} has no body")
            else:
                try:
                    record.body = _mapping(body_value, f"{subject} request body")
                except ReplayError as exc:
                    fail("request-bodies", str(exc))
            requests[request_id] = record
            continue
        if kind == "llm.response.start":
            try:
                request_id = _text(line.get("request_id"), f"{subject} request_id")
            except ReplayError as exc:
                fail("requests-paired", str(exc))
                continue
            record = requests.setdefault(request_id, _PendingRequest(request_id=request_id))
            if record.start is not None:
                fail("requests-unique", f"{subject}: duplicate response.start for {request_id}")
            else:
                record.start = line
            continue
        if kind == "llm.response.end":
            try:
                request_id = _text(line.get("request_id"), f"{subject} request_id")
            except ReplayError as exc:
                fail("requests-paired", str(exc))
                continue
            record = requests.setdefault(request_id, _PendingRequest(request_id=request_id))
            if record.end is not None:
                fail("requests-unique", f"{subject}: duplicate response.end for {request_id}")
            else:
                record.end = line
            continue
        if kind == "llm.sse":
            try:
                request_id = _text(line.get("request_id"), f"{subject} request_id")
                raw = _text(line.get("raw"), f"{subject} raw SSE line")
            except ReplayError as exc:
                fail("sse-parse", str(exc))
                continue
            if not raw.startswith("data:"):
                fail("sse-parse", f"{subject}: raw payload does not start with data:")
            record = requests.setdefault(request_id, _PendingRequest(request_id=request_id))
            record.sse.append(raw)
            continue
        if kind == "tool.call":
            try:
                request_id = _text(line.get("request_id"), f"{subject} request_id")
                call_id = _text(line.get("call_id"), f"{subject} call_id")
                name = _text(line.get("name"), f"{subject} tool name")
                arguments = _text(line.get("arguments"), f"{subject} tool arguments")
            except ReplayError as exc:
                fail("assembled-tool-calls", str(exc))
                continue
            try:
                json.loads(arguments)
            except json.JSONDecodeError as exc:
                fail("assembled-tool-calls", f"{subject}: tool.call arguments are invalid JSON ({exc.msg})")
            record = requests.setdefault(request_id, _PendingRequest(request_id=request_id))
            record.calls.append({"call_id": call_id, "name": name, "arguments": arguments})
            continue
        if kind == "tool.result":
            try:
                call_id = _text(line.get("call_id"), f"{subject} call_id")
                _text(line.get("name"), f"{subject} tool name")
                _text(line.get("content"), f"{subject} tool content")
                if not isinstance(line.get("is_error"), bool):
                    raise ReplayError(f"{subject} is_error must be a boolean")
            except ReplayError as exc:
                fail("tool-results", str(exc))
                continue
            if call_id in results:
                fail("tool-results", f"{subject}: duplicate tool.result for {call_id}")
            results[call_id] = line

    if header is None:
        fail("case-header", "missing case header line")
    else:
        try:
            case_id = _text(header.get("case_id"), "case_id")
            if case_id != recording_path.stem:
                fail("case-header", f"case_id {case_id} does not match filename {recording_path.stem}")
            _text(header.get("model"), "case model")
            _text(header.get("provider"), "case provider")
            _text(header.get("harness_version"), "case harness_version")
        except ReplayError as exc:
            fail("case-header", str(exc))

    for request_id in sorted(requests):
        record = requests[request_id]
        subject = f"request {request_id}"
        if record.body is None or record.start is None or record.end is None:
            fail("requests-paired", f"{subject}: missing request, response.start or response.end")
        if record.body is not None:
            _check_request_body(record.body, subject, fail)
        if record.start is not None:
            status = record.start.get("status")
            if status != 200:
                fail("response-status", f"{subject}: HTTP status {status!r}")
        if record.end is None:
            continue
        if not record.sse:
            fail("sse-parse", f"{subject}: no recorded SSE chunks")
            continue
        try:
            assembled = _assemble_stream(record.sse, subject)
        except ReplayError as exc:
            fail("sse-parse", str(exc))
            continue
        if not assembled.done:
            fail("sse-done", f"{subject}: stream has no [DONE] payload")
        if assembled.chunks == 0:
            fail("sse-parse", f"{subject}: stream has no chat completion chunks")
        expected_content = record.end.get("content")
        if expected_content != assembled.content:
            fail("assembled-content", f"{subject}: recorded content does not match the stream")
        expected_finish = record.end.get("finish_reason")
        if expected_finish != assembled.finish_reason:
            fail("finish-reason", f"{subject}: {expected_finish!r} != {assembled.finish_reason!r}")
        expected_call_count = record.end.get("tool_call_count")
        if expected_call_count != len(assembled.calls):
            fail(
                "assembled-tool-calls",
                f"{subject}: recorded {expected_call_count!r} tool calls but stream has {len(assembled.calls)}",
            )
        _compare_calls(record.calls, assembled.calls, subject, fail)
        _compare_usage(record.end.get("usage"), assembled.usage, subject, fail)

    recorded_call_ids = [str(call.get("call_id")) for request in requests.values() for call in request.calls]
    missing_results = [call_id for call_id in recorded_call_ids if call_id not in results]
    if missing_results:
        fail("tool-results", f"tool calls without a result: {', '.join(missing_results)}")
    orphan_results = sorted(set(results) - set(recorded_call_ids))
    if orphan_results:
        fail("tool-results", f"tool results without a call: {', '.join(orphan_results)}")

    observed_calls = sum(len(request.calls) for request in requests.values())
    if trailer is None:
        fail("case-status", "missing case.end line")
    else:
        status = trailer.get("status")
        if status != "ok":
            fail("case-status", f"recording status {status!r}")
        if trailer.get("requests") != len(requests):
            fail("case-counts", f"case.end requests {trailer.get('requests')!r} != {len(requests)}")
        if trailer.get("tool_calls") != observed_calls:
            fail("case-counts", f"case.end tool_calls {trailer.get('tool_calls')!r} != {observed_calls}")

    case_id = recording_path.stem
    if header is not None and isinstance(header.get("case_id"), str):
        case_id = str(header["case_id"])
    status_value = trailer.get("status") if trailer is not None else None
    return ReplayCaseResult(
        case_id=case_id,
        file=file_label,
        status=status_value if isinstance(status_value, str) else "unknown",
        requests=len(requests),
        tool_calls=observed_calls,
        checks=tuple(_aggregate(name, failures) for name in CASE_CHECK_NAMES),
    )


def _load_json_object(path: Path, subject: str) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReplayError(f"{subject}: cannot read manifest ({exc})") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{subject}: invalid JSON ({exc.msg})") from exc
    return _mapping(payload, subject)


def replay_fixtures(root: str | Path) -> ReplayReport:
    """Replay every fixture listed in the native manifest.

    Args:
        root: Directory that contains manifest.json and recordings/.

    Returns:
        A ReplayReport sorted by case id.  Manifest failures are reported as
        named checks; a missing manifest yields a single failed manifest check.
    """
    root_path = Path(root)
    manifest_path = root_path / MANIFEST_FILENAME
    try:
        manifest = _load_json_object(manifest_path, manifest_path.name)
    except ReplayError as exc:
        return ReplayReport(checks=(ReplayCheck("manifest", False, str(exc)),))

    failures: dict[str, list[str]] = {}

    def fail(name: str, detail: str) -> None:
        failures.setdefault(name, []).append(detail)

    try:
        raw_cases = _sequence(manifest.get("cases"), "manifest cases")
    except ReplayError as exc:
        raw_cases = []
        fail("manifest-shape", str(exc))
    cases: list[dict[str, object]] = []
    for index, raw_case in enumerate(raw_cases):
        try:
            cases.append(_mapping(raw_case, f"manifest case {index}"))
        except ReplayError as exc:
            fail("manifest-shape", str(exc))
    if not isinstance(manifest.get("harness_version"), str):
        fail("manifest-shape", "manifest harness_version must be a string")
    if manifest.get("case_count") != len(cases):
        fail("manifest-counts", f"manifest case_count {manifest.get('case_count')!r} != {len(cases)}")

    expected_files: set[str] = set()
    results: list[ReplayCaseResult] = []
    total_requests = 0
    total_tool_calls = 0
    total_bytes = 0
    for case in cases:
        case_id_value = case.get("case_id")
        file_value = case.get("file")
        if not isinstance(case_id_value, str) or not isinstance(file_value, str):
            fail("manifest-shape", "manifest case needs string case_id and file")
            continue
        case_id = case_id_value
        relative = file_value
        if relative in expected_files:
            fail("manifest-duplicates", f"case {case_id}: duplicate file {relative}")
            continue
        expected_files.add(relative)
        path = root_path / relative
        if not path.is_file():
            fail("manifest-files", f"case {case_id}: missing recording {relative}")
            continue
        payload = path.read_bytes()
        total_bytes += len(payload)
        if case.get("bytes") != len(payload):
            fail("manifest-integrity", f"case {case_id}: manifest byte count does not match {relative}")
        digest = hashlib.sha256(payload).hexdigest()
        if case.get("sha256") != digest:
            fail("manifest-integrity", f"case {case_id}: manifest sha256 does not match {relative}")
        result = replay_recording(path)
        if result.case_id != case_id:
            fail("manifest-integrity", f"case {case_id}: recording header says {result.case_id}")
        if case.get("requests") != result.requests:
            fail("manifest-counts", f"case {case_id}: manifest requests do not match replay")
        if case.get("tool_calls") != result.tool_calls:
            fail("manifest-counts", f"case {case_id}: manifest tool_calls do not match replay")
        total_requests += result.requests
        total_tool_calls += result.tool_calls
        results.append(result)

    for recording in sorted((root_path / RECORDINGS_DIRNAME).glob("*.jsonl")):
        relative = recording.relative_to(root_path).as_posix()
        if relative not in expected_files:
            fail("manifest-files", f"unlisted recording {relative}")
    if manifest.get("total_requests") != total_requests:
        fail("manifest-totals", "manifest total_requests does not match replay")
    if manifest.get("total_tool_calls") != total_tool_calls:
        fail("manifest-totals", "manifest total_tool_calls does not match replay")
    if manifest.get("total_bytes") != total_bytes:
        fail("manifest-totals", "manifest total_bytes does not match the recordings")

    results.sort(key=lambda result: result.case_id)
    return ReplayReport(
        cases=tuple(results),
        checks=tuple(_aggregate(name, failures) for name in MANIFEST_CHECK_NAMES),
    )
