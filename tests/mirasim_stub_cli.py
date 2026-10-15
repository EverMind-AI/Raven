"""A stand-in ``mirasim`` executable, run as a real child process by the Mirasim adapter tests.

Not a test module (pytest collects ``test_*``). It answers ``version`` and the
``ui-cli`` commands the adapter runs, in the shapes measured against Mirasim
0.0.465: server log lines on stdout around the one JSON document, one of them
carrying the server's token, and a failed turn that still exits 0. A turn
announces its server's port the way a real one does, and the commands sent to
that port (``raw`` frames, ``answer``) reach the turn through files, so a test
can watch a turn stop on a question and be answered from outside. A session's
route is kept per ``--workdir``, the way Mirasim keeps it in an index per server
directory: ``prompt --route`` sets it for a new session only, ``session route``
sets or clears it for any key without checking the key, and a turn's reply says
the route it ran on when it is not Mirasim's default.

State lives in ``MIRASIM_STUB_DIR``: ``calls.jsonl`` records every argv, so a
test can read what the adapter asked for, ``sessions.json`` holds the
conversations this stand-in minted, so a second process continues the first
one's, and ``routes.json`` the routes set, by server directory. ``agents.json``, ``catalog.json`` and ``questions.json`` there, when
present, replace the built-in answers, so a test can change Mirasim's menus
under a running adapter or what a turn asks. ``turns/<port>.json`` is a running
turn as its server shows it, and ``answers/<promptId>.json`` an answer given to
it. ``MIRASIM_STUB_MODE`` picks how the commands behave:

- ``ok``      -- answers with what it was asked, on which model and effort.
- ``fail``    -- the measured Codex failure: ``error`` set, exit status 0.
- ``forget``  -- every ``--session`` is refused as unknown; a new one works.
- ``hang``    -- starts a grandchild in a session of its own, records its pid in
  ``grandchild.pid``, and never answers, printing a server log line every
  fifth of a second the way a live server keeps logging.
- ``asks``    -- stops on each interaction in ``questions.json`` (one choice
  question by default) until it is answered, then settles with the answers.
- ``flaky``   -- asks like ``asks``, and the first ``answer`` sent fails.
- ``ask``     -- comes back unsettled on a question, the way a turn returns once
  its command's own timeout runs out with nobody having answered.
- ``broken``  -- ``agents`` and ``catalog`` fail; ``version`` still answers.
- ``stray``   -- answers like ``ok``, after starting a helper that inherits its
  output and outlives it by half a minute, its pid in ``stray.pid``.
- ``mute``    -- ``version`` fails; everything else answers.
- ``nomenu``  -- ``catalog`` fails; everything else answers.
- ``slow``    -- ``version`` and ``agents`` take five seconds.
- ``stubborn`` -- ``agents`` takes half a minute and ignores SIGTERM while it does.
- ``unsettled`` -- a turn comes back neither settled nor asking anything.
- ``garbage`` -- a turn prints only log lines and exits 3 with nothing on stderr.
- ``noroute`` -- ``session route`` fails; everything else answers like ``ok``.
- ``routehang`` -- ``session route`` never answers; everything else answers like ``ok``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def install(folder: Path, mode: str = "ok") -> Path:
    """Write a ``mirasim`` into ``folder`` that runs this stand-in in ``mode``.

    State goes to ``folder/stub``, which a second install in another mode
    shares: a conversation minted in one mode is continued in the next.
    """
    state = folder / "stub"
    state.mkdir(parents=True, exist_ok=True)
    exe = folder / "mirasim"
    exe.write_text(
        f'#!/bin/sh\nMIRASIM_STUB_DIR="{state}" MIRASIM_STUB_MODE="{mode}" exec "{sys.executable}" "{__file__}" "$@"\n'
    )
    exe.chmod(0o755)
    return exe


STATE = Path(os.environ.get("MIRASIM_STUB_DIR", "."))
MODE = os.environ.get("MIRASIM_STUB_MODE", "ok")
TOKEN_LINE = "[server 12:31:37] ui-cli server on http://127.0.0.1:4970/?token=stub-secret-token"
ASK_DEADLINE_S = 120.0
ROUTE_SHOWN = {"platform": "cloud", "own": "local", "default": "(global default)"}

AGENTS = {
    "agents": [
        {"id": "claude", "label": "Claude Code", "installed": True, "capabilities": {}},
        {"id": "zcode", "label": "ZCode CLI", "installed": False, "capabilities": {}},
        {"id": "qwen", "label": "Qwen Code", "installed": True, "capabilities": {}},
    ]
}

RUNGS = [{"id": "low", "label": "Low"}, {"id": "high", "label": "High"}, {"id": "max", "label": "Max"}]

CATALOG = {
    "defaultModel": "sonnet",
    "defaultEffort": "high",
    "models": [
        {"id": "sonnet", "label": "Sonnet"},
        {"id": "opus", "label": "Opus"},
        {"id": "glm-5.3-flash", "label": "GLM-5.3 Flash"},
        {"id": "profile:abc", "label": "My key"},
    ],
    "routeByModel": {"glm-5.3-flash": "relay", "profile:abc": "direct"},
    "effort": RUNGS,
    "effortByModel": {"sonnet": RUNGS, "opus": RUNGS, "glm-5.3-flash": [], "profile:abc": RUNGS[:2]},
}

QUESTION = {
    "promptId": "claude-control-1",
    "kind": "user-input",
    "title": "Claude asks",
    "hint": None,
    "questions": [
        {
            "id": "q0",
            "header": "Colour",
            "text": "Which colour?",
            "type": "select",
            "choices": [{"label": "Red", "value": "Red"}, {"label": "Blue", "value": "Blue"}],
            "allowFreeform": True,
            "allowEmpty": False,
        }
    ],
    "currentIndex": 0,
}


def emit(doc: object) -> None:
    print(TOKEN_LINE)
    print("[model-inject 12:31:38] route resolved")
    print(json.dumps(doc, indent=2))
    print("[server 12:31:39] shutting down")


def flags(argv: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for arg in argv:
        if arg.startswith("--") and "=" in arg:
            key, _, value = arg[2:].partition("=")
            out.setdefault(key, []).append(value)
    return out


def answer(name: str, default: object) -> object:
    path = STATE / name
    return json.loads(path.read_text()) if path.exists() else default


def sessions() -> dict[str, int]:
    path = STATE / "sessions.json"
    return json.loads(path.read_text()) if path.exists() else {}


def routes() -> dict[str, dict[str, str]]:
    path = STATE / "routes.json"
    return json.loads(path.read_text()) if path.exists() else {}


def put_route(workdir: str, key: str, route: str) -> None:
    held = routes()
    if route == "default":
        held.get(workdir, {}).pop(key, None)
    else:
        held.setdefault(workdir, {})[key] = route
    (STATE / "routes.json").write_text(json.dumps(held))


def turn_file(port: int) -> Path:
    return STATE / "turns" / f"{port}.json"


def show(port: int, key: str, interactions: list[object], phase: str = "streaming") -> None:
    """What the turn's server shows a ``subscribe`` for the turn right now."""
    path = turn_file(port)
    path.parent.mkdir(exist_ok=True)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps({"sessionKey": key, "phase": phase, "interactions": interactions}))
    scratch.replace(path)


