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

import asyncio
import base64
import json
import math
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from raven.agent.loop.failure_streak import failure_class, is_hard_tool_failure
from raven.agent.tools import media_gen
from raven.agent.tools.media_gen import ImageGenerateTool, SpeechGenerateTool, VideoGenerateTool


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


# ── the image path: which endpoint a model is sent to ──

_PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
_B64 = base64.b64encode(_PNG).decode()


def _image_tool(monkeypatch, handler, *, model: str, workspace: Path, api_base: str = "https://openrouter.ai/api/v1"):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    return ImageGenerateTool(SimpleNamespace(api_base=api_base, model=model, api_key="k"), workspace=workspace)


@pytest.mark.parametrize(
    "model,wire_model",
    [
        ("openai/gpt-image-2", "openai/gpt-image-2"),
        ("openai/gpt-image-2.5-sunburst", "openai/gpt-image-2.5-sunburst"),
        ("openrouter/openai/gpt-image-2.5-sunburst", "openai/gpt-image-2.5-sunburst"),
        ("openai/gpt-image-2.5-flare", "openai/gpt-image-2.5-flare"),
        ("openrouter/openai/gpt-image-2.5-flare", "openai/gpt-image-2.5-flare"),
    ],
)
@pytest.mark.parametrize("from_config", [True, False])
async def test_a_dedicated_image_model_goes_to_the_images_api_with_its_references(
    monkeypatch, tmp_path, model, wire_model, from_config
) -> None:
    """gpt-image-2 answers 404 on chat/completions whatever the modalities say;
    OpenRouter serves it only through ``/images``, where edit inputs travel as
    ``input_references``."""
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"data": [{"b64_json": _B64, "media_type": "image/png"}]})

    ref = tmp_path / "logo.png"
    ref.write_bytes(_PNG)
    configured = model if from_config else ""
    tool = _image_tool(monkeypatch, handler, model=configured, workspace=tmp_path / "ws")
    out = json.loads(
        await tool.execute(
            "a poster", model=None if from_config else model, images=[str(ref)], aspect_ratio="3:4", quality="high"
        )
    )
    assert out["success"] and Path(out["paths"][0]).read_bytes() == _PNG
    assert out["model"] == wire_model
    assert tool._config_static.model == configured
    assert len(seen) == 1
    path, body = seen[0]
    assert path == "/api/v1/images"
    assert (body["model"], body["aspect_ratio"], body["quality"]) == (wire_model, "3:4", "high")
    reference = body["input_references"][0]
    assert reference["type"] == "image_url" and reference["image_url"]["url"].startswith("data:image/png;base64,")


async def test_a_confined_tool_accepts_an_output_dir_inside_the_bound_session(monkeypatch, tmp_path) -> None:
    """The session directory a turn binds may live outside agent home; the
    fence has to admit it, or a confined task cannot keep its images in its
    own folder -- the case output_dir exists for."""
    from raven.agent import workdir

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": _B64, "media_type": "image/png"}]})

    home, session = tmp_path / "home", tmp_path / "session"
    home.mkdir()
    session.mkdir()
    tool = _image_tool(monkeypatch, handler, model="openai/gpt-image-2", workspace=home)
    tool._restrict_to_workspace = True
    with workdir.bind(session):
        out = json.loads(await tool.execute("a mark", output_dir="assets/generated"))
        assert Path(out["paths"][0]).parent == (session / "assets/generated").resolve()
        with pytest.raises(PermissionError):
            tool._output_path("png", str(tmp_path / "elsewhere"))


async def test_output_dir_places_the_image_where_the_caller_asks(monkeypatch, tmp_path) -> None:
    """A task confined to one folder needs its images inside that folder, not in
    the workspace-level generated/ scratch; a confined tool still refuses a
    directory outside the workspace."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": _B64, "media_type": "image/png"}]})

    ws = tmp_path / "ws"
    tool = _image_tool(monkeypatch, handler, model="openai/gpt-image-2", workspace=ws)
    out = json.loads(await tool.execute("a mark", output_dir="task/assets/generated"))
    assert Path(out["paths"][0]).parent == (ws / "task/assets/generated").resolve()
    tool._restrict_to_workspace = True
    with pytest.raises(PermissionError):
        tool._output_path("png", str(tmp_path / "elsewhere"))


async def test_an_openai_compatible_base_takes_a_size_and_edits_by_multipart(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(
        monkeypatch, handler, model="gpt-image-2", workspace=tmp_path / "ws", api_base="https://gw.test/v1"
    )
    assert json.loads(await tool.execute("a poster", aspect_ratio="3:4"))["success"]
    generation = seen[0]
    assert generation.url.path == "/v1/images/generations"
    assert json.loads(generation.content)["size"] == "1024x1536"

    ref = tmp_path / "logo.png"
    ref.write_bytes(_PNG)
    assert json.loads(await tool.execute("blend the logo in", images=[str(ref)]))["success"]
    edit = seen[1]
    assert edit.url.path == "/v1/images/edits"
    assert edit.headers["content-type"].startswith("multipart/form-data")
    assert b'name="image[]"' in edit.content and _PNG in edit.content and b"gpt-image-2" in edit.content


_JPG = b"\xff\xd8\xff\xe0" + b"jpg pixels"


async def test_an_ark_plan_base_is_answered_in_url_form_and_downloads_the_picture(monkeypatch, tmp_path) -> None:
    """The Volces Ark Agent-Plan endpoint speaks its own dialect on
    /images/generations -- ``size`` as an explicit WxH pair at the 2K budget,
    no response_format -- and answers with ``data[].url`` only, so the picture
    behind the URL is fetched and written like any other generated file."""
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            sent["body"] = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/tos/signed.jpeg"}]})
        sent["download"] = request
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_JPG)

    tool = _image_tool(
        monkeypatch,
        handler,
        model="doubao-seedream-5-0-pro",
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
    )
    out = json.loads(await tool.execute("a poster", aspect_ratio="16:9"))
    assert out["success"] and out["model"] == "doubao-seedream-5-0-pro"
    path = Path(out["paths"][0])
    assert path.suffix == ".jpg" and path.read_bytes() == _JPG
    assert sent["body"] == {"model": "doubao-seedream-5-0-pro", "prompt": "a poster", "n": 1, "size": "2736x1539"}
    # The download URL is the provider's own storage, not its API: the key the
    # generation call carried must not travel with the fetch.
    assert sent["download"].headers.get("Authorization") is None


async def test_an_ark_plan_base_without_a_ratio_leaves_size_to_the_endpoint(monkeypatch, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            assert "size" not in json.loads(request.content)
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/tos/signed.jpeg"}]})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_JPG)

    tool = _image_tool(
        monkeypatch,
        handler,
        model="doubao-seedream-5-0-pro",
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
    )
    assert json.loads(await tool.execute("a poster"))["success"]


async def test_an_ark_plan_base_with_an_unknown_ratio_falls_back_to_the_2k_tier(monkeypatch, tmp_path) -> None:
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            sent["body"] = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/tos/signed.jpeg"}]})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_JPG)

    tool = _image_tool(
        monkeypatch,
        handler,
        model="doubao-seedream-5-0-pro",
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
    )
    assert json.loads(await tool.execute("a poster", aspect_ratio="5:1"))["success"]
    assert sent["body"]["size"] == "2K"


async def test_a_seedream_3_t2i_ark_base_is_sized_inside_its_own_window(monkeypatch, tmp_path) -> None:
    """Seedream 3.0-t2i's card lists WxH only, in [512x512, 2048x2048] -- the
    model-independent 2736x1539 is a 400 there, and an unspellable ratio has
    no tier to fall back to, so the endpoint default stands in."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/tos/signed.jpeg"}]})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_JPG)

    tool = _image_tool(
        monkeypatch,
        handler,
        model="doubao-seedream-3-0-t2i-250415",
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
    )
    for ratio in ("16:9", "21:9", "5:1"):
        assert json.loads(await tool.execute("a poster", aspect_ratio=ratio))["success"]
    # 16:9 hits the 2048 per-side cap; 21:9 lands on the pair that always
    # worked pre-PR; 5:1 is unspellable and 3.0-t2i has no tier to send.
    assert [body.get("size") for body in sent] == ["2048x1152", "1536x658", None]


_EP_ID = "ep-20250101000000-abcde"


def _ark_ep_tool(monkeypatch, handler, tmp_path: Path) -> ImageGenerateTool:
    return _image_tool(
        monkeypatch,
        handler,
        model=_EP_ID,
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
    )


