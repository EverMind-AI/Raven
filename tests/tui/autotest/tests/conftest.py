"""pytest fixtures + marker registration for tui-autotest framework + dogfood tests.

`harness` fixture (scope=function): yields a fresh Harness; teardown calls
kill() idempotently. Per specs/tui-autotest.md §S3.1.

`e2e` marker: tests that spawn real subprocesses (slow, network/LLM-dependent).
Run only the unit tests with: `uv run pytest -m "not e2e"`.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from tests.tui.autotest.runner import Harness


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "e2e: marks tests that spawn real subprocesses via tui-use (slow, may need network/LLM)",
    )


@pytest.fixture
def harness() -> Iterator[Harness]:
    h = Harness(cols=120, rows=40)
    try:
        yield h
    finally:
        # idempotent — no raise if subprocess already exited or never spawned
        h.kill()
