"""The digest reads a long page in chunks and rolls the extraction forward.

Ported from HyWe's ``INCREMENTAL_EXTRACTOR_PROMPT`` so that the two harnesses differ
on the page path in nothing we did not choose. What the tests below pin is what the
port is for: every chunk reaches a model (the tail of a long page is no longer
dropped at ``sourceCapChars``), each call is shown what the previous ones found, and
a failure partway keeps the chunks already read instead of falling back to the
truncation the first chunk's failure falls back to.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow import plugin as flow_plugin  # noqa: E402
from research_flow.config import DigestConfig  # noqa: E402

from raven.providers.base import LLMProvider, LLMResponse  # noqa: E402

_CHUNK = re.compile(r"## New Webpage Chunk \((\d+)/(\d+)\)")


class _Chunks(LLMProvider):
    """Records each call's prompt; answers chunk *i* with ``replies[i - 1]``, else JSON.

    Keyed on the chunk number in the prompt rather than on the call count, because
    ``chat_with_retry`` runs its own ladder: a reply meant for chunk 2 would otherwise
    be handed to chunk 1's second attempt.
    """

    def __init__(self, replies: "list[str | Exception | None] | None" = None):
        super().__init__(api_key="test")
        self.replies = replies or []
        self.calls: list[str] = []
        self.systems: list[str] = []
        self.chunk_count = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        self.systems.append(messages[0]["content"])
        seen = _CHUNK.search(prompt)
        index = int(seen.group(1)) if seen else 1
        self.chunk_count = int(seen.group(2)) if seen else 1
        reply = self.replies[index - 1] if index - 1 < len(self.replies) else None
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            reply = json.dumps({"rational": "r", "evidence": [f"e{index}"], "summary": f"s{index}"})
        return LLMResponse(content=reply, finish_reason="stop")

    def get_default_model(self) -> str:
        return "flash"


def _digest(provider, **kw):
    kw.setdefault("chunk_chars", 100)
    kw.setdefault("source_cap_chars", 10_000)
    return flow_plugin._make_digest_fn(provider, None, **kw)


def test_every_chunk_of_a_long_page_reaches_the_model():
    provider = _Chunks()
    page = "".join(f"line {i} of the page\n" for i in range(60))  # ~1.2k chars, 100-char chunks

    out = asyncio.run(_digest(provider, max_chunks=50)(page, "the founding year"))

    assert provider.chunk_count == len(provider.calls) > 10
    assert provider.calls[0].startswith("Please process the following webpage chunk")
    assert all(s == "You extract useful page information for a web agent." for s in provider.systems)
    # Nothing of the page is dropped: the last line is in the last call, and every
    # chunk the splitter made was handed to a call of its own.
    assert "line 59 of the page" in provider.calls[-1]
    assert [i for i in range(1, provider.chunk_count + 1)] == [int(_CHUNK.search(c).group(1)) for c in provider.calls]
    # The last chunk's extraction is what ships, in HyWe's section order.
    assert out.startswith(f"Evidence in page:\ne{provider.chunk_count}\n\nSummary:\ns{provider.chunk_count}")
    assert "summarized incrementally" in out


def test_each_call_is_shown_what_the_previous_ones_found():
    provider = _Chunks()
    page = "x" * 350

    asyncio.run(_digest(provider)(page, "goal"))

    assert "## Existing Extraction\n\n\n## New Webpage Chunk (1/" in provider.calls[0]
    assert "## Existing Extraction\nEvidence:\ne1\n\nSummary:\ns1\n" in provider.calls[1]
    assert f"(2/{provider.chunk_count})" in provider.calls[1]


def test_a_reply_that_is_not_json_still_carries_forward_and_ships():
    provider = _Chunks(["first pass, plain text", "second pass, plain text"])
    out = asyncio.run(_digest(provider)("y" * 150, "goal"))

    assert "first pass, plain text" in provider.calls[1]
    assert out.startswith("Note:\n") and "second pass, plain text" in out


def test_a_later_chunk_that_fails_keeps_what_was_read():
    provider = _Chunks([None, RuntimeError("provider down")])
    out = asyncio.run(_digest(provider)("z" * 150, "goal"))

    assert "Evidence in page:\ne1" in out
    assert "Extraction stopped after chunk 1 of 2: RuntimeError." in out


def test_the_first_chunk_failing_is_the_whole_digest_failing():
    """So the tool falls back to truncation, where a digest that cannot run belongs."""
    provider = _Chunks([RuntimeError("provider down")])
    with pytest.raises(RuntimeError, match="provider down"):
        asyncio.run(_digest(provider)("z" * 150, "goal"))


def test_an_empty_reply_does_not_erase_the_chunks_already_read():
    provider = _Chunks([None, "   "])
    out = asyncio.run(_digest(provider)("z" * 150, "goal"))

    assert "Evidence in page:\ne1" in out


def test_the_read_is_capped_by_chunks_and_by_characters_and_says_so():
    provider = _Chunks()
    out = asyncio.run(_digest(provider, max_chunks=2)("w" * 1000, "goal"))

    assert len(provider.calls) == 2
    assert "Only the first 2 chunks (200 characters) were read." in out

    provider = _Chunks()
    out = asyncio.run(_digest(provider, source_cap_chars=150)("w" * 1000, "goal"))
    assert "Reader output was capped at 150 of 1000 characters." in out


def test_map_reduce_off_is_the_single_call_on_a_truncated_page():
    provider = _Chunks(["the old shape"])
    out = asyncio.run(_digest(provider, map_reduce=False)("v" * 1000, "the founding year"))

    assert len(provider.calls) == 1
    assert provider.systems[0].startswith("You extract exactly the requested information")
    assert provider.calls[0].startswith("Information needed: the founding year")
    assert out == "the old shape"


def test_the_page_head_still_rides_along_when_it_is_configured():
    provider = _Chunks()
    out = asyncio.run(flow_plugin._make_digest_fn(provider, None, 20, chunk_chars=100)("abc" * 100, "goal"))

    assert out.endswith("\n\n[page head, verbatim]\n" + ("abc" * 100)[:20])


def test_the_defaults_let_the_character_cap_stop_the_read_not_the_chunk_count():
    """The splitter strips its boundaries, so a chunk carries less than chunk_chars.

    Measured on a 1.28M character page: ten 100k chunks covered 802,422 characters.
    With max_chunks as the binding limit the read would stop a fifth short of the
    documented ceiling, which is the failure sourceCapChars itself was raised to fix.
    """
    cfg = DigestConfig()
    assert (cfg.map_reduce, cfg.chunk_chars) == (True, 100_000)
    assert cfg.chunk_chars * cfg.max_chunks > cfg.source_cap_chars
