"""SQLite FTS5 long-term memory backend.

The backend is stdlib-only: one sqlite3 database with a normal records table and a
standalone FTS5 index table. Every record carries provenance (source session,
message and run ids plus created_at, content sha256, importance and tags) so a
retrieved fact can be traced back to the native session/event that produced it.

Construction is idempotent: the schema is created with IF NOT EXISTS and the
schema version moves forward only when needed, so opening an existing database
never destroys data. The default location honors DEEPPYRO_HOME through
deeppyro.paths.deeppyro_home, and tests pass an explicit path inside tmp_path.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from pathlib import Path

from deeppyro.memory.backend import (
    DEFAULT_SEARCH_LIMIT,
    MemoryBackendStatus,
    MemoryProvenance,
    MemoryRecord,
    MemorySearchHit,
    format_timestamp,
    hash_content,
    utc_now,
    validate_content,
    validate_created_at,
    validate_dedupe_key,
    validate_importance,
    validate_limit,
    validate_memory_id,
    validate_tags,
)
from deeppyro.memory.errors import (
    MemoryBackendCorruptError,
    MemoryBackendError,
    MemoryBackendUnavailableError,
    MemoryRecordNotFoundError,
    MemoryValidationError,
)
from deeppyro.memory.store import Clock
from deeppyro.paths import deeppyro_home

#: Filename of the long-term memory database below DEEPPYRO_HOME.
DB_FILENAME = "memory.sqlite3"

#: On-disk schema version tracked in PRAGMA user_version.
SCHEMA_VERSION = 2

#: The standalone FTS5 index table name.
FTS_TABLE = "memory_search"

#: Longest accepted search query in characters.
MAX_QUERY_LENGTH = 512

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS memory_records (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        source_session_id TEXT,
        source_message_id TEXT,
        run_id TEXT,
        created_at TEXT NOT NULL,
        importance TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '[]',
        dedupe_key TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS memory_records_created_at
        ON memory_records (created_at)
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5 (
        memory_id UNINDEXED,
        body,
        tags,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
)

#: Partial unique index backing dedupe-key upserts; created after the legacy
#: table upgrade because ALTER TABLE must land before the index exists.
_DEDUPE_KEY_INDEX = """
    CREATE UNIQUE INDEX IF NOT EXISTS memory_records_dedupe_key
        ON memory_records (dedupe_key)
        WHERE dedupe_key IS NOT NULL
    """


def default_memory_db_path() -> Path:
    """Return the default long-term memory database path under DEEPPYRO_HOME."""
    return deeppyro_home() / DB_FILENAME


def build_fts_expression(query: str) -> str:
    """Turn a plain query into a safe FTS5 AND expression of quoted terms.

    Each whitespace-separated term is quoted so FTS5 operators typed by a user
    are treated as text instead of syntax; terms are ANDed.
    """
    if not isinstance(query, str) or not query.strip():
        raise MemoryValidationError("search query must be a non-empty string")
    if len(query) > MAX_QUERY_LENGTH:
        raise MemoryValidationError(f"search query may not exceed {MAX_QUERY_LENGTH} characters")
    terms: list[str] = []
    for term in query.split():
        terms.append('"' + term.replace('"', '""') + '"')
    return " ".join(terms)


class SqliteMemoryBackend:
    """SQLite FTS5 implementation of the long-term memory backend contract.

    The instance owns one sqlite3 connection and is meant to be used from a single
    thread (the service wraps it synchronously). Closing is idempotent and the
    backend supports the context-manager protocol.
    """

    def __init__(self, path: str | Path | None = None, *, clock: Clock | None = None) -> None:
        """Open (and, if needed, initialize) the database at path or the default."""
        self._path = Path(path) if path is not None else default_memory_db_path()
        self._clock = clock if clock is not None else utc_now
        self._closed = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path))
        except (OSError, sqlite3.Error) as exc:
            raise MemoryBackendUnavailableError(
                f"cannot open long-term memory database at {self._path}: {exc}"
            ) from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._migrate()
        except sqlite3.Error as exc:
            self._conn.close()
            self._closed = True
            raise MemoryBackendCorruptError(
                f"cannot initialize long-term memory database at {self._path}: {exc}"
            ) from exc

    @property
    def path(self) -> Path:
        """Absolute filesystem path of the database file."""
        return self._path

    @property
    def schema_version(self) -> int:
        """On-disk schema version recorded in PRAGMA user_version."""
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        """Close the connection; safe to call more than once."""
        if self._closed:
            return
        self._conn.close()
        self._closed = True

    def __enter__(self) -> SqliteMemoryBackend:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def write(
        self,
        content: str,
        *,
        source_session_id: str | None = None,
        source_message_id: str | None = None,
        run_id: str | None = None,
        importance: str = "normal",
        tags: Sequence[str] = (),
        created_at: str | None = None,
    ) -> MemoryRecord:
        """Persist one memory record with provenance and return the stored record."""
        self._ensure_open()
        checked_content, provenance = self._prepare_record(
            content,
            source_session_id=source_session_id,
            source_message_id=source_message_id,
            run_id=run_id,
            importance=importance,
            tags=tags,
            created_at=created_at,
        )
        record = MemoryRecord(
            id="mem_" + uuid.uuid4().hex[:16],
            content=checked_content,
            provenance=provenance,
        )
        indexed_tags = " ".join(provenance.tags)
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO memory_records (
                        id, content, content_sha256, source_session_id,
                        source_message_id, run_id, created_at, importance, tags
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.content,
                        record.provenance.content_sha256,
                        record.provenance.source_session_id,
                        record.provenance.source_message_id,
                        record.provenance.run_id,
                        record.provenance.created_at,
                        record.provenance.importance,
                        json.dumps(list(record.provenance.tags), ensure_ascii=False),
                    ),
                )
                self._conn.execute(
                    "INSERT INTO memory_search (memory_id, body, tags) VALUES (?, ?, ?)",
                    (record.id, record.content, indexed_tags),
                )
        except sqlite3.Error as exc:
            raise MemoryBackendError(f"cannot write long-term memory record: {exc}") from exc
        return record

    def upsert(
        self,
        content: str,
        *,
        dedupe_key: str,
        source_session_id: str | None = None,
        source_message_id: str | None = None,
        run_id: str | None = None,
        importance: str = "normal",
        tags: Sequence[str] = (),
        created_at: str | None = None,
    ) -> tuple[MemoryRecord, bool]:
        """Insert or overwrite the record occupying one dedupe key.

        Returns the stored record and True when it was created, False when the
        existing record for that key was refreshed in place (same id).
        """
        self._ensure_open()
        checked_key = validate_dedupe_key(dedupe_key)
        if checked_key is None:
            raise MemoryValidationError("memory dedupe key must be a non-empty string")
        checked_content, provenance = self._prepare_record(
            content,
            source_session_id=source_session_id,
            source_message_id=source_message_id,
            run_id=run_id,
            importance=importance,
            tags=tags,
            created_at=created_at,
            dedupe_key=checked_key,
        )
        indexed_tags = " ".join(provenance.tags)
        encoded_tags = json.dumps(list(provenance.tags), ensure_ascii=False)
        try:
            with self._conn:
                row = self._conn.execute(
                    "SELECT id FROM memory_records WHERE dedupe_key = ?",
                    (checked_key,),
                ).fetchone()
                created = row is None
                record_id = "mem_" + uuid.uuid4().hex[:16] if created else str(row["id"])
                if created:
                    self._conn.execute(
                        """
                        INSERT INTO memory_records (
                            id, content, content_sha256, source_session_id,
                            source_message_id, run_id, created_at, importance, tags,
                            dedupe_key
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record_id,
                            checked_content,
                            provenance.content_sha256,
                            provenance.source_session_id,
                            provenance.source_message_id,
                            provenance.run_id,
                            provenance.created_at,
                            provenance.importance,
                            encoded_tags,
                            checked_key,
                        ),
                    )
                else:
                    self._conn.execute(
                        "DELETE FROM memory_search WHERE memory_id = ?",
                        (record_id,),
                    )
                    self._conn.execute(
                        """
                        UPDATE memory_records SET
                            content = ?, content_sha256 = ?, source_session_id = ?,
                            source_message_id = ?, run_id = ?, created_at = ?,
                            importance = ?, tags = ?
                        WHERE id = ?
                        """,
                        (
                            checked_content,
                            provenance.content_sha256,
                            provenance.source_session_id,
                            provenance.source_message_id,
                            provenance.run_id,
                            provenance.created_at,
                            provenance.importance,
                            encoded_tags,
                            record_id,
                        ),
                    )
                self._conn.execute(
                    "INSERT INTO memory_search (memory_id, body, tags) VALUES (?, ?, ?)",
                    (record_id, checked_content, indexed_tags),
                )
        except sqlite3.Error as exc:
            raise MemoryBackendError(f"cannot write long-term memory record: {exc}") from exc
        return (
            MemoryRecord(id=record_id, content=checked_content, provenance=provenance),
            created,
        )

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SEARCH_LIMIT,
        tags: Sequence[str] = (),
    ) -> tuple[MemorySearchHit, ...]:
        """Return ranked hits for one full-text query, best (lowest bm25) first."""
        self._ensure_open()
        expression = build_fts_expression(query)
        checked_limit = validate_limit(limit)
        checked_tags = validate_tags(tags)
        sql = [
            "SELECT records.*, bm25(memory_search) AS rank",
            "FROM memory_search",
            "JOIN memory_records AS records ON records.id = memory_search.memory_id",
            "WHERE memory_search MATCH ?",
        ]
        params: list[object] = [expression]
        for tag in checked_tags:
            sql.append(
                "AND records.id IN (SELECT tag_record.id FROM memory_records AS tag_record,"
                " json_each(tag_record.tags) AS tag WHERE tag.value = ?)"
            )
            params.append(tag)
        sql.append("ORDER BY rank ASC, records.created_at DESC, records.id ASC LIMIT ?")
        params.append(checked_limit)
        try:
            rows = self._conn.execute(" ".join(sql), params).fetchall()
        except sqlite3.Error as exc:
            raise MemoryBackendError(f"cannot search long-term memory: {exc}") from exc
        hits: list[MemorySearchHit] = []
        for index, row in enumerate(rows, start=1):
            rank_value = float(row["rank"])
            hits.append(
                MemorySearchHit(
                    record=self._row_to_record(row),
                    rank=index,
                    score=-rank_value,
                )
            )
        return tuple(hits)

    def forget(self, memory_id: str) -> MemoryRecord:
        """Delete one record (including its FTS row) and return what was removed."""
        self._ensure_open()
        checked_id = validate_memory_id(memory_id)
        row = self._conn.execute(
            "SELECT * FROM memory_records WHERE id = ?",
            (checked_id,),
        ).fetchone()
        if row is None:
            raise MemoryRecordNotFoundError(f"long-term memory record {checked_id} does not exist")
        record = self._row_to_record(row)
        try:
            with self._conn:
                self._conn.execute("DELETE FROM memory_search WHERE memory_id = ?", (checked_id,))
                self._conn.execute("DELETE FROM memory_records WHERE id = ?", (checked_id,))
        except sqlite3.Error as exc:
            raise MemoryBackendError(f"cannot forget long-term memory record: {exc}") from exc
        return record

    def count(self) -> int:
        """Return the number of stored long-term memory records."""
        self._ensure_open()
        return int(self._conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0])

    def status(self) -> MemoryBackendStatus:
        """Return the backend health/size snapshot."""
        self._ensure_open()
        try:
            version = self.schema_version
            count = self.count()
            table_names = {
                row[0]
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
        except sqlite3.Error as exc:
            raise MemoryBackendError(f"cannot inspect long-term memory database: {exc}") from exc
        size_bytes = 0
        for candidate in (self._path, Path(str(self._path) + "-wal")):
            try:
                size_bytes += candidate.stat().st_size
            except OSError:
                continue
        return MemoryBackendStatus(
            path=str(self._path),
            schema_version=version,
            count=count,
            fts=FTS_TABLE in table_names,
            size_bytes=size_bytes,
        )

    def _prepare_record(
        self,
        content: str,
        *,
        source_session_id: str | None,
        source_message_id: str | None,
        run_id: str | None,
        importance: str,
        tags: Sequence[str],
        created_at: str | None,
        dedupe_key: str | None = None,
    ) -> tuple[str, MemoryProvenance]:
        """Validate one write and build its content plus provenance."""
        checked_content = validate_content(content)
        checked_importance = validate_importance(importance)
        checked_tags = validate_tags(tags)
        timestamp = (
            validate_created_at(created_at) if created_at is not None else format_timestamp(self._clock())
        )
        for name, value in (
            ("source_session_id", source_session_id),
            ("source_message_id", source_message_id),
            ("run_id", run_id),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise MemoryValidationError(f"memory {name} must be a non-empty string or null")
        return checked_content, MemoryProvenance(
            content_sha256=hash_content(checked_content),
            created_at=timestamp,
            importance=checked_importance,
            source_session_id=source_session_id,
            source_message_id=source_message_id,
            run_id=run_id,
            tags=checked_tags,
            dedupe_key=dedupe_key,
        )

    def _migrate(self) -> None:
        version = self.schema_version
        if version > SCHEMA_VERSION:
            raise MemoryBackendCorruptError(
                f"long-term memory database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        for statement in _SCHEMA_STATEMENTS:
            self._conn.execute(statement)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(memory_records)")}
        if "dedupe_key" not in columns:
            # Schema 1 databases predate the dedupe column; upgrade in place.
            self._conn.execute("ALTER TABLE memory_records ADD COLUMN dedupe_key TEXT")
        self._conn.execute(_DEDUPE_KEY_INDEX)
        self._conn.commit()
        if version < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
        self._verify_schema()

    def _verify_schema(self) -> None:
        table_names = {
            row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing = [name for name in ("memory_records", FTS_TABLE) if name not in table_names]
        if missing:
            raise MemoryBackendCorruptError(
                f"long-term memory database at {self._path} lacks table {missing[0]}"
            )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(memory_records)")}
        if "dedupe_key" not in columns:
            raise MemoryBackendCorruptError(
                f"long-term memory database at {self._path} lacks column dedupe_key"
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise MemoryBackendUnavailableError("long-term memory backend is closed")

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        try:
            tags = json.loads(row["tags"] or "[]")
        except json.JSONDecodeError as exc:
            raise MemoryBackendCorruptError(
                f"long-term memory record {row['id']} has malformed tags JSON"
            ) from exc
        try:
            provenance = MemoryProvenance(
                content_sha256=row["content_sha256"],
                created_at=row["created_at"],
                importance=row["importance"],
                source_session_id=row["source_session_id"],
                source_message_id=row["source_message_id"],
                run_id=row["run_id"],
                tags=tuple(str(tag) for tag in tags),
                dedupe_key=row["dedupe_key"],
            )
        except MemoryValidationError as exc:
            raise MemoryBackendCorruptError(f"long-term memory record {row['id']} is corrupt: {exc}") from exc
        return MemoryRecord(id=row["id"], content=row["content"], provenance=provenance)


def open_memory_backend(
    path: str | Path | None = None,
    *,
    clock: Clock | None = None,
) -> SqliteMemoryBackend:
    """Open the default SQLite long-term memory backend."""
    return SqliteMemoryBackend(path, clock=clock)


__all__ = [
    "DB_FILENAME",
    "FTS_TABLE",
    "MAX_QUERY_LENGTH",
    "SCHEMA_VERSION",
    "SqliteMemoryBackend",
    "build_fts_expression",
    "default_memory_db_path",
    "open_memory_backend",
]
