"""What an openai-backed sub-agent's record says about the middle of its run.

The endpoint reports its steps in `reasoning_steps` -- buffered as a whole list,
streamed as fragments across many frames -- and the backend published none of it,
so an instance log held a prompt and an answer and nothing between them. These
tests drive the real `run` on both paths: the buffered one through a stubbed
`_post_chat`, the streamed one through a local endpoint serving the captured
305-frame response, so aiohttp and the frame loop are exercised rather than
mocked.
"""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

from aiohttp import web

from raven.agent.subagent import activity
from raven.agent.subagent.backends.openai_api import OpenAIApiBackend

_FIXTURES = Path(__file__).parent / "fixtures" / "mirothinker"


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _sse_endpoint(frames: list[bytes]) -> AsyncIterator[str]:
    """A local stub answering one chat completion as a stream, yielding its base url.

    A real endpoint rather than a fake response object: what has to publish is the
    frame loop inside `_stream_chat`, aiohttp's line splitting included, and the
    sibling backend tests in `test_subagent_third_party.py` drive it the same way.
    """

    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for frame in frames:
            await resp.write(frame)
        return resp

    port = _free_port()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        await runner.cleanup()


def _captured_stream() -> list[bytes]:
    text = (_FIXTURES / "stream_research.sse").read_text(encoding="utf-8")
    return [f"{line}\n\n".encode() for line in text.splitlines() if line.strip()]


async def _no_delta(text: str) -> None:
    return None


def _tool_names(messages: list[dict[str, Any]]) -> list[str]:
    return [call["function"]["name"] for m in messages for call in m.get("tool_calls") or []]


async def test_a_buffered_openai_call_publishes_its_steps_and_its_cost(monkeypatch) -> None:
    """The lane could always see the middle -- the endpoint sends it in
    `reasoning_steps` -- and published none of it."""
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "3.14.3",
                    "reasoning_steps": [
                        {"type": "thinking", "thought": "look it up"},
                        {
                            "type": "web_search",
                            "web_search": {"search_keywords": ["aiohttp"], "search_results": [{"url": "u"}]},
                        },
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }
    backend = OpenAIApiBackend(name="Researcher", base_url="https://x/v1", model="m")

    async def fake_post(self, url, *, json, headers, timeout):
        return body

    monkeypatch.setattr(OpenAIApiBackend, "_post_chat", fake_post)

    with activity.collecting() as did:
        reply = await backend.run("q", task_id="t1", workspace=None, executor=None)

    assert reply == "3.14.3"
    call = next(m for m in did.transcript if m.get("tool_calls"))
    assert call["tool_calls"][0]["function"]["name"] == "web_search"
    assert call["reasoning_content"] == "look it up"
    assert next(m for m in did.transcript if m["role"] == "tool")
    assert all(m.get("content") != "3.14.3" for m in did.transcript), "the answer is the record's, not a row"
    assert did.tokens_in == 10 and did.tokens_out == 4


async def test_a_buffered_call_that_reported_no_steps_publishes_no_rows(monkeypatch) -> None:
    """Most OpenAI-compatible endpoints report no steps at all, and inventing a
    row for one would make its record claim work nobody did."""
    backend = OpenAIApiBackend(name="Plain", base_url="https://x/v1", model="m")

    async def fake_post(self, url, *, json, headers, timeout):
        return {"choices": [{"message": {"content": "just the answer"}}]}

    monkeypatch.setattr(OpenAIApiBackend, "_post_chat", fake_post)

    with activity.collecting() as did:
        reply = await backend.run("q", task_id="t2", workspace=None, executor=None)

    assert reply == "just the answer"
    assert did.transcript == []
    assert did.tokens_in is None and did.tokens_out is None


async def test_the_empty_answer_fallback_is_not_the_step_trace(monkeypatch) -> None:
    """`reasoning_content` is the answer's fallback when a model puts the answer
    only there; `reasoning_steps` is the trace. Different fields, both readable
    on one response, and the fallback text is the reply rather than a row."""
    backend = OpenAIApiBackend(name="Researcher", base_url="https://x/v1", model="m")

    async def fake_post(self, url, *, json, headers, timeout):
        return {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "the answer",
                        "reasoning_steps": [{"type": "thinking", "thought": "hm"}],
                    }
                }
            ]
        }

    monkeypatch.setattr(OpenAIApiBackend, "_post_chat", fake_post)

    with activity.collecting() as did:
        reply = await backend.run("q", task_id="t3", workspace=None, executor=None)

    assert reply == "the answer"
    row = did.transcript[0]
    assert len(did.transcript) == 1
    assert (row["role"], row["content"], row["reasoning_content"]) == ("assistant", "", "hm")


async def test_a_streamed_openai_call_republishes_its_steps_as_they_arrive(monkeypatch) -> None:
    """A live view has only the activity to read, so the steps have to land
    before the turn does."""
    seen: list[int] = []
    published = activity.note_transcript

    def note(rows):
        seen.append(len(rows or []))
        published(rows)

    monkeypatch.setattr(activity, "note_transcript", note)

    async with _sse_endpoint(_captured_stream()) as base_url:
        backend = OpenAIApiBackend(name="Researcher", base_url=base_url, model="mirothinker-1-7-deepresearch")
        with activity.collecting() as did:
            reply = await backend.run("q", task_id="t4", workspace=None, executor=None, on_delta=_no_delta)

    assert len(seen) > 1, "a transcript published once at the end is not a live view of anything"
    assert seen == sorted(seen), "an event list only grows, so the rows built from it cannot shrink"
    assert seen[-1] == len(did.transcript), "the last live publish and the settled record are the same rows"
    assert _tool_names(did.transcript) == ["web_search", "fetch_url_content", "fetch_url_content"]
    assert reply and all(m.get("content") != reply for m in did.transcript), "the answer is the record's"


