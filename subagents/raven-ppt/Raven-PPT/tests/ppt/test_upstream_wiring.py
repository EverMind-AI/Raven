"""The four gaps the deck pipeline needs closed in shared code.

None of these are PPT-specific -- they are general capabilities the deck route
happens to be the first caller to need at scale -- so they live in shared code
and are tested here alongside the reason they exist.
"""

from __future__ import annotations

from typing import Any

from raven.agent.tools.base import Tool, ToolResult
from raven.agent.tools.registry import ToolRegistry
from raven.utils.helpers import image_block, labelled_images, text_block


class _Stub(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._name

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"stub {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:  # type: ignore[override]
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        return "ok"


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(_Stub(name))
    return registry


def test_every_picture_keeps_the_line_that_names_it() -> None:
    """A deck review returns a dozen renders; unlabelled they are interchangeable."""
    blocks = [
        text_block("summary of the deck"),
        text_block("page 1 of 3"),
        image_block("data:image/png;base64,AAAA"),
        text_block("page 2 of 3"),
        image_block("data:image/png;base64,BBBB"),
    ]
    assert labelled_images(blocks) == [
        text_block("page 1 of 3"),
        image_block("data:image/png;base64,AAAA"),
        text_block("page 2 of 3"),
        image_block("data:image/png;base64,BBBB"),
    ]


def test_only_the_line_directly_before_a_picture_counts_as_its_label() -> None:
    blocks = [
        text_block("preamble"),
        text_block("about to show you something"),
        image_block("data:image/png;base64,A"),
        text_block("and this one"),
        image_block("data:image/png;base64,B"),
    ]
    attached = labelled_images(blocks)
    assert attached == [
        text_block("about to show you something"),
        image_block("data:image/png;base64,A"),
        text_block("and this one"),
        image_block("data:image/png;base64,B"),
    ]
    assert text_block("preamble") not in attached


def test_one_picture_travels_bare_because_there_is_nothing_to_disambiguate() -> None:
    """The shape a plain read_file on a PNG produces, kept byte-identical.

    Its one line of geometry is already in the result text; repeating it in the
    following message would be noise, and there is only one image to pair it with.
    """
    blocks = [text_block("[image: /w/shot.png] | 300x200px"), image_block("data:image/png;base64,A")]
    assert labelled_images(blocks) == [image_block("data:image/png;base64,A")]


def test_a_label_is_not_reused_by_a_second_picture() -> None:
    blocks = [text_block("page 1"), image_block("data:image/png;base64,A"), image_block("data:image/png;base64,B")]
    assert labelled_images(blocks) == [
        text_block("page 1"),
        image_block("data:image/png;base64,A"),
        image_block("data:image/png;base64,B"),
    ]


def test_a_block_list_with_no_pictures_attaches_nothing() -> None:
    assert labelled_images([text_block("just words")]) == []
    assert labelled_images([]) == []


def test_definitions_can_be_narrowed_to_one_stage_of_a_pipeline() -> None:
    registry = _registry("ppt_ingest", "ppt_build", "ppt_publish")
    assert [d["function"]["name"] for d in registry.get_definitions()] == ["ppt_ingest", "ppt_build", "ppt_publish"]
    narrowed = registry.get_definitions(only_names=["ppt_build", "ppt_ingest"])
    # Registration order, not the caller's order: the caller is describing a
    # stage, not an ordering.
    assert [d["function"]["name"] for d in narrowed] == ["ppt_ingest", "ppt_build"]


def test_naming_a_tool_that_is_not_installed_shortens_the_list_rather_than_failing() -> None:
    registry = _registry("ppt_ingest")
    assert [d["function"]["name"] for d in registry.get_definitions(only_names=["ppt_ingest", "ppt_nope"])] == [
        "ppt_ingest"
    ]
    assert registry.get_definitions(only_names=[]) == []
