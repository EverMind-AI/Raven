"""Host-issued receiver credentials bound to current Registry and mailbox identity."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass

from raven.contracts.mailbox import MailboxEnvelope, MailboxError, MailboxInstanceRef
from raven.contracts.terminal import TerminalError
from raven.mailbox.codec import canonical_bytes, decode_envelope
from raven.mailbox.db import canonical_id

CAPABILITIES = frozenset({"poll", "terminal_notify", "native_notify", "artifact_read", "handoff"})


@dataclass(frozen=True)
class ReceiverBinding:
    binding_id: str
    ref: MailboxInstanceRef
    scope: dict[str, str]
    agent_name: str
    registry_generation: int
    terminal_handle: str | None
    terminal_incarnation: str | None
    session_key: str | None
    capabilities: frozenset[str]

    def public(self) -> dict:
        return {
            "binding_id": self.binding_id,
            "ref": self.ref.model_dump(),
            "scope": self.scope,
            "agent_name": self.agent_name,
            "registry_generation": self.registry_generation,
            "terminal_handle": self.terminal_handle,
            "terminal_incarnation": self.terminal_incarnation,
            "session_key": self.session_key,
            "capabilities": sorted(self.capabilities),
        }


class ReceiverService:
    def __init__(self, store, registry, host=None, *, upgrade=True):
        self.store = store
        self.registry = registry
        self.host = host
        if upgrade:
            self.store.db.upgrade_receivers()

    def _binding(self, row) -> ReceiverBinding:
        return ReceiverBinding(
            row["binding_id"],
            MailboxInstanceRef(
                authority_id=self.store.db.authority_id,
                tenant_id=self.store.db.tenant_id,
                agent_id=row["agent_id"],
                instance_id=row["instance_id"],
                generation=row["generation"],
            ),
            {"task_id": row["task_id"], "workspace_id": row["workspace_id"]},
            row["agent_name"],
            row["registry_generation"],
            row["terminal_handle"],
            row["terminal_incarnation"],
            row["session_key"],
            frozenset(json.loads(row["capability_json"])),
        )

    def _validate(self, binding, conn):
        card = self.store._current(conn, binding.ref)
        if binding.scope not in json.loads(card["allowed_scopes"]):
            raise MailboxError("scope_denied")
        try:
            record = self.registry.show(binding.agent_name)
            if (
                record.orphan
                or record.exited_at is not None
                or record.binding_generation != binding.registry_generation
            ):
                raise MailboxError("receiver_binding_stale")
            if record.task_ref != binding.scope["task_id"]:
                raise MailboxError("scope_denied")
            if binding.terminal_handle:
                if self.host is None or record.binding is None:
                    raise MailboxError("receiver_binding_stale")
                terminal = self.host.show(binding.terminal_handle)
                if (
                    record.binding.handle != binding.terminal_handle
                    or record.binding.incarnation_id != binding.terminal_incarnation
                    or terminal.incarnation_id != binding.terminal_incarnation
                    or terminal.worktree_id != binding.scope["workspace_id"]
                    or terminal.liveness != "live"
                ):
                    raise MailboxError("receiver_binding_stale")
            elif not binding.session_key or record.session_key != binding.session_key:
                raise MailboxError("receiver_binding_stale")
        except TerminalError:
            raise MailboxError("receiver_binding_stale") from None

    def grant(self, agent_name, ref, scope, *, request_id, capabilities=("poll", "artifact_read", "handoff")) -> dict:
        self.store.db.upgrade_receivers()
        request_id = canonical_id(request_id)
        if (
            not isinstance(scope, dict)
            or set(scope) != {"task_id", "workspace_id"}
            or any(
                not isinstance(value, str) or not value.isascii() or not 1 <= len(value) <= 128
                for value in scope.values()
            )
        ):
            raise MailboxError("scope_denied")
        capabilities = frozenset(capabilities)
        if not capabilities <= CAPABILITIES:
            raise MailboxError("receiver_capability_unavailable")
        try:
            record = self.registry.show(agent_name)
        except TerminalError:
            raise MailboxError("receiver_binding_stale") from None
        terminal = record.binding
        binding = ReceiverBinding(
            request_id,
            ref,
            dict(scope),
            agent_name,
            record.binding_generation,
            terminal.handle if terminal else None,
            terminal.incarnation_id if terminal else None,
            record.session_key,
            capabilities,
        )
        credential = secrets.token_urlsafe(32)
        with self.store.db.connection(write=True) as conn:
            self._validate(binding, conn)
            existing = conn.execute("SELECT * FROM receiver_bindings WHERE binding_id=?", (request_id,)).fetchone()
            if existing:
                if self._binding(existing) != binding:
                    raise MailboxError("request_conflict")
                raise MailboxError("credential_already_issued")
            conn.execute(
                "INSERT INTO receiver_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    ref.agent_id,
                    ref.instance_id,
                    ref.generation,
                    agent_name,
                    record.binding_generation,
                    scope["task_id"],
                    scope["workspace_id"],
                    binding.terminal_handle,
                    binding.terminal_incarnation,
                    binding.session_key,
                    canonical_bytes(sorted(capabilities)).decode(),
                    hashlib.sha256(credential.encode()).hexdigest(),
                    None,
                    int(time.time()),
                ),
            )
        return {"binding": binding.public(), "credential": credential}

    def current(self, binding_id) -> ReceiverBinding:
        binding_id = canonical_id(binding_id)
        with self.store.db.connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
                raise MailboxError("receiver_capability_unavailable")
            row = conn.execute("SELECT * FROM receiver_bindings WHERE binding_id=?", (binding_id,)).fetchone()
            if row is None:
                raise MailboxError("receiver_unauthorized")
            if row["revoked_at"] is not None:
                raise MailboxError("receiver_revoked")
            binding = self._binding(row)
            self._validate(binding, conn)
            return binding

    def authenticate(self, token) -> ReceiverBinding:
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            raise MailboxError("receiver_unauthorized")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.db.connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
                raise MailboxError("receiver_capability_unavailable")
            row = conn.execute("SELECT binding_id FROM receiver_bindings WHERE credential_hash=?", (digest,)).fetchone()
        if row is None:
            raise MailboxError("receiver_unauthorized")
        return self.current(row[0])

    def revoke(self, binding_id):
        with self.store.db.connection(write=True) as conn:
            changed = conn.execute(
                "UPDATE receiver_bindings SET revoked_at=coalesce(revoked_at,?) WHERE binding_id=?",
                (int(time.time()), canonical_id(binding_id)),
            ).rowcount
            if not changed:
                raise MailboxError("receiver_unauthorized")
        return {"revoked": True}

    def for_session(self, session_key) -> ReceiverBinding:
        with self.store.db.connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
                raise MailboxError("receiver_capability_unavailable")
            rows = conn.execute(
                "SELECT binding_id FROM receiver_bindings WHERE session_key=? "
                "AND terminal_handle IS NULL AND revoked_at IS NULL",
                (session_key,),
            ).fetchall()
        bindings = []
        for row in rows:
            try:
                bindings.append(self.current(row[0]))
            except MailboxError as exc:
                if exc.code not in {"receiver_binding_stale", "instance_fenced", "receiver_revoked"}:
                    raise
        if len(bindings) != 1:
            raise MailboxError("receiver_binding_ambiguous" if bindings else "receiver_unauthorized")
        return bindings[0]

    def message(self, binding, message_id) -> MailboxEnvelope:
        binding = self.current(binding.binding_id)
        with self.store.db.connection() as conn:
            row = self.store._message(conn, binding.ref.agent_id, canonical_id(message_id))
            if row is None:
                raise MailboxError("message_not_found")
            if "envelope_bytes" not in row.keys():
                raise MailboxError("terminal_compacted", message_id=message_id)
            envelope = decode_envelope(row["envelope_bytes"])
            if envelope.scope.model_dump() != binding.scope:
                raise MailboxError("scope_denied")
            if envelope.digest.value != row["digest"]:
                raise MailboxError("storage_conflict")
            return envelope