async def test_an_ark_endpoint_id_keeps_the_ratio_and_retries_sizeless_on_a_400(monkeypatch, tmp_path) -> None:
    """An inference endpoint id names none of the family keywords, so the ratio
    used to be dropped on the floor and the endpoint rendered its default
    frame; and no single WxH is valid for every family behind an ep- id. The
    ratio passes through, and a sized call the endpoint refuses is retried once
    with no size at all, letting the endpoint default stand."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            sent.append(json.loads(request.content))
            if len(sent) == 1:
                return httpx.Response(400, json={"error": {"message": "size exceeds the model's range"}})
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/tos/signed.jpeg"}]})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_JPG)

    out = json.loads(await _ark_ep_tool(monkeypatch, handler, tmp_path).execute("a poster", aspect_ratio="16:9"))
    assert out["success"] and Path(out["paths"][0]).read_bytes() == _JPG
    # The explicit pair drawn from the default window first, then no size key.
    assert sent[0]["size"] == "2736x1539"
    assert "size" not in sent[1]


async def test_an_ark_endpoint_id_whose_sized_call_succeeds_never_reposts(monkeypatch, tmp_path) -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/tos/signed.jpeg"}]})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_JPG)

    assert json.loads(await _ark_ep_tool(monkeypatch, handler, tmp_path).execute("a poster", aspect_ratio="16:9"))[
        "success"
    ]
    assert len(sent) == 1 and sent[0]["size"] == "2736x1539"


async def test_an_ark_endpoint_id_refused_both_times_propagates_the_400(monkeypatch, tmp_path) -> None:
    """The retry is one shot: a size-less call still refused is a real error,
    and it reaches the model the way every other HTTP failure does."""
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            posts.append(json.loads(request.content))
        return httpx.Response(400, text="InvalidParameter")

    out = json.loads(await _ark_ep_tool(monkeypatch, handler, tmp_path).execute("a poster", aspect_ratio="16:9"))
    assert out["error"] == "HTTP 400"
    assert len(posts) == 2 and "size" not in posts[1]


async def test_a_ratio_less_ark_endpoint_call_sends_no_size_on_the_first_try(monkeypatch, tmp_path) -> None:
    """Without a ratio there is nothing to spell, exactly as for a named model:
    the first request carries no size key and no retry is waiting behind it."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/tos/signed.jpeg"}]})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_JPG)

    assert json.loads(await _ark_ep_tool(monkeypatch, handler, tmp_path).execute("a poster"))["success"]
    assert len(sent) == 1 and "size" not in sent[0]


async def test_an_identified_family_s_400_is_not_retried(monkeypatch, tmp_path) -> None:
    """A matched card's window is documented, so a 400 naming the size is a
    real error there, not a guess to take back."""
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            posts.append(json.loads(request.content))
        return httpx.Response(400, text="InvalidParameter")

    tool = _image_tool(
        monkeypatch,
        handler,
        model="doubao-seedream-4-5-pro",
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
    )
    out = json.loads(await tool.execute("a poster", aspect_ratio="16:9"))
    assert out["error"] == "HTTP 400"
    assert len(posts) == 1 and posts[0]["size"] == "2736x1539"


def test_ark_explicit_sizes_stay_exact_and_inside_every_models_window() -> None:
    """The reviewer's hand enumeration, locked into CI: for every supported
    ratio and every Seedream window in the table (default included), the pair
    sent keeps the ratio exact by construction -- the same coprime pair scaled
    by one integer -- and fits the card's per-side and total-pixel bounds."""
    windows = [(f"doubao-{needle}", *bounds) for needle, *bounds in media_gen._ARK_SIZE_WINDOWS]
    windows.append(("doubao-seedream-future", None, None, 2560 * 1440, 4096 * 4096))
    for ratio, dims in media_gen._SIZES.items():
        width, height = (int(side) for side in dims.split("x"))
        divisor = math.gcd(width, height)
        for model_id, min_side, max_side, min_total, max_total in windows:
            size = media_gen.ark_explicit_size(model_id, ratio)
            assert size is not None, (model_id, ratio)
            sw, sh = (int(side) for side in size.split("x"))
            common = math.gcd(sw, sh)
            assert (sw // common, sh // common) == (width // divisor, height // divisor)
            assert min_total <= sw * sh <= max_total, (model_id, ratio, size)
            if min_side:
                assert min_side <= min(sw, sh), (model_id, ratio, size)
            if max_side:
                assert max(sw, sh) <= max_side, (model_id, ratio, size)


async def test_an_ark_plan_base_still_takes_an_inline_b64_picture(monkeypatch, tmp_path) -> None:
    """b64_json keeps working wherever it appears: the url form was added for
    Ark, not swapped for it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": _B64, "media_type": "image/png"}]})

    tool = _image_tool(
        monkeypatch,
        handler,
        model="doubao-seedream-5-0-pro",
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
    )
    out = json.loads(await tool.execute("a poster"))
    assert out["success"] and Path(out["paths"][0]).read_bytes() == _PNG


async def test_any_openai_compatible_base_may_answer_in_url_form(monkeypatch, tmp_path) -> None:
    """data[].url is read on every Images base, not only Ark's: a gateway
    answering OpenAI's documented url form used to look like no image at all,
    and the pixel size the same base takes is unchanged."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/generations":
            assert json.loads(request.content)["size"] == "1024x1536"
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/cdn/signed.png"}]})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_PNG)

    tool = _image_tool(
        monkeypatch, handler, model="gpt-image-2", workspace=tmp_path / "ws", api_base="https://gw.test/v1"
    )
    out = json.loads(await tool.execute("a poster", aspect_ratio="3:4"))
    assert out["success"] and Path(out["paths"][0]).read_bytes() == _PNG


async def test_a_generated_picture_pointing_inward_is_refused_before_anything_is_fetched(monkeypatch, tmp_path) -> None:
    """The download side of the URL form is a provider-supplied address too: the
    guarded fetch refuses a link-local or private target, and -- the generation
    already billed -- that refusal is its own error, not "no image returned"."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/plan/v3/images/generations":
            return httpx.Response(200, json={"data": [{"url": "http://169.254.169.254/latest/meta-data/"}]})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_JPG)

    tool = _image_tool(
        monkeypatch,
        handler,
        model="doubao-seedream-5-0-pro",
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
    )
    out = json.loads(await tool.execute("a poster"))
    assert out["error"] == "generated, but the picture URL was refused or could not be fetched"
    assert out["url"] == "http://169.254.169.254/latest/meta-data/"
    assert len(seen) == 1


async def test_a_generated_picture_a_dead_network_cannot_deliver_is_not_read_as_missing(monkeypatch, tmp_path) -> None:
    """A public URL the transport cannot reach is the same billed generation:
    the answer names the fetch failure instead of reading as no image."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/tos/signed.jpeg"}]})
        raise httpx.ConnectError("[Errno 61] Connection refused to 1.2.3.4")

    tool = _image_tool(
        monkeypatch,
        handler,
        model="doubao-seedream-5-0-pro",
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
    )
    out = json.loads(await tool.execute("a poster"))
    assert out["error"] == "generated, but the picture URL was refused or could not be fetched"
    assert out["url"] == "https://1.2.3.4/tos/signed.jpeg"
    assert "1.2.3.4" in out["detail"]


@pytest.mark.parametrize(
    ("status", "body"),
    [(403, "AccessDenied: Request has expired"), (404, "NoSuchKey"), (500, "InternalError")],
    ids=["expired-signature", "gone", "storage-error"],
)
async def test_a_generated_picture_its_storage_will_not_serve_is_not_read_as_a_failed_request(
    monkeypatch, tmp_path, status, body
) -> None:
    """The storage behind the URL can answer with an error status of its own --
    a signature that expired, an object already gone -- after the generation is
    billed. That is the same unfetchable picture, not the generation request
    failing: read as the request's own HTTP 403, it told the model to retry."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plan/v3/images/generations":
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/tos/signed.jpeg"}]})
        return httpx.Response(status, text=body)

    tool = _image_tool(
        monkeypatch,
        handler,
        model="doubao-seedream-5-0-pro",
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/plan/v3",
    )
    out = json.loads(await tool.execute("a poster"))
    assert out["error"] == "generated, but the picture URL was refused or could not be fetched"
    assert out["url"] == "https://1.2.3.4/tos/signed.jpeg"
    assert out["detail"].startswith(f"HTTP {status}") and body in out["detail"]


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254/latest/meta-data/", "https://1.2.3.4/cdn/signed.png"],
    ids=["refused", "unreachable"],
)
async def test_a_gateway_s_picture_url_it_cannot_fetch_is_its_own_error_too(monkeypatch, tmp_path, url) -> None:
    """Any Images base that answers in URL form has billed the picture before
    the download, so a gateway's refused or unreachable URL is answered the way
    Ark's is, not as "no image returned"."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/images/generations":
            return httpx.Response(200, json={"data": [{"url": url}]})
        raise httpx.ConnectError("Connection refused to 1.2.3.4")

    tool = _image_tool(
        monkeypatch, handler, model="gpt-image-2", workspace=tmp_path / "ws", api_base="https://gw.test/v1"
    )
    out = json.loads(await tool.execute("a poster"))
    assert out["error"] == "generated, but the picture URL was refused or could not be fetched"
    assert out["url"] == url
    assert seen[0].url.path == "/v1/images/generations"


@pytest.mark.parametrize(
    ("model", "ratio", "size"),
    [
        ("gpt-image-2", "16:9", "1536x1024"),
        ("gpt-image-2", "21:9", "1536x1024"),
        ("gpt-image-2", "3:4", "1024x1536"),
        ("gpt-image-2", "9:16", "1024x1536"),
        ("gpt-image-2", "1:1", "1024x1024"),
        ("openai/gpt-image-1", "4:3", "1536x1024"),
        ("bytedance/seedream-4.5", "16:9", "1536x864"),
        ("qwen/qwen-image-2", "3:4", "1152x1536"),
        ("gpt-image-2", None, None),
    ],
)
def test_the_images_api_size_is_one_the_model_family_draws(model, ratio, size) -> None:
    """gpt-image draws three frames and refuses 1536x864 outright; the other
    families take the pixel table. The deck engine leans on the same mapping."""
    from raven.agent.tools.media_gen import images_api_size

    assert images_api_size(model, ratio) == size


