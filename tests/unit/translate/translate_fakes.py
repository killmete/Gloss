"""Fakes for the translation tests. Nothing here touches the network or the real keyring."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from gloss.core.contracts import (
    Health,
    TokenUsage,
    TranslationRequest,
    TranslationResponse,
    TranslationResult,
)

SECRET = "sk-fake-0123456789abcdefghij-DO-NOT-LEAK"


def reply(
    translations: list[dict[str, Any]] | None = None,
    *,
    text: str | None = None,
    stop: str = "end_turn",
    usage: tuple[int, int, int, int] = (100, 20, 0, 0),
) -> SimpleNamespace:
    """A stand-in for an SDK Message."""
    if text is None:
        text = json.dumps({"translations": translations or []}, ensure_ascii=False)
    u = SimpleNamespace(
        input_tokens=usage[0],
        output_tokens=usage[1],
        cache_read_input_tokens=usage[2],
        cache_creation_input_tokens=usage[3],
    )
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block], stop_reason=stop, usage=u)


def echo(kwargs: dict[str, Any], omit: set[int] = frozenset()) -> SimpleNamespace:  # type: ignore[assignment]
    payload = json.loads(kwargs["messages"][0]["content"])
    rows = [
        {"i": ln["i"], "en": f"[en] {ln['t']}"} for ln in payload["lines"] if ln["i"] not in omit
    ]
    return reply(rows)


class FakeStatusError(Exception):
    """Looks like the SDK's APIStatusError to the adapter: status_code, body, response."""

    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.body = body
        self.response = SimpleNamespace(headers=headers or {})


class FakeClient:
    """Mimics the parts of ``anthropic.Anthropic`` the adapter uses, and records the calls."""

    def __init__(self, responder: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.responder = responder or echo
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.model_info: Any = SimpleNamespace(
            capabilities=SimpleNamespace(
                thinking=SimpleNamespace(
                    types=SimpleNamespace(disabled=SimpleNamespace(supported=True))
                )
            )
        )
        self.retrieve_error: Exception | None = None
        self.messages = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(retrieve=self._retrieve)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.responder(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    def _retrieve(self, model: str) -> Any:
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return self.model_info

    def close(self) -> None:
        self.closed = True


class KeySecrets:
    """A SecretStore that holds a fake key."""

    def __init__(self, value: str | None = SECRET) -> None:
        self.value = value

    def get(self, name: str) -> str | None:
        return self.value

    def set(self, name: str, value: str) -> None:
        self.value = value

    def delete(self, name: str) -> None:
        self.value = None


class _Scripted:
    """A translator whose behaviour each test scripts. ``behave(request, call_number)``
    returns a TranslationResponse or raises."""

    REQUIRES_CONTRACT = ">=1.0,<2.0"

    def __init__(
        self, name: str, behave: Callable[..., Any] | None = None, model: str | None = None
    ):
        self.name = name  # type: ignore[misc]
        self.model = model
        self.behave = behave or self.ok
        self.requests: list[TranslationRequest] = []
        self.opens = 0
        self.open_error: Exception | None = None
        self._health = Health("ok", "")

    def open(self) -> None:
        self.opens += 1
        if self.open_error is not None:
            raise self.open_error

    def close(self) -> None:
        pass

    def health(self) -> Health:
        return self._health

    def translate(self, request: TranslationRequest) -> TranslationResponse:
        self.requests.append(request)
        return self.behave(request, len(self.requests))

    def ok(self, request: TranslationRequest, call_number: int = 1) -> TranslationResponse:
        return self.respond(request)

    def respond(
        self, request: TranslationRequest, failed: dict[str, str] | None = None
    ) -> TranslationResponse:
        """Translate every line to ``[provider] text``, except ``failed`` (text -> reason)."""
        failed = failed or {}
        results = []
        for item in request.items:
            common: dict[str, Any] = {
                "line_id": item.line_id,
                "source_text": item.text,
                "provider": self.name,
                "model": self.model,
                "cached": False,
                "latency_ms": 5,
            }
            if item.text in failed:
                results.append(
                    TranslationResult(text="", status="failed", failure=failed[item.text], **common)
                )
            elif self.LOCAL_PASSTHROUGH:
                results.append(TranslationResult(text=item.text, status="passthrough", **common))
            else:
                results.append(TranslationResult(text=f"[{self.name}] {item.text}", **common))
        usage = None
        if not self.LOCAL_PASSTHROUGH:
            usage = TokenUsage(self.name, self.model or "m", 1_000_000, 0)
        return TranslationResponse(tuple(results), usage)

    LOCAL_PASSTHROUGH = False


class CloudScripted(_Scripted):
    """No LOCAL_ONLY, so the service treats it as paid."""


class LocalScripted(_Scripted):
    LOCAL_ONLY = True
    LOCAL_PASSTHROUGH = True
