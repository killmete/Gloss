"""Per-line language and "is this worth translating?" heuristics for OCR output.

Only three source languages are in scope (``zh``, ``en``, ``ja``), so script counting is enough:

- any kana makes a line Japanese,
- Han characters without kana make it Chinese (a kanji-only Japanese line is read as Chinese;
  that is a known blind spot, and the translator is told to auto-detect anyway),
- Latin letters alone make it English,
- anything else (Hangul, Thai, Cyrillic, ...) has ``lang=None`` but is still sent for
  translation, because a person may want it read.

A line with nothing to translate (digits, times, symbols, emoji, a bare identifier such as
``Lucky_77``) is marked ``needs_translation=False``.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["classify_line", "detect_lang"]

_IDENTIFIER = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_KANA_MARK = "ー"  # the prolonged sound mark appears in kana words, and in nothing else here


def _script_counts(text: str) -> dict[str, int]:
    counts = {"han": 0, "kana": 0, "latin": 0, "digit": 0, "other_letter": 0}
    for ch in text:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF and ch != _KANA_MARK or 0x31F0 <= code <= 0x31FF:
            counts["kana"] += 1
        elif (
            0x4E00 <= code <= 0x9FFF
            or 0x3400 <= code <= 0x4DBF
            or 0x20000 <= code <= 0x2A6DF
            or 0xF900 <= code <= 0xFAFF
        ):
            counts["han"] += 1
        elif ch.isascii() and ch.isalpha() or 0xC0 <= code <= 0x24F and ch.isalpha():
            counts["latin"] += 1
        elif ch.isdigit():
            counts["digit"] += 1
        elif unicodedata.category(ch)[0] == "L":
            counts["other_letter"] += 1
    return counts


def detect_lang(text: str) -> str | None:
    """Return ``zh``, ``ja``, ``en`` or None when the line has no letters in those scripts."""
    counts = _script_counts(text)
    if counts["kana"]:
        return "ja"
    if counts["han"]:
        return "zh"
    if counts["latin"] and not counts["other_letter"]:
        return "en"
    return None


def classify_line(text: str, skip_langs: tuple[str, ...] = ()) -> tuple[str | None, bool]:
    """Return ``(lang, needs_translation)`` for one line.

    ``skip_langs`` are languages that need no translation, normally the target language.
    """
    text = text.strip()
    counts = _script_counts(text)
    lang = detect_lang(text)
    letters = counts["han"] + counts["kana"] + counts["latin"] + counts["other_letter"]
    if letters == 0:
        return lang, False  # numbers, times, punctuation, emoji, UI glyphs
    if lang in skip_langs:
        return lang, False
    if letters + counts["digit"] <= 1 and not counts["han"] + counts["kana"]:
        return lang, False  # one stray letter: an emoji or an icon read as text
    if lang == "en" and _IDENTIFIER.match(text) and (counts["digit"] or "_" in text or "@" in text):
        return lang, False  # a lone name or ID such as Lucky_77, not a sentence
    return lang, True
