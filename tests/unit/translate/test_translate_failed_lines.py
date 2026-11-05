"""Per-line failures inside successful replies: retry once, pass through trivia, warn, health."""

from __future__ import annotations

import logging

import pytest
from test_translate_anthropic import opened, request
from test_translate_service import Harness, cloud, local, texts

from gloss.adapters.translate.anthropic_translator import AnthropicTranslator
from gloss.core.contracts import Glossary
from gloss.services.translation_service import LINES_FAILED_NOTE, TranslationService


def test_a_missing_line_is_retried_alone_and_then_shown_translated(tmp_path):
    primary = cloud()
    primary.behave = lambda r, n: primary.respond(r, {"二": "missing"} if n == 1 else None)
    h = Harness(tmp_path, [primary, local()])
    results = h.translate("一", "二", "三")
    assert texts(results) == ["[cloud] 一", "[cloud] 二", "[cloud] 三"]
    assert [i.text for i in primary.requests[1].items] == ["二"]
    assert primary.requests[1].context == primary.requests[0].context


def test_a_line_still_missing_after_the_retry_falls_back_and_logs_counts_only(tmp_path, caplog):
    primary = cloud()
    primary.behave = lambda r, n: primary.respond(r, {"机密": "missing", "空": "invalid_output"})
    h = Harness(tmp_path, [primary, local()])
    with caplog.at_level(logging.WARNING):
        results = h.translate("一", "机密", "空", "四")
    assert [r.provider for r in results] == ["cloud", "null", "null", "cloud"]
    assert len(primary.requests) == 2
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "Cloud left 2 of 4 lines untranslated (missing 1, empty 1)" in " ".join(warnings)
    assert not any("机密" in w or "空" in w for w in warnings)


def test_a_truncated_single_line_is_asked_once_more_and_batches_are_split(tmp_path):
    primary = cloud()
    primary.behave = lambda r, n: primary.respond(
        r, {i.text: "truncated" for i in r.items} if len(r.items) > 1 or n == 1 else None
    )
    h = Harness(tmp_path, [primary])
    assert all(r.status == "ok" for r in h.translate("一"))
    assert [len(r.items) for r in primary.requests] == [1, 1]


def test_digit_and_punctuation_lines_pass_through_as_ok_without_a_call(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    results = h.translate("12:34", "...!?", "👍👍", "$ 5.00")
    assert [r.status for r in results] == ["ok"] * 4
    assert texts(results) == ["12:34", "...!?", "👍👍", "$ 5.00"]
    assert primary.requests == []
    assert h.service.status().health.state == "ok"


def test_refusal_of_a_single_line_is_not_retried(tmp_path):
    primary = cloud()
    primary.behave = lambda r, n: primary.respond(r, {i.text: "refused" for i in r.items})
    h = Harness(tmp_path, [primary, local()])
    (result,) = h.translate("敏感")
    assert result.provider == "null"
    assert len(primary.requests) == 1


def test_budget_counts_both_the_first_call_and_the_retry(tmp_path):
    primary = cloud()
    primary.behave = lambda r, n: primary.respond(r, {"二": "missing"} if n == 1 else None)
    h = Harness(tmp_path, [primary])
    h.translate("一", "二")
    two_calls = h.ledger.total_since(0)
    single = Harness(tmp_path / "single", [cloud()])
    single.translate("一")
    assert len(primary.requests) == 2
    assert two_calls == pytest.approx(2 * single.ledger.total_since(0))


def test_health_degrades_when_most_recent_lines_fail_and_recovers(tmp_path):
    primary = cloud()
    primary.behave = lambda r, n: primary.respond(r, {i.text: "missing" for i in r.items})
    h = Harness(tmp_path, [primary, local()])
    h.translate("一", "二", "三", "四", "五")
    health = h.service.status().health
    assert health.state == "degraded" and LINES_FAILED_NOTE in h.service.status().banner
    primary.behave = primary.ok
    for i in range(12):
        h.translate(f"好{i}", f"行{i}")
    assert h.service.status().health.state == "ok"


def test_prompt_demands_no_cjk_and_pinyin_names_and_keeps_ids():
    a = opened()
    a.translate(request("小林-钻石-阿月", glossary=Glossary("g1", ())))
    text = a.client.calls[0]["system"][0]["text"]
    assert "no Chinese or Japanese characters" in text
    assert "pinyin" in text and "romaji" in text and "the same way every time" in text
    assert "Rank, tier, role or title words" in text
    assert "Keep IDs, numbers, prices and emoji unchanged" in text


def test_cache_key_changes_with_the_prompt_version(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    run = type("R", (), {"glossary": Glossary("g1", ())})()
    provider = h.service._providers[0]
    plain = h.service._key(provider, run, "你好").glossary_version
    primary.PROMPT_VERSION = 2
    assert h.service._key(provider, run, "你好").glossary_version != plain
    assert AnthropicTranslator.PROMPT_VERSION >= 2
    assert TranslationService is not None
