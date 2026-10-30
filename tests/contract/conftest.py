"""Runs each port's checks against every adapter registered for that port.

A test module sets ``PORT`` and ``CHECKS`` (and optionally ``WINDOWS_ONLY``). Parametrisation
happens at collection, from ``Registry.names(PORT)`` after ``discover()``. With no adapter
registered, each module gets one skipped test that says why, and the fakes still run.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from testkit.core import Subject
from testkit.discovery import registered_names


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.path)).parents:
            item.add_marker(pytest.mark.contract)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "real_case" not in metafunc.fixturenames:
        return
    port = metafunc.module.PORT
    windows_only = getattr(metafunc.module, "WINDOWS_ONLY", frozenset())
    names = registered_names(port)
    if not names:
        reason = (
            f"no {port} adapter is registered yet, so there is nothing real to check. "
            "The fake-based tests in this file still run."
        )
        param = pytest.param(
            None, id="no-adapter-registered", marks=pytest.mark.skip(reason=reason)
        )
        metafunc.parametrize("real_case", [param])
        return
    params = []
    for name in names:
        for check in metafunc.module.CHECKS:
            marks = [pytest.mark.windows_only] if check in windows_only else []
            check_id = check.__name__.removeprefix("check_")
            params.append(pytest.param((name, check), id=f"{name}-{check_id}", marks=marks))
    metafunc.parametrize("real_case", params)


@pytest.fixture(scope="module")
def real_subjects() -> Iterator[dict[tuple[str, str], Subject]]:
    cache: dict[tuple[str, str], Subject] = {}
    yield cache
    for subject in cache.values():
        subject.release()
