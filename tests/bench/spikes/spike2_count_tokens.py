"""Spike 2: real token counts via count_tokens (no generation). Prints only numbers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import spike_common as sc
from lines_zh import LINES

MODEL = "claude-haiku-5-5"


def count(cl, system, content):
    kw = {"model": MODEL, "messages": [{"role": "user", "content": content}]}
    if system:
        kw["system"] = system
    return cl.messages.count_tokens(**kw).input_tokens


def main():
    cl = sc.client()
    try:
        base = count(cl, None, "x")
        sys_only = count(cl, sc.system_text(), "x")
        print("baseline 1-char msg:", base, "system+glossary:", sys_only - base)
        sample = [LINES[k] for k in range(0, 100, 2)]  # 50 lines
        tot_chars = tot_tok = 0
        per_cat = {}
        for cat, text, _ in sample:
            n = count(cl, None, text) - base + 1  # minus overhead of 'x' (1 tok approx)
            tot_chars += len(text)
            tot_tok += n
            c = per_cat.setdefault(cat, [0, 0, 0])
            c[0] += len(text)
            c[1] += n
            c[2] += 1
        print(
            "50 lines: chars",
            tot_chars,
            "tokens",
            tot_tok,
            "tok/char %.2f" % (tot_tok / tot_chars),
            "tok/line %.1f" % (tot_tok / 50),
            "chars/line %.1f" % (tot_chars / 50),
        )
        for cat, (ch, tk, n) in per_cat.items():
            print(
                " ",
                cat,
                "n",
                n,
                f"chars/line {ch / n:.1f} tok/line {tk / n:.1f} tok/char {tk / ch:.2f}",
            )
        # JSON wrapper cost: batch of 5 in the real format vs raw text
        for size in (1, 5):
            items = [(k + 1, sample[k][1]) for k in range(size)]
            wrapped = count(cl, None, sc.user_payload(items)) - base + 1
            raw = sum(count(cl, None, t) - base + 1 for _, t in items)
            print(
                "batch",
                size,
                "wrapped tokens",
                wrapped,
                "raw tokens",
                raw,
                "wrapper overhead",
                wrapped - raw,
            )
        # English output tokens per line: count a typical English translation set
        en = [
            "Hey, are you free tonight?",
            "LOL I'm dying",
            "Boss, wanna team up for a game?",
            "Order complete, pending settlement",
            "Skill Card",
            "Boss tipped you 50 Cat Food",
            "Rank me up tonight, boss?",
            "Order cancelled, Diamonds refunded",
        ]
        et = sum(count(cl, None, t) - base + 1 for t in en)
        print(
            "english sample 8 lines:",
            et,
            f"tokens, avg {et / 8:.1f}/line, chars avg {sum(map(len, en)) / 8:.1f}",
        )
    except Exception as e:
        print("ERROR", sc.scrub(f"{type(e).__name__}: {e}")[:300])


main()
