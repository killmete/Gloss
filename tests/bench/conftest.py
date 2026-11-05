"""Benchmark session: one Metrics collector, results written at the end.

``tests/bench/results/<date>.json`` is written after every run that measured something.
Set GLOSS_BENCH_UPDATE=1 to also record the run as the new ``baseline.json``, or
GLOSS_BENCH_UPDATE=worst to keep the worse of the old baseline and this run.
"""

from __future__ import annotations

import os

import pytest
from testkit.bench import UPDATE_ENV, Metrics

_METRICS = Metrics()


@pytest.fixture(scope="session")
def metrics() -> Metrics:
    return _METRICS


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _METRICS.values:
        return
    _METRICS.write_results()
    mode = os.environ.get(UPDATE_ENV)
    if mode in ("1", "worst") and exitstatus == 0:
        _METRICS.write_baseline(keep_worst=mode == "worst")
