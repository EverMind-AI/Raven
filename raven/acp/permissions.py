"""Ask the client's user before running a protected command.

Raven already has an approval round trip, and this is not a second one: it is a
second *transport* for the same one. ``ExecTool`` calls
``ApprovalResponder.await_approval`` after ``ShellCommandPolicy`` classifies a
command as needing approval; the TUI's implementation emits ``approval.request``
on its own wire, and this one sends ``session/request_permission`` on the ACP
wire. Everything about authority stays where it was -- the model never decides,
the grant covers one exact command, and there is no persistent policy.

Two shapes of the protocol constrain what can be offered:

* **There is no "always".** ``ApprovalBroker.resolve`` takes ``{allow, deny}``
  and its docstring says outright that there is no always-allow state;
  ``denied_digests`` is cleared at every turn boundary. So only ``allow_once``
  and ``reject_once`` are offered. ``allow_always`` is in the schema and an
  editor's user will want it -- offering it with nothing behind it would be a
  lie the client then renders as a saved preference.
* **A refusal is a selection.** ``RequestPermissionOutcome`` has exactly two
  variants, ``cancelled`` and ``selected``; there is no ``denied``. Rejecting is
  ``selected`` carrying a reject option's id.

**Every client failure is handled here, and every one of them denies.** Measured
from the client direction on codex-acp: any error in reply to this request
cancels the whole turn -- so the mirror rule is that nothing from the client may
escape this function. Four ways it can go wrong, all of them seen in the wild:
no answer at all, a JSON-RPC error, an option id nobody minted, and an explicit
``cancelled``. Options are minted per request and the answer is checked against
that set, so a client replying with a stale or invented id is refused rather than
believed -- and so is a client that answers "the first option" without reading
the kinds.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from loguru import logger

from raven.acp import redact
from raven.acp.outbound import (
    DEFAULT_REQUEST_TIMEOUT_S,
    ConnectionClosedError,
    OutboundRequests,
    RequestFailedError,
)
from raven.acp.updates import UpdateTranslator

ALLOW_KIND = "allow_once"
REJECT_KIND = "reject_once"


class AcpPermissionBroker:
    """An :class:`~raven.agent.tools.shell.ApprovalResponder` over the ACP wire.

    Structurally duck-typed rather than declared: the protocol lives in
    ``raven.agent.tools.shell`` and importing it here would tie the ACP layer to
    the tool module for a single method signature.
    """

    def __init__(
        self,
        *,
        outbound: OutboundRequests,
        translator: UpdateTranslator,
        timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        self._outbound = outbound
        self._translator = translator
        # A parameter rather than the callee's default, and not because a test
        # wants it short. The RPC broker's ceiling is 35 seconds because a
        # terminal overlay owns a visible countdown; here a person is reading a
        # diff, and the deadline is a product decision that belongs to whoever
        # assembles the connection.
        self._timeout_s = timeout_s
        # Counted per outcome rather than logged per request: a turn that ran
        # twenty commands would otherwise write twenty lines saying the same
        # thing, and what a reader wants afterwards is the tally.
        self.outcomes: dict[str, int] = {}

    async def await_approval(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        tool_call_id: str,
        command: str,
        description: str,
    ) -> bool:
        """Ask, and return whether this exact command may run once.

        Fails closed on every path. The signature is the one ``ExecTool`` calls,
        including the keyword-only arguments, so this object can be handed
        wherever the TUI's broker goes.
        """
        session_id = self._session_for(conversation_id)
        if session_id is None:
            # No ACP session owns this turn -- a cron or runtime turn sharing the
            # process. There is nobody to ask, and "nobody to ask" is not
            # permission.
            return self._record("no-session", False)

        allow_id = f"allow-{uuid4().hex}"
        reject_id = f"reject-{uuid4().hex}"
        request = {
            "sessionId": session_id,
            # Required by the schema, and genuinely wanted: a prompt with no
            # subject is a dialog a person cannot answer. The id is the live tool
            # call's, so a client that already drew the row updates it in place
            # instead of opening an unrelated sheet.
            "toolCall": {
                "toolCallId": tool_call_id or f"exec-{uuid4().hex}",
                "title": redact.redact(f"{description}: {command}" if description else command),
                "kind": "execute",
                "status": "pending",
            },
            "options": [
                {"optionId": allow_id, "name": "Allow once", "kind": ALLOW_KIND},
                {"optionId": reject_id, "name": "Reject", "kind": REJECT_KIND},
            ],
            # Not a standard field, and the spec forbids custom keys on standard
            # types -- so the turn correlation rides here, where the schema
            # declares a home for it.
            "_meta": {"raven.turnId": turn_id} if turn_id else None,
        }
        if request["_meta"] is None:
            del request["_meta"]

        try:
            result = await self._outbound.call("session/request_permission", request, timeout=self._timeout_s)
        except RequestFailedError as exc:
            # The first measured failure mode. An error here is the client saying
            # it cannot ask, which is not the same as the user saying yes.
            logger.info("acp: permission request refused by the client: {}", exc)
            return self._record("client-error", False)
        except ConnectionClosedError:
            return self._record("connection-closed", False)
        except TimeoutError:
            # A client that never answers. Distinguished from a denial in the
            # tally because they mean different things to whoever reads it: one
            # is a decision, the other is a client that stopped talking.
            # (``asyncio.TimeoutError`` is this same class since 3.11, so one
            # clause covers both spellings.)
            logger.warning("acp: permission request went unanswered; treating it as a refusal")
            return self._record("timeout", False)
        except asyncio.CancelledError:
            # The turn is being torn down. Re-raised rather than converted to a
            # refusal: a cancelled turn has no decision to report, and swallowing
            # it here would report the tool call as denied in a turn that no
            # longer exists.
            self._record("cancelled-turn", False)
            raise
        except Exception:
            # The catch-all matters, and this is why: the write itself can fail.
            # ``OutboundRequests.call`` puts the frame on the wire before it
            # awaits, so a closed or full pipe raises an ``OSError`` that is none
            # of the three cases above -- and without this clause it would travel
            # up to ``ToolRegistry.execute``'s ``except Exception`` and be
            # reported to the model as a *failed tool call* rather than as a
            # refusal. Failing closed is the whole contract of this function.
            logger.exception("acp: asking for permission failed; treating it as a refusal")
            return self._record("transport-error", False)

        return self._read_outcome(result, allow_id=allow_id, reject_id=reject_id)

    def _read_outcome(self, result: Any, *, allow_id: str, reject_id: str) -> bool:
        """Read the answer, believing only what this request minted."""
        if not isinstance(result, dict):
            logger.warning("acp: permission answer was not an object; treating it as a refusal")
            return self._record("malformed", False)
        outcome = result.get("outcome")
        if not isinstance(outcome, dict):
            return self._record("malformed", False)
        kind = outcome.get("outcome")
        if kind == "cancelled":
            # The third measured failure mode, and the only one that is not a
            # failure: the spec requires a client to answer every pending
            # permission with this when it cancels a turn.
            return self._record("cancelled", False)
        if kind != "selected":
            return self._record("unknown-outcome", False)
        option_id = outcome.get("optionId")
        if option_id == allow_id:
            return self._record("allowed", True)
        if option_id == reject_id:
            return self._record("rejected", False)
        # The fourth. An id from an earlier request, or one the client invented
        # -- including the ``allow_always`` a client might synthesise because its
        # UI offers one. Believing it would grant authority nobody granted.
        logger.warning("acp: permission answer carried an option id this request did not mint")
        return self._record("unknown-option", False)

    def _session_for(self, conversation_id: str) -> str | None:
        """The ACP session a turn's lane belongs to.

        A sub-agent's direct chat runs on ``<session>#<agent>/<handle>``, so the
        lane is not the session. ``session_of`` splits on the first separator,
        which is what makes a handle containing anything at all safe here.
        """
        from raven.spine import session_of

        if not conversation_id:
            return None
        session = self._translator.get(session_of(conversation_id))
        return None if session is None else session.session_id

    def _record(self, outcome: str, allowed: bool) -> bool:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        return allowed


__all__ = ["ALLOW_KIND", "REJECT_KIND", "AcpPermissionBroker"]
