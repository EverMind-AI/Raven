"""media_gen tool: where the provider key is allowed to travel.

Every URL the video path fetches -- the poll target and the content URL --
arrives inside a provider response, so "where the response said to go" is what
decided where the ``Authorization`` header went. The decision was a string
prefix test against ``api_base``, which is host-substitutable as soon as
``api_base`` is configured without a path component (a self-hosted proxy):
``https://api.mycorp.example.attacker.test/`` starts with
``https://api.mycorp.example`` and collected the key.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.agent.tools.media_gen import ImageGenerateTool, VideoGenerateTool


def _tool(api_base: str) -> VideoGenerateTool:
    """``api_base`` is resolved at call time from the tool's config, so the
    config is where the test states it."""
    return VideoGenerateTool(SimpleNamespace(api_base=api_base, model=""))


@pytest.mark.parametrize(
    "base,url,same",
    [
        # A base with no path -- the case the prefix test got wrong.
        ("https://api.mycorp.example", "https://api.mycorp.example/v1/videos/x", True),
        ("https://api.mycorp.example", "https://api.mycorp.example.attacker.test/steal", False),
        ("https://api.mycorp.example", "https://api.mycorp.example-evil.test/steal", False),
        # The shipped default, where prefix and origin already agreed.
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/videos/x", True),
        ("https://openrouter.ai/api/v1", "https://cdn.example.com/signed", False),
        # A default port written out is the same origin; a scheme change is not.
        ("https://api.mycorp.example", "https://api.mycorp.example:443/v1/x", True),
        ("https://api.mycorp.example", "http://api.mycorp.example/v1/x", False),
        ("https://api.mycorp.example", "not a url at all", False),
    ],
)
def test_only_the_api_origin_counts_as_the_api(base: str, url: str, same: bool) -> None:
    assert _tool(base)._is_api_origin(url) is same


def test_credentials_are_withheld_from_every_other_origin() -> None:
    tool = _tool("https://api.mycorp.example")
    headers = {"Authorization": "Bearer sk-test"}

    assert tool._api_headers_for("https://api.mycorp.example/v1/x", headers) == headers
    assert tool._api_headers_for("https://api.mycorp.example.attacker.test/x", headers) is None


def test_a_confined_tool_reads_input_images_only_under_the_workspace(tmp_path) -> None:
    """The file tools honour restrict_to_workspace; an image the model names by
    path is the same read and gets the same rule."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    inside = workspace / "in.png"
    inside.write_bytes(b"\x89PNG\r\n")
    outside = tmp_path / "out.png"
    outside.write_bytes(b"\x89PNG\r\n")
    tool = ImageGenerateTool(
        SimpleNamespace(api_base="https://x.test", model=""), workspace=workspace, restrict_to_workspace=True
    )

    assert tool._image_part(str(inside))["image_url"]["url"].startswith("data:image/png;base64,")
    with pytest.raises(PermissionError):
        tool._image_part(str(outside))


def test_an_unconfined_tool_reads_the_paths_it_is_given(tmp_path) -> None:
    outside = tmp_path / "out.png"
    outside.write_bytes(b"\x89PNG\r\n")
    tool = ImageGenerateTool(SimpleNamespace(api_base="https://x.test", model=""), workspace=tmp_path / "ws")
    assert tool._image_part(str(outside))["image_url"]["url"].startswith("data:image/png;base64,")
