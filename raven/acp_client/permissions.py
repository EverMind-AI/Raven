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

"Always approve" stops at the host's own refusals. Approving is the answer
for the ask tier, where nobody is there to be asked; a deny rule needs nobody,
and approving past one handed a sub-agent every command the operator had
refused raven itself. So a request naming a command is first read against the
two rulings the host's gate consults before any mode: the builtin deny list
with ``tools.exec.extraDenyPatterns``, and a ``deny`` rule in
``permissions.tools``. A match is answered with the agent's reject option.
This sees only what an agent asks about -- a call its own gate allows never
reaches here -- which is why raven's own products also carry the host's
refusals in their rendered config (``product_render.inherit_host_denials``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from loguru import logger

from raven.acp_client import elicitation
from raven.acp_client.client import UNHANDLED
from raven.security.redact import redact

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


def refusal_outcome(params: dict[str, Any]) -> dict[str, Any]:
    """The ``outcome`` that refuses one request: a reject option, else cancel.

    Cancelling is the last resort because codex cancels the whole turn on it;
    it is still the answer when nothing else was offered, since selecting an
    allow option would run the command being refused.
    """
    options = _options(params)
    for kind in ("reject_once", "reject_always"):
        for option in options:
            option_id = option.get("optionId")
            if option.get("kind") == kind and isinstance(option_id, str):
                return {"outcome": "selected", "optionId": option_id}
    return {"outcome": "cancelled"}


def requested_commands(params: dict[str, Any]) -> list[str]:
    """Every spelling of the shell command one permission request names.

    Read from the places the dialects read (``acp_dialects``): the spec's
    ``toolCall.rawInput.command``, as sent and with the quotes codex wraps it
    in taken off, and codex's parsed ``commandActions``. All of them, not the
    first: a compound command parses into one action per segment, and the
    segment a rule refuses need not be the first one.
    """
    found: list[str] = []
    tool = params.get("toolCall")
    raw = tool.get("rawInput") if isinstance(tool, dict) else None
    command = raw.get("command") if isinstance(raw, dict) else None
    if isinstance(command, str):
        found += [command.strip(), command.strip().strip('"').strip()]
    meta = params.get("_meta")
    codex = meta.get("codex") if isinstance(meta, dict) else None
    codex_params = codex.get("params") if isinstance(codex, dict) else None
    actions = codex_params.get("commandActions") if isinstance(codex_params, dict) else None
    for action in actions if isinstance(actions, list) else []:
        text = action.get("command") if isinstance(action, dict) else None
        if isinstance(text, str):
            found.append(text.strip())
    return list(dict.fromkeys(text for text in found if text))


@lru_cache(maxsize=1)
def _host_policy() -> tuple[Any, Any]:
    """The host's live config and builtin rulings, built once per process.

    Both re-read the config file on every call (by content), so an operator
    tightening a rule binds the next request without a restart.
    """
    from raven.config.live import LiveConfig, exec_extra_deny_patterns
    from raven.permissions import BuiltinRulings

    live = LiveConfig()
    return live, BuiltinRulings(extra_deny_source=lambda: exec_extra_deny_patterns(live))


def host_refusal(params: dict[str, Any]) -> str | None:
    """Why the host's own deny rules refuse this request's command, or None.

    A parse error is not a refusal here: the host's gate hands that back to
    its own model to fix, and an adapter's quoting is not the sub-agent's
    model's to fix.
    """
    commands = requested_commands(params)
    if not commands:
        return None
    from raven.config.live import permissions_config
    from raven.contracts.permissions import DecisionSource, Deny, Tier
    from raven.permissions.rules import user_tier

    live, builtin = _host_policy()
    rules = permissions_config(live).tools
    for command in commands:
        call = {"command": command}
        ruling = builtin.ruling("exec", call)
        if isinstance(ruling, Deny) and ruling.source is DecisionSource.BUILTIN_DENY:
            return f"{command}: {ruling.reason}"
        if user_tier("exec", call, rules) is Tier.DENY:
            return f"{command}: blocked by a deny rule in the host's permissions config"
    return None


def auto_approver(name: str, observe: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None) -> "Any":
    """An ``on_request`` handler that approves permissions and refuses the rest.

    A permission whose command the host's deny rules refuse is the exception
    (:func:`host_refusal`) and is answered with a reject option. What either
    log line names is redacted first: the command is the sub-agent's to write,
    and a denied ``curl -H "Authorization: ..."`` is exactly the one that lands
    in the retained log. A check that
    raises refuses too: the request still has to be answered, and approving a
    command nobody could check is the failure this exception exists to stop.

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
        try:
            refusal = host_refusal(params)
        except Exception:  # noqa: BLE001 - an unanswered request cancels the turn
            logger.exception("acp agent {!r}: checking the host's deny rules failed; refusing", name)
            refusal = "the host's deny rules could not be checked"
        tool = params.get("toolCall")
        title = tool.get("title") or tool.get("kind") if isinstance(tool, dict) else None
        if refusal is not None:
            outcome = refusal_outcome(params)
            logger.info("acp agent {!r}: refusing {} ({})", name, redact(refusal), outcome)
        else:
            outcome = permission_outcome(params)
            logger.debug("acp agent {!r}: approving {} ({})", name, redact(str(title or "a tool call")), outcome)
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


__all__ = [
    "PERMISSION_METHOD",
    "auto_approver",
    "host_refusal",
    "permission_outcome",
    "refusal_outcome",
    "request_dispatcher",
    "requested_commands",
]
