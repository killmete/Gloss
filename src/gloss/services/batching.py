"""Collects lines into a batch: up to ``max_lines``, or until ``max_wait`` after the first line.

Used by the translate stage worker. The worker blocks for the first line, then keeps taking
lines for at most ``max_wait_s`` (``translate.batch_max_wait_ms``, 200 ms by default) so a
burst of chat lines goes out as one request, while a single line is delayed very little.
"""

from __future__ import annotations

import queue
import time
from collections.abc import Callable


def collect_batch[T](
    source: queue.Queue[T],
    max_lines: int,
    max_wait_s: float,
    *,
    first_timeout_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[T]:
    """Return the next batch, or an empty list if no line arrived within ``first_timeout_s``.

    With ``first_timeout_s=None`` this blocks until a line arrives. The wait for more lines
    starts when the first one is taken, not when this is called. Lines already in the queue
    are taken at once, without waiting.
    """
    try:
        batch = [source.get(timeout=first_timeout_s) if first_timeout_s else source.get()]
    except queue.Empty:
        return []
    deadline = clock() + max_wait_s
    while len(batch) < max_lines:
        try:
            batch.append(source.get_nowait())
            continue
        except queue.Empty:
            pass
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            batch.append(source.get(timeout=remaining))
        except queue.Empty:
            break
    return batch
