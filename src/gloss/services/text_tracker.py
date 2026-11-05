"""TextTracker: turns jittery per-frame OCR into stable, de-duplicated, ordered lines.

OCR reads the same line slightly differently from frame to frame, and a scrolling chat moves
every line. The tracker follows each line across frames and emits it once, when it is stable.

How it works
------------
*Matching.* Each new read is matched to a known line by fuzzy text similarity plus bounding-box
overlap (IoU). A line that moved (scrolled) is matched by near-identical text in the same
column, so a scroll does not create "new" lines. The pipeline can also call ``scroll`` when the
change detector knows the shift, which keeps the IoU rule working.

*Stable reads.* A line is emitted after ``stable_reads`` consistent reads. Pixels the change
detector says did not change count as a read without another OCR: ``update`` credits every
pending line that lies outside ``scanned`` (the regions OCR just read), and ``unchanged`` credits
all of them. Reads that disagree vote for the variant they agree with, so one bad frame is
outvoted. A read that is a clipped part of a longer one (a popup covering the line's end) votes
for the longer text.

*Dedupe.* A line that becomes stable with the same normalised text as one seen within
``dedupe_window_s`` is not emitted again. It is still returned in ``visible`` with the
original ``line_id``, so a renderer can show the translation it already has. A second identical
message while the first is still on screen is a genuine repeat, so it is emitted. Texts shorter
than ``dedupe_min_chars`` are never deduped, because short repeats ("666") are usually real.

*Order and groups.* Output is in reading order. Lines that form one message (see
``ocr_layout``) share a ``group_id``.

The tracker keeps no pixels and no clock: callers pass ``now`` (monotonic seconds). It is not
thread safe; use it from the OCR/tracking worker only.
"""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from rapidfuzz import fuzz, process

from gloss.core.contracts import Rect, TextBlock
from gloss.services.ocr_lang import classify_line
from gloss.services.ocr_layout import group_rects
from gloss.services.ocr_regions import intersection_area, iou, reading_order

__all__ = ["TextTracker", "TrackerConfig", "TrackerUpdate", "normalize_text", "text_key"]

_CJK = "⺀-鿿぀-ヿ豈-﫿＀-￯"
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
_MAX_SEEN = 2000  # default for TrackerConfig.dedupe_max_lines
_CJK_GAP = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")


