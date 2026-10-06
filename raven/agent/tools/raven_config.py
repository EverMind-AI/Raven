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
import difflib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from raven.config import self_surface as surface
from raven.config.self_surface import EFFECT_TEXT, PENDING_EFFECTS, Effect, Section, Setting
from raven.contracts.asking import CredentialOutcome, CredentialRequest
from raven.contracts.tool import Tool
from raven.permissions.turn import current_turn

RpcCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
Restarter = Callable[[str], Awaitable[str]]
#: A conversation's model and whether it chose it (False: it follows the default).
SessionModel = Callable[[str], tuple[str, bool]]

GUIDE_SKILL_ID = "local/raven-self-config"

_ACTIONS = ("describe", "get", "set", "unset", "add", "test", "restart")
READ_ACTIONS = frozenset({"describe", "get"})
_RESTART_TARGETS = ("reload", "restart")
#: Memory roles that run on the main model when unset (``raven_everos.config.FOLLOWS_MAIN_ROLES``).
_FOLLOWS_MAIN = ("llm",)

_EFFECT_SHORT = {
    Effect.NEXT_TURN: "next turn",
    Effect.IMMEDIATE: "at once",
    Effect.RELOAD: "needs reload",
    Effect.RESTART: "needs restart",
    Effect.MEMORY_SERVER: "memory server restarts itself",
    Effect.INERT: "no effect",
}
#: One vendor's key among several siblings, listed as one line rather than nine.
_VENDOR_KEY = re.compile(r"^(?P<head>.+)\.(?P<name>[^.*]+)\.apiKey$")

#: The channels a name people use can mean, for a read that names one of them.
_CHANNEL_CANDIDATES = {
    "wechat": ("weixin", "wecom"),
    "enterprisewechat": ("wecom",),
    "lark": ("feishu",),
}
#: Names people use for a channel whose id is something else.
_CHANNEL_ALIASES = {
    "wechat": ". WeChat is `weixin` (a personal account, QR login) or `wecom` (WeCom / Enterprise WeChat)",
    "lark": ". Lark is `feishu`",
}

_INDEX_HEAD = (
    "Every setting with its current value: path = value [type, when it applies] summary. "
    'Change with set (value as JSON; a model is {"provider": ..., "model": ...}); several settings '
    "for one request go in one set with no path and value {path: value, ...}. The set reply confirms "
    "the new value, so there is no need to read it back. describe <path> shows one setting's notes; "
    "describe <words> searches."
)


class _RefusalError(ValueError):
    """A settings method said no; ``data`` is what it said as fields (a sub-agent's ``remedy``)."""

    def __init__(self, text: str, data: Any = None) -> None:
        super().__init__(text)
        self.data = data if isinstance(data, dict) else {}


_NOT_HERE = (
    "If none of these is it, Raven cannot change that through raven_config: tell the user so, and where "
    "it lives if the Settings page has it. Do not look for it in Raven's source code, logs or config files."
)


_parse_value = surface.decode_value


def _conversation() -> str:
    """The conversation this call runs in, as the permission turn names it; empty outside one."""
    return current_turn().conversation_id


def _check_subagent_value(path: str, value: Any) -> None:
    """Refuse a value ``subagents.<name>.<field>`` cannot take, before anything is written."""
    field_name = path.rsplit(".", 1)[-1]
    if field_name == "enabled" and not isinstance(value, bool):
        raise ValueError(f"{path} takes true or false")
    if field_name == "description" and not (isinstance(value, str) and value.strip()):
        raise ValueError(f"{path} takes a non-empty string")
    if field_name == "lendKeys" and not (isinstance(value, list) and all(isinstance(p, str) for p in value)):
        raise ValueError(f'{path} takes a list of Raven providers, e.g. ["openrouter"]; [] lends none')
    if field_name not in ("enabled", "description", "model", "lendKeys"):
        raise LookupError(f"sub-agents expose description, enabled, model and lendKeys; not {field_name!r}")


def _with_mode_note(reply: str, paths: list[str]) -> str:
    """``reply``, plus what a default approval mode means for a conversation that set its own.

    ``permissions.mode`` is the default; a conversation whose picker chose a mode
    keeps it, so "takes effect from the next turn" was wrong for this one --
    and wrong in the unsafe direction when the user asked to go back to ask.
    """
    if "permissions.mode" not in paths or reply.startswith("Error"):
        return reply
    from raven.permissions.session import session_mode

    own = session_mode(_conversation()) if _conversation() else None
    if not own:
        return reply
    return (
        f"{reply}\nThis conversation has its own approval mode ({own}), which this does not change: only "
        "conversations without their own mode follow the default. Tell the user to switch this one in the "
        "composer's mode picker."
    )


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


#: What a refusal's ``remedy.kind`` asks for, when Raven can do it itself.
_YOURS = {
    "silent": "It gave no reason. Run `{command}` with exec; it prints what its model provider answered.",
    "exited": "It quit on start; its last words are in the refusal. Fix what they name (a dependency, a flag, "
    "its config), then retry.",
    "download": "Fetching it failed. Run `{command}` once with exec (nothing times it out there), then retry.",
    "upgrade": "It is too old to be connected. Upgrade it with `{command}`, then retry.",
    "runtime": "Its Node.js is too old (needs {needs}, found {found}). Upgrade Node.js{with_command}, then retry.",
    "model": "Its provider will not serve the model it is set to. Pick another one it lists and retry.",
    "quota": "It is rate-limited or out of quota on this model. Try another model it lists; if none works, "
    "tell the user when it resets.",
    "network": "Its provider could not be reached. Check the host and proxy in its own settings, then retry.",
    "config": "Its config file is invalid. `{command}` says where; fix that, then retry.",
}
#: What only the user can do: a sign-in, a key, money.
_THEIRS = {
    "sign_in": "It needs its own sign-in (a browser or a device code): the user runs `{command}`{then}. "
    "Offer to retry once they have.",
    "setup": "It needs its provider chosen and signed in: the user runs `{command}`{then}. Offer to retry after.",
    "api_key": "It needs an API key: the user enters it in this agent's settings.",
    "billing": "Its provider account is out of credit: the user tops it up, unless another model it lists is free.",
    "plan": "The account's plan does not include it: `{command}`.",
}


def _connected_line(name: str, row: dict[str, Any] | None) -> str:
    """One connected agent as the add reply reports it: applied, and what it answered with."""
    line = f"Connected sub-agent {name} ({EFFECT_TEXT[Effect.IMMEDIATE]})."
    if row:
        if row.get("probe_detail"):
            line += f" {row['probe_detail']}."
        choices = [c.get("value") for c in row.get("model_choices") or [] if isinstance(c, dict) and c.get("value")]
        if choices:
            line += f" Models it lists: {', '.join(str(c) for c in choices[:12])}."
    return line


def _model_check(value: str, row: dict[str, Any]) -> str:
    """Whether a model just set is one the agent itself lists, so the write needs no read-back."""
    choices = [str(c.get("value")) for c in row.get("model_choices") or [] if isinstance(c, dict) and c.get("value")]
    if not choices:
        return "Its model list has not been measured yet; test it to see that it takes this one."
    if value in choices:
        return f"It is one of the models the agent lists ({', '.join(choices[:12])})."
    return f"The agent does not list it ({', '.join(choices[:12])}); it may refuse it -- pick one of those."


