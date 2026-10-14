"""``python -m raven.node``: run this process as a Raven Subagent Server, or as the way to one.

``--serve --dir D`` runs the daemon of this install under the node directory
``D``; ``--attach --dir D`` bridges this process's stdin and stdout to that
daemon, starting it when none is up -- what the host runs over ssh;
``--stdio`` serves the host on stdin and stdout directly, with agents that
end when the host hangs up (tests, and a machine with no socket to spare);
``--version`` prints what the host checks before using a node: the protocol
and the digest of this code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m raven.node", description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stdio", action="store_true", help="serve JSON-RPC on stdin and stdout; agents end with it")
    group.add_argument("--serve", action="store_true", help="run the daemon under --dir until idle or stopped")
    group.add_argument("--attach", action="store_true", help="bridge stdin and stdout to the daemon under --dir")
    group.add_argument("--version", action="store_true", help="print the protocol and the code digest as JSON")
    parser.add_argument("--dir", help="the node directory on this machine (run/, tasks/, log/ live under it)")
    parser.add_argument("--idle", type=float, default=None, help="seconds a daemon stays with nothing to do")
    args = parser.parse_args(argv)
    if args.version:
        from raven.node import bundle, protocol

        print(json.dumps({"protocol": protocol.PROTOCOL, "digest": bundle.digest()}))
        return 0
    if args.stdio:
        from raven.node.server import run_stdio

        return run_stdio()
    if not args.dir:
        parser.error("--serve and --attach need --dir")
    root = Path(args.dir).expanduser()
    if args.serve:
        from raven.node.server import IDLE_EXIT_S, run_daemon

        try:
            return run_daemon(root, idle_s=IDLE_EXIT_S if args.idle is None else args.idle)
        except PermissionError as exc:
            # The socket's place is not this user's to use; said in one line, in the serve log.
            print(f"raven node: {exc}", file=sys.stderr)
            return 1
    from raven.node.relay import run_attach

    return run_attach(root)


if __name__ == "__main__":
    sys.exit(main())