async def test_a_reference_pointing_inward_is_refused_before_anything_is_fetched(monkeypatch, tmp_path) -> None:
    """``images`` is model-controlled and may name any URL. The multipart edit
    path is the one that fetches such a URL from this process, so it goes
    through the guarded fetch: a link-local or private target is refused, and
    nothing is read from it or uploaded to the provider."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(
        monkeypatch, handler, model="gpt-image-2", workspace=tmp_path / "ws", api_base="https://gw.test/v1"
    )
    out = json.loads(await tool.execute("blend it in", images=["http://169.254.169.254/latest/meta-data/"]))
    assert "refused" in out["error"]
    assert seen == []


@pytest.mark.parametrize("model", ["vendor/pictures-only", "openrouter/vendor/pictures-only"])
async def test_a_chat_refusal_for_an_image_only_model_falls_back_to_the_images_api(
    monkeypatch, tmp_path, model
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert json.loads(request.content)["model"] == "vendor/pictures-only"
        if request.url.path.endswith("/chat/completions"):
            message = (
                "vendor/pictures-only is an image generation model and cannot be used with the "
                "chat/completions endpoint. Use the /api/v1/images endpoint instead."
            )
            return httpx.Response(404, json={"error": {"message": message, "code": 404}})
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(monkeypatch, handler, model=model, workspace=tmp_path / "ws")
    assert json.loads(await tool.execute("a poster"))["success"]
    assert seen == ["/api/v1/chat/completions", "/api/v1/images"]


@pytest.mark.parametrize("model", ["google/gemini-2.5-flash-image", "openrouter/google/gemini-2.5-flash-image"])
async def test_a_chat_routed_image_model_still_takes_chat_completions(monkeypatch, tmp_path, model) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert json.loads(request.content)["modalities"] == ["image", "text"]
        assert json.loads(request.content)["model"] == "google/gemini-2.5-flash-image"
        message = {"images": [{"image_url": {"url": "data:image/png;base64," + _B64}}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    tool = _image_tool(monkeypatch, handler, model=model, workspace=tmp_path / "ws")
    assert json.loads(await tool.execute("a poster"))["success"]
    assert seen == ["/api/v1/chat/completions"]


@pytest.mark.parametrize("api_base", ["https://gateway.test/v1", "https://openrouter.ai.gateway.test/v1"])
async def test_a_custom_image_endpoint_keeps_its_model_prefix(monkeypatch, tmp_path, api_base) -> None:
    seen = []
    model = "openrouter/openai/gpt-image-2.5-sunburst"

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(monkeypatch, handler, model=model, workspace=tmp_path, api_base=api_base)
    out = json.loads(await tool.execute("a poster"))
    assert out["success"] and out["model"] == model
    assert len(seen) == 1 and str(seen[0].url) == api_base + "/images/generations"
    assert json.loads(seen[0].content)["model"] == model


@pytest.mark.parametrize("model", ["sensenova-u1.5-lite", "google/gemini-2.5-flash-image"])
async def test_an_openai_compatible_base_takes_every_model_through_the_images_api(monkeypatch, tmp_path, model) -> None:
    """Chat routing with output modalities is OpenRouter's own. An OpenAI-compatible
    base serves pictures at ``/images/generations`` whatever the model is called, and
    answers ``chat/completions`` for an image model in its own words, which the
    OpenRouter fallback does not recognise -- so the chat attempt only ever failed."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(monkeypatch, handler, model=model, workspace=tmp_path / "ws", api_base="https://relay.test/v1")
    out = json.loads(await tool.execute("a poster"))
    assert out["success"] and out["model"] == model
    assert seen == ["/v1/images/generations"]


async def test_the_exported_openrouter_key_never_goes_to_another_address(monkeypatch, tmp_path) -> None:
    """``OPENROUTER_API_KEY`` is OpenRouter's credential. A keyless section pointed at
    another endpoint used to take it anyway and send it there as the bearer token."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    elsewhere = SimpleNamespace(api_base="https://relay.test/v1", model="gpt-image-2", api_key="")
    tool = ImageGenerateTool(elsewhere, workspace=tmp_path)
    assert tool.api_key == "" and not ImageGenerateTool.has_key(elsewhere)
    out = json.loads(await tool.execute("a poster"))
    assert "no API key" in out["error"] and seen == []

    on_openrouter = SimpleNamespace(api_base="", model="gpt-image-2", api_key="")
    assert ImageGenerateTool(on_openrouter, workspace=tmp_path).api_key == "sk-or-env"


async def test_a_named_provider_s_headers_ride_the_image_request(monkeypatch, tmp_path) -> None:
    """A provider's connection is its address, its key and its headers: an endpoint
    that routes or authenticates by header refuses a request carrying the first two
    alone. Driven from the file through the live reader, the way the loop builds it."""
    from raven.config.live import LiveConfig, media_tool_config

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "custom": {"apiKey": "sk-c", "apiBase": "https://relay.test/v1", "extraHeaders": {"X-Tenant": "t1"}}
                },
                "tools": {"media": {"image": {"model": "gpt-image-2", "provider": "custom"}}},
            }
        )
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    live = LiveConfig(path)
    tool = ImageGenerateTool(lambda: media_tool_config(live, "image"), workspace=tmp_path / "ws")
    assert json.loads(await tool.execute("a poster"))["success"]
    assert str(seen[0].url) == "https://relay.test/v1/images/generations"
    assert (seen[0].headers["x-tenant"], seen[0].headers["authorization"]) == ("t1", "Bearer sk-c")


@pytest.mark.parametrize(
    ("build", "call", "path"),
    [
        (lambda c: ImageGenerateTool(c, workspace=None), "a poster", "/v1/images/generations"),
        (lambda c: SpeechGenerateTool(c), "read me", "/v1/chat/completions"),
        (lambda c: VideoGenerateTool(c), "a river", "/v1/videos"),
    ],
    ids=["image", "speech", "video"],
)
async def test_the_section_headers_ride_every_media_request(monkeypatch, build, call, path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, text="stop here")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    section = SimpleNamespace(
        api_base="https://relay.test/v1", api_key="k", model="gpt-image-2", extra_headers={"X-Tenant": "t1"}
    )
    await build(section).execute(call)
    assert seen and seen[0].url.path == path
    assert (seen[0].headers["x-tenant"], seen[0].headers["authorization"]) == ("t1", "Bearer k")


async def test_a_section_header_replaces_the_key_s_in_any_case(monkeypatch) -> None:
    """Header names compare without case, so a section's ``authorization`` replaces
    the bearer the key builds instead of travelling beside it as a second one."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, text="stop here")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    section = SimpleNamespace(
        api_base="https://relay.test/v1", api_key="k", model="gpt-image-2", extra_headers={"authorization": "Bearer h"}
    )
    await ImageGenerateTool(section, workspace=None).execute("a poster")
    assert seen[0].headers.get_list("authorization") == ["Bearer h"]


def test_the_section_headers_stay_home_with_the_key() -> None:
    """They can be credentials too, so a poll or content URL on another origin gets
    neither them nor the key."""
    tool = VideoGenerateTool(
        SimpleNamespace(api_base="https://api.mycorp.example", model="", api_key="k", extra_headers={"X-Key": "s"})
    )
    headers = tool._headers()
    assert headers["X-Key"] == "s"
    assert tool._api_headers_for("https://api.mycorp.example/v1/x", headers) == headers
    assert tool._api_headers_for("https://cdn.elsewhere.test/x", headers) is None


async def test_a_section_on_a_named_provider_sends_the_id_that_provider_serves(monkeypatch, tmp_path) -> None:
    """A provider's models are listed under its own prefix (``openai/gpt-image-2``) and
    its Images API knows the bare name, so the prefix comes off on the way out -- the
    way the embedding pin's does -- and the call goes to the provider's own address."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    section = SimpleNamespace(
        provider="openai", api_base="https://api.openai.com/v1", api_key="sk-openai", model="openai/gpt-image-2"
    )
    tool = ImageGenerateTool(section, workspace=tmp_path / "ws")
    out = json.loads(await tool.execute("a poster", aspect_ratio="16:9", quality="low"))
    assert out["success"] and out["model"] == "gpt-image-2"
    assert [str(r.url) for r in seen] == ["https://api.openai.com/v1/images/generations"]
    assert seen[0].headers["authorization"] == "Bearer sk-openai"
    body = json.loads(seen[0].content)
    assert (body["model"], body["size"], body["quality"]) == ("gpt-image-2", "1536x1024", "low")


async def test_a_section_on_volcengine_speaks_ark_on_the_address_it_borrows(monkeypatch, tmp_path) -> None:
    """``provider: volcengine`` lends VolcEngine's address and key, and that address
    is Volces Ark: the frame goes out as Ark's explicit pair, and the picture is
    fetched from the URL the answer carries, without the key."""
    from raven.config.loader import load_config

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v3/images/generations":
            return httpx.Response(200, json={"data": [{"url": "https://1.2.3.4/tos/signed.jpeg"}]})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_JPG)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    config = load_config(tmp_path / "absent.json")
    config.providers.volcengine.api_key = "ark-key"
    config.tools.media.image.model = "volcengine/doubao-seedream-5-0-pro"
    config.tools.media.image.provider = "volcengine"
    tool = ImageGenerateTool(config.effective_media_config().image, workspace=tmp_path / "ws")
    out = json.loads(await tool.execute("a poster", aspect_ratio="16:9"))
    assert out["success"] and Path(out["paths"][0]).read_bytes() == _JPG
    generation, download = seen
    assert str(generation.url) == "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    assert generation.headers["authorization"] == "Bearer ark-key"
    body = json.loads(generation.content)
    assert body == {"model": "doubao-seedream-5-0-pro", "prompt": "a poster", "n": 1, "size": "2736x1539"}
    assert download.headers.get("authorization") is None


async def test_an_edit_on_an_ark_base_is_refused_before_anything_is_sent(monkeypatch, tmp_path) -> None:
    """Ark has no ``/images/edits`` route, so a call with references is answered
    there in so many words instead of with the 404 the edit form would draw."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, text="Not Found")

    tool = _image_tool(
        monkeypatch,
        handler,
        model="doubao-seedream-5-0-pro",
        workspace=tmp_path / "ws",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
    )
    ref = tmp_path / "logo.png"
    ref.write_bytes(_PNG)
    out = json.loads(await tool.execute("blend the logo in", images=[str(ref)]))
    assert seen == []
    assert "edit" in out["error"] and out["model"] == "doubao-seedream-5-0-pro"