_ADD_KEYS = ("preset", "name", "description", "model", "lend_key")


def _add_params(value: dict[str, Any]) -> dict[str, Any]:
    """One add's RPC params, refusing what would otherwise be dropped without a word."""
    unknown = sorted(set(value) - set(_ADD_KEYS))
    if unknown:
        raise ValueError(
            f"add does not take {unknown}; it takes {list(_ADD_KEYS)}. A preset's launch command is fixed."
        )
    return {k: value[k] for k in _ADD_KEYS if isinstance(value.get(k), str)}


def _lending(preset: str | None, name: str | None = None) -> tuple[list[str], list[str]]:
    """``(lent, can_lend)``: the Raven providers this agent is started with, and the ones it could be.

    ``can_lend`` is every provider the preset reads a key for that Raven holds
    a key for and does not lend it yet; the key itself is never read out.
    """
    from raven.agent.subagent.presets import lendable_keys

    readable = lendable_keys(preset)
    if not readable:
        return [], []
    try:
        raw = surface.read_raw()
    except (OSError, ValueError):
        return [], []
    lent: list[str] = []
    for row in surface.lookup(raw, "subagents.agents")[1] or []:
        if isinstance(row, dict) and name and row.get("name") == name:
            lent = [str(p) for p in row.get("lendKeys") or row.get("lend_keys") or []]
    held = [p for p in readable if (surface.lookup(raw, f"providers.{p}.apiKey")[1] or "").strip()]
    return lent, [p for p in held if p not in lent]


def _lend_line(preset: str, name: str | None, can_lend: list[str]) -> str:
    target = f"set subagents.{name}.lendKeys to {json.dumps(can_lend[:1])}" if name else ""
    add = f'add with {{"preset": "{preset}", "lend_key": "{can_lend[0]}"}}'
    return (
        f"- Raven holds a key it can use ({', '.join(can_lend)}): it can be started with Raven's key instead of "
        f"a login of its own -- {target + ', or ' if target else ''}{add}; the user confirms and nothing is "
        "entered or read. Offer that before asking the user to sign it in."
    )


def _what_it_needs(
    preset: str, row: dict[str, Any], *, remedy: dict[str, Any] | None, detail: str, model: str
) -> list[str]:
    """The next step for one agent that did not answer, read off its refusal's remedy."""
    from raven.agent.subagent.presets import DIAGNOSE_HINTS

    remedy = remedy or {}
    kind = str(remedy.get("kind") or "")
    fallback = {
        "download": "its launch command (npx -y <package> --version fetches it)",
        "config": "its own config check (its --help names it: doctor, config validate)",
    }.get(kind, "its own sign-in or setup command (find it in its --help: login, auth, onboard, configure)")
    fields = {
        "command": remedy.get("command") or fallback,
        "then": f", then types {remedy['then']}" if remedy.get("then") else "",
        "needs": remedy.get("needs") or "newer",
        "found": remedy.get("found") or "older",
        "with_command": f" with `{remedy['command']}`" if remedy.get("command") else "",
    }
    lent, can_lend = _lending(preset, row.get("name") if row.get("configured") else None)
    if kind in ("sign_in", "setup", "api_key") and can_lend:
        lines = [_lend_line(preset, row.get("name") if row.get("configured") else None, can_lend)]
    elif kind in _THEIRS:
        lines = ["- This one needs the user. " + _THEIRS[kind].format(**fields)]
    elif kind in _YOURS and (kind != "silent" or remedy.get("command")):
        lines = ["- " + _YOURS[kind].format(**fields)]
    elif "not on the login shell PATH" in detail or "cannot start" in detail:
        lines = [
            "- It is not installed. Install it with exec: the command in the refusal, or its official "
            "one; the user confirms. Then retry."
        ]
    else:
        run = DIAGNOSE_HINTS.get(preset)
        lines = [
            f"- Run `{run}` with exec; it prints the answer its model provider gave."
            if run
            else "- Run it on its own with exec in its one-shot mode (its --help names it); that prints the real error."
        ]
    choices = [c.get("value") for c in row.get("model_choices") or [] if isinstance(c, dict) and c.get("value")]
    if choices:
        lines.append(f"- Models it lists: {', '.join(str(c) for c in choices[:12])}. To try another, {model}.")
    return lines


def _fix_rules(retry: str) -> list[str]:
    """What holds for every agent that did not answer, said once however many did."""
    return [
        "Find out why yourself and fix what is yours to fix, then " + retry + ". A few commands are enough: "
        "if three have not told you why, stop and report what you saw. Running an agent yourself is quicker "
        "than scripting its protocol.",
        "- What it says decides who acts. Yours: not installed, too old, a model it will not serve, a switch in "
        "its config. The user's: a sign-in or a key (401, 403, authentication failed, unauthorized, invalid API "
        "key, not logged in, token missing) -- name the agent's own command for it -- and a model that costs "
        "money (ask; if nobody answers, do not switch to a paid one).",
        "- Never read, copy or test a key yourself: no hunting for one elsewhere, no curl, no credential stores "
        "or databases (a key Raven holds reaches an agent only by lending it, which you never see); do not "
        "generate or replace its tokens, and do not restart or stop its services -- other apps depend on them.",
        "- Its settings are its own files (keys in them come back redacted); Raven's config, logs and state are "
        "no help here. Tell the user what you found and what you already fixed.",
    ]


def _fix_it_yourself(
    preset: str, row: dict[str, Any], *, remedy: dict[str, Any] | None, detail: str, retry: str, model: str
) -> str:
    """What to do about an agent that did not answer: fix what Raven can, ask only for what needs the user."""
    needs = _what_it_needs(preset, row, remedy=remedy, detail=detail, model=model)
    rules = _fix_rules(retry)
    return "\n".join([rules[0], *needs, *rules[1:]])


_MEDIA_MODEL = re.compile(r"^tools\.media\.(?P<kind>image|speech|video)\.model$")


def _media_needs(raw: dict[str, Any], path: str) -> str:
    """What else an unset media model needs, read from the keys actually on file.

    The rule is ``config.schema``'s (``media_provider``): an empty media key is
    borrowed from the provider the section names, from ``providers.openrouter``
    while the section calls OpenRouter, and from nobody for a section pointed
    elsewhere.
    Whether a model alone is enough depends on whether that key or the tool's
    own is set, so it is said per install rather than as a fixed sentence that
    is wrong wherever neither is.
    """
    found = _MEDIA_MODEL.match(path)
    if found is None:
        return ""
    from types import SimpleNamespace

    from raven.config.schema import media_provider

    kind = found["kind"]
    _, own = surface.lookup(raw, f"tools.media.{kind}.apiKey")
    _, named = surface.lookup(raw, f"tools.media.{kind}.provider")
    _, base = surface.lookup(raw, f"tools.media.{kind}.apiBase")
    runs_on = media_provider(SimpleNamespace(provider=named or "", api_base=base or ""))
    if runs_on and runs_on != "openrouter":
        _, borrowed = surface.lookup(raw, f"providers.{runs_on}.apiKey")
        own, wanted = None, f"providers.{runs_on}.apiKey"
    elif runs_on:
        _, borrowed = surface.lookup(raw, "providers.openrouter.apiKey")
        wanted = f"tools.media.{kind}.apiKey, or one under providers.openrouter"
    else:
        borrowed, wanted = None, f"tools.media.{kind}.apiKey"
    if own or borrowed:
        return "a model is all it lacks: a key it can use is already set"
    return f"it also needs a key: {wanted}"


