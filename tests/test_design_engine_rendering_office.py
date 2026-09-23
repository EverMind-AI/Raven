"""Unit tests for Office renderer selection and fallback behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fitz")
pytest.importorskip("PIL")

from raven_design.rendering.models import (
    AdapterResult,
    Detection,
    RenderConfig,
    RenderError,
    RenderRequest,
)
from raven_design.rendering.office import LibreOfficeBackend, OfficeRouter


class _Backend:
    def __init__(self, name: str, *, available: bool) -> None:
        self.name = name
        self._available = available
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def render(self, source, bundle_root, detection, request):
        self.calls += 1
        return AdapterResult({}, {}, {}, {}, [], [])


def _detection(format: str = "docx") -> Detection:
    return Detection(
        format=format,
        family="office",
        mime="application/octet-stream",
        declared_extension=f".{format}",
        support_level="guaranteed",
        metadata={},
    )


def _request(source: Path, output: Path) -> RenderRequest:
    return RenderRequest(path=source, output_dir=output)


def test_router_uses_first_available_configured_backend(tmp_path: Path) -> None:
    onlyoffice = _Backend("onlyoffice", available=False)
    libreoffice = _Backend("libreoffice", available=True)
    router = OfficeRouter(
        ("onlyoffice", "libreoffice"),
        {"onlyoffice": onlyoffice, "libreoffice": libreoffice},
    )

    result = router.render(
        tmp_path / "input.docx",
        tmp_path / "bundle",
        _detection(),
        _request(tmp_path / "input.docx", tmp_path),
    )

    assert isinstance(result, AdapterResult)
    assert onlyoffice.calls == 0
    assert libreoffice.calls == 1


def test_graph_backend_is_never_used_without_cloud_opt_in(tmp_path: Path) -> None:
    graph = _Backend("graph", available=True)
    router = OfficeRouter(("graph",), {"graph": graph}, allow_cloud=False)

    with pytest.raises(RenderError) as raised:
        router.render(
            tmp_path / "input.docx",
            tmp_path / "bundle",
            _detection(),
            _request(tmp_path / "input.docx", tmp_path),
        )

    assert raised.value.code == "renderer_unavailable"
    assert graph.calls == 0


def test_libreoffice_false_success_without_pdf_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.docx"
    source.write_bytes(b"not-used")
    config = RenderConfig(
        chrome_path=None,
        libreoffice_path="/usr/bin/libreoffice",
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="convert complete",
            stderr="",
        ),
    )

    with pytest.raises(RenderError) as raised:
        LibreOfficeBackend(config).render(
            source,
            tmp_path / "bundle",
            _detection(),
            _request(source, tmp_path),
        )

    assert raised.value.code == "conversion_failed"


def test_libreoffice_timeout_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.docx"
    source.write_bytes(b"not-used")
    config = RenderConfig(
        chrome_path=None,
        libreoffice_path="/usr/bin/libreoffice",
        timeout_seconds=1,
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("libreoffice", 1)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(RenderError) as raised:
        LibreOfficeBackend(config).render(
            source,
            tmp_path / "bundle",
            _detection(),
            _request(source, tmp_path),
        )

    assert raised.value.code == "render_timeout"
    assert raised.value.retryable is True


def test_libreoffice_profile_carries_the_hosts_chinese_faces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The design engine's preview runs LibreOffice with a profile of its own;
    on a Mac that profile is the only place the Chinese faces reach it from."""
    from raven.utils import office

    songti = tmp_path / "Songti.ttc"
    songti.write_bytes(b"ttcf")
    monkeypatch.setattr(office.sys, "platform", "darwin")
    monkeypatch.setattr(office, "MACOS_SYSTEM_HAN_FACES", (songti,))
    monkeypatch.setattr(office, "MACOS_ASSET_FONTS", tmp_path / "no-assets")
    source = tmp_path / "input.pptx"
    source.write_bytes(b"not-used")
    config = RenderConfig(chrome_path=None, libreoffice_path="/usr/bin/libreoffice")
    seen: list[str] = []

    def run(command, **kwargs):
        uri = next(token for token in command if token.startswith("-env:UserInstallation="))
        profile = Path(uri.split("file://", 1)[1])
        seen.extend(p.name for p in (profile / "user" / "fonts").iterdir())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RenderError):
        LibreOfficeBackend(config).render(source, tmp_path / "bundle", _detection("pptx"), _request(source, tmp_path))

    assert seen == ["Songti.ttc"]
