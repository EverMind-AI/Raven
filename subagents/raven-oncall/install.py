#!/usr/bin/env python3
"""Register this folder's subagent with a running Raven gateway.

`subagent.json` ships with `{SUBAGENT_DIR}` and `{PYTHON}` unresolved, so the
published file carries no path from the machine it was built on. The gateway
substitutes only `{agent_id}`, `{prompt}` and `{prompt_file}` and spawns with
the *session workspace* as cwd, so neither a relative command nor a `cwd` field
can stand in for the real path: it has to be resolved at install time, which is
what this does - against this file's own location, so moving the folder and
re-running is the whole migration story.

The write goes through `PUT /raven/subagents`, not by editing the gateway's
config file: the RPC validates the entry and hot-applies it, where a file edit
is picked up only on restart and can be silently overwritten. The endpoint
takes the *entire* list, so this reads the current one first, merges this entry
into it by name, and keeps a timestamped backup.

Standard library only, and no `import raven` - this must run under a bare
`python3` on a machine that has never installed the runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_ENDPOINT = "http://127.0.0.1:8000/raven/subagents"


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


def request(url: str, method: str, payload: object | None = None) -> object:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    # The gateway is on loopback; an inherited http_proxy would send this out to
    # the internet, which is both wrong and a way to leak the payload.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else None


def entries_of(payload: object) -> tuple[list, str | None]:
    """Return the subagent list and the key it sat under, if any.

    The endpoint has been seen returning both a bare list and a wrapper object;
    preserving the shape matters because the PUT has to send back the same one.
    """
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        for key in ("agents", "subagents", "thirdParty", "third_party", "items"):
            if isinstance(payload.get(key), list):
                return payload[key], key
    raise SystemExit(f"error: cannot find a subagent list in the response: {payload!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument(
        "--python",
        default=None,
        help="Interpreter the gateway should invoke (default: $SUBAGENT_PYTHON, "
        "or this interpreter). run.py is standard-library only, so any python3 works.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print the resolved entry and stop")
    args = ap.parse_args()

    template = json.loads((HERE / "subagent.json").read_text(encoding="utf-8"))
    python = args.python or env_value("SUBAGENT_PYTHON") or sys.executable
    entry = resolve(template, python)

    if unresolved := [f for f in ("command", "resumeCommand") if "{SUBAGENT_DIR}" in str(entry.get(f, ""))]:
        raise SystemExit(f"error: placeholders left unresolved in {', '.join(unresolved)}")

    print(json.dumps(entry, indent=2, ensure_ascii=False))
    if args.dry_run:
        return 0

    try:
        payload = request(args.endpoint, "GET")
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: cannot reach the gateway at {args.endpoint}: {exc}") from exc

    entries, key = entries_of(payload)
    backup = HERE / f"subagents-backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    backup.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"backed up the current list ({len(entries)} entries) to {backup}")

    merged = [e for e in entries if e.get("name") != entry["name"]]
    replaced = len(merged) != len(entries)
    merged.append(entry)

    request(args.endpoint, "PUT", merged if key is None else {**payload, key: merged})
    print(f"{'replaced' if replaced else 'added'} {entry['name']}; the list now has {len(merged)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
