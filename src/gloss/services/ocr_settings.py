"""Reading an OCR adapter's settings table. Wrong values raise PermanentError, never TypeError."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gloss.core.contracts import PermanentError

__all__ = ["read_bool", "read_languages", "read_number", "read_str"]


def read_languages(settings: Mapping[str, Any]) -> tuple[str, ...]:
    raw = settings.get("languages")
    if isinstance(raw, str):
        raw = [raw]
    if (
        not isinstance(raw, (list, tuple))
        or not raw
        or not all(isinstance(x, str) and x.strip() for x in raw)
    ):
        raise PermanentError("ocr.languages must be a non-empty list of codes such as ['zh','en']")
    return tuple(dict.fromkeys(x.strip().lower() for x in raw))


def read_number(
    settings: Mapping[str, Any],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> float:
    value = settings.get(key, default)
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if integer:
        ok = ok and float(value).is_integer()
    if not ok or not minimum <= value <= maximum:
        kind = "a whole number" if integer else "a number"
        raise PermanentError(f"ocr setting '{key}' must be {kind} from {minimum} to {maximum}")
    return int(value) if integer else float(value)


def read_bool(settings: Mapping[str, Any], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if not isinstance(value, bool):
        raise PermanentError(f"ocr setting '{key}' must be true or false")
    return value


def read_str(settings: Mapping[str, Any], key: str, default: str) -> str:
    value = settings.get(key, default)
    if not isinstance(value, str):
        raise PermanentError(f"ocr setting '{key}' must be text")
    return value