def announce() -> int:
    """Print the line a real turn's private server boots with, and return its port."""
    port = 41000 + os.getpid() % 20000
    print(
        f"[server 12:31:37] mirasim server v0.0.465: http://localhost:{port}/?token=stub-secret-token (workdir /w)",
        flush=True,
    )
    return port


def ask_through(port: int, key: str) -> str | None:
    """Stop on each question until it is answered; the answers, or ``None`` when nobody answered."""
    got: list[str] = []
    deadline = time.monotonic() + ASK_DEADLINE_S
    for item in answer("questions.json", [QUESTION]):
        show(port, key, [item])
        given = STATE / "answers" / f"{item['promptId']}.json"
        while not given.exists():
            if time.monotonic() > deadline:
                return None
            time.sleep(0.05)
        reply = json.loads(given.read_text())
        got.append(f"{reply['action']}={reply['value']}")
    show(port, key, [], phase="done")
    return " | ".join(got)


def prompt(argv: list[str], workdir: str) -> int:
    given = flags(argv)
    text = given.get("text", [""])[0]
    key = given.get("session", [None])[0]
    known = sessions()
    port = announce()
    if MODE == "hang":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            start_new_session=True,
        )
        (STATE / "grandchild.pid").write_text(str(child.pid))
        show(port, key or "sess-hang", [])
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            print("[server 12:31:40] turn still running", flush=True)
            time.sleep(0.2)
        return 0
    if MODE == "stray":
        helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        (STATE / "stray.pid").write_text(str(helper.pid))
    if key is not None and (MODE == "forget" or key not in known):
        print(TOKEN_LINE)
        print("ui-cli: --session: not one of this server's sessions", file=sys.stderr)
        return 1
    if key is None:
        key = f"sess-{len(known) + 1}"
        known[key] = 0
        if given.get("route"):
            put_route(workdir, key, given["route"][0])
    known[key] += 1
    route = routes().get(workdir, {}).get(key, "default")
    (STATE / "sessions.json").write_text(json.dumps(known))
    base = {"sessionKey": key, "taskId": f"task-{known[key]}", "queued": False, "phase": "done"}
    if MODE == "fail":
        emit(
            {
                **base,
                "text": "",
                "error": "failed to initialize sqlite state runtime",
                "incomplete": True,
                "settled": True,
            }
        )
        return 0
    if MODE == "unsettled":
        emit({**base, "phase": "running", "text": "", "error": None, "incomplete": False, "settled": False})
        return 0
    if MODE == "garbage":
        print(TOKEN_LINE)
        return 3
    if MODE == "ask":
        unanswered = {
            **base,
            "phase": "streaming",
            "interactions": [QUESTION, "not an interaction", {"title": "no prompt id"}],
            "settled": False,
        }
        emit(unanswered)
        return 0
    if MODE in ("asks", "flaky"):
        answers = ask_through(port, key)
        if answers is None:
            emit({**base, "phase": "streaming", "interactions": [QUESTION], "settled": False})
            return 0
        emit({**base, "text": f"turn {known[key]} of {key} got {answers}", "error": None, "settled": True})
        return 0
    files = [f"{Path(path).name}:{Path(path).stat().st_size}" for path in given.get("file", [])]
    via = "" if route == "default" else f" via {route}"
    reply = (
        f"turn {known[key]} of {key} on {given.get('model', ['-'])[0]}{via} "
        f"effort {given.get('effort', ['-'])[0]} files {files}: {text}"
    )
    emit({**base, "text": reply, "error": None, "incomplete": False, "settled": True})
    return 0


