"""Redacts long digit runs and emails before text leaves the PC, and restores them for display.

Each redacted piece becomes a numbered placeholder such as ``[#1]``, numbered per line. The
translator sees only the placeholder, the cache stores only the placeholder text (so it never
holds a card or phone number), and ``restore`` puts the original back in the translation.

What counts as sensitive:
- ``min_digits`` or more digits in a row (any Unicode digit, so full-width ones count);
- groups of at least three digits joined by single spaces or hyphens, whose digits add up to
  ``min_digits`` or more, such as ``4111 1111 1111 1111`` or ``138-1234-5678``. Dates like
  ``2024-11-04`` have short groups and are left alone;
- email addresses.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_EMAIL = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
_PLACEHOLDER = re.compile(r"\[\s*#\s*(\d+)\s*\]")


@dataclass(frozen=True, slots=True)
class Redacted:
    """Redacted text plus what was removed, in placeholder order (``[#1]`` is ``values[0]``)."""

    text: str
    values: tuple[str, ...] = ()


class Redactor:
    def __init__(self, min_digits: int) -> None:
        if min_digits < 1:
            raise ValueError("min_digits must be at least 1")
        self._min = min_digits
        self._pattern = re.compile(rf"{_EMAIL}|\d(?:[ \-]?\d)*")
        self._long_runs = re.compile(rf"\d{{{min_digits},}}")

    def redact(self, text: str) -> Redacted:
        values: list[str] = []

        def replace(match: re.Match[str]) -> str:
            found = match.group(0)
            if "@" in found or self._is_sensitive(found):
                values.append(found)
                return f"[#{len(values)}]"
            return self._long_runs.sub(hide_run, found)

        def hide_run(match: re.Match[str]) -> str:
            values.append(match.group(0))
            return f"[#{len(values)}]"

        return Redacted(self._pattern.sub(replace, text), tuple(values))

    def _is_sensitive(self, run: str) -> bool:
        groups = re.split(r"[ \-]", run)
        if sum(len(g) for g in groups) < self._min:
            return False
        # One unbroken run counts at any length. Joined groups need three digits each,
        # which keeps dates and "1 2 3 4 5 6 7 8" style lists readable.
        return len(groups) == 1 or all(len(g) >= 3 for g in groups)


def restore(text: str, values: Sequence[str]) -> str:
    """Put the original pieces back. Placeholders without a matching value are left as is."""
    if not values:
        return text

    def put_back(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return values[index] if 0 <= index < len(values) else match.group(0)

    return _PLACEHOLDER.sub(put_back, text)
