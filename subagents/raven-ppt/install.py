#!/usr/bin/env python3
"""Register this folder's subagent in the host raven's config.

`subagent.json` ships with `{SUBAGENT_DIR}` and `{PYTHON}` unresolved, so the
published file carries no path from the machine it was built on. The gateway
substitutes only `{agent_id}`, `{prompt}` and `{prompt_file}` and spawns with
the *session workspace* as cwd, so neither a relative command nor a `cwd` field
can stand in for the real path: it has to be resolved at install time, which is
what this does - against this file's own location, so moving the folder and
re-running is the whole migration story.

The entry is written into `subagents.thirdParty` through
`raven.config.update_subagents`, the only supported write path: it validates
against the schema, replaces the entry that shares this one's name, refuses a list
that would hold duplicates, and replaces the file atomically.
Hand-editing the JSON skips all three, and a half-written config is one raven
will not start on. Nothing needs to be running for this, but nothing running
picks it up either - a live raven holds the roster it read at startup, so
restart it (or the gateway) afterwards.

That module lives wherever the host raven is installed, which is not necessarily
the interpreter running this file, so it is reached through a subprocess instead
of an import. This stays standard-library only, and installable under a bare
`python3`.

Two manifests, one name. `subagent.json` is the `cli` entry and stays the default:
it is also the file the host's own folder scan reads, and that scan validates
every `*/subagent.json` as a cli config -- so a folder whose only manifest
declared `kind: "acp"` would be skipped with a warning and disappear from the
roster. `--acp` writes `subagent.acp.json` instead, which registers the same name
over the ACP transport (`acp.py`). Both cannot be installed at once, by
construction: they share a name, and the write path replaces an entry that shares
one.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Runs under the host raven's interpreter, reading one JSON payload on stdin and
# writing one on stdout. Kept to a single `-c` string so this file stays the
# whole installer: a temp file would have to be cleaned up on every error path.
WRITER = r"""
import json
import sys
from pathlib import Path

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.config.update_subagents import add_third_party_subagent, get_third_party_subagents

payload = json.load(sys.stdin)
path = Path(payload["config"]) if payload.get("config") else get_config_path()
backup = Path(payload["backup"])
# Raw, not get_third_party_subagents(): loading coerces fields away (an openai
# entry's readsLocalFiles is dropped), so a validated round-trip is not what was
# on disk, and restoring from it would silently strip whatever it dropped.
raw = read_raw_or_raise(path) if path.exists() else {}
stored = raw.get("subagents") or {}
# All three spellings: the key was renamed to `agents`, and reading only the
# older two makes this backup an empty list on a config that has already been
# migrated -- so the rollback path it exists to provide would wipe every
# configured agent instead of restoring them.
before = stored.get("agents") or stored.get("thirdParty") or stored.get("third_party") or []
# Before the write, not after: a backup that lands only on success is not a
# rollback path. Mode 600 because an openai entry among the others holds its
# own api key.
try:
    backup.write_text(json.dumps(before, indent=2, ensure_ascii=False), encoding="utf-8")
    backup.chmod(0o600)
except OSError as exc:
    raise SystemExit(f"error: cannot write the backup at {backup} ({exc}); the config is untouched")
