"""Per-model identity prompts for the "coding" profile.

The same instruction can help one model family and hurt another: a nudge that
raises tool-call rate on a model that under-reaches for tools will over-trigger
one that already reaches too often. opencode solves this by shipping one prompt
file per family and dispatching on the model id, falling back to a default; this
is that mechanism (see ``session/system.ts`` there).

Two properties the dispatch deliberately has:

*A missing file falls back rather than raising.* ``_FAMILIES`` may name a family
whose prompt has not been written yet, so a family can be reserved in the table
before anyone writes its text, and deleting a prompt file degrades to the
default instead of breaking every run on that model.

*``default.txt`` is the previous hard-coded text, verbatim.* A model with no
prompt of its own therefore behaves exactly as it did before this dispatch
existed, which keeps a new prompt file the only variable when its family's
numbers move.

Matching is first-hit on an ordered list, not a dict lookup, because model ids
overlap: ``gpt-4`` has to be tested before the bare ``gpt`` prefix, the same way
opencode orders its checks.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPT_DIR = Path(__file__).parent / "prompts" / "coding"

DEFAULT_FAMILY = "default"

# (substrings, family). First family whose substrings match the lowercased model
# id wins. Order matters: narrower ids first.
_FAMILIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("claude", "anthropic"), "anthropic"),
    (("gpt", "o1-", "o3-", "codex"), "gpt"),
    (("gemini",), "gemini"),
    (("deepseek",), "deepseek"),
    (("qwen",), "qwen"),
    (("kimi",), "kimi"),
)


def resolve_family(model: str | None) -> str:
    """Return the prompt family for ``model``, or ``default``."""
    if not model:
        return DEFAULT_FAMILY
    needle = model.lower()
    for substrings, family in _FAMILIES:
        if any(sub in needle for sub in substrings):
            return family
    return DEFAULT_FAMILY


@lru_cache(maxsize=None)
def _read(family: str) -> str | None:
    path = _PROMPT_DIR / f"{family}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def load_template(model: str | None) -> tuple[str, str]:
    """Return ``(family, template)`` for ``model``.

    The returned family is the one actually served, so a caller that logs it
    reports the prompt in force rather than the prompt that was asked for.
    """
    family = resolve_family(model)
    text = _read(family)
    if text is None and family != DEFAULT_FAMILY:
        family, text = DEFAULT_FAMILY, _read(DEFAULT_FAMILY)
    if text is None:
        raise FileNotFoundError(f"no coding identity prompt found in {_PROMPT_DIR}")
    return family, text


def available_families() -> tuple[str, ...]:
    """Families that have a prompt file on disk."""
    if not _PROMPT_DIR.is_dir():
        return ()
    return tuple(sorted(p.stem for p in _PROMPT_DIR.glob("*.txt")))
