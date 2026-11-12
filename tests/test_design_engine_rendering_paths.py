"""Unit tests for rendering path containment and bundle publication."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven_design.rendering.backend import (
    SandboxRenderBackend,
    _copy_asset_tree,
    _outcome_from_wire,
    _posix_locale,
)
from raven_design.rendering.bundle import validate_published_bundle
from raven_design.rendering.models import RenderConfig, RenderError, RenderRequest
from raven_design.rendering.paths import RenderPathPolicy
from raven_design.rendering.worker import _read_request


def _policy(tmp_path: Path) -> RenderPathPolicy:
    workspace = tmp_path / "workspace"
    media = tmp_path / "media"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    media.mkdir()
    runtime.mkdir()
    return RenderPathPolicy(
        workspace=workspace,
        media_root=media,
        runtime_root=runtime,
        restrict_to_workspace=True,
    )


def test_relative_paths_resolve_inside_workspace(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    source = policy.workspace / "page.html"
    source.write_text("<html/>", encoding="utf-8")

    assert policy.resolve_source("page.html") == source
    assert policy.resolve_output_dir("output") == policy.workspace / "output"


def test_browser_locale_is_normalized_for_worker_processes() -> None:
    assert _posix_locale("en-US") == "en_US.UTF-8"
    assert _posix_locale("C.UTF-8") == "C.UTF-8"


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    outside = tmp_path / "outside.html"
    outside.write_text("<html/>", encoding="utf-8")
    link = policy.workspace / "escape.html"
    link.symlink_to(outside)

    with pytest.raises(RenderError) as raised:
        policy.resolve_source(link)

    assert raised.value.code == "path_not_allowed"


def test_external_output_and_broad_asset_root_are_rejected(tmp_path: Path) -> None:
    policy = _policy(tmp_path)

    with pytest.raises(RenderError, match="outside") as output_error:
        policy.resolve_output_dir(tmp_path / "external")
    with pytest.raises(RenderError, match="too broad") as asset_error:
        policy.resolve_asset_root(Path("/"))

    assert output_error.value.code == "path_not_allowed"
    assert asset_error.value.code == "path_not_allowed"


def test_internal_preview_output_may_use_runtime_root(tmp_path: Path) -> None:
    policy = _policy(tmp_path)

    output = policy.resolve_output_dir(policy.runtime_root / "preview", internal=True)

    assert output == policy.runtime_root / "preview"


def test_published_bundle_rejects_extra_entries_and_symlinks(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "pages").mkdir(parents=True)
    (bundle / "preview").mkdir()
    (bundle / "document.pdf").write_bytes(b"%PDF-test")
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RenderError) as extra:
        validate_published_bundle(bundle, 1024)
    assert extra.value.code == "invalid_output"

    (bundle / "manifest.json").unlink()
    (bundle / "preview" / "link.png").symlink_to(bundle / "document.pdf")
    with pytest.raises(RenderError) as symlink:
        validate_published_bundle(bundle, 1024)
    assert symlink.value.code == "invalid_output"


def test_published_bundle_rejects_invalid_file_signatures(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "pages").mkdir(parents=True)
    (bundle / "preview").mkdir()
    (bundle / "document.pdf").write_bytes(b"%PDF-1.7")
    (bundle / "pages" / "page.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (bundle / "preview" / "preview.png").write_bytes(b"not-a-png")

    with pytest.raises(RenderError) as raised:
        validate_published_bundle(bundle, 1024)

    assert raised.value.code == "invalid_output"


def test_worker_asset_staging_rejects_filesystem_root(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    source.write_text("<html/>", encoding="utf-8")

    with pytest.raises(RenderError) as raised:
        _copy_asset_tree(Path("/"), source, tmp_path / "target", 1024)

    assert raised.value.code == "path_not_allowed"


def test_worker_metadata_cannot_escape_published_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "pages").mkdir(parents=True)
    (bundle / "preview").mkdir()
    (bundle / "document.pdf").write_bytes(b"%PDF-test")
    (bundle / "pages" / "page.png").write_bytes(b"page")
    (bundle / "preview" / "preview.png").write_bytes(b"preview")
    response = {
        "detection": {
            "format": "docx",
            "family": "office",
            "mime": "application/octet-stream",
            "declared_extension": ".docx",
            "support_level": "guaranteed",
            "metadata": {},
            "motion_signals": [],
        },
        "page_records": [{"path": "pages/page.png"}],
        "preview_records": [{"path": "../outside.png"}],
        "preview_candidate_count": 1,
        "warnings": [],
    }

    with pytest.raises(RenderError) as raised:
        _outcome_from_wire(bundle, response)

    assert raised.value.code == "invalid_output"


def test_worker_request_schema_rejects_unknown_config_fields(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "source": "/workspace/input/page.html",
        "output_dir": "/workspace/output",
        "asset_root": None,
        "preview_limit": 1,
        "options": {
            "motion_mode": "static",
            "capture_duration_seconds": 0.25,
            "capture_at_seconds": 0,
            "page_range": None,
            "viewport_width": 640,
            "viewport_height": 360,
            "actions": None,
        },
        "config": asdict(RenderConfig(chrome_path=None, libreoffice_path=None)),
    }
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    assert _read_request(request)["version"] == 1
    payload["config"]["unexpected"] = True
    request.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RenderError) as raised:
        _read_request(request)

    assert raised.value.code == "invalid_parameters"


@pytest.mark.asyncio
async def test_cancelling_a_sandboxed_job_stops_the_sandbox_and_removes_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy(tmp_path)
    source = policy.workspace / "page.html"
    source.write_text("<html/>", encoding="utf-8")
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Executor:
        async def start(self):
            return None

        async def exec(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

        async def stop(self):
            stopped.set()

    backend = SandboxRenderBackend(
        RenderConfig(chrome_path=None, libreoffice_path=None),
        policy,
        image="renderer:test",
        cpus=1,
        memory_mib=512,
        create_timeout_seconds=1,
    )
    monkeypatch.setattr(backend, "_executor", lambda job_root: Executor())
    task = asyncio.create_task(
        backend.run(
            RenderRequest(
                path=source,
                output_dir=policy.workspace / "output",
                motion_mode="static",
                capture_duration_seconds=0.25,
            ),
            preview_limit=1,
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert list(policy.runtime_root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_sandbox_that_cannot_start_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK's absence has to arrive as an answer, not as a wait that never ends."""
    from raven.sandbox.interfaces import SandboxInitError

    policy = _policy(tmp_path)
    source = policy.workspace / "page.html"
    source.write_text("<html/>", encoding="utf-8")

    class Executor:
        async def start(self):
            raise SandboxInitError("The OpenShell SDK is not installed in this raven's environment.")

        async def exec(self, *args, **kwargs):
            raise AssertionError("the command must not run without a sandbox")

        async def stop(self):
            return None

    backend = SandboxRenderBackend(
        RenderConfig(chrome_path=None, libreoffice_path=None),
        policy,
        image="renderer:test",
        cpus=1,
        memory_mib=512,
        create_timeout_seconds=1,
    )
    monkeypatch.setattr(backend, "_executor", lambda job_root: Executor())

    with pytest.raises(RenderError) as raised:
        await backend.run(
            RenderRequest(
                path=source,
                output_dir=policy.workspace / "output",
                motion_mode="static",
                capture_duration_seconds=0.25,
            ),
            preview_limit=1,
        )

    assert raised.value.code == "renderer_unavailable"
    assert "OpenShell SDK is not installed" in raised.value.message


