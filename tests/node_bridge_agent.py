"""An agent on a machine, as the bridge tests need one: it spawns the install's bridge the way an agent would, and relays.

Started by the daemon as a task, with the bridge's whole argv as its own
arguments. Every line on its stdin goes to the bridge's stdin, every line the
bridge writes goes out on stdout (so the host reads it as the agent's frames),
the end of its stdin ends the bridge's, and when the bridge leaves it says so
on one last line: ``BRIDGE_EXIT <code> <stderr>``.
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
        for line in sys.stdin.buffer:
            try:
                bridge.stdin.write(line)
                bridge.stdin.flush()
            except OSError:
                break
        with contextlib.suppress(OSError):
            bridge.stdin.close()

    threading.Thread(target=inward, daemon=True).start()
    for line in bridge.stdout:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
    code = bridge.wait()
    said = bridge.stderr.read().decode("utf-8", "replace").strip().replace("\n", " ")
    sys.stdout.buffer.write(f"BRIDGE_EXIT {code} {said}\n".encode())
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
