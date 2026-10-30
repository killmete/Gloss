"""Translator contract checks, plus a conforming fake and deliberately broken ones."""

from __future__ import annotations

import socket
import time
import unicodedata
from typing import Any
from unittest import mock

from gloss.core.config import load_config
from gloss.core.contracts import (
    AdapterContext,
    ContextLine,
    Glossary,
    GlossaryEntry,
    GlossError,
    Health,
    PermanentError,
    TokenUsage,
    TranslationItem,
    TranslationRequest,
    TranslationResponse,
    TranslationResult,
)
from testkit.core import (
    COMMON_CHECKS,
    Broken,
    Subject,
    call,
    check_close_is_always_safe,
    check_health_is_instant_in_every_state,
    hostile_settings_breakers,
    identity_breakers,
    registered_subject,
)
from testkit.discovery import NO_USER_CONFIG

PORT = "translate"
FAKE_TIMEOUT_S = 0.3
FAILURES = ("refused", "missing", "truncated", "invalid_output", "other")
LINES = (
    "你好，今天一起打游戏吗？",
    "こんにちは、元気ですか",
    "Order #123 accepted \U0001f389",
    "520",
    "[REDACTED] 请发货",
)


class NoSecrets:
    """A SecretStore that holds nothing, so a real adapter can never find an API key."""

    def get(self, name: str) -> str | None:
        return None

    def set(self, name: str, value: str) -> None:
        raise AssertionError("contract tests must never write secrets")

    def delete(self, name: str) -> None:
        raise AssertionError("contract tests must never delete secrets")


def make_request(
    texts: tuple[str, ...] = LINES, *, source_lang: str | None = None, rich: bool = False
) -> TranslationRequest:
    items = tuple(
        TranslationItem(f"L{i}", t, low_quality=rich and i == 0) for i, t in enumerate(texts)
    )
    extra: dict[str, Any] = {}
    if rich:
        extra = {
            "context": (ContextLine("你好", "hello"),),
            "glossary": Glossary("7", (GlossaryEntry("老板", "boss", "polite address"),)),
        }
    return TranslationRequest(items, source_lang, "en", **extra)


def translate(adapter: Any, request: TranslationRequest) -> TranslationResponse:
    response = call(adapter.translate, request)
    assert isinstance(response, TranslationResponse), (
        f"translate() must return a TranslationResponse, got {type(response).__name__}"
    )
    return response


def assert_valid_response(
    request: TranslationRequest, response: TranslationResponse, provider: str
) -> None:
    """The rules every response must meet. Real adapters' own unit tests can reuse this."""
    assert isinstance(response.results, tuple), "results must be a tuple"
    assert len(response.results) == len(request.items), (
        f"{len(request.items)} lines in, {len(response.results)} results out. "
        "A line the provider skipped comes back as status='failed', failure='missing'."
    )
    for item, result in zip(request.items, response.results, strict=True):
        assert isinstance(result, TranslationResult)
        assert result.line_id == item.line_id, "results must follow the request's order"
        assert result.source_text == item.text, (
            f"source_text must echo the request exactly: {item.text!r} came back "
            f"as {result.source_text!r}"
        )
        assert result.provider == provider, f"provider {result.provider!r}, expected {provider!r}"
        assert result.model is None or isinstance(result.model, str)
        assert type(result.latency_ms) is int and result.latency_ms >= 0
        assert result.cached is False, "cached is set by the TranslationService, not adapters"
        assert isinstance(result.rough, bool)
        assert result.status in ("ok", "passthrough", "failed"), f"bad status {result.status!r}"
        if result.status == "ok":
            assert result.text.strip(), "an ok line needs a translation"
        elif result.status == "passthrough":
            assert result.text == item.text, "passthrough must return the original text"
        else:
            assert result.text == "" and result.failure in FAILURES, (
                f"a failed line has empty text and one of {FAILURES}, got {result.failure!r}"
            )
    usage = response.usage
    if usage is not None:
        assert isinstance(usage, TokenUsage)
        assert usage.provider == provider, f"usage.provider {usage.provider!r} != {provider!r}"
        assert isinstance(usage.model, str) and usage.model
        counts = (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        )
        assert all(type(n) is int and n >= 0 for n in counts), (
            f"token counts must be whole numbers, 0 or more: {counts}"
        )


