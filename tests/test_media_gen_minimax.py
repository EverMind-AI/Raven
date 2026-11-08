"""MiniMax's video API on the provider the video section names: the request each
model family takes, the poll and file handoff, and where the key may travel."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from raven.agent.tools import media_gen as media_gen_module
from raven.agent.tools.media_gen import VideoGenerateTool

_VIDEO_BYTES = b"generated-video"
# The download is judged before it connects, so the video sits at a public
# literal the judge passes without a DNS lookup.
_CDN = "https://1.2.3.4/video.mp4"
_PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


def _serve(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen_module.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    return seen


def _tool(tmp_path: Path, **section) -> VideoGenerateTool:
    """A video section on MiniMax as borrowing leaves it: the provider named, its
    address and key in place."""
    fields = {
        "provider": "minimax",
        "api_base": "https://api.minimax.io/v1",
        "api_key": "mm-key",
        "model": "MiniMax-H3",
    }
    restrict = section.pop("restrict_to_workspace", False)
    return VideoGenerateTool(
        SimpleNamespace(**{**fields, **section}), workspace=tmp_path, restrict_to_workspace=restrict
    )


def _v2(task: dict | None = None):
    """MiniMax's V2 flow: a ``task_id``, then a finished job whose video is on the CDN."""
    finished = task or {"id": "t2", "status": "succeeded", "content": {"url": _CDN}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "t2"})
        if request.url.path == "/v2/query/video_generation/t2":
            return httpx.Response(200, json={"task": finished})
        return httpx.Response(200, content=_VIDEO_BYTES)

    return handler


def _v1(base_resp_on_submit: dict | None = None):
    """MiniMax's V1 flow: a ``task_id``, a finished status naming a file, the file's link."""
    ok = {"status_code": 0, "status_msg": "success"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "t1", "base_resp": base_resp_on_submit or ok})
        if request.url.path == "/v1/query/video_generation":
            return httpx.Response(200, json={"task_id": "t1", "status": "Success", "file_id": "f1", "base_resp": ok})
        if request.url.path == "/v1/files/retrieve":
            return httpx.Response(200, json={"file": {"file_id": 1, "download_url": _CDN}, "base_resp": ok})
        return httpx.Response(200, content=_VIDEO_BYTES)

    return handler


async def test_a_text_only_h3_call_sends_the_text_and_a_ratio(monkeypatch, tmp_path) -> None:
    """Text-to-video on V2 takes a ratio and refuses ``adaptive``, so one is sent when
    the call names none; resolution and duration are required, and the defaults
    fit both V2 models."""
    seen = _serve(monkeypatch, _v2())
    out = json.loads(await _tool(tmp_path).execute(prompt="waves roll in"))

    assert out["success"] and Path(out["path"]).read_bytes() == _VIDEO_BYTES
    submit, query, download = seen
    assert str(submit.url) == "https://api.minimax.io/v2/video_generation"
    assert json.loads(submit.content) == {
        "model": "MiniMax-H3",
        "content": [{"type": "text", "text": "waves roll in"}],
        "resolution": "768P",
        "duration": 6,
        "ratio": "16:9",
    }
    assert str(query.url) == "https://api.minimax.io/v2/query/video_generation/t2"
    assert submit.headers["authorization"] == query.headers["authorization"] == "Bearer mm-key"
    assert str(download.url) == _CDN and "authorization" not in download.headers


async def test_a_first_frame_is_the_first_frame_item_and_the_frame_sets_the_ratio(monkeypatch, tmp_path) -> None:
    seen = _serve(monkeypatch, _v2())
    params = {"duration": 8, "resolution": "2K"}
    out = json.loads(
        await _tool(tmp_path).execute(prompt="it wakes", first_frame_image="https://img.example/a.png", params=params)
    )

    assert out["success"]
    assert json.loads(seen[0].content) == {
        "model": "MiniMax-H3",
        "content": [
            {"type": "text", "text": "it wakes"},
            {"type": "image_url", "image_url": {"url": "https://img.example/a.png"}, "role": "first_frame"},
        ],
        "resolution": "2K",
        "duration": 8,
    }


async def test_a_ratio_beside_a_first_frame_is_refused_before_anything_is_sent(monkeypatch, tmp_path) -> None:
    """MiniMax takes the frame's own shape and ignores any other ratio without a
    word, so the call is told so instead of being answered with a video that is
    not the shape it asked for."""
    seen = _serve(monkeypatch, _v2())
    out = json.loads(
        await _tool(tmp_path).execute(
            prompt="x", first_frame_image="https://img.example/a.png", params={"ratio": "9:16"}
        )
    )
    assert "ratio" in out["error"] and seen == []


