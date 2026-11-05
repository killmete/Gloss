"""Only one Gloss runs per user: a second copy sees the first one's lock."""

from __future__ import annotations

import subprocess
import sys

from gloss.app import claim_single_instance

HOLD = """
import sys, time
from gloss.app import claim_single_instance
from pathlib import Path
lock = claim_single_instance(Path(sys.argv[1]))
print("held" if lock is not None else "busy", flush=True)
sys.stdin.readline()
"""


def test_a_second_copy_is_refused_while_the_first_runs(tmp_path):
    first = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout.readline().strip() == "held"
        assert claim_single_instance(tmp_path) is None
    finally:
        first.stdin.write("\n")
        first.stdin.flush()
        first.wait(timeout=10)
    lock = claim_single_instance(tmp_path)  # free again once the first copy quits
    assert lock is not None
    lock.unlock()


def test_a_lock_left_by_a_crashed_copy_is_taken_over(tmp_path):
    crashed = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert crashed.stdout.readline().strip() == "held"
    crashed.kill()  # no clean unlock
    crashed.wait(timeout=10)
    lock = claim_single_instance(tmp_path)
    assert lock is not None
    lock.unlock()
