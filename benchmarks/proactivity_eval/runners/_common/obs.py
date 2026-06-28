"""Render raw obs + SynthesizedContext into prompt placeholder strings.

Kept system-neutral — Raven, Hermes and OpenClaw all use the same block
shape inside their system/user templates.
"""

from __future__ import annotations

from typing import Any


def build_obs_block(obs: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[t={e.get('time', '?')}] {e.get('event', '')}" for e in obs
    )


def build_synth_block(synth: Any) -> str:
    """Render a ``SynthesizedContext`` (or None) into the {synth_block}
    placeholder value used by every pbench prompt template.
    """
    if synth is None:
        return ""
    parts: list[str] = [
        "\n",
        "Additional synthesized context from Sentinel's upstream observer:",
    ]
    if synth.user_profile:
        parts.append(f"\nUser profile:\n  {synth.user_profile}")
    if synth.routines:
        parts.append("\nCandidate routines (not yet user-confirmed):")
        for r in synth.routines:
            parts.append(f"  - {r.pattern}")
    if synth.memory_md:
        parts.append(f"\nRecent memory:\n  {synth.memory_md}")
    parts.append("")
    return "\n".join(parts)


__all__ = ["build_obs_block", "build_synth_block"]
