"""Schema 1 session errors.

All native session failures inherit the existing SessionError so callers can
keep one recovery branch. The messages are frozen by docs/native/schema.md
section 9; raise them with the format shown there rather than inventing prose.
"""

from __future__ import annotations

from deeppyro.core.errors import SessionError


class CorruptLineError(SessionError):
    """A line is not valid JSON, is blank, or the file has no complete last line."""


class LineTooLargeError(SessionError):
    """A single line exceeds the 32 MiB reader guard."""


class SessionV4Error(SessionError):
    """The file is a v4 session log; schema 1 is a deliberate break."""


class UnsupportedSchemaError(SessionError):
    """The header names a schema other than 1."""


class HeaderError(SessionError):
    """The header is missing, duplicated, malformed, or mismatched with the file name."""


class SequenceError(SessionError):
    """Event seq numbers are not strictly consecutive from 1."""


class TimestampError(SessionError):
    """Event timestamps move backwards or predate the header."""


class UnknownEventError(SessionError):
    """A core-looking event type is not in EventKind and is not plugin-namespaced."""


class PluginNamespaceError(SessionError):
    """A plugin event type does not match plugin.<id>.<event>."""


class EventFieldError(SessionError):
    """An envelope or data object has an unknown/missing/wrongly typed field."""


class MessageError(SessionError):
    """An embedded Message does not satisfy the native message model."""


class MessageRefError(SessionError):
    """message.replaced/compaction.done references an unknown or non-contiguous message."""


class ToolPairingError(SessionError):
    """A tool.call and tool.result are missing, duplicated, or mismatched."""


class OrderingError(SessionError):
    """turn/step nesting or event placement is illegal."""


class SessionLockedError(SessionError):
    """Another process holds the session writer lock."""


__all__ = [
    "CorruptLineError",
    "EventFieldError",
    "HeaderError",
    "LineTooLargeError",
    "MessageError",
    "MessageRefError",
    "OrderingError",
    "PluginNamespaceError",
    "SequenceError",
    "SessionLockedError",
    "SessionV4Error",
    "TimestampError",
    "ToolPairingError",
    "UnknownEventError",
    "UnsupportedSchemaError",
]