# ── the image path: several pictures in one call ──


def _chat_reply(*, count: int = 1) -> dict:
    return {"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64," + _B64}}] * count}}]}


async def _refuse_every_fetch(_client, _url, *, what="") -> None:
    """Stand in for ``guarded_fetch`` judging a target private: it answers None,
    which is the branch that names the refused address."""
    return None


def _prompt_of(request: httpx.Request) -> str:
    content = json.loads(request.content)["messages"][0]["content"]
    return content if isinstance(content, str) else content[0]["text"]


async def test_one_prompt_still_answers_exactly_the_way_it_always_did(monkeypatch, tmp_path) -> None:
    """The single-prompt form is what every existing caller sends, the deck engine
    included; a batch answer reaching it would be read as a failed generation."""
    tool = _image_tool(
        monkeypatch,
        lambda _r: httpx.Response(200, json=_chat_reply()),
        model="google/gemini-2.5-flash-image",
        workspace=tmp_path / "ws",
    )

    out = json.loads(await tool.execute("a poster"))

    assert set(out) == {"success", "model", "quality", "paths"}
    assert out["success"] and len(out["paths"]) == 1
    # A prompts list carrying exactly one picture is the same one ask, so it is
    # answered in the same shape rather than as a batch of one.
    single = json.loads(await tool.execute(prompts=[{"prompt": "a poster"}]))
    assert set(single) == {"success", "model", "quality", "paths"}


async def test_a_batch_comes_back_as_every_path_in_one_call(monkeypatch, tmp_path) -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(_prompt_of(request))
        return httpx.Response(200, json=_chat_reply())

    tool = _image_tool(monkeypatch, handler, model="google/gemini-2.5-flash-image", workspace=tmp_path / "ws")
    out = json.loads(
        await tool.execute(prompts=[{"prompt": "cover"}, {"prompt": "chapter one"}, {"prompt": "closing"}])
    )

    assert sorted(asked) == ["chapter one", "closing", "cover"]
    assert out["success"] and len(out["paths"]) == 3 and "failed" not in out
    assert [item["index"] for item in out["results"]] == [0, 1, 2]
    assert [item["prompt"] for item in out["results"]] == ["cover", "chapter one", "closing"]
    assert [len(item["paths"]) for item in out["results"]] == [1, 1, 1]
    assert all(Path(p).read_bytes() == _PNG for p in out["paths"])


async def test_a_top_level_prompt_is_generated_as_the_first_picture_of_the_batch(monkeypatch, tmp_path) -> None:
    tool = _image_tool(
        monkeypatch,
        lambda _r: httpx.Response(200, json=_chat_reply()),
        model="google/gemini-2.5-flash-image",
        workspace=tmp_path / "ws",
    )

    out = json.loads(await tool.execute("cover", prompts=[{"prompt": "closing"}]))

    assert [item["prompt"] for item in out["results"]] == ["cover", "closing"]


async def test_one_picture_failing_does_not_take_the_batch_with_it(monkeypatch, tmp_path) -> None:
    """The reason a batch exists is that eight pictures are one wait; a ninth that
    the provider refuses must not throw the eight away."""

    def handler(request: httpx.Request) -> httpx.Response:
        if _prompt_of(request) == "chapter one":
            return httpx.Response(500, text="upstream exploded")
        return httpx.Response(200, json=_chat_reply())

    tool = _image_tool(monkeypatch, handler, model="google/gemini-2.5-flash-image", workspace=tmp_path / "ws")
    out = json.loads(
        await tool.execute(prompts=[{"prompt": "cover"}, {"prompt": "chapter one"}, {"prompt": "closing"}])
    )

    assert out["success"] and out["failed"] == 1
    assert len(out["paths"]) == 2
    assert "paths" in out["results"][0] and "paths" in out["results"][2]
    assert "500" in out["results"][1]["error"] and "paths" not in out["results"][1]
    assert "1 of 3" in out["note"]


async def test_a_picture_that_raises_is_that_picture_s_answer(monkeypatch, tmp_path) -> None:
    """A reference outside a confined workspace raises; that picture wears the
    refusal as its own result instead of taking every sibling down with it. The
    reason rides in ``detail``, because ``error`` is the streak's classification
    key and a path interpolated into it would split one bad reference per file."""
    outside = tmp_path / "out.png"
    outside.write_bytes(_PNG)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = _image_tool(
        monkeypatch,
        lambda _r: httpx.Response(200, json=_chat_reply()),
        model="google/gemini-2.5-flash-image",
        workspace=workspace,
    )
    tool._restrict_to_workspace = True

    out = json.loads(await tool.execute(prompts=[{"prompt": "cover"}, {"prompt": "logo", "images": [str(outside)]}]))

    assert out["success"] and out["failed"] == 1 and len(out["paths"]) == 1
    assert out["results"][1]["error"] == "could not read input image"
    assert "outside the workspace" in out["results"][1]["detail"]


async def test_the_pictures_of_a_batch_are_in_flight_at_once(monkeypatch, tmp_path) -> None:
    """Serially, eight backdrops measured 645 seconds of pure waiting. What makes a
    batch worth having is that the calls overlap, so that is what is asserted."""
    inflight, peak = 0, 0
    released = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        if inflight >= 3:
            released.set()
        await released.wait()
        inflight -= 1
        return httpx.Response(200, json=_chat_reply())

    tool = _image_tool(monkeypatch, handler, model="google/gemini-2.5-flash-image", workspace=tmp_path / "ws")
    out = json.loads(
        await asyncio.wait_for(
            tool.execute(prompts=[{"prompt": "one"}, {"prompt": "two"}, {"prompt": "three"}]), timeout=10
        )
    )

    assert peak == 3 and len(out["paths"]) == 3


async def test_more_pictures_than_the_cap_wait_their_turn(monkeypatch, tmp_path) -> None:
    """One key answers a wide fan-out with 429s, so the batch holds at
    _BATCH_CONCURRENCY in flight and the rest queue behind it."""
    inflight, peak = 0, 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return httpx.Response(200, json=_chat_reply())

    tool = _image_tool(monkeypatch, handler, model="google/gemini-2.5-flash-image", workspace=tmp_path / "ws")
    out = json.loads(await tool.execute(prompts=[{"prompt": f"page {i}"} for i in range(9)]))

    assert len(out["paths"]) == 9
    assert peak == media_gen._BATCH_CONCURRENCY


async def test_a_picture_overrides_only_what_it_names(monkeypatch, tmp_path) -> None:
    """The call's aspect_ratio and quality are the batch's; an item that names its
    own overrules them for itself alone."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": _B64, "media_type": "image/png"}]})

    tool = _image_tool(monkeypatch, handler, model="openai/gpt-image-2", workspace=tmp_path / "ws")
    out = json.loads(
        await tool.execute(
            aspect_ratio="16:9",
            quality="low",
            prompts=[{"prompt": "wide"}, {"prompt": "tall", "aspect_ratio": "3:4", "quality": "high"}],
        )
    )

    assert len(out["paths"]) == 2
    sent = {body["prompt"]: (body["aspect_ratio"], body["quality"]) for body in seen}
    assert sent == {"wide": ("16:9", "low"), "tall": ("3:4", "high")}


async def test_a_named_file_keeps_its_name_and_cannot_leave_the_output_dir(monkeypatch, tmp_path) -> None:
    """A batch of eight uuid-named files tells the caller nothing about which is
    which. The name is the model's, so a directory inside it is dropped."""
    tool = _image_tool(
        monkeypatch,
        lambda _r: httpx.Response(200, json=_chat_reply(count=2)),
        model="google/gemini-2.5-flash-image",
        workspace=tmp_path / "ws",
    )

    out = json.loads(
        await tool.execute(
            output_dir="art",
            prompts=[{"prompt": "cover", "filename": "cover.png"}, {"prompt": "end", "filename": "../../escape.png"}],
        )
    )

    made = sorted(Path(p) for p in out["paths"])
    assert [p.name for p in made] == ["cover-2.png", "cover.png", "escape-2.png", "escape.png"]
    assert {p.parent for p in made} == {(tmp_path / "ws" / "art").resolve()}


def _distinct_payloads(monkeypatch, tmp_path, *, workspace: str = "ws"):
    """Every answer carries a different picture, so a file written over is visible
    as content that went missing rather than only as a path returned twice."""
    seen = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        payload = base64.b64encode(f"picture-{seen['n']}".encode()).decode()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64," + payload}}]}}]},
        )

    return _image_tool(monkeypatch, handler, model="google/gemini-2.5-flash-image", workspace=tmp_path / workspace)


