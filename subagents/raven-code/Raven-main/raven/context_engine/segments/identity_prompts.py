"""Per-domain, per-model identity prompts.

Two independent axes, in that order of directories:
``prompts/<domain>/<model-family>.txt``.

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

The *domain* axis exists because the same discipline can be wrong rather than
merely unhelpful: a data-analysis task told to reproduce the failing code path,
mimic the file's code conventions and rank the project's existing tests first is
being instructed to do work its grader never looks at. Domain comes from the run
profile (``RAVEN_DOMAIN``, see :mod:`raven.agent.profile`), never from the model
id, and the fallback chain is domain-family -> domain-default -> coding-default,
so a domain with only one prompt file written still serves every model, and a
domain with none degrades to the original behaviour instead of raising.
"""

from __future__ import annotations

import pathlib
from functools import lru_cache
from pathlib import Path

_PROMPT_ROOT = Path(__file__).parent / "prompts"

DEFAULT_FAMILY = "default"
DEFAULT_DOMAIN = "coding"

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
def _read(domain: str, family: str) -> str | None:
    path = _PROMPT_ROOT / domain / f"{family}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def load_template(model: str | None, domain: str = DEFAULT_DOMAIN) -> tuple[str, str, str]:
    """Return ``(domain, family, template)`` actually served for ``model``.

    Both returned keys are the ones in force, not the ones asked for, so a
    caller that logs them reports the prompt the model really received.
    """
    family = resolve_family(model)
    for candidate_domain, candidate_family in (
        (domain, family),
        (domain, DEFAULT_FAMILY),
        (DEFAULT_DOMAIN, DEFAULT_FAMILY),
    ):
        text = _read(candidate_domain, candidate_family)
        if text is not None:
            return candidate_domain, candidate_family, text
    raise FileNotFoundError(f"no identity prompt found under {_PROMPT_ROOT}")


def available_domains() -> tuple[str, ...]:
    """Domains that have a prompt directory on disk."""
    if not _PROMPT_ROOT.is_dir():
        return ()
    return tuple(sorted(d.name for d in _PROMPT_ROOT.iterdir() if d.is_dir() and any(d.glob("*.txt"))))


def available_families(domain: str = DEFAULT_DOMAIN) -> tuple[str, ...]:
    """Families that have a prompt file on disk for ``domain``."""
    directory = _PROMPT_ROOT / domain
    if not directory.is_dir():
        return ()
    return tuple(sorted(p.stem for p in directory.glob("*.txt")))


def prompt_path(domain: str, family: str) -> pathlib.Path:
    """On-disk location of one prompt file (for tests and audits)."""
    return _PROMPT_ROOT / domain / f"{family}.txt"
