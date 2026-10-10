"""``python -m raven.node``: run this process as a Raven node.

``--stdio`` serves the host that started it; ``--version`` prints what the
host checks before using a node -- the protocol and the digest of this code.
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m raven.node", description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stdio", action="store_true", help="serve JSON-RPC on stdin and stdout")
    group.add_argument("--version", action="store_true", help="print the protocol and the code digest as JSON")
    args = parser.parse_args(argv)
    if args.version:
        from raven.node import bundle, protocol

        print(json.dumps({"protocol": protocol.PROTOCOL, "digest": bundle.digest()}))
        return 0
    from raven.node.server import run_stdio

    return run_stdio()


if __name__ == "__main__":
    sys.exit(main())
