"""The cloud backend must pass the same contract every memory plugin does."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from raven.memory_engine import LifecycleContractTests, MemoryBackendContractTests
from raven.plugins import PluginContext, ServiceLocator
from raven_everos_cloud.backend import EverosCloudBackend
from tests._everos_cloud_fake import FakeCloud


@pytest.fixture(autouse=True)
def _workspace(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    request.instance.workspace = tmp_path


def _build(workspace: Path) -> EverosCloudBackend:
    ctx = PluginContext(
        config={"api_key": "contract-key", "base_url": "http://fake"},
        services=ServiceLocator(workspace=workspace, user_id="contract-test", agent_id="contract-agent"),
        logger=logging.getLogger("contract"),
    )
    return EverosCloudBackend(ctx, client=httpx.AsyncClient(transport=FakeCloud().transport()))


class TestEverosCloudBackendContract(MemoryBackendContractTests):
    workspace: Path

    async def make_backend(self) -> EverosCloudBackend:
        return _build(self.workspace)


class TestEverosCloudBackendLifecycle(LifecycleContractTests):
    workspace: Path

    async def make_backend(self) -> EverosCloudBackend:
        return _build(self.workspace)
