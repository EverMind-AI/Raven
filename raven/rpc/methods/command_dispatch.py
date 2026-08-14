"""``command.dispatch`` RPC handler -- the slash fallback ui-tui reaches for last.

``createSlashHandler.ts`` tries ``slash.exec`` first and calls this only when
that one *rejects*. ``slash.exec`` never raises by design, so in practice this
runs when the RPC layer itself failed -- and until now it answered -32601,
which the client then printed in place of whatever actually went wrong.

Two resolutions, in order:

* the name matches a local skill -> ``{type: "skill"}`` with the skill body as
  the message, which the client sends as the next turn. This is the one case
  where the fallback does something ``slash.exec`` cannot: skills are not CLI
  verbs, so ``/<skill-name>`` has no other route.
* otherwise -> ``{type: "exec"}`` carrying whatever the CLI dispatcher says,
  including its "unknown command" text, so the user gets the real answer.

The contract's ``alias`` and ``plugin`` and ``send`` variants are never
returned: the command catalog is 1:1 alias-to-canonical in this version, and
Raven has no plugin commands. Returning a shape the client can render but the
runtime cannot mean would be worse than not returning it.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.rpc.errors import (
    CliCommandTimeoutError,
    ConfigValidationError,
    NotDispatchCompatibleError,
)
from raven.rpc.methods.cli_dispatch import cli_dispatch

if TYPE_CHECKING:
    from raven.rpc.confirm_broker import ConfirmBroker
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


_TIMEOUT_S = 30.0
_WIDTH = 100


def _skill_body(name: str, agent_loop_factory: "AgentLoopFactory | None") -> str | None:
    """The SKILL.md body for ``name``, or ``None`` if no such skill is on disk."""
    loop = agent_loop_factory() if agent_loop_factory is not None else None
    skills = getattr(getattr(loop, "context", None), "skills", None) if loop is not None else None
    registry = getattr(skills, "registry", None) if skills is not None else None
    if registry is None:
        return None
    try:
        meta = registry.get(name)
        if meta is None:
            lowered = name.lower()
            meta = next((m for m in registry.list_all() if m.name.lower() == lowered), None)
        return registry.get_body(meta.name) if meta is not None else None
    except Exception:
        logger.exception("command.dispatch: skill lookup failed for {}", name)
        return None


async def command_dispatch(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
    confirm_broker: "ConfirmBroker | None" = None,
) -> dict[str, Any]:
    """``command.dispatch`` -- resolve one slash name to a skill load or a CLI run."""
    name = str(params.get("name") or "").strip()
    arg = str(params.get("arg") or "").strip()
    if not name:
        return {"type": "exec", "output": "(empty command)"}

    if (body := _skill_body(name, agent_loop_factory)) is not None:
        # The arg is appended rather than dropped: `/review the auth module`
        # has to reach the model, and the skill body alone would lose it.
        message = f"{body}\n\n{arg}".rstrip() if arg else body
        return {"type": "skill", "name": name, "message": message}

    try:
        argv = shlex.split(f"{name} {arg}".strip())
    except ValueError as exc:
        return {"type": "exec", "output": f"could not parse command: {exc}"}

    try:
        result = await cli_dispatch(
            {"argv": argv, "width": _WIDTH, "timeout_s": _TIMEOUT_S},
            confirm_broker=confirm_broker,
        )
    except NotDispatchCompatibleError:
        return {"type": "exec", "output": f"unknown command: /{name}"}
    except CliCommandTimeoutError:
        return {"type": "exec", "output": f"/{name} exceeded {_TIMEOUT_S:.0f}s timeout"}
    except ConfigValidationError as exc:
        return {"type": "exec", "output": f"invalid command payload: {exc}"}

    stdout = str(result.get("stdout", ""))
    stderr = str(result.get("stderr", ""))
    output = stdout if int(result.get("exit_code", 0) or 0) == 0 else "\n".join(p for p in (stdout, stderr) if p)
    return {"type": "exec", "output": output}


def register_command_dispatch_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
    confirm_broker: "ConfirmBroker | None" = None,
) -> None:
    """Register ``command.dispatch`` on a dispatcher instance."""

    async def _dispatch(params: dict[str, Any]) -> dict[str, Any]:
        return await command_dispatch(
            params,
            agent_loop_factory=agent_loop_factory,
            confirm_broker=confirm_broker,
        )

    dispatcher.register("command.dispatch", _dispatch)


__all__ = ["command_dispatch", "register_command_dispatch_methods"]