async def test_one_filename_shared_by_a_batch_still_makes_a_file_each(monkeypatch, tmp_path) -> None:
    """The call's top-level `filename` falls through to every item that named none,
    so the whole batch aimed at one path and all but the last picture was written
    over -- with the same path handed back for each of them."""
    tool = _distinct_payloads(monkeypatch, tmp_path)

    out = json.loads(await tool.execute(filename="shared.png", prompts=[{"prompt": "one"}, {"prompt": "two"}]))

    assert [Path(p).name for p in out["paths"]] == ["shared.png", "shared-2.png"]
    made = tmp_path / "ws" / "generated"
    assert (made / "shared.png").read_bytes() == b"picture-1"
    assert (made / "shared-2.png").read_bytes() == b"picture-2"


async def test_two_items_asking_for_one_name_are_told_apart_by_their_place(monkeypatch, tmp_path) -> None:
    """Naming the same file twice collides the same way. The picture that keeps the
    bare name is the earlier one in `prompts` rather than whichever answered first,
    and the one that moves takes the number of its place in the batch -- the same
    number its `results` entry is filed under -- so the suffix says which prompt it
    came from. A name only one picture asked for is left exactly as it was asked."""
    tool = _distinct_payloads(monkeypatch, tmp_path)

    out = json.loads(
        await tool.execute(
            prompts=[
                {"prompt": "a", "filename": "dup.png"},
                {"prompt": "b", "filename": "keep.png"},
                {"prompt": "c", "filename": "dup.png"},
            ]
        )
    )

    assert [Path(p).name for p in out["paths"]] == ["dup.png", "keep.png", "dup-3.png"]
    assert [r["index"] for r in out["results"]] == [0, 1, 2]
    assert len(set(out["paths"])) == 3


async def test_a_name_already_on_disk_is_numbered_rather_than_written_over(monkeypatch, tmp_path) -> None:
    """The uuid names this tool used before `filename` existed could never land on
    anything; a name the caller chooses can, and the workspace it writes into is
    the one the user keeps their own files in."""
    tool = _distinct_payloads(monkeypatch, tmp_path)
    settled = tmp_path / "ws" / "generated"
    settled.mkdir(parents=True)
    (settled / "hero.png").write_bytes(b"a file the user already had")

    out = json.loads(await tool.execute(prompt="hero shot", filename="hero.png"))

    assert [Path(p).name for p in out["paths"]] == ["hero-2.png"]
    assert (settled / "hero.png").read_bytes() == b"a file the user already had"


async def test_a_call_with_no_prompt_at_all_says_what_to_send(monkeypatch, tmp_path) -> None:
    tool = _image_tool(
        monkeypatch,
        lambda _r: httpx.Response(200, json=_chat_reply()),
        model="google/gemini-2.5-flash-image",
        workspace=tmp_path / "ws",
    )

    assert "nothing to generate" in json.loads(await tool.execute())["error"]
    blank = json.loads(await tool.execute(prompts=[{"prompt": "a"}, {"prompt": "  "}]))
    assert blank["error"] == "a picture has no prompt" and blank["index"] == 1


# ── the speech path: what the stream carries, and what the file becomes ──


def _sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def _audio_delta(*, data: bytes | None = None, transcript: str | None = None) -> dict:
    audio: dict[str, str] = {}
    if data is not None:
        audio["data"] = base64.b64encode(data).decode()
    if transcript is not None:
        audio["transcript"] = transcript
    return {"choices": [{"delta": {"audio": audio}}]}


def _speech_tool(monkeypatch: pytest.MonkeyPatch, body: str, status: int = 200) -> SpeechGenerateTool:
    """A speech tool whose HTTP client answers with ``body`` and nothing else."""
    transport = httpx.MockTransport(lambda _request: httpx.Response(status, content=body.encode()))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        media_gen.httpx,
        "AsyncClient",
        lambda *_a, **_kw: real_client(transport=transport),
    )
    return SpeechGenerateTool(SimpleNamespace(api_base="https://api.test", model="", api_key="k"))


async def test_the_pcm_chunks_concatenate_and_the_transcript_joins(monkeypatch) -> None:
    tool = _speech_tool(
        monkeypatch,
        _sse(
            _audio_delta(data=b"\x01\x02", transcript="hello "),
            _audio_delta(data=b"\x03\x04"),
            {"choices": [{"delta": {}}]},
            _audio_delta(transcript="there"),
        ),
    )

    pcm, transcript = await tool._stream_audio_pcm({"model": "m"})

    assert pcm == b"\x01\x02\x03\x04"
    assert transcript == "hello there"


async def test_a_line_that_is_not_an_event_is_skipped_rather_than_fatal(monkeypatch) -> None:
    tool = _speech_tool(
        monkeypatch,
        ": keep-alive\n\n" + "data: {not json\n\n" + _sse(_audio_delta(data=b"\x05\x06")),
    )

    pcm, transcript = await tool._stream_audio_pcm({"model": "m"})

    assert pcm == b"\x05\x06"
    assert transcript == ""


async def test_an_error_status_raises_with_the_body_loaded(monkeypatch) -> None:
    tool = _speech_tool(monkeypatch, '{"error": "no such model"}', status=404)

    with pytest.raises(httpx.HTTPStatusError):
        await tool._stream_audio_pcm({"model": "m"})


def test_the_wav_declares_the_rate_the_model_emits(tmp_path: Path) -> None:
    """OpenRouter hands over raw pcm16 with no header; these three numbers are
    the whole of what makes it playable."""
    tool = SpeechGenerateTool(SimpleNamespace(api_base="https://api.test", model=""))
    out = tmp_path / "speech.wav"

    tool._write_wav(out, b"\x00\x01" * 240)

    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == 24_000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 240


