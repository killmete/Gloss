"""A small shared base for the SQLite-backed cache and spend ledger.

Both live in one database file (``storage.database``) but use separate connections. WAL mode
lets them coexist. Every ``sqlite3`` error is wrapped in a Gloss error, as the storage ports
require, and messages never contain row contents.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from gloss.core.contracts import PermanentError, TransientError

T = TypeVar("T")

log = logging.getLogger(__name__)

# Recovery design: one module-level lock plus a registry of live stores per file. Lock order is
# always the store's own lock, then _RECOVERY_LOCK. The recovering store holds _RECOVERY_LOCK, so
# nobody can open the file meanwhile, takes each other store's lock (waiting for any call in
# progress), closes its connection, then renames the damaged files. A generation counter per file
# stops a second store that hit the same damage from setting the fresh database aside again.
_RECOVERY_LOCK = threading.RLock()
_LIVE: dict[str, weakref.WeakSet[SqliteStore]] = {}
_GENERATION: dict[str, int] = {}
LOCK_WAIT_S = 5.0


def _now() -> float:
    return time.time()


DEFAULT_DB_NAME = "gloss.sqlite3"
META_SCHEMA = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL NOT NULL);"
BUSY_TIMEOUT_S = 5.0


def _is_corrupt(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    text = str(exc).lower()
    return "malformed" in text or "not a database" in text


def database_file(path: Path) -> Path:
    """A directory gets the default file name inside it. A file path is used as given."""
    if path.is_dir() or not path.suffix:
        return path / DEFAULT_DB_NAME
    return path


class SqliteStore:
    """One connection, opened on first use, guarded by a lock so any thread may call."""

    SCHEMA = ""

    def __init__(self, path: Path) -> None:
        self._file = database_file(Path(path))
        self._key = str(self._file.resolve())
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._opened_gen = 0
        self.recovered = 0  # times a damaged database was set aside; the shell may tell the user
        with _RECOVERY_LOCK:
            _LIVE.setdefault(self._key, weakref.WeakSet()).add(self)

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            with _RECOVERY_LOCK:  # never open the file while another store is renaming it
                self._file.parent.mkdir(parents=True, exist_ok=True)
                self._opened_gen = _GENERATION.get(self._key, 0)
                conn = sqlite3.connect(
                    self._file,
                    timeout=BUSY_TIMEOUT_S,
                    check_same_thread=False,
                    isolation_level=None,
                )
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.executescript(META_SCHEMA + self.SCHEMA)
                except BaseException:
                    conn.close()
                    raise
                self._conn = conn
        return self._conn

    def _run(self, work: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            for attempt in (1, 2):
                try:
                    return work(self._connection())
                except sqlite3.Error as exc:
                    if _is_corrupt(exc) and attempt == 1:
                        self._recover()
                        continue
                    raise self._wrap(exc) from None
                except (UnicodeError, OverflowError):
                    raise PermanentError("the database could not be used") from None
            raise PermanentError("the database could not be used")  # pragma: no cover

    @staticmethod
    def _wrap(exc: sqlite3.Error) -> Exception:
        text = str(exc).lower()
        if isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
            return TransientError("the database is busy")
        return PermanentError("the database could not be used")

    def _close_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _recover(self) -> None:
        """Set the damaged file aside (never delete) so a fresh one can start.

        Called with this store's lock held.
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._close_connection()  # first, so a store waiting on us never waits on this one
        with _RECOVERY_LOCK:
            if _GENERATION.get(self._key, 0) != self._opened_gen:
                return  # another store already set the damaged file aside
            others = [s for s in list(_LIVE.get(self._key, ())) if s is not self]
            held: list[SqliteStore] = []
            try:
                deadline = time.monotonic() + LOCK_WAIT_S
                for store in others:
                    while not store._lock.acquire(timeout=0.05):
                        if store._conn is None:
                            break  # it is waiting for the recovery lock and holds no connection
                        if time.monotonic() > deadline:
                            raise PermanentError("the database could not be used")
                    else:
                        held.append(store)
                    store._close_connection()
                self._set_aside(stamp)
                self._write_reset_mark()
                _GENERATION[self._key] = _GENERATION.get(self._key, 0) + 1
                for store in (self, *others):
                    store.recovered += 1
            finally:
                for store in held:
                    store._lock.release()
        log.warning("The local database was damaged; it was set aside and a new one started")

    def _write_reset_mark(self) -> None:
        """Record the reset time in the fresh file now, so a restart cannot lose it."""
        try:
            conn = sqlite3.connect(self._file, timeout=BUSY_TIMEOUT_S, isolation_level=None)
            try:
                conn.executescript(META_SCHEMA)
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('reset_at', ?)", (_now(),))
            finally:
                conn.close()
        except sqlite3.Error:
            log.warning("Could not record when the local database was reset")

    def _set_aside(self, stamp: str) -> None:
        target = self._file.with_name(f"{self._file.name}.damaged-{stamp}")
        n = 1
        while any(
            target.with_name(target.name + suffix).exists() for suffix in ("", "-wal", "-shm")
        ):
            n += 1
            target = self._file.with_name(f"{self._file.name}.damaged-{stamp}-{n}")
        try:
            for suffix in ("", "-wal", "-shm"):
                src = self._file.with_name(self._file.name + suffix)
                if src.exists():
                    src.rename(target.with_name(target.name + suffix))
        except OSError:
            raise PermanentError("the database could not be used") from None

    def reset_at(self) -> float | None:
        """When a damaged database was last set aside and replaced, or None."""

        def work(conn):
            row = conn.execute("SELECT value FROM meta WHERE key = 'reset_at'").fetchone()
            return None if row is None else float(row[0])

        return self._run(work)

    def close(self) -> None:
        """Safe to call twice, or before anything was opened."""
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
