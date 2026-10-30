"""Rules for the whole test run."""

from __future__ import annotations

import socket
import sys

import pytest

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never use the network. Only loopback connections (event loops) are allowed."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def refuse_if_remote(address: object) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            raise RuntimeError(f"network access is blocked in tests (tried {host!r})")

    def connect(self: socket.socket, address: object) -> None:
        refuse_if_remote(address)
        return real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        refuse_if_remote(address)
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if sys.platform == "win32":
        return
    skip = pytest.mark.skip(reason="needs Windows (marked windows_only)")
    for item in items:
        if "windows_only" in item.keywords:
            item.add_marker(skip)
