"""Static candidate checks and evidence for optional isolated host probes."""

from ..harness import Candidate


def static_checks(candidate: Candidate) -> list[str]:
    errors = []
    for path, text in candidate.artifact.files.items():
        if path.endswith(".py"):
            try:
                compile(text, path, "exec")
            except (SyntaxError, ValueError) as exc:
                errors.append(f"{path}: {exc}")
    return errors


async def run_probe(bound, probe):
    """Report the existing host probe's execution separately from candidate assembly."""
    if probe is None:
        bound.recorder.add("validation.probe", status="not_supplied")
        return
    name = f"{probe.__module__}:{getattr(probe, '__qualname__', type(probe).__qualname__)}"
    bound.recorder.add("validation.probe", status="started", probe=name)
    try:
        await probe(bound)
    except Exception as exc:
        bound.recorder.add("validation.error", probe=name, error=f"{type(exc).__name__}: {exc}")
        raise
    bound.recorder.add("validation.probe", status="completed", probe=name)
