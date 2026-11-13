"""Persistent state of the server: SQLite plus IQ capture files (ARCHITECTURE.md section 7).

One SQLite database under the data directory (stdlib ``sqlite3``, WAL journal) holds:

- ``config_versions``: every configuration the server ran or the operator saved, as
  YAML text with its integer version and where it came from (``file`` = the file
  given on the command line, ``api`` = saved through ``PUT /config``).
- ``scenario_versions``: every simulated world document (the scenario as YAML) with
  its integer version and what changed it (``file``, ``api``, ``preset``, ``reset``,
  ``config``: a configuration change re-announced to the simulated nodes).
- ``events``: the whole console ``Event`` document of each interference event, its
  state and times (one row per event, replaced on every published change).
- ``event_transitions``, ``evidence_snapshots``, ``measurements``, ``solutions``
  (``Solution.as_dict()`` as JSON) and ``calibrations``: append-only history.
- ``node_sessions``: one row per node session (connect and disconnect times).
- ``captures``: the IQ captures written to ``data_dir/captures`` (SigMF pairs) and
  which of them the retention limit has deleted.
- ``runs``: one row per server run.

Retention. Capture files are kept up to ``capture_limit_bytes`` in total (the oldest
are deleted beyond it). Event history is kept for ever unless ``history_max_age_s``
or ``history_max_events`` is set: then ``prune_history`` deletes the events not
updated for longer than the age, and those beyond the count (least recently updated
first), with their transitions, evidence snapshots, measurements and solutions;
calibrations and node sessions older than the age go too. The store prunes when it
opens and then at most once every ``PRUNE_INTERVAL_S`` while events are written.

Threading. A single writer connection lives on one dedicated worker thread; every
read and write is a short transaction run there, in submission order, so the event
loop never waits on the disk. The ``async`` methods return when their transaction is
done and raise ``StoreError`` on failure. The ``put_*`` / ``add_*`` methods are
fire-and-forget for the pipeline: they return an ``asyncio.Future`` at once, a failure
is logged with the table it concerns and counted in ``counters()``, and ``close()``
waits for all of them.

Absolute times are integer nanoseconds (SQLite ``INTEGER`` is 64-bit).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, TypeVar

import numpy as np

from petal_locate.core.timebase import iso_from_ns

__all__ = [
    "CAPTURE_DIR_NAME",
    "DB_FILE_NAME",
    "DEFAULT_CAPTURE_LIMIT_BYTES",
    "PRUNE_INTERVAL_S",
    "SCHEMA_VERSION",
    "PruneResult",
    "ConfigSource",
    "SavedCapture",
    "Store",
    "StoreError",
    "StoredConfig",
    "StoredEvent",
    "StoredScenario",
    "store_threads",
]

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Final[int] = 1
"""Version of the database layout written to ``meta.schema_version``."""

DB_FILE_NAME: Final[str] = "petal-locate.sqlite3"
"""File name of the database inside the data directory."""

CAPTURE_DIR_NAME: Final[str] = "captures"
"""Subdirectory of the data directory that holds saved IQ captures."""

MAX_PENDING_WRITES: Final[int] = 4096
"""Fire-and-forget writes allowed to wait for the worker thread; beyond it a write is
refused with ``StoreError`` (logged by the caller) instead of queuing without bound."""

DEFAULT_CAPTURE_LIMIT_BYTES: Final[int] = 512 * 1024 * 1024
"""Default total size of saved capture data files before the oldest are deleted."""

PRUNE_INTERVAL_S: float = 60.0
"""Shortest time between two automatic history prunes while events are written, s."""

_EVENT_TABLES: Final[tuple[str, ...]] = ("event_transitions", "evidence_snapshots", "measurements", "solutions")
_DELETE_CHUNK: Final[int] = 500

_BUSY_TIMEOUT_S: Final[float] = 5.0
_SAFE_NAME: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]")

ConfigSource = Literal["file", "api"]
"""Where a stored configuration came from."""

T = TypeVar("T")

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    started_ns INTEGER NOT NULL,
    stopped_ns INTEGER
);
CREATE TABLE IF NOT EXISTS config_versions (
    version  INTEGER PRIMARY KEY,
    saved_ns INTEGER NOT NULL,
    source   TEXT NOT NULL CHECK (source IN ('file', 'api')),
    fmt      TEXT NOT NULL CHECK (fmt IN ('yaml', 'json')),
    text     TEXT NOT NULL,
    run_id   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenario_versions (
    version  INTEGER PRIMARY KEY,
    saved_ns INTEGER NOT NULL,
    source   TEXT NOT NULL,
    name     TEXT NOT NULL,
    text     TEXT NOT NULL,
    run_id   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    seq           INTEGER NOT NULL,
    run_id        TEXT NOT NULL,
    created_ns    INTEGER NOT NULL,
    updated_ns    INTEGER NOT NULL,
    state         TEXT NOT NULL CHECK (state IN
                  ('detected', 'characterizing', 'locating', 'located', 'lost', 'closed')),
    verdict       TEXT NOT NULL CHECK (verdict IN ('uplink', 'local', 'undetermined')),
    first_seen_ns INTEGER NOT NULL,
    last_seen_ns  INTEGER NOT NULL,
    uplink_lo_hz  REAL,
    uplink_hi_hz  REAL,
    doc           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_updated ON events (updated_ns);
CREATE TABLE IF NOT EXISTS event_transitions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    t_ns     INTEGER NOT NULL,
    state    TEXT NOT NULL,
    verdict  TEXT NOT NULL,
    reason   TEXT
);
CREATE INDEX IF NOT EXISTS event_transitions_event ON event_transitions (event_id, id);
CREATE TABLE IF NOT EXISTS evidence_snapshots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    t_ns     INTEGER NOT NULL,
    rows     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_snapshots_event ON evidence_snapshots (event_id, id);
CREATE TABLE IF NOT EXISTS measurements (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    t_ns     INTEGER NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('tdoa', 'fdoa')),
    used     INTEGER NOT NULL,
    row      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS measurements_event ON measurements (event_id, seq);
CREATE TABLE IF NOT EXISTS solutions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id  TEXT NOT NULL,
    t_ns      INTEGER NOT NULL,
    converged INTEGER NOT NULL,
    solution  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS solutions_event ON solutions (event_id, id);
CREATE TABLE IF NOT EXISTS calibrations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    t_ns       INTEGER NOT NULL,
    carrier_id TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('tdoa', 'fdoa')),
    a_sat_id   TEXT NOT NULL,
    a_site_id  TEXT NOT NULL,
    b_sat_id   TEXT NOT NULL,
    b_site_id  TEXT NOT NULL,
    correction REAL NOT NULL,
    sigma      REAL NOT NULL,
    detail     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id         TEXT NOT NULL,
    node_id         TEXT,
    run_id          TEXT NOT NULL,
    connected_ns    INTEGER NOT NULL,
    disconnected_ns INTEGER,
    reason          TEXT,
    driver          TEXT,
    software        TEXT
);
CREATE INDEX IF NOT EXISTS node_sessions_site ON node_sessions (site_id, id);
CREATE TABLE IF NOT EXISTS captures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    t_ns       INTEGER NOT NULL,
    saved_ns   INTEGER NOT NULL,
    event_id   TEXT,
    channel_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    path       TEXT NOT NULL UNIQUE,
    bytes      INTEGER NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0
);
"""


