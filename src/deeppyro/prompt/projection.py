"""Pure projections for the two loop-owned surface messages.

This module ports DSH's @deepseek-ai/dsh-agent-loop/runtime-context projections.
They decide what a step should append or replace; they never append themselves
and never mutate the session log.

* SystemPromptProjection keeps the rendered system prompt on surface node 0
  while preserving a cached history prefix when the provider route can replay
  it (in_history).
* RuntimeContextProjection keeps exactly one retained dynamic runtime-context
  snapshot, so an unchanged snapshot is not re-sent each step.

Callers commit each ProjectionUpdate as a system/message or user/message record
with the envelope described on the update.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from deeppyro.core.ids import new_message_id
from deeppyro.core.wire import LogRecord, Message, TextPart
from deeppyro.session.derive import fold_surface

#: Message source kind owned by the runtime-context projection.
SOURCE = "runtime-context"
#: Text recorded when the dynamic context becomes empty (DSH's exact wording).
CLEARED = "Current runtime context: none. Earlier runtime-context snapshots no longer apply."

#: Aliases with explicit names for package exports.
RUNTIME_CONTEXT_SOURCE = SOURCE
RUNTIME_CONTEXT_CLEARED = CLEARED

ProjectionKind = Literal["append", "replace"]


@dataclass(slots=True)
class ProjectionUpdate:
    """One record a projection wants the caller to commit.

    An append update carries the message to append with surfaceOp="append".
    A replace update carries the replacement message and exactly one
    (seq, text) pair: append that message as a new system/message with
    surfaceOp {"op": "replace", "startSeq": seq, "endSeq": seq} and
    sourceEventSeqs=[seq].
    """

    kind: ProjectionKind
    message: Message | None = None
    replace: list[tuple[int, str]] = field(default_factory=list)


#: Ordered updates returned by SystemPromptProjection.project.
ProjectionAction = list[ProjectionUpdate]


@dataclass(slots=True, frozen=True)
class RetainedSnapshot:
    """The runtime-context snapshot currently retained on the surface."""

    seq: int
    text: str


_MISSING = object()


def create_system_message(text: str) -> Message:
    """Create a fresh system message holding *text*.

    An empty rendering is recorded as content=[] (DSH's "no system prompt"
    node); a non-empty rendering becomes one text part.
    """
    return Message(
        role="system",
        content=[] if text == "" else [TextPart(type="text", text=text)],
        id=new_message_id(),
        source={"kind": "system-prompt"},
    )


def create_runtime_context_message(snapshot: str, sections: list[dict[str, Any]] | None = None) -> Message:
    """Create a fresh runtime-context user message payload.

    *sections* names the contributions that formed the snapshot. The message
    source keeps that provenance so a later projection can tell a plain
    snapshot from a composed one.
    """
    retained_sections = copy.deepcopy(list(sections or []))
    source: dict[str, Any] = {"kind": SOURCE}
    if retained_sections:
        source["form"] = "snapshot"
        source["sections"] = retained_sections
    return Message(
        role="user",
        content=[TextPart(type="text", text=snapshot)],
        id=new_message_id(),
        source=source,
    )


def _content_text(content: object) -> str:
    """Concatenate the text parts of one message content list."""
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if isinstance(part, TextPart):
            chunks.append(part.text)
        elif isinstance(part, Mapping) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def _system_text(record: LogRecord) -> str:
    message = record.data.get("message")
    if not isinstance(message, Mapping):
        return ""
    return _content_text(message.get("content"))


class SystemPromptProjection:
    """Decide how one rendered system prompt reaches the session surface.

    The projection is stateless apart from the record prefix it reads: every
    call re-folds the current records, so a caller may keep one instance over a
    live session log.

    DSH semantics:

    * the first prompt, even an empty one, reserves surface node 0;
    * a route that can replay history (in_history=True) appends changed
      non-empty prompts after the cached history, preserving the request
      prefix;
    * an incapable route, a cleared prompt, or a new series whose effective
      text differs instead normalizes the first system node and empties later
      active nodes;
    * a series start whose latest surviving node already holds the rendering
      emits nothing, because moving the same text to node 0 would change the
      request prefix without changing the prompt.
    """

    def __init__(self, records: Sequence[LogRecord]) -> None:
        self._records = records

    def system_nodes(self) -> list[LogRecord]:
        """Return the surviving system/message nodes in surface order."""
        return [node for node in fold_surface(self._records) if node.type == "system/message"]

    def project(self, rendered: str, *, in_history: bool, starts_series: bool) -> list[ProjectionUpdate]:
        """Return the ordered updates needed to make *rendered* effective."""
        nodes = self.system_nodes()
        if not nodes:
            return [ProjectionUpdate(kind="append", message=create_system_message(rendered))]
        head = nodes[0]
        head_text = _system_text(head)
        latest_text = head_text
        for node in reversed(nodes):
            text = _system_text(node)
            if text != "":
                latest_text = text
                break
        if not in_history or rendered == "" or (starts_series and latest_text != rendered):
            updates: list[ProjectionUpdate] = []
            for node in nodes[1:]:
                if _system_text(node) == "":
                    continue
                updates.append(
                    ProjectionUpdate(
                        kind="replace",
                        message=create_system_message(""),
                        replace=[(node.seq, "")],
                    )
                )
            if head_text != rendered:
                updates.append(
                    ProjectionUpdate(
                        kind="replace",
                        message=create_system_message(rendered),
                        replace=[(head.seq, rendered)],
                    )
                )
            return updates
        if latest_text == rendered:
            return []
        return [ProjectionUpdate(kind="append", message=create_system_message(rendered))]


class RuntimeContextProjection:
    """Track the single retained dynamic runtime-context snapshot.

    The projection restores its retained state from the authoritative record
    prefix: the newest user/message with source.kind == "runtime-context" that
    is still on the surface wins. A replacement that shadows that node makes
    the snapshot non-retained; the next non-empty rendering is appended again,
    and an empty rendering appends the DSH CLEARED marker once.
    """

    SOURCE = SOURCE
    CLEARED = CLEARED

    def __init__(self, records: Sequence[LogRecord]) -> None:
        self._records = records
        self._retained: RetainedSnapshot | None | object = _MISSING
        self.refresh()

    @property
    def retained(self) -> RetainedSnapshot | None:
        """The retained snapshot, or None when none is retained."""
        return self._retained if isinstance(self._retained, RetainedSnapshot) else None

    def refresh(self, records: Sequence[LogRecord] | None = None) -> None:
        """Recompute retained state from *records* (or the stored prefix)."""
        if records is not None:
            self._records = records
        surface_seqs = {node.seq for node in fold_surface(self._records)}
        retained: RetainedSnapshot | None | object = _MISSING
        for record in reversed(self._records):
            if record.type != "user/message":
                continue
            source = record.data.get("source")
            if not isinstance(source, Mapping) or source.get("kind") != SOURCE:
                continue
            if retained is _MISSING:
                retained = None
            if record.seq in surface_seqs:
                retained = RetainedSnapshot(
                    seq=record.seq,
                    text=_content_text(record.data.get("content")),
                )
                break
        self._retained = retained

    def project(self, current: str, sections: list[dict[str, Any]] | None = None) -> ProjectionUpdate | None:
        """Return the append update for *current*, or None when unchanged.

        Records are re-folded first, so appending a replacement event to the
        live prefix clears the retained snapshot without an explicit call.
        """
        self.refresh()
        if self._retained is _MISSING and current == "":
            return None
        snapshot = CLEARED if current == "" else current
        retained = self._retained
        if isinstance(retained, RetainedSnapshot) and retained.text == snapshot:
            return None
        return ProjectionUpdate(
            kind="append",
            message=create_runtime_context_message(snapshot, sections),
        )
