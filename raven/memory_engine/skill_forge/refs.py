"""Skill body ref-path resolution.

Replaces `{baseDir}/x` placeholders and markdown links to bundled files
(``references/``, ``scripts/``, ``assets/``, ``examples/``) with absolute
paths rooted at the skill's directory. Used by both the active-skills
render path (in :class:`LocalSkillCatalog`) and the router-hits render
path (in :class:`SkillsSegmentBuilder`'s post-gate hydrate step) so the
two flows produce identical bodies.

Resolution is per-ref existence-checked: a ``{baseDir}/x`` whose target
is missing on disk is left literal rather than handed to the agent as a
confident 404. Code fences are skipped entirely so example markup is not
silently mutated.
"""

from __future__ import annotations

import re
from pathlib import Path

_BUNDLED_DIRS = ("references", "scripts", "assets", "examples")

_MD_LINK_RE = re.compile(
    r"\[([^\]]+)\]\((?:\.{0,2}/)?"
    rf"((?:{'|'.join(_BUNDLED_DIRS)})/[^)\s]+)\)"
)
_BASE_DIR_REF_RE = re.compile(r"\{baseDir\}/(\S+?)(?=[\s)\'\"`]|$)")
_BARE_BASE_DIR_RE = re.compile(r"\{baseDir\}(?!/)")
_CODE_FENCE_RE = re.compile(r"(```.*?```)", re.S)


def resolve_refs(body: str, skill_dir: Path | str | None) -> tuple[str, bool]:
    """Return ``(rewritten_body, any_resolved)``.

    ``skill_dir`` is the directory of ``SKILL.md`` — bundled files live
    under it (``<skill_dir>/references/x.md`` etc.). When ``None`` or
    not a real directory, the function strips ``{baseDir}/`` to bare
    relative paths and leaves markdown links alone — the agent then sees
    a bare ``references/x.md`` it can't auto-resolve but at least no
    nonsense literal ``{baseDir}/`` remains in the prompt.

    Returns ``any_resolved=True`` when at least one substitution
    materialized a real path on disk, so callers can decide whether to
    emit a "Skill directory: ..." hint header.
    """
    if not body:
        return "", False

    skill_path = Path(skill_dir) if skill_dir is not None else None
    has_dir = skill_path is not None and skill_path.is_dir()

    if not has_dir:
        if "{baseDir}" in body:
            body = body.replace("{baseDir}/", "").replace("{baseDir}", "")
        return body, False

    base_dir = str(skill_path)
    any_resolved = False

    def _md_sub(mo: re.Match[str]) -> str:
        nonlocal any_resolved
        rel = mo.group(2).rstrip(".,;:")
        cut = min((i for i in (rel.find("#"), rel.find("?")) if i != -1), default=-1)
        frag = rel[cut:] if cut != -1 else ""
        rel_file = rel[:cut] if cut != -1 else rel
        if rel_file and (skill_path / rel_file).exists():
            any_resolved = True
            return f"[{mo.group(1)}]({base_dir}/{rel_file}{frag})"
        return mo.group(0)

    segments = _CODE_FENCE_RE.split(body)
    body = "".join(seg if seg.startswith("```") else _MD_LINK_RE.sub(_md_sub, seg) for seg in segments)

    if "{baseDir}" in body:

        def _bd_sub(mo: re.Match[str]) -> str:
            nonlocal any_resolved
            ref = mo.group(1).rstrip(".,;:")
            if ref and (skill_path / ref).exists():
                any_resolved = True
                return f"{base_dir}/{mo.group(1)}"
            return mo.group(0)

        body = _BASE_DIR_REF_RE.sub(_bd_sub, body)
        body, bare_n = _BARE_BASE_DIR_RE.subn(base_dir, body)
        if bare_n:
            any_resolved = True

    return body, any_resolved


__all__ = ["resolve_refs"]
