"""Plugin ``tools`` contribution point — manifest, registry, CLI stack,
render surfacing, and the EverOS ``understand_media`` tool itself.
"""

from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.plugin import (
    Contributes,
    DiscoveredPlugin,
    PluginConflict,
    PluginContext,
    PluginManifest,
    PluginNotFound,
    PluginRegistry,
    ServiceLocator,
    Source,
    ToolContribution,
)

# ---------------------------------------------------------------------------
# Test-module injection (mirrors test_plugin_registry.py)
# ---------------------------------------------------------------------------


def _install_test_module(name: str, attrs: dict[str, object]) -> None:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


@pytest.fixture(autouse=True)
def _cleanup_modules():
    snapshot = set(sys.modules)
    yield
    for k in set(sys.modules) - snapshot:
        sys.modules.pop(k, None)


def _discovered_with_tools(
    plugin_id: str,
    tools: list[tuple[str, str]],
) -> DiscoveredPlugin:
    mf = PluginManifest(
        id=plugin_id,
        version="0.1.0",
        enabled_by_default=True,
        contributes=Contributes(
            tools=[ToolContribution(name=n, factory=f) for n, f in tools],
        ),
    )
    return DiscoveredPlugin(manifest=mf, source=Source.USER, location=None)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifestTools:
    def test_parses_tools_contribution(self) -> None:
        toml = textwrap.dedent("""
            [plugin]
            id = "everos-memory"
            version = "0.1.0"
            [[plugin.contributes.tools]]
            name = "understand_media"
            factory = "raven.plugin.memory.everos.tools:make_understand_media_tool"
        """)
        mf = PluginManifest.from_toml_str(toml)
        assert [t.name for t in mf.contributes.tools] == ["understand_media"]
        assert mf.contributes.tools[0].factory.endswith(":make_understand_media_tool")

    def test_default_tools_empty(self) -> None:
        mf = PluginManifest(id="p", version="0.1.0")
        assert mf.contributes.tools == []

    def test_bad_factory_ref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ToolContribution(name="t", factory="not-a-ref")

    def test_duplicate_tool_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate tool name"):
            PluginManifest(
                id="p",
                version="0.1.0",
                contributes=Contributes(
                    tools=[
                        ToolContribution(name="dup", factory="m:a"),
                        ToolContribution(name="dup", factory="m:b"),
                    ],
                ),
            )

    def test_backend_and_tool_may_share_name(self) -> None:
        # Uniqueness is per-kind; a backend and a tool named the same is fine.
        from raven.plugin import MemoryBackendContribution

        mf = PluginManifest(
            id="p",
            version="0.1.0",
            contributes=Contributes(
                memory_backends=[MemoryBackendContribution(name="x", factory="m:b")],
                tools=[ToolContribution(name="x", factory="m:t")],
            ),
        )
        assert mf.contributes.tools[0].name == "x"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistryTools:
    def test_activates_and_resolves_tool(self) -> None:
        def fake_factory(ctx):
            return "tool-instance"

        _install_test_module("_tp_tools_a", {"make_tool": fake_factory})
        reg = PluginRegistry()
        reg.activate([_discovered_with_tools("alpha", [("understand_media", "_tp_tools_a:make_tool")])])

        assert reg.tool_names() == ["understand_media"]
        assert reg.tool_plugin_id("understand_media") == "alpha"
        assert reg.get_tool_factory("understand_media") is fake_factory

    def test_build_tool_invokes_factory_with_context(self, tmp_path: Path) -> None:
        captured = {}

        def fake_factory(ctx: PluginContext):
            captured["config"] = ctx.config
            return "built"

        _install_test_module("_tp_tools_b", {"make_tool": fake_factory})
        reg = PluginRegistry()
        reg.activate([_discovered_with_tools("p", [("t", "_tp_tools_b:make_tool")])])

        out = reg.build_tool(
            "t", config={"k": 1}, services=ServiceLocator(workspace=tmp_path),
        )
        assert out == "built"
        assert captured["config"] == {"k": 1}

    def test_tool_name_conflict_across_plugins(self) -> None:
        def f(ctx):
            return None

        _install_test_module("_tp_tools_c", {"make_tool": f})
        reg = PluginRegistry()
        with pytest.raises(PluginConflict, match="tool 'dup'"):
            reg.activate([
                _discovered_with_tools("one", [("dup", "_tp_tools_c:make_tool")]),
                _discovered_with_tools("two", [("dup", "_tp_tools_c:make_tool")]),
            ])

    def test_unknown_tool_raises(self) -> None:
        reg = PluginRegistry()
        with pytest.raises(PluginNotFound):
            reg.get_tool_factory("nope")


