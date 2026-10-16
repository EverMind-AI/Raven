"""An agent on a machine, as the bridge tests need one: it spawns the install's bridge the way an agent would, and relays.

Started by the daemon as a task, with the bridge's whole argv as its own
arguments. Every line on its stdin goes to the bridge's stdin, every line the
bridge writes goes out on stdout (so the host reads it as the agent's frames),
and when the bridge leaves it says so on one line: ``BRIDGE_EXIT <code>
<stderr>``. Then it lives on until its own stdin ends -- an agent outlives one
of its MCP servers -- so its task is still running for whatever the test asks
of the daemon next; the end of its stdin ends the bridge's too.
"""

import contextlib
import subprocess
import sys
import threading


def main() -> int:
    bridge = subprocess.Popen(  # noqa: S603 - the test's own argv
        sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert bridge.stdin is not None and bridge.stdout is not None and bridge.stderr is not None

    def inward() -> None:
        # Once the bridge is gone, what comes on stdin is read and dropped:
        # the agent goes on until its own stdin ends.
        open_ = True
        for line in sys.stdin.buffer:
            if not open_:
                continue
            try:
                bridge.stdin.write(line)
                bridge.stdin.flush()
            except OSError:
                open_ = False
        with contextlib.suppress(OSError):
            bridge.stdin.close()

    reading = threading.Thread(target=inward, daemon=True)
    reading.start()
    for line in bridge.stdout:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
    code = bridge.wait()
    said = bridge.stderr.read().decode("utf-8", "replace").strip().replace("\n", " ")
    sys.stdout.buffer.write(f"BRIDGE_EXIT {code} {said}\n".encode())
    sys.stdout.buffer.flush()
    reading.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
