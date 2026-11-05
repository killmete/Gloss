"""Harness for the failure drills: the real Pipeline and the real translation service, with
fakes only at the edges (the network, the screen, the OCR engine, the key store).

Nothing here touches the network, the real keyring or the user's %APPDATA%.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from gloss.adapters.translate.anthropic_translator import AnthropicTranslator
from gloss.adapters.translate.null_translator import NullTranslator
from gloss.core.contracts import CaptureTarget, Rect
from gloss.core.pipeline import Pipeline
from gloss.core.registry import Registry
from testkit.pipeline import (
    CaptureScript,
    OcrScript,
    RecordingSink,
    block,
    fast_config,
    solid,
    wait_until,
    with_patch,
)

SECRET = "sk-drill-0123456789abcdefghij-NOT-A-REAL-KEY"
LINE1 = "你好吗朋友"
LINE2 = "今天天气很好"
NEW = "新来的消息内容"
NEWER = "又来了一条消息"


class Network:
    """The pretend internet. ``mode`` is ok, offline, bad_key, hang (translate calls
    stall) or hang_open (the start-up model check stalls)."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.calls = 0
        self.gate = threading.Event()  # a hung call waits here

    def go(self, mode: str) -> None:
        self.mode = mode

    def _check(self, call: str) -> None:
        self.calls += 1
        if self.mode == "offline":
            raise ConnectionError("no route to host")  # what the SDK raises: no status code
        if self.mode == "bad_key":
            error = Exception("HTTP 401")
            error.status_code = 401  # type: ignore[attr-defined]
            error.body = {"error": {"message": "invalid x-api-key"}}  # type: ignore[attr-defined]
            error.response = SimpleNamespace(headers={})  # type: ignore[attr-defined]
            raise error
        if self.mode == "hang" and call == "translate":  # the key check passes, then it stalls
            self.gate.wait(30)
        if self.mode == "hang_open" and call == "model_check":
            self.gate.wait(30)

    def client(self) -> Any:
        net = self

        def create(**kwargs: Any) -> Any:
            net._check("translate")
            payload = json.loads(kwargs["messages"][0]["content"])
            rows = [{"i": ln["i"], "en": f"EN:{ln['t']}"} for ln in payload["lines"]]
            text = json.dumps({"translations": rows}, ensure_ascii=False)
            usage = SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            )
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                stop_reason="end_turn",
                usage=usage,
            )

        def retrieve(model: str) -> Any:
            net._check("model_check")
            return SimpleNamespace(capabilities=None)

        return SimpleNamespace(
            messages=SimpleNamespace(create=create),
            models=SimpleNamespace(retrieve=retrieve),
            close=lambda: None,
        )


class Keys:
    """A key store holding one fake key."""

    def __init__(self, value: str | None = SECRET) -> None:
        self.value = value

    def get(self, name: str) -> str | None:
        return self.value

    def set(self, name: str, value: str) -> None:
        self.value = value

    def delete(self, name: str) -> None:
        self.value = None


class Drill:
    """A running pipeline: fake capture, fake OCR (unless ``ocr_class`` is given), the real
    Anthropic adapter on a fake network, the real null translator and the real service."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: Any,
        *,
        timeout_ms: int = 400,
        cooldown_s: float = 0.5,
        ocr_class: type | None = None,
        ocr_name: str = "ocr",
        image: Any = None,
    ) -> None:
        import gloss.core.config as config_module

        # The translation cache and spend ledger go to a private folder, never to %APPDATA%.
        monkeypatch.setattr(config_module, "user_config_path", lambda: tmp_path / "config.toml")
        base = fast_config(
            provider="anthropic",
            translate_fallbacks=("null",),
            ocr=(ocr_name,),
            timeout_ms=timeout_ms,
            translate_timeout_ms=timeout_ms,
            fps=60.0,
        )
        self.config = base.model_copy(
            update={
                "reliability": base.reliability.model_copy(
                    update={"breaker_cooldown_s": cooldown_s}
                )
            }
        )
        self.net = Network()
        self.keys = Keys()
        self.sink = RecordingSink()
        self.registry = Registry()
        self.cap = CaptureScript("cap", solid(300, 240) if image is None else image)
        self.cap.register(self.registry)
        self.ocr = OcrScript("ocr", [block(LINE1, y=10), block(LINE2, y=60)])
        if ocr_class is None:
            self.ocr.register(self.registry)
        else:
            assert self.registry.register("ocr", ocr_name, ocr_class)
        net = self.net

        class FakeNetAnthropic(AnthropicTranslator):
            def _make_client(self, api_key: str) -> Any:
                return net.client()

        assert self.registry.register("translate", "anthropic", FakeNetAnthropic)
        assert self.registry.register("translate", "null", NullTranslator)
        from gloss.app import make_translation_factory

        self.pipeline = Pipeline(
            self.config,
            registry=self.registry,
            sink=self.sink,
            make_translation=make_translation_factory(self.config, self.registry, self.keys),
            secrets=self.keys,
            target=CaptureTarget("region", region=Rect(100, 50, 200, 120)),
        )
        self._flips = 0

    def start(self) -> Pipeline:
        self.pipeline.start()
        return self.pipeline

    def close(self) -> None:
        self.net.gate.set()
        self.cap.release()
        self.pipeline.stop(timeout_s=1.0)

    # ---- what the user would see ----------------------------------------------------------

    def alive(self) -> bool:
        """Every stage has a running worker (a restarted one counts)."""
        return bool(
            wait_until(
                lambda: all(s.thread.is_alive() for s in self.pipeline._stages.values()),
                3.0,
                "every worker to be running",
            )
        )

    def dot(self) -> str | None:
        """The state the status dot was last told about."""
        health = self.sink.last_health()
        return health.state if health else None

    def status(self) -> tuple[str, str]:
        h = self.pipeline.health()
        return h.state, h.detail

    def shown(self, status: str, text: str) -> list[Any]:
        return [i for i in self.sink.shown(status) if i.result.source_text == text]

    def wait_shown(self, status: str, text: str, timeout: float = 8.0) -> Any:
        return wait_until(lambda: self.shown(status, text), timeout, f"{text!r} shown as {status}")[
            -1
        ]

    def wait_state(self, state: str, timeout: float = 8.0) -> str:
        wait_until(lambda: self.status()[0] == state, timeout, f"status {state}")
        return self.status()[1]

    def new_line(self, text: str, y: int) -> None:
        """A new line appears on the screen."""
        self.ocr.blocks.append(block(text, y=y))
        self._flips += 1
        self.cap.image = with_patch(self.cap.image, x=10 + self._flips % 100)
