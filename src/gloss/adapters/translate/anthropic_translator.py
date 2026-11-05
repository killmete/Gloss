"""Anthropic translator: one structured-output call per batch, with a cached system prompt.

Follows ADR-0001 section 5. Everything tunable comes from ``[translate.adapters.anthropic]``
(model, thinking, effort, token limits, cache TTL) plus the shared ``timeout_ms``. Nothing
about the provider is hard-coded here. The API key comes from the SecretStore, once, in
``open``.

What one call looks like
- ``system``: the style rules and the glossary, as one block marked for prompt caching. It
  changes only when the glossary or target language does, so it is a stable cache prefix.
- ``messages``: one user message, a JSON object with ``lines`` (``[{i, t}]``), the recent
  ``context`` (``[{t, en}]``) and an optional ``source_lang`` hint. Context sits after the
  cached prefix, so it never breaks caching.
- ``output_config``: a JSON schema (``{translations: [{i, en}]}``) and the effort level.
  ``thinking`` is switched off unless config says otherwise.
- The SDK's own retries are off (``max_retries=0``). Retrying lives in the TranslationService.

Failure model (``ports.py``): a problem with some lines comes back as per-line ``failed``
results, and the billed tokens are still reported. A refusal or truncation affects the whole
call, so every line of that call is marked failed, and the service splits the batch. Only
whole-call problems (network, rate limit, bad key, no credit) raise.

Error messages are fixed strings. They never include the key, the request or the reply.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gloss.core.contracts import (
    AdapterContext,
    AdapterUnavailable,
    FailureReason,
    Glossary,
    Health,
    PermanentError,
    TokenUsage,
    TransientError,
    TranslationRequest,
    TranslationResponse,
    TranslationResult,
)
from gloss.core.registry import register_adapter

log = logging.getLogger(__name__)

PROMPT_FILE = Path(__file__).parent / "prompts" / "system.txt"
THINKING_MODES = ("disabled", "adaptive", "default")
CACHE_TTLS = ("5m", "1h")

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "en": {"type": "string"}},
                "required": ["i", "en"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


def _number(settings: Mapping[str, Any], key: str, *, minimum: float) -> float:
    value = settings.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < minimum:
        raise PermanentError(f"Anthropic setting '{key}' must be a number of at least {minimum}")
    return value


def _text(settings: Mapping[str, Any], key: str) -> str:
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PermanentError(f"Anthropic setting '{key}' must be non-empty text")
    return value


def render_glossary(glossary: Glossary) -> str:
    if not glossary.entries:
        return "(none)"
    lines = []
    for entry in glossary.entries:
        note = f" ({entry.note})" if entry.note else ""
        lines.append(f"{entry.term} => {entry.translation}{note}")
    return "\n".join(lines)


def render_system(template: str, target_lang: str, glossary: Glossary) -> str:
    return template.replace("{{target_lang}}", target_lang).replace(
        "{{glossary}}", render_glossary(glossary)
    )


def build_user_payload(request: TranslationRequest) -> str:
    """The user message: lines numbered 1..n by position, then context, then the lang hint."""
    lines = []
    for position, item in enumerate(request.items, start=1):
        entry: dict[str, Any] = {"i": position, "t": item.text}
        if item.low_quality:
            entry["q"] = True
        lines.append(entry)
    payload: dict[str, Any] = {"lines": lines}
    if request.context:
        payload["context"] = [
            {"t": c.source_text, "en": c.translated_text} for c in request.context
        ]
    if request.source_lang:
        payload["source_lang"] = request.source_lang
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_translations(text: str) -> dict[int, str] | None:
    """Return ``{position: translation}`` from the reply, or None if it is not usable JSON.

    An id seen twice keeps its first value. Entries of the wrong shape are skipped.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    items = data.get("translations") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    found: dict[int, str] = {}
    for entry in items:
        if not isinstance(entry, dict):
            continue
        ident, english = entry.get("i"), entry.get("en")
        if isinstance(ident, bool) or not isinstance(ident, int) or not isinstance(english, str):
            continue
        found.setdefault(ident, english)
    return found


