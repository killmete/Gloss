"""Pipeline unit tests wait on idle workers; shorten the idle poll so teardown is quick."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def quick_idle_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gloss.core.pipeline._IDLE_WAIT_S", 0.05)