def on_port(port: int, argv: list[str]) -> int:
    """A command sent to a running turn's server: it prints the document alone, as a real one does."""
    command = argv[0] if argv else ""
    path = turn_file(port)
    turn = json.loads(path.read_text()) if path.exists() else None
    if command == "raw":
        frame = json.loads(argv[1])
        responses: list[object] = []
        if frame.get("type") == "listSessions":
            rows: list[object] = [{"sessionKey": "claude:elsewhere", "agent": "claude", "runState": "running"}]
            if turn is not None:
                rows.append({"sessionKey": turn["sessionKey"], "runState": "running", "transport": {"inFlight": 1}})
            responses.append({"type": "sessions", "sessions": rows, "hasMore": False})
        elif frame.get("type") == "subscribe":
            if turn is not None and turn["sessionKey"] == frame.get("sessionKey"):
                snapshot = {"phase": turn["phase"], "interactions": turn["interactions"], "text": ""}
                responses.append({"type": "snapshot", "sessionKey": turn["sessionKey"], "seq": 2, "snapshot": snapshot})
            responses.append({"type": "outbox", "sessionKey": frame.get("sessionKey"), "items": [], "baseline": True})
        print(json.dumps({"sent": frame, "responses": responses}, indent=2))
        return 0
    if command == "answer":
        given = flags(argv[1:])
        prompt_id = given["prompt-id"][0]
        if MODE == "flaky" and not (STATE / "flaked").exists():
            (STATE / "flaked").write_text("1")
            print("mirasim ui-cli: timed out waiting for the host answer", file=sys.stderr)
            return 1
        (STATE / "answers").mkdir(exist_ok=True)
        reply = {"action": given["action"][0], "value": given.get("value", [None])[0]}
        (STATE / "answers" / f"{prompt_id}.json").write_text(json.dumps(reply))
        print(json.dumps({"answered": prompt_id, "action": reply["action"]}, indent=2))
        return 0
    print(f"unknown command {argv}", file=sys.stderr)
    return 2


def main() -> int:
    argv = sys.argv[1:]
    with (STATE / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    if argv[:1] == ["version"]:
        if MODE == "mute":
            print("version: cannot read the install", file=sys.stderr)
            return 1
        if MODE == "slow":
            time.sleep(5)
        print("0.0.465")
        return 0
    rest = argv[1:] if argv[:1] == ["ui-cli"] else []
    if rest and rest[0].startswith("--port="):
        return on_port(int(rest[0].partition("=")[2]), rest[1:])
    workdir = os.getcwd()
    if rest and rest[0].startswith("--workdir="):
        workdir = rest[0].partition("=")[2]
        rest = rest[1:]
    command = rest[0] if rest else ""
    if (MODE == "broken" and command in ("agents", "catalog")) or (MODE == "nomenu" and command == "catalog"):
        print(TOKEN_LINE)
        print("ui-cli: the server could not start", file=sys.stderr)
        return 1
    if command == "agents":
        if MODE == "stubborn":
            import signal

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(30 if MODE == "stubborn" else 5 if MODE == "slow" else 0)
        emit(answer("agents.json", AGENTS))
        return 0
    if command == "catalog":
        emit(answer("catalog.json", CATALOG))
        return 0
    if command == "prompt":
        return prompt(rest[1:], workdir)
    if rest[:2] == ["session", "route"]:
        given = flags(rest[2:])
        if MODE == "noroute":
            print(TOKEN_LINE)
            print("ui-cli: the host did not answer session route", file=sys.stderr)
            return 1
        if MODE == "routehang":
            print(TOKEN_LINE, flush=True)
            time.sleep(600)
        key, route = given["session"][0], given["to"][0]
        put_route(workdir, key, route)
        emit({"sessionKey": key, "route": ROUTE_SHOWN[route]})
        return 0
    print(f"unknown command {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
