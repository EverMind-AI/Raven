#!/usr/bin/env python3
"""EverOS memory round-trip check — store -> extract -> recall against a
real everos runtime, using a live Raven config.

Unlike the ``real_llm`` integration tests (which point
``EVEROS_MEMORY__ROOT`` at a throwaway tmp dir for isolation), this
script drives the **real** everos memory root configured on the machine
(``~/.everos`` by default), so it both verifies the wiring AND leaves
inspectable artifacts under
``<root>/<app>/<project>/users/<user_id>/user.md``.

It reads the plugin slice from an Raven config file
(``plugins.config["everos-memory"]``), builds the embedded backend with
exactly that config, stores a small demo corpus, drains the async
extraction pipeline, then recalls — printing hits and the on-disk path
where the user-track memory landed.

Requires a working everos LLM + embedding runtime (``~/.everos/config.toml``
or ``EVEROS_*`` env). Needs network + filesystem access, so run it from a
normal shell, not a sandboxed one.

Usage:
  python scripts/everos_memory_roundtrip.py                 # default config + demo corpus
  python scripts/everos_memory_roundtrip.py --recall-only   # skip store, just query
  python scripts/everos_memory_roundtrip.py --track agent    # exercise the agent track
  python scripts/everos_memory_roundtrip.py --query "..." --top-k 10
  python scripts/everos_memory_roundtrip.py --config /path/to/config.json

Exit code 0 when the round-trip succeeds with >=1 recall hit; non-zero
otherwise (store error, no hits, or backend degraded to no-op).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".raven" / "config.json"
DEFAULT_WORKSPACE = Path.home() / ".raven" / "workspace"

# Demo conversation whose user turns carry extractable profile facts.
_DEMO_CORPUS = [
    {"role": "user", "content": "My name is Raven and I work as a backend engineer."},
    {"role": "assistant", "content": "Nice to meet you, Raven."},
    {"role": "user", "content": "I strongly prefer Python over Java, and I drink oat-milk lattes."},
    {"role": "assistant", "content": "Got it - Python and oat-milk lattes noted."},
    {"role": "user", "content": "I live in Shanghai and usually work late at night."},
    {"role": "assistant", "content": "Understood."},
]


def _load_plugin_slice(config_path: Path) -> dict:
    """Read ``plugins.config["everos-memory"]`` from an Raven config,
    falling back to the friendlier ``"everos"`` key."""
    cfg = json.loads(config_path.read_text())
    slices = cfg.get("plugins", {}).get("config", {})
    slice_ = slices.get("everos-memory") or slices.get("everos") or {}
    if not slice_:
        print(
            f"warning: no plugins.config['everos-memory'] in {config_path}; "
            "backend will run on its built-in defaults.",
            file=sys.stderr,
        )
    return slice_


async def _drain_extraction(deadline: float, interval: float = 0.5) -> None:
    """Block until everos's async extraction + md->lancedb cascade is
    idle. everos ``memorize`` returns before the background strategies
    finish, so recall before draining can miss freshly-stored facts.

    Degrades to a fixed sleep if the everos internals this relies on
    have moved (they are not part of a stable public API)."""
    try:
        from everos.infra.persistence.sqlite import md_change_state_repo
        from everos.service.memorize import _get_engine
    except Exception as e:  # noqa: BLE001 - internal API may move
        print(f"  (drain helper unavailable: {e}; sleeping {interval * 6:.0f}s instead)")
        await asyncio.sleep(interval * 6)
        return

    engine = _get_engine()
    async with asyncio.timeout(deadline):
        while True:
            if await engine.wait_idle(timeout=0.5):
                summary = await md_change_state_repo.queue_summary()
                if summary.pending == 0:
                    return
            await asyncio.sleep(interval)


def _everos_user_dir(user_id: str) -> Path | None:
    """Resolve the on-disk dir everos writes user-track markdown to, so
    the caller can eyeball ``user.md``. Best-effort: returns None if the
    everos settings layout can't be resolved."""
    try:
        from everos.config import load_settings

        root = Path(load_settings().memory.root).expanduser()
    except Exception:  # noqa: BLE001
        return None
    return root / "default_app" / "default_project" / "users" / user_id


async def _run(args: argparse.Namespace) -> int:
    from raven.plugin import PluginContext, ServiceLocator
    from raven.plugin.memory.everos.backend import EverosBackend, _RealEverosAdapter

    slice_ = _load_plugin_slice(args.config)
    be = EverosBackend(PluginContext(
        config=slice_,
        services=ServiceLocator(workspace=args.workspace),
    ))
    if not isinstance(be._adapter, _RealEverosAdapter):
        print(
            "FAIL: embedded backend degraded to no-op adapter — everos isn't "
            "importable / configured. Check ~/.everos/config.toml.",
            file=sys.stderr,
        )
        return 2

    user_id = be._user_id
    agent_id = be._agent_id
    print(f"config:   {args.config}")
    print(f"mode={be._mode}  user_id={user_id!r}  agent_id={agent_id!r}  track={args.track}")

    if args.track == "user" and not user_id:
        print("FAIL: user track requested but plugin config has no user_id.", file=sys.stderr)
        return 2

    await be.start()
    try:
        if not args.recall_only:
            print(f"storing {args.times} turn(s) of demo corpus...")
            for i in range(args.times):
                await be.store(f"everos-roundtrip-{i}", _DEMO_CORPUS)
            print("draining extraction pipeline (may take a minute)...")
            await _drain_extraction(deadline=args.deadline)
            print("drained.")

        recall_kwargs = {"user_id": user_id} if args.track == "user" else {"agent_id": agent_id}
        hits = await be.recall(args.query, top_k=args.top_k, **recall_kwargs)
        print(f"\nrecall({args.track}, query={args.query!r}) -> {len(hits)} hit(s)")
        for h in hits:
            print(f"  [{h.score:.3f}] {h.text[:140]}")
    finally:
        await be.stop()

    if args.track == "user":
        d = _everos_user_dir(user_id)
        if d is not None:
            exists = (d / "user.md").is_file()
            print(f"\nuser-track markdown: {d / 'user.md'}  ({'present' if exists else 'absent'})")

    if not hits:
        print("\nFAIL: recall returned no hits.", file=sys.stderr)
        return 1
    print("\nOK: round-trip succeeded.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="EverOS memory store->recall round-trip check.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help=f"Raven config file (default: {DEFAULT_CONFIG})")
    p.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                   help=f"Workspace dir for ServiceLocator (default: {DEFAULT_WORKSPACE})")
    p.add_argument("--track", choices=("user", "agent"), default="user",
                   help="Which track to recall (default: user)")
    p.add_argument("--query", default="what are the user's preferences and job",
                   help="Recall query string")
    p.add_argument("--top-k", type=int, default=5, help="Recall top-K (default: 5)")
    p.add_argument("--times", type=int, default=3,
                   help="How many store() calls of the demo corpus (default: 3)")
    p.add_argument("--recall-only", action="store_true",
                   help="Skip store; recall against whatever is already indexed")
    p.add_argument("--deadline", type=float, default=180.0,
                   help="Max seconds to wait for extraction to drain (default: 180)")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
