#!/usr/bin/env python3
"""Render the DR prompt segment from a full config and stamp it.

Why this exists alongside the harness's own prompt stamp. The system prompt
never enters the persisted trajectory, so a prompt change is unverifiable after
the fact unless something renders it and records a hash. The harness has such a
tool, but it constructs the segment builder with only ``promptSectionOverride``:

    builder = DRModeSegmentBuilder(override)

so every other prompt-affecting field was rendered at its class default. On this
build that blindness is worth **-863 characters** (``measuredGuidance=false``) and
**+311** (``finalShape.requireMarker=true``), and it fails in whichever direction
the class default happens to point: through dr@2.5 it under-reported the product
arm, and the dr@2.6 default flip turned the same blindness into a **+311
over-report on all three bench treated arms**, which would have red-lighted a
prompt that had not moved by a byte. "Reports no change when it changed" was only
half of it.

Both stamps now render through ``build_dr_flow`` itself rather than a copy of what
it does, so neither can drift from the run again. This one stays because it prints
the pairwise diff an acceptance review actually wants ("off vs on differ by exactly
this clause"), and because it is small enough to run against an arbitrary config
without a workspace.

Usage
    <venv>/python scripts/stamp_dr_segment.py <config.json> [<config.json> ...]

Prints one line per config: sha, char count, and the fields that fed it. With
two or more configs it also prints the pairwise diff, which is the form an
acceptance review actually wants ("off vs on differ by exactly this clause").
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raven.agent.flow.dr import build_dr_flow  # noqa: E402
from raven.config.raven import DRFlowConfig  # noqa: E402
from raven.context_engine.base import AssemblyContext, TokenBudget  # noqa: E402


def render(cfg: DRFlowConfig) -> str:
    """Render through ``build_dr_flow`` itself, not a copy of what it does.

    This used to construct the builder here with every field copied across, kept
    correct by a comment. That is still a probe on a different path than the code
    it certifies, and a probe on a different path is untrustworthy in both
    directions - it clears a prompt that moved and it flags one that did not. The
    harness stamp failed exactly that way twice, in opposite directions, because
    the class default it silently inherited changed underneath it.

    The two integers below never reach the segment builder: ``build_dr_flow``
    hands it four config fields and spends the rest on observers.
    """
    asm = build_dr_flow(cfg, None, cfg.max_iterations or 0, 0)
    if asm is None:
        return ""
    builder = asm.segment_builder
    budget = TokenBudget(context_length=0, reserved_output=0, reserved_tools=0,
                         reserved_system=0, available_history=0)
    ctx = AssemblyContext(session_key="stamp", current_message="", media=None,
                          channel=None, chat_id=None, session_messages=[], budget=budget)
    seg = asyncio.run(builder.build(ctx))
    return (seg.text if seg else "") or ""


def tool_sha(cfg: DRFlowConfig) -> tuple[str, int]:
    """Sha of the ask_user tool definition, read off the same assembly.

    The description and the parameter schema are prompt text - they enter the
    tool schema the model reads and they move the call rate - and the fallback
    string is read by any call the gate declines to hand off. None of it is in
    the segment sha, so without this a prompt-affecting change ships unstamped.

    ``asm.ask_user_tool``, never a fresh ``DRAskUserTool(...)`` here: this file
    was rewritten precisely because a probe on a different path than the code it
    certifies clears a prompt that moved and flags one that did not.
    """
    asm = build_dr_flow(cfg, None, cfg.max_iterations or 0, 0)
    tool = getattr(asm, "ask_user_tool", None) if asm else None
    if tool is None:
        return "", 0
    from raven.agent.flow.ask_user_tool import (
        _FALLBACK_NO_QUESTIONS,
        _FALLBACK_NOT_DELIVERED,
    )

    text = json.dumps(
        {"description": tool.description, "parameters": tool.parameters,
         "fallback_no_questions": _FALLBACK_NO_QUESTIONS,
         "fallback_not_delivered": _FALLBACK_NOT_DELIVERED},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], len(text)


def stamp(path: Path) -> dict:
    raw = json.loads(path.read_text())
    flow = raw.get("drFlow") or {}
    cfg = DRFlowConfig(**flow)
    if not cfg.enabled:
        # Same key set as the enabled branch: ``--json`` is consumed downstream and
        # a branch that omits a key turns an off arm into a KeyError there.
        return {"config": path.name, "enabled": False, "sha": "", "chars": 0,
                "ask_user_tool_sha": "", "ask_user_tool_chars": 0,
                "note": "flow off - no DR segment; this is the anchor's state"}
    text = render(cfg)
    t_sha, t_chars = tool_sha(cfg)
    return {
        "config": path.name,
        "enabled": True,
        "version": cfg.version,
        "sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "chars": len(text),
        # Empty when the feature is off, which is every bench arm: the two knobs
        # resolve to one state in build_dr_flow, so an absent sha here IS the
        # off-state fact, not a missing measurement.
        "ask_user_tool_sha": t_sha,
        "ask_user_tool_chars": t_chars,
        "inputs": {
            "measured_guidance": cfg.measured_guidance,
            "require_answer_marker": cfg.final_shape.require_marker,
            "prompt_section_override": bool(cfg.prompt_section_override),
            "identity_override": bool(cfg.identity_override),
            "final_shape_record": cfg.final_shape.record,
            "ask_user": cfg.ask_user.enabled,
            "ask_user_outline": cfg.ask_user.outline,
            "ask_user_mode": cfg.ask_user.mode,
        },
        "_text": text,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("configs", nargs="+", type=Path)
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    out = [stamp(p) for p in a.configs]
    for s in out:
        if not s["enabled"]:
            print(f"  {s['config']:24s} flow=off   sha=(none)          {s['note']}")
            continue
        i = s["inputs"]
        print(f"  {s['config']:24s} {s['version']:8s} sha={s['sha']}  {s['chars']:6d} chars"
              f"   guidance={i['measured_guidance']!s:5s} marker={i['require_answer_marker']!s:5s}"
              f" record={i['final_shape_record']!s:5s}")
        if s["ask_user_tool_sha"]:
            print(f"  {'':24s}          ask_user tool sha={s['ask_user_tool_sha']}"
                  f"  {s['ask_user_tool_chars']:6d} chars"
                  f"   outline={i['ask_user_outline']!s:5s} mode={i['ask_user_mode']}")

    on = [s for s in out if s["enabled"]]
    if len(on) >= 2:
        print("\n  pairwise:")
        for i in range(len(on) - 1):
            x, y = on[i], on[i + 1]
            same = "IDENTICAL" if x["sha"] == y["sha"] else "differ"
            print(f"    {x['config']} vs {y['config']}: {same}"
                  f"  {y['chars'] - x['chars']:+d} chars")
            if same == "differ":
                # Report the added tail, which is the whole point of an appended
                # clause: everything before it must be at its measured offset.
                a_t, b_t = x["_text"], y["_text"]
                pre = len(a_t) if b_t.startswith(a_t.rstrip()) else -1
                if pre >= 0:
                    print(f"      appended only (prefix byte-identical); added tail:")
                    for line in b_t[len(a_t.rstrip()):].strip().splitlines()[:6]:
                        print(f"        | {line}")
                else:
                    print("      !! not a pure append - earlier bytes moved."
                          " dr@2.0 lost a headline to exactly this.")

    if a.json:
        Path(a.json).write_text(json.dumps(
            [{k: v for k, v in s.items() if k != "_text"} for s in out],
            ensure_ascii=False, indent=2))
        print(f"\n  wrote {a.json}")


if __name__ == "__main__":
    main()
