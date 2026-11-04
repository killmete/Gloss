"""Loads the glossary YAML and reloads it when the file changes.

The file (``storage.glossary_path``) is user-editable. ``GlossaryStore.current()`` checks the
file's size and modified time on each call, which is one cheap ``stat``, and re-reads it only
if either changed. There is no background thread.

``Glossary.version`` is a short hash of the entries, so it changes exactly when the content
changes. It goes into the cache key, so editing a term makes old translations of that text
miss the cache instead of serving the old wording. The ``version:`` field in the YAML is
ignored on purpose, because people forget to bump it.

A file that cannot be read or parsed never replaces a good glossary. The previous one stays
in use and ``last_error`` says what went wrong (never the file's contents).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from gloss.core.contracts import Glossary, GlossaryEntry

log = logging.getLogger(__name__)

EMPTY = Glossary()


def parse_glossary(data: Any) -> tuple[Glossary, list[str]]:
    """Build a Glossary from parsed YAML. Returns it with a list of skipped-entry warnings."""
    warnings: list[str] = []
    if not isinstance(data, Mapping):
        raise ValueError("the glossary file must be a YAML mapping with a 'terms' list")
    terms = data.get("terms") or []
    if not isinstance(terms, list):
        raise ValueError("'terms' must be a list")
    entries: list[GlossaryEntry] = []
    for index, raw in enumerate(terms, start=1):
        if not isinstance(raw, Mapping) or raw.get("term") is None or raw.get("en") is None:
            warnings.append(f"entry {index} needs both 'term' and 'en', so it was skipped")
            continue
        term, translation = str(raw["term"]).strip(), str(raw["en"]).strip()
        if not term or not translation:
            warnings.append(f"entry {index} has an empty 'term' or 'en', so it was skipped")
            continue
        note = raw.get("note")
        entries.append(
            GlossaryEntry(term, translation, str(note).strip() if note not in (None, "") else None)
        )
    entries.sort(key=lambda e: (e.term, e.translation, e.note or ""))  # stable prompt prefix
    return Glossary(_version_of(entries), tuple(entries)), warnings


def _version_of(entries: list[GlossaryEntry]) -> str:
    if not entries:
        return "0"
    payload = json.dumps([[e.term, e.translation, e.note] for e in entries], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def resolve_glossary_path(configured: str, *, root: Path | None = None) -> Path:
    """Turn ``storage.glossary_path`` into a file path. A relative one is read from the app root."""
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path
    if root is None:
        from gloss.core.config import default_config_path

        root = default_config_path().parent.parent
    return root / path


class GlossaryStore:
    """Holds the current glossary and reloads it when the file changes. Thread-safe."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._glossary = EMPTY
        self._stamp: tuple[int, int] | None = None
        self._loaded_once = False
        self.last_error: str | None = None
        self.warnings: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, glossary_path: str, *, root: Path | None = None) -> GlossaryStore:
        return cls(resolve_glossary_path(glossary_path, root=root))

    @property
    def path(self) -> Path:
        return self._path

    def current(self) -> Glossary:
        """Return the glossary, re-reading the file first if it changed since the last call."""
        with self._lock:
            try:
                info = os.stat(self._path)
                stamp: tuple[int, int] | None = (info.st_mtime_ns, info.st_size)
            except FileNotFoundError:
                stamp = None
            except OSError as exc:
                self._fail(f"cannot read the glossary file ({type(exc).__name__})")
                return self._glossary
            if self._loaded_once and stamp == self._stamp:
                return self._glossary
            self._reload(stamp)
            return self._glossary

    def _reload(self, stamp: tuple[int, int] | None) -> None:
        self._loaded_once = True
        self._stamp = stamp
        if stamp is None:
            self._glossary, self.warnings = EMPTY, ()
            self._fail("the glossary file was not found, so no glossary is used")
            return
        try:
            text = self._path.read_text(encoding="utf-8")
            glossary, warnings = parse_glossary(yaml.safe_load(text))
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
            self._fail(f"the glossary file could not be used ({type(exc).__name__})")
            return
        self._glossary, self.warnings = glossary, tuple(warnings)
        self.last_error = None
        log.info("Glossary loaded: %d terms (version %s)", len(glossary.entries), glossary.version)

    def _fail(self, message: str) -> None:
        if message != self.last_error:
            log.warning("%s", message)
        self.last_error = message
