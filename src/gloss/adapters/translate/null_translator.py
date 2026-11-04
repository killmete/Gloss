"""The ``null`` translator: shows the original text. The chain's last resort, and the offline mode.

Every line comes back as ``passthrough`` with its own text. Nothing is sent anywhere, so it is
the one translator that is always allowed when ``privacy.cloud_translation`` is off.
"""

from __future__ import annotations

from gloss.core.contracts import (
    AdapterContext,
    Health,
    TranslationRequest,
    TranslationResponse,
    TranslationResult,
)
from gloss.core.registry import register_adapter


@register_adapter("translate", "null")
class NullTranslator:
    name = "null"
    REQUIRES_CONTRACT = ">=1.0,<2.0"
    LOCAL_ONLY = True  # Declared on this class itself, as the registry requires.

    def __init__(self, context: AdapterContext) -> None:
        self._open = False

    def open(self) -> None:
        self._open = True

    def translate(self, request: TranslationRequest) -> TranslationResponse:
        results = tuple(
            TranslationResult(
                line_id=item.line_id,
                source_text=item.text,
                text=item.text,
                provider=self.name,
                model=None,
                cached=False,
                latency_ms=0,
                status="passthrough",
            )
            for item in request.items
        )
        return TranslationResponse(results)

    def close(self) -> None:
        self._open = False

    def health(self) -> Health:
        if self._open:
            return Health("degraded", "Showing the original text. Nothing is translated")
        return Health("down", "Not started")