def _compact(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 100 else text[:97] + "..."


def _words(text: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return [w for w in re.split(r"[^0-9A-Za-z]+", spaced.lower()) if w]


def _search(query: str) -> list[Setting]:
    """Settings whose path, summary or notes carry the query's words, most words first."""
    wanted = set(_words(query))
    if not wanted:
        return []
    scored = []
    for setting in surface.all_settings():
        have = set(_words(" ".join((setting.path, setting.summary, setting.note, setting.unset_means))))
        hits = len(wanted & have) + sum(1 for w in wanted - have if any(h.startswith(w) for h in have if len(w) > 2))
        if hits:
            scored.append((hits, setting))
    scored.sort(key=lambda pair: -pair[0])
    best = scored[0][0] if scored else 0
    return [s for n, s in scored if n == best][:8]


class RavenConfigTool(Tool):
    """Read and change Raven's own configuration through the catalog."""

    timeout_seconds = 120.0
    approval_kind = "config.change"

    def __init__(
        self,
        *,
        guide_skill_id: str | None = GUIDE_SKILL_ID,
        session_model: SessionModel | None = None,
        tool_names: Callable[[], list[str]] | None = None,
    ) -> None:
        self._guide = guide_skill_id
        self._session_model = session_model
        self._tool_names = tool_names
        self._call: RpcCaller | None = None
        self._restart: Restarter | None = None
        self._pending: dict[str, Effect] = {}

    def set_rpc_caller(self, call: RpcCaller | None) -> None:
        """Lend the entrance's settings methods (``settings.set`` and kin)."""
        self._call = call

    def set_restarter(self, restart: Restarter | None) -> None:
        """Lend the gateway's reload and restart; absent everywhere else."""
        self._restart = restart

    def blocking_for(self, params: dict[str, Any]) -> bool:
        """A call that asks the user to type a key waits on them, so no tool timeout cuts it short."""
        if params.get("action") != "set":
            return False
        changes = surface.batch_of(params) or [(surface.path_of(params), params.get("value"))]
        return any(surface.is_secret_path(path) for path, _ in changes)

    def approval_evidence(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("action") == "restart" and not (
            isinstance(target := surface.decode_value(params.get("value")), str) and target
        ):
            # What `_do_restart` falls back to for the same value, so the card
            # names the restart that will run and not a reload.
            params = {**params, "value": self._needed_restart()}
        view = surface.change_view(params, surface.read_raw())
        for row in view.get("changes") or [view]:
            if row.get("setting") == "session.model" and (now := self._conversation_model()) is not None:
                row["was"] = now
        if view.get("agents"):
            from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS

            # The name the agents page shows a preset under ("Pi", not "pi"),
            # where the call did not name the agent itself.
            for agent in view["agents"]:
                preset = THIRD_PARTY_SUBAGENT_PRESETS.get(agent.get("preset", ""), {})
                agent["title"] = agent.get("name") or str(preset.get("name") or agent.get("preset") or "")
        return view

    @property
    def name(self) -> str:
        return "raven_config"

    @property
    def description(self) -> str:
        guide = f" Read skill {self._guide} before changing anything." if self._guide else ""
        return (
            f"Read and change Raven's own settings. Changes follow the user's approval mode.{guide} "
            "describe lists every setting with its value; describe <words> searches."
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
        path = surface.path_of(kwargs)
        value = _parse_value(kwargs.get("value"))
        try:
            if action == "describe":
                return await self._describe(path)
            if action == "get":
                return await self._get(path, value)
            if action == "set":
                if not path and isinstance(value, dict):
                    return _with_mode_note(await self._set_many(value), [surface.canonical_path(p) for p in value])
                return _with_mode_note(await self._set(path, value), [path])
            if action == "unset":
                return _with_mode_note(await self._unset(path), [path])
            if action == "test":
                return await self._test(path)
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
            return await self._index(None)
        if section := surface.section_of(path):
            return await self._index(section)
        if path.startswith("channels.") and path.count(".") == 1:
            name = path.split(".", 1)[1]
            if candidates := _CHANNEL_CANDIDATES.get(name.lower()):
                return await self._describe_candidates(name, candidates)
            return _dump(self._describe_channel(name) | await self._channel_state(name))
        if path.startswith("subagents.") and path.count(".") == 1:
            return _dump(await self._describe_subagent(path.split(".", 1)[1]))
        found = surface.find(path)
        raw = await asyncio.to_thread(surface.read_raw)
        if found is None:
            below = [s for s in surface.all_settings() if s.path.startswith(path + ".")]
            if below:
                roles = await self._everos_roles() if path.startswith("memory") else None
                return "\n".join(self._setting_lines(raw, below, roles, detail=True))
            return await self._search_reply(path, raw)
        setting = found[0]
        roles = await self._everos_roles() if setting.writer == "everos" else None
        out = setting.describe() | {"path": path, "value": self._value_view(raw, setting, path, roles)}
        if path == "tools.disabledTools" and self._tool_names is not None:
            out["tool_names"] = sorted(self._tool_names())
        return _dump(out)

    async def _index(self, only: Section | None) -> str:
        """The catalog with current values, whole or one section: one read instead of a walk."""
        raw = await asyncio.to_thread(surface.read_raw)
        wants = {only.name} if only is not None else {s.name for s in surface.sections()}
        roles = await self._everos_roles() if "memory" in wants else None
        agents = await self._subagent_rows_or_none() if "subagents" in wants else None
        lines = [_INDEX_HEAD]
        for section in (only,) if only is not None else surface.sections():
            lines.append(f"[{section.name}] {section.summary}")
            if section.name == "channels":
                lines.extend(self._channel_lines(raw))
            if section.name == "subagents":
                lines.extend(self._subagent_lines(agents))
                continue
            if section.name == "providers":
                lines.append(
                    "  providers.<name>.catalog -- get it for the model ids that provider serves "
                    '(value filters: "glm", or alternatives "opus, gpt-5, gemini"); pick a model from there, not from memory'
                )
            listed = [s for s in section.settings if not (section.name == "channels" and "*" in s.path)]
            lines.extend(self._setting_lines(raw, listed, roles, detail=only is not None))
        if self._pending:
            lines.append(f"pending: {json.dumps(self._pending_view(), ensure_ascii=False)}")
        if self._call is None:
            lines.append("note: this process lent no settings writer: only some settings can be changed here")
        return "\n".join(lines)

    def _setting_lines(
        self, raw: dict[str, Any], settings: list[Setting], roles: dict[str, Any] | None, *, detail: bool
    ) -> list[str]:
        lines: list[str] = []
        keys: dict[str, tuple[Setting, list[str], list[str]]] = {}
        for setting in settings:
            for path in surface.concrete_paths(setting, raw):
                grouped = _VENDOR_KEY.match(path) if setting.secret and "*" not in setting.path else None
                if grouped is not None:
                    head = grouped["head"]
                    if head not in keys:
                        keys[head] = (setting, [], [])
                        lines.append(f"\0{head}")
                    present, value = surface.lookup(raw, path)
                    keys[head][1 if present and value else 2].append(grouped["name"])
                    continue
                lines.append(self._setting_line(raw, setting, path, roles, detail=detail))
        patterns: dict[str, list[str]] = {}
        for setting in settings:
            if "*" in setting.path:
                head, _, field_name = setting.path.rpartition(".")
                patterns.setdefault(head.replace("*", "<name>"), []).append(field_name)
        for head, fields in patterns.items():
            lines.append(f"  {head}.{'|'.join(fields)} -- the same settings for an instance not listed above")
        out = []
        for line in lines:
            if line.startswith("\0"):
                head = line[1:]
                setting, have, lack = keys[head]
                out.append(
                    f"  {head}.<name>.apiKey [secret, {_EFFECT_SHORT[setting.effect]}] key set for: "
                    f"{', '.join(have) or 'none'}; not set: {', '.join(lack) or 'none'}"
                )
            else:
                out.append(line)
        return out

    def _setting_line(
        self, raw: dict[str, Any], setting: Setting, path: str, roles: dict[str, Any] | None, *, detail: bool
    ) -> str:
        value = self._value_view(raw, setting, path, roles)
        if isinstance(value, dict) and set(value) == {"default"}:
            default = value["default"]
            if default in (None, "", [], {}) and setting.unset_means:
                shown = f"not set ({setting.unset_means})"
            elif default in (None, ""):
                shown = "not set"
            else:
                shown = f"{_compact(default)} (default)"
        else:
            shown = _compact(value)
        line = f"  {path} = {shown} [{self._meta(setting)}] {setting.summary}"
        if shown.startswith("not set") and (missing := _media_needs(raw, path)):
            line += f"; {missing}"
        if detail and setting.note:
            line += f"; note: {setting.note}"
        if setting.sensitive:
            line += f"; sensitive: {setting.sensitive}"
        return line

    @staticmethod
    def _meta(setting: Setting) -> str:
        parts = ["secret" if setting.secret else setting.kind]
        if setting.choices and len(setting.choices) <= 10:
            parts[0] += " " + "|".join(c or '""' for c in setting.choices)
        if setting.low is not None or setting.high is not None:
            lo = "" if setting.low is None else f"{setting.low:g}"
            hi = "" if setting.high is None else f"{setting.high:g}"
            parts[0] += f" {lo}..{hi}"
        parts.append(_EFFECT_SHORT[setting.effect])
        if setting.session:
            parts.append("this conversation only")
        return ", ".join(parts)

    @staticmethod
    def _channel_lines(raw: dict[str, Any]) -> list[str]:
        from raven.config.update_channels import channel_names

        on, off = [], []
        for name in channel_names():
            present, enabled = surface.lookup(raw, f"channels.{name}.enabled")
            (on if present and enabled is True else off).append(name)
        return [
            f"  switched on: {', '.join(on) or 'none'}; off: {', '.join(off) or 'none'} "
            "(on is not connected: describe channels.<name> says whether it is)",
            "  each channel's fields (enabled, allowFrom, its credentials): describe channels.<name>",
        ]

    def _subagent_lines(self, rows: list[dict[str, Any]] | None) -> list[str]:
        if rows is None:
            return ["  (the roster is read through the gateway, which this process does not reach)"]
        lines = []
        for row in rows:
            view = self._subagent_view(row)
            state = "not added" if view.get("added") is False else ("on" if view.get("enabled") else "off")
            bits = [state, str(view.get("kind") or "")]
            if view.get("model"):
                bits.append(f"model {view['model']}")
            if view.get("status") not in (None, "ready", "installed"):
                bits.append(
                    f"status {view['status']}" + (f": {view['status_detail']}" if view.get("status_detail") else "")
                )
            if view.get("last_test", {}).get("ok") is False:
                bits.append(f"last test failed: {view['last_test'].get('detail')}")
            lines.append(f"  subagents.{view['name']}: {'; '.join(b for b in bits if b)}")
        lines.append(
            '  a preset marked "not added" is connected with add subagents {"preset": "<preset>"}; an agent '
            "not listed here cannot be connected from here; several agents go in one add as a list; "
            "describe subagents.<name> for one agent's settings and health"
        )
        return lines

    async def _search_reply(self, query: str, raw: dict[str, Any]) -> str:
        instance = await self._instance_named(query)
        if instance:
            return instance
        hits = _search(query)
        close = difflib.get_close_matches(query, [s.path for s in surface.all_settings()], n=3, cutoff=0.6)
        picked = list(dict.fromkeys([*close, *(s.path for s in hits)]))[:8]
        if not picked:
            hint = " (search matches the English words of paths and summaries)" if not query.isascii() else ""
            return f"No setting matches {query!r}{hint}. {_NOT_HERE}"
        by_path = {s.path: s for s in surface.all_settings()}
        lines = [f"{query!r} is not a path; the closest settings:"]
        for path in picked:
            setting = by_path[path]
            if "*" in path:
                lines.append(f"  {path.replace('*', '<name>')} [{self._meta(setting)}] {setting.summary}")
            else:
                lines.append(self._setting_line(raw, setting, path, None, detail=True))
        lines.append(_NOT_HERE)
        return "\n".join(lines)

    def _describe_channel(self, name: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
        """One channel's fields with their values (secrets as set / not set), and how it logs in."""
        from raven.config.update_channels import channel_field_specs, channel_names

        if name not in channel_names():
            hint = _CHANNEL_ALIASES.get(name.lower(), "")
            raise LookupError(f"unknown channel {name!r}; known: {channel_names()}{hint}")
        raw = surface.read_raw() if raw is None else raw
        specs = {k: v for k, v in channel_field_specs(name).items() if k != "workspace"}
        fields = []
        for key, spec in specs.items():
            entry: dict[str, Any] = {"path": f"channels.{name}.{key}", "type": str(spec.get("type"))}
            entry["value"] = self._channel_value(raw, {"path": entry["path"], "secret": spec.get("is_secret")})
            if spec.get("required"):
                entry["required"] = True
            if spec.get("description"):
                entry["summary"] = spec["description"]
            if spec.get("is_secret"):
                entry["secret"] = True
            if reason := surface.sensitive_reason(entry["path"]):
                entry["sensitive"] = reason
            fields.append(entry)
        out: dict[str, Any] = {
            "channel": name,
            "takes_effect": EFFECT_TEXT[Effect.IMMEDIATE] + " (the gateway restarts this channel)",
            "fields": fields,
        }
        if not any(spec.get("required") for spec in specs.values()):
            out["login"] = (
                "by scanning a QR code, not by credentials: switch it on and the code appears in Settings > "
                f"Channels > {name} for the user to scan; its token is filled in by that scan, so leave it"
            )
        else:
            secrets = [k for k, v in specs.items() if v.get("required") and v.get("is_secret")]
            if secrets:
                verb = "is a secret" if len(secrets) == 1 else "are secrets"
                out["login"] = (
                    f"with credentials from the {name} developer console: {', '.join(secrets)} {verb} the user "
                    "enters in Settings > Channels; set the other required fields and enabled here"
                )
        return out

    async def _describe_subagent(self, name: str) -> dict[str, Any]:
        row = await self._subagent_row(name)
        out = self._subagent_view(row)
        source = row.get("model_source")
        if source == "fixed":
            out["model_note"] = "this agent's model is fixed (it carries its own key, or its kind has no model switch)"
        elif source == "agent":
            out["model_choices"] = row.get("model_choices") or []
            if not out["model_choices"] and out.get("added") is not False:
                out["model_note"] = (
                    f"its model list is not measured yet: set subagents.{row.get('name')}.model to the one you want; "
                    "the list is read then, and the reply says whether the agent offers it"
                )
        elif source == "raven":
            out["model_note"] = 'pick from Raven\'s own providers: {"provider": ..., "model": ...}'
        if row.get("builtin"):
            out["description_note"] = "the built-in row's description is fixed"
        if out.get("added") is not False:
            out["settings"] = [
                s.describe() | {"path": s.path.replace("*", str(row.get("name")))}
                for s in surface.section_of("subagents").settings  # type: ignore[union-attr]
            ]
        return out

    @staticmethod
    def _subagent_view(row: dict[str, Any]) -> dict[str, Any]:
        """One roster row with what explains a failure: added or not, health, the last test."""
        out: dict[str, Any] = {k: row.get(k) for k in ("name", "kind", "enabled", "description", "model")}
        if not (row.get("configured") or row.get("builtin") or row.get("vendored")):
            preset = row.get("preset") or row.get("name")
            out["added"] = False
            out["next_step"] = f'not added yet: add subagents {{"preset": "{preset}"}} connects it'
        status = row.get("probe_status")
        if status:
            out["status"] = status
        if row.get("probe_detail"):
            out["status_detail"] = row["probe_detail"]
        if row.get("probe_missing"):
            out["missing_program"] = row["probe_missing"]
        lent, can_lend = _lending(row.get("preset"), row.get("name") if row.get("configured") else None)
        if lent:
            out["lends_keys"] = lent
        if can_lend:
            out["can_lend"] = can_lend
        if row.get("needs_auth"):
            out["needs_auth"] = (
                "it needs a credential: Raven's key can be lent (can_lend), or its own login or API key"
                if can_lend
                else "it needs a credential of its own (its login or API key), set up outside Raven"
            )
        if row.get("last_test_ok") is not None:
            last: dict[str, Any] = {"ok": row["last_test_ok"]}
            if row.get("last_test_detail"):
                last["detail"] = row["last_test_detail"]
            if row.get("last_test_remedy"):
                last["remedy"] = row["last_test_remedy"]
            out["last_test"] = last
        return out

    async def _get(self, path: str, value: Any = None) -> str:
        if not path:
            raise ValueError("get needs a path; describe lists them")
        raw = await asyncio.to_thread(surface.read_raw)
        roles = await self._everos_roles() if path == "memory" or path.startswith("memory.models") else None
        if section := surface.section_of(path):
            if section.name == "subagents":
                return _dump([self._subagent_view(r) for r in await self._subagent_rows()])
            return _dump(
                {
                    p: self._value_view(raw, s, p, roles)
                    for s in section.settings
                    for p in surface.concrete_paths(s, raw)
                }
            )
        if path.startswith("providers.") and path.endswith(".catalog") and path.count(".") == 2:
            return await self._catalog(path.split(".")[1], value)
        if path.startswith("subagents."):
            parts = path.split(".")
            row = await self._subagent_row(parts[1])
            if len(parts) == 2:
                return _dump(self._subagent_view(row))
            return _dump({path: surface.redacted({parts[2]: row.get(parts[2])})[parts[2]]})
        if path.startswith("channels.") and path.count(".") == 1:
            name = path.split(".", 1)[1]
            view = self._describe_channel(name, raw)
            return _dump({f["path"]: f["value"] for f in view["fields"]})
        found = surface.find(path)
        if found is None:
            if path.startswith("channels."):
                return _dump({path: self._channel_value(raw, {"path": path, "secret": surface.is_secret_path(path)})})
            below = {
                p: self._value_view(raw, s, p, roles)
                for s in surface.all_settings()
                for p in surface.concrete_paths(s, raw)
                if p.startswith(path + ".")
            }
            if below:
                return _dump(below)
            return await self._search_reply(path, raw)
        return _dump({path: self._value_view(raw, found[0], path, roles)})

    def _value_view(self, raw: dict[str, Any], setting: Setting, path: str, roles: dict[str, Any] | None = None) -> Any:
        if setting.session:
            now = self._conversation_model()
            return now if now is not None else "unknown outside a conversation"
        if setting.writer == "everos":
            return self._everos_view(raw, setting, path, roles or {})
        present, value = surface.lookup(raw, path)
        if setting.secret:
            return "set" if present and value else "not set"
        if not present:
            value = {"default": surface.default_of(path)}
            if setting.keys and isinstance(value["default"], dict):
                value = {"default": {k: value["default"].get(k) for k in setting.keys}}
            return surface.redacted(value, path)
        if setting.keys and isinstance(value, dict):
            value = {k: surface.lookup(value, k)[1] for k in setting.keys}
        return surface.redacted(value, path)

    async def _everos_roles(self) -> dict[str, Any] | None:
        if self._call is None:
            return None
        try:
            result = await self._rpc("settings.everos", {})
        except ValueError:
            return None
        return result if isinstance(result, dict) else None

    @staticmethod
    def _everos_view(raw: dict[str, Any], setting: Setting, path: str, roles: dict[str, Any]) -> Any:
        """A memory role as the memory server will run it, which is not always what is stored."""
        if roles.get("available") is False:
            return f"unavailable: {roles.get('note') or 'the memory plugin is not installed'}"
        role = path.rsplit(".", 1)[1]
        row = (roles.get("sections") or {}).get(role) or {}
        _, stored = surface.lookup(raw, setting.stored_at)
        stored = stored if isinstance(stored, dict) else {}
        model = str(row.get("model") or stored.get("model") or "")
        provider = str(row.get("provider") or stored.get("provider") or "")
        if model:
            view: dict[str, Any] = {"provider": provider, "model": model}
            if row and not row.get("api_key_set"):
                view["note"] = f"{provider} has no usable API key, so this does not run"
            return view
        if row.get("follows_main"):
            agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
            defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
            now = {"provider": defaults.get("provider"), "model": defaults.get("model")}
            return {"follows": "the main model", "now": now}
        if role in _FOLLOWS_MAIN:
            return "not set, and the main model cannot stand in (no API key Raven can pass on): memory is off"
        return f"not set ({setting.unset_means})"

    @staticmethod
    def _channel_value(raw: dict[str, Any], field: dict[str, Any]) -> Any:
        present, value = surface.lookup(raw, field["path"])
        if field.get("secret"):
            return "set" if present and value else "not set"
        return value if present else None

    # -- set / unset / add ---------------------------------------------------

    async def _set(self, path: str, value: Any) -> str:
        if not path:
            raise ValueError("set needs a path, or no path and value as an object {path: value, ...} for several")
        if path.startswith("channels.") and path.count(".") == 2:
            return await self._set_channel(path, value)
        if path.startswith("channels.") and path.count(".") == 1 and isinstance(value, dict):
            return await self._set_channel_fields(path.split(".")[1], value)
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
        return self._report(path, setting.effect, previous, value) + self._unknown_tools(path, value)

    async def _set_many(self, changes: dict[str, Any]) -> str:
        """Several settings under the one confirmation the user already gave.

        Every value is checked before anything is written, so a typo in the last
        one does not leave the first ones applied. Secrets are reported the way
        a single set reports them: entered on the card, or still to enter.
        """
        plan: list[tuple[str, Setting, list[str], Any]] = []
        channels: dict[str, dict[str, Any]] = {}
        agents: list[tuple[str, Any]] = []
        named = [surface.canonical_path(p) for p in changes]
        if len(set(named)) < len(named):
            raise ValueError("one setting is named twice in this call; nothing was changed")
        for path, raw in ((surface.canonical_path(p), r) for p, r in changes.items()):
            if path.startswith("channels.") and path.count(".") == 2:
                _, name, field_name = path.split(".")
                channels.setdefault(name, {})[field_name] = _parse_value(raw)
                continue
            if path.startswith("subagents.") and path.count(".") == 2:
                agents.append((path, _parse_value(raw)))
                continue
            found = surface.find(path)
            if found is None:
                raise LookupError(f"{path} is not in the catalog; nothing was changed")
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
        for name, fields in channels.items():
            self._channel_plan(name, fields)
        for path, value in agents:
            _check_subagent_value(path, value)
        if (channels or agents) and self._call is None:
            raise ValueError(
                "channels and sub-agents are changed through Raven's services, which this process does not serve; "
                "nothing was changed"
            )
        lines: list[str] = []
        try:
            for path, setting, bound, value in plan:
                if setting.secret:
                    lines.append(await self._secret_outcome(path, setting, value))
                    continue
                previous = await self._write(setting, path, bound, value)
                lines.append(self._report(path, setting.effect, previous, value) + self._unknown_tools(path, value))
            for name, fields in channels.items():
                lines.append(await self._set_channel_fields(name, fields))
            for path, value in agents:
                lines.append(await self._set_subagent(path, value))
        except (ValueError, KeyError, LookupError) as exc:
            # What a service refused only once it was asked; say what already took.
            if lines:
                raise type(exc)(f"{exc}; applied before it: " + " ".join(lines)) from exc
            raise
        return "\n".join(lines)

    async def _secret_outcome(self, path: str, setting: Setting, value: Any) -> str:
        """Ask the user to type a secret on a credential card, and say whether it is set now.

        The value never reaches this tool: the card sends it to the host, which
        writes it and answers only saved or skipped. Where no surface can show
        the card (a terminal, a chat channel) the user is told where to enter it.
        """
        if value not in (None, ""):
            return f"{path} was not written: a key never goes through a tool call. Ask the user to rotate it."
        present, now = surface.lookup(await asyncio.to_thread(surface.read_raw), path)
        where = setting.note or "set it in Settings"
        turn = current_turn()
        if surface.secret_input(path) is None or turn.credentials is None or not turn.conversation_id:
            state = "is set" if present and now else "is not set"
            return f"{path} {state}, and it cannot be typed in here. Ask the user to {where}."
        outcome = await turn.credentials.request_credential(
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            request=CredentialRequest(
                target=f"config:{path}", label=surface.secret_label(path), replaces=bool(present and now)
            ),
        )
        if outcome is CredentialOutcome.SAVED:
            return f"{path} is set (the user entered it; the value is not shown). It {EFFECT_TEXT[setting.effect]}."
        kept = " The key already set stays as it was." if present and now else ""
        return f"The user skipped entering {path}.{kept} If they still want it, ask them to {where}."

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
        if writer == "everos":
            model, provider = self._model_ref(value)
            if not provider:
                raise ValueError(f'{path} needs its provider: {{"provider": ..., "model": ...}}')
            _, previous = surface.lookup(await asyncio.to_thread(surface.read_raw), setting.stored_at)
            await self._rpc(
                "settings.everos_set", {"section": path.rsplit(".", 1)[1], "model": model, "provider": provider}
            )
            return previous
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
        if setting.writer == "everos":
            raw = await asyncio.to_thread(surface.read_raw)
            present, previous = surface.lookup(raw, setting.stored_at)
            if not present:
                return f"{path} is already unset: {setting.unset_means}."
            await self._rpc("settings.everos_set", {"section": path.rsplit(".", 1)[1], "clear": True})
            return (
                f"Cleared {path} (was {json.dumps(previous, ensure_ascii=False)}): {setting.unset_means}. "
                f"It {EFFECT_TEXT[setting.effect]}."
            )
        if setting.writer != "raw" or setting.secret:
            return f"{path} cannot be reset from here; set it to the value you want instead."
        previous = await asyncio.to_thread(surface.remove_value, path)
        return self._report(path, setting.effect, previous, {"default": surface.default_of(path)})

    async def _set_channel(self, path: str, value: Any) -> str:
        _, name, field_name = path.split(".")
        return await self._set_channel_fields(name, {field_name: value})

    def _channel_plan(self, name: str, changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """``changes`` checked against the channel's own fields: what to write, and the secrets named."""
        from raven.config.update_channels import channel_field_specs, channel_names

        if name not in channel_names():
            hint = _CHANNEL_ALIASES.get(name.lower(), "")
            raise LookupError(f"unknown channel {name!r}; known: {channel_names()}{hint}; nothing was changed")
        specs = channel_field_specs(name)
        write: dict[str, Any] = {}
        secrets: list[str] = []
        for field_name, value in changes.items():
            key = surface.channel_key(field_name, specs)
            if key in write or key in secrets:
                # Two spellings of one field (allowFrom and allow_from): the card
                # showed both values and the write kept whichever came last.
                raise ValueError(f"channels.{name}.{key} is named twice in one call; nothing was changed")
            if key not in specs or key == "workspace":
                raise LookupError(
                    f"channel {name} has no setting {field_name!r}; describe channels.{name} lists them; "
                    "nothing was changed"
                )
            if specs[key].get("is_secret"):
                secrets.append(key)
                continue
            if key == "enabled" and not isinstance(value, bool):
                raise ValueError(f"channels.{name}.enabled takes true or false; nothing was changed")
            write[key] = value
        return write, secrets

    async def _set_channel_fields(self, name: str, changes: dict[str, Any]) -> str:
        """Several fields of one channel in one write, so the gateway restarts it once."""
        write, secrets = self._channel_plan(name, changes)
        # The secrets first: a channel switched on below then starts with them.
        lines: list[str] = [await self._channel_secret_outcome(name, key) for key in secrets]
        if write:
            enabled = write.pop("enabled", None)
            params: dict[str, Any] = {"name": name}
            if write:
                params["fields"] = write
            if enabled is not None:
                params["enabled"] = enabled
            elif write:
                raw = await asyncio.to_thread(surface.read_raw)
                _, running = surface.lookup(raw, f"channels.{name}.enabled")
                if running is True:
                    # Sent with the switch on so the gateway rebuilds the adapter:
                    # a running channel holds the slice it was built with.
                    params["enabled"] = True
            result = await self._rpc("channels.configure", params)
            outcome = result.get("outcome") if isinstance(result, dict) else None
            done = {**write, **({"enabled": enabled} if enabled is not None else {})}
            line = "Set " + ", ".join(
                f"channels.{name}.{k} to {json.dumps(v, ensure_ascii=False)}" for k, v in done.items()
            )
            if outcome == "unreachable":
                line += (
                    ". Saved; no gateway answered, so the channel starts once Raven runs as a gateway "
                    "(`raven gateway`, or `raven web`, which starts one); nothing else needs setting for that"
                )
            elif outcome:
                line += f". Gateway said: {outcome}"
            if isinstance(result, dict) and result.get("detail"):
                line += f". {result['detail']}"
            lines.append(line + ".")
            if enabled is True:
                state = await self._channel_state(name)
                login = state.get("login") or self._describe_channel(name).get("login")
                if login:
                    lines.append(f"It logs in {login}." if login.startswith(("by ", "with ")) else login)
                if state.get("missing"):
                    lines.append(f"Still missing: {state['missing']}.")
        return "\n".join(lines)

    async def _channel_secret_outcome(self, name: str, key: str) -> str:
        """A channel's secret field typed on the credential card, the way a vendor key is."""
        where = f"enter it in Settings > Channels > {name}"
        present, now = surface.lookup(await asyncio.to_thread(surface.read_raw), f"channels.{name}.{key}")
        turn = current_turn()
        if turn.credentials is None or not turn.conversation_id:
            return f"channels.{name}.{key} is a secret, and it cannot be typed in here; ask the user to {where}."
        outcome = await turn.credentials.request_credential(
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            request=CredentialRequest(
                target=f"channel:{name}.{key}",
                label=f"{name} {key.replace('_', ' ')}",
                replaces=bool(present and now),
            ),
        )
        if outcome is CredentialOutcome.SAVED:
            return f"channels.{name}.{key} is set (the user entered it; the value is not shown)."
        kept = " The value already set stays as it was." if present and now else ""
        return f"The user skipped entering channels.{name}.{key}.{kept} If they still want it, ask them to {where}."

    async def _set_subagent(self, path: str, value: Any) -> str:
        _, name, field_name = path.split(".")
        row = await self._subagent_row(name)
        if self._subagent_view(row).get("added") is False:
            preset = row.get("preset") or name
            return (
                f"{name} is a preset that is not added yet, so it has no settings to change. "
                f'Connect it with add subagents {{"preset": "{preset}"}}; it is on once added.'
            )
        _check_subagent_value(path, value)
        if field_name == "enabled":
            await self._rpc("subagents.toggle", {"name": name, "enabled": value})
        elif field_name == "description":
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
            await self._rpc("subagents.update", {"name": name, "lend_keys": value})
        said = f"Set {path} to {json.dumps(value, ensure_ascii=False)} ({EFFECT_TEXT[Effect.IMMEDIATE]})."
        if field_name == "model" and value is not None:
            said += " " + _model_check(str(value), await self._subagent_row(name))
        return said

    async def _add(self, path: str, value: Any) -> str:
        path = path or "subagents"
        if path != "subagents":
            raise ValueError("add only connects a sub-agent: path 'subagents', value {\"preset\": ...}")
        specs = value if isinstance(value, list) else [value]
        if not specs or not all(isinstance(v, dict) and isinstance(v.get("preset"), str) for v in specs):
            raise ValueError(
                'add takes {"preset": "<preset>", "model"?: ...} for an agent in the describe list -- or a list '
                "of those to connect several under one confirmation"
            )
        params_list = [_add_params(v) for v in specs]
        rows: list[dict[str, Any]] | None = None
        done: list[str] = []
        failed: list[str] = []
        for params in params_list:
            try:
                result = await self._rpc("subagents.add", params)
            except ValueError as exc:
                text = str(exc)
                if "test message" not in text and "no content" not in text:
                    failed.append(f"{params.get('preset') or params.get('name')}: {text}")
                    continue
                if rows is None:
                    try:
                        rows = await self._subagent_rows()
                    except (ValueError, LookupError):
                        rows = []
                key = str(params.get("preset") or params.get("name", "")).lower()
                row = next((r for r in rows if key and key in (r.get("preset"), str(r.get("name", "")).lower())), {})
                again = {k: v for k, v in params.items() if k != "description"} | {"model": "<one>"}
                refusal = getattr(exc, "data", {})
                remedy = refusal.get("remedy")
                if not row.get("model_choices") and refusal.get("models"):
                    row = row | {"model_choices": [{"value": m} for m in refusal["models"]]}
                needs = _what_it_needs(
                    str(params.get("preset") or ""),
                    row,
                    remedy=remedy if isinstance(remedy, dict) else None,
                    detail=text,
                    model=f"add with {json.dumps(again, ensure_ascii=False)}",
                )
                failed.append("\n".join([f"Not added: {text}", *needs]))
                continue
            name = result.get("name") if isinstance(result, dict) else params.get("preset")
            done.append(str(name))
        if done:
            # What a follow-up describe would have been read for: the state it is now in.
            try:
                state = {str(r.get("name")): r for r in await self._subagent_rows()}
            except (ValueError, LookupError):
                state = {}
            done = [_connected_line(name, state.get(name)) for name in done]
        if len(params_list) == 1 and not failed:
            return done[0]
        if len(params_list) == 1 and not failed[0].startswith("Not added"):
            raise ValueError(failed[0].split(": ", 1)[1])
        parts = done + failed
        if any(f.startswith("Not added") for f in failed):
            parts.append("\n".join(_fix_rules("add again")))
        return "\n".join(parts)

    async def _test(self, path: str) -> str:
        """Dispatch a sub-agent once, the way the settings page's Test button does, and report the verdict."""
        if not (path.startswith("subagents.") and path.count(".") == 1):
            raise ValueError("test takes one sub-agent: path 'subagents.<name>'")
        row = await self._subagent_row(path.split(".", 1)[1])
        view = self._subagent_view(row)
        if view.get("added") is False:
            return f"{row.get('name')} is not added yet; {view['next_step']} (adding it runs the same test)."
        source = "vendored" if row.get("vendored") else "config"
        result = await self._rpc("subagents.test", {"name": row.get("name"), "source": source})
        ok = isinstance(result, dict) and result.get("ok")
        after = self._subagent_view(await self._subagent_row(str(row.get("name"))))
        out: dict[str, Any] = {"subagent": row.get("name"), "ok": bool(ok)}
        if isinstance(result, dict) and result.get("detail"):
            out["detail"] = result["detail"]
        if after.get("last_test", {}).get("remedy"):
            out["remedy"] = after["last_test"]["remedy"]
        if after.get("status"):
            out["status"] = after["status"]
        if not ok:
            model = f"set subagents.{row.get('name')}.model to one and test again"
            remedy = after.get("last_test", {}).get("remedy")
            out["next"] = _fix_it_yourself(
                str(row.get("preset") or ""),
                row,
                remedy=remedy if isinstance(remedy, dict) else None,
                detail=str(out.get("detail") or ""),
                retry="test again",
                model=model,
            )
        return _dump(out)

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

    def _unknown_tools(self, path: str, value: Any) -> str:
        if path != "tools.disabledTools" or self._tool_names is None or not isinstance(value, list):
            return ""
        known = set(self._tool_names())
        unknown = [name for name in value if name not in known]
        if not unknown:
            return ""
        return (
            f" Not a tool here (kept, in case it arrives later with a plugin): {unknown}; "
            "describe tools.disabledTools lists the names."
        )

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
            raise _RefusalError(f"{method} refused: {exc}", getattr(exc, "data", None)) from exc

    async def _subagent_rows(self) -> list[dict[str, Any]]:
        result = await self._rpc("subagents.list", {})
        rows = result.get("rows") if isinstance(result, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    async def _catalog(self, slug: str, words: Any) -> str:
        """The model ids a provider serves, so a switch names a real one instead of a remembered one."""
        result = await self._rpc("model.fetch_models", {"slug": slug})
        rows = [r for r in (result.get("models") or []) if isinstance(r, dict)] if isinstance(result, dict) else []
        # "opus, gpt-5 | gemini": alternatives, each a set of words that must all appear.
        groups = [_words(g) for g in re.split(r"[,|]", words)] if isinstance(words, str) else []
        groups = [g for g in groups if g]
        if groups:
            rows = [
                r
                for r in rows
                if any(all(w in f"{r.get('id', '')} {r.get('label', '')}".lower() for w in g) for g in groups)
            ]
        if not groups and len(rows) > 40:
            # Forty of five hundred in alphabetical order answers nothing; the
            # ones the user already picked do, and a filter finds the rest.
            added = [r for r in rows if r.get("added")]
            return _dump(
                {
                    "provider": slug,
                    "count": len(rows),
                    "added": [r.get("id") for r in added],
                    "narrow": 'too many to list: get it again with value, e.g. "glm" or "opus, gpt-5, gemini"',
                }
            )
        shown = [
            {k: r[k] for k in ("id", "kind", "context_window", "added") if r.get(k) not in (None, "")} for r in rows
        ]
        out: dict[str, Any] = {"provider": slug, "models": shown[:40]}
        if len(shown) > 40:
            out["more"] = f'{len(shown) - 40} more; pass value with words to narrow (e.g. "glm")'
        if isinstance(result, dict) and result.get("status") not in (None, "ok"):
            out["status"] = result.get("status")
        out["use"] = 'set agents.defaults.model or session.model to {"provider": "%s", "model": "<id>"}' % slug
        return _dump(out)

    async def _describe_candidates(self, asked: str, candidates: tuple[str, ...]) -> str:
        if len(candidates) == 1:
            name = candidates[0]
            return f"{asked!r} is the channel channels.{name}:\n" + _dump(
                self._describe_channel(name) | await self._channel_state(name)
            )
        views = []
        for name in candidates:
            view = self._describe_channel(name)
            required = [f["path"] for f in view["fields"] if f.get("required")]
            views.append({"channel": f"channels.{name}", "login": view.get("login"), "required": required})
        return _dump(
            {
                "asked": asked,
                "means_one_of": views,
                "next": "pick by what the user said (a personal account or a company one); ask only if unclear",
            }
        )

    async def _instance_named(self, query: str) -> str:
        """A channel or sub-agent the query names, read as if its path had been given."""
        from raven.config.update_channels import channel_names

        key = "".join(ch for ch in query.lower() if ch.isalnum())
        if not key:
            return ""
        for name in channel_names():
            if key == name:
                return f"{query!r} is the channel channels.{name}:\n" + _dump(
                    self._describe_channel(name) | await self._channel_state(name)
                )
        if candidates := _CHANNEL_CANDIDATES.get(key):
            return await self._describe_candidates(query, candidates)
        raw = await asyncio.to_thread(surface.read_raw)
        providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
        for slug in providers:
            if key == "".join(ch for ch in str(slug).lower() if ch.isalnum()):
                lines = self._setting_lines(raw, list(surface.section_of("providers").settings), None, detail=False)  # type: ignore[union-attr]
                mine = [line for line in lines if f"providers.{slug}." in line]
                return (
                    f"{query!r} is the provider providers.{slug}:\n"
                    + "\n".join(mine)
                    + f"\n  providers.{slug}.catalog -- get it for the model ids {slug} serves (value filters)"
                )
        for row in await self._subagent_rows_or_none() or []:
            names = (row.get("name"), row.get("preset"))
            if any(key == "".join(ch for ch in str(n or "").lower() if ch.isalnum()) for n in names):
                return f"{query!r} is the sub-agent subagents.{row.get('name')}:\n" + _dump(self._subagent_view(row))
        return ""

    async def _channel_state(self, name: str) -> dict[str, Any]:
        """What the gateway says about a channel now: running, connected, waiting for a scan, missing fields."""
        if self._call is None:
            return {}
        try:
            status = await self._rpc("channels.status", {})
        except ValueError:
            return {}
        rows = status.get("channels") if isinstance(status, dict) else None
        row = next((r for r in rows or [] if isinstance(r, dict) and r.get("name") == name), None)
        if row is None:
            return {}
        out: dict[str, Any] = {}
        if row.get("missing"):
            out["missing"] = row["missing"]
        if not status.get("gateway_running"):
            out["state"] = (
                "no gateway is running, so no channel is connected: channels are served by Raven running as a "
                "gateway (`raven gateway`, or `raven web`, which starts one)"
            )
            return out
        scan = bool(row.get("qr_login")) or not any(f.get("required") for f in row.get("fields") or [])
        if row.get("connected"):
            out["state"] = "connected"
        elif row.get("running"):
            out["state"] = "running, not connected yet"
        elif row.get("enabled"):
            out["state"] = "enabled, not running (the gateway starts it within a few seconds, or it failed)"
        else:
            out["state"] = "off"
        if scan and not row.get("connected"):
            out["login"] = (
                f"{name} logs in by scanning a QR code: once it is on, the code appears in Settings > Channels > "
                f"{name}; ask the user to scan it there. describe channels.{name} again shows when it is connected."
            )
        return out

    async def _subagent_rows_or_none(self) -> list[dict[str, Any]] | None:
        if self._call is None:
            return None
        try:
            return await self._subagent_rows()
        except ValueError:
            return None

    async def _subagent_row(self, name: str) -> dict[str, Any]:
        rows = await self._subagent_rows()
        for row in rows:
            if str(row.get("name", "")).lower() == name.lower():
                return row
        for row in rows:
            if row.get("preset") and str(row["preset"]).lower() == name.lower():
                return row
        raise LookupError(f"no sub-agent named {name!r}; known: {[r.get('name') for r in rows]}")


__all__ = ["GUIDE_SKILL_ID", "READ_ACTIONS", "RavenConfigTool"]