def test_the_sandbox_backend_can_build_the_executor_it_asks_raven_for(tmp_path: Path) -> None:
    """The constructor call crosses the raven/design-engine seam, so a new
    required argument on raven's side fails here synchronously, before any sandbox.
    The cancellation test above replaces ``_executor``, so it cannot see that."""
    from raven.sandbox.openshell_executor import OpenShellExecutor

    backend = SandboxRenderBackend(
        RenderConfig(chrome_path=None, libreoffice_path=None),
        _policy(tmp_path),
        image="renderer:test",
        cpus=1,
        memory_mib=512,
        create_timeout_seconds=1,
    )

    executor = backend._executor(tmp_path / "job")

    assert isinstance(executor, OpenShellExecutor)
    assert (executor._image, executor._cpus, executor._memory_mib) == ("renderer:test", 1, 512)
    assert executor._network_source() == {}


@pytest.mark.parametrize(
    ("allow_network", "allowlist", "expected"),
    [(False, ("a.example",), False), (True, (), True), (True, ("a.example",), ["a.example"])],
)
def test_the_render_network_setting_maps_onto_the_sandbox_policy(
    tmp_path: Path, allow_network: bool, allowlist: tuple[str, ...], expected: object
) -> None:
    backend = SandboxRenderBackend(
        RenderConfig(chrome_path=None, libreoffice_path=None, allow_network=allow_network, network_allowlist=allowlist),
        _policy(tmp_path),
        image="renderer:test",
        cpus=1,
        memory_mib=512,
        create_timeout_seconds=1,
    )
    from raven.sandbox.network_policy import compile_network_policy, network_from_allow_net

    assert backend._executor(tmp_path / "job")._network_source() == compile_network_policy(
        network_from_allow_net(expected)
    )


@pytest.mark.parametrize("name", ["auto", "sandbox", "boxlite"])
def test_every_isolated_backend_name_builds_the_sandbox_renderer(tmp_path: Path, name: str) -> None:
    from raven_design.rendering.service import RenderService

    built = RenderService._build_backend(
        SimpleNamespace(config=RenderConfig(chrome_path=None, libreoffice_path=None), path_policy=_policy(tmp_path)),
        name,
        worker_image="renderer:test",
        worker_cpus=1,
        worker_memory_mib=512,
        worker_create_timeout_seconds=1,
    )
    assert isinstance(built, SandboxRenderBackend)