class StoreError(RuntimeError):
    """The store could not do what was asked: it is not open, the database is
    unusable, or a statement failed. The message names the operation."""


@dataclass(frozen=True)
class StoredConfig:
    """One row of ``config_versions``.

    version: the configuration version (the console's ETag). text: the document as
    YAML or JSON, without its ``version`` field (the column is authoritative).
    fmt: ``"yaml"`` or ``"json"``. source: ``"file"`` or ``"api"``. saved_ns: when it
    was stored, integer UTC ns. run_id: the server run that stored it.
    """

    version: int
    text: str
    fmt: str
    source: str
    saved_ns: int
    run_id: str


@dataclass(frozen=True)
class StoredScenario:
    """One row of ``scenario_versions``: ``version``, scenario ``text`` (YAML),
    ``source`` (what changed it), scenario ``name``, ``saved_ns`` and ``run_id``."""

    version: int
    text: str
    source: str
    name: str
    saved_ns: int
    run_id: str


@dataclass(frozen=True)
class StoredEvent:
    """One row of ``events``: the console ``Event`` document ``doc`` (JSON text) with
    its ``event_id``, sequence number, ``state``, the ``run_id`` that created it and
    its ``created_ns`` / ``updated_ns`` (integer UTC ns)."""

    event_id: str
    seq: int
    run_id: str
    state: str
    created_ns: int
    updated_ns: int
    doc: str


@dataclass(frozen=True)
class PruneResult:
    """What one ``prune_history`` deleted: events (with their history rows),
    calibrations and node sessions."""

    events: int
    calibrations: int
    node_sessions: int


@dataclass(frozen=True)
class SavedCapture:
    """A capture written to disk: ``data_path`` (``.sigmf-data``, complex64 little
    endian), ``meta_path`` (``.sigmf-meta`` JSON), ``bytes`` of the data file, and
    ``deleted``: older captures removed to stay within the retention limit."""

    data_path: Path
    meta_path: Path
    bytes: int
    deleted: tuple[Path, ...]


def _now_ns() -> int:
    return time.time_ns()


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name}: must be an integer, got {type(value).__name__}")
    return int(value)


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}: must be a non-empty string")
    return value


def _finite(value: Any, name: str) -> float:
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name}: must be finite, got {value!r}")
    return out


