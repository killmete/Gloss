"""The benchmark comparison itself must be right, or a regression slips through."""

from __future__ import annotations

from testkit.bench import Metrics, machine_summary, same_machine


def baseline(**values: float) -> dict:
    return {"metrics": {k: {"value": v, "unit": "ms"} for k, v in values.items()}}


def run(**values: float) -> Metrics:
    m = Metrics()
    for k, v in values.items():
        m.record(k, v, "ms")
    return m


def test_more_than_20_percent_worse_is_reported():
    problems = run(ocr_panel_p95_ms=300.0).regressions(baseline(ocr_panel_p95_ms=240.0))
    assert len(problems) == 1 and "ocr_panel_p95_ms" in problems[0] and "25%" in problems[0]


def test_20_percent_or_less_is_fine_and_faster_is_fine():
    assert not run(a=288.0).regressions(baseline(a=240.0))
    assert not run(a=100.0).regressions(baseline(a=240.0))


def test_tiny_values_get_a_small_absolute_allowance():
    assert not run(detect_chat_static_ms=2.5).regressions(baseline(detect_chat_static_ms=1.5))
    assert run(detect_chat_static_ms=4.0).regressions(baseline(detect_chat_static_ms=1.5))


def test_info_only_and_new_metrics_are_not_gated():
    assert not run(scroll_boxes_p50_ms=900.0).regressions(baseline(scroll_boxes_p50_ms=50.0))
    assert not run(brand_new_ms=9e9).regressions(baseline(other_ms=1.0))


def test_machine_summary_has_no_personal_data():
    summary = machine_summary()
    assert set(summary) == {
        "cpu_model",
        "physical_cores",
        "logical_cores",
        "ram_gb",
        "os",
        "python",
    }
    assert same_machine(summary, dict(summary))
    assert not same_machine(summary, {**summary, "cpu_model": "Another CPU"})
