"""A small shared base for the SQLite-backed cache and spend ledger.

Both live in one database file (``storage.database``) but use separate connections. WAL mode
lets them coexist. Every ``sqlite3`` error is wrapped in a Gloss error, as the storage ports
require, and messages never contain row contents.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from gloss.core.contracts import PermanentError, TransientError

T = TypeVar("T")

DEFAULT_DB_NAME = "gloss.sqlite3"
BUSY_TIMEOUT_S = 5.0


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

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self._file, timeout=BUSY_TIMEOUT_S, check_same_thread=False, isolation_level=None
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(self.SCHEMA)
            self._conn = conn
        return self._conn

    def _run(self, work: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            try:
                return work(self._connection())
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "locked" in text or "busy" in text:
                    raise TransientError("the database is busy") from None
                raise PermanentError("the database could not be used") from None
            except (sqlite3.Error, UnicodeError, OverflowError):
                raise PermanentError("the database could not be used") from None

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
