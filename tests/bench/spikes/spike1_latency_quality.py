"""Spike 1: latency / tokens / validity / leakage / quality for Haiku 5.5 settings and Sonnet 5.5.

usage: spike1_latency_quality.py <config> <outdir> [--smoke]
configs: haiku_off_low, haiku_low, haiku_default, sonnet_low
Per config: 1 warm-up call (not counted), then 50 single-line calls (every 2nd line) and
20 batches of 5 lines (all 100 lines), sequential. Hard budget stop via <outdir>/spend.txt.
Use tests/bench/spikes/out/ as <outdir>, so the output stays gitignored.
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import spike_common as sc
from lines_zh import LINES, WARMUP

CONFIGS = {
    "haiku_off_low": ("claude-haiku-5-5", {"thinking": {"type": "disabled"}, "effort": "low"}),
    "haiku_low": ("claude-haiku-5-5", {"effort": "low"}),
    "haiku_default": ("claude-haiku-5-5", {}),
    "sonnet_low": ("claude-sonnet-5-5", {"effort": "low"}),
}
BUDGET_STOP = 0.80
LEAK = re.compile(
    r"\b(let me|the user|i need to|i should|i'll translate|translating|wait,|hmm|note:|"
    r"here is|here's the|the glossary|according to)\b",
    re.I,
)


def spend_get(outdir):
    p = Path(outdir) / "spend.txt"
    return float(p.read_text()) if p.exists() else 0.0


def spend_add(outdir, v):
    p = Path(outdir) / "spend.txt"
    p.write_text(str(spend_get(outdir) + v))


def call(cl, model, extra, items):
    kw = {"max_tokens": 2048}
    out_cfg = {"format": {"type": "json_schema", "schema": sc.SCHEMA}}
    if "effort" in extra:
        out_cfg["effort"] = extra["effort"]
    if "thinking" in extra:
        kw["thinking"] = extra["thinking"]
    t0 = time.perf_counter()
    try:
        r = cl.messages.create(
            model=model,
            system=sc.system_blocks(),
            messages=[{"role": "user", "content": sc.user_payload(items)}],
            output_config=out_cfg,
            **kw,
        )
    except Exception as e:  # scrubbed
        return {
            "error": sc.scrub(f"{type(e).__name__}: {e}")[:300],
            "ms": (time.perf_counter() - t0) * 1000,
        }
    ms = (time.perf_counter() - t0) * 1000
    u = r.usage
    texts = [b.text for b in r.content if b.type == "text"]
    n_think = sum(1 for b in r.content if b.type == "thinking")
    res = {
        "ms": ms,
        "stop": r.stop_reason,
        "in": u.input_tokens,
        "cw": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cr": getattr(u, "cache_read_input_tokens", 0) or 0,
        "out": u.output_tokens,
        "thinking_blocks": n_think,
        "cost": sc.cost(model, u),
        "n_text_blocks": len(texts),
    }
    raw = "".join(texts)
    res["valid"] = False
    res["leak"] = False
    try:
        data = json.loads(raw)
        tr = data["translations"]
        ids = [t["i"] for t in tr]
        want = sorted(i for i, _ in items)
        res["valid"] = sorted(ids) == want and all(isinstance(t["en"], str) for t in tr)
        res["tr"] = {t["i"]: t["en"] for t in tr}
        res["leak"] = any(LEAK.search(t["en"]) or len(t["en"]) > 220 for t in tr)
    except Exception as e:
        res["parse_error"] = type(e).__name__
        res["raw_head"] = raw[:200]
    return res


def main():
    name, outdir = sys.argv[1], sys.argv[2]
    smoke = "--smoke" in sys.argv
    model, extra = CONFIGS[name]
    cl = sc.client()
    Path(outdir).mkdir(parents=True, exist_ok=True)
    rows = []

    def run(kind, items):
        if spend_get(outdir) > BUDGET_STOP:
            print("BUDGET STOP", flush=True)
            sys.exit(3)
        r = call(cl, model, extra, items)
        r["kind"] = kind
        r["ids"] = [i for i, _ in items]
        if "cost" in r:
            spend_add(outdir, r["cost"])
        return r

    warm = run("warmup", [(1, WARMUP)])
    print(
        name,
        "warmup",
        {k: warm.get(k) for k in ("ms", "stop", "in", "cw", "cr", "out", "valid", "error")},
        flush=True,
    )
    if smoke:
        print(warm.get("tr"))
        return
    for idx in range(0, 100, 2):
        rows.append(run("single", [(idx + 1, LINES[idx][1])]))
    for b in range(0, 100, 5):
        rows.append(run("batch5", [(k + 1, LINES[k][1]) for k in range(b, b + 5)]))
    Path(outdir, f"{name}.json").write_text(
        json.dumps({"warmup": warm, "rows": rows}, ensure_ascii=False)
    )
    for kind in ("single", "batch5"):
        rr = [r for r in rows if r["kind"] == kind and "error" not in r]
        ms = [r["ms"] for r in rr]
        print(
            name,
            kind,
            "n",
            len(rr),
            "err",
            sum(1 for r in rows if r["kind"] == kind and "error" in r),
            f"p50 {sc.pct(ms, 50):.0f} p95 {sc.pct(ms, 95):.0f}",
            "valid",
            sum(r["valid"] for r in rr),
            "leak",
            sum(r["leak"] for r in rr),
            "refusal",
            sum(r["stop"] == "refusal" for r in rr),
            "think_blocks",
            sum(r["thinking_blocks"] for r in rr),
            "out_avg {:.0f}".format(sum(r["out"] for r in rr) / max(1, len(rr))),
            "cost {:.4f}".format(sum(r["cost"] for r in rr)),
            flush=True,
        )
    print(f"cumulative spend {spend_get(outdir):.4f}")


if __name__ == "__main__":
    main()
