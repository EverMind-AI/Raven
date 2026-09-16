"""The three hosted backends pass the contract every memory plugin does.

Each subclass runs the shared ``MemoryBackendContractTests`` and
``LifecycleContractTests`` against its fake service; nothing is overridden,
so a backend that answers the agent track with a request, raises on a 404
delete, or cannot ``start`` twice fails here before it fails a user.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from raven.memory_engine import LifecycleContractTests, MemoryBackendContractTests
from raven.plugins import PluginContext, ServiceLocator
from raven_cloud_memory.mem0 import Mem0Backend
from raven_cloud_memory.memos import MemosBackend
from raven_cloud_memory.zep import ZepBackend
from tests._cloud_memory_fakes import FakeCloud, FakeMem0, FakeMemos, FakeZep, client_for

CONTRACT_USER = "contract-test"


def _ctx(tmp_path: Path, fake: FakeCloud) -> PluginContext:
    return PluginContext(
        config={"api_key": fake.KEY},
        services=ServiceLocator(workspace=tmp_path, user_id=CONTRACT_USER, agent_id="agent"),
        logger=logging.getLogger("test.cloud"),
    )


@pytest.fixture(autouse=True)
def _no_env_keys(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    for var in ("MEM0_API_KEY", "ZEP_API_KEY", "MEMOS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    request.instance.tmp_path = tmp_path


class _Padded:
    """A fake with more hits than any ``top_k`` the contract asks for, and one
    memory already there so recall has something to return."""

    fake_cls: type[FakeCloud]
    backend_cls: type

    def make_fake(self) -> FakeCloud:
        fake = self.fake_cls()
        fake.extra_hits = 10
        fake.seed(CONTRACT_USER, "the user likes contract tests")
        return fake

    async def make_backend(self):
        fake = self.make_fake()
        return self.backend_cls(_ctx(self.tmp_path, fake), client=client_for(fake))


class TestMem0Contract(_Padded, MemoryBackendContractTests):
    fake_cls, backend_cls = FakeMem0, Mem0Backend


class TestMem0Lifecycle(_Padded, LifecycleContractTests):
    fake_cls, backend_cls = FakeMem0, Mem0Backend


class TestZepContract(_Padded, MemoryBackendContractTests):
    fake_cls, backend_cls = FakeZep, ZepBackend


class TestZepLifecycle(_Padded, LifecycleContractTests):
    fake_cls, backend_cls = FakeZep, ZepBackend


class TestMemosContract(_Padded, MemoryBackendContractTests):
    fake_cls, backend_cls = FakeMemos, MemosBackend


class TestMemosLifecycle(_Padded, LifecycleContractTests):
    fake_cls, backend_cls = FakeMemos, MemosBackend
