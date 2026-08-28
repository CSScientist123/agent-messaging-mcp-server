"""A small SQLite wrapper that makes "SQLite as a lightweight queue" safe.

The concurrency model is deliberately narrow:

- Exactly one writer thread ever issues a write. `write(fn)` hands `fn` to
  that thread via a queue and blocks the calling thread until it's done.
  Nothing outside this module ever opens a write transaction directly.
- Every write is wrapped in an explicit `BEGIN IMMEDIATE` / `COMMIT`, with a
  `ROLLBACK` on any exception. Python's implicit transaction handling is
  turned off (`isolation_level=None`) so this module controls transactions
  precisely instead of guessing at sqlite3's autocommit heuristics.
- Reads never go through the writer and never block on it: each reading
  thread gets its own connection (WAL mode lets readers and the writer work
  concurrently without blocking each other). That connection is opened
  read-only at the OS level (a `mode=ro` URI), not merely flagged read-only
  by a togglable pragma -- see `_new_reader_connection` below. A statement
  that tries to write through it fails because the underlying file
  descriptor has no write access, which cannot be undone by anything a
  caller can pass to `read()`.
- `:memory:` is the one exception to per-thread reader connections: an
  in-memory database is private to the connection that opened it, so there
  can only be one connection, ever. In that mode reads are also routed
  through the writer thread and its single shared connection -- see
  `_is_memory` below, which is the deliberate branch point for that case.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import TypeVar

from . import config

T = TypeVar("T")

# Sentinel placed on the job queue to tell the writer thread to shut down.
_STOP = object()

#: Columns added to the schema after a database may already have been created.
#: `_apply_schema_if_needed` only ever runs `schema.sql` against an empty
#: database, so a column added here to an existing table would silently be
#: missing from any database file created before the change -- and the first
#: read or write that touches it would fail with "no such column" instead of
#: at startup, where the cause is obvious. Each entry is reconciled by
#: `_reconcile_added_columns` on every open, not only a fresh one.
_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("message_queue", "summary_phase INTEGER NOT NULL DEFAULT 0 CHECK (summary_phase IN (0, 1))"),
    ("message_queue", "origin_behavior TEXT REFERENCES label_caps(behavior)"),
    ("message_queue",
     "awaiting_resolution INTEGER NOT NULL DEFAULT 0 "
     "CHECK (awaiting_resolution IN (0, 1))"),
)


class Database:
    """A thread-safe SQLite handle with one writer thread and many readers."""

    def __init__(
        self,
        path: str | Path | None = None,
        schema: str | Path | None = None,
    ) -> None:
        if path is None:
            resolved_path = config.db_path()
        else:
            resolved_path = path if str(path) == ":memory:" else Path(path)
        self._path: str = str(resolved_path)
        self._is_memory: bool = self._path == ":memory:"

        if not self._is_memory:
            # db_path() already runs this guard for the default path; do it
            # again here so an explicitly-passed disk path gets the same
            # protection instead of only the default one.
            config.assert_native_filesystem(Path(self._path))

        self._schema_path: Path = Path(schema) if schema is not None else config.schema_path()

        self._closed = False
        self._submit_lock = threading.Lock()
        self._job_queue: "queue.Queue" = queue.Queue()

        self._local = threading.local()
        self._reader_lock = threading.Lock()
        self._reader_conns: list[sqlite3.Connection] = []

        if self._is_memory:
            # A :memory: database exists only inside the connection that
            # created it -- there is no file another connection could open.
            # So, unlike the on-disk case, there is exactly one connection
            # for the whole Database instance, and it is owned by the
            # writer thread. Reads are marshalled through that same thread
            # (see read/read_one) instead of getting their own connections.
            self._shared_conn: sqlite3.Connection | None = self._new_connection()
            self._apply_schema_if_needed(self._shared_conn)
            self._reconcile_added_columns(self._shared_conn)
            self._configure_connection(self._shared_conn, wal=False)
        else:
            self._shared_conn = None
            bootstrap = self._new_connection()
            try:
                self._apply_schema_if_needed(bootstrap)
                self._reconcile_added_columns(bootstrap)
                self._configure_connection(bootstrap, wal=True)
            finally:
                bootstrap.close()

        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="messaging-db-writer", daemon=True
        )
        self._writer_thread.start()

    # -- connection setup -------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _new_reader_connection(self) -> sqlite3.Connection:
        """Open an on-disk reader connection that the OS itself refuses to write through.

        `mode=ro` in the URI opens the underlying file descriptor without
        write access -- this is not a connection *setting* like
        `PRAGMA query_only` that a later statement on the same connection can
        flip back off. Whatever a caller passes to `read()`, including
        `PRAGMA query_only = OFF`, this connection still cannot write,
        because the OS never gave it permission to in the first place.

        Only used for the on-disk case: a `:memory:` database has exactly
        one connection ever (see the module docstring), and there is no file
        a second, read-only connection could open.
        """
        # SQLite URI filenames percent-encode anything that isn't a plain
        # path character; the resolved path may not exist yet (a directory
        # component might), but by the time any reader connection is
        # actually opened the writer's bootstrap connection in __init__ has
        # already created and initialized the file.
        quoted = urllib.parse.quote(str(Path(self._path).resolve()))
        uri = f"file:{quoted}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _configure_connection(self, conn: sqlite3.Connection, *, wal: bool) -> None:
        # foreign_keys is per-connection, not stored in the file: every
        # connection this class ever opens must set it explicitly.
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if wal:
            conn.execute("PRAGMA journal_mode = WAL")

    def _apply_schema_if_needed(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()
        if row[0] == 0:
            script = self._schema_path.read_text()
            conn.executescript(script)

    def _reconcile_added_columns(self, conn: sqlite3.Connection) -> None:
        """Add any column listed in `_ADDITIVE_COLUMNS` that a database predates.

        This is the whole migration story this project has: there is no
        version table and no migration runner, because every schema change
        so far has been adding a column with a default. This method does
        exactly that and nothing more -- it does NOT drop a column, does NOT
        change a column's type, does NOT reorder columns, and has no way to
        express a non-additive change (renaming a column, tightening a
        constraint on existing data, splitting a table). A change like that
        needs a real migration, not another entry here.

        Runs on every open, including a fresh database that
        `_apply_schema_if_needed` just built from `schema.sql` -- checking
        `PRAGMA table_info` first, before ever issuing an ALTER, is what
        keeps that overwhelmingly common case a no-op instead of a write.
        """
        for table, ddl in _ADDITIVE_COLUMNS:
            columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not columns:
                # The table itself doesn't exist -- e.g. a schema change that
                # is still ahead of this database in some other way. Nothing
                # additive can be reconciled onto a table that isn't there.
                continue
            column_name = ddl.split(maxsplit=1)[0]
            existing = {row[1] for row in columns}
            if column_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    # -- writer thread ------------------------------------------------------

    def _writer_loop(self) -> None:
        conn = self._shared_conn if self._is_memory else self._new_connection()
        if not self._is_memory:
            self._configure_connection(conn, wal=True)
        try:
            while True:
                item = self._job_queue.get()
                if item is _STOP:
                    break
                fn, future, is_write = item
                if is_write:
                    self._run_write(conn, fn, future)
                else:
                    self._run_read(conn, fn, future)
        finally:
            if not self._is_memory:
                conn.close()

    @staticmethod
    def _run_write(conn: sqlite3.Connection, fn: Callable, future: "Future") -> None:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except BaseException as exc:  # pragma: no cover - defensive
            future.set_exception(exc)
            return
        try:
            result = fn(conn)
        except BaseException as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            future.set_exception(exc)
            return
        try:
            conn.execute("COMMIT")
        except BaseException as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            future.set_exception(exc)
            return
        future.set_result(result)

    @staticmethod
    def _run_read(conn: sqlite3.Connection, fn: Callable, future: "Future") -> None:
        # query_only is bracketed around just this job rather than left on
        # permanently, because this same connection (the :memory: shared
        # connection) is also the one write() uses. The writer thread
        # processes one job at a time, so there is no concurrent access to
        # race against while the flag is flipped.
        conn.execute("PRAGMA query_only = ON")
        try:
            result = fn(conn)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        finally:
            conn.execute("PRAGMA query_only = OFF")

    def _submit(self, fn: Callable, *, is_write: bool):
        with self._submit_lock:
            if self._closed:
                raise RuntimeError("Database is closed")
            future: "Future" = Future()
            self._job_queue.put((fn, future, is_write))
        return future.result()

    # -- public API ---------------------------------------------------------

    def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run `fn(conn)` on the single writer thread inside BEGIN IMMEDIATE.

        Commits on success, rolls back on any exception. The calling thread
        blocks until `fn` has run; exceptions raised inside `fn` propagate
        to the caller with their original type and traceback.
        """
        return self._submit(fn, is_write=True)

    def read(self, sql: str, params: Sequence | Mapping = ()) -> list[sqlite3.Row]:
        """Run a read-only query and return all matching rows."""
        if self._is_memory:
            return self._submit(lambda conn: conn.execute(sql, params).fetchall(), is_write=False)
        conn = self._reader_connection()
        return conn.execute(sql, params).fetchall()

    def read_one(self, sql: str, params: Sequence | Mapping = ()) -> sqlite3.Row | None:
        """Run a read-only query and return the first row, or None."""
        if self._is_memory:
            return self._submit(lambda conn: conn.execute(sql, params).fetchone(), is_write=False)
        conn = self._reader_connection()
        return conn.execute(sql, params).fetchone()

    def _reader_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_reader_connection()
            # foreign_keys/busy_timeout are per-connection settings, not
            # stored in the file, so every connection sets them explicitly
            # -- same as _configure_connection, but this connection was
            # opened `mode=ro` (see _new_reader_connection) so journal_mode
            # is never touched here; it's the writer's job, once, globally.
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            # The single-writer invariant is enforced by the connection's
            # OS-level read-only file descriptor (see
            # _new_reader_connection), not by this pragma -- query_only is
            # set here only as defense in depth. Unlike the pragma alone,
            # nothing a caller can pass to read() (including
            # "PRAGMA query_only = OFF") can undo the read-only file
            # descriptor underneath it.
            conn.execute("PRAGMA query_only = ON")
            self._local.conn = conn
            with self._reader_lock:
                self._reader_conns.append(conn)
        return conn

    def close(self) -> None:
        """Stop the writer thread and close all connections. Safe to call twice."""
        with self._submit_lock:
            if self._closed:
                return
            self._closed = True
            self._job_queue.put(_STOP)
        self._writer_thread.join()

        with self._reader_lock:
            conns, self._reader_conns = self._reader_conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False