def deadline(subject: Subject) -> float:
    if subject.timeout_s is not None:
        return subject.timeout_s
    return load_config(user_path=NO_USER_CONFIG).config.reliability.translate_timeout_ms / 1000


# ---- checks ------------------------------------------------------------------------------


def check_empty_request_returns_nothing(subject: Subject) -> None:
    adapter = subject.opened()
    response = translate(adapter, make_request(()))
    assert response.results == (), "an empty request returns no results"
    usage = response.usage
    assert usage is None or (usage.input_tokens == 0 and usage.output_tokens == 0), (
        "an empty request makes no provider call, so it bills nothing"
    )


def check_one_result_per_line_in_request_order(subject: Subject) -> None:
    adapter = subject.opened()
    request = make_request()
    assert_valid_response(request, translate(adapter, request), adapter.name)


def check_text_survives_unicode_exactly(subject: Subject) -> None:
    adapter = subject.opened()
    tricky = ("​开头", "ＡＢＣ　全角", "é café", "🎮🎮", "line with  two  spaces")
    request = make_request(tricky)
    response = translate(adapter, request)
    assert_valid_response(request, response, adapter.name)


def check_big_batches_and_repeated_text_keep_every_line(subject: Subject) -> None:
    adapter = subject.opened()
    request = make_request(tuple(f"同一句话 {i % 3}" for i in range(30)))
    assert_valid_response(request, translate(adapter, request), adapter.name)


def check_usage_is_reported_honestly(subject: Subject) -> None:
    adapter = subject.opened()
    request = make_request()
    response = translate(adapter, request)
    assert_valid_response(request, response, adapter.name)
    if response.usage is not None:
        assert response.usage.input_tokens + response.usage.output_tokens > 0, (
            "a billed call with lines in it cannot use zero tokens"
        )


def check_glossary_context_and_low_quality_flag_are_accepted(subject: Subject) -> None:
    adapter = subject.opened()
    for lang in (None, "zh", "ja"):
        request = make_request(source_lang=lang, rich=True)
        assert_valid_response(request, translate(adapter, request), adapter.name)


def check_translate_returns_within_the_time_limit(subject: Subject) -> None:
    adapter = subject.opened()
    start = time.perf_counter()
    translate(adapter, make_request())
    took = time.perf_counter() - start
    assert took < deadline(subject), (
        f"translate() took {took:.2f} s, over reliability.translate_timeout_ms"
    )


def check_local_only_declaration_is_true_to_its_word(subject: Subject) -> None:
    """LOCAL_ONLY = True means no text ever leaves this PC. Missing means cloud.

    The check watches every attempt to reach a non-local address, even one the adapter
    swallows, while the adapter opens, translates and closes.
    """
    cls = type(subject.new())
    declared = vars(cls).get("LOCAL_ONLY", False)  # the class's own, as the registry reads it
    assert isinstance(declared, bool), f"LOCAL_ONLY must be True or False, got {declared!r}"
    assert not (getattr(cls, "LOCAL_ONLY", False) is True and "LOCAL_ONLY" not in vars(cls)), (
        f"{cls.__name__} only inherits LOCAL_ONLY = True. A translator must declare it on its "
        "own class, so a cloud subclass of a local one cannot pass as local by accident."
    )
    if not declared:
        return
    attempts: list[str] = []

    def spy(kind: str):
        def refuse(*args: Any, **kwargs: Any) -> Any:
            target = args[1] if kind == "connect" and len(args) > 1 else args[0] if args else ""
            host = target[0] if isinstance(target, tuple) else target
            if host not in ("127.0.0.1", "::1", "localhost"):
                attempts.append(f"{kind} {host}")
                raise OSError("blocked by the contract test")
            return None

        return refuse

    with (
        mock.patch.object(socket.socket, "connect", spy("connect")),
        mock.patch.object(socket.socket, "connect_ex", spy("connect")),
        mock.patch.object(socket, "getaddrinfo", spy("lookup")),
        mock.patch.object(socket, "create_connection", spy("connect")),
    ):
        adapter = subject.new()
        subject.open(adapter)
        translate(adapter, make_request())
        call(adapter.close)
    assert not attempts, f"a LOCAL_ONLY translator tried the network: {attempts}"