def test_a_config_callable_is_read_per_call_not_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live form: the loop passes a reader over the config file, so a key
    added or rotated there serves the next call with no re-registration."""
    from raven.config.schema import MediaToolConfig

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    holder = {"cfg": MediaToolConfig()}
    tool = ImageGenerateTool(lambda: holder["cfg"])
    assert tool.api_key == ""

    holder["cfg"] = MediaToolConfig(api_key="sk-added-later", model="some/model")
    assert tool.api_key == "sk-added-later"
    assert tool._model(None) == "some/model"


def test_a_plain_config_section_stays_a_snapshot() -> None:
    from raven.config.schema import MediaToolConfig

    tool = ImageGenerateTool(MediaToolConfig(api_key="sk-static"))
    assert tool.api_key == "sk-static"


@pytest.mark.parametrize(
    "model",
    [
        "bytedance-seed/seedream-5-0-lite",
        "bytedance-seed/seedream-5-0-pro",
        "qwen/qwen-image-3",
        "qwen/qwen-image-3-pro",
        "x-ai/grok-imagine-image-2.0",
        "black-forest-labs/flux.2-pro",
        "recraft/recraft-v4-vector",
        "vendor/mai-image-2",
        "vendor/krea-1",
        "vendor/riverflow-2",
        "vendor/muse-image",
    ],
)
async def test_image_families_route_directly_without_gpt_quality(monkeypatch, tmp_path, model):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(monkeypatch, handler, model=model, workspace=tmp_path)
    result = json.loads(await tool.execute("a circle", quality="high", aspect_ratio="9:16"))
    assert result["success"]
    assert len(seen) == 1 and seen[0].url.path == "/api/v1/images"
    body = json.loads(seen[0].content)
    assert "quality" not in body
    if any(name in model for name in ("seedream", "qwen-image", "grok-imagine")):
        assert body["aspect_ratio"] == "9:16"
    else:
        assert "aspect_ratio" not in body


async def test_a_chat_routed_model_is_asked_for_the_frame_through_image_config(monkeypatch, tmp_path):
    """OpenRouter's chat route takes the frame as ``image_config.aspect_ratio``;
    a chat-routed model given nothing answers in its own default frame, which
    for Nano Banana was 1408x768 whatever ratio the caller asked for. No ratio
    asked, no key sent."""
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        message = {"images": [{"image_url": {"url": "data:image/png;base64," + _B64}}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    tool = _image_tool(monkeypatch, handler, model="google/gemini-3.1-flash-image", workspace=tmp_path)
    assert json.loads(await tool.execute("a lantern", aspect_ratio="9:16"))["success"]
    assert seen[-1]["image_config"] == {"aspect_ratio": "9:16"}
    assert json.loads(await tool.execute("a lantern"))["success"]
    assert "image_config" not in seen[-1]


@pytest.mark.parametrize("chat", [False, True])
@pytest.mark.parametrize(
    "mime,extension,data",
    [
        ("image/jpeg", ".jpg", b"\xff\xd8\xffpixels"),
        ("image/svg+xml", ".svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
        ("image/webp", ".webp", b"RIFFpixelsWEBP"),
        ("image/png", ".png", _PNG),
    ],
)
async def test_image_output_uses_the_response_mime(monkeypatch, tmp_path, chat, mime, extension, data):
    encoded = base64.b64encode(data).decode()

    def handler(request):
        if chat:
            message = {"images": [{"image_url": {"url": f"data:{mime};base64,{encoded}"}}]}
            return httpx.Response(200, json={"choices": [{"message": message}]})
        return httpx.Response(200, json={"data": [{"b64_json": encoded, "media_type": mime}]})

    model = "google/gemini-3.1-flash-image" if chat else "openai/gpt-image-2"
    tool = _image_tool(monkeypatch, handler, model=model, workspace=tmp_path)
    result = json.loads(await tool.execute("a circle"))
    path = Path(result["paths"][0])
    assert path.suffix == extension and path.read_bytes() == data


async def test_image_selection_is_live_and_settings_win(monkeypatch, tmp_path):
    from raven.config.schema import MediaToolConfig

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(monkeypatch, handler, model="", workspace=tmp_path)
    holder = {"config": MediaToolConfig(api_key="k")}
    tool._config_source = lambda: holder["config"]
    await tool.execute("a circle")
    assert seen[-1]["model"] == "openai/gpt-image-2.5-sunburst"
    assert "quality" not in seen[-1]
    holder["config"] = MediaToolConfig(api_key="k", model="openai/gpt-image-2", quality="low")
    await tool.execute("a circle")
    assert seen[-1]["quality"] == "low"
    await tool.execute("a circle", quality="high")
    assert seen[-1]["quality"] == "low"
    await tool.execute("a circle", model="qwen/qwen-image-3")
    assert seen[-1]["model"] == "openai/gpt-image-2"
    holder["config"] = MediaToolConfig(api_key="k", model="qwen/qwen-image-3")
    await tool.execute("a circle", model="openai/gpt-image-2", quality="high")
    assert seen[-1]["model"] == "qwen/qwen-image-3" and "quality" not in seen[-1]
    holder["config"].quality = ""
    await tool.execute("a circle")
    assert "quality" not in seen[-1]
    holder["config"] = MediaToolConfig(api_key="k", quality="")
    await tool.execute("a circle")
    assert seen[-1]["model"] == "openai/gpt-image-2.5-sunburst" and "quality" not in seen[-1]


async def test_borrowed_image_selection_updates_without_recreating_tool(monkeypatch, tmp_path):
    from raven.config.schema import MediaToolConfig

    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    source = tmp_path / "host.json"

    def configure(base, key, model, quality):
        source.write_text(
            json.dumps(
                {
                    "tools": {
                        "media": {
                            "image": {
                                "apiBase": base,
                                "apiKey": key,
                                "model": model,
                                "quality": quality,
                            }
                        }
                    }
                }
            )
        )

    configure("https://custom.example/v1", "custom-key", "openai/gpt-image-2", "low")
    tool = _image_tool(monkeypatch, handler, model="", workspace=tmp_path)
    tool._config_static = MediaToolConfig(api_key="worker-key", selection_config=str(source))
    result = json.loads(await tool.execute("a circle", model="qwen/qwen-image-3", quality="high"))
    assert result["model"] == "openai/gpt-image-2" and result["quality"] == "low"
    assert seen[-1][:2] == ("https://custom.example/v1/images/generations", "Bearer custom-key")
    assert seen[-1][2]["quality"] == "low"
    configure("https://openrouter.ai/api/v1", "router-key", "openrouter/openai/gpt-image-2.5-sunburst", "low")
    result = json.loads(await tool.execute("a circle", model="google/gemini-3-pro-image", quality="high"))
    assert result["model"] == "openai/gpt-image-2.5-sunburst"
    assert seen[-1][:2] == ("https://openrouter.ai/api/v1/images", "Bearer router-key")
    assert seen[-1][2]["model"] == "openai/gpt-image-2.5-sunburst"
    assert seen[-1][2]["quality"] == "low"
    assert json.loads(source.read_text())["tools"]["media"]["image"]["model"] == (
        "openrouter/openai/gpt-image-2.5-sunburst"
    )
    configure("https://openrouter.ai/api/v1", "rotated-key", "qwen/qwen-image-3", "")
    result = json.loads(await tool.execute("a circle", model="openai/gpt-image-2", quality="high"))
    assert result["model"] == "qwen/qwen-image-3" and result["quality"] == ""
    assert "quality" not in seen[-1][2]
    source.write_text("{")
    await tool.execute("a circle")
    assert seen[-1][2]["model"] == "qwen/qwen-image-3"


async def test_image_provider_refusal_preserves_reason(monkeypatch, tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": None},
                        "finish_reason": "content_filter",
                        "native_finish_reason": "IMAGE_RECITATION",
                    }
                ]
            },
        )

    tool = _image_tool(monkeypatch, handler, model="google/gemini-3.1-flash-image", workspace=tmp_path)
    result = json.loads(await tool.execute("a circle"))
    assert result["error"] == "image generation refused"
    assert result["native_finish_reason"] == "IMAGE_RECITATION"
    assert result["retryable"] is False
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("route", ["chat", "openrouter", "generation", "edit", "fallback"])
@pytest.mark.parametrize("cost", [None, 0, 0.12])
async def test_image_usage_reaches_runtime_tracker_and_telemetry(monkeypatch, tmp_path, route, cost):
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import EngineWiring, ToolWiring
    from raven.config.schema import MediaGenConfig, MediaToolConfig
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.token_wise.registry import StrategyRegistry
    from raven.token_wise.usage_tracker import UsageTracker

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    model = "google/test-image" if route in ("chat", "fallback") else "openai/gpt-image-2"
    base = "https://openrouter.ai/api/v1" if route in ("openrouter", "fallback") else "https://gateway.test/v1"
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if route == "fallback" and request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                404, json={"error": {"message": "This image model is not available on chat/completions; use /images"}}
            )
        chat = request.url.path.endswith("/chat/completions")
        data = (
            {"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64," + _B64}}]}}]}
            if chat
            else {"data": [{"b64_json": _B64}]}
        )
        if cost is not None:
            data["usage"] = {
                ("prompt_tokens" if chat or route in ("openrouter", "fallback") else "input_tokens"): 100,
                ("completion_tokens" if chat or route in ("openrouter", "fallback") else "output_tokens"): 20,
                ("prompt_tokens_details" if chat or route in ("openrouter", "fallback") else "input_tokens_details"): {
                    "cached_tokens": 60,
                    "cache_write_tokens": 10,
                },
                "cost": cost,
            }
        return httpx.Response(200, json=data)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    tracker = UsageTracker()
    agent = AgentLoop(
        provider=LiteLLMProvider(default_model="test"),
        workspace=tmp_path / "workspace",
        engine=EngineWiring(strategies=StrategyRegistry([tracker])),
        tools=ToolWiring(
            media_config=MediaGenConfig(image=MediaToolConfig(api_key="test", api_base=base, model=model))
        ),
    )
    tool = agent.tools.get("image_generate")
    refs = None
    if route == "edit":
        reference = tmp_path / "reference.png"
        reference.write_bytes(_PNG)
        refs = [str(reference)]
    result = json.loads(await tool.execute("a circle", images=refs))
    assert result.get("success"), result
    rows = [
        json.loads(line) for p in (tmp_path / "telemetry").glob("usage-*.jsonl") for line in p.read_text().splitlines()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == model and row["cost_usd"] == cost
    assert row["input_tokens"] == (30 if cost is not None else None)
    assert row["output_tokens"] == (20 if cost is not None else None)
    assert row["cache_read_tokens"] == (60 if cost is not None else None)
    assert row["cache_write_tokens"] == (10 if cost is not None else None)
    assert tracker.snapshot().cost_usd == cost


async def test_image_usage_is_recorded_before_file_write_failure(monkeypatch, tmp_path):
    from raven.token_wise.usage_tracker import UsageTracker

    def handler(request):
        return httpx.Response(200, json={"data": [{"b64_json": _B64}], "usage": {"cost": 0.2}})

    tracker = UsageTracker(telemetry_dir=tmp_path / "telemetry")
    tool = _image_tool(monkeypatch, handler, model="openai/gpt-image-2", workspace=tmp_path)

    async def record(usage):
        await tracker.after_llm_call({}, usage)

    tool._usage_recorder = record

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", fail)
    with pytest.raises(OSError, match="disk full"):
        await tool.execute("a circle")
    assert tracker.snapshot().cost_usd == 0.2
    assert tracker.snapshot().calls == 1


@pytest.mark.parametrize("prompt,completion", [(0, 4175), (16, 272)])
def test_images_endpoint_accepts_openrouter_chat_style_usage(prompt, completion):
    from raven.providers.usage import image_usage

    usage = image_usage(
        {
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "cost": 0.04,
            }
        },
        "images",
    )
    assert usage["input_tokens"] == prompt
    assert usage["output_tokens"] == completion
    assert usage["cost_usd"] == 0.04
    assert usage["cache_read_tokens"] is None
    assert usage["cache_write_tokens"] is None


# ── what the loop reads off these envelopes ──


def _class_of(answer: str) -> str:
    return failure_class(answer)


async def test_one_dead_endpoint_is_one_streak_class(monkeypatch, tmp_path) -> None:
    """The loop breaks a tool-failure loop at two, keyed on ``(tool, failure_class)``,
    and an envelope is classified by its ``error`` string alone. A vendor body carries
    a request id, so a body interpolated into that key gave every call its own class
    and the stop-repeating nudge was unreachable for this whole tool."""
    bodies = iter(
        [
            '{"error":{"message":"upstream unavailable","request_id":"req_aaa111"}}',
            '{"error":{"message":"upstream unavailable","request_id":"req_bbb222"}}',
        ]
    )
    tool = _image_tool(
        monkeypatch,
        lambda _r: httpx.Response(500, text=next(bodies)),
        model="google/gemini-2.5-flash-image",
        workspace=tmp_path / "ws",
    )

    first = await tool.execute("a circle")
    second = await tool.execute("a square")

    assert _class_of(first) == _class_of(second)
    # Not vacuous: both have to COUNT as failures for the streak to reach two at all.
    assert is_hard_tool_failure(first) and is_hard_tool_failure(second)
    # And the body the model needs is still in front of it, one key over.
    assert "req_aaa111" in json.loads(first)["detail"]


async def test_two_different_statuses_stay_two_classes(monkeypatch, tmp_path) -> None:
    """The control on the test above, and the failure mode it must not cause: a model
    that hit a missing model and then a server fault has changed what it is doing, and
    folding those together would fire the nudge at a model still adapting."""
    statuses = iter([404, 500])
    tool = _image_tool(
        monkeypatch,
        lambda _r: httpx.Response(next(statuses), text='{"error":"no"}'),
        model="google/gemini-2.5-flash-image",
        workspace=tmp_path / "ws",
    )

    assert _class_of(await tool.execute("a circle")) != _class_of(await tool.execute("a square"))


async def test_a_reference_it_cannot_read_is_one_class_across_files(monkeypatch, tmp_path) -> None:
    """The same rule where the variable part is a path rather than a request id."""
    tool = _image_tool(
        monkeypatch,
        lambda _r: httpx.Response(200, json=_chat_reply()),
        model="google/gemini-2.5-flash-image",
        workspace=tmp_path / "ws",
    )

    first = await tool.execute("a circle", images=[str(tmp_path / "gone-a.png")])
    second = await tool.execute("a square", images=[str(tmp_path / "gone-b.png")])

    assert _class_of(first) == _class_of(second)
    assert "gone-a.png" in json.loads(first)["detail"]


async def test_two_transports_dying_the_same_way_are_one_class(tmp_path) -> None:
    """The catch-all, where the exception's own text spells the host it failed to
    reach. Two hosts behind one dead network are one cause; a different exception
    type is not, which is the other half the type keeps apart."""

    async def dies(exc: Exception, prompt: str) -> str:
        # Each transport gets its own patch window: patching over a live patch
        # would leave every tool here sharing the first one's transport.
        with pytest.MonkeyPatch.context() as mp:

            def handler(_r: httpx.Request) -> httpx.Response:
                raise exc

            tool = _image_tool(mp, handler, model="google/gemini-2.5-flash-image", workspace=tmp_path / "ws")
            return await tool.execute(prompt)

    first = await dies(httpx.ConnectError("[Errno 61] Connection refused to a.example"), "a circle")
    second = await dies(httpx.ConnectError("[Errno 61] Connection refused to b.example"), "a square")
    other = await dies(httpx.ReadError("the reader gave up"), "a star")

    assert _class_of(first) == _class_of(second)
    assert _class_of(first) != _class_of(other)
    assert "a.example" in json.loads(first)["detail"]


async def test_a_denied_call_still_names_the_proxy_it_needs(monkeypatch, tmp_path) -> None:
    """Splitting the envelope must not cost the operator the one thing a 403 is
    worth saying: the media calls can be routed through a configured proxy."""
    tool = _image_tool(
        monkeypatch,
        lambda _r: httpx.Response(403, text="denied"),
        model="google/gemini-2.5-flash-image",
        workspace=tmp_path / "ws",
    )

    answer = json.loads(await tool.execute("a circle"))

    assert answer["error"] == "HTTP 403"
    assert "tools.media.proxy" in answer["hint"]


def _raising_client(mp: pytest.MonkeyPatch, exc: Exception) -> None:
    """Every client this module builds dies the same way, whatever the route."""

    def _boom(*_a, **_kw):
        raise exc

    mp.setattr(media_gen.httpx, "AsyncClient", _boom)


async def test_the_images_route_classes_its_failures_like_the_chat_route(tmp_path) -> None:
    """A dedicated image model never touches chat/completions, so its own copies of
    both handlers need the same rule -- one that drifted would put the streak back
    where it was for every ``gpt-image`` call."""
    with pytest.MonkeyPatch.context() as mp:
        tool = _image_tool(
            mp,
            lambda _r: httpx.Response(200, json={"data": []}),
            model="openai/gpt-image-2",
            workspace=tmp_path / "ws",
        )
        unreadable = [await tool.execute("a poster", images=[str(tmp_path / f"gone-{n}.png")]) for n in ("a", "b")]

    assert len({failure_class(raw) for raw in unreadable}) == 1
    assert "gone-a.png" in json.loads(unreadable[0])["detail"]

    dead = []
    for host in ("alpha.example", "beta.example"):
        with pytest.MonkeyPatch.context() as mp:
            tool = _image_tool(
                mp, lambda _r: httpx.Response(200), model="openai/gpt-image-2", workspace=tmp_path / "ws"
            )
            _raising_client(mp, httpx.ConnectError(f"cannot reach {host}"))
            dead.append(await tool.execute("a poster"))

    assert len({failure_class(raw) for raw in dead}) == 1
    assert "alpha.example" in json.loads(dead[0])["detail"]


@pytest.mark.parametrize(
    "build,call",
    [
        (lambda: SpeechGenerateTool(SimpleNamespace(api_base="https://api.test", model="", api_key="k")), "read me"),
        (lambda: VideoGenerateTool(SimpleNamespace(api_base="https://api.test", model="", api_key="k")), "a river"),
    ],
    ids=["speech", "video"],
)
async def test_every_media_tool_reads_one_dead_network_as_one_class(build, call) -> None:
    """The streak key is ``(tool, failure_class)``, so each tool accumulates its own.
    Speech and video each carry their own catch-all, and an exception's text names the
    host it could not reach: left there, neither tool could ever reach the nudge."""
    answers = []
    for host in ("alpha.example", "beta.example"):
        with pytest.MonkeyPatch.context() as mp:
            _raising_client(mp, httpx.ConnectError(f"[Errno 61] cannot reach {host}"))
            answers.append(await build().execute(call))

    assert all(is_hard_tool_failure(raw) for raw in answers)
    assert len({failure_class(raw) for raw in answers}) == 1
    assert "alpha.example" in json.loads(answers[0])["detail"]


async def test_a_batched_403_names_the_proxy_the_single_call_names(monkeypatch, tmp_path) -> None:
    """``error`` carrying only the class is what the streak needs; it is not a
    reason to tell a batched picture less than a single one. The way out of a 403
    lives in ``hint`` now, so the reducer that folds eight answers into one has to
    carry it, or asking for eight backdrops hides the proxy setting that asking for
    one would have named."""

    def handler(request: httpx.Request) -> httpx.Response:
        if _prompt_of(request) == "chapter one":
            return httpx.Response(403, text="denied")
        return httpx.Response(200, json=_chat_reply())

    tool = _image_tool(monkeypatch, handler, model="google/gemini-2.5-flash-image", workspace=tmp_path / "ws")

    out = json.loads(await tool.execute(prompts=[{"prompt": "cover"}, {"prompt": "chapter one"}]))

    failed = out["results"][1]
    assert out["failed"] == 1 and failed["error"] == "HTTP 403"
    assert "tools.media.proxy" in failed["hint"]
    assert failed["detail"] == "denied"


async def test_a_batched_picture_is_told_which_reference_was_refused(monkeypatch, tmp_path) -> None:
    """The other key ``error`` was split into: the address a guarded fetch refused
    moved to ``url``, and a picture is one of several that could have carried a bad
    reference, so dropping it in the fold leaves the model with a refusal it cannot
    act on."""
    monkeypatch.setattr(media_gen, "guarded_fetch", _refuse_every_fetch)

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": _B64, "media_type": "image/png"}]})

    tool = _image_tool(
        monkeypatch,
        handler,
        model="openai/gpt-image-2",
        workspace=tmp_path / "ws",
        api_base="https://api.mycorp.example/v1",
    )

    out = json.loads(
        await tool.execute(
            prompts=[
                {"prompt": "cover"},
                {"prompt": "chapter one", "images": ["https://10.0.0.1/logo.png"]},
            ]
        )
    )

    failed = out["results"][1]
    assert out["failed"] == 1 and failed["error"] == "input image refused: not a fetchable public target"
    assert failed["url"] == "https://10.0.0.1/logo.png"


# ── MiniMax, a provider the image section can name ──


def _minimax_image_tool(monkeypatch, handler, workspace: Path, **section) -> ImageGenerateTool:
    """An image section on MiniMax as borrowing leaves it: the provider named, its
    address and key in place."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    recorder = section.pop("usage_recorder", None)
    fields = {"provider": "minimax", "api_base": "https://api.minimax.io/v1", "api_key": "mm-key", "model": "image-01"}
    return ImageGenerateTool(SimpleNamespace(**{**fields, **section}), workspace=workspace, usage_recorder=recorder)


