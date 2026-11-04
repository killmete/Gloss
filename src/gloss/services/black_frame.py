"""Notices a capture backend that returns black, and asks for one trial of the next source.

GDI capture (``mss``) can return an all-black image for some hardware-accelerated windows,
without any error. Adapters hand black frames back like any other (see ``CaptureSource.grab``),
so the check lives here, outside the adapters, and the pipeline calls it after every grab:

    needs_trial = check.observe(frame)         # True once per black streak
    if needs_trial:
        trial = next_source.grab()             # one frame from the next source in the chain
        if check.record_trial(trial): ...switch to next_source...
    status = check.health()                    # readable, for the status dot

A frame is black when its mean brightness (0-255) is at or below ``black_frame_max_mean``;
``black_frame_reads`` black frames in a row start the trial. A genuinely dark screen therefore
costs one trial grab, not a permanent switch: if the next source is black too, nothing is
switched and ``health()`` stays ``degraded`` with an explanation until a non-black frame arrives.

``health()`` may be called from any thread; every other method belongs to the capture worker.
"""

from __future__ import annotations

from gloss.core.contracts import Frame, Health

_SAMPLE_STEP = 4  # look at one pixel in 16: enough for a mean, and cheap on 1080p


class BlackFrameCheck:
    def __init__(self, reads: int = 8, max_mean: float = 4.0) -> None:
        if reads < 1:
            raise ValueError("reads must be at least 1")
        if not 0 <= max_mean <= 255:
            raise ValueError("max_mean must be between 0 and 255")
        self._reads = reads
        self._max_mean = float(max_mean)
        self._run = 0
        self._trial_started = False
        self._health = Health("ok", "")

    @property
    def consecutive_black(self) -> int:
        return self._run

    def is_black(self, frame: Frame) -> bool:
        sample = frame.image[::_SAMPLE_STEP, ::_SAMPLE_STEP]
        if sample.size == 0:
            return True
        return float(sample.mean()) <= self._max_mean

    def observe(self, frame: Frame) -> bool:
        """Count one frame. Returns True once, when the black streak reaches the limit."""
        if not self.is_black(frame):
            self.reset()
            return False
        self._run += 1
        if self._run < self._reads or self._trial_started:
            return False
        self._trial_started = True
        self._health = Health(
            "degraded",
            f"The capture has been black for {self._run} reads in a row. "
            "Trying another capture method.",
        )
        return True

    def record_trial(self, frame: Frame | None) -> bool:
        """Report the trial frame from the next source. True means: switch to that source."""
        if frame is None:
            self._health = Health(
                "degraded", "The capture is black and the other capture method gave no picture."
            )
            return False
        if not self.is_black(frame):
            self._health = Health("degraded", "The first capture method was black; switched.")
            return True
        self._health = Health(
            "degraded",
            "Both capture methods show black. The screen may be dark, or this window may "
            "block screen capture.",
        )
        return False

    def trial_unavailable(self) -> None:
        """The next source could not be opened at all."""
        self._health = Health(
            "degraded", "The capture is black and no other capture method is available."
        )

    def reset(self) -> None:
        """Forget the streak: after a non-black frame, or after switching sources."""
        self._run = 0
        self._trial_started = False
        self._health = Health("ok", "")

    def health(self) -> Health:
        return self._health