def check_failed_line_is_returned_not_raised(subject: Subject) -> None:
    adapter = subject.opened()
    request = make_request()
    try:
        response = translate(adapter, request)
    except GlossError as exc:
        raise AssertionError(
            f"one bad line raised {type(exc).__name__}. It must come back with status='failed' "
            "so it does not count toward the circuit breaker."
        ) from exc
    assert_valid_response(request, response, adapter.name)
    failed = [r for r in response.results if r.status == "failed"]
    assert [r.line_id for r in failed] == ["L1"], "the line that was refused must be marked failed"
    assert failed[0].failure == "refused"
    assert all(r.status != "failed" for r in response.results if r.line_id != "L1")


def check_failed_lines_still_report_billed_tokens(subject: Subject) -> None:
    adapter = subject.opened()
    response = translate(adapter, make_request())
    assert any(r.status == "failed" for r in response.results), "this check needs a failed line"
    assert response.usage is not None and response.usage.input_tokens > 0, (
        "tokens billed for a call with failed lines must still be reported in usage"
    )


def check_a_line_missing_from_the_reply_is_failed_missing(subject: Subject) -> None:
    adapter = subject.opened()
    request = make_request()
    response = translate(adapter, request)
    assert_valid_response(request, response, adapter.name)
    gone = response.results[2]
    assert gone.status == "failed" and gone.failure == "missing", (
        f"a line the provider left out must be failed/missing, got {gone.status}/{gone.failure}"
    )


CHECKS = (
    *COMMON_CHECKS,
    check_empty_request_returns_nothing,
    check_one_result_per_line_in_request_order,
    check_text_survives_unicode_exactly,
    check_big_batches_and_repeated_text_keep_every_line,
    check_usage_is_reported_honestly,
    check_glossary_context_and_low_quality_flag_are_accepted,
    check_translate_returns_within_the_time_limit,
    check_local_only_declaration_is_true_to_its_word,
)
WINDOWS_ONLY: frozenset = frozenset()
# These need an adapter that can be told to fail a line. Only the fakes can.
SCRIPTED_CHECKS = (
    (check_failed_line_is_returned_not_raised, {"fail_ids": ["L1"]}),
    (check_failed_lines_still_report_billed_tokens, {"fail_ids": ["L1"]}),
    (check_a_line_missing_from_the_reply_is_failed_missing, {"omit_ids": ["L2"]}),
)

# ---- fakes -------------------------------------------------------------------------------


class FakeTranslator:
    """Follows the translator contract. It 'bills' tokens, like a paid provider."""

    name = "fake_good"
    REQUIRES_CONTRACT = ">=1.0,<2.0"
    PROVIDER = "fake_good"

    def __init__(self, context: AdapterContext) -> None:
        settings = context.settings
        self._fail = set(settings.get("fail_ids", ()))
        self._omit = set(settings.get("omit_ids", ()))
        self._open = False

    def open(self) -> None:
        self._open = True

    def translate(self, request: TranslationRequest) -> TranslationResponse:
        if not request.items:
            return TranslationResponse(())
        results = tuple(self._line(item) for item in request.items)
        return TranslationResponse(results, self._usage(request))

    def _line(self, item: TranslationItem) -> TranslationResult:
        common: dict[str, Any] = {
            "line_id": item.line_id,
            "source_text": item.text,
            "provider": self.name,
            "model": "fake-model",
            "cached": False,
            "latency_ms": 12,
        }
        if item.line_id in self._omit:
            return TranslationResult(text="", status="failed", failure="missing", **common)
        if item.line_id in self._fail:
            return TranslationResult(text="", status="failed", failure="refused", **common)
        return TranslationResult(text=f"[en] {item.text}", **common)

    def _usage(self, request: TranslationRequest) -> TokenUsage:
        chars = sum(len(i.text) for i in request.items)
        return TokenUsage(self.name, "fake-model", 20 + chars, 5 + chars, 0, 3)

    def close(self) -> None:
        self._open = False

    def health(self) -> Health:
        return Health("ok" if self._open else "down", "")


