"""The Anthropic adapter, driven by a fake SDK client. No real API call is ever made."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from testkit.core import registered_subject
from testkit.discovery import NO_USER_CONFIG
from testkit.translate import (
    CHECKS,
    assert_valid_response,
    check_a_line_missing_from_the_reply_is_failed_missing,
    check_failed_lines_still_report_billed_tokens,
)
from translate_fakes import SECRET, FakeClient, FakeStatusError, KeySecrets, echo, reply

from gloss.adapters.translate.anthropic_translator import (
    AnthropicTranslator,
    build_user_payload,
    parse_translations,
)
from gloss.core.config import load_config
from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    ContextLine,
    Glossary,
    GlossaryEntry,
    PermanentError,
    TransientError,
    TranslationItem,
    TranslationRequest,
)
from gloss.core.registry import default_registry

CONFIG = load_config(user_path=NO_USER_CONFIG).config
SETTINGS = CONFIG.adapter_settings("translate", "anthropic")


class WithFakeClient(AnthropicTranslator):
    """The real adapter, with the SDK client swapped for a fake."""

    client: FakeClient

    def __init__(self, context: AdapterContext) -> None:
        super().__init__(context)
        omit = {int(i[1:]) + 1 for i in context.settings.get("omit_ids", ())}
        self.client = FakeClient(lambda kw: echo(kw, omit))

    def _make_client(self, api_key: str) -> FakeClient:
        return self.client


def adapter(settings: dict | None = None, secrets=None) -> WithFakeClient:
    context = AdapterContext({**SETTINGS, **(settings or {})}, secrets or KeySecrets())
    return WithFakeClient(context)


def opened(settings: dict | None = None) -> WithFakeClient:
    a = adapter(settings)
    a.open()
    return a


def request(*texts: str, **extra) -> TranslationRequest:
    items = tuple(TranslationItem(f"L{i}", t) for i, t in enumerate(texts))
    return TranslationRequest(items, extra.pop("source_lang", None), "en", **extra)


# ---- registration and the contract checks ------------------------------------------------


def test_registered_with_a_contract_requirement():
    from testkit.discovery import registered_names

    assert "anthropic" in registered_names("translate")
    cls = default_registry.create("translate", "anthropic", AdapterContext(SETTINGS))
    assert cls.REQUIRES_CONTRACT == ">=1.0,<2.0"
    assert "LOCAL_ONLY" not in vars(type(cls)), "a cloud translator must not claim LOCAL_ONLY"


def subject():
    return registered_subject(
        "translate",
        "anthropic",
        WithFakeClient,
        base_settings=dict(SETTINGS),
        scripted=True,
        secrets=KeySecrets(),
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
def test_contract_checks_pass_with_a_fake_client(check):
    s = subject()
    try:
        check(s)
    finally:
        s.release()


def test_missing_line_check_passes():
    s = registered_subject(
        "translate",
        "anthropic",
        WithFakeClient,
        base_settings={**SETTINGS, "omit_ids": ["L2"]},
        scripted=True,
        secrets=KeySecrets(),
    )
    try:
        check_a_line_missing_from_the_reply_is_failed_missing(s)
        check_failed_lines_still_report_billed_tokens(s)
    finally:
        s.release()


# ---- settings and opening ----------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"model": None},
        {"model": ""},
        {"thinking": "maybe"},
        {"effort": 3},
        {"max_tokens_base": "lots"},
        {"max_tokens_base": 0, "max_tokens_per_line": 0},
        {"cache_ttl": "2h"},
        {"timeout_ms": "soon"},
        {"timeout_ms": 0},
        {"secret_name": ""},
        {"base_url": 5},
    ],
)
def test_bad_settings_raise_a_gloss_error(bad):
    with pytest.raises(PermanentError):
        adapter(bad)


def test_open_without_a_key_is_unavailable_and_health_says_so():
    a = adapter(secrets=KeySecrets(None))
    with pytest.raises(AdapterUnavailable) as err:
        a.open()
    assert a.health().state == "down"
    assert SECRET not in str(err.value)


def test_open_without_a_key_store_is_unavailable():
    with pytest.raises(AdapterUnavailable):
        WithFakeClient(AdapterContext(dict(SETTINGS), None)).open()


def test_open_reads_the_key_from_the_configured_secret_name():
    asked = []

    class Spy(KeySecrets):
        def get(self, name):
            asked.append(name)
            return super().get(name)

    a = WithFakeClient(AdapterContext(dict(SETTINGS), Spy()))
    a.open()
    assert asked == [SETTINGS["secret_name"]]
    assert a.health().state == "ok"


def test_open_rejects_a_model_the_provider_does_not_know():
    a = adapter()
    a.client.retrieve_error = FakeStatusError(404)
    with pytest.raises(PermanentError, match="model"):
        a.open()


def test_open_rejects_a_key_the_provider_refuses():
    a = adapter()
    a.client.retrieve_error = FakeStatusError(401)
    with pytest.raises(PermanentError, match="key"):
        a.open()


def test_open_says_so_when_the_provider_cannot_be_reached():
    for error in (ConnectionError("down " + SECRET), FakeStatusError(529), FakeStatusError(429)):
        a = adapter()
        a.client.retrieve_error = error
        with pytest.raises(AdapterUnavailable, match="internet") as caught:
            a.open()
        assert SECRET not in str(caught.value)
        assert a.health().state == "down"


@pytest.mark.parametrize(
    ("status", "body", "words"),
    [
        (401, None, "key"),
        (403, None, "not allowed"),
        (404, None, "model"),
        (400, {"error": {"message": "Your credit balance is too low"}}, "credit"),
    ],
)
def test_open_messages_are_plain_and_never_hold_the_key(status, body, words):
    a = adapter()
    a.client.retrieve_error = FakeStatusError(status, body)
    with pytest.raises(PermanentError, match=words) as caught:
        a.open()
    assert SECRET not in str(caught.value)
    assert a.health().state == "down"
    assert a.client.calls == [], "open() sends no text"


def test_open_with_a_malformed_stored_key_is_refused_without_a_call():
    class Bad(KeySecrets):
        def get(self, name):
            return "has spaces in it"

    a = WithFakeClient(AdapterContext(dict(SETTINGS), Bad()))
    with pytest.raises(PermanentError, match="key"):
        a.open()
    assert a.client is None or a.client.calls == []


def test_thinking_is_dropped_if_the_model_cannot_switch_it_off():
    a = adapter()
    a.client.model_info.capabilities.thinking.types.disabled.supported = False
    a.open()
    a.translate(request("你好"))
    assert "thinking" not in a.client.calls[0]


def test_close_is_safe_and_closes_the_client():
    a = opened()
    a.close()
    a.close()
    assert a.client.closed
    assert a.health().state == "down"
    with pytest.raises(AdapterUnavailable):
        a.translate(request("你好"))


def test_the_key_never_appears_in_repr_or_health():
    a = opened()
    assert SECRET not in repr(a.__dict__.get("_health")) + a.health().detail
    assert not any(SECRET in str(v) for k, v in vars(a).items() if k != "client")


def test_real_sdk_client_is_built_with_no_retries_and_a_timeout():
    class Real(AnthropicTranslator):
        pass

    a = Real(AdapterContext(dict(SETTINGS), KeySecrets()))
    client = a._make_client("not-a-real-key-just-for-construction")
    assert client.max_retries == 0
    assert client.timeout == SETTINGS["timeout_ms"] / 1000


# ---- request shape -----------------------------------------------------------------------


def test_request_uses_config_for_model_tokens_effort_and_thinking():
    a = opened()
    a.translate(request("你好", "谢谢", "再见"))
    call = a.client.calls[0]
    assert call["model"] == SETTINGS["model"]
    assert call["max_tokens"] == SETTINGS["max_tokens_base"] + 3 * SETTINGS["max_tokens_per_line"]
    assert call["output_config"]["effort"] == SETTINGS["effort"]
    assert call["thinking"] == {"type": "disabled"}
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["required"] == ["translations"]


def test_config_changes_change_the_request_without_code_changes():
    a = opened({"model": "other-model", "effort": "high", "thinking": "default"})
    a.translate(request("你好"))
    call = a.client.calls[0]
    assert call["model"] == "other-model"
    assert call["output_config"]["effort"] == "high"
    assert "thinking" not in call
    assert a.model == "other-model"


def test_adaptive_thinking_is_passed_through():
    a = opened({"thinking": "adaptive"})
    a.translate(request("你好"))
    assert a.client.calls[0]["thinking"] == {"type": "adaptive"}


def test_system_prompt_is_one_cached_block_with_style_rules_and_glossary():
    a = opened()
    glossary = Glossary("g1", (GlossaryEntry("老板", "boss", "paying customer"),))
    a.translate(request("你好", glossary=glossary))
    (block,) = a.client.calls[0]["system"]
    assert block["cache_control"] == {"type": "ephemeral"}
    text = block["text"]
    assert "老板 => boss (paying customer)" in text
    assert "No commentary" in text and "emoji" in text
    assert "{{" not in text, "every template placeholder must be filled"


def test_cache_ttl_comes_from_config():
    a = opened({"cache_ttl": "1h"})
    a.translate(request("你好"))
    assert a.client.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_system_prefix_is_identical_across_calls_so_it_can_be_cached():
    a = opened()
    glossary = Glossary("g1", (GlossaryEntry("老板", "boss"),))
    a.translate(request("你好", glossary=glossary))
    a.translate(request("再见", "谢谢", glossary=glossary, context=(ContextLine("a", "b"),)))
    first, second = (c["system"][0]["text"] for c in a.client.calls)
    assert first == second


def test_changed_glossary_changes_the_prefix():
    a = opened()
    a.translate(request("你好", glossary=Glossary("1", (GlossaryEntry("老板", "boss"),))))
    a.translate(request("你好", glossary=Glossary("2", (GlossaryEntry("老板", "sir"),))))
    assert a.client.calls[0]["system"][0]["text"] != a.client.calls[1]["system"][0]["text"]


def test_context_and_lines_go_in_the_user_message_not_the_cached_prefix():
    a = opened()
    ctx = (ContextLine("你好", "hello"), ContextLine("再见", "bye"))
    a.translate(request("谢谢", context=ctx, source_lang="zh"))
    call = a.client.calls[0]
    assert "hello" not in call["system"][0]["text"]
    payload = json.loads(call["messages"][0]["content"])
    assert payload["context"] == [{"t": "你好", "en": "hello"}, {"t": "再见", "en": "bye"}]
    assert payload["lines"] == [{"i": 1, "t": "谢谢"}]
    assert payload["source_lang"] == "zh"


def test_payload_flags_low_quality_lines_and_omits_empty_extras():
    req = TranslationRequest(
        (TranslationItem("a", "你好", low_quality=True), TranslationItem("b", "ok")), None, "en"
    )
    payload = json.loads(build_user_payload(req))
    assert payload == {"lines": [{"i": 1, "t": "你好", "q": True}, {"i": 2, "t": "ok"}]}


def test_text_is_sent_as_is_not_escaped_to_ascii():
    req = request("你好 🎉")
    assert "你好 🎉" in build_user_payload(req)


def test_empty_request_makes_no_call_and_bills_nothing():
    a = opened()
    response = a.translate(request())
    assert response.results == () and response.usage is None
    assert a.client.calls == []


# ---- responses ---------------------------------------------------------------------------


def test_results_follow_request_order_whatever_order_the_model_answers_in():
    a = opened()
    a.client.responder = lambda kw: reply(
        [{"i": 2, "en": "two"}, {"i": 3, "en": "three"}, {"i": 1, "en": "one"}]
    )
    response = a.translate(request("a", "b", "c"))
    assert [r.text for r in response.results] == ["one", "two", "three"]
    assert [r.line_id for r in response.results] == ["L0", "L1", "L2"]
    assert all(r.provider == "anthropic" and r.model == SETTINGS["model"] for r in response.results)


def test_usage_is_reported_from_the_providers_numbers():
    a = opened()
    a.client.responder = lambda kw: reply([{"i": 1, "en": "hi"}], usage=(11, 22, 33, 44))
    usage = a.translate(request("你好")).usage
    assert (usage.input_tokens, usage.output_tokens) == (11, 22)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (33, 44)
    assert usage.provider == "anthropic" and usage.model == SETTINGS["model"]


def test_a_line_missing_from_the_reply_is_failed_missing_and_others_survive():
    a = opened()
    a.client.responder = lambda kw: reply([{"i": 1, "en": "one"}, {"i": 3, "en": "three"}])
    response = a.translate(request("a", "b", "c"))
    statuses = [(r.status, r.failure) for r in response.results]
    assert statuses == [("ok", None), ("failed", "missing"), ("ok", None)]
    assert response.usage.input_tokens > 0


@pytest.mark.parametrize(
    "text",
    ["not json at all", "[]", '{"translations": "x"}', '{"other": []}', "", "null"],
)
def test_malformed_replies_fail_every_line_as_invalid_output(text):
    a = opened()
    a.client.responder = lambda kw: reply(text=text)
    response = a.translate(request("a", "b"))
    assert [(r.status, r.failure) for r in response.results] == [("failed", "invalid_output")] * 2
    assert response.usage is not None
    assert_valid_response(request("a", "b"), response, "anthropic")


def test_garbage_entries_are_skipped_and_the_rest_is_kept():
    a = opened()
    a.client.responder = lambda kw: reply(
        [{"i": 1, "en": "one"}, "junk", {"i": "2", "en": "x"}, {"i": True, "en": "y"}, {"i": 2}]
    )
    response = a.translate(request("a", "b"))
    assert [(r.status, r.failure) for r in response.results] == [
        ("ok", None),
        ("failed", "missing"),
    ]


def test_a_blank_translation_is_invalid_output():
    a = opened()
    a.client.responder = lambda kw: reply([{"i": 1, "en": "  "}])
    (result,) = a.translate(request("a")).results
    assert (result.status, result.failure) == ("failed", "invalid_output")


def test_duplicate_ids_keep_the_first():
    assert parse_translations('{"translations":[{"i":1,"en":"a"},{"i":1,"en":"b"}]}') == {1: "a"}


def test_refusal_fails_every_line_as_refused_and_still_reports_usage():
    a = opened()
    a.client.responder = lambda kw: reply(text="", stop="refusal", usage=(50, 0, 0, 0))
    request_ = request("a", "b", "c")
    response = a.translate(request_)
    assert [(r.status, r.failure, r.text) for r in response.results] == [
        ("failed", "refused", "")
    ] * 3
    assert response.usage.input_tokens == 50
    assert_valid_response(request_, response, "anthropic")


def test_refusal_does_not_raise_or_look_like_an_outage():
    a = opened()
    a.client.responder = lambda kw: reply(text="", stop="refusal")
    a.translate(request("a"))
    assert a.health().state == "ok"


def test_truncation_fails_missing_lines_as_truncated():
    a = opened()
    a.client.responder = lambda kw: reply(
        text='{"translations":[{"i":1,"en":"one"},{"i":2,"en":"tw', stop="max_tokens"
    )
    response = a.translate(request("a", "b"))
    assert [r.failure for r in response.results] == ["truncated", "truncated"]
    assert response.usage.output_tokens > 0


def test_truncation_with_valid_json_keeps_complete_lines():
    a = opened()
    a.client.responder = lambda kw: reply([{"i": 1, "en": "one"}], stop="max_tokens")
    response = a.translate(request("a", "b"))
    assert [(r.status, r.failure) for r in response.results] == [
        ("ok", None),
        ("failed", "truncated"),
    ]


def test_reasoning_blocks_are_ignored():
    a = opened()
    thinking = SimpleNamespace(type="thinking", thinking="hmm")
    good = reply([{"i": 1, "en": "hi"}])
    good.content.insert(0, thinking)
    a.client.responder = lambda kw: good
    (result,) = a.translate(request("你好")).results
    assert result.text == "hi"


# ---- errors ------------------------------------------------------------------------------


def fail_with(a: WithFakeClient, error: Exception) -> Exception:
    a.client.responder = lambda kw: error
    with pytest.raises((TransientError, PermanentError)) as caught:
        a.translate(request("你好"))
    return caught.value


@pytest.mark.parametrize("status", [500, 502, 503, 529, 408, 409])
def test_server_trouble_is_transient(status):
    a = opened()
    error = fail_with(a, FakeStatusError(status))
    assert isinstance(error, TransientError)
    assert a.health().state == "degraded"


def test_rate_limit_is_transient_and_carries_retry_after():
    a = opened()
    error = fail_with(a, FakeStatusError(429, headers={"retry-after": "7"}))
    assert isinstance(error, TransientError) and error.retry_after_s == 7.0


def test_rate_limit_with_a_junk_retry_after_is_still_transient():
    a = opened()
    error = fail_with(a, FakeStatusError(429, headers={"retry-after": "soon"}))
    assert isinstance(error, TransientError) and error.retry_after_s is None


def test_spend_limit_is_permanent_not_retried():
    a = opened()
    body = {"error": {"type": "rate_limit_error", "message": "enforced_spend_limit_reached"}}
    error = fail_with(a, FakeStatusError(429, body))
    assert isinstance(error, PermanentError) and "spend limit" in str(error)
    assert a.health().state == "down"


def test_no_credit_is_permanent_with_a_clear_message():
    a = opened()
    body = {"error": {"message": "Your credit balance is too low to access the API"}}
    error = fail_with(a, FakeStatusError(400, body))
    assert isinstance(error, PermanentError) and "credit" in str(error)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_client_errors_are_permanent(status):
    assert isinstance(fail_with(opened(), FakeStatusError(status)), PermanentError)


def test_network_errors_and_timeouts_are_transient():
    assert isinstance(fail_with(opened(), TimeoutError("read timed out")), TransientError)
    assert isinstance(fail_with(opened(), ConnectionError("reset")), TransientError)


def test_error_messages_never_contain_the_reply_body_or_the_key():
    body = {"error": {"message": f"bad things about 你好 and {SECRET}"}}
    error = fail_with(opened(), FakeStatusError(400, body))
    assert SECRET not in str(error) and "你好" not in str(error)


def test_default_config_is_what_the_adr_says():
    assert SETTINGS["thinking"] == "disabled" and SETTINGS["effort"] == "low"
