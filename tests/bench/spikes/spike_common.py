"""Shared helpers for the Phase 2 translation spikes. Never prints or stores the API key."""

import json
import re
import sys
from pathlib import Path

import keyring
import yaml

ROOT = Path(__file__).resolve().parents[3]
GLOSSARY = ROOT / "docs" / "glossary" / "zh_gaming.yaml"

PRICES = {  # USD per token: in, cache write 5m, cache read, out
    "claude-haiku-5-5": (0.10e-6, 0.125e-6, 0.01e-6, 0.50e-6),
    "claude-sonnet-5-5": (2e-6, 2.5e-6, 0.10e-6, 10e-6),
}

SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "en": {"type": "string"}},
                "required": ["i", "en"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}

STYLE = (
    "You translate lines of text read off a screen (Chinese chat rooms, order cards, UI labels) "
    "into natural English.\n"
    'Input: a JSON list of {"i": id, "t": source text}. Output: JSON {"translations": '
    '[{"i": id, "en": English}]} with every id exactly once.\n'
    "Style: casual and natural, as a friend would say it. Keep usernames, IDs, numbers and emoji "
    "unchanged (transliterate Chinese usernames to pinyin only when no Latin form exists). "
    "UI labels get short title-style English. Use the glossary renderings below whenever the "
    "term appears. Output only the translation: no commentary, no notes, no explanations, "
    "no reasoning."
)


def system_text():
    g = yaml.safe_load(GLOSSARY.read_text(encoding="utf-8"))
    rows = []
    for t in sorted(g["terms"], key=lambda x: str(x["term"])):
        note = f" ({t['note']})" if t.get("note") else ""
        rows.append(f"{t['term']} => {t['en']}{note}")
    return STYLE + "\n\nGlossary (source => English):\n" + "\n".join(rows)


def system_blocks():
    return [{"type": "text", "text": system_text(), "cache_control": {"type": "ephemeral"}}]


def user_payload(items):
    """items: list of (id, text)."""
    return json.dumps([{"i": i, "t": t} for i, t in items], ensure_ascii=False)


_KEY = None


def api_key():
    global _KEY
    if _KEY is None:
        _KEY = keyring.get_password("gloss/anthropic", "api_key")
        if not _KEY:
            sys.exit("no key in keyring")
    return _KEY


def scrub(text):
    s = str(text)
    if _KEY:
        s = s.replace(_KEY, "[KEY]")
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "[KEY]", s)


def client(**kw):
    import anthropic

    return anthropic.Anthropic(api_key=api_key(), max_retries=0, timeout=30.0, **kw)


def cost(model, u):
    pi, pw, pr, po = PRICES[model]
    return (
        (u.input_tokens or 0) * pi
        + (getattr(u, "cache_creation_input_tokens", 0) or 0) * pw
        + (getattr(u, "cache_read_input_tokens", 0) or 0) * pr
        + (u.output_tokens or 0) * po
    )


def pct(vals, p):
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)
