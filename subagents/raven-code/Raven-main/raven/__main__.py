"""
Entry point for running Raven as a module: python -m raven
"""

import os


def _scrub_runtime_pythonpath() -> None:
    """Drop raven's own install dir from PYTHONPATH before anything else runs.

    When raven is launched from a bundled runtime via PYTHONPATH, child
    processes spawned by the exec tool inherit that PYTHONPATH; the bundle's
    packages (built for the bundled interpreter) then shadow the workspace
    project's own — typically much older — Python environment and break its
    tooling (import errors, syntax errors). sys.path of this process is
    already resolved at this point, so raven itself is unaffected.
    """
    pp = os.environ.get("PYTHONPATH")
    if not pp:
        return
    try:
        import raven

        own = os.path.realpath(os.path.dirname(os.path.dirname(raven.__file__)))
    except Exception:
        return
    keep = []
    for entry in pp.split(os.pathsep):
        try:
            if entry and os.path.realpath(entry) == own:
                continue
        except OSError:
            pass
        keep.append(entry)
    if keep:
        os.environ["PYTHONPATH"] = os.pathsep.join(keep)
    else:
        os.environ.pop("PYTHONPATH", None)


_scrub_runtime_pythonpath()

from raven.cli.commands import run  # noqa: E402  (must import after the scrub)

if __name__ == "__main__":
    run()