def _minimax_answer(*, pictures: list[str] | None = None, failed: str = "0", **extra) -> httpx.Response:
    """MiniMax's image answer as its reference documents it, counts as strings."""
    pictures = [_B64] if pictures is None else pictures
    body = {
        "id": "trace-1",
        "data": {"image_base64": pictures},
        "metadata": {"success_count": str(len(pictures)), "failed_count": failed},
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }
    return httpx.Response(200, json={**body, **extra})


@pytest.mark.parametrize(
    ("provider", "model", "url"),
    [
        ("minimax", "minimax/image-01", "https://api.minimax.io/v1/image_generation"),
        ("minimax_cn_api", "image-01", "https://api.minimaxi.com/v1/image_generation"),
    ],
)
async def test_an_image_section_on_minimax_borrows_its_connection(monkeypatch, tmp_path, provider, model, url) -> None:
    """``provider: minimax`` lends MiniMax's address and key, and the picture is asked
    of MiniMax's own image route with the id the provider serves; the capability
    report reads the same key the tool sends."""
    from raven.agent.tools.capabilities import CAPABILITIES, configured_from, has_credential
    from raven.config.loader import load_config

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _minimax_answer()

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    config = load_config(tmp_path / "absent.json")
    getattr(config.providers, provider).api_key = "mm-key"
    config.tools.media.image.model = model
    config.tools.media.image.provider = provider
    tool = ImageGenerateTool(config.effective_media_config().image, workspace=tmp_path / "ws")

    out = json.loads(await tool.execute("a portrait", aspect_ratio="16:9"))
    assert out["success"] and Path(out["paths"][0]).read_bytes() == _PNG
    assert [str(r.url) for r in seen] == [url]
    assert seen[0].headers["authorization"] == "Bearer mm-key"
    assert json.loads(seen[0].content) == {
        "model": "image-01",
        "prompt": "a portrait",
        "n": 1,
        "response_format": "base64",
        "aspect_ratio": "16:9",
    }
    cap = next(c for c in CAPABILITIES if c.tool == "image_generate")
    assert has_credential(cap, config) and configured_from(cap, config) == f"borrowed: providers.{provider}.apiKey"