# ---------------------------------------------------------------------------
# CLI stack — build_plugin_tools
# ---------------------------------------------------------------------------


class TestBuildPluginTools:
    def _config(self, plugin_config: dict | None = None):
        from raven.config.raven import RavenConfig, PluginsConfig

        return RavenConfig(plugins=PluginsConfig(config=dict(plugin_config or {})))

    def test_builds_tools_from_registry(self, tmp_path: Path) -> None:
        from raven.cli._plugin_stack import build_plugin_tools

        seen = {}

        def fake_factory(ctx):
            seen["config"] = ctx.config
            return f"tool::{ctx.config.get('flag')}"

        _install_test_module("_tp_tools_d", {"make_tool": fake_factory})
        reg = PluginRegistry()
        reg.activate([_discovered_with_tools("myplugin", [("t1", "_tp_tools_d:make_tool")])])

        cfg = self._config({"myplugin": {"flag": "on"}})
        tools = build_plugin_tools(tmp_path, cfg, registry=reg)
        assert tools == ["tool::on"]
        assert seen["config"] == {"flag": "on"}

    def test_empty_when_no_tools(self, tmp_path: Path) -> None:
        from raven.cli._plugin_stack import build_plugin_tools

        assert build_plugin_tools(tmp_path, self._config(), registry=PluginRegistry()) == []

    def test_failing_factory_is_skipped(self, tmp_path: Path) -> None:
        from raven.cli._plugin_stack import build_plugin_tools

        def boom(ctx):
            raise RuntimeError("nope")

        _install_test_module("_tp_tools_e", {"make_tool": boom})
        reg = PluginRegistry()
        reg.activate([_discovered_with_tools("p", [("t", "_tp_tools_e:make_tool")])])
        # One bad tool doesn't crash the build — it's logged + skipped.
        assert build_plugin_tools(tmp_path, self._config(), registry=reg) == []

    def test_none_factory_is_skipped(self, tmp_path: Path) -> None:
        from raven.cli._plugin_stack import build_plugin_tools

        # A factory may return None to decline contribution (e.g. an
        # optional dependency is absent) — skipped without error.
        def opt_out(ctx):
            return None

        _install_test_module("_tp_tools_f", {"make_tool": opt_out})
        reg = PluginRegistry()
        reg.activate([_discovered_with_tools("p", [("t", "_tp_tools_f:make_tool")])])
        assert build_plugin_tools(tmp_path, self._config(), registry=reg) == []


# ---------------------------------------------------------------------------
# render.build_user_content — attachment surfacing
# ---------------------------------------------------------------------------


class TestRenderAttachments:
    def test_non_image_surfaced_as_note(self, tmp_path: Path) -> None:
        from raven.context_engine.segments import render

        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4 data")
        out = render.build_user_content("summarize this", [str(pdf)])
        assert isinstance(out, str)
        assert "report.pdf" in out
        assert "understand_media" in out
        assert "summarize this" in out

    def test_image_inlined_as_block(self, tmp_path: Path) -> None:
        import base64 as _b64

        from raven.context_engine.segments import render

        png = tmp_path / "a.png"
        png.write_bytes(_b64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        ))
        out = render.build_user_content("look", [str(png)])
        assert isinstance(out, list)
        assert out[0]["type"] == "image_url"
        assert out[-1] == {"type": "text", "text": "look"}

    def test_mixed_image_and_doc(self, tmp_path: Path) -> None:
        import base64 as _b64

        from raven.context_engine.segments import render

        png = tmp_path / "a.png"
        png.write_bytes(_b64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        ))
        pdf = tmp_path / "d.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        out = render.build_user_content("q", [str(png), str(pdf)])
        assert isinstance(out, list)
        assert out[0]["type"] == "image_url"
        text_block = out[-1]["text"]
        assert "d.pdf" in text_block and "understand_media" in text_block

    def test_no_media_returns_text(self) -> None:
        from raven.context_engine.segments import render

        assert render.build_user_content("hi", None) == "hi"


