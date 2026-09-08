"""Build native provider resume arguments without shell interpolation."""

from pathlib import Path

from raven.contracts.terminal import TerminalError


def resume_command(command: list[str], session_id: str) -> list[str]:
    if not session_id.strip() or session_id.lstrip().startswith("-") or "\x00" in session_id:
        raise TerminalError(
            "invalid_params", "Resume requires a nonempty native provider session ID without a leading dash or NUL"
        )
    brand = Path(command[0]).name if command else ""
    if brand not in {"claude", "codex"}:
        raise TerminalError("invalid_params", "Resume supports only native claude or codex commands")
    selectors = {"--resume", "--continue", "--session-id", "--last", "-r"}
    if brand == "claude":
        selectors.add("-c")
    if any(arg.split("=", 1)[0] in selectors or arg == "resume" for arg in command[1:]):
        raise TerminalError("invalid_params", "Command already contains a resume or session selector")
    operation = "--resume" if brand == "claude" else "resume"
    return [command[0], operation, session_id, *command[1:]]
