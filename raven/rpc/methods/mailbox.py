"""Mailbox and strict DAG RPC using host-established receiver authority."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import fields, is_dataclass

from pydantic import BaseModel, ValidationError

from raven.contracts.mailbox import MailboxClaim, MailboxError
from raven.mailbox.codec import decode_envelope, encode_envelope
from raven.mailbox.delivery import MailboxDelivery
from raven.rpc.connection import current_state
from raven.rpc.errors import RpcError
from raven.rpc.mailbox_models import MAILBOX_METHOD_MODELS


class MailboxRpcError(RpcError):
    MESSAGE = "mailbox_error"


def _public_dag(record):
    if is_dataclass(record):
        record = {field.name: getattr(record, field.name) for field in fields(record)}
    elif isinstance(record, BaseModel):
        record = record.model_dump()
    if isinstance(record, dict):
        return {
            key: _public_dag(value)
            for key, value in record.items()
            if key not in {"staged_envelope_bytes", "submit_now"}
        }
    if isinstance(record, (tuple, list)):
        return [_public_dag(value) for value in record]
    return record


class MailboxMethods:
    def __init__(self, receivers=None, *, receiver_factory=None):
        self._receivers = receivers
        self.receiver_factory = receiver_factory
        self.notification_receiver = None
        self.notification_factory = None
        self._handoff = None
        self._dag_ledger = None
        self._dag_runtime = None
        self.dag_factory = None

    def dag_ledger(self):
        if self._dag_ledger is None:
            from raven.mailbox.dag import StrictDagLedger

            self._dag_ledger = StrictDagLedger(self.receivers.store, upgrade=False)
        return self._dag_ledger

    def dag_runtime(self):
        if self._dag_runtime is None and self.dag_factory is not None:
            self._dag_runtime = self.dag_factory()
        if self._dag_runtime is None:
            raise MailboxError("receiver_capability_unavailable")
        return self._dag_runtime

    def handoff(self):
        if self._handoff is None:
            from raven.mailbox.handoff import MailboxHandoff

            self._handoff = MailboxHandoff(self.receivers.store, upgrade=False)
        return self._handoff

    def notifier(self):
        if self.notification_receiver is None and self.notification_factory is not None:
            self.notification_receiver = self.notification_factory(self.receivers)
        if self.notification_receiver is None:
            raise MailboxError("receiver_capability_unavailable")
        return self.notification_receiver

    def resume_notifications(self):
        try:
            with self.receivers.store.db.connection() as conn:
                if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
                    return
                ids = [
                    row[0] for row in conn.execute("SELECT binding_id FROM receiver_bindings WHERE revoked_at IS NULL")
                ]
            for binding_id in ids:
                try:
                    binding = self.receivers.current(binding_id)
                except MailboxError:
                    continue
                if binding.capabilities & {"terminal_notify", "native_notify"}:
                    self.notifier().start()
                    return
        except MailboxError:
            return

    @property
    def receivers(self):
        if self._receivers is None:
            if self.receiver_factory is None:
                raise MailboxError("receiver_capability_unavailable")
            self._receivers = self.receiver_factory()
        return self._receivers

    @staticmethod
    def admin():
        state = current_state() or {}
        if state.get("mailbox_binding_id") or not state.get("mailbox_admin"):
            raise MailboxError("receiver_unauthorized")

    def binding(self, params):
        state = current_state() or {}
        binding_id = state.get("mailbox_binding_id")
        requested = getattr(params, "binding_id", None)
        if binding_id:
            if requested is not None and requested != binding_id:
                raise MailboxError("receiver_unauthorized")
        else:
            self.admin()
            binding_id = requested
        if binding_id is None:
            raise MailboxError("receiver_unauthorized")
        return self.receivers.current(binding_id)

    def invoke(self, name, params):
        if name == "enroll":
            self.admin()
            return self.receivers.grant(
                params.agent_name,
                params.ref,
                params.scope,
                request_id=params.request_id,
                capabilities=params.capabilities,
            )
        if name == "revoke":
            self.admin()
            return self.receivers.revoke(params.binding_id)
        if name == "bindings":
            self.admin()
            with self.receivers.store.db.connection() as conn:
                if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
                    raise MailboxError("receiver_capability_unavailable")
                rows = conn.execute("SELECT * FROM receiver_bindings WHERE revoked_at IS NULL").fetchall()
            return {"bindings": [self.receivers._binding(row).public() for row in rows]}
        if name == "overview":
            self.admin()
            store = self.receivers.store
            scope = {"task_id": params.task_id, "workspace_id": params.workspace_id}
            with store.db.connection() as conn:
                if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
                    raise MailboxError("receiver_capability_unavailable")
                rows = conn.execute(
                    "SELECT * FROM receiver_bindings WHERE task_id=? AND workspace_id=? AND revoked_at IS NULL",
                    (params.task_id, params.workspace_id),
                ).fetchall()
                bindings = [
                    self.receivers._binding(row)
                    for row in rows
                    if params.terminal_handle is None or row["terminal_handle"] == params.terminal_handle
                ]
                agents = {binding.ref.agent_id for binding in bindings}
                messages = []
                if agents:
                    placeholders = ",".join("?" for _ in agents)
                    for row in conn.execute(
                        "SELECT * FROM messages WHERE authority_id=? AND tenant_id=? AND "
                        f"(recipient IN ({placeholders}) OR sender_agent IN ({placeholders})) "
                        "AND json_extract(envelope_bytes,'$.scope.task_id')=? "
                        "AND json_extract(envelope_bytes,'$.scope.workspace_id')=? "
                        "ORDER BY created_at DESC,message_id LIMIT 100",
                        (
                            store.db.authority_id,
                            store.db.tenant_id,
                            *agents,
                            *agents,
                            params.task_id,
                            params.workspace_id,
                        ),
                    ):
                        if store._scope_allowed(row, [scope]):
                            try:
                                envelope = MailboxDelivery._envelope(row)
                                messages.append(
                                    {
                                        **store._status(row),
                                        "envelope": envelope.model_dump(),
                                        "attempt": row["attempt"],
                                        "direction": "incoming" if row["recipient"] in agents else "outgoing",
                                    }
                                )
                            except MailboxError:
                                messages.append({**store._status(row), "error": "storage_conflict"})
                notifications = []
                for binding in bindings:
                    notifications.extend(
                        dict(row)
                        for row in conn.execute(
                            "SELECT * FROM notifications WHERE binding_id=? ORDER BY created_at DESC LIMIT 100",
                            (binding.binding_id,),
                        )
                    )
                authority = conn.execute(
                    "SELECT * FROM task_authority WHERE task_id=? AND workspace_id=?",
                    (params.task_id, params.workspace_id),
                ).fetchone()
            for message in messages:
                envelope = message.get("envelope") or {}
                if envelope.get("kind") != "handoff.accept":
                    continue
                message["handoff"] = {"status": "accept_received"}
                if authority and authority["accept_message_id"] == message["message_id"]:
                    message["handoff"] = {"status": "CONFIRMED", "handoff_id": authority["confirmed_handoff_id"]}
                    continue
                for candidate in bindings:
                    if candidate.ref.agent_id != envelope["sender_identity"]["agent_id"]:
                        continue
                    try:
                        live = self.receivers.current(candidate.binding_id)
                        message["handoff"] = self.handoff().propose(
                            live.ref,
                            message["message_id"],
                            offer_message_id=envelope["payload"]["data"]["offer_message_id"],
                            scope=scope,
                        )
                        break
                    except MailboxError as exc:
                        message["handoff"]["reason"] = exc.code
            return {
                "bindings": [binding.public() for binding in bindings],
                "messages": messages[:100],
                "notifications": notifications[:100],
                "authority": dict(authority) if authority else None,
            }
        if name == "handoff.create":
            self.admin()
            self.receivers.store.db.upgrade_receivers()
            return self.handoff().create_task(**params.model_dump())
        if name == "handoff.commit":
            self.admin()
            binding = self.receivers.current(params.binding_id)
            inputs = params.model_dump(exclude={"binding_id"})
            return self.handoff().commit(
                **inputs,
                binding_id=binding.binding_id,
                receiver_ref=binding.ref,
                scope=binding.scope,
                revalidate_receiver=lambda: self.receivers.current(binding.binding_id),
            )
        binding = self.binding(params)
        if name == "dag.status":
            return _public_dag(self.dag_ledger().status(params.root_id, scope=binding.scope))
        store = self.receivers.store
        delivery = MailboxDelivery(store)
        if name in {"poll", "ack", "renew"} and "poll" not in binding.capabilities:
            raise MailboxError("receiver_capability_unavailable")
        if name == "send":
            raw = encode_envelope(params.envelope)
            envelope = decode_envelope(raw)
            if envelope.scope.model_dump() != binding.scope:
                raise MailboxError("scope_denied")
            return store.send(raw, binding.ref)
        if name == "poll":
            if params.peek:
                if params.request_id is not None:
                    raise MailboxError("invalid_argument")
                return {"messages": store.peek(binding.ref, limit=params.limit, allowed_scopes=[binding.scope])}
            if params.request_id is None:
                raise MailboxError("invalid_argument")
            claims = delivery.poll(
                binding.ref,
                request_id=params.request_id,
                limit=params.limit,
                lease_seconds=params.lease_seconds,
                allowed_scopes=[binding.scope],
            )
            return {"claims": [claim.model_dump() for claim in claims]}
        if name in {"ack", "renew"}:
            claim = MailboxClaim.model_validate(params.claim)
            original = self.receivers.message(binding, claim.envelope.message_id)
            if original != claim.envelope or claim.instance_ref != binding.ref:
                raise MailboxError("stale_claim")
            if name == "renew":
                return delivery.renew(
                    binding.ref, claim, request_id=params.request_id, lease_seconds=params.lease_seconds
                ).model_dump()
            if params.action == "finish":
                if params.result is None or params.reason is not None:
                    raise MailboxError("invalid_argument")
                result = delivery.finish(binding.ref, claim, params.result)
            else:
                if params.reason is None or params.result is not None:
                    raise MailboxError("invalid_argument")
                method = delivery.retry if params.action == "retry" else delivery.reject
                result = method(binding.ref, claim, reason=params.reason)
            delivery.flush_receipts(binding.ref)
            return result
        if name == "status":
            self.receivers.message(binding, params.message_id)
            return store.status(binding.ref.agent_id, params.message_id)
        if name == "messages":
            with store.db.connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE authority_id=? AND tenant_id=? AND recipient=? "
                    "ORDER BY created_at DESC,message_id",
                    store._key(binding.ref.agent_id),
                ).fetchall()
                records = []
                for row in rows:
                    if not store._scope_allowed(row, [binding.scope]):
                        continue
                    envelope = MailboxDelivery._envelope(row)
                    records.append({**store._status(row), "envelope": envelope.model_dump(), "attempt": row["attempt"]})
                    if len(records) >= params.limit:
                        break
            return {"messages": records}
        if name in {"artifact.read", "handoff.propose", "handoff.status"}:
            handoff = self.handoff()
            if name == "artifact.read":
                if "artifact_read" not in binding.capabilities:
                    raise MailboxError("receiver_capability_unavailable")
                self.receivers.message(binding, params.offer_message_id)
                content = handoff.read_artifact(
                    binding.ref, params.offer_message_id, params.artifact_hash, scope=binding.scope
                )
                return {
                    "artifact_hash": params.artifact_hash,
                    "bytes_read": len(content),
                    "content_base64": base64.b64encode(content).decode(),
                }
            if name == "handoff.propose":
                if "handoff" not in binding.capabilities:
                    raise MailboxError("receiver_capability_unavailable")
                return handoff.propose(
                    binding.ref, params.accept_message_id, offer_message_id=params.offer_message_id, scope=binding.scope
                )
            return handoff.task_status(**binding.scope)
        if name == "notify.status":
            result = self.notifier().notifications.status(params.request_id)
            if result["binding_id"] != binding.binding_id:
                raise MailboxError("scope_denied")
            return result
        raise MailboxError("receiver_capability_unavailable")

    async def dispatch(self, name, params):
        try:
            model = MAILBOX_METHOD_MODELS[f"mailbox.{name}"][0].model_validate(params)
            if name == "notify":
                self.admin()
                result = await self.notifier().notify(
                    model.binding_id, request_id=model.request_id, message_ids=model.message_ids
                )
            elif name in {"dag.create", "dag.start", "dag.recover", "dag.resolve"}:
                self.admin()
                binding = self.binding(model)
                if name == "dag.create" and binding.session_key != model.session_key:
                    raise MailboxError("scope_denied")
                mutation = getattr(self.dag_runtime(), name.removeprefix("dag."))
                result = _public_dag(await mutation(binding, **model.model_dump(exclude={"binding_id"})))
            else:
                result = await asyncio.to_thread(self.invoke, name, model)
                if name == "enroll" and {"native_notify", "terminal_notify"} & set(model.capabilities):
                    self.notifier().start()
            return {"data": result}
        except MailboxError as exc:
            raise MailboxRpcError(
                data={"code": exc.code, "retryable": exc.retryable, "message_id": exc.message_id}
            ) from None
        except ValidationError:
            error = MailboxRpcError(data={"code": "invalid_argument", "retryable": False, "message_id": None})
            error.CODE = -32602
            raise error from None


def register_mailbox_methods(dispatcher, *, receivers=None, receiver_factory=None):
    methods = MailboxMethods(receivers, receiver_factory=receiver_factory)
    for method in MAILBOX_METHOD_MODELS:

        async def handler(params, name=method.removeprefix("mailbox.")):
            return await methods.dispatch(name, params)

        dispatcher.register(method, handler)
    dispatcher.mailbox_receivers = methods
    return methods