class Store:
    """The server's SQLite database and capture directory.

    Construct, then ``await open()``; ``await close()`` when done. Every method that
    touches the disk must be called on the event loop that opened the store.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        save_captures: bool = True,
        capture_limit_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES,
        history_max_age_s: float | None = None,
        history_max_events: int | None = None,
        clock: Callable[[], int] = _now_ns,
    ) -> None:
        """
        data_dir: directory for the database and ``captures/``; created when missing.
        save_captures: whether ``save_capture`` writes IQ to disk (False: it returns
            None and writes nothing).
        capture_limit_bytes: total size of capture data files kept; the oldest are
            deleted beyond it (> 0).
        history_max_age_s: delete events not updated for longer than this, seconds
            (> 0), with their history rows, and older calibrations and node sessions;
            None keeps everything.
        history_max_events: keep at most this many events (>= 1), the most recently
            updated; None for no limit.
        clock: integer UTC ns, for the times the store stamps itself (and the age of
            the history).
        Raises ValueError naming a bad argument.
        """
        self._data_dir = Path(data_dir)
        self._save_captures = bool(save_captures)
        limit = _require_int(capture_limit_bytes, "capture_limit_bytes")
        if limit <= 0:
            raise ValueError("capture_limit_bytes: must be positive")
        self._capture_limit = limit
        if history_max_age_s is not None:
            age = _finite(history_max_age_s, "history_max_age_s")
            if age <= 0:
                raise ValueError("history_max_age_s: must be positive")
            self._max_age_ns: int | None = int(age * 1e9)
        else:
            self._max_age_ns = None
        if history_max_events is not None:
            count = _require_int(history_max_events, "history_max_events")
            if count < 1:
                raise ValueError("history_max_events: must be at least 1")
            self._max_events: int | None = count
        else:
            self._max_events = None
        self._last_prune_ns: int | None = None
        self._clock = clock
        self._run_id = uuid.uuid4().hex[:12]
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._conn: sqlite3.Connection | None = None  # touched only on the worker thread
        self._pending: set[asyncio.Future[Any]] = set()
        self._state: Literal["new", "open", "closing", "closed"] = "new"
        self._counters: Counter[str] = Counter()
        self._previous_run: str | None = None

    # ------------------------------------------------------------------ properties

    @property
    def data_dir(self) -> Path:
        """The data directory."""
        return self._data_dir

    @property
    def path(self) -> Path:
        """The database file."""
        return self._data_dir / DB_FILE_NAME

    @property
    def captures_dir(self) -> Path:
        """Where IQ captures are written."""
        return self._data_dir / CAPTURE_DIR_NAME

    @property
    def run_id(self) -> str:
        """Identifier of this server run (stored with everything it writes)."""
        return self._run_id

    @property
    def previous_run_id(self) -> str | None:
        """The run before this one, or None for a fresh data directory (set by ``open``)."""
        return self._previous_run

    @property
    def is_open(self) -> bool:
        """Whether ``open`` has completed and ``close`` has not started."""
        return self._state == "open"

    @property
    def save_captures_enabled(self) -> bool:
        """Whether ``save_capture`` writes to disk."""
        return self._save_captures

    @property
    def capture_limit_bytes(self) -> int:
        """Total size of capture data files kept, bytes."""
        return self._capture_limit

    @property
    def history_limits(self) -> tuple[float | None, int | None]:
        """``(history_max_age_s, history_max_events)``; None where there is no limit."""
        age = None if self._max_age_ns is None else self._max_age_ns / 1e9
        return age, self._max_events

    def counters(self) -> dict[str, int]:
        """Diagnostics: writes done and failed per operation."""
        return dict(self._counters)

    @property
    def pending_writes(self) -> int:
        """Fire-and-forget writes not finished yet."""
        return len(self._pending)

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        """Create the directory and database if needed, apply the schema, register the
        run. Raises ``StoreError`` naming the file when the database cannot be used
        (not a database, unreadable, schema of a newer version)."""
        if self._state != "new":
            raise StoreError(f"open: the store is {self._state}")
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="petal-store"
        )
        try:
            await self._run("open", self._open_db)
        except BaseException:
            self._state = "closed"
            self._executor.shutdown(wait=True)
            self._executor = None
            raise
        self._state = "open"
        logger.info(
            "store opened at %s (run %s, previous run %s)",
            self.path,
            self._run_id,
            self._previous_run,
            extra={"path": str(self.path), "run_id": self._run_id},
        )
        if self._max_age_ns is not None or self._max_events is not None:
            await self.prune_history()

    async def close(self) -> None:
        """Wait for every pending write, record the end of the run and close the
        database. Idempotent. Never raises for a write that failed earlier (those were
        logged when they failed)."""
        if self._state in ("closing", "closed", "new"):
            if self._state == "new":
                self._state = "closed"
            return
        self._state = "closing"
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
        executor = self._executor
        assert executor is not None
        try:
            await asyncio.get_running_loop().run_in_executor(executor, self._close_db)
        except Exception:  # closing must finish whatever happened to the database
            logger.exception("store: closing the database failed")
        finally:
            # The worker is idle (its queue was drained above), so joining it is instant.
            executor.shutdown(wait=True)
            self._executor = None
            self._state = "closed"
        logger.info("store closed", extra={"path": str(self.path), "counters": dict(self._counters)})

    # -------------------------------------------------------------- worker plumbing

    def _check_open(self, what: str) -> concurrent.futures.ThreadPoolExecutor:
        if self._state != "open" or self._executor is None:
            raise StoreError(f"{what}: the store is {self._state}")
        return self._executor

    async def _run(self, what: str, fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn(conn, *args)`` on the worker thread and wait for it."""
        executor = self._executor if what == "open" else self._check_open(what)
        assert executor is not None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, functools.partial(self._guarded, what, fn, *args))

    def _submit(self, what: str, fn: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
        """Queue ``fn(conn, *args)`` on the worker thread without waiting; failures are
        logged and counted. Raises ``StoreError`` when ``MAX_PENDING_WRITES`` writes are
        already waiting (a stalled disk must not grow memory without bound; the caller
        logs what was not stored)."""
        executor = self._check_open(what)
        if len(self._pending) >= MAX_PENDING_WRITES:
            self._counters[f"{what}_refused_backlog"] += 1
            raise StoreError(f"{what}: {len(self._pending)} writes are already waiting (is the disk stalled?)")
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(executor, functools.partial(self._guarded, what, fn, *args))
        self._pending.add(future)
        future.add_done_callback(functools.partial(self._settled, what))
        return future

    def _settled(self, what: str, future: asyncio.Future[Any]) -> None:
        self._pending.discard(future)
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            logger.error(
                "store write %s failed: %s", what, exc, extra={"operation": what, "path": str(self.path)}
            )

    def _guarded(self, what: str, fn: Callable[..., T], *args: Any) -> T:
        """Worker-thread wrapper: count, and turn database errors into StoreError."""
        try:
            if what == "open":
                result = fn(*args)
            else:
                if self._conn is None:
                    raise StoreError(f"{what}: the database is not open")
                result = fn(self._conn, *args)
        except sqlite3.Error as exc:
            self._counters[f"{what}_failed"] += 1
            raise StoreError(f"{what}: {exc}") from exc
        except (OSError, ValueError, TypeError) as exc:
            self._counters[f"{what}_failed"] += 1
            raise StoreError(f"{what}: {exc}") from exc
        except StoreError:
            self._counters[f"{what}_failed"] += 1
            raise
        self._counters[what] += 1
        return result

    def _open_db(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_S)
        except sqlite3.Error as exc:
            raise StoreError(f"open: {self.path}: {exc}") from exc
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise StoreError(f"open: {self.path}: the database refused WAL journalling ({mode})")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            with conn:
                row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
                    )
                elif int(row[0]) > SCHEMA_VERSION:
                    raise StoreError(
                        f"open: {self.path}: written by a newer version (schema {row[0]}, "
                        f"this server understands {SCHEMA_VERSION})"
                    )
                previous = conn.execute(
                    "SELECT run_id FROM runs ORDER BY started_ns DESC, rowid DESC LIMIT 1"
                ).fetchone()
                self._previous_run = None if previous is None else str(previous[0])
                now = self._clock()
                conn.execute("INSERT INTO runs (run_id, started_ns) VALUES (?, ?)", (self._run_id, now))
                conn.execute(
                    "UPDATE node_sessions SET reason = 'server stopped' "
                    "WHERE disconnected_ns IS NULL AND run_id != ?",
                    (self._run_id,),
                )
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise StoreError(f"open: {self.path} is not a usable database: {exc}") from exc
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def _close_db(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            with conn:
                conn.execute("UPDATE runs SET stopped_ns = ? WHERE run_id = ?", (self._clock(), self._run_id))
        finally:
            conn.close()

    # ---------------------------------------------------------------- configuration

    async def save_config(self, text: str, fmt: str, source: ConfigSource) -> int:
        """Store a configuration document; returns its new version (one more than the
        largest stored, 1 for the first). ``text`` should not carry the version."""
        _require_text(text, "text")
        if fmt not in ("yaml", "json"):
            raise ValueError(f"fmt: must be 'yaml' or 'json', got {fmt!r}")
        if source not in ("file", "api"):
            raise ValueError(f"source: must be 'file' or 'api', got {source!r}")

        def work(conn: sqlite3.Connection) -> int:
            with conn:
                row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM config_versions").fetchone()
                version = int(row[0]) + 1
                conn.execute(
                    "INSERT INTO config_versions (version, saved_ns, source, fmt, text, run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (version, self._clock(), source, fmt, text, self._run_id),
                )
            return version

        return await self._run("save_config", work)

    async def latest_config(self) -> StoredConfig | None:
        """The configuration with the largest version, or None when none is stored."""
        return await self._run("latest_config", self._config_row, None)

    async def config_version(self, version: int) -> StoredConfig | None:
        """The stored configuration of one version, or None."""
        return await self._run("config_version", self._config_row, _require_int(version, "version"))

    @staticmethod
    def _config_row(conn: sqlite3.Connection, version: int | None) -> StoredConfig | None:
        sql = "SELECT version, text, fmt, source, saved_ns, run_id FROM config_versions "
        if version is None:
            row = conn.execute(sql + "ORDER BY version DESC LIMIT 1").fetchone()
        else:
            row = conn.execute(sql + "WHERE version = ?", (version,)).fetchone()
        if row is None:
            return None
        return StoredConfig(int(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4]), str(row[5]))

    # -------------------------------------------------------------------- scenario

    async def save_scenario(self, text: str, name: str, source: str) -> int:
        """Store a scenario document; returns its new version (one more than the
        largest stored). ``source`` says what changed it (``file``, ``api``,
        ``preset``, ``reset``, ``config``)."""
        _require_text(text, "text")
        _require_text(source, "source")
        if not isinstance(name, str):
            raise ValueError("name: must be a string")

        def work(conn: sqlite3.Connection) -> int:
            with conn:
                row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM scenario_versions").fetchone()
                version = int(row[0]) + 1
                conn.execute(
                    "INSERT INTO scenario_versions (version, saved_ns, source, name, text, run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (version, self._clock(), source, name, text, self._run_id),
                )
            return version

        return await self._run("save_scenario", work)

    async def latest_scenario(self) -> StoredScenario | None:
        """The scenario with the largest version, or None."""

        def work(conn: sqlite3.Connection) -> StoredScenario | None:
            row = conn.execute(
                "SELECT version, text, source, name, saved_ns, run_id FROM scenario_versions "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return StoredScenario(int(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4]), str(row[5]))

        return await self._run("latest_scenario", work)

    # ---------------------------------------------------------------------- events

    async def max_event_seq(self) -> int:
        """The largest event sequence number stored (0 when none)."""

        def work(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
            return int(row[0])

        return await self._run("max_event_seq", work)

    async def load_events(self, limit: int = 500) -> list[StoredEvent]:
        """The ``limit`` most recently updated events, newest first."""
        n = _require_int(limit, "limit")
        if n <= 0:
            raise ValueError("limit: must be positive")

        def work(conn: sqlite3.Connection) -> list[StoredEvent]:
            rows = conn.execute(
                "SELECT event_id, seq, run_id, state, created_ns, updated_ns, doc FROM events "
                "ORDER BY updated_ns DESC, seq DESC LIMIT ?",
                (n,),
            ).fetchall()
            return [
                StoredEvent(str(r[0]), int(r[1]), str(r[2]), str(r[3]), int(r[4]), int(r[5]), str(r[6]))
                for r in rows
            ]

        return await self._run("load_events", work)

    async def event_transitions(self, event_id: str) -> list[tuple[int, str, str, str | None]]:
        """Stored transitions of one event, oldest first: ``(t_ns, state, verdict, reason)``."""

        def work(conn: sqlite3.Connection) -> list[tuple[int, str, str, str | None]]:
            rows = conn.execute(
                "SELECT t_ns, state, verdict, reason FROM event_transitions WHERE event_id = ? ORDER BY id",
                (event_id,),
            ).fetchall()
            return [(int(r[0]), str(r[1]), str(r[2]), None if r[3] is None else str(r[3])) for r in rows]

        return await self._run("event_transitions", work)

    async def count_rows(self, table: str, event_id: str | None = None) -> int:
        """Number of rows of an append-only table (diagnostics and tests)."""
        allowed = {
            "event_transitions",
            "evidence_snapshots",
            "measurements",
            "solutions",
            "calibrations",
            "node_sessions",
            "captures",
            "events",
            "config_versions",
            "scenario_versions",
        }
        if table not in allowed:
            raise ValueError(f"table: unknown table {table!r}")

        def work(conn: sqlite3.Connection) -> int:
            if event_id is None:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 - whitelisted
            else:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE event_id = ?", (event_id,)  # noqa: S608
                ).fetchone()
            return int(row[0])

        return await self._run("count_rows", work)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run a read-only ``SELECT`` (diagnostics and tests). Raises ValueError for
        anything that is not a single SELECT statement."""
        text = sql.strip()
        if not text.lower().startswith("select") or ";" in text.rstrip(";"):
            raise ValueError("sql: only a single SELECT statement is allowed")

        def work(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
            return [tuple(r) for r in conn.execute(text, tuple(params)).fetchall()]

        return await self._run("query", work)

    def put_event(
        self,
        *,
        event_id: str,
        seq: int,
        created_ns: int,
        updated_ns: int,
        state: str,
        verdict: str,
        first_seen_ns: int,
        last_seen_ns: int,
        uplink_lo_hz: float | None,
        uplink_hi_hz: float | None,
        doc: str,
    ) -> asyncio.Future[Any]:
        """Insert or replace the row of one event (fire-and-forget)."""
        args = (
            _require_text(event_id, "event_id"),
            _require_int(seq, "seq"),
            self._run_id,
            _require_int(created_ns, "created_ns"),
            _require_int(updated_ns, "updated_ns"),
            state,
            verdict,
            _require_int(first_seen_ns, "first_seen_ns"),
            _require_int(last_seen_ns, "last_seen_ns"),
            None if uplink_lo_hz is None else _finite(uplink_lo_hz, "uplink_lo_hz"),
            None if uplink_hi_hz is None else _finite(uplink_hi_hz, "uplink_hi_hz"),
            _require_text(doc, "doc"),
        )

        def work(conn: sqlite3.Connection) -> None:
            with conn:
                # The run that created an event keeps owning it.
                conn.execute(
                    "INSERT INTO events (event_id, seq, run_id, created_ns, updated_ns, state, verdict, "
                    "first_seen_ns, last_seen_ns, uplink_lo_hz, uplink_hi_hz, doc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(event_id) DO UPDATE SET updated_ns = excluded.updated_ns, "
                    "state = excluded.state, verdict = excluded.verdict, "
                    "first_seen_ns = excluded.first_seen_ns, last_seen_ns = excluded.last_seen_ns, "
                    "uplink_lo_hz = excluded.uplink_lo_hz, uplink_hi_hz = excluded.uplink_hi_hz, "
                    "doc = excluded.doc",
                    args,
                )

        future = self._submit("put_event", work)
        self._maybe_prune()
        return future

    # --------------------------------------------------------------------- retention

    def _prune_work(self, conn: sqlite3.Connection) -> PruneResult:
        now = self._clock()
        victims: set[str] = set()
        with conn:
            if self._max_age_ns is not None:
                cutoff = now - self._max_age_ns
                victims.update(
                    str(r[0]) for r in conn.execute("SELECT event_id FROM events WHERE updated_ns < ?", (cutoff,))
                )
            if self._max_events is not None:
                victims.update(
                    str(r[0])
                    for r in conn.execute(
                        "SELECT event_id FROM events ORDER BY updated_ns DESC, seq DESC LIMIT -1 OFFSET ?",
                        (self._max_events,),
                    )
                )
            ids = sorted(victims)
            for start in range(0, len(ids), _DELETE_CHUNK):
                chunk = ids[start:start + _DELETE_CHUNK]
                marks = ",".join("?" * len(chunk))
                for table in _EVENT_TABLES:
                    conn.execute(f"DELETE FROM {table} WHERE event_id IN ({marks})", chunk)  # noqa: S608
                conn.execute(f"DELETE FROM events WHERE event_id IN ({marks})", chunk)  # noqa: S608
            calibrations = sessions = 0
            if self._max_age_ns is not None:
                cutoff = now - self._max_age_ns
                calibrations = conn.execute("DELETE FROM calibrations WHERE t_ns < ?", (cutoff,)).rowcount
                sessions = conn.execute(
                    "DELETE FROM node_sessions WHERE disconnected_ns IS NOT NULL AND disconnected_ns < ?",
                    (cutoff,),
                ).rowcount
        return PruneResult(events=len(ids), calibrations=max(calibrations, 0), node_sessions=max(sessions, 0))

    def _log_prune(self, result: PruneResult) -> None:
        if result.events or result.calibrations or result.node_sessions:
            logger.info(
                "history retention: deleted %d event(s), %d calibration(s), %d node session(s)",
                result.events, result.calibrations, result.node_sessions,
                extra={"max_age_s": self.history_limits[0], "max_events": self._max_events},
            )

    async def prune_history(self) -> PruneResult:
        """Apply the history limits now (see the module docstring); returns what was
        deleted. Does nothing (all zero) when no limit is set."""
        if self._max_age_ns is None and self._max_events is None:
            return PruneResult(0, 0, 0)
        self._last_prune_ns = self._clock()
        result = await self._run("prune_history", self._prune_work)
        self._log_prune(result)
        return result

    def _maybe_prune(self) -> None:
        """Queue a prune when limits are set and the last one is ``PRUNE_INTERVAL_S`` old."""
        if self._max_age_ns is None and self._max_events is None:
            return
        now = self._clock()
        if self._last_prune_ns is not None and now - self._last_prune_ns < PRUNE_INTERVAL_S * 1e9:
            return
        self._last_prune_ns = now
        try:
            future = self._submit("prune_history", self._prune_work)
        except StoreError:
            return
        future.add_done_callback(
            lambda f: None if f.cancelled() or f.exception() is not None else self._log_prune(f.result())
        )

    def add_transitions(
        self, event_id: str, rows: Iterable[tuple[int, str, str, str | None]]
    ) -> asyncio.Future[Any]:
        """Append transitions ``(t_ns, state, verdict, reason)`` of one event."""
        data = [(event_id, _require_int(t, "t_ns"), s, v, r) for t, s, v, r in rows]

        def work(conn: sqlite3.Connection) -> None:
            with conn:
                conn.executemany(
                    "INSERT INTO event_transitions (event_id, t_ns, state, verdict, reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    data,
                )

        return self._submit("add_transitions", work)

    def add_evidence_snapshot(self, event_id: str, t_ns: int, rows_json: str) -> asyncio.Future[Any]:
        """Append the evidence rows of one event at one time (JSON array text)."""
        args = (_require_text(event_id, "event_id"), _require_int(t_ns, "t_ns"), _require_text(rows_json, "rows_json"))

        def work(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute("INSERT INTO evidence_snapshots (event_id, t_ns, rows) VALUES (?, ?, ?)", args)

        return self._submit("add_evidence_snapshot", work)

    def add_measurement(
        self, event_id: str, seq: int, t_ns: int, kind: str, used: bool, row_json: str
    ) -> asyncio.Future[Any]:
        """Append one measurement row (the console ``MeasurementRow`` as JSON)."""
        args = (
            _require_text(event_id, "event_id"),
            _require_int(seq, "seq"),
            _require_int(t_ns, "t_ns"),
            kind,
            1 if used else 0,
            _require_text(row_json, "row_json"),
        )

        def work(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(
                    "INSERT INTO measurements (event_id, seq, t_ns, kind, used, row) VALUES (?, ?, ?, ?, ?, ?)",
                    args,
                )

        return self._submit("add_measurement", work)

    def add_solution(self, event_id: str, t_ns: int, converged: bool, solution_json: str) -> asyncio.Future[Any]:
        """Append one solution (``Solution.as_dict()`` as JSON)."""
        args = (_require_text(event_id, "event_id"), _require_int(t_ns, "t_ns"), 1 if converged else 0,
                _require_text(solution_json, "solution_json"))

        def work(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(
                    "INSERT INTO solutions (event_id, t_ns, converged, solution) VALUES (?, ?, ?, ?)", args
                )

        return self._submit("add_solution", work)

    def add_calibration(
        self,
        *,
        t_ns: int,
        carrier_id: str,
        kind: str,
        a_sat_id: str,
        a_site_id: str,
        b_sat_id: str,
        b_site_id: str,
        correction: float,
        sigma: float,
        detail: Mapping[str, Any] | None = None,
    ) -> asyncio.Future[Any]:
        """Append one reference calibration (MATH.md section 9): the correction of a
        pair and kind measured on a reference carrier, with its sigma (s or Hz)."""
        if kind not in ("tdoa", "fdoa"):
            raise ValueError(f"kind: must be 'tdoa' or 'fdoa', got {kind!r}")
        args = (
            _require_int(t_ns, "t_ns"),
            _require_text(carrier_id, "carrier_id"),
            kind,
            _require_text(a_sat_id, "a_sat_id"),
            _require_text(a_site_id, "a_site_id"),
            _require_text(b_sat_id, "b_sat_id"),
            _require_text(b_site_id, "b_site_id"),
            _finite(correction, "correction"),
            _finite(sigma, "sigma"),
            json.dumps(dict(detail or {}), allow_nan=False, default=str),
        )

        def work(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(
                    "INSERT INTO calibrations (t_ns, carrier_id, kind, a_sat_id, a_site_id, b_sat_id, "
                    "b_site_id, correction, sigma, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    args,
                )

        return self._submit("add_calibration", work)

    # ------------------------------------------------------------------ node sessions

    def node_session_opened(
        self,
        site_id: str,
        node_id: str | None,
        connected_ns: int,
        driver: str | None = None,
        software: str | None = None,
    ) -> asyncio.Future[Any]:
        """Record that a node session started. Any session of the site still open in
        this run is closed first (reason ``replaced``)."""
        args = (_require_text(site_id, "site_id"), node_id, _require_int(connected_ns, "connected_ns"), driver, software)

        def work(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(
                    "UPDATE node_sessions SET disconnected_ns = ?, reason = 'replaced' "
                    "WHERE site_id = ? AND run_id = ? AND disconnected_ns IS NULL",
                    (args[2], args[0], self._run_id),
                )
                conn.execute(
                    "INSERT INTO node_sessions (site_id, node_id, run_id, connected_ns, driver, software) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (args[0], args[1], self._run_id, args[2], args[3], args[4]),
                )

        return self._submit("node_session_opened", work)

    def node_session_closed(self, site_id: str, disconnected_ns: int, reason: str) -> asyncio.Future[Any]:
        """Record that the open session of a site ended (no-op when none is open)."""
        args = (_require_int(disconnected_ns, "disconnected_ns"), str(reason), _require_text(site_id, "site_id"))

        def work(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(
                    "UPDATE node_sessions SET disconnected_ns = ?, reason = ? "
                    "WHERE site_id = ? AND run_id = ? AND disconnected_ns IS NULL",
                    (args[0], args[1], args[2], self._run_id),
                )

        return self._submit("node_session_closed", work)

    # ------------------------------------------------------------------- captures

    async def save_capture(
        self,
        iq: np.ndarray,
        *,
        t_first_ns: int,
        sample_rate_hz: float,
        center_hz: float,
        channel_id: str,
        capture_id: str,
        event_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> SavedCapture | None:
        """Write one IQ capture as a SigMF pair under ``captures_dir`` and apply the
        retention limit.

        iq: complex samples (written as complex64 little endian); do not modify it
            until this returns. t_first_ns: labelled time of ``iq[0]``, integer UTC ns.
        sample_rate_hz, center_hz: as the node used them. channel_id, capture_id,
            event_id: recorded in the metadata and the table. extra: more metadata
            (JSON-able) stored under ``petal:extra``.
        Returns the files written, or None when saving is disabled or the capture alone
        exceeds the limit (logged). Raises StoreError when the files cannot be written.
        """
        if not self._save_captures:
            return None
        samples = np.asarray(iq)
        if samples.ndim != 1 or samples.size == 0 or not np.iscomplexobj(samples):
            raise ValueError("iq: must be a non-empty 1-D complex array")
        t_first = _require_int(t_first_ns, "t_first_ns")
        fs = _finite(sample_rate_hz, "sample_rate_hz")
        fc = _finite(center_hz, "center_hz")
        size = int(samples.size) * 8
        if size > self._capture_limit:
            logger.warning(
                "capture %s (%d bytes) exceeds the capture retention limit (%d bytes); not saved",
                capture_id,
                size,
                self._capture_limit,
                extra={"capture_id": capture_id, "bytes": size},
            )
            return None
        stem = f"{t_first}_{_SAFE_NAME.sub('_', channel_id)}_{_SAFE_NAME.sub('_', capture_id)}"
        meta = {
            "global": {
                "core:datatype": "cf32_le",
                "core:sample_rate": fs,
                "core:version": "1.0.0",
                "core:description": f"PETAL Locate capture {capture_id} on channel {channel_id}",
                "petal:t_first_ns": str(t_first),
                "petal:channel_id": channel_id,
                "petal:capture_id": capture_id,
                "petal:event_id": event_id,
                "petal:extra": dict(extra or {}),
            },
            "captures": [
                {"core:sample_start": 0, "core:frequency": fc, "core:datetime": iso_from_ns(t_first)}
            ],
            "annotations": [],
        }
        meta_text = json.dumps(meta, allow_nan=False, default=str, indent=1)

        def work(conn: sqlite3.Connection) -> SavedCapture:
            directory = self.captures_dir
            directory.mkdir(parents=True, exist_ok=True)
            data_path = directory / f"{stem}.sigmf-data"
            meta_path = directory / f"{stem}.sigmf-meta"
            samples.astype("<c8", copy=False).tofile(data_path)
            meta_path.write_text(meta_text, encoding="utf-8")
            deleted: list[Path] = []
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO captures (t_ns, saved_ns, event_id, channel_id, capture_id, path, bytes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (t_first, self._clock(), event_id, channel_id, capture_id, str(data_path), size),
                )
                total = int(
                    conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM captures WHERE deleted = 0").fetchone()[0]
                )
                if total > self._capture_limit:
                    rows = conn.execute(
                        "SELECT id, path, bytes FROM captures WHERE deleted = 0 AND path != ? ORDER BY saved_ns, id",
                        (str(data_path),),
                    ).fetchall()
                    for row_id, path_text, nbytes in rows:
                        if total <= self._capture_limit:
                            break
                        old = Path(path_text)
                        for victim in (old, old.with_suffix(".sigmf-meta")):
                            try:
                                victim.unlink()
                            except FileNotFoundError:
                                pass
                        conn.execute("UPDATE captures SET deleted = 1 WHERE id = ?", (row_id,))
                        total -= int(nbytes)
                        deleted.append(old)
            return SavedCapture(data_path, meta_path, size, tuple(deleted))

        saved = await self._run("save_capture", work)
        if saved.deleted:
            logger.info(
                "capture retention: deleted %d older capture(s)",
                len(saved.deleted),
                extra={"deleted": len(saved.deleted), "limit_bytes": self._capture_limit},
            )
        return saved

    async def capture_bytes(self) -> int:
        """Total size of the capture data files currently kept, bytes."""

        def work(conn: sqlite3.Connection) -> int:
            return int(conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM captures WHERE deleted = 0").fetchone()[0])

        return await self._run("capture_bytes", work)


def store_threads() -> list[threading.Thread]:
    """Live worker threads of any store (tests use this to prove nothing is left behind)."""
    return [t for t in threading.enumerate() if t.name.startswith("petal-store")]
