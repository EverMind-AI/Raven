#!/usr/bin/env python3
"""ACP launcher for the raven-ppt sub-agent: render the config, then become the server.

The counterpart of `run.py`, and deliberately a fraction of its size. `run.py`
exists because the CLI sub-agent contract gave it three jobs at once: get the
named files in, keep the key out of a published file, and rebuild a result from
the child's stdout. On ACP only the middle one is still a launcher's job.

- **Material** arrives through the protocol now. The task text still carries the
  fenced block, but it is read inside the server (`raven/acp/materials.py`), at
  `session/prompt` rather than at launch -- so a second turn can add to it.
- **The result** is a `session/prompt` response plus the `session/update` stream.
  Nothing is scraped, so there is no output to parse and no watchdog to kill a
  child whose stdout went quiet.
- **The key** still has to reach the runtime without being committed, which is
  this file's whole remaining purpose: `config.json` ships and holds no secret,
  `.env` supplies it, and the two are merged into a rendered config under the
  state root.

Then `execv`, rather than a subprocess. This process must not sit between the
client and the server: stdin and stdout are the protocol, and a middleman is one
more buffer to flush and one more place a stray byte can enter the frame stream.
Replacing the image hands the descriptors over untouched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# `run.py` owns the config rendering, and there is exactly one right way to merge
# a key into that file. Imported rather than copied so the two launchers cannot
# drift into disagreeing about where the key goes.
sys.path.insert(0, str(HERE))

import run  # noqa: E402  (the path has to be set first)


def main() -> int:
    raven = run.CHECKOUT / ".venv" / "bin" / "raven"
    # Executability, not existence: `subagents/install.sh` classifies the same
    # folder with `[ -x ]`, so a third reader disagreeing on a present-but-
    # unexecutable file would have the installer call this folder ready while the
    # launcher calls it missing.
    if not (raven.is_file() and os.access(raven, os.X_OK)):
        sys.stderr.write(
            f"error: {raven} is not an executable. Build the checkout's venv first:\n"
            f"  cd {run.CHECKOUT} && uv sync --extra ppt\n"
        )
        return 1

    # Diagnostics to stderr, which is the channel an ACP client surfaces. The
    # launcher has no log file of its own here: everything after the exec logs
    # through the runtime's own sink.
    run._VERBOSE = True
    try:
        rendered = run.render_config(Path(os.environ.get("PPT_CONFIG", "").strip() or run.DEFAULT_CONFIG))
    except SystemExit as exc:
        # `render_config` raises SystemExit with the operator's next step in it --
        # a missing key, a host config with nothing to inherit. Written to stderr
        # rather than allowed to propagate, so the client shows the sentence
        # instead of a traceback.
        sys.stderr.write(f"{exc}\n")
        return 1

    argv = [str(raven), "acp", "--config", str(rendered)]
    sys.stderr.write(f"[acp] serving from {run.CHECKOUT} with {rendered}\n")
    sys.stderr.flush()
    # cwd is the checkout, as `run.py` sets it for the CLI: the runtime resolves
    # its bundled templates and skills relative to the installed package, and a
    # launch from an arbitrary directory has been the cause of a missing-skill
    # report before. It is NOT the session's working directory -- that arrives per
    # session in `session/new`.
    os.chdir(run.CHECKOUT)
    os.execv(argv[0], argv)


if __name__ == "__main__":
    sys.exit(main())
