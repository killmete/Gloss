"""A small shared base for the SQLite-backed cache and spend ledger.

Both live in one database file (``storage.database``) but use separate connections. WAL mode
lets them coexist. Every ``sqlite3`` error is wrapped in a Gloss error, as the storage ports
require, and messages never contain row contents.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import weakref
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from gloss.core.contracts import PermanentError, TransientError

T = TypeVar("T")

log = logging.getLogger(__name__)

# Recovery design: one module-level lock plus a registry of live stores per file. The store that
# hits corruption closes every store's connection on that file first (Windows cannot rename an
# open file), then renames the file set aside, and all of them reopen a fresh database.
_RECOVERY_LOCK = threading.RLock()
_LIVE: dict[str, weakref.WeakSet[SqliteStore]] = {}

DEFAULT_DB_NAME = "gloss.sqlite3"
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
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self.recovered = 0  # times a damaged database was set aside; the shell may tell the user
        with _RECOVERY_LOCK:
            _LIVE.setdefault(str(self._file.resolve()), weakref.WeakSet()).add(self)

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self._file, timeout=BUSY_TIMEOUT_S, check_same_thread=False, isolation_level=None
            )
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(self.SCHEMA)
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

    def _recover(self) -> None:
        """Set the damaged file aside (never delete) so a fresh one can start."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        with _RECOVERY_LOCK:
            siblings = list(_LIVE.get(str(self._file.resolve()), ()))
            for store in {self, *siblings}:
                conn, store._conn = store._conn, None
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
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
            for store in {self, *siblings}:
                store.recovered += 1
        log.warning("The local database was damaged; it was set aside and a new one started")

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
