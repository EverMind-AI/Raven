"""How raven answers an ACP agent's ``session/request_permission``.

Raven dispatches these agents unattended: there is no operator watching a
direct chat or a DAG node, and no surface that could render an approval prompt.
So the only two answers available are "always approve" and "always refuse", and
raven approves -- the same trust boundary the cli transport already ran under
(``codex -a never``, ``claude --permission-mode auto``), stated here instead of
buried in a command template.

Not answering is not the third option, which is what made this module
necessary. Measured against ``@agentclientprotocol/codex-acp@1.1.14``: its
``CodexApprovalHandler`` turns *any* error from this request -- including the
``method not found`` raven used to send -- into ``{decision: "cancel"}``, which
cancels the whole turn. The agent had already streamed a sentence or two by
then, so the turn came back with ``stopReason: "cancelled"``, a partial reply,
and no other sign anything had gone wrong.

An option is chosen from the ones the agent offered, by ``kind``, never by
``optionId``: the ids are the agent's own vocabulary (codex mints
``allow_always``, another adapter may mint anything) while the four kinds are
the protocol's. An id raven invented would come back as "declined" from codex
and as anything at all from the rest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from raven.agent.acp_client import elicitation
from raven.agent.acp_client.client import UNHANDLED

PERMISSION_METHOD = "session/request_permission"

# Most permissive first. A grant that lasts the session is preferred over a
# per-call one so an agent doing ten things stops asking after the first,
# rather than paying a round trip per tool call for an answer that never
# varies. The reject kinds are last and are only ever reached when the agent
# offered nothing else: selecting one keeps the turn alive, which cancelling
# would not.
_KIND_ORDER = ("allow_always", "allow_once", "reject_once", "reject_always")


def _options(params: dict[str, Any]) -> list[dict[str, Any]]:
    raw = params.get("options")
    return [o for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []


def permission_outcome(params: dict[str, Any]) -> dict[str, Any]:
    """The ``outcome`` to answer one permission request with.

    ``{"outcome": "cancelled"}`` only when the agent offered no option at all,
    because there is then nothing selectable to answer with -- an ``optionId``
    raven made up is worse, being indistinguishable from a real choice.
    """
    options = _options(params)
    for kind in _KIND_ORDER:
        for option in options:
            option_id = option.get("optionId")
            if option.get("kind") == kind and isinstance(option_id, str):
                return {"outcome": "selected", "optionId": option_id}

    # Kind is optional in neither direction of the schema raven measured, but an
    # id alone is still enough to answer with, so take one rather than cancel.
    for option in options:
        option_id = option.get("optionId")
        if isinstance(option_id, str):
            return {"outcome": "selected", "optionId": option_id}

    return {"outcome": "cancelled"}


def auto_approver(name: str, observe: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None) -> "Any":
    """An ``on_request`` handler that approves permissions and refuses the rest.

    Refusing the rest is deliberate: ``fs/read_text_file`` and its siblings are
    advertised as unsupported in ``CLIENT_CAPABILITIES``, and answering one
    here would claim a capability raven does not serve.

    ``observe`` is handed each permission request after the outcome is decided.
    It exists because the request carries what the matching ``session/update``
    does not -- codex badges a shell command as a ``read`` and sends the command
    only here. It runs inside a ``try``: an unanswered request cancels the whole
    turn, so nothing an observer does may reach the answer.
    """

    async def handle(method: str, params: dict[str, Any]) -> Any:
        if method != PERMISSION_METHOD:
            return UNHANDLED
        outcome = permission_outcome(params)
        tool = params.get("toolCall")
        title = tool.get("title") or tool.get("kind") if isinstance(tool, dict) else None
        logger.debug("acp agent {!r}: approving {} ({})", name, title or "a tool call", outcome)
        if observe is not None:
            try:
                await observe(method, params)
            except Exception as exc:  # noqa: BLE001 - an observer must not reach the answer
                logger.debug("acp agent {!r}: permission observer failed: {}", name, exc)
        return {"outcome": outcome}

    return handle


def request_dispatcher(
    name: str,
    observe: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    *,
    elicitors: Any = None,
) -> "Any":
    """The connection's `on_request`: permissions, elicitations, nothing else.

    One handler rather than a chain, because the two answers have nothing in
    common and the third case -- everything raven does not serve -- has to stay
    an explicit `method not found`.
    """
    approve = auto_approver(name, observe)

    async def handle(method: str, params: dict[str, Any]) -> Any:
        if method != elicitation.METHOD:
            return await approve(method, params)
        if elicitors is None:
            return elicitation.decline()
        try:
            answer = await elicitors.answer(params)
        except Exception as exc:  # noqa: BLE001 - a declared capability must answer
            logger.warning("acp agent {!r}: elicitation failed, declining: {}", name, exc)
            return elicitation.decline()
        # No run owns this session, or the scope was `requestId` -- an auth-phase
        # elicitation with no session at all. Declining is the answer; `-32601`
        # would deny a capability raven advertised.
        return answer if answer is not None else elicitation.decline()

    return handle


__all__ = ["PERMISSION_METHOD", "auto_approver", "permission_outcome", "request_dispatcher"]