async def test_a_streamed_openai_call_publishes_the_endpoints_token_cost() -> None:
    """Measured: `usage` arrives on one frame, the one before `[DONE]`. Kept and
    published once the stream ends, so the counts are the endpoint's own -- an
    `openai` call has never had a token cost in its record at all."""
    async with _sse_endpoint(_captured_stream()) as base_url:
        backend = OpenAIApiBackend(name="Researcher", base_url=base_url, model="mirothinker-1-7-deepresearch")
        with activity.collecting() as did:
            await backend.run("q", task_id="t5", workspace=None, executor=None, on_delta=_no_delta)

    assert (did.tokens_in, did.tokens_out) == (17423, 878)


async def test_usage_is_read_from_a_frame_that_carries_no_choices() -> None:
    """OpenAI's own `stream_options: {"include_usage": true}` frame reports the
    cost with an empty `choices`, which the delta guard drops -- so where the
    read sits in the frame loop decides whether the cost is recorded at all."""
    frames = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n',
        b"data: [DONE]\n\n",
    ]
    async with _sse_endpoint(frames) as base_url:
        backend = OpenAIApiBackend(name="Researcher", base_url=base_url, model="m")
        with activity.collecting() as did:
            reply = await backend.run("q", task_id="t6", workspace=None, executor=None, on_delta=_no_delta)

    assert reply == "hi"
    assert (did.tokens_in, did.tokens_out) == (3, 1)


async def test_a_streamed_call_survives_steps_it_cannot_read() -> None:
    """`_stream_chat` skips a frame it cannot make sense of rather than raising,
    and a step list is one more thing a nominally-compatible endpoint can get
    wrong. A turn that answered must not fail on its own audit trail."""
    frames = [
        b'data: {"choices":[{"delta":{"reasoning_steps":7}}]}\n\n',
        b'data: {"choices":[{"delta":{"reasoning_steps":["not-a-step",null]}}]}\n\n',
        b'data: {"choices":[{"delta":{"reasoning_steps":[{"type":"thinking"}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"fine"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    async with _sse_endpoint(frames) as base_url:
        backend = OpenAIApiBackend(name="Researcher", base_url=base_url, model="m")
        with activity.collecting() as did:
            reply = await backend.run("q", task_id="t7", workspace=None, executor=None, on_delta=_no_delta)

    assert reply == "fine"
    assert did.transcript == []


async def test_a_streamed_calls_rows_read_like_the_buffered_ones() -> None:
    """One reader, two response shapes: an instance's conversation must not say
    which transport delivered a turn. The fixtures are separate captures, so the
    grammar is what is pinned -- every result row answers the call before it."""
    async with _sse_endpoint(_captured_stream()) as base_url:
        backend = OpenAIApiBackend(name="Researcher", base_url=base_url, model="mirothinker-1-7-deepresearch")
        with activity.collecting() as did:
            await backend.run("q", task_id="t8", workspace=None, executor=None, on_delta=_no_delta)

    for previous, row in zip(did.transcript, did.transcript[1:]):
        if row["role"] == "tool":
            assert previous["tool_calls"][0]["id"] == row["tool_call_id"]
    thoughts = [m["reasoning_content"] for m in did.transcript if m.get("reasoning_content")]
    assert thoughts and all(len(t) > 1 for t in thoughts), "106 thinking fragments join into whole thoughts"
    searched = json.loads(
        next(m for m in did.transcript if m.get("tool_calls"))["tool_calls"][0]["function"]["arguments"]
    )
    assert "search_keywords" in searched, "the arguments are the endpoint's own, not a renamed alias"


async def test_a_provider_repeating_its_cumulative_usage_is_not_counted_twice() -> None:
    """``note_usage`` sums, because an in-process loop reports once per iteration.
    A stream is the other case: OpenAI's own ``include_usage`` sends one
    cumulative total, so a provider that repeats it on every frame would have the
    run's cost multiplied by the frame count. The last report wins instead."""
    frames = [
        b'data: {"choices":[{"delta":{"content":"hi"}}],"usage":{"prompt_tokens":100,"completion_tokens":20}}\n\n',
        b'data: {"choices":[{"delta":{"content":"!"}}],"usage":{"prompt_tokens":100,"completion_tokens":20}}\n\n',
        b"data: [DONE]\n\n",
    ]
    async with _sse_endpoint(frames) as base_url:
        backend = OpenAIApiBackend(name="Researcher", base_url=base_url, model="m")
        with activity.collecting() as did:
            reply = await backend.run("q", task_id="t9", workspace=None, executor=None, on_delta=_no_delta)

    assert reply == "hi!"
    assert (did.tokens_in, did.tokens_out) == (100, 20), "summed would be 200 / 40"


async def test_every_published_row_carries_a_clock_as_the_acp_lane_does() -> None:
    """Both lanes write through one builder, so a row from either must be
    renderable by the same code. The endpoint stamps nothing, so an unstamped
    openai row would silently lose the duration an `acp` row shows."""
    async with _sse_endpoint(_captured_stream()) as base_url:
        backend = OpenAIApiBackend(name="Researcher", base_url=base_url, model="mirothinker")
        with activity.collecting() as did:
            await backend.run("q", task_id="t10", workspace=None, executor=None, on_delta=_no_delta)

    assert did.transcript, "nothing was published"
    assert all(row.get("timestamp") for row in did.transcript), f"unstamped row in {did.transcript}"