@pytest.mark.parametrize("provider", ["minimax", "minimax_cn_api"])
@pytest.mark.parametrize("model", ["MiniMax-H3", "MiniMax-H3-Max"])
async def test_a_text_only_v2_adaptive_ratio_is_refused_before_anything_is_sent(
    monkeypatch, tmp_path, provider, model
) -> None:
    """``adaptive`` beside a first frame is the only mode that takes it; a text-only
    call that names it would override the default ratio and be refused upstream."""
    seen = _serve(monkeypatch, _v2())
    out = json.loads(
        await _tool(tmp_path, provider=provider, model=model).execute(prompt="x", params={"ratio": "adaptive"})
    )
    assert "ratio" in out["error"] and "adaptive" in out["detail"] and seen == []


async def test_a_text_only_v2_call_keeps_a_concrete_ratio_it_is_given(monkeypatch, tmp_path) -> None:
    seen = _serve(monkeypatch, _v2())
    out = json.loads(await _tool(tmp_path).execute(prompt="x", params={"ratio": "9:16"}))
    assert out["success"] and json.loads(seen[0].content)["ratio"] == "9:16"


async def test_a_first_frame_takes_an_explicit_adaptive_ratio(monkeypatch, tmp_path) -> None:
    seen = _serve(monkeypatch, _v2())
    out = json.loads(
        await _tool(tmp_path).execute(
            prompt="x", first_frame_image="https://img.example/a.png", params={"ratio": "adaptive"}
        )
    )
    assert out["success"] and json.loads(seen[0].content)["ratio"] == "adaptive"


async def test_h3_max_is_a_v2_model_too(monkeypatch, tmp_path) -> None:
    seen = _serve(monkeypatch, _v2())
    assert json.loads(await _tool(tmp_path, model="MiniMax-H3-Max").execute(prompt="x"))["success"]
    assert seen[0].url.path == "/v2/video_generation"


async def test_a_v1_model_animates_a_frame_and_retrieves_its_file(monkeypatch, tmp_path) -> None:
    """The China section's default address carries a trailing slash, and every
    route still hangs off the host once."""
    seen = _serve(monkeypatch, _v1())
    tool = _tool(
        tmp_path, provider="minimax_cn_api", api_base="https://api.minimaxi.com/v1/", model="MiniMax-Hailuo-2.3"
    )
    frame = "data:image/png;base64," + base64.b64encode(_PNG).decode()
    out = json.loads(
        await tool.execute(
            prompt="clouds gather", first_frame_image=frame, params={"duration": 6, "resolution": "768P"}
        )
    )

    assert out["success"] and Path(out["path"]).read_bytes() == _VIDEO_BYTES
    submit, query, retrieve, download = seen
    assert str(submit.url) == "https://api.minimaxi.com/v1/video_generation"
    assert json.loads(submit.content) == {
        "model": "MiniMax-Hailuo-2.3",
        "prompt": "clouds gather",
        "first_frame_image": frame,
        "duration": 6,
        "resolution": "768P",
    }
    assert (query.url.path, query.url.params["task_id"]) == ("/v1/query/video_generation", "t1")
    assert (retrieve.url.path, retrieve.url.params["file_id"]) == ("/v1/files/retrieve", "f1")
    assert str(download.url) == _CDN and "authorization" not in download.headers


async def test_a_v1_text_only_call_sends_no_first_frame(monkeypatch, tmp_path) -> None:
    seen = _serve(monkeypatch, _v1())
    out = json.loads(await _tool(tmp_path, model="MiniMax-Hailuo-02").execute(prompt="a red kite"))
    assert out["success"]
    assert json.loads(seen[0].content) == {"model": "MiniMax-Hailuo-02", "prompt": "a red kite"}