class LocalFakeTranslator(FakeTranslator):
    """A translator that really stays on this PC, like null or quickmt."""

    name = "fake_local"
    LOCAL_ONLY = True


class LocalOnlyLiar(FakeTranslator):
    name = "fake_local_liar"
    LOCAL_ONLY = True

    def translate(self, request):
        try:
            socket.getaddrinfo("api.provider.test", 443)  # sends the text out, then hides it
        except OSError:
            pass
        return super().translate(request)


class InheritsLocalOnly(LocalFakeTranslator):
    """A cloud translator built on a local one, never saying so itself."""

    name = "fake_inherits_local"


class NonBoolLocalOnly(FakeTranslator):
    name = "fake_local_not_bool"
    LOCAL_ONLY = "yes"


def _swap(result: TranslationResult, **changes: Any) -> TranslationResult:
    values = {f: getattr(result, f) for f in TranslationResult.__dataclass_fields__}
    values.update(changes)
    return TranslationResult(**values)


class _Wrapping(FakeTranslator):
    """Base for fakes that change what FakeTranslator returns."""

    def translate(self, request: TranslationRequest) -> TranslationResponse:
        return self.alter(request, super().translate(request))

    def alter(self, request: TranslationRequest, response: TranslationResponse) -> Any:
        return response


class DropsLastLine(_Wrapping):
    name = "fake_drops_line"

    def alter(self, request, response):
        return TranslationResponse(response.results[:-1], response.usage)


class ReordersResults(_Wrapping):
    name = "fake_reorders"

    def alter(self, request, response):
        return TranslationResponse(tuple(reversed(response.results)), response.usage)


class NormalisesSourceText(_Wrapping):
    name = "fake_normalises_source"

    def alter(self, request, response):
        fixed = tuple(
            _swap(r, source_text=unicodedata.normalize("NFKC", r.source_text).strip())
            for r in response.results
        )
        return TranslationResponse(fixed, response.usage)


class BillsEmptyRequests(_Wrapping):
    name = "fake_bills_empty"

    def alter(self, request, response):
        if not request.items:
            return TranslationResponse((), TokenUsage(self.name, "fake-model", 40, 0))
        return response


class NegativeTokens(_Wrapping):
    name = "fake_negative_tokens"

    def alter(self, request, response):
        return TranslationResponse(response.results, TokenUsage(self.name, "fake-model", 5, -1))


class FloatTokens(_Wrapping):
    name = "fake_float_tokens"

    def alter(self, request, response):
        usage = TokenUsage(self.name, "fake-model", 5.5, 2)  # type: ignore[arg-type]
        return TranslationResponse(response.results, usage)


class WrongUsageProvider(_Wrapping):
    name = "fake_wrong_usage_provider"

    def alter(self, request, response):
        usage = TokenUsage("someone_else", "fake-model", 5, 5)
        return TranslationResponse(response.results, usage)


class ZeroTokensForRealWork(_Wrapping):
    name = "fake_zero_tokens"

    def alter(self, request, response):
        return TranslationResponse(response.results, TokenUsage(self.name, "fake-model", 0, 0))


class DropsUsageOnFailure(_Wrapping):
    name = "fake_drops_usage_on_failure"

    def alter(self, request, response):
        if any(r.status == "failed" for r in response.results):
            return TranslationResponse(response.results, None)
        return response


