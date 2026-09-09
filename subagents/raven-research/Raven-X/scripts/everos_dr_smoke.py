#!/usr/bin/env python3
"""Live-DR acceptance smoke of the EverOS deferred write path.

Launches a real ``raven agent -m`` one-shot on a multi-hop research
question — the same subprocess shape a measurement batch launches — and
verifies the trajectory was captured into a live EverOS service through
the production assembly chain the synthetic e2e deliberately bypasses:
config JSON -> plugin discovery -> ``EverosBackend`` -> AgentLoop
after-turn store dispatch -> sidecar -> post-hoc flush script.

What it asserts (all structural — extraction content is LLM-dependent):

1. preflight passes (``scripts/everos_preflight.py``);
2. the agent run exits 0;
3. the workspace sidecar ``.everos_sessions.jsonl`` records exactly one
   generated session under the smoke scope;
4. the unprocessed buffer holds at least as many rows as the real
   ``_convert_messages`` yields on the saved session transcript (a lower
   bound: the store slice can carry messages the session save drops,
   e.g. synthetic recovery nudges), and every row respects the 50k clip;
5. nothing was extracted inside the window (no ``users/`` / ``agents/``
   under the project dir before flush);
6. the real ``scripts/everos_flush_batch.py`` promotes the session:
   buffer drains, a user-track episode lands on disk, and — when the
   trajectory is thick enough for everalgo's case gate — an agent case
   appears in the OME background queue's own time.

The smoke config overlays the operator's base config (which supplies the
provider key) with the deferred write profile, ``recall_enabled: false``
and ``flush_timeout_s: 420`` — above the server's
``memorize.session_lock_timeout_seconds`` (360 s) so a slow flush
surfaces the server's own error envelope rather than a client
ReadTimeout. The merged config lands in the workspace with mode 0600
and is deleted before exit either way.

Scope hygiene: everything writes under ``app_id=raven_dr_e2e`` with a
per-run ``project_id=live_<HHMMSS>`` — clearly-labeled smoke artifacts,
hard-isolated from real batch scopes, safe to delete.

Usage:
  uv run python scripts/everos_dr_smoke.py --config ~/my_live_config.json
  uv run python scripts/everos_dr_smoke.py --config cfg.json --question "..." \
      --everos-root /root/.everos

Buffer/disk checks need the EverOS data root to be readable from this
host; pass ``--everos-root ''`` to skip them when the service is remote.

Exit code 0 iff every check passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SMOKE_APP_ID = "raven_dr_e2e"
DEFAULT_QUESTION = (
    "In what year was the Antikythera mechanism recovered from its "
    "shipwreck, who first noticed a gear wheel among the finds and in "
    "which month, and what imaging technique did the Antikythera "
    "Mechanism Research Project use in 2005? Cross-check each fact "
    "against at least two independent sources."
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def note(msg: str) -> None:
    print(f"----  {msg}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--config",
        required=True,
        help="base raven config JSON with a working provider key; "
        "overlaid (not modified) with the smoke memory profile",
    )
    p.add_argument("--question", default=DEFAULT_QUESTION)
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument(
        "--everos-root",
        default="/root/.everos",
        help="EverOS data root for buffer/disk assertions; '' skips them",
    )
    p.add_argument(
        "--workspace",
        default=None,
        help="agent workspace dir (default: a fresh temp dir)",
    )
    p.add_argument(
        "--keep-workspace",
        action="store_true",
        help="keep the workspace even when all checks pass",
    )
    p.add_argument(
        "--project-id",
        default=None,
        help="override the generated live_<HHMMSS> smoke project id",
    )
    p.add_argument(
        "--agent-timeout",
        type=float,
        default=2400.0,
        help="wall budget for the agent run (default %(default)ss)",
    )
    p.add_argument("--skip-preflight", action="store_true")
    p.add_argument(
        "--env-file",
        default=None,
        help="dotenv-style file whose variables (e.g. SERPER_API_KEY, "
        "JINA_API_KEY) are exported into the agent subprocess when not "
        "already in the environment — same contract as the batch "
        "launchers; values are never printed",
    )
    return p.parse_args()


def load_env_file(path: Path, env: dict[str, str]) -> list[str]:
    """Merge KEY=VALUE lines into ``env`` (existing vars win)."""
    added: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and not env.get(key):
            env[key] = value
            added.append(key)
    return added


def raven_executable() -> str:
    sibling = Path(sys.executable).parent / "raven"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("raven")
    if found:
        return found
    raise SystemExit("raven CLI not found next to the interpreter or on PATH; run via `uv run`")


def build_smoke_config(base_config: Path, project_id: str, base_url: str) -> dict:
    with base_config.open(encoding="utf-8") as f:
        cfg = json.load(f)
    # A base config pinned to an older flow label would be rejected by
    # this build's superseded-version gate; the smoke is not a
    # measurement, so let the label fall to the build default.
    cfg.get("drFlow", {}).pop("version", None)
    cfg.setdefault("memory", {})["backend"] = "everos"
    plugin_cfg = cfg.setdefault("plugins", {}).setdefault("config", {})
    everos_cfg = plugin_cfg.setdefault("everos-memory", {})
    everos_cfg.update(
        {
            "mode": "http",
            "base_url": base_url,
            "api_version": "auto",
            "app_id": SMOKE_APP_ID,
            "project_id": project_id,
            "agent_id": "raven_dr",
            "session_id_prefix": "raven_dr",
            "defer_extraction": True,
            "emit_user_messages": True,
            "recall_enabled": False,
            # Above the server's memorize.session_lock_timeout_seconds
            # (360 s): a slow flush returns the server's error envelope
            # instead of a client-side disconnect racing the cancel.
            "flush_timeout_s": 420,
        }
    )
    return cfg


def session_messages(workspace: Path) -> tuple[list[dict], Path | None]:
    """All message lines of the run's single session transcript."""
    files = sorted(workspace.glob("sessions/*/*.jsonl"))
    files = [f for f in files if not f.name.endswith(".partial.jsonl")]
    if len(files) != 1:
        return [], files[0] if files else None
    msgs: list[dict] = []
    with files[0].open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict) and "role" in row and row.get("_type") != "metadata":
                msgs.append(row)
    return msgs, files[0]