async def test_a_local_first_frame_travels_inline_from_inside_a_confined_workspace(monkeypatch, tmp_path) -> None:
    """A picture the image tool just saved is a file under the workspace, and is
    sent as a data URL; one outside a confined workspace is refused unread."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "frame.png").write_bytes(_PNG)
    (tmp_path / "outside.png").write_bytes(_PNG)
    seen = _serve(monkeypatch, _v2())
    tool = _tool(workspace, restrict_to_workspace=True)

    assert json.loads(await tool.execute(prompt="x", first_frame_image=str(workspace / "frame.png")))["success"]
    sent = json.loads(seen[0].content)["content"][1]["image_url"]["url"]
    assert sent == "data:image/png;base64," + base64.b64encode(_PNG).decode()

    seen.clear()
    out = json.loads(await tool.execute(prompt="x", first_frame_image=str(tmp_path / "outside.png")))
    assert "outside the workspace" in out["detail"] and seen == []


@pytest.mark.parametrize(
    ("model", "params", "unknown"),
    [
        ("MiniMax-Hailuo-2.3", {"aspect_ratio": "16:9", "duration": 6}, "aspect_ratio"),
        ("MiniMax-H3", {"prompt_optimizer": False}, "prompt_optimizer"),
    ],
)
async def test_a_param_the_model_does_not_take_is_refused_naming_what_it_takes(
    monkeypatch, tmp_path, model, params, unknown
):
    """MiniMax answers an unknown key by ignoring it, which would read as success for
    a knob that did nothing; the call is told before anything is billed."""
    seen = _serve(monkeypatch, _v2())
    out = json.loads(await _tool(tmp_path, model=model).execute(prompt="x", params=params))
    assert unknown in out["detail"] and "duration" in out["detail"] and seen == []


async def test_h3_max_takes_its_documented_prompt_expansion_setting(monkeypatch, tmp_path) -> None:
    seen = _serve(monkeypatch, _v2())
    extra = {"prompt_expansion_mode": "quality"}
    out = json.loads(await _tool(tmp_path, model="MiniMax-H3-Max").execute(prompt="x", params={"extra": extra}))
    assert out["success"] and json.loads(seen[0].content)["extra"] == extra


@pytest.mark.parametrize(("provider", "taken"), [("minimax_cn_api", True), ("minimax", False)])
async def test_the_aigc_watermark_is_taken_on_the_china_section_only(monkeypatch, tmp_path, provider, taken) -> None:
    seen = _serve(monkeypatch, _v2())
    out = json.loads(await _tool(tmp_path, provider=provider).execute(prompt="x", params={"aigc_watermark": True}))
    if taken:
        assert out["success"] and json.loads(seen[0].content)["aigc_watermark"] is True
    else:
        assert "aigc_watermark" in out["detail"] and seen == []


async def test_a_failed_v2_job_keeps_minimax_s_reason(monkeypatch, tmp_path) -> None:
    failed = {"id": "t2", "status": "failed", "error": {"code": "1026", "message": "sensitive content"}}
    _serve(monkeypatch, _v2(failed))
    out = json.loads(await _tool(tmp_path).execute(prompt="x"))
    assert out["error"] == "video job status=failed" and out["detail"]["code"] == "1026"


async def test_a_v1_status_is_the_error_class_and_its_message_the_detail(monkeypatch, tmp_path) -> None:
    seen = _serve(monkeypatch, _v1({"status_code": 1008, "status_msg": "insufficient balance"}))
    out = json.loads(await _tool(tmp_path, model="I2V-01").execute(prompt="x", first_frame_image=_CDN))
    assert out == {"error": "MiniMax status 1008", "detail": "insufficient balance", "model": "I2V-01"}
    assert len(seen) == 1


async def test_an_answer_without_a_task_id_is_an_error_not_a_poll(monkeypatch, tmp_path) -> None:
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, json={}))
    out = json.loads(await _tool(tmp_path).execute(prompt="x"))
    assert "task" in out["error"] and len(seen) == 1


async def test_the_poll_gives_up_at_its_ceiling(monkeypatch, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "t2"})
        return httpx.Response(200, json={"task": {"id": "t2", "status": "running"}})

    _serve(monkeypatch, handler)

    async def no_wait(_seconds) -> None:
        return None

    monkeypatch.setattr(media_gen_module.asyncio, "sleep", no_wait)
    tool = _tool(tmp_path)
    tool._POLL_TIMEOUT_S = 1
    assert json.loads(await tool.execute(prompt="x"))["error"] == "video job status=timeout"


async def test_a_video_url_the_guard_refuses_is_named_as_already_generated(monkeypatch, tmp_path) -> None:
    """The video is billed before its link arrives, so a link this process may not
    fetch reads as that, never as a failed generation to try again."""
    seen = _serve(monkeypatch, _v2({"id": "t2", "status": "succeeded", "content": {"url": "http://127.0.0.1/v.mp4"}}))
    out = json.loads(await _tool(tmp_path).execute(prompt="x"))
    assert out["error"] == "generated, but the video URL was refused or could not be fetched"
    assert out["url"] == "http://127.0.0.1/v.mp4" and len(seen) == 2


async def test_a_video_section_on_a_provider_without_a_video_path_is_refused(monkeypatch, tmp_path) -> None:
    """A hand-written provider the video tool has no request path for gets a sentence
    saying so, not OpenRouter's request shapes posted to that provider's address."""
    seen = _serve(monkeypatch, _v2())
    tool = _tool(tmp_path, provider="openai", api_base="https://api.openai.com/v1", model="sora-2")
    out = json.loads(await tool.execute(prompt="x"))
    assert "openai" in out["detail"] and seen == []


async def test_a_first_frame_on_openrouter_is_refused_before_anything_is_sent(monkeypatch, tmp_path) -> None:
    seen = _serve(monkeypatch, _v2())
    tool = _tool(tmp_path, provider="", api_base="", model="kwaivgi/kling-v3.0-std")
    out = json.loads(await tool.execute(prompt="x", first_frame_image="https://img.example/a.png"))
    assert "first_frame_image" in out["error"] and seen == []


@pytest.mark.parametrize(("provider", "url"), [("minimax", "https://api.minimax.io/v1/video_generation")])
async def test_a_video_section_on_minimax_borrows_its_connection(monkeypatch, tmp_path, provider, url) -> None:
    """``provider: minimax`` lends MiniMax's address and key; the model the provider's
    list spells under its prefix goes out bare, and the capability report reads the
    same key the tool sends."""
    from raven.agent.tools.capabilities import CAPABILITIES, configured_from, has_credential
    from raven.config.loader import load_config

    seen = _serve(monkeypatch, _v1())
    config = load_config(tmp_path / "absent.json")
    getattr(config.providers, provider).api_key = "mm-key"
    config.tools.media.video.model = f"{provider}/MiniMax-Hailuo-2.3"
    config.tools.media.video.provider = provider
    tool = VideoGenerateTool(config.effective_media_config().video, workspace=tmp_path)

    assert json.loads(await tool.execute(prompt="a kite"))["success"]
    assert str(seen[0].url) == url and seen[0].headers["authorization"] == "Bearer mm-key"
    assert json.loads(seen[0].content)["model"] == "MiniMax-Hailuo-2.3"
    cap = next(c for c in CAPABILITIES if c.tool == "video_generate")
    assert has_credential(cap, config) and configured_from(cap, config) == f"borrowed: providers.{provider}.apiKey"


async def test_a_minimax_key_only_in_the_environment_is_no_key_for_the_video_section(monkeypatch, tmp_path) -> None:
    from raven.agent.tools.capabilities import CAPABILITIES, has_credential
    from raven.config.loader import load_config

    monkeypatch.setenv("MINIMAX_API_KEY", "mm-env")
    seen = _serve(monkeypatch, _v1())
    config = load_config(tmp_path / "absent.json")
    config.tools.media.video.model = "MiniMax-Hailuo-2.3"
    config.tools.media.video.provider = "minimax"
    tool = VideoGenerateTool(config.effective_media_config().video, workspace=tmp_path)
    cap = next(c for c in CAPABILITIES if c.tool == "video_generate")

    assert (tool.api_key, has_credential(cap, config)) == ("", False)
    assert "providers.minimax" in json.loads(await tool.execute(prompt="x"))["error"] and seen == []


def test_the_video_tool_offers_a_first_frame_only_where_it_runs_on_minimax() -> None:
    """The provider decides the route and what the model is offered, read per turn:
    off MiniMax the model sees the OpenRouter tool, with nothing it would refuse."""
    from raven.agent.tools.registry import ToolRegistry

    section = SimpleNamespace(provider="", api_base="", api_key="k", model="")
    registry = ToolRegistry()
    registry.register(VideoGenerateTool(lambda: section))

    def offered() -> dict:
        return next(d["function"] for d in registry.get_definitions() if d["function"]["name"] == "video_generate")

    assert "first_frame_image" not in offered()["parameters"]["properties"]
    assert "MiniMax" not in json.dumps(offered())
    section.provider, section.api_base = "minimax", "https://api.minimax.io/v1"
    assert "first_frame_image" in offered()["parameters"]["properties"]
    assert "MiniMax" in offered()["parameters"]["properties"]["params"]["description"]