class RaisesOnRefusal(FakeTranslator):
    name = "fake_raises_on_refusal"

    def _line(self, item):
        if item.line_id in self._fail:
            raise PermanentError("the provider refused a line")
        return super()._line(item)


class SilentlyDropsMissing(_Wrapping):
    name = "fake_drops_missing"

    def alter(self, request, response):
        kept = tuple(r for r in response.results if r.failure != "missing")
        return TranslationResponse(kept, response.usage)


class MarksCached(_Wrapping):
    name = "fake_marks_cached"

    def alter(self, request, response):
        return TranslationResponse(
            tuple(_swap(r, cached=True) for r in response.results), response.usage
        )


class RewritesPassthrough(_Wrapping):
    name = "fake_rewrites_passthrough"

    def alter(self, request, response):
        fixed = tuple(
            _swap(r, status="passthrough", text=r.source_text.upper()) for r in response.results
        )
        return TranslationResponse(fixed, None)


class EmptyTranslation(_Wrapping):
    name = "fake_empty_translation"

    def alter(self, request, response):
        fixed = tuple(_swap(r, text="  ") for r in response.results)
        return TranslationResponse(fixed, response.usage)


class WrongProviderName(_Wrapping):
    name = "fake_wrong_provider_name"

    def alter(self, request, response):
        fixed = tuple(_swap(r, provider="other") for r in response.results)
        return TranslationResponse(fixed, response.usage)


class ListNotTuple(_Wrapping):
    name = "fake_list_results"

    def alter(self, request, response):
        return TranslationResponse(list(response.results), response.usage)  # type: ignore[arg-type]


class ReturnsBareList(FakeTranslator):
    name = "fake_bare_list"

    def translate(self, request):
        return list(super().translate(request).results)


class RaisesTimeoutError(FakeTranslator):
    name = "fake_raw_timeout"

    def translate(self, request):
        if request.items:
            raise TimeoutError("read timed out")
        return super().translate(request)


class RaisesOnSourceLanguage(FakeTranslator):
    name = "fake_crashes_on_lang"

    def translate(self, request):
        if request.source_lang == "ja":
            raise KeyError("ja")
        return super().translate(request)


class SlowTranslator(FakeTranslator):
    name = "fake_slow"

    def translate(self, request):
        if request.items:
            time.sleep(FAKE_TIMEOUT_S + 0.2)
        return super().translate(request)


class CrashesOnGlossary(FakeTranslator):
    name = "fake_crashes_on_glossary"

    def translate(self, request):
        if request.glossary.entries:
            raise ValueError(request.glossary.entries[0].note.upper())
        return super().translate(request)


class DuplicateTextCollapses(_Wrapping):
    name = "fake_dedupes_text"

    def alter(self, request, response):
        seen: set[str] = set()
        kept = []
        for r in response.results:
            if r.source_text not in seen:
                seen.add(r.source_text)
                kept.append(r)
        return TranslationResponse(tuple(kept), response.usage)


class TransientOnOpenCrash(FakeTranslator):
    name = "fake_open_crash"

    def open(self) -> None:
        raise ConnectionError("dns failure")


class CloseTwiceTranslator(FakeTranslator):
    name = "fake_close_twice"

    def close(self) -> None:
        if not self._open:
            raise RuntimeError("not open")
        super().close()


class SlowHealthTranslator(FakeTranslator):
    name = "fake_slow_health"

    def health(self) -> Health:
        time.sleep(0.2)
        return super().health()


def fake_subject(
    adapter: type = FakeTranslator,
    *,
    via_registry: bool = True,
    settings: dict[str, Any] | None = None,
) -> Subject:
    return registered_subject(
        PORT,
        adapter.name,
        adapter,
        via_registry=via_registry,
        base_settings=settings,
        scripted=True,
        timeout_s=FAKE_TIMEOUT_S,
        secrets=NoSecrets(),
    )


