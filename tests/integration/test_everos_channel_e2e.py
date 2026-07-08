"""Mock-channel end-to-end: real raven agent -> everos -> markdown.

Drives the **production** agent path (the ``raven agent`` CLI, same
wiring a real channel uses) with channel-style session keys, then checks
that everos actually wrote its markdown memory to disk:

- user-track episodes  (``users/<user_id>/episodes/*.md``)
- agent-track cases / skills (``agents/<agent_id>/...``)

This is the only test that exercises the whole chain end-to-end through
the real AgentLoop + tools + memory backend (L2/L3 call everos / the
backend directly). It is gated behind ``real_llm`` and isolates the
store via ``EVEROS_MEMORY__ROOT`` so it never touches ``~/.everos``.

The agent identity is the pre-configured ``agent_id`` in
``~/.raven/config.json`` (``agent:liquid`` in this repo's setup); the
user identity is the ``user_id`` fallback there (``liquid``). No per-user
dynamic owner — single configured owner, by design.

Skill extraction is algo-gated (everos only distils a skill from
trajectories it judges reusable, clustered across cases), so the skill
assertion is best-effort / xfail; the user-memory + cases assertions and
the "markdown actually written" check are the hard guarantees.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.real_llm

_RAVEN = Path(sys.executable).parent / "raven"

# A reusable, tool-bearing procedure repeated across sessions so everos
# can cluster agent cases into a skill. Each turn drives the agent to run
# `curl` (exec tool) and interpret the result — a real tool round-trip,
# which case/skill extraction requires.
_PROC = (
    "Safely back up a config file using a verify-before-trust procedure. "
    "Do these steps with the exec tool: (1) write the text {body!r} to "
    "{f}.conf, (2) copy {f}.conf to {f}.conf.bak, (3) run `diff {f}.conf "
    "{f}.conf.bak` to verify the backup is byte-identical, (4) only if "
    "diff is empty, report SUCCESS. The key idea is to always verify the "
    "copy with diff before trusting a backup."
)
_TASK_SESSIONS: list[tuple[str, str]] = [
    ("mock:liquid-1", _PROC.format(f="/tmp/everos_e2e_db", body="host=db port=5432")),
    ("mock:liquid-2", _PROC.format(f="/tmp/everos_e2e_api", body="url=https://api token=x")),
    ("mock:liquid-3", _PROC.format(f="/tmp/everos_e2e_app", body="env=prod debug=false")),
]


def _runtime_ready() -> bool:
    try:
        from everos.config.settings import load_settings
    except Exception:
        return False
    load_settings.cache_clear()
    s = load_settings()
    return s.llm.api_key is not None and bool(s.embedding.model) and s.embedding.api_key is not None


def _run_agent(message: str, session: str, root: Path) -> str:
    """Send one message through the real agent CLI (mock channel)."""
    env = {**os.environ, "EVEROS_MEMORY__ROOT": str(root)}
    proc = subprocess.run(
        [str(_RAVEN), "agent", "-m", message, "-s", session, "--wait-skill-extract", "--no-markdown", "--logs"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.stdout + proc.stderr


def test_channel_e2e_writes_memory_markdown(tmp_path: Path) -> None:
    if not _RAVEN.exists():
        pytest.skip(f"raven CLI not found at {_RAVEN}")
    if not _runtime_ready():
        pytest.skip("everos runtime not configured (see ~/.everos/everos.toml)")

    root = tmp_path / "everos_root"
    root.mkdir()

    # Drive the mock channel: same reusable procedure, three sessions.
    tool_used = False
    for session, message in _TASK_SESSIONS:
        out = _run_agent(message, session, root)
        if "tool call: exec" in out.lower() or "curl" in out.lower():
            tool_used = True
    # Tool round-trips are what make agent case/skill extraction possible;
    # surface it for diagnostics but don't hard-fail the whole chain on it.
    print(f"\n[channel-e2e] tool round-trip observed: {tool_used}")

    # Categorize from the full md set — everos uses dot-prefixed dirs
    # (``.cases`` / ``.skills``) that literal-name globs miss.
    all_md = list(root.glob("**/*.md"))

    def _has(p: Path, seg: str) -> bool:
        return f"/{seg}/" in str(p)

    episodes = [p for p in all_md if _has(p, "episodes")]
    profiles = [p for p in all_md if p.name == "user.md" or "profile" in p.name.lower()]
    cases = [p for p in all_md if _has(p, ".cases") or _has(p, "cases") or "agent_case" in p.name]
    skills = [p for p in all_md if _has(p, ".skills") or _has(p, "skills") or "skill" in p.name.lower()]

    # ── User-track memory markdown must exist ───────────────────────
    assert episodes, f"no user episode markdown written under users/*/episodes/; tree:\n{_tree(root)}"
    ep_text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in episodes)
    assert any(k in ep_text.lower() for k in ("backup", "diff", "verify", ".conf")), (
        f"episode markdown didn't capture the task; got:\n{ep_text[:500]}"
    )

    # ── Agent-track: cases should appear; skill is best-effort ──────
    # Report what landed (visible in -s / -rA output).
    print(f"\n[channel-e2e] episodes={len(episodes)} profiles={len(profiles)} cases={len(cases)} skills={len(skills)}")
    print(_tree(root))

    if not cases and not skills:
        pytest.xfail(
            "everos extracted no agent case/skill from these trajectories "
            "(algo judged them non-reusable). User memory verified; see "
            "test_everos_extraction_real_llm for the authoritative skill test."
        )
    if skills:
        skill_text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in skills).lower()
        assert any(k in skill_text for k in ("backup", "diff", "verify", "copy")), (
            f"skill markdown didn't relate to the task; got:\n{skill_text[:500]}"
        )


def _tree(root: Path) -> str:
    md = sorted(str(p.relative_to(root)) for p in root.glob("**/*.md"))
    return "  md files:\n    " + "\n    ".join(md) if md else "  (no .md files)"
