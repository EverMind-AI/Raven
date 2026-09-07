"""Atomic identity generations kept separately from human-edited agent kinds."""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from raven.contracts.terminal import TerminalError, TerminalRecord
from raven.home import get_config_path
from raven.utils.atomic_io import atomic_replace, write_transaction


class IdentityModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class IdentityAlias(IdentityModel):
    alias: str = Field(min_length=1)
    source: str = Field(min_length=1)


class IdentityBinding(IdentityModel):
    handle: str | None = None
    incarnation_id: str | None = None
    worktree_id: str | None = None
    tab_id: str | None = None
    leaf_id: str | None = None


class IdentityRecord(IdentityModel):
    agent_name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    kind_ref: str
    brand: str
    role: str = ""
    task_ref: str = ""
    description: str = ""
    aliases: list[IdentityAlias] = Field(default_factory=list)
    binding: IdentityBinding | None = None
    binding_generation: int = Field(ge=1)
    first_observed_at: float
    last_observed_at: float
    orphan: bool = False
    schema_version: Literal[1] = 1
    exited_at: float | None = None


class IdentityState(BaseModel):
    schema_version: Literal[1] = 1
    records: list[IdentityRecord] = Field(default_factory=list)
    archive: list[IdentityRecord] = Field(default_factory=list)


def _config_rows() -> list:
    from raven.agent.subagent.builtin_agents import merge_builtin_seeds
    from raven.config.loader import load_config

    return merge_builtin_seeds(load_config().subagents.agents)


def _brand(row: dict) -> str:
    if row.get("kind") == "builtin" or str(row["name"]).startswith("raven-"):
        return "raven"
    preset = row.get("preset")
    if preset:
        return str(preset).removesuffix("-acp").removesuffix("-cli")
    argv = shlex.split(row.get("command", ""))
    return Path(argv[0]).name if argv else str(row.get("kind", "unknown"))


class IdentityRegistry:
    def __init__(
        self,
        path: Path | None = None,
        *,
        config_rows: Callable[[], list] = _config_rows,
        clock: Callable[[], float] = time.time,
    ):
        self.path = path or get_config_path().parent / "agent_registry.json"
        self.config_rows = config_rows
        self.clock = clock
        self.startup_error: TerminalError | None = None
        try:
            self.reconcile()
        except TerminalError as exc:
            self.startup_error = exc

    def _rows(self) -> dict[str, dict]:
        rows = [row.model_dump() if isinstance(row, BaseModel) else dict(row) for row in self.config_rows()]
        return {row["name"]: row for row in rows}

    def _load(self) -> IdentityState:
        try:
            return IdentityState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return IdentityState()
        except (OSError, ValidationError, ValueError) as exc:
            raise TerminalError("identity_state_invalid", "Cannot read agent identity state") from exc

    def _save(self, state: IdentityState) -> None:
        try:
            atomic_replace(self.path, state.model_dump_json(indent=2), mode=0o600)
        except OSError as exc:
            raise TerminalError("identity_write_failed", "Cannot write agent identity state") from exc

    def register(
        self,
        agent_name: str,
        *,
        kind_ref: str,
        binding: TerminalRecord | IdentityBinding | dict | None = None,
        role: str = "",
        task_ref: str = "",
        description: str | None = None,
        aliases: list[dict] | None = None,
    ) -> IdentityRecord:
        rows = self._rows()
        if kind_ref not in rows:
            raise TerminalError("invalid_kind_ref", f"Unknown kind_ref: {kind_ref}")
        if isinstance(binding, TerminalRecord):
            binding = IdentityBinding.model_validate(binding.model_dump())
        now = self.clock()
        with write_transaction(self.path):
            state = self._load()
            previous = [record for record in state.records + state.archive if record.agent_name == agent_name]
            last = max(previous, key=lambda record: record.binding_generation) if previous else None
            record = IdentityRecord(
                agent_name=agent_name,
                kind_ref=kind_ref,
                brand=_brand(rows[kind_ref]),
                role=role,
                task_ref=task_ref,
                description=description if description is not None else rows[kind_ref].get("description", ""),
                aliases=aliases if aliases is not None else (last.aliases if last else []),
                binding=binding,
                binding_generation=last.binding_generation + 1 if last else 1,
                first_observed_at=last.first_observed_at if last else now,
                last_observed_at=now,
            )
            state.records.append(record)
            self._save(state)
        return record

    def ensure_host(self, kind_ref: str | None = None) -> IdentityRecord:
        rows = self._rows()
        if kind_ref is None:
            kind_ref = next((name for name, row in rows.items() if row.get("kind") == "builtin"), None)
        if kind_ref is None or kind_ref not in rows or rows[kind_ref].get("kind") != "builtin":
            raise TerminalError("invalid_kind_ref", "The native host needs a builtin kind_ref")
        try:
            existing = self.show("raven")
        except TerminalError as exc:
            if exc.code != "agent_not_found":
                raise
        else:
            if existing.kind_ref == kind_ref and existing.binding is not None and existing.binding.handle is None:
                return existing
            raise TerminalError("host_identity_conflict", "The raven identity already has a different binding")
        return self.register("raven", kind_ref=kind_ref, binding=IdentityBinding(), role="host")

    def reconcile(self) -> None:
        if not self.path.exists():
            return
        rows = self._rows()
        with write_transaction(self.path):
            state = self._load()
            before = state.model_dump_json()
            retained = []
            for record in state.records:
                record.orphan = record.kind_ref not in rows
                if record.exited_at is not None and self.clock() - record.exited_at > 30 * 86400:
                    state.archive.append(record)
                else:
                    retained.append(record)
            state.records = retained
            if state.model_dump_json() != before:
                self._save(state)

    def list(self) -> list[IdentityRecord]:
        self.reconcile()
        latest: dict[str, IdentityRecord] = {}
        for record in self._load().records:
            if (
                record.agent_name not in latest
                or record.binding_generation > latest[record.agent_name].binding_generation
            ):
                latest[record.agent_name] = record
        return sorted(latest.values(), key=lambda record: record.agent_name)

    def show(self, agent_name: str) -> IdentityRecord:
        record = next((record for record in self.list() if record.agent_name == agent_name), None)
        if record is None:
            raise TerminalError("agent_not_found", f"Unknown agent: {agent_name}")
        return record

    def resolve(self, mention: str) -> dict:
        candidates = []
        for record in self.list():
            reason = (
                "exact"
                if mention == record.agent_name
                else ("alias" if any(alias.alias == mention for alias in record.aliases) else None)
            )
            if reason:
                candidates.append({"agent": record.model_dump(by_alias=True, mode="json"), "reason": reason})
        return {"candidates": candidates, "unique": len(candidates) == 1 and not candidates[0]["agent"]["orphan"]}

    def mark_exited(self, handle: str) -> None:
        with write_transaction(self.path):
            state = self._load()
            changed = False
            for record in state.records:
                if record.binding and record.binding.handle == handle and record.exited_at is None:
                    record.exited_at = self.clock()
                    changed = True
            if changed:
                self._save(state)
