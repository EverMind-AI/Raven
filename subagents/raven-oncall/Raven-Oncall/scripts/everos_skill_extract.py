#!/usr/bin/env python3
"""EverOS agent-skill extraction check — distil a weather skill, verify it.

Drives the production agent-track path against a real everos runtime using
a live Raven config: stores several verify-before-report weather tool
trajectories, lets everos extract a case per session, cluster them, and
distil ONE reusable ``agent_skill``, then recalls it through
``EverosBackend.recall(agent_id=...)`` (the same call ``EverosSkillSource``
makes to fill the ``# Skills`` context section).

Like ``everos_memory_roundtrip.py`` this targets the **real** everos memory
root (``~/.everos`` by default), so it leaves an inspectable artifact at
``<root>/default_app/default_project/agents/<agent_id>/skills/<skill>/SKILL.md``.

Requires a working everos LLM + embedding runtime (``~/.everos/config.toml``
or ``EVEROS_*`` env) and network + filesystem access — run it from a normal
shell, not a sandboxed one. Forces ``EVEROS_MEMORIZE__MODE=agent`` (unless
already set) so the agent-memory pipeline runs.

Usage:
  python scripts/everos_skill_extract.py                      # default config, real ~/.everos
  python scripts/everos_skill_extract.py --query "..." --top-k 10
  python scripts/everos_skill_extract.py --config /path/to/config.json

Exit code 0 when a weather skill is extracted AND recalled; non-zero otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Must be set before everos imports so the agent-memory pipeline runs.
os.environ.setdefault("EVEROS_MEMORIZE__MODE", "agent")

DEFAULT_CONFIG = Path.home() / ".raven" / "config.json"
DEFAULT_WORKSPACE = Path.home() / ".raven" / "workspace"

_RECALL_QUERY = "retrieve and verify the current weather conditions for a city"
_CITIES = [
    ("Tokyo", "+18C", "Partly cloudy"),
    ("London", "+12C", "Light rain"),
    ("Paris", "+15C", "Sunny"),
    ("Berlin", "+10C", "Overcast"),
    ("Madrid", "+22C", "Clear"),
    ("Oslo", "+3C", "Snow"),
    ("Cairo", "+30C", "Sunny"),
    ("Lima", "+19C", "Cloudy"),
]


def _weather_session(city: str, temp: str, cond: str, user_id: str) -> list[dict]:
    """A channel weather dialog with a real two-step verify-before-report
    tool trajectory. The shared shape across cities lets everos cluster the
    per-session cases into one reusable weather skill."""
    return [
        {"role": "user", "sender_id": user_id, "content": f"What's the weather in {city} right now?"},
        {
            "role": "assistant",
            "content": f"Checking {city} — I'll query wttr.in for a one-line summary.",
            "tool_calls": [
                {
                    "id": f"{city}-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": f'{{"command": "curl -s wttr.in/{city}?format=3"}}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": f"{city}-1", "content": f"{city}: {cond} {temp}"},
        {
            "role": "assistant",
            "content": "Re-querying condition+temp+wind to verify before reporting.",
            "tool_calls": [
                {
                    "id": f"{city}-2",
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "arguments": f'{{"command": "curl -s wttr.in/{city}?format=%C+%t+%w"}}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": f"{city}-2", "content": f"{cond} {temp} 11km/h"},
        {
            "role": "assistant",
            "content": (
                f"It's {cond.lower()} and {temp} in {city}. Procedure: query "
                "wttr.in/<city>?format=3 for a quick summary, then re-query "
                "wttr.in/<city>?format=%C+%t+%w to confirm condition, temperature "
                "and wind before reporting. Always verify with the second query."
            ),
        },
    ]


def _load_plugin_slice(config_path: Path) -> dict:
    cfg = json.loads(config_path.read_text())
    slices = cfg.get("plugins", {}).get("config", {})
    return slices.get("everos-memory") or slices.get("everos") or {}


async def _drain(deadline: float, interval: float = 0.5) -> None:
    try:
        from everos.infra.persistence.sqlite import md_change_state_repo
        from everos.service.memorize import _get_engine
    except Exception as e:  # noqa: BLE001 - internal API may move
        print(f"  (drain helper unavailable: {e}; sleeping {interval * 8:.0f}s)")
        await asyncio.sleep(interval * 8)
        return
    engine = _get_engine()
    async with asyncio.timeout(deadline):
        while True:
            if await engine.wait_idle(timeout=0.5):
                if (await md_change_state_repo.queue_summary()).pending == 0:
                    return
            await asyncio.sleep(interval)


async def _run(args: argparse.Namespace) -> int:
    from everos.config import load_settings
    from everos.memory.search.dto import SearchRequest
    from everos.service.search import search

    from raven.plugin import PluginContext, ServiceLocator
    from raven.plugin.memory.everos.backend import EverosBackend, _RealEverosAdapter

    slice_ = _load_plugin_slice(args.config)
    # flush_every_turns=1 → each store() is a boundary flush, so extraction
    # runs deterministically per weather session (no accumulate-by-volume).
    slice_ = {"mode": "embedded", "flush_every_turns": 1, **slice_}
    be = EverosBackend(PluginContext(config=slice_, services=ServiceLocator(workspace=args.workspace)))
    if not isinstance(be._adapter, _RealEverosAdapter):
        print("FAIL: backend degraded to no-op — everos not importable/configured.", file=sys.stderr)
        return 2

    user_id = be._user_id or "user-default"
    agent_id = be._agent_id
    root = Path(load_settings().memory.root).expanduser()
    print(f"config: {args.config}\nroot:   {root}\nagent_id={agent_id!r} user_id={user_id!r}")

    await be.start()
    try:
        print(f"storing {len(_CITIES)} weather sessions...")
        for i, (city, temp, cond) in enumerate(_CITIES):
            await be.store(f"weather-{city.lower()}-{i}", _weather_session(city, temp, cond, user_id))
        print("draining extraction + clustering (may take a few minutes)...")
        await _drain(deadline=args.deadline)
        print("drained.\n")

        data = (await search(SearchRequest(agent_id=agent_id, query=args.query, top_k=10))).data
        print(f"agent_skills extracted: {len(data.agent_skills)}")
        for s in data.agent_skills:
            print(
                f"  SKILL {s.name!r} conf={s.confidence:.2f} "
                f"maturity={s.maturity_score:.2f} from {len(s.source_case_ids)} cases"
            )

        hits = await be.recall(args.query, agent_id=agent_id, top_k=10)
    finally:
        await be.stop()

    print(f"\nproduction recall(agent_id={agent_id!r}) -> {len(hits)} hit(s)")
    for h in hits[:3]:
        print(f"  [{h.score:.3f}] type={(h.metadata or {}).get('type')} {h.text[:100]}")

    skills_dir = root / "default_app" / "default_project" / "agents" / agent_id / "skills"
    mds = list(skills_dir.rglob("SKILL.md")) if skills_dir.is_dir() else []
    print(f"\nSKILL.md files under {skills_dir}:")
    for p in mds:
        print("  ", p)

    ok = bool(data.agent_skills) and bool(hits) and bool(mds)
    print(
        "\n"
        + (
            "OK: weather skill extracted, recallable, and on disk."
            if ok
            else "FAIL: skill not extracted / not recallable / no SKILL.md."
        )
    )
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="EverOS agent-skill extraction check.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    p.add_argument("--query", default=_RECALL_QUERY)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--deadline", type=float, default=240.0, help="Max seconds to wait for extraction to drain.")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