BROKEN = (
    Broken(DropsLastLine, check_one_result_per_line_in_request_order, "a line disappears"),
    Broken(ReordersResults, check_one_result_per_line_in_request_order, "order is reversed"),
    Broken(
        NormalisesSourceText,
        check_text_survives_unicode_exactly,
        "source_text is normalised instead of echoed",
    ),
    Broken(BillsEmptyRequests, check_empty_request_returns_nothing, "empty request is billed"),
    Broken(NegativeTokens, check_usage_is_reported_honestly, "negative token count"),
    Broken(FloatTokens, check_usage_is_reported_honestly, "fractional token count"),
    Broken(WrongUsageProvider, check_usage_is_reported_honestly, "usage names another provider"),
    Broken(ZeroTokensForRealWork, check_usage_is_reported_honestly, "zero tokens for real lines"),
    Broken(MarksCached, check_one_result_per_line_in_request_order, "adapter sets cached=True"),
    Broken(
        RewritesPassthrough,
        check_one_result_per_line_in_request_order,
        "passthrough text differs from the original",
    ),
    Broken(EmptyTranslation, check_one_result_per_line_in_request_order, "blank translation"),
    Broken(WrongProviderName, check_one_result_per_line_in_request_order, "provider mismatch"),
    Broken(ListNotTuple, check_one_result_per_line_in_request_order, "results is a list"),
    Broken(
        ReturnsBareList,
        check_one_result_per_line_in_request_order,
        "returns a bare list, not a TranslationResponse",
    ),
    Broken(
        RaisesTimeoutError,
        check_one_result_per_line_in_request_order,
        "raw TimeoutError instead of TransientError",
    ),
    Broken(
        RaisesOnSourceLanguage,
        check_glossary_context_and_low_quality_flag_are_accepted,
        "KeyError for source_lang='ja'",
    ),
    Broken(
        CrashesOnGlossary,
        check_glossary_context_and_low_quality_flag_are_accepted,
        "crashes on a glossary entry",
    ),
    Broken(
        LocalOnlyLiar,
        check_local_only_declaration_is_true_to_its_word,
        "claims LOCAL_ONLY but looks up a remote host",
    ),
    Broken(
        InheritsLocalOnly,
        check_local_only_declaration_is_true_to_its_word,
        "LOCAL_ONLY is inherited from a local base class, not declared",
    ),
    Broken(
        NonBoolLocalOnly,
        check_local_only_declaration_is_true_to_its_word,
        "LOCAL_ONLY is a string",
    ),
    Broken(SlowTranslator, check_translate_returns_within_the_time_limit, "ignores time limit"),
    Broken(
        DuplicateTextCollapses,
        check_big_batches_and_repeated_text_keep_every_line,
        "identical texts are merged into one result",
    ),
    Broken(TransientOnOpenCrash, check_close_is_always_safe, "ConnectionError from open()"),
    Broken(CloseTwiceTranslator, check_close_is_always_safe, "close() without open() raises"),
    Broken(SlowHealthTranslator, check_health_is_instant_in_every_state, "health() blocks"),
)

# Broken fakes for the scripted checks: (case, settings that make the fault happen).
SCRIPTED_BROKEN = (
    (
        Broken(
            RaisesOnRefusal,
            check_failed_line_is_returned_not_raised,
            "a refused line raises instead of coming back failed",
        ),
        {"fail_ids": ["L1"]},
    ),
    (
        Broken(
            DropsUsageOnFailure,
            check_failed_lines_still_report_billed_tokens,
            "usage is dropped when a line failed",
        ),
        {"fail_ids": ["L1"]},
    ),
    (
        Broken(
            SilentlyDropsMissing,
            check_a_line_missing_from_the_reply_is_failed_missing,
            "a line missing from the reply vanishes",
        ),
        {"omit_ids": ["L2"]},
    ),
)

BROKEN = (*BROKEN, *identity_breakers(FakeTranslator), *hostile_settings_breakers(FakeTranslator))
