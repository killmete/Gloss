"""Spike 3: prompt caching with structured output (json_schema). Prints usage numbers only.

Calls: A1 (schema, fresh prefix), A2 (identical), A3 (same prefix, different user text),
B1 (no schema, same prefix), B2 (schema again after B). Uses Haiku 5.5, effort low.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import spike_common as sc
from lines_zh import LINES

MODEL = "claude-haiku-5-5"


def go(cl, label, items, schema=True, extra_sys=""):
    system = sc.system_blocks()
    out = {"effort": "low"}
    if schema:
        out["format"] = {"type": "json_schema", "schema": sc.SCHEMA}
    t0 = time.perf_counter()
    r = cl.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        output_config=out,
        messages=[{"role": "user", "content": sc.user_payload(items)}],
    )
    ms = (time.perf_counter() - t0) * 1000
    u = r.usage
    print(
        label,
        f"ms {ms:.0f}",
        "in",
        u.input_tokens,
        "cache_write",
        u.cache_creation_input_tokens,
        "cache_read",
        u.cache_read_input_tokens,
        "out",
        u.output_tokens,
        "stop",
        r.stop_reason,
        flush=True,
    )


def main():
    cl = sc.client()
    a = [(1, LINES[30][1]), (2, LINES[31][1])]
    b = [(1, LINES[60][1]), (2, LINES[80][1])]
    try:
        go(cl, "A1 schema fresh      ", a)
        go(cl, "A2 schema identical  ", a)
        go(cl, "A3 schema new text   ", b)
        go(cl, "B1 no schema         ", b, schema=False)
        go(cl, "B2 schema again      ", a)
    except Exception as e:
        print("ERROR", sc.scrub(f"{type(e).__name__}: {e}")[:300])


main()
