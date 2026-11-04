"""The translation cache (SQLite).

Keyed per SPEC section 2 by ``(provider, model, target_lang, glossary_version,
normalised_text)``. The key is stored as a SHA-256 hash of those five fields, so the source
text is not kept in the database. ``model=None`` and the text ``"None"`` are different keys.

The cache holds the translator's output for already-redacted text, so it never contains a
redacted number. The TranslationService decides what may be written (no low-quality OCR
lines, no passthrough). This class only stores and returns.

To stop the file growing for ever, it keeps at most ``max_entries`` rows and drops the
least recently used ones in a batch when it goes over.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from gloss.core.contracts import CacheKey
from gloss.services.sqlite_store import SqliteStore

DEFAULT_MAX_ENTRIES = 50_000
_PRUNE_EVERY = 500  # puts between size checks


def key_hash(key: CacheKey) -> str:
    fields = [key.provider, key.model, key.target_lang, key.glossary_version, key.normalised_text]
    return hashlib.sha256(
        json.dumps(fields, ensure_ascii=False).encode("utf-8", "surrogatepass")
    ).hexdigest()


class SqliteTranslationCache(SqliteStore):
    """Implements ``TranslationCache``."""

    CONTRACT_PORT = "cache"
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS translations (
            key_hash TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            used_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS translations_used ON translations (used_at);
    """

    def __init__(self, path: Path, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        super().__init__(path)
        self._max_entries = max_entries
        self._puts = 0

    @classmethod
    def for_contract_test(cls, path: Path) -> SqliteTranslationCache:
        return cls(path)

    def get(self, key: CacheKey) -> str | None:
        digest = key_hash(key)

        def work(conn):
            row = conn.execute("SELECT text FROM translations WHERE key_hash = ?", (digest,))
            found = row.fetchone()
            if found is None:
                return None
            conn.execute(
                "UPDATE translations SET used_at = ? WHERE key_hash = ?", (time.time(), digest)
            )
            return found[0]

        return self._run(work)

    def put(self, key: CacheKey, text: str) -> None:
        digest = key_hash(key)
        self._puts += 1
        prune = self._puts % _PRUNE_EVERY == 0

        def work(conn):
            conn.execute(
                "INSERT INTO translations (key_hash, text, used_at) VALUES (?,?,?) "
                "ON CONFLICT(key_hash) DO UPDATE SET text = excluded.text, "
                "used_at = excluded.used_at",
                (digest, text, time.time()),
            )
            if prune:
                self._prune(conn)

        self._run(work)

    def _prune(self, conn) -> None:
        (count,) = conn.execute("SELECT COUNT(*) FROM translations").fetchone()
        excess = count - self._max_entries
        if excess > 0:
            # Drop a tenth extra, so pruning does not run again on the very next batch.
            drop = excess + self._max_entries // 10
            conn.execute(
                "DELETE FROM translations WHERE key_hash IN "
                "(SELECT key_hash FROM translations ORDER BY used_at LIMIT ?)",
                (drop,),
            )