def normalize_text(text: str) -> str:
    """NFKC (which also turns full-width letters and punctuation into half-width), no control
    or zero-width characters, whitespace collapsed, and no spaces between CJK characters."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf") or c in "\t\n ")
    text = " ".join(text.split())
    return _CJK_GAP.sub("", text)


def text_key(normalized: str) -> str:
    """The form used to compare and de-duplicate: no case, spaces or punctuation."""
    return "".join(c for c in normalized.casefold() if unicodedata.category(c)[0] not in "ZPC")


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """All tracker settings. ``stable_reads`` and ``dedupe_window_s`` come from ``[ocr]`` in the
    config, and ``skip_langs`` should hold the translation target language."""

    stable_reads: int = 2
    dedupe_window_s: float = 30.0
    dedupe_min_chars: int = 4
    skip_langs: tuple[str, ...] = ()
    min_iou: float = 0.3  # box overlap that, with similar text, means "the same line"
    text_match: float = 0.5  # least text similarity for a same-place match
    scroll_match: float = 0.85  # text similarity that matches a line that moved
    consistent: float = 0.9  # reads this similar agree with each other
    revise_below: float = 0.8  # an emitted line is replaced by text this different
    dup_match: float = 0.85  # dedupe similarity (fuzzy, so jitter does not defeat it)
    pending_max_misses: int = 2  # scans in a row that may miss a not-yet-stable line
    emitted_max_misses: int = 3  # ... and an emitted one, before it leaves ``visible``
    piece_inside: float = 0.8  # share of a read's box inside a known line to be a piece of it
    piece_min_chars: int = 2  # shortest read treated as a piece
    group_lines: bool = True
    dedupe_max_lines: int = _MAX_SEEN  # most texts kept for dedupe, whatever the window

    def __post_init__(self) -> None:
        if self.stable_reads < 1:
            raise ValueError("stable_reads must be at least 1")
        if self.dedupe_window_s < 0:
            raise ValueError("dedupe_window_s must not be negative")
        if self.dedupe_max_lines < 1:
            raise ValueError("dedupe_max_lines must be at least 1")
        if self.pending_max_misses < 1 or self.emitted_max_misses < 1:
            raise ValueError("max misses must be at least 1")
        for name in (
            "min_iou",
            "text_match",
            "scroll_match",
            "consistent",
            "dup_match",
            "piece_inside",
        ):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class TrackerUpdate:
    """What one call changed.

    ``new`` holds lines that became stable now and were not seen before. Send those with
    ``needs_translation`` to the translator. ``visible`` is every stable line on screen,
    including re-detections of known lines (same ``line_id``), so a renderer can place them.
    ``dropped`` lists ``line_id`` values that left the screen. All three are in reading order.
    ``read`` holds the ``line_id`` of stable lines that a read in this very update matched
    (including known lines that just came back); lines only carried along are not in it.
    """

    new: tuple[TextBlock, ...] = ()
    visible: tuple[TextBlock, ...] = ()
    dropped: tuple[str, ...] = ()
    read: frozenset[str] = frozenset()


@dataclass(slots=True)
class _Variant:
    text: str
    key: str
    count: int
    conf: float | None
    lang: str | None
    low_quality: bool
    needs_translation: bool


@dataclass(slots=True)
class _Track:
    bbox: Rect
    first_seen: float
    last_seen: float
    variants: dict[str, _Variant] = field(default_factory=dict)  # by key
    reads: int = 0
    misses: int = 0
    stable: bool = False
    line_id: str | None = None
    group: str | None = None
    emitted_key: str = ""
    emitted: TextBlock | None = None

    def lead(self) -> _Variant:
        return max(
            self.variants.values(),
            key=lambda v: (v.count, len(v.key), v.conf if v.conf is not None else 1.0),
        )


def _digits(key: str) -> str:
    return "".join(c for c in key if c.isdigit())


def _more_confident(new: float | None, old: float | None) -> bool:
    return (new if new is not None else 1.0) > (old if old is not None else 1.0)


class TextTracker:
    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.config = config or TrackerConfig()
        self._tracks: list[_Track] = []
        # key -> (line_id, last time it was on screen or emitted), oldest first
        self._seen: OrderedDict[str, tuple[str, float]] = OrderedDict()
        # lines that left the screen: (key, line_id, last box, time), oldest first. The box is
        # shifted by scroll() like a live line's, so a line that flickers away and back at the
        # same spot (a fading badge) gets its old id even when it is too short to be de-duped.
        self._gone: list[tuple[str, str, Rect, float]] = []
        self._next_line = 1
        self._next_group = 1
        self._left: set[str] = set()  # ids removed by scroll(), reported on the next result

    # ---- public API ----------------------------------------------------------------------

    def update(
        self,
        blocks: Iterable[TextBlock],
        *,
        now: float,
        scanned: Sequence[Rect] | None = None,
        stable_reads: int | None = None,
    ) -> TrackerUpdate:
        """Feed one OCR result.

        ``scanned`` are the frame regions OCR read (None means the whole frame). Known lines
        outside them are assumed unchanged: they are neither marked missing nor re-read, and a
        pending one earns a read. ``stable_reads`` replaces the configured value for the lines
        read in this call only (None: use the config).
        """
        if stable_reads is not None and stable_reads < 1:
            raise ValueError("stable_reads must be at least 1")
        before = self._live_ids()
        reads = [r for r in (self._read(b) for b in blocks) if r is not None]
        reads = self._collapse_reads(reads)
        reads, supporting, pending_pieces = self._split_pieces(reads)
        matched, absorbed = self._match(reads)
        touched: list[_Track] = list(supporting)
        for track in supporting:
            track.misses = 0  # seen, as pieces
        for index, read in enumerate(reads):
            track = matched.get(index)
            if track is None:
                track = _Track(read[0].bbox, now, now)
                self._tracks.append(track)
            elif index in absorbed:
                track.variants.clear()  # a pending piece, now read whole: start from the whole
            self._apply(track, read, now)
            touched.append(track)
        for track, read in pending_pieces:
            if id(track) not in set(map(id, touched)):  # a clipped read votes for the longer text
                box = track.bbox
                self._apply(track, read, now)
                track.bbox = box
                touched.append(track)
        self._age(set(map(id, touched)), scanned, now)
        new = self._stabilise(now, set(map(id, touched)), stable_reads)
        read = frozenset(t.line_id for t in touched if t.stable and t.line_id)
        return self._result(new, before, now, read)

    def unchanged(self, *, now: float) -> TrackerUpdate:
        """The whole frame is unchanged, so every pending line earns a read without OCR."""
        return self.update((), now=now, scanned=())

    def scroll(self, dy: int, dx: int = 0, *, frame: Rect | None = None) -> None:
        """Move every known line by (dx, dy), when the change detector found a scroll shift.

        With ``frame`` (frame coordinates, ``Rect(0, 0, w, h)``), lines that no longer touch it
        are removed, because nothing will ever scan them again. Their ``line_id`` values are
        reported in ``dropped`` by the next ``update`` or ``unchanged``. Dedupe history is kept,
        so a line that scrolls back is not translated again.
        """
        for track in self._tracks:
            track.bbox = track.bbox.translated(dx, dy)
        self._gone = [(k, i, b.translated(dx, dy), t) for k, i, b, t in self._gone]
        if frame is None:
            return
        keep = []
        for track in self._tracks:
            if intersection_area(track.bbox, frame) > 0:
                keep.append(track)
            elif track.stable and track.line_id:
                self._left.add(track.line_id)
        self._tracks = keep

    def reset(self) -> None:
        """Forget everything, for a new capture target. Dedupe history is cleared too."""
        self._tracks.clear()
        self._seen.clear()
        self._gone.clear()
        self._left.clear()

    @property
    def visible(self) -> tuple[TextBlock, ...]:
        return tuple(reading_order([self._block(t) for t in self._tracks if t.stable]))

    # ---- reading one OCR line ------------------------------------------------------------

    def _read(self, block: TextBlock) -> tuple[TextBlock, str, str] | None:
        text = normalize_text(block.text)
        key = text_key(text)
        if not key:
            return None
        return block, text, key

    # ---- pieces of a known line ----------------------------------------------------------

    def _split_pieces(
        self, reads: list[tuple[TextBlock, str, str]]
    ) -> tuple[list[tuple[TextBlock, str, str]], list[_Track], list[tuple[_Track, tuple]]]:
        """Take out reads that are only a piece of a stable line (OCR split it in two, for
        example around an animated sticker). They keep that line alive and nothing more."""
        cfg = self.config
        stable = [t for t in self._tracks if self._whole_key(t)]
        if not stable:
            return reads, [], []
        rest: list[tuple[TextBlock, str, str]] = []
        supporting: dict[int, _Track] = {}
        pending: list[tuple[_Track, tuple]] = []
        for read in reads:
            owner = self._piece_owner(read, stable) if len(read[2]) >= cfg.piece_min_chars else None
            if owner is None:
                rest.append(read)
            elif owner.stable:
                supporting[id(owner)] = owner
            else:
                pending.append((owner, read))
        return rest, list(supporting.values()), pending

    @staticmethod
    def _whole_key(track: _Track) -> str:
        if track.stable:
            return track.emitted_key
        return track.lead().key if track.variants else ""

    def _is_piece(self, small: Rect, small_key: str, big: Rect, big_key: str) -> bool:
        """``small`` is a clipped part of ``big``: inside its box and contained in its text."""
        cfg = self.config
        if len(small_key) < cfg.piece_min_chars or len(small_key) > len(big_key):
            return False
        if intersection_area(small, big) < cfg.piece_inside * max(small.area, 1):
            return False
        return small_key in big_key or fuzz.partial_ratio(small_key, big_key) >= cfg.dup_match * 100

    def _collapse_reads(
        self, reads: list[tuple[TextBlock, str, str]]
    ) -> list[tuple[TextBlock, str, str]]:
        """Within one OCR result, drop reads that are only a piece of another read."""
        if len(reads) < 2:
            return reads
        order = sorted(reads, key=lambda r: (-len(r[2]), -r[0].bbox.area))
        kept: list[tuple[TextBlock, str, str]] = []
        for read in order:
            if any(self._is_piece(read[0].bbox, read[2], k[0].bbox, k[2]) for k in kept):
                continue
            kept.append(read)
        keep_ids = {id(r) for r in kept}
        return [r for r in reads if id(r) in keep_ids]

    def _piece_owner(self, read: tuple[TextBlock, str, str], stable: list[_Track]) -> _Track | None:
        box, key = read[0].bbox, read[2]
        best: _Track | None = None
        for track in stable:
            whole = self._whole_key(track)
            if key == whole or not self._is_piece(box, key, track.bbox, whole):
                continue  # the whole line, or more than it: normal matching
            if best is None or track.bbox.area < best.bbox.area:
                best = track  # the tightest line that holds it
        return best

    # ---- matching ------------------------------------------------------------------------

    def _similarity(self, key: str, track: _Track) -> float:
        return max(fuzz.ratio(key, v.key) for v in track.variants.values()) / 100

    def _match(self, reads: list[tuple[TextBlock, str, str]]) -> tuple[dict[int, _Track], set[int]]:
        cfg = self.config
        absorbing: set[tuple[int, int]] = set()
        pairs: list[tuple[float, int, _Track]] = []  # by position (or absorbing a piece)
        moved: list[tuple[float, float, int, _Track]] = []  # text only: (|dy|, -sim, read, track)
        positional: set[int] = set()  # reads that have a position candidate
        for i, (block, _text, key) in enumerate(reads):
            box = block.bbox
            for track in self._tracks:
                if (
                    not track.stable
                    and track.variants
                    and self._whole_key(track) != key
                    and self._is_piece(track.bbox, self._whole_key(track), box, key)
                ):  # a pending piece, now seen whole
                    pairs.append((200.0, i, track))
                    absorbing.add((i, id(track)))
                    positional.add(i)
                    continue
                sim = self._similarity(key, track)
                overlap = iou(box, track.bbox)
                if overlap >= cfg.min_iou and sim >= cfg.text_match:
                    pairs.append((100 * sim + 10 * overlap, i, track))
                    positional.add(i)
                    continue
                if (
                    sim >= cfg.scroll_match
                    and len(key) >= 2
                    and self._same_column(box, track.bbox)
                    and self._same_digits(key, track)
                ):
                    moved.append((abs(box.y - track.bbox.y), -sim, i, track))
        pairs.sort(key=lambda p: -p[0])
        taken: set[int] = set()
        matched: dict[int, _Track] = {}
        for _score, i, track in pairs:
            if i in matched or id(track) in taken:
                continue
            matched[i] = track
            taken.add(id(track))
        # Text-only matches: only for reads with no position candidate, nearest first.
        moved.sort(key=lambda m: (m[0], m[1]))
        text_matched: dict[int, _Track] = {}
        for _dy, _sim, i, track in moved:
            if i in positional or i in matched or id(track) in taken:
                continue
            text_matched[i] = track
            taken.add(id(track))
        # A moved match whose shift disagrees with the others is a look-alike line.
        if len(text_matched) >= 3:
            shifts = {i: reads[i][0].bbox.y - t.bbox.y for i, t in text_matched.items()}
            for i in list(text_matched):
                others = sorted(v for j, v in shifts.items() if j != i)
                median = others[len(others) // 2]
                if abs(shifts[i] - median) > reads[i][0].bbox.h:
                    del text_matched[i]
        matched.update(text_matched)
        return matched, {i for i, t in matched.items() if (i, id(t)) in absorbing}

    def _same_digits(self, key: str, track: _Track) -> bool:
        digits = _digits(key)
        return any(_digits(v.key) == digits for v in track.variants.values())

    @staticmethod
    def _same_column(a: Rect, b: Rect) -> bool:
        overlap = min(a.right, b.right) - max(a.x, b.x)
        if overlap < 0.5 * min(a.w, b.w):
            return False
        return 0.6 <= (a.h / b.h if b.h else 0) <= 1.7

    # ---- updating a line -----------------------------------------------------------------

    def _apply(self, track: _Track, read: tuple[TextBlock, str, str], now: float) -> None:
        block, text, key = read
        cfg = self.config
        lang, needs = classify_line(text, cfg.skip_langs)
        needs = needs and block.needs_translation
        lang = block.lang or lang
        track.bbox = block.bbox
        track.last_seen = now
        track.misses = 0
        track.reads += 1
        self._vote(track, _Variant(text, key, 1, block.confidence, lang, block.low_quality, needs))

    def _vote(self, track: _Track, new: _Variant) -> None:
        cfg = self.config
        variants = track.variants
        for key, old in list(variants.items()):
            if fuzz.ratio(new.key, key) / 100 >= cfg.consistent:
                old.count += 1
                if _more_confident(new.conf, old.conf):
                    old.text, old.conf, old.lang = new.text, new.conf, new.lang
                    old.needs_translation = new.needs_translation
                old.low_quality = old.low_quality or new.low_quality
                return
            short, long_ = sorted((new.key, key), key=len)
            if len(short) >= 4 and short in long_:  # one read is a clipped piece of the other
                if len(new.key) <= len(key):
                    old.count += 1
                elif track.stable and key == track.emitted_key:
                    variants[new.key] = new  # a longer text must earn its own stable reads
                else:
                    new.count = old.count + 1  # the longer read wins and inherits the votes
                    del variants[key]
                    variants[new.key] = new
                return
        variants[new.key] = new

    def _vote_unchanged(self, track: _Track) -> None:
        track.reads += 1
        track.lead().count += 1

    # ---- aging ---------------------------------------------------------------------------

    def _age(self, touched: set[int], scanned: Sequence[Rect] | None, now: float) -> None:
        cfg = self.config
        keep: list[_Track] = []
        fresh = [t.bbox for t in self._tracks if id(t) in touched]
        for track in self._tracks:
            if id(track) in touched:
                keep.append(track)
                continue
            if self._covered(track.bbox, scanned):
                track.misses += 1
                limit = cfg.emitted_max_misses if track.stable else cfg.pending_max_misses
                if track.misses >= limit or any(iou(track.bbox, b) >= 0.5 for b in fresh):
                    self._forget(track, now)  # gone, or other text now sits where it was
                    continue
            elif not track.stable and track.variants:
                self._vote_unchanged(track)  # pixels did not change: that is another read
            keep.append(track)
        self._tracks = keep

    def _forget(self, track: _Track, now: float) -> None:
        if track.stable and track.line_id and track.emitted_key:
            self._gone.append((track.emitted_key, track.line_id, track.bbox, now))
            del self._gone[: max(0, len(self._gone) - self.config.dedupe_max_lines)]

    def _returning(self, key: str, track: _Track, now: float) -> str | None:
        """The id of a line with exactly this text that left from (about) this spot."""
        window = self.config.dedupe_window_s
        self._gone = [g for g in self._gone if now - g[3] <= window]
        live = {t.line_id for t in self._tracks if t is not track and t.stable}
        for n, (gkey, line_id, box, _t) in enumerate(self._gone):
            if gkey == key and line_id not in live and iou(track.bbox, box) >= 0.5:
                del self._gone[n]
                return line_id
        return None

    @staticmethod
    def _covered(box: Rect, scanned: Sequence[Rect] | None) -> bool:
        if scanned is None:
            return True
        return any(intersection_area(box, r) >= 0.5 * max(box.area, 1) for r in scanned)

    # ---- becoming stable -----------------------------------------------------------------

    def _ready(self, track: _Track, need: int) -> bool:
        if track.lead().count >= need:
            return True
        # A line that flickers between readings must not wait forever.
        return track.reads >= 2 * need + 2

    def _revision(self, track: _Track) -> _Variant | None:
        """For an emitted line: a different text that has now been read stably, if any."""
        cfg = self.config
        for key, variant in track.variants.items():
            if key == track.emitted_key or variant.count < cfg.stable_reads:
                continue
            if fuzz.ratio(key, track.emitted_key) / 100 < cfg.revise_below:
                return variant
        return None

    def _stabilise(self, now: float, touched: set[int], override: int | None) -> list[_Track]:
        """Promote tracks that became stable. Return those that are genuinely new lines."""
        new: list[_Track] = []
        for track in self._tracks:
            if track.stable:
                revised = self._revision(track)
                if revised is None:
                    continue
                track.variants = {revised.key: revised}  # the text really changed: start over
                track.stable = False
                track.line_id = None
                track.group = None
            need = (
                override
                if override is not None and id(track) in touched
                else self.config.stable_reads
            )
            if not track.variants or not self._ready(track, need):
                continue
            lead = track.lead()
            track.stable = True
            track.emitted_key = lead.key
            track.emitted = self._build(track, lead)
            existing = self._lookup(lead.key, track, now)
            if existing is not None:
                track.line_id = existing  # a re-detection: same line, nothing new to translate
            else:
                track.line_id = f"l{self._next_line}"
                self._next_line += 1
                self._remember(lead.key, track.line_id, now)
                new.append(track)
        return new

    def _lookup(self, key: str, track: _Track, now: float) -> str | None:
        """The ``line_id`` of a recently seen line with this text, unless it is a real repeat."""
        cfg = self.config
        if cfg.dedupe_window_s <= 0:
            return None
        back = self._returning(key, track, now)
        if back is not None:
            return back
        if len(key) < cfg.dedupe_min_chars:
            return None
        hit = self._seen.get(key)
        if hit is None and self._seen:
            digits = _digits(key)
            for found, _score, _ in process.extract(
                key, self._seen.keys(), scorer=fuzz.ratio, score_cutoff=cfg.dup_match * 100, limit=5
            ):
                if _digits(found) == digits:  # "room 5" and "room 6" are different messages
                    hit = self._seen[found]
                    break
        if hit is None or now - hit[1] > cfg.dedupe_window_s:
            return None
        line_id = hit[0]
        for other in self._tracks:  # that line is still on screen, so this is a second copy
            if (
                other is not track
                and other.stable
                and other.line_id == line_id
                and other.misses == 0
            ):
                return None
        return line_id

    def _remember(self, key: str, line_id: str, now: float) -> None:
        self._seen[key] = (line_id, now)
        self._seen.move_to_end(key)
        while len(self._seen) > self.config.dedupe_max_lines:
            self._seen.popitem(last=False)

    # ---- output --------------------------------------------------------------------------

    def _build(self, track: _Track, lead: _Variant) -> TextBlock:
        confs = [v.conf for v in track.variants.values() if v.conf is not None]
        return TextBlock(
            text=lead.text,
            bbox=track.bbox,
            confidence=max(confs) if confs else None,
            lang=lead.lang,
            line_id=track.line_id,
            low_quality=lead.low_quality,
            needs_translation=lead.needs_translation,
            group_id=track.group,
        )

    def _block(self, track: _Track) -> TextBlock:
        """The track as a block. Once stable, the text is frozen at what was emitted."""
        if track.stable and track.emitted is not None:
            return replace(
                track.emitted, bbox=track.bbox, line_id=track.line_id, group_id=track.group
            )
        return self._build(track, track.lead())

    def _live_ids(self) -> set[str]:
        return {t.line_id for t in self._tracks if t.stable and t.line_id}

    def _result(
        self, new: list[_Track], before: set[str], now: float, read: frozenset[str]
    ) -> TrackerUpdate:
        stable = [t for t in self._tracks if t.stable]
        if self.config.group_lines and stable:
            self._assign_groups(stable)
        for track in stable:  # keep the dedupe window sliding while a line is on screen
            if track.misses == 0 and track.emitted_key in self._seen:
                line_id, _ = self._seen[track.emitted_key]
                self._seen[track.emitted_key] = (line_id, now)
                self._seen.move_to_end(track.emitted_key)
        self._expire(now)
        visible = reading_order([self._block(t) for t in stable])
        ordered_new = reading_order([self._block(t) for t in new])
        after = {b.line_id for b in visible}
        dropped = tuple(sorted(i for i in (before - after) | (self._left - after) if i))
        self._left.clear()
        return TrackerUpdate(tuple(ordered_new), tuple(visible), dropped, read)

    def _expire(self, now: float) -> None:
        window = self.config.dedupe_window_s
        while self._seen:
            key, (_, seen_at) = next(iter(self._seen.items()))
            if now - seen_at <= window:
                break
            del self._seen[key]

    def _assign_groups(self, stable: list[_Track]) -> None:
        pairs = [(self._block(t), t) for t in stable]
        owner = {id(block): track for block, track in pairs}
        ordered = reading_order([block for block, _ in pairs])
        members: dict[int, list[_Track]] = {}
        numbers = group_rects(
            [b.bbox for b in ordered], starts=[bool(_TIME.search(b.text)) for b in ordered]
        )
        for block, number in zip(ordered, numbers, strict=True):
            members.setdefault(number, []).append(owner[id(block)])
        for tracks in members.values():
            if len(tracks) < 2:
                for t in tracks:
                    t.group = None
                continue
            existing = [t.group for t in tracks if t.group]
            name = min(existing, key=lambda g: int(g[1:])) if existing else f"g{self._next_group}"
            if not existing:
                self._next_group += 1
            for t in tracks:
                t.group = name
