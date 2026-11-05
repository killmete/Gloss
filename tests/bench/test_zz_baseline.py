"""Compare this run with tests/bench/baseline.json (SPEC section 5: more than 20% worse blocks).

Named zz so it runs after the benchmarks that fill the metrics. It skips, with the reason,
when there is no baseline or the baseline was recorded on a different machine (CPU model or
core count): timings from another PC prove nothing. To record a baseline on this PC:

    GLOSS_BENCH_UPDATE=1 uv run pytest -m bench       (first run)
    GLOSS_BENCH_UPDATE=worst uv run pytest -m bench   (twice more: keeps the worst of three)
"""

from __future__ import annotations

import pytest
from testkit.bench import (
    UPDATE_ENV,
    load_baseline,
    machine_summary,
    same_machine,
)

pytestmark = pytest.mark.bench


def test_no_metric_is_more_than_20_percent_worse_than_the_baseline(metrics) -> None:
    import os

    if os.environ.get(UPDATE_ENV) in ("1", "worst"):
        pytest.skip("recording a new baseline from this run")
    baseline = load_baseline()
    if baseline is None:
        pytest.skip("tests/bench/baseline.json is missing; record one with GLOSS_BENCH_UPDATE=1")
    here, there = machine_summary(), baseline.get("machine", {})
    if not same_machine(here, there):
        pytest.skip(
            f"the baseline was recorded on a different machine ({there.get('cpu_model')}, "
            f"{there.get('logical_cores')} threads); this is {here['cpu_model']}"
        )
    if not metrics.values:
        pytest.skip("no benchmark ran in this session, so there is nothing to compare")
    problems = metrics.regressions(baseline)
    assert not problems, "slower than the baseline by more than 20%:\n" + "\n".join(problems)