def buffer_stats(db: Path, session_id: str) -> tuple[int, int]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        n, longest = con.execute(
            "SELECT COUNT(*), COALESCE(MAX(LENGTH(text)), 0) "
            "FROM unprocessed_buffer WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return n, longest
    finally:
        con.close()


def poll_dir(path: Path, budget_s: float) -> list[Path]:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if path.exists():
            files = [p for p in path.rglob("*") if p.is_file()]
            if files:
                return files
        time.sleep(5)
    return []


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    project_id = args.project_id or f"live_{time.strftime('%H%M%S')}"
    root = Path(args.everos_root).expanduser() if args.everos_root else None
    workspace = Path(args.workspace) if args.workspace else Path(
        tempfile.mkdtemp(prefix="everos_dr_smoke_")
    )
    workspace.mkdir(parents=True, exist_ok=True)
    note(f"scope: app_id={SMOKE_APP_ID} project_id={project_id}")
    note(f"workspace: {workspace}")

    if not args.skip_preflight:
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts/everos_preflight.py"), "--base-url", base_url],
        )
        check("preflight passed", proc.returncode == 0, f"exit={proc.returncode}")
        if proc.returncode != 0:
            return 1

    merged = workspace / "smoke_config.json"
    merged.touch(mode=0o600)
    merged.write_text(
        json.dumps(build_smoke_config(Path(args.config).expanduser(), project_id, base_url)),
        encoding="utf-8",
    )
    try:
        # ── the real run ────────────────────────────────────────────
        env = dict(os.environ)
        env["RAVEN_TRACING"] = "0"
        if args.env_file:
            added = load_env_file(Path(args.env_file).expanduser(), env)
            note(f"env-file: exported {', '.join(added) if added else 'nothing new'}")
        log_path = workspace / "agent_run.log"
        note(f"launching raven agent -m …  (log: {log_path})")
        t0 = time.monotonic()
        with log_path.open("w", encoding="utf-8") as log:
            try:
                proc = subprocess.run(
                    [
                        raven_executable(),
                        "agent",
                        "-m",
                        args.question,
                        "--config",
                        str(merged),
                        "--workspace",
                        str(workspace),
                        "--no-markdown",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                    cwd=REPO,
                    timeout=args.agent_timeout,
                )
                agent_ok = proc.returncode == 0
                detail = f"exit={proc.returncode}, {time.monotonic() - t0:.0f}s"
            except subprocess.TimeoutExpired:
                agent_ok = False
                detail = f"timed out after {args.agent_timeout:.0f}s"
        check("agent run exited 0", agent_ok, detail)
        if not agent_ok:
            note(f"see {log_path}")
            return 1
    finally:
        merged.unlink(missing_ok=True)

    # ── sidecar ─────────────────────────────────────────────────────
    sidecar = workspace / ".everos_sessions.jsonl"
    check("sidecar written", sidecar.exists())
    if not sidecar.exists():
        return 1
    rows = [json.loads(x) for x in sidecar.read_text(encoding="utf-8").splitlines() if x.strip()]
    check("sidecar has exactly one session", len(rows) == 1, f"rows={len(rows)}")
    row = rows[0]
    sid = row.get("session_id", "")
    check(
        "sidecar scope + generated id",
        sid.startswith("raven_dr_")
        and row.get("app_id") == SMOKE_APP_ID
        and row.get("project_id") == project_id,
        sid,
    )

    # ── transcript-derived expectations ─────────────────────────────
    from raven.plugin.memory.everos.backend import EverosBackend

    msgs, session_file = session_messages(workspace)
    check(
        "single session transcript found",
        bool(msgs),
        str(session_file) if session_file else "none",
    )
    expected_min = len(
        EverosBackend._convert_messages(msgs, agent_id="raven_dr", user_id="default")
    )
    tool_rounds = sum(1 for m in msgs if m.get("role") == "assistant" and m.get("tool_calls"))
    note(f"transcript: {len(msgs)} saved messages, {tool_rounds} tool rounds, "
         f"expected >= {expected_min} buffered rows")

    if root is None:
        note("no --everos-root: buffer/disk checks skipped")
    else:
        db = root / ".index/sqlite/system.db"
        n, longest = buffer_stats(db, sid)
        # Lower bound, not equality: the store slice can carry messages
        # the session save drops (synthetic recovery nudges), and the
        # save truncates tool results the buffer keeps at full clip.
        check(
            "buffer rows >= transcript expectation",
            0 < expected_min <= n,
            f"rows={n}, expected>={expected_min}",
        )
        check("capture clipped to 50k", longest <= 50_000, f"max len={longest}")
        proj_dir = root / SMOKE_APP_ID / project_id
        check(
            "no extraction inside the window",
            not (proj_dir / "users").exists() and not (proj_dir / "agents").exists(),
        )

    # ── post-hoc flush through the real batch script ────────────────
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/everos_flush_batch.py"),
            "--sessions-file",
            str(sidecar),
            "--flush-timeout-s",
            "420",
            "--wait-secs",
            "60",
        ],
    )
    check("flush script exit 0", proc.returncode == 0, f"exit={proc.returncode}")

    if root is not None:
        n, _ = buffer_stats(root / ".index/sqlite/system.db", sid)
        check("buffer drained after flush", n == 0, f"rows={n}")
        proj_dir = root / SMOKE_APP_ID / project_id
        episodes = poll_dir(proj_dir / "users", 120)
        check(
            "user-track episode on disk",
            bool(episodes),
            episodes[0].name if episodes else "none in 120s",
        )
        # everalgo's AgentCaseExtractor rejects < 3 tool rounds before any
        # LLM call and only fast-passes its LLM filter above 20 rounds; in
        # between the filter may legitimately decline, so the check is
        # hard only where the outcome is deterministic.
        if tool_rounds < 3:
            note(f"case check skipped: {tool_rounds} tool rounds < 3 (gate rejects by design)")
        else:
            cases = poll_dir(proj_dir / "agents" / "raven_dr" / ".cases", 360)
            if tool_rounds > 20:
                check(
                    "agent case extracted (fast-pass band)",
                    bool(cases),
                    cases[0].name if cases else "none in 360s",
                )
            elif cases:
                note(f"agent case extracted: {cases[0].name}")
            else:
                note(
                    f"no agent case in 360s at {tool_rounds} tool rounds — the "
                    "3..20 band goes through everalgo's LLM filter, so this is "
                    "not by itself a failure"
                )

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if not failed and not args.keep_workspace and args.workspace is None:
        shutil.rmtree(workspace, ignore_errors=True)
        note("workspace removed (use --keep-workspace to keep it)")
    else:
        note(f"workspace kept: {workspace}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
