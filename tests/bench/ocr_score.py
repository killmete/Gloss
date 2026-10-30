"""Score _raw_<variant>.json (kept in _draft/) against the frozen, human-checked expected text.

uv run --no-project --python 3.12 --with rapidfuzz==3.14.6 python tests/bench/ocr_score.py
Metric (order-independent, because layouts are multi-column):
  for each expected line e (>=2 chars after normalising) take the best candidate among every
  OCR line and every pair of consecutive OCR lines; if the candidate is longer than e, compare e
  with the best-aligned window of len(e). err = Levenshtein(e, window) / len(e), capped at 1.
  char accuracy = 1 - sum(err*len)/sum(len). line recall = share of lines with err <= 0.2.
  No one-to-one consumption, so repeated labels can be credited twice (slightly optimistic).
"""

import json
import statistics
import unicodedata
from itertools import pairwise
from pathlib import Path

from rapidfuzz.distance import Levenshtein
from rapidfuzz.fuzz import partial_ratio_alignment

ROOT = Path(__file__).resolve().parents[2]
E = ROOT / "tests/golden/expected"  # frozen expected text, read only
D = E / "_draft"  # raw OCR output and miss reports
CAT = {
    1: "chat",
    2: "profile",
    3: "menu",
    4: "profile",
    5: "profile",
    6: "menu",
    7: "profile",
    8: "menu",
    9: "profile",
    10: "profile",
    11: "profile",
    12: "chat",
    13: "chat",
    14: "order card",
    15: "menu",
    16: "profile",
    17: "profile",
    18: "profile",
    19: "chat",
}


def norm(s):
    s = unicodedata.normalize("NFKC", s).replace("[?]", "")
    return "".join(c for c in s.lower() if unicodedata.category(c)[0] not in "PSZC")


def best_err(e, cands):
    best = 1.0
    for c in cands:
        if not c:
            continue
        if len(c) > len(e):
            a = partial_ratio_alignment(e, c)
            w = c[a.dest_start : a.dest_start + len(e)]
            err = Levenshtein.distance(e, w) / len(e)
        else:
            err = min(1.0, Levenshtein.distance(e, c) / len(e))
        best = min(best, err)
    return best


def score(exp_lines, ocr_lines):
    cands = ocr_lines + [a + b for a, b in pairwise(ocr_lines)]
    errs = [(e, best_err(e, cands)) for e in exp_lines]
    return errs


def main():
    stems = sorted(p.stem for p in E.glob("Screenshot*.txt"))
    res = {}
    for raw in sorted(D.glob("_raw_*.json")):
        data = json.loads(raw.read_text(encoding="utf-8"))
        per = {}
        miss_txt = []
        for i, st in enumerate(stems, 1):
            exp = [norm(x) for x in (E / f"{st}.txt").read_text(encoding="utf-8").splitlines()]
            exp = [e for e in exp if len(e) >= 2]
            key = f"img{i:02d}"
            ocr = [norm(ln["text"]) for ln in data["images"][key]["lines"]]
            errs = score(exp, ocr)
            tot = sum(len(e) for e, _ in errs)
            err_chars = sum(len(e) * x for e, x in errs)
            per[key] = {
                "cat": CAT[i],
                "chars": tot,
                "err": err_chars,
                "lines": len(errs),
                "found": sum(1 for _, x in errs if x <= 0.2),
                "ms": data["images"][key]["ms"],
                "n_ocr": len(ocr),
            }
            bad = [f"{x:.2f} {e}" for e, x in errs if x > 0.2]
            # OCR lines absent from the draft: either draft gaps or OCR hallucinations
            extra = [
                f"[extra] {ln['text']} (score {ln['score']:.2f})"
                for ln in data["images"][key]["lines"]
                if len(norm(ln["text"])) >= 2 and best_err(norm(ln["text"]), exp) > 0.3
            ]
            bad += extra
            miss_txt.append(f"== {key} {st}\n" + "\n".join(bad))
        (D / f"_misses_{data['variant']}.txt").write_text("\n".join(miss_txt), encoding="utf-8")
        res[data["variant"]] = {"per": per, "peak": data["peak_rss_mb"]}
    for v, r in res.items():
        print(f"\n### {v}  peak RSS {r['peak']:.0f} MB")
        cats = {}
        for k, p in r["per"].items():
            cats.setdefault(p["cat"], []).append(p)
            print(
                f"{k} {p['cat']:10s} acc {1 - p['err'] / max(1, p['chars']):.3f} "
                f"recall {p['found']}/{p['lines']} ocr_lines {p['n_ocr']} {p['ms']:.0f}ms"
            )
        for c, group in list(cats.items()) + [("ALL", [x for grp in cats.values() for x in grp])]:
            ch = sum(p["chars"] for p in group)
            er = sum(p["err"] for p in group)
            ln = sum(p["lines"] for p in group)
            fd = sum(p["found"] for p in group)
            ms = sorted(p["ms"] for p in group)
            p95 = ms[min(len(ms) - 1, int(0.95 * len(ms)))]
            print(
                f"CAT {c:10s} n={len(group)} acc {1 - er / ch:.3f} recall {fd}/{ln}={fd / ln:.3f} "
                f"missed/20 {(ln - fd) / ln * 20:.1f} "
                f"ms median {statistics.median(ms):.0f} p95 {p95:.0f}"
            )


main()
