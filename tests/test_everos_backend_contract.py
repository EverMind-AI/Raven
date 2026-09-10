"""The everos backend must pass the same contract every memory plugin does."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.memory_engine import LifecycleContractTests, MemoryBackendContractTests
from raven_everos.backend import EverosBackend
from tests.test_everos_backend import _backend, _FakeAdapter


@pytest.fixture(autouse=True)
def _workspace(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    request.instance.workspace = tmp_path


def _episodes_plus_profile(n: int) -> SimpleNamespace:
    return SimpleNamespace(
        episodes=[SimpleNamespace(id=f"e{i}", summary=f"fact {i}", score=0.5) for i in range(n)],
        profiles=[SimpleNamespace(id="p1", profile_data={"role": "dev"}, score=None)],
        agent_skills=[],
        agent_cases=[],
    )


class TestEverosBackendContract(MemoryBackendContractTests):
    async def make_backend(self) -> EverosBackend:
        # More episodes than any top_k the contract asks for, plus the profile
        # row the adapter appends outside the server's top_k.
        return _backend(self.workspace, adapter=_FakeAdapter(search_response=_episodes_plus_profile(10)))


class TestEverosBackendLifecycle(LifecycleContractTests):
    async def make_backend(self) -> EverosBackend:
        return _backend(self.workspace, adapter=_FakeAdapter())
