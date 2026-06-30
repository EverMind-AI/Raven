#!/usr/bin/env python3
"""Hermes subprocess inner for longrun — monkeypatches hermes_time.now, then
dispatches to hermes CLI via hermes_cli.main.main().

Must be invoked with the Python interpreter from hermes's venv so
``import hermes_time`` + ``from hermes_cli.main import main`` succeed.

Env in:
  HERMES_EVAL_FAKE_NOW   — ISO datetime (tz-aware) to patch hermes_time.now
  HERMES_EVAL_TURN_SPEC  — JSON {user_message, session_id, resume}
  HERMES_HOME            — isolated ~/.hermes/ for this persona (optional;
                           hermes reads from ~/.hermes by default)

Stdout: hermes's own output (final response after `--quiet`). We then
append one JSON line with session_id parsed from stderr + success status.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from datetime import datetime


def main() -> None:
    try:
        fake_now_iso = os.environ["HERMES_EVAL_FAKE_NOW"]
        spec = json.loads(os.environ["HERMES_EVAL_TURN_SPEC"])
    except KeyError as exc:
        print(json.dumps({"success": False, "error": f"missing env: {exc}"}), flush=True)
        sys.exit(1)

    fake_now = datetime.fromisoformat(fake_now_iso)
    if fake_now.tzinfo is None:
        from datetime import timezone

        fake_now = fake_now.replace(tzinfo=timezone.utc)

    # Patch BEFORE hermes_cli.main imports cron modules that cache
    # `from hermes_time import now`.
    try:
        import hermes_time  # noqa: E402
    except ImportError as exc:
        print(json.dumps({"success": False, "error": f"hermes_time import: {exc}"}), flush=True)
        sys.exit(1)
    hermes_time.now = lambda: fake_now  # noqa: E731

    user_message = (spec.get("user_message") or "").strip()
    session_id = spec.get("session_id")
    resume = bool(spec.get("resume"))
    if not user_message:
        print(json.dumps({"success": False, "error": "empty user_message"}), flush=True)
        sys.exit(1)

    # Build argv mimicking `hermes chat -q <msg> [--resume <sid>] -Q --pass-session-id`
    argv = ["hermes", "chat", "-q", user_message, "-Q", "--pass-session-id"]
    if resume and session_id:
        argv.extend(["--resume", session_id])
    sys.argv = argv

    # Capture stdout + stderr so we can post-process them into a single JSON
    # payload on the very last line (simpler for caller parsing).
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    buf_out, buf_err = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = buf_out, buf_err

    success = True
    err_msg: str | None = None
    try:
        from hermes_cli.main import main as hermes_main  # noqa: E402

        hermes_main()
    except SystemExit as exc:
        success = exc.code == 0 or exc.code is None
        if not success:
            err_msg = f"hermes exited with code {exc.code}"
    except Exception as exc:
        success = False
        err_msg = f"{type(exc).__name__}: {exc}"
    finally:
        sys.stdout, sys.stderr = orig_stdout, orig_stderr

    stdout_text = buf_out.getvalue()
    stderr_text = buf_err.getvalue()

    # session_id from stderr ("session_id: xxxxx") or passthrough
    sid_match = re.search(r"session[-_]id:\s*(\S+)", stderr_text + stdout_text, re.IGNORECASE)
    session_id_out = sid_match.group(1) if sid_match else (session_id or "unknown")

    # The response body is stdout minus the "session_id: ..." echo.
    response = re.sub(r"^session[-_]id:.*$", "", stdout_text, flags=re.IGNORECASE | re.MULTILINE).strip()

    payload = {
        "success": bool(success),
        "response": response,
        "session_id": session_id_out,
        "fake_now": fake_now_iso,
    }
    if err_msg:
        payload["error"] = err_msg
    if not success:
        payload["stderr_tail"] = stderr_text[-800:]
    # Emit payload on its own last line so HermesAdapter can parse it.
    print(json.dumps(payload, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
