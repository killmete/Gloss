"""The TranslationService pipeline, with scripted translators and real SQLite storage."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime

import pytest
from testkit.discovery import NO_USER_CONFIG
from translate_fakes import (
    CloudScripted,
    FakeClient,
    KeySecrets,
    LocalScripted,
    reply,
)

from gloss.adapters.translate.anthropic_translator import AnthropicTranslator
from gloss.adapters.translate.null_translator import NullTranslator
from gloss.core.config import load_config
from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    GlossaryEntry,
    PermanentError,
    TransientError,
    TranslationItem,
)
from gloss.services.budget import BudgetGuard
from gloss.services.cache import SqliteTranslationCache
from gloss.services.glossary import GlossaryStore
from gloss.services.ledger import SqliteSpendLedger
from gloss.services.translation_service import ServiceSettings, TranslationService

CONFIG = load_config(user_path=NO_USER_CONFIG).config
SETTINGS = ServiceSettings.from_config(CONFIG)
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC).timestamp()


def items(*texts: str, low: bool = False) -> list[TranslationItem]:
    return [TranslationItem(f"L{i}", t, low_quality=low) for i, t in enumerate(texts)]


class Harness:
    def __init__(self, tmp_path, translators, *, budget=True, cache=True, glossary=None, **kw):
        self.tmp_path = tmp_path
        self.sleeps: list[float] = []
        self.time = {"mono": 0.0, "wall": NOW}
        self.cache = SqliteTranslationCache(tmp_path) if cache else None
        self.ledger = SqliteSpendLedger(tmp_path)
        self.guard = (
            BudgetGuard(
                self.ledger,
                CONFIG.translate.price_for,
                monthly_usd=kw.pop("monthly", 1000.0),
                hourly_usd=kw.pop("hourly", 100.0),
                warn_at=0.8,
                clock=lambda: self.time["wall"],
            )
            if budget
            else None
        )
        self.service = TranslationService(
            translators,
            settings=kw.pop("settings", SETTINGS),
            cache=self.cache,
            budget=self.guard,
            glossary=glossary,
            clock=lambda: self.time["mono"],
            sleep=self.sleeps.append,
            rng=random.Random(3),
        )
        self.translators = translators

    def translate(self, *texts, **kw):
        return self.service.translate(items(*texts, **kw))


def cloud(name="cloud", behave=None, model="claude-haiku-5-5"):
    return CloudScripted(name, behave, model)


def local(name="null"):
    return LocalScripted(name)


def texts(results):
    return [r.text for r in results]


# ---- basics ------------------------------------------------------------------------------


def test_empty_input_returns_nothing_and_calls_nobody(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    assert h.service.translate([]) == ()
    assert primary.requests == []


def test_results_keep_order_ids_source_text_and_provider(tmp_path):
    h = Harness(tmp_path, [cloud()])
    results = h.translate("你好", "再见", "谢谢")
    assert [r.line_id for r in results] == ["L0", "L1", "L2"]
    assert [r.source_text for r in results] == ["你好", "再见", "谢谢"]
    assert texts(results) == ["[cloud] 你好", "[cloud] 再见", "[cloud] 谢谢"]
    assert all(r.provider == "cloud" and r.status == "ok" and not r.cached for r in results)


def test_one_provider_call_per_batch(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    h.translate(*[f"行{i}" for i in range(20)])
    assert len(primary.requests) == 1 and len(primary.requests[0].items) == 20


def test_identical_lines_in_a_batch_are_sent_once_and_fanned_out(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    results = h.translate("同一句", "别的", "同一句")
    assert [i.text for i in primary.requests[0].items] == ["同一句", "别的"]
    assert texts(results) == ["[cloud] 同一句", "[cloud] 别的", "[cloud] 同一句"]
    assert [r.line_id for r in results] == ["L0", "L1", "L2"]


def test_source_lang_and_target_lang_reach_the_translator(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    h.service.translate(items("你好"), source_lang="zh")
    assert primary.requests[0].source_lang == "zh"
    assert primary.requests[0].target_lang == CONFIG.translate.target_lang


# ---- redaction ---------------------------------------------------------------------------


def test_numbers_never_reach_the_translator_and_come_back_in_the_result(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    (result,) = h.translate("转账给13800138000 谢谢")
    sent = primary.requests[0].items[0].text
    assert "13800138000" not in sent and "[#1]" in sent
    assert result.text == "[cloud] 转账给13800138000 谢谢"
    assert result.source_text == "转账给13800138000 谢谢"


def test_context_is_redacted_too(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    h.translate("我的卡号 6222021234567890")
    h.translate("好的")
    context = primary.requests[1].context
    assert context and all("6222" not in c.source_text + c.translated_text for c in context)
    assert "[#1]" in context[0].source_text


def test_the_cache_holds_only_redacted_text(tmp_path):
    h = Harness(tmp_path, [cloud(model="m1")])
    h.translate("号码 13800138000")
    h.cache.close()
    blob = b"".join(p.read_bytes() for p in tmp_path.glob("gloss.sqlite3*"))
    assert b"13800138000" not in blob


def test_same_text_different_numbers_is_one_cache_entry_with_each_own_number(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    (first,) = h.translate("转账 11111111")
    (second,) = h.translate("转账 22222222")
    assert len(primary.requests) == 1
    assert second.cached and second.text == "[cloud] 转账 22222222"
    assert first.text == "[cloud] 转账 11111111"


# ---- cache -------------------------------------------------------------------------------


def test_cache_hit_makes_no_api_call_and_is_marked_cached(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    h.translate("你好")
    (again,) = h.translate("你好")
    assert len(primary.requests) == 1
    assert again.cached and again.latency_ms == 0 and again.provider == "cloud"
    assert again.text == "[cloud] 你好" and again.model == "claude-haiku-5-5"


def test_cache_survives_a_restart(tmp_path):
    first = Harness(tmp_path, [cloud()])
    first.translate("你好")
    first.service.close()
    second_primary = cloud()
    second = Harness(tmp_path, [second_primary])
    (result,) = second.translate("你好")
    assert result.cached and second_primary.requests == []


def test_a_glossary_edit_misses_the_cache(tmp_path):
    path = tmp_path / "g.yaml"
    path.write_text("terms:\n  - {term: 老板, en: boss}\n", encoding="utf-8")
    store = GlossaryStore(path)
    primary = cloud()
    h = Harness(tmp_path, [primary], glossary=store)
    h.translate("你好")
    h.translate("你好")
    assert len(primary.requests) == 1
    path.write_text("terms:\n  - {term: 老板, en: sir}\n", encoding="utf-8")
    import os
    import time

    stamp = time.time_ns() + 5_000_000_000
    os.utime(path, ns=(stamp, stamp))
    h.translate("你好")
    assert len(primary.requests) == 2
    assert primary.requests[1].glossary.entries == (GlossaryEntry("老板", "sir"),)


def test_glossary_is_passed_to_the_translator(tmp_path):
    path = tmp_path / "g.yaml"
    path.write_text("terms:\n  - {term: 老板, en: boss}\n", encoding="utf-8")
    primary = cloud()
    Harness(tmp_path, [primary], glossary=GlossaryStore(path)).translate("你好")
    assert primary.requests[0].glossary.entries[0].term == "老板"


def test_provider_and_model_are_part_of_the_cache_key(tmp_path):
    a = Harness(tmp_path, [cloud(model="m1")])
    a.translate("你好")
    a.service.close()
    other_model = cloud(model="m2")
    b = Harness(tmp_path, [other_model])
    (result,) = b.translate("你好")
    assert not result.cached and len(other_model.requests) == 1


def test_low_quality_lines_are_never_cached(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    h.translate("糊糊的字", low=True)
    h.translate("糊糊的字")
    assert len(primary.requests) == 2
    assert primary.requests[0].items[0].low_quality is True


def test_passthrough_and_failed_results_are_not_cached(tmp_path):
    h = Harness(
        tmp_path, [cloud(behave=lambda r, n: (_ for _ in ()).throw(TransientError("x"))), local()]
    )
    h.translate("你好")
    fresh = cloud()
    again = Harness(tmp_path, [fresh])
    (result,) = again.translate("你好")
    assert not result.cached


def test_a_broken_cache_never_stops_translation(tmp_path):
    class BrokenCache:
        def get(self, key):
            raise PermanentError("db")

        def put(self, key, text):
            raise PermanentError("db")

        def close(self):
            pass

    primary = cloud()
    service = TranslationService([primary], settings=SETTINGS, cache=BrokenCache())
    (result,) = service.translate(items("你好"))
    assert result.status == "ok"


def test_fallback_cache_is_used_only_while_the_primary_is_unusable(tmp_path):
    seed = Harness(tmp_path, [cloud("small")])
    seed.translate("你好", "谢谢")
    seed.service.close()
    healthy = Harness(tmp_path, [cloud("big"), cloud("small")])
    (result,) = healthy.translate("谢谢")
    assert result.provider == "big" and not result.cached, "a healthy primary is preferred"
    down = cloud("big", lambda r, n: (_ for _ in ()).throw(PermanentError("no key")))
    h2 = Harness(tmp_path, [down, cloud("small")])
    h2.translate("先试一下")  # this call switches the primary off
    (hit,) = h2.translate("你好")
    assert hit.provider == "small" and hit.cached


# ---- retry, breaker, fallback ------------------------------------------------------------


def test_transient_errors_are_retried_with_backoff_then_succeed(tmp_path):
    def behave(request, n):
        if n < 3:
            raise TransientError("blip")
        return primary.respond(request)

    primary = cloud(behave=behave)
    h = Harness(tmp_path, [primary])
    (result,) = h.translate("你好")
    assert result.status == "ok" and len(primary.requests) == 3
    assert len(h.sleeps) == 2 and all(s > 0 for s in h.sleeps)
    assert h.service.status().providers[0].state == "ok"


def test_retry_after_is_honoured(tmp_path):
    def behave(request, n):
        if n == 1:
            raise TransientError("429", retry_after_s=2.0)
        return primary.respond(request)

    primary = cloud(behave=behave)
    h = Harness(tmp_path, [primary])
    h.translate("你好")
    assert h.sleeps[0] >= 2.0


def test_retries_exhausted_falls_to_the_next_translator_with_one_breaker_failure(tmp_path):
    primary = cloud(behave=lambda r, n: (_ for _ in ()).throw(TransientError("down")))
    h = Harness(tmp_path, [primary, local()])
    (result,) = h.translate("你好")
    assert result.provider == "null" and result.status == "passthrough" and result.text == "你好"
    assert len(primary.requests) == 1 + SETTINGS.retry.retry_max
    status = h.service.status()
    assert status.using_fallback and status.health.state == "degraded"


def test_breaker_opens_after_n_failed_calls_and_primary_is_then_skipped(tmp_path):
    primary = cloud(behave=lambda r, n: (_ for _ in ()).throw(TransientError("down")))
    h = Harness(tmp_path, [primary, local()])
    for i in range(SETTINGS.breaker_failures):
        h.translate(f"行{i}")
    calls = len(primary.requests)
    h.translate("又一行")
    assert len(primary.requests) == calls, "an open circuit means no more calls"
    status = h.service.status()
    assert status.providers[0].state == "open"
    assert "not responding" in status.banner


def test_breaker_recovers_after_the_cooldown(tmp_path):
    state = {"down": True}

    def behave(request, n):
        if state["down"]:
            raise TransientError("down")
        return primary.respond(request)

    primary = cloud(behave=behave)
    h = Harness(tmp_path, [primary, local()])
    for i in range(SETTINGS.breaker_failures):
        h.translate(f"行{i}")
    state["down"] = False
    h.time["mono"] += SETTINGS.breaker_cooldown_s + 1
    (result,) = h.translate("恢复了")
    assert result.provider == "cloud" and h.service.status().providers[0].state == "ok"


def test_a_stray_exception_counts_as_transient_and_never_escapes(tmp_path):
    primary = cloud(behave=lambda r, n: (_ for _ in ()).throw(KeyError("boom 你好")))
    h = Harness(tmp_path, [primary, local()])
    (result,) = h.translate("你好")
    assert result.status == "passthrough"


def test_permanent_error_switches_the_provider_off_and_shows_why(tmp_path):
    primary = cloud(
        behave=lambda r, n: (_ for _ in ()).throw(PermanentError("The API key was rejected."))
    )
    h = Harness(tmp_path, [primary, local()])
    h.translate("你好")
    h.translate("再来")
    assert len(primary.requests) == 1
    status = h.service.status()
    assert status.providers[0].state == "disabled"
    assert status.banner == "The API key was rejected."


def test_unavailable_at_open_skips_the_provider_and_retries_after_the_cooldown(tmp_path):
    primary = cloud()
    primary.open_error = AdapterUnavailable("No API key. Add one in settings.")
    h = Harness(tmp_path, [primary, local()])
    h.service.open()
    status = h.service.status()
    assert status.providers[0].state == "unavailable" and "No API key" in status.banner
    (result,) = h.translate("你好")
    assert result.provider == "null" and primary.opens == 1
    h.time["mono"] += SETTINGS.breaker_cooldown_s + 1
    primary.open_error = None
    (later,) = h.translate("别的")
    assert later.provider == "cloud" and primary.opens == 2


def test_retry_unavailable_picks_up_a_newly_added_key_at_once(tmp_path):
    primary = cloud()
    primary.open_error = AdapterUnavailable("No API key")
    h = Harness(tmp_path, [primary, local()])
    h.service.open()
    primary.open_error = None
    h.service.retry_unavailable()
    (result,) = h.translate("你好")
    assert result.provider == "cloud"


def test_every_translator_failing_returns_failed_lines_not_an_exception(tmp_path):
    down = lambda r, n: (_ for _ in ()).throw(TransientError("x"))  # noqa: E731
    h = Harness(tmp_path, [cloud(behave=down), cloud("second", down)])
    results = h.translate("你好", "再见")
    assert [(r.status, r.text) for r in results] == [("failed", "")] * 2
    assert h.service.status().health.state == "down"


# ---- failed lines ------------------------------------------------------------------------


def test_a_refused_batch_is_bisected_until_the_offending_line_is_alone(tmp_path):
    bad = "敏感内容"

    def behave(request, n):
        if any(i.text == bad for i in request.items):
            return primary.respond(request, failed={i.text: "refused" for i in request.items})
        return primary.respond(request)

    primary = cloud(behave=behave)
    h = Harness(tmp_path, [primary, local()])
    results = h.translate("一", "二", bad, "四", "五")
    by_source = {r.source_text: r for r in results}
    assert by_source[bad].provider == "null" and by_source[bad].status == "passthrough"
    assert all(by_source[t].provider == "cloud" for t in ("一", "二", "四", "五"))
    assert h.service.status().providers[0].state == "ok", "refusals do not trip the breaker"
    assert len(primary.requests) <= 2 * 5, "the search for the bad line stays cheap"


def test_a_line_refused_everywhere_comes_back_failed_with_the_reason(tmp_path):
    refuse = lambda r, n: CloudScripted("c").respond(r, {i.text: "refused" for i in r.items})  # noqa: E731
    h = Harness(tmp_path, [cloud(behave=refuse)])
    (result,) = h.translate("敏感")
    assert (result.status, result.failure, result.text) == ("failed", "refused", "")


def test_refusals_never_open_the_breaker(tmp_path):
    primary = cloud()
    primary.behave = lambda r, n: primary.respond(r, {i.text: "refused" for i in r.items})
    h = Harness(tmp_path, [primary, local()])
    for i in range(SETTINGS.breaker_failures + 2):
        h.translate(f"行{i}")
    assert h.service.status().providers[0].state == "ok"


def test_missing_lines_are_requeued_once_then_moved_down_the_chain(tmp_path):
    primary = cloud()
    primary.behave = lambda r, n: primary.respond(r, {"二": "missing"})
    h = Harness(tmp_path, [primary, local()])
    results = h.translate("一", "二", "三")
    assert [r.provider for r in results] == ["cloud", "null", "cloud"]
    assert [len(r.items) for r in primary.requests] == [3, 1]
    assert [i.text for i in primary.requests[1].items] == ["二"]


def test_a_missing_line_that_succeeds_on_the_requeue_is_translated(tmp_path):
    primary = cloud()
    primary.behave = lambda r, n: primary.respond(r, {"二": "missing"} if n == 1 else None)
    h = Harness(tmp_path, [primary])
    results = h.translate("一", "二")
    assert texts(results) == ["[cloud] 一", "[cloud] 二"]


def test_truncated_batches_are_split(tmp_path):
    primary = cloud()
    primary.behave = lambda r, n: (
        primary.respond(r, {i.text: "truncated" for i in r.items})
        if len(r.items) > 2
        else primary.respond(r)
    )
    h = Harness(tmp_path, [primary])
    results = h.translate("一", "二", "三", "四")
    assert all(r.status == "ok" for r in results)
    assert [len(r.items) for r in primary.requests] == [4, 2, 2]


def test_malformed_provider_output_is_not_trusted(tmp_path):
    primary = cloud()
    primary.behave = lambda r, n: CloudScripted("x").respond(
        type(r)(r.items[:-1], r.source_lang, r.target_lang)
    )
    h = Harness(tmp_path, [primary, local()])
    results = h.translate("一", "二")
    assert all(r.provider == "null" for r in results)


# ---- budget ------------------------------------------------------------------------------


def test_usage_is_priced_recorded_and_exposed(tmp_path):
    primary = cloud(model="claude-haiku-5-5")  # the scripted usage is 1M input tokens
    h = Harness(tmp_path, [primary], hourly=100.0, monthly=1000.0)
    h.translate("你好")
    price = CONFIG.translate.price_for("claude-haiku-5-5").input
    status = h.service.status()
    assert status.session_cost_usd == pytest.approx(price)
    assert status.budget.hour_usd == pytest.approx(price)
    assert h.ledger.total_since(0) == pytest.approx(price)


def test_hourly_limit_stops_paid_calls_and_the_chain_moves_on(tmp_path):
    primary = cloud(model="claude-haiku-5-5")
    h = Harness(tmp_path, [primary, local()], hourly=0.05, monthly=100.0)
    h.translate("你好")  # costs $0.10, over the $0.05 hourly limit
    (result,) = h.translate("再见")
    assert result.provider == "null" and len(primary.requests) == 1
    status = h.service.status()
    assert status.budget.level == "hourly_limit"
    assert "hourly" in status.banner and status.health.state == "degraded"


def test_cache_hits_still_work_when_the_budget_is_spent(tmp_path):
    primary = cloud(model="claude-haiku-5-5")
    h = Harness(tmp_path, [primary, local()], hourly=0.05, monthly=100.0)
    h.translate("你好")
    (result,) = h.translate("你好")
    assert result.cached and result.provider == "cloud"


def test_budget_resumes_when_the_hour_passes(tmp_path):
    primary = cloud(model="claude-haiku-5-5")
    h = Harness(tmp_path, [primary, local()], hourly=0.05, monthly=100.0)
    h.translate("你好")
    h.time["wall"] += 3700
    (result,) = h.translate("再见")
    assert result.provider == "cloud"


def test_local_providers_are_not_stopped_by_the_budget(tmp_path):
    h = Harness(tmp_path, [local()], hourly=0.0001, monthly=0.0001)
    (result,) = h.translate("你好")
    assert result.status == "passthrough"


def test_monthly_warning_shows_in_the_banner_but_calls_continue(tmp_path):
    h = Harness(tmp_path, [cloud(model="claude-haiku-5-5")], hourly=100.0, monthly=0.12)
    h.translate("你好")  # $0.10 of $0.12 is over 80 percent
    status = h.service.status()
    assert status.budget.level == "warn" and "monthly" in status.banner
    assert status.providers[0].state == "ok"
    (result,) = h.translate("再见")
    assert result.provider == "cloud"


# ---- context -----------------------------------------------------------------------------


def test_context_has_the_last_k_translated_lines_only(tmp_path):
    k = SETTINGS.context_lines
    primary = cloud()
    h = Harness(tmp_path, [primary])
    for i in range(k + 5):
        h.translate(f"第{i}行")
    context = primary.requests[-1].context
    assert len(context) == k
    assert context[-1].source_text == f"第{k + 3}行"
    assert context[0].source_text == f"第{4}行"


def test_context_zero_sends_none(tmp_path):
    from dataclasses import replace

    primary = cloud()
    h = Harness(tmp_path, [primary], settings=replace(SETTINGS, context_lines=0))
    h.translate("一")
    h.translate("二")
    assert primary.requests[1].context == ()


def test_low_quality_and_passthrough_lines_do_not_become_context(tmp_path):
    primary = cloud()
    h = Harness(tmp_path, [primary])
    h.translate("糊", low=True)
    h.translate("好")
    assert primary.requests[1].context == ()


# ---- status ------------------------------------------------------------------------------


def test_status_is_ok_with_no_banner_when_all_is_well(tmp_path):
    h = Harness(tmp_path, [cloud(), local()])
    h.service.open()
    h.translate("你好")
    status = h.service.status()
    assert status.health.state == "ok" and status.banner == "" and not status.using_fallback
    assert status.provider == "cloud"
    assert [p.name for p in status.providers] == ["cloud", "null"]


def test_close_closes_translators_and_cache_and_is_safe_twice(tmp_path):
    h = Harness(tmp_path, [cloud(), local()])
    h.translate("你好")
    h.service.close()
    h.service.close()


# ---- the real adapters through the service -----------------------------------------------


class RealWithFake(AnthropicTranslator):
    def __init__(self, context, client):
        super().__init__(context)
        self.client = client

    def _make_client(self, api_key):
        return self.client


def real_primary(client):
    settings = CONFIG.adapter_settings("translate", "anthropic")
    return RealWithFake(AdapterContext(settings, KeySecrets()), client)


def test_real_adapter_through_the_service_end_to_end(tmp_path):
    client = FakeClient()
    h = Harness(tmp_path, [real_primary(client), NullTranslator(AdapterContext())])
    h.service.open()
    results = h.translate("你好，转账 13800138000", "谢谢 🎉")
    assert all(r.provider == "anthropic" and r.status == "ok" for r in results)
    assert results[0].text == "[en] 你好，转账 13800138000"
    sent = client.calls[0]["messages"][0]["content"]
    assert "13800138000" not in sent
    again = h.translate("你好，转账 13800138000")
    assert again[0].cached and len(client.calls) == 1
    assert h.service.status().session_cost_usd > 0


def test_real_adapter_refusal_isolates_the_line_and_falls_to_null(tmp_path):
    def responder(kwargs):
        payload = json.loads(kwargs["messages"][0]["content"])
        if any("敏感" in line["t"] for line in payload["lines"]):
            return reply(text="", stop="refusal", usage=(40, 0, 0, 0))
        rows = [{"i": ln["i"], "en": f"ok {ln['t']}"} for ln in payload["lines"]]
        return reply(rows)

    client = FakeClient(responder)
    h = Harness(tmp_path, [real_primary(client), NullTranslator(AdapterContext())])
    results = h.translate("你好", "敏感词", "再见")
    assert [(r.provider, r.status) for r in results] == [
        ("anthropic", "ok"),
        ("null", "passthrough"),
        ("anthropic", "ok"),
    ]
    assert h.service.status().providers[0].state == "ok"


def test_no_credit_error_switches_anthropic_off_and_shows_the_message(tmp_path):
    from translate_fakes import FakeStatusError

    body = {"error": {"message": "Your credit balance is too low"}}
    client = FakeClient(lambda kw: FakeStatusError(400, body))
    h = Harness(tmp_path, [real_primary(client), NullTranslator(AdapterContext())])
    h.service.open()
    (result,) = h.translate("你好")
    assert result.provider == "null"
    assert "no credit" in h.service.status().banner
    h.translate("再来")
    assert len(client.calls) == 1
