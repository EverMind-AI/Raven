"""Real OpenRouter generation and reference-image edit coverage."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.agent.tools.base import ToolResult
from raven.agent.tools.media_gen import ImageGenerateTool
from raven.config.schema import MediaToolConfig
from raven.utils.helpers import detect_image_mime

pytestmark = pytest.mark.integration


def _key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        pytest.skip("OPENROUTER_API_KEY is required")
    return key


def _metadata(result: str | ToolResult) -> dict:
    assert isinstance(result, ToolResult)
    return json.loads(result.model_text)


@pytest.mark.asyncio
async def test_image_generation_and_edit_real_api(tmp_path: Path) -> None:
    tool = ImageGenerateTool(
        MediaToolConfig(
            api_key=_key(),
            api_base="https://openrouter.ai/api/v1",
            model="openai/gpt-image-2",
            api_style="openrouter_images",
            allow_model_override=False,
        ),
        workspace=tmp_path,
    )

    generated = _metadata(
        await tool.execute(
            prompt="A single cobalt blue circle centered on a pure white square, with no text.",
            quality="low",
            aspect_ratio="1:1",
        )
    )
    generated_path = Path(generated["paths"][0])
    assert generated["operation"] == "generation"
    assert generated_path.is_file()
    assert detect_image_mime(generated_path.read_bytes()) == "image/png"
    assert generated["usage"]["cost"] > 0

    edited = _metadata(
        await tool.execute(
            prompt="Change the blue circle to vivid red. Keep the white background, size, and composition unchanged.",
            images=[str(generated_path)],
            quality="low",
            aspect_ratio="1:1",
        )
    )
    edited_path = Path(edited["paths"][0])
    assert edited["operation"] == "edit"
    assert edited_path.is_file()
    assert detect_image_mime(edited_path.read_bytes()) == "image/png"
    assert edited_path.read_bytes() != generated_path.read_bytes()
    assert edited["usage"]["cost"] > 0

    print(
        json.dumps(
            {
                "generation_path": str(generated_path),
                "generation_cost": generated["usage"]["cost"],
                "edit_path": str(edited_path),
                "edit_cost": edited["usage"]["cost"],
            }
        )
    )
