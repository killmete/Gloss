"""Shared pieces for the benchmarks in tests/bench/.

- ``chat_canvas``: a synthetic chat picture (flat background, rounded bubbles, text). It is
  drawn from invented sentences, never from a real screenshot or a user's screen.
- ``Metrics``: collects named values, writes ``tests/bench/results/<date>.json`` and compares
  them with ``tests/bench/baseline.json``.
- ``machine_summary``: CPU model and core counts only. No user name, host name or paths.

Regression rule (SPEC section 5): a metric more than 20% worse than the baseline fails. A tiny
absolute allowance (``NOISE_FLOOR``) stops a 0.4 ms value from failing on a 0.1 ms wobble.
The comparison only happens on the machine that recorded the baseline; elsewhere it is skipped.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BENCH_DIR = Path(__file__).resolve().parents[1] / "bench"
BASELINE = BENCH_DIR / "baseline.json"
RESULTS_DIR = BENCH_DIR / "results"
TOLERANCE = 0.20  # SPEC section 5: more than 20% worse blocks
NOISE_FLOOR = {"ms": 2.0, "pct": 2.0, "mb": 10.0}  # absolute slack by unit, see module docstring
UPDATE_ENV = "GLOSS_BENCH_UPDATE"  # 1 = write baseline.json from this run; "worst" = keep the
# worse of the old baseline and this run (run it a few times: the baseline is the worst of them)
# Reported but not gated: these medians sit on the capture frame interval (83 ms at 12 fps) and
# move by 100+ ms between identical runs. Their p95 values are gated.
INFO_ONLY = frozenset({"scroll_boxes_p50_ms", "scroll_line_p50_ms"})

# ---- the synthetic chat ---------------------------------------------------------------------

_CJK_FONT = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
# Common characters and words. Every line is a different mix of them, so that no two lines
# look alike to the text tracker (which treats near-identical text as a duplicate).
HANZI = (
    "我你他她们的是不了在有这个人中大来上国为以会时要就出也你可下对生能而子那得于着"
    "然后多没天好看起发当想样现只家种用心前所进行都同方如意过话事做还什么吃喝玩游戏"
    "朋友今明晚早饭路车手机电影音乐书学校工作公司老师同学快乐开心谢谢请问东西钱买卖"
)
WORDS = (
    "apple river window garden coffee mirror bridge pencil rocket thunder candle pillow "
    "ladder market island forest silver yellow orange purple travel summer winter spring "
    "music dinner doctor friend letter moment number office parcel record school ticket "
    "visit weather answer border castle dragon engine flower guitar helmet jacket kitchen"
).split()


def has_cjk_font() -> bool:
    return _CJK_FONT.is_file()


def chat_text(index: int) -> str:
    """Sentence number ``index``: always the same for the same index, unlike any other index."""
    rng = np.random.default_rng(1000 + index)
    if has_cjk_font():
        return "".join(HANZI[int(k)] for k in rng.integers(0, len(HANZI), 11))
    return " ".join(WORDS[int(k)] for k in rng.integers(0, len(WORDS), 4))


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if has_cjk_font():
        return ImageFont.truetype(str(_CJK_FONT), size)
    return ImageFont.load_default(size=size)


@dataclass(frozen=True)
class Canvas:
    image: np.ndarray  # (h, w, 3) uint8
    texts: tuple[str, ...]
    line_h: int
    background: tuple[int, int, int]

    def view(self, top: int, height: int) -> np.ndarray:
        """A copy of ``height`` rows starting at row ``top`` (it owns its memory)."""
        return np.ascontiguousarray(self.image[top : top + height])

    def line_top(self, index: int) -> int:
        return index * self.line_h


def chat_canvas(
    lines: int, width: int = 720, line_h: int = 48, font_px: int = 24, seed: int = 0
) -> Canvas:
    """A tall chat: flat background, alternating left/right bubbles with a small avatar."""
    background = (236, 236, 240)
    image = Image.new("RGB", (width, lines * line_h), background)
    draw = ImageDraw.Draw(image)
    font = _font(font_px)
    rng = np.random.default_rng(seed)
    texts = []
    for i in range(lines):
        text = chat_text(i)
        texts.append(text)
        top = i * line_h
        left = bool(i % 2 == 0)
        box = draw.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        pad = 10
        bw = min(tw + 2 * pad, width - 120)
        x0 = 60 if left else width - 60 - bw
        fill = (255, 255, 255) if left else (149, 236, 105)
        draw.rounded_rectangle((x0, top + 4, x0 + bw, top + line_h - 4), radius=8, fill=fill)
        draw.text(
            (x0 + pad - box[0], top + (line_h - th) // 2 - box[1]),
            text,
            font=font,
            fill=(20, 20, 20),
        )
        ax = 8 if left else width - 48
        shade = tuple(int(v) for v in rng.integers(90, 200, 3))
        draw.ellipse((ax, top + 6, ax + 36, top + 6 + 36), fill=shade)
    return Canvas(np.array(image, dtype=np.uint8), tuple(texts), line_h, background)


def noise_image(height: int, width: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (height, width, 3), dtype=np.uint8)


# ---- machine and results --------------------------------------------------------------------


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            ) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return platform.processor() or "unknown"


def machine_summary() -> dict[str, Any]:
    import psutil

    return {
        "cpu_model": " ".join(_cpu_name().split()),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "ram_gb": round(psutil.virtual_memory().total / 2**30),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
    }


def same_machine(a: dict[str, Any], b: dict[str, Any]) -> bool:
    keys = ("cpu_model", "physical_cores", "logical_cores")
    return all(a.get(k) == b.get(k) for k in keys)


def load_baseline() -> dict[str, Any] | None:
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class Metrics:
    """name -> {"value": float, "unit": "ms"|"pct"|"mb"}. Lower is better for all of them."""

    values: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, name: str, value: float, unit: str) -> float:
        if unit not in NOISE_FLOOR:
            raise ValueError(f"unknown unit {unit!r}")
        self.values[name] = {"value": round(float(value), 2), "unit": unit}
        return float(value)

    def snapshot(self) -> dict[str, Any]:
        return {
            "date": date.today().isoformat(),
            "machine": machine_summary(),
            "metrics": dict(sorted(self.values.items())),
        }

    def write_results(self) -> Path | None:
        if not self.values:
            return None
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = RESULTS_DIR / f"{date.today().isoformat()}.json"
        path.write_text(json.dumps(self.snapshot(), indent=2) + "\n", encoding="utf-8")
        return path

    def write_baseline(self, keep_worst: bool = False) -> None:
        """Merge this run into baseline.json (a partial run keeps the other metrics)."""
        old = load_baseline() or {"metrics": {}}
        snap = self.snapshot()
        merged = {**old.get("metrics", {}), **snap["metrics"]}
        if keep_worst:
            for name, then in old.get("metrics", {}).items():
                if name in snap["metrics"] and then["value"] > merged[name]["value"]:
                    merged[name] = then
        snap["metrics"] = dict(sorted(merged.items()))
        BASELINE.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")

    def regressions(self, baseline: dict[str, Any]) -> list[str]:
        """One sentence per metric that is more than 20% worse than the baseline."""
        problems = []
        for name, now in sorted(self.values.items()):
            then = baseline.get("metrics", {}).get(name)
            if then is None or name in INFO_ONLY:
                continue
            limit = max(then["value"] * (1 + TOLERANCE), then["value"] + NOISE_FLOOR[then["unit"]])
            if now["value"] > limit:
                problems.append(
                    f"{name}: {now['value']} {now['unit']} now, {then['value']} in the baseline "
                    f"(limit {limit:.2f}; +{(now['value'] / then['value'] - 1) * 100:.0f}%)"
                    if then["value"]
                    else f"{name}: {now['value']} {now['unit']} now, 0 in the baseline"
                )
        return problems