async def test_a_minimax_key_only_in_the_environment_is_no_key_for_the_image_section(monkeypatch, tmp_path) -> None:
    """The key travels with the provider's connection, so ``MINIMAX_API_KEY`` alone
    reaches neither the tool nor the capability report, and the answer says where
    the key belongs."""
    from raven.agent.tools.capabilities import CAPABILITIES, has_credential
    from raven.config.loader import load_config

    monkeypatch.setenv("MINIMAX_API_KEY", "mm-env")
    config = load_config(tmp_path / "absent.json")
    config.tools.media.image.model = "image-01"
    config.tools.media.image.provider = "minimax"
    tool = ImageGenerateTool(config.effective_media_config().image, workspace=tmp_path / "ws")
    cap = next(c for c in CAPABILITIES if c.tool == "image_generate")

    assert (tool.api_key, has_credential(cap, config)) == ("", False)
    assert "providers.minimax" in json.loads(await tool.execute("a portrait"))["error"]


async def test_a_minimax_address_with_no_provider_named_is_an_openai_compatible_base(monkeypatch, tmp_path) -> None:
    """MiniMax's own request shape follows the provider a section names, never its
    address: a section naming none is any other OpenAI-compatible base."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(monkeypatch, handler, model="image-01", workspace=tmp_path, api_base="https://api.minimax.io/v1")
    assert json.loads(await tool.execute("a portrait"))["success"]
    assert [r.url.path for r in seen] == ["/v1/images/generations"]


async def test_a_minimax_picture_keeps_one_character_and_a_local_one_travels_inline(monkeypatch, tmp_path) -> None:
    """MiniMax keeps a character from one reference image per request: of several
    the first is sent, a file on disk as a data URL."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    portrait = workspace / "portrait.png"
    portrait.write_bytes(_PNG)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _minimax_answer()

    tool = _minimax_image_tool(monkeypatch, handler, workspace)
    refs = [str(portrait), "https://example.test/second.png", "https://example.test/third.png"]
    out = json.loads(await tool.execute("in a garden", images=refs, aspect_ratio="3:4", filename="garden"))

    assert Path(out["paths"][0]).name == "garden.png"
    assert seen[0]["subject_reference"] == [{"type": "character", "image_file": f"data:image/png;base64,{_B64}"}]


async def test_a_minimax_batch_keeps_each_picture_s_own_reference(monkeypatch, tmp_path) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _minimax_answer()

    tool = _minimax_image_tool(monkeypatch, handler, tmp_path)
    out = json.loads(
        await tool.execute(
            prompts=[
                {"prompt": "first portrait", "images": ["https://example.test/first.png"]},
                {"prompt": "second portrait", "images": ["https://example.test/second.png"]},
            ]
        )
    )
    assert len(out["paths"]) == 2
    files = sorted(body["subject_reference"][0]["image_file"] for body in seen)
    assert files == ["https://example.test/first.png", "https://example.test/second.png"]


async def test_a_minimax_reference_outside_a_confined_workspace_is_refused_before_anything_is_sent(
    monkeypatch, tmp_path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("a reference the workspace forbids must not reach the API")

    tool = _minimax_image_tool(monkeypatch, handler, tmp_path / "ws")
    tool._restrict_to_workspace = True
    out = json.loads(await tool.execute("portrait", images=[str(tmp_path / "outside.png")]))
    assert "outside the workspace" in out["detail"]


@pytest.mark.parametrize(("code", "message"), [(1008, "insufficient balance"), (2013, "invalid params")])
async def test_a_minimax_status_is_the_error_class_and_its_message_the_detail(monkeypatch, tmp_path, code, message):
    """The vendor's sentence varies per call while the code does not, and the loop's
    failure streak keys on ``error``: the code is the class, the sentence the detail."""
    answer = httpx.Response(200, json={"base_resp": {"status_code": code, "status_msg": message}})
    tool = _minimax_image_tool(monkeypatch, lambda _r: answer, tmp_path)
    out = json.loads(await tool.execute("portrait"))
    assert out == {"error": f"MiniMax status {code}", "detail": message, "model": "image-01"}


@pytest.mark.parametrize(("failed", "error"), [("1", "image generation refused"), ("0", "no image returned")])
async def test_a_minimax_picture_its_safety_check_withheld_reads_as_refused(monkeypatch, tmp_path, failed, error):
    """``failed_count`` counts pictures MiniMax's content check withheld, which is a
    refusal to rephrase around rather than an empty answer to ask again for."""
    tool = _minimax_image_tool(monkeypatch, lambda _r: _minimax_answer(pictures=[], failed=failed), tmp_path)
    out = json.loads(await tool.execute("portrait"))
    assert (out["error"], out.get("retryable")) == (error, False if failed == "1" else None)


async def test_a_minimax_picture_is_counted_as_a_call(monkeypatch, tmp_path) -> None:
    """MiniMax reports no tokens, so the record carries none; the call still counts,
    as the other image routes' do."""
    recorded: list = []

    async def recorder(snapshot) -> None:
        recorded.append(snapshot)

    tool = _minimax_image_tool(monkeypatch, lambda _r: _minimax_answer(), tmp_path, usage_recorder=recorder)
    assert json.loads(await tool.execute("portrait"))["success"]
    assert [(s.model, s.input_tokens, s.output_tokens) for s in recorded] == [("image-01", None, None)]


@pytest.mark.parametrize(
    "picture_url,fetched", [("https://8.8.8.8/image.png", True), ("http://127.0.0.1/image.png", False)]
)
async def test_a_minimax_picture_url_is_fetched_guarded_and_without_the_key(
    monkeypatch, tmp_path, picture_url, fetched
):
    downloads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"image_urls": [picture_url]}, "base_resp": {"status_code": 0}})
        assert "authorization" not in request.headers
        downloads.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_PNG)

    tool = _minimax_image_tool(monkeypatch, handler, tmp_path)
    out = json.loads(await tool.execute("portrait"))
    if fetched:
        assert Path(out["paths"][0]).read_bytes() == _PNG and downloads == [picture_url]
    else:
        assert out["error"] == "generated, but the picture URL was refused or could not be fetched"
        assert (out["url"], downloads) == (picture_url, [])


async def test_a_minimax_picture_keeps_the_format_its_bytes_declare(monkeypatch, tmp_path) -> None:
    """MiniMax's base64 answer names no format, and its pictures are JPEG: the file
    takes the extension the bytes declare instead of one assumed for every picture."""
    jpeg = base64.b64encode(_JPG).decode()
    tool = _minimax_image_tool(monkeypatch, lambda _r: _minimax_answer(pictures=[jpeg]), tmp_path)
    out = json.loads(await tool.execute("portrait"))
    assert Path(out["paths"][0]).suffix == ".jpg" and Path(out["paths"][0]).read_bytes() == _JPG


def test_the_image_tool_says_what_references_do_on_the_provider_it_runs_on() -> None:
    """Edited or varied on most routes, kept as a character on MiniMax: the model reads
    the one its section's provider applies, per turn."""
    from raven.agent.tools.registry import ToolRegistry

    section = SimpleNamespace(provider="", api_base="", api_key="k", model="")
    registry = ToolRegistry()
    registry.register(ImageGenerateTool(lambda: section, workspace=None))

    def offered() -> dict:
        return next(d["function"] for d in registry.get_definitions() if d["function"]["name"] == "image_generate")

    assert "MiniMax" not in json.dumps(offered())
    section.provider, section.api_base = "minimax", "https://api.minimax.io/v1"
    assert "character" in offered()["parameters"]["properties"]["images"]["description"]
    assert "edit" not in offered()["description"].split(".")[0]


@pytest.mark.parametrize("tool_class", [ImageGenerateTool, VideoGenerateTool])
def test_a_media_tool_read_off_its_class_answers_the_default_schema(tool_class) -> None:
    """The class has no section, so no provider to pick by: it answers what a section
    on no named provider is offered, and a reader that takes an argument's spelling
    off the class still finds a schema there."""
    tool = tool_class(lambda: SimpleNamespace(provider="", api_base="", api_key="k", model=""))
    assert (tool_class.description, tool_class.parameters) == (tool.description, tool.parameters)


def test_a_plain_section_an_embedder_hands_in_for_video_is_a_video_section() -> None:
    """An embedder handing ``AgentLoop`` a plain ``MediaToolConfig`` for video keeps
    working now that the video section has a class of its own, as the image one did."""
    from raven.config.schema import MediaGenConfig, MediaToolConfig, VideoToolConfig

    media = MediaGenConfig(video=MediaToolConfig(api_key="k", model="kwaivgi/kling-v3.0-std"))
    assert isinstance(media.video, VideoToolConfig)
    assert (media.video.api_key, media.video.model, media.video.provider) == ("k", "kwaivgi/kling-v3.0-std", "")
