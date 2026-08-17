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

from typing import Any

from loguru import logger

from raven.agent.acp.client import UNHANDLED

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


def auto_approver(name: str) -> "Any":
    """An ``on_request`` handler that approves permissions and refuses the rest.

    Refusing the rest is deliberate: ``fs/read_text_file`` and its siblings are
    advertised as unsupported in ``CLIENT_CAPABILITIES``, and answering one
    here would claim a capability raven does not serve.
    """

    async def handle(method: str, params: dict[str, Any]) -> Any:
        if method != PERMISSION_METHOD:
            return UNHANDLED
        outcome = permission_outcome(params)
        tool = params.get("toolCall")
        title = tool.get("title") or tool.get("kind") if isinstance(tool, dict) else None
        logger.debug("acp agent {!r}: approving {} ({})", name, title or "a tool call", outcome)
        return {"outcome": outcome}

    return handle


__all__ = ["PERMISSION_METHOD", "auto_approver", "permission_outcome"]
