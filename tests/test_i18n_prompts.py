"""Model-facing prompt templates live per language under ``raven/templates/prompts``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from raven.i18n import prompt_in

TEMPLATES = Path(__file__).resolve().parent.parent / "raven" / "templates" / "prompts"
CJK = re.compile(r"[\u3400-\u9fff\uff00-\uffef\u3000-\u303f]")


@pytest.mark.parametrize("name", sorted(p.stem for p in (TEMPLATES / "en").glob("*.md")))
def test_every_english_template_is_english_and_has_its_chinese_twin(name: str) -> None:
    en = prompt_in("en", name)
    assert en.strip() and not CJK.search(en), name
    zh = prompt_in("zh", name)
    assert zh.strip() and zh != en, name


def test_an_unknown_language_falls_back_to_english() -> None:
    assert prompt_in("fr", "sentinel_planner") == prompt_in("en", "sentinel_planner")


def test_an_unknown_template_is_an_error() -> None:
    with pytest.raises(FileNotFoundError):
        prompt_in("en", "no_such_prompt")