@register_adapter("translate", "anthropic")
class AnthropicTranslator:
    name = "anthropic"
    REQUIRES_CONTRACT = ">=1.0,<2.0"
    # Bump when prompts/system.txt changes meaning, so cached translations made under the old
    # rules are not reused (the service adds it to the cache key).
    PROMPT_VERSION = 2

    def __init__(self, context: AdapterContext) -> None:
        s = context.settings
        self._model = _text(s, "model")
        self._thinking = _text(s, "thinking")
        if self._thinking not in THINKING_MODES:
            raise PermanentError(f"Anthropic setting 'thinking' must be one of {THINKING_MODES}")
        self._effort = _text(s, "effort")
        self._tokens_base = int(_number(s, "max_tokens_base", minimum=0))
        self._tokens_per_line = int(_number(s, "max_tokens_per_line", minimum=0))
        if self._tokens_base + self._tokens_per_line < 1:
            raise PermanentError("Anthropic token limits must not both be zero")
        self._cache_ttl = _text(s, "cache_ttl")
        if self._cache_ttl not in CACHE_TTLS:
            raise PermanentError(f"Anthropic setting 'cache_ttl' must be one of {CACHE_TTLS}")
        self._timeout_s = _number(s, "timeout_ms", minimum=1) / 1000
        self._secret_name = _text(s, "secret_name")
        base_url = s.get("base_url")
        if base_url is not None and (not isinstance(base_url, str) or not base_url.strip()):
            raise PermanentError("Anthropic setting 'base_url' must be text or left out")
        self._base_url = base_url or None
        self._secrets = context.secrets
        self._client: Any = None
        self._template: str | None = None
        self._system_cache: tuple[tuple[str, str], str] | None = None
        self._thinking_param: dict[str, str] | None = self._thinking_request(self._thinking)
        self._health = Health("down", "Not started")

    @property
    def model(self) -> str:
        """The configured model. The TranslationService puts it in the cache key."""
        return self._model

    # ---- lifecycle ----------------------------------------------------------------------

    def open(self) -> None:
        self._client = None
        if self._secrets is None:
            self._health = Health("down", "No key store is available")
            raise AdapterUnavailable("No key store is available for the Anthropic API key")
        key = self._secrets.get(self._secret_name)
        if not key or not key.strip():
            self._health = Health("down", "No API key. Add one in settings")
            raise AdapterUnavailable("No Anthropic API key is stored. Add one in settings.")
        if any(ch.isspace() for ch in key.strip()) or not key.isascii():
            self._health = Health("down", "The stored API key looks wrong")
            raise PermanentError("The stored API key looks wrong. Enter it again in settings.")
        client = self._make_client(key)
        del key
        self._load_template()
        self._check_model(client)
        self._client = client
        self._health = Health("ok", "Ready")

    def close(self) -> None:
        client, self._client = self._client, None
        closer = getattr(client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # Closing must never fail the app.
                log.debug("Closing the Anthropic client failed", exc_info=False)
        self._health = Health("down", "Not started")

    def health(self) -> Health:
        return self._health

    def _make_client(self, api_key: str) -> Any:
        """Build the SDK client. Retries are off: retrying is the service's job."""
        try:
            import anthropic
        except ImportError:
            raise AdapterUnavailable("The 'anthropic' package is not installed") from None
        options: dict[str, Any] = {"api_key": api_key, "max_retries": 0, "timeout": self._timeout_s}
        if self._base_url:
            options["base_url"] = self._base_url
        return anthropic.Anthropic(**options)

    def _load_template(self) -> None:
        if self._template is None:
            try:
                self._template = PROMPT_FILE.read_text(encoding="utf-8")
            except OSError:
                raise AdapterUnavailable("The translation prompt file is missing") from None

    def _check_model(self, client: Any) -> None:
        """Look the model up once at start. This doubles as the key check (no text, no cost).

        A key or model problem raises ``PermanentError``; being unable to reach the API raises
        ``AdapterUnavailable``. Both messages are plain English and safe to show.
        """
        try:
            info = client.models.retrieve(self._model)
        except Exception as exc:
            problem = self._classify(exc)
            if isinstance(problem, PermanentError):
                self._health = Health("down", str(problem))
                raise problem from None
            self._health = Health("down", "Could not reach the Anthropic API")
            log.info("Model check could not reach the API (%s)", type(exc).__name__)
            raise AdapterUnavailable(
                "Could not reach the Anthropic API. Check your internet and retry."
            ) from None
        disabled = self._capability(info, "thinking", "types", "disabled", "supported")
        if self._thinking == "disabled" and disabled is False:
            log.warning("The configured model cannot switch thinking off; leaving it at default")
            self._thinking_param = None

    @staticmethod
    def _thinking_request(mode: str) -> dict[str, str] | None:
        return {"disabled": {"type": "disabled"}, "adaptive": {"type": "adaptive"}}.get(mode)

    @staticmethod
    def _capability(info: Any, *path: str) -> Any:
        node = getattr(info, "capabilities", None)
        for part in path:
            node = getattr(node, part, None)
        return node

    # ---- translating --------------------------------------------------------------------

    def translate(self, request: TranslationRequest) -> TranslationResponse:
        if not request.items:
            return TranslationResponse(())
        if self._client is None or self._template is None:
            raise AdapterUnavailable("The Anthropic translator is not open")
        arguments = self._arguments(request)
        started = time.perf_counter()
        try:
            reply = self._client.messages.create(**arguments)
        except Exception as exc:
            problem = self._classify(exc)
            self._health = (
                Health("degraded", str(problem))
                if isinstance(problem, TransientError)
                else Health("down", str(problem))
            )
            raise problem from None
        latency_ms = int((time.perf_counter() - started) * 1000)
        response = self._interpret(request, reply, latency_ms)
        self._health = Health("ok", "Ready")
        return response

    def _arguments(self, request: TranslationRequest) -> dict[str, Any]:
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}
        }
        if self._effort != "default":
            output_config["effort"] = self._effort
        arguments: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._tokens_base + self._tokens_per_line * len(request.items),
            "system": [self._system_block(request)],
            "messages": [{"role": "user", "content": build_user_payload(request)}],
            "output_config": output_config,
        }
        if self._thinking_param is not None:
            arguments["thinking"] = self._thinking_param
        return arguments

    def _system_block(self, request: TranslationRequest) -> dict[str, Any]:
        stamp = (request.glossary.version, request.target_lang)
        if self._system_cache is None or self._system_cache[0] != stamp:
            assert self._template is not None
            text = render_system(self._template, request.target_lang, request.glossary)
            self._system_cache = (stamp, text)
        control: dict[str, str] = {"type": "ephemeral"}
        if self._cache_ttl != "5m":
            control["ttl"] = self._cache_ttl
        return {"type": "text", "text": self._system_cache[1], "cache_control": control}

    def _interpret(
        self, request: TranslationRequest, reply: Any, latency_ms: int
    ) -> TranslationResponse:
        usage = self._usage(reply)
        stop = getattr(reply, "stop_reason", None)
        translations: dict[int, str] | None = None
        refusal: FailureReason | None = "refused" if stop == "refusal" else None
        if refusal is None:
            blocks = getattr(reply, "content", None) or []
            text = "".join(b.text for b in blocks if getattr(b, "type", None) == "text")
            translations = parse_translations(text)
        truncated = stop == "max_tokens"
        results = []
        for position, item in enumerate(request.items, start=1):
            failure: FailureReason | None = refusal
            english = ""
            if failure is None:
                if translations is None:
                    failure = "truncated" if truncated else "invalid_output"
                elif position not in translations:
                    failure = "truncated" if truncated else "missing"
                elif not translations[position].strip():
                    failure = "invalid_output"
                else:
                    english = translations[position]
            results.append(
                TranslationResult(
                    line_id=item.line_id,
                    source_text=item.text,
                    text=english,
                    provider=self.name,
                    model=self._model,
                    cached=False,
                    latency_ms=latency_ms,
                    status="failed" if failure else "ok",
                    failure=failure,
                )
            )
        if any(r.status == "failed" for r in results):
            log.debug(
                "Anthropic returned %d failed line(s) of %d",
                sum(r.status == "failed" for r in results),
                len(results),
            )
        return TranslationResponse(tuple(results), usage)

    def _usage(self, reply: Any) -> TokenUsage:
        u = getattr(reply, "usage", None)

        def count(field: str) -> int:
            value = getattr(u, field, 0)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        return TokenUsage(
            provider=self.name,
            model=self._model,
            input_tokens=count("input_tokens"),
            output_tokens=count("output_tokens"),
            cache_read_tokens=count("cache_read_input_tokens"),
            cache_write_tokens=count("cache_creation_input_tokens"),
        )

    # ---- errors -------------------------------------------------------------------------

    def _classify(self, exc: Exception) -> TransientError | PermanentError:
        """Map an SDK exception to a Gloss error with a fixed message (no body, no key)."""
        status = getattr(exc, "status_code", None)
        body = _lowered_body(exc)
        name = type(exc).__name__
        if isinstance(status, int):
            if "spend_limit" in body or "spend limit" in body:
                return PermanentError(
                    "The Anthropic spend limit was reached. Raise it in the console."
                )
            if "credit balance" in body:
                return PermanentError(
                    "The Anthropic account has no credit. Add credit in the console."
                )
            if status == 401:
                return PermanentError("The Anthropic API key was rejected. Enter it again.")
            if status == 403:
                return PermanentError("The Anthropic API key is not allowed to do this.")
            if status == 404:
                return PermanentError(
                    "The configured model was not found. Check translate settings."
                )
            if status in (400, 413, 422):
                return PermanentError(f"The Anthropic API rejected the request (HTTP {status}).")
            return TransientError(
                f"Anthropic API error (HTTP {status})", retry_after_s=_retry_after(exc)
            )
        log.info("Anthropic call failed (%s)", name)
        return TransientError("Could not reach the Anthropic API")


def _lowered_body(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    try:
        return json.dumps(body, default=str).lower() if body is not None else ""
    except (TypeError, ValueError):
        return ""


def _retry_after(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        value = headers.get("retry-after") if headers is not None else None
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return seconds if seconds is not None and seconds >= 0 else None