# ---------------------------------------------------------------------------
# EverOS understand_media tool (needs raven.plugin.memory.everos importable)
# ---------------------------------------------------------------------------

pytest.importorskip("raven.plugin.memory.everos")
from raven.plugin.memory.everos.tools import UnderstandMediaTool, make_understand_media_tool  # noqa: E402


class TestUnderstandMediaTool:
    def test_factory_returns_tool_when_extra_available(self, monkeypatch) -> None:
        from raven.agent.tools.base import Tool

        import raven.plugin.memory.everos.tools as tools_mod

        # Gate on the parser extra; force it "available" so the assertion
        # holds regardless of whether the heavy extra is installed here.
        monkeypatch.setattr(tools_mod, "_multimodal_available", lambda: True)
        t = make_understand_media_tool(None)
        assert isinstance(t, Tool)
        assert t.name == "understand_media"
        assert "paths" in t.parameters["properties"]

    def test_factory_returns_none_when_extra_missing(self, monkeypatch) -> None:
        # No everos[multimodal] extra → factory declines to contribute the
        # tool (returns None) so the host never registers it.
        import raven.plugin.memory.everos.tools as tools_mod

        monkeypatch.setattr(tools_mod, "_multimodal_available", lambda: False)
        assert make_understand_media_tool(None) is None

    async def test_missing_paths_errors(self) -> None:
        t = UnderstandMediaTool()
        assert (await t.execute(paths=None)).startswith("Error")
        assert (await t.execute(paths=[])).startswith("Error")

    async def test_degrades_when_unavailable(self, monkeypatch) -> None:
        # When the multimodal runtime is unavailable, understand_files
        # raises MultimodalUnavailable and the tool surfaces one clear
        # error message rather than propagating the exception.
        import raven.plugin.memory.everos.tools as tools_mod
        from raven.plugin.memory.everos.multimodal import MultimodalUnavailable

        async def fake_unavailable(paths):
            raise MultimodalUnavailable("multimodal extra not configured")

        monkeypatch.setattr(tools_mod, "understand_files", fake_unavailable)
        out = await UnderstandMediaTool().execute(paths=["/tmp/x.pdf"])
        assert "unavailable" in out.lower()

    async def test_formats_results(self, monkeypatch) -> None:
        import raven.plugin.memory.everos.tools as tools_mod

        async def fake_understand(paths):
            return [
                {"path": paths[0], "name": "a.pdf", "text": "hello world"},
                {"path": paths[1], "name": "b.mp4", "error": "video not supported"},
            ]

        monkeypatch.setattr(tools_mod, "understand_files", fake_understand)
        out = await UnderstandMediaTool().execute(paths=["/x/a.pdf", "/x/b.mp4"])
        assert "## a.pdf" in out and "hello world" in out
        assert "## b.mp4" in out and "video not supported" in out


class TestContentItemRouting:
    """Input routing in multimodal._content_item_for — file path vs URL.

    Unit-level so it needs no LLM / network: just asserts the ContentItem
    shape everalgo will receive.
    """

    def test_http_url_detection(self) -> None:
        from raven.plugin.memory.everos.multimodal import _is_http_url

        assert _is_http_url("https://example.com/page")
        assert _is_http_url("http://example.com")
        assert not _is_http_url("/tmp/a.png")
        assert not _is_http_url("file:///tmp/a.png")
        assert not _is_http_url("relative/path.pdf")

    def test_url_becomes_uri_item(self) -> None:
        from raven.plugin.memory.everos.multimodal import _content_item_for

        item = _content_item_for("https://example.com/doc")
        # uri-backed → everalgo fetches + dispatches by Content-Type;
        # no base64 payload, no local read.
        assert item == {
            "type": "url",
            "uri": "https://example.com/doc",
            "name": "https://example.com/doc",
        }

    def test_local_file_becomes_base64_item(self, tmp_path: Path) -> None:
        from raven.plugin.memory.everos.multimodal import _content_item_for

        f = tmp_path / "hello.txt"
        f.write_text("hi")
        item = _content_item_for(str(f))
        assert item["name"] == "hello.txt"
        assert item["ext"] == ".txt"
        assert "base64" in item and "uri" not in item

    def test_missing_local_file_raises(self) -> None:
        from raven.plugin.memory.everos.multimodal import _content_item_for

        with pytest.raises(FileNotFoundError):
            _content_item_for("/no/such/file.pdf")
