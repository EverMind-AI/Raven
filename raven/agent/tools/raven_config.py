"""``raven_config`` -- the agent reading and changing its own configuration.

One tool with a small schema over a catalog it discloses on demand
(:mod:`raven.config.self_surface`): ``describe`` walks the catalog a section at a
time, ``get`` reads values, ``set`` / ``unset`` / ``add`` change them, and
``restart`` applies the changes that only a gateway reload or a process
restart can. The schema names no setting, so the catalog costs nothing until
the model asks for the part it needs.

Every change is confirmed by the user. That is not this tool's doing: the
permission gate rules every mutating call of this tool as needing approval,
ahead of the user's allow rules and of ``full`` mode
(:func:`raven.permissions.rules.self_config_tier`), so the tool never runs a
write nobody saw. Reads are allowed without a prompt.

A change goes through the writer that owns it. ``raw`` settings are written by
the catalog's own validated writer; the rest go through the RPC methods the
settings page uses, reached through a caller the entrance lends
(:meth:`RavenConfigTool.set_rpc_caller`). Where no entrance lent one -- a
one-shot ``raven agent`` -- those settings are read-only and the tool says so.
Restarting is the gateway's to perform (:meth:`RavenConfigTool.set_restarter`);
it waits for the turn that asked to finish.

Secrets are never carried through a tool call: the tool reports whether one is
set and where the user enters it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from pydantic.alias_generators import to_snake

from raven.config import self_surface as surface
from raven.config.self_surface import EFFECT_TEXT, PENDING_EFFECTS, Effect, Setting
from raven.contracts.tool import Tool

RpcCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
Restarter = Callable[[str], Awaitable[str]]
#: A conversation's model and whether it chose it (False: it follows the default).
SessionModel = Callable[[str], tuple[str, bool]]

GUIDE_SKILL_ID = "local/raven-self-config"

_ACTIONS = ("describe", "get", "set", "unset", "add", "restart")
READ_ACTIONS = frozenset({"describe", "get"})
_RESTART_TARGETS = ("reload", "restart")


def _parse_value(raw: Any) -> Any:
    """The value the model meant: JSON when it parses, the bare string otherwise."""
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except ValueError:
        return raw


def _conversation() -> str:
    """The conversation this call runs in, as the permission turn names it; empty outside one."""
    from raven.permissions.turn import current_turn

    return current_turn().conversation_id


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


class RavenConfigTool(Tool):
    """Read and change Raven's own configuration through the catalog."""

    timeout_seconds = 120.0
    approval_kind = "config.change"

    def __init__(
        self, *, guide_skill_id: str | None = GUIDE_SKILL_ID, session_model: SessionModel | None = None
    ) -> None:
        self._guide = guide_skill_id
        self._session_model = session_model
        self._call: RpcCaller | None = None
        self._restart: Restarter | None = None
        self._pending: dict[str, Effect] = {}

    def set_rpc_caller(self, call: RpcCaller | None) -> None:
        """Lend the entrance's settings methods (``settings.set`` and kin)."""
        self._call = call

    def set_restarter(self, restart: Restarter | None) -> None:
        """Lend the gateway's reload and restart; absent everywhere else."""
        self._restart = restart

    def approval_evidence(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("action") == "restart" and not params.get("value"):
            params = {**params, "value": self._needed_restart()}
        view = surface.change_view(params, surface.read_raw())
        for row in view.get("changes") or [view]:
            if row.get("setting") == "session.model" and (now := self._conversation_model()) is not None:
                row["was"] = now
        return view

    @property
    def name(self) -> str:
        return "raven_config"

    @property
    def description(self) -> str:
        guide = f" Read skill {self._guide} before changing anything." if self._guide else ""
        return (
            f"Read and change Raven's own settings; check them when a tool, sub-agent or channel fails "
            f"or is missing. The user confirms each change.{guide} describe lists sections and settings."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_ACTIONS)},
                "path": {
                    "type": "string",
                    "description": "Section or setting path from describe",
                },
                "value": {
                    "type": "string",
                    "description": "JSON value",
                },
            },
            "required": ["action"],
        }

    def display_call(self, args: dict[str, Any]) -> str | None:
        action = str(args.get("action") or "")
        path = str(args.get("path") or "")
        return f"config {action} {path}".strip()

    async def execute(self, **kwargs: Any) -> str:
        action = str(kwargs.get("action") or "")
        path = str(kwargs.get("path") or "").strip().strip(".")
        value = _parse_value(kwargs.get("value"))
        try:
            if action == "describe":
                return await self._describe(path)
            if action == "get":
                return await self._get(path)
            if action == "set":
                if not path and isinstance(value, dict):
                    return await self._set_many(value)
                return await self._set(path, value)
            if action == "unset":
                return await self._unset(path)
            if action == "add":
                return await self._add(path, value)
            if action == "restart":
                return await self._do_restart(value)
        except (ValueError, KeyError, LookupError) as exc:
            return f"Error: {exc}"
        return f"Error: unknown action {action!r}; use one of {list(_ACTIONS)}"

    # -- describe / get ------------------------------------------------------

    async def _describe(self, path: str) -> str:
        if not path:
            out: dict[str, Any] = {
                "sections": [
                    {"name": s.name, "summary": s.summary, "settings": len(s.settings)} for s in surface.sections()
                ]
            }
            if self._pending:
                out["pending"] = self._pending_view()
            if self._call is None:
                out["note"] = "this process lent no settings writer: only raw settings can be changed here"
            return _dump(out)
        if section := surface.section_of(path):
            return _dump(
                {
                    "section": section.name,
                    "summary": section.summary,
                    "settings": [s.describe() for s in section.settings],
                }
            )
        if path.startswith("channels.") and path.count(".") == 1:
            return _dump(self._describe_channel(path.split(".", 1)[1]))
        if path.startswith("subagents.") and path.count(".") == 1:
            return _dump(await self._describe_subagent(path.split(".", 1)[1]))
        found = surface.find(path)
        if found is None:
            below = [s.describe() for s in surface.all_settings() if s.path.startswith(path + ".")]
            if below:
                return _dump({"prefix": path, "settings": below})
            raise LookupError(f"{path} is not in the catalog; describe with no path lists the sections")
        return _dump(found[0].describe() | {"path": path})

    def _describe_channel(self, name: str) -> dict[str, Any]:
        from raven.config.update_channels import channel_field_specs, channel_names

        if name not in channel_names():
            raise LookupError(f"unknown channel {name!r}; known: {channel_names()}")
        fields = []
        for key, spec in channel_field_specs(name).items():
            if key == "workspace":
                continue
            entry: dict[str, Any] = {
                "path": f"channels.{name}.{key}",
                "type": str(spec.get("type")),
                "takes_effect": EFFECT_TEXT[Effect.IMMEDIATE] + " (the gateway restarts this channel)",
            }
            if spec.get("description"):
                entry["summary"] = spec["description"]
            if spec.get("is_secret"):
                entry["secret"] = True
                entry["note"] = "set it in Settings > Channels"
            if key == "allow_from":
                entry["sensitive"] = "widening it lets more people instruct Raven"
            fields.append(entry)
        return {"channel": name, "settings": fields}

    async def _describe_subagent(self, name: str) -> dict[str, Any]:
        row = await self._subagent_row(name)
        out: dict[str, Any] = {
            "subagent": row.get("name"),
            "kind": row.get("kind"),
            "enabled": row.get("enabled"),
            "description": row.get("description"),
            "model": row.get("model"),
        }
        source = row.get("model_source")
        if source == "fixed":
            out["model_note"] = "this agent's model is fixed (it carries its own key, or its kind has no model switch)"
        elif source == "agent":
            out["model_choices"] = row.get("model_choices") or []
        elif source == "raven":
            out["model_note"] = 'pick from Raven\'s own providers: {"provider": ..., "model": ...}'
        if row.get("builtin"):
            out["description_note"] = "the built-in row's description is fixed"
        out["settings"] = [
            s.describe() | {"path": s.path.replace("*", name)}
            for s in surface.section_of("subagents").settings  # type: ignore[union-attr]
        ]
        return out

    async def _get(self, path: str) -> str:
        if not path:
            raise ValueError("get needs a path; describe lists them")
        raw = await asyncio.to_thread(surface.read_raw)
        if section := surface.section_of(path):
            if section.name == "subagents":
                rows = await self._subagent_rows()
                return _dump(
                    [
                        {k: r.get(k) for k in ("name", "kind", "enabled", "description", "model", "model_source")}
                        for r in rows
                    ]
                )
            return _dump(
                {p: self._value_view(raw, s, p) for s in section.settings for p in surface.concrete_paths(s, raw)}
            )
        if path.startswith("subagents."):
            parts = path.split(".")
            row = await self._subagent_row(parts[1])
            if len(parts) == 2:
                return _dump({k: row.get(k) for k in ("name", "kind", "enabled", "description", "model")})
            return _dump({path: row.get(parts[2])})
        if path.startswith("channels.") and path.count(".") == 1:
            name = path.split(".", 1)[1]
            view = self._describe_channel(name)
            return _dump({f["path"]: self._channel_value(raw, f) for f in view["settings"]})
        found = surface.find(path)
        if found is None:
            if path.startswith("channels."):
                return _dump({path: self._channel_value(raw, {"path": path})})
            below = {
                p: self._value_view(raw, s, p)
                for s in surface.all_settings()
                for p in surface.concrete_paths(s, raw)
                if p.startswith(path + ".")
            }
            if below:
                return _dump(below)
            raise LookupError(f"{path} is not in the catalog; describe with no path lists the sections")
        return _dump({path: self._value_view(raw, found[0], path)})

    def _value_view(self, raw: dict[str, Any], setting: Setting, path: str) -> Any:
        if setting.session:
            now = self._conversation_model()
            return now if now is not None else "unknown outside a conversation"
        present, value = surface.lookup(raw, path)
        if setting.secret:
            return "set" if present and value else "not set"
        if not present:
            value = {"default": surface.default_of(path)}
            if setting.keys and isinstance(value["default"], dict):
                value = {"default": {k: value["default"].get(k) for k in setting.keys}}
            return surface.redacted(value)
        if setting.keys and isinstance(value, dict):
            value = {k: surface.lookup(value, k)[1] for k in setting.keys}
        return surface.redacted(value)

    @staticmethod
    def _channel_value(raw: dict[str, Any], field: dict[str, Any]) -> Any:
        present, value = surface.lookup(raw, field["path"])
        if field.get("secret"):
            return "set" if present and value else "not set"
        return value if present else None

    # -- set / unset / add ---------------------------------------------------

    async def _set(self, path: str, value: Any) -> str:
        if not path:
            raise ValueError("set needs a path")
        if path.startswith("channels.") and path.count(".") == 2:
            return await self._set_channel(path, value)
        if path.startswith("subagents.") and path.count(".") == 2:
            return await self._set_subagent(path, value)
        found = surface.find(path)
        if found is None:
            raise LookupError(f"{path} is not in the catalog; describe with no path lists the sections")
        setting, bound = found
        if setting.secret:
            return await self._secret_outcome(path, setting, value)
        if setting.effect is Effect.INERT:
            return f"{path} is not read by anything ({setting.note or EFFECT_TEXT[Effect.INERT]}); nothing was changed."
        value = surface.check_value(setting, value)
        if bound and setting.writer == "raw":
            instance = surface.instance_of(setting, path)
            present, _ = surface.lookup(await asyncio.to_thread(surface.read_raw), instance)
            if not present:
                raise LookupError(f"{instance} is not configured; describe {setting.path.split('.*')[0]} first")
        previous = await self._write(setting, path, bound, value)
        return self._report(path, setting.effect, previous, value)

    async def _set_many(self, changes: dict[str, Any]) -> str:
        """Several settings under the one confirmation the user already gave.

        Every value is checked before anything is written, so a typo in the last
        one does not leave the first ones applied. Secrets are reported the way
        a single set reports them: entered on the card, or still to enter.
        """
        plan: list[tuple[str, Setting, list[str], Any]] = []
        for path, raw in changes.items():
            found = surface.find(path)
            if found is None:
                raise LookupError(
                    f"{path} is not in the catalog (channels and sub-agents are changed one at a time); nothing "
                    "was changed"
                )
            setting, bound = found
            if setting.effect is Effect.INERT:
                raise ValueError(f"{path} is not read by anything; nothing was changed")
            if not setting.secret and setting.writer != "raw" and self._call is None:
                raise ValueError(
                    f"{path} is changed through Raven's settings service, which this process does not serve; "
                    "nothing was changed"
                )
            value = raw if setting.secret else surface.check_value(setting, _parse_value(raw))
            plan.append((path, setting, bound, value))
        lines = []
        for path, setting, bound, value in plan:
            if setting.secret:
                lines.append(await self._secret_outcome(path, setting, value))
                continue
            previous = await self._write(setting, path, bound, value)
            lines.append(self._report(path, setting.effect, previous, value))
        return "\n".join(lines)

    async def _secret_outcome(self, path: str, setting: Setting, value: Any) -> str:
        """What became of a secret the confirmation card asked the user to type.

        The value never reaches this tool: the card saves it through the page's
        settings methods before it answers the approval. So the only question
        left is whether it is set now.
        """
        if value not in (None, ""):
            return f"{path} was not written: a key never goes through a tool call. Ask the user to rotate it."
        present, now = surface.lookup(await asyncio.to_thread(surface.read_raw), path)
        if present and now:
            return f"{path} is set (the user entered it; the value is not shown). It {EFFECT_TEXT[setting.effect]}."
        where = setting.note or "set it in Settings"
        if surface.secret_input(path) is None:
            return f"{path} is still not set: the confirmation card has no field for it. Ask the user to {where}."
        return (
            f"{path} is still not set: the user left the field empty, or answered where there is no field "
            f"(the terminal, a chat channel). Ask them to {where}."
        )

    def _conversation_model(self) -> str | None:
        conversation = _conversation()
        if self._session_model is None or not conversation:
            return None
        model, own = self._session_model(conversation)
        return model if own else f"{model} (the default)"

    async def _write(self, setting: Setting, path: str, bound: list[str], value: Any) -> Any:
        writer = setting.writer
        if writer == "raw":
            return await asyncio.to_thread(surface.write_value, path, value)
        if writer == "settings":
            result = await self._rpc("settings.set", {"key": path, "value": value})
            return result.get("previous") if isinstance(result, dict) else None
        if writer == "config.model":
            model, provider = self._model_ref(value)
            if not provider:
                raise ValueError('the default model needs its provider: {"provider": ..., "model": ...}')
            params: dict[str, Any] = {"key": "model", "value": model, "provider": provider}
            if setting.session:
                conversation = _conversation()
                if not conversation:
                    raise ValueError("session.model needs a conversation; this call is not part of one")
                params |= {"scope": "session", "session_id": conversation}
            result = await self._rpc("config.set", params)
            return result.get("previous") if isinstance(result, dict) else None
        if writer == "model.fields":
            result = await self._rpc("model.set_fields", {"slug": bound[0], "fields": {"api_base": value}})
            previous = result.get("previous") if isinstance(result, dict) else None
            return previous.get("api_base") if isinstance(previous, dict) else previous
        raise ValueError(f"{path} has no writer in this process")

    async def _unset(self, path: str) -> str:
        found = surface.find(path)
        if found is None:
            raise LookupError(f"{path} is not in the catalog")
        setting, _ = found
        if setting.writer != "raw" or setting.secret:
            return f"{path} cannot be reset from here; set it to the value you want instead."
        previous = await asyncio.to_thread(surface.remove_value, path)
        return self._report(path, setting.effect, previous, {"default": surface.default_of(path)})

    async def _set_channel(self, path: str, value: Any) -> str:
        from raven.config.update_channels import channel_field_specs, channel_names

        _, name, field_name = path.split(".")
        if name not in channel_names():
            raise LookupError(f"unknown channel {name!r}; known: {channel_names()}")
        key = to_snake(field_name)
        specs = channel_field_specs(name)
        if key not in specs or key == "workspace":
            raise LookupError(f"channel {name} has no setting {field_name!r}; describe channels.{name} lists them")
        if specs[key].get("is_secret"):
            return f"{path} is a secret; ask the user to enter it in Settings > Channels."
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError(f"{path} takes true or false")
            result = await self._rpc("channels.configure", {"name": name, "enabled": value})
        else:
            raw = await asyncio.to_thread(surface.read_raw)
            _, running = surface.lookup(raw, f"channels.{name}.enabled")
            params: dict[str, Any] = {"name": name, "fields": {key: value}}
            if running is True:
                # Sent with the switch on so the gateway rebuilds the adapter:
                # a running channel holds the slice it was built with.
                params["enabled"] = True
            result = await self._rpc("channels.configure", params)
        outcome = result.get("outcome") if isinstance(result, dict) else None
        tail = f" Gateway said: {outcome}." if outcome else ""
        if isinstance(result, dict) and result.get("detail"):
            tail += f" {result['detail']}"
        return f"Set {path} to {json.dumps(value, ensure_ascii=False)}.{tail}"

    async def _set_subagent(self, path: str, value: Any) -> str:
        _, name, field_name = path.split(".")
        if field_name == "enabled":
            if not isinstance(value, bool):
                raise ValueError(f"{path} takes true or false")
            await self._rpc("subagents.toggle", {"name": name, "enabled": value})
        elif field_name == "description":
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{path} takes a non-empty string")
            await self._rpc("subagents.update", {"name": name, "description": value})
        elif field_name == "model":
            if value is None:
                await self._rpc("subagents.update", {"name": name, "clear_model": True})
            else:
                model, provider = self._model_ref(value)
                params: dict[str, Any] = {"name": name, "model": model}
                if provider:
                    params["provider"] = provider
                await self._rpc("subagents.update", params)
        else:
            raise LookupError(f"sub-agents expose description, enabled and model; not {field_name!r}")
        return f"Set {path} to {json.dumps(value, ensure_ascii=False)} ({EFFECT_TEXT[Effect.IMMEDIATE]})."

    async def _add(self, path: str, value: Any) -> str:
        if path != "subagents":
            raise ValueError("add only connects a sub-agent preset: path 'subagents', value {\"preset\": ...}")
        if not isinstance(value, dict) or not isinstance(value.get("preset"), str):
            raise ValueError('add takes {"preset": "<preset>", "name"?: ..., "description"?: ...}')
        params = {k: value[k] for k in ("preset", "name", "description") if isinstance(value.get(k), str)}
        result = await self._rpc("subagents.add", params)
        name = result.get("name") if isinstance(result, dict) else params["preset"]
        return f"Connected sub-agent {name} ({EFFECT_TEXT[Effect.IMMEDIATE]})."

    # -- restart -------------------------------------------------------------

    async def _do_restart(self, value: Any) -> str:
        if not (isinstance(value, str) and value) and not self._pending:
            return (
                "Nothing changed in this process is waiting for a restart. Pass value 'reload' or 'restart' "
                "only if the user asked for one anyway."
            )
        target = value if isinstance(value, str) and value else self._needed_restart()
        if target not in _RESTART_TARGETS:
            raise ValueError(f"restart takes 'reload' or 'restart', not {target!r}")
        if self._restart is None:
            return (
                "This process cannot restart itself (only the gateway can). Tell the user to restart Raven "
                "so the pending changes take effect."
            )
        answer = await self._restart(target)
        if target == "restart" or (target == "reload" and Effect.RESTART not in self._pending.values()):
            self._pending.clear()
        else:
            self._pending = {p: e for p, e in self._pending.items() if e is Effect.RESTART}
        return answer

    def _needed_restart(self) -> str:
        return "restart" if Effect.RESTART in self._pending.values() else "reload"

    # -- helpers ---------------------------------------------------------------

    def _report(self, path: str, effect: Effect, previous: Any, value: Any) -> str:
        line = (
            f"Set {path}: {json.dumps(previous, ensure_ascii=False, default=str)} -> "
            f"{json.dumps(value, ensure_ascii=False, default=str)}. It {EFFECT_TEXT[effect]}."
        )
        if effect in PENDING_EFFECTS:
            self._pending[path] = effect
            line += (
                f" Pending until {'a restart' if effect is Effect.RESTART else 'a reload'}: "
                f"{sorted(self._pending)}. Batch further changes first, then call restart once."
            )
        return line

    def _pending_view(self) -> dict[str, list[str]]:
        view: dict[str, list[str]] = {}
        for path, effect in sorted(self._pending.items()):
            view.setdefault(effect.value, []).append(path)
        return view

    @staticmethod
    def _model_ref(value: Any) -> tuple[str, str]:
        if isinstance(value, str):
            return value, ""
        if isinstance(value, dict):
            return str(value.get("model") or ""), str(value.get("provider") or "")
        raise ValueError('a model is "<id>" or {"provider": ..., "model": ...}')

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        if self._call is None:
            raise ValueError(
                "this setting is changed through Raven's settings service, which this process does not "
                "serve (e.g. a one-shot `raven agent`). Ask the user to change it in Settings."
            )
        try:
            return await self._call(method, params)
        except (ValueError, LookupError):
            raise
        except Exception as exc:  # noqa: BLE001 - the writer's refusal is the model's to read
            logger.debug("raven_config: {} refused: {}", method, exc)
            raise ValueError(f"{method} refused: {exc}") from exc

    async def _subagent_rows(self) -> list[dict[str, Any]]:
        result = await self._rpc("subagents.list", {})
        rows = result.get("rows") if isinstance(result, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    async def _subagent_row(self, name: str) -> dict[str, Any]:
        rows = await self._subagent_rows()
        for row in rows:
            if str(row.get("name", "")).lower() == name.lower():
                return row
        raise LookupError(f"no sub-agent named {name!r}; known: {[r.get('name') for r in rows]}")


__all__ = ["GUIDE_SKILL_ID", "READ_ACTIONS", "RavenConfigTool"]