add_third_party_subagent(payload["entry"], config_path=path)
after = get_third_party_subagents(config_path=path)
json.dump(
    {"config": str(path), "before": [e.get("name") for e in before], "after": [e["name"] for e in after]},
    sys.stdout,
)
"""


def env_value(name: str) -> str | None:
    """Read `name` from the environment, falling back to `.env` beside this file."""
    if value := os.environ.get(name):
        return value.strip()
    env_file = HERE / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip()
    return None


def resolve(entry: dict, python: str) -> dict:
    """Substitute the install-time placeholders in every command field."""
    resolved = dict(entry)
    for field in ("command", "resumeCommand"):
        if template := resolved.get(field):
            resolved[field] = template.replace("{SUBAGENT_DIR}", str(HERE)).replace("{PYTHON}", python)
    return resolved


def host_interpreter(explicit: str | None) -> list[str]:
    """Return the argv prefix for the interpreter that has `raven` importable.

    Read out of the `raven` console script's shebang rather than assumed to be
    the one running this file: the installer is meant to work under a bare
    `python3`, and the runtime usually sits in a tool env of its own. `shlex`
    because the shebang may be an `env` line, which is two words.
    """
    if explicit:
        return shlex.split(explicit)
    if value := env_value("RAVEN_PYTHON"):
        return shlex.split(value)
    script = shutil.which("raven")
    if not script:
        raise SystemExit("error: no `raven` on PATH; pass --raven-python with the interpreter that has raven installed")
    first = Path(script).read_bytes().split(b"\n", 1)[0]
    if not first.startswith(b"#!"):
        raise SystemExit(f"error: {script} carries no shebang to read the interpreter from; pass --raven-python")
    return shlex.split(first[2:].decode("utf-8", "replace").strip())


def write_entry(interpreter: list[str], entry: dict, config: str | None, backup: Path) -> dict:
    payload = json.dumps({"entry": entry, "config": config, "backup": str(backup)})
    proc = subprocess.run([*interpreter, "-c", WRITER], input=payload, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(
            f"error: the write failed under {shlex.join(interpreter)} (exit {proc.returncode}); "
            "if it could not import raven, pass --raven-python with the right interpreter"
        )
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise SystemExit(f"error: cannot read the writer's output ({exc}): {proc.stdout[:400]!r}") from exc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--python",
        default=None,
        help="Interpreter the gateway should invoke (default: $SUBAGENT_PYTHON, "
        "or this interpreter). run.py is standard-library only, so any python3 works.",
    )
    ap.add_argument(
        "--raven-python",
        default=None,
        help="Interpreter that has raven installed, used to perform the write "
        "(default: $RAVEN_PYTHON, or the shebang of `raven` on PATH)",
    )
    ap.add_argument("--config", default=None, help="Config file to write (default: the host raven's own)")
    ap.add_argument("--dry-run", action="store_true", help="Print the resolved entry and stop")
    ap.add_argument(
        "--acp",
        action="store_true",
        help="Register the ACP entry (subagent.acp.json) instead of the cli one; the agent is then "
        "driven as a protocol peer rather than forked per task",
    )
    args = ap.parse_args()

    manifest = HERE / ("subagent.acp.json" if args.acp else "subagent.json")
    if not manifest.is_file():
        raise SystemExit(f"error: {manifest} is missing; this folder cannot be registered")
    template = json.loads(manifest.read_text(encoding="utf-8"))
    python = args.python or env_value("SUBAGENT_PYTHON") or sys.executable
    entry = resolve(template, python)

    if unresolved := [f for f in ("command", "resumeCommand") if "{SUBAGENT_DIR}" in str(entry.get(f, ""))]:
        raise SystemExit(f"error: placeholders left unresolved in {', '.join(unresolved)}")

    print(f"registering {manifest.name} ({entry.get('kind', 'cli')})")
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    interpreter = host_interpreter(args.raven_python)
    if args.dry_run:
        print(f"would write through {shlex.join(interpreter)}")
        return 0

    # The writer takes it, so that a backup which cannot be written stops the
    # install instead of going missing after a config that already changed.
    backup = HERE / f"subagents-backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    result = write_entry(interpreter, entry, args.config, backup)
    print(f"backed up the previous list ({len(result['before'])} entries) to {backup}")

    replaced = entry["name"] in result["before"]
    print(
        f"{'replaced' if replaced else 'added'} {entry['name']} in {result['config']}; "
        f"the list now has {len(result['after'])} entries"
    )
    print("restart raven (or the gateway) to pick it up: a running one holds the roster it read at startup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
