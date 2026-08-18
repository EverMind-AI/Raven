"""``config.get`` / ``config.set`` RPC handlers (specs §3.6).

Contract source: ``docs/openspec/changes/tui-ipc-bridge/specs/tui-ipc.md §3.6``.

The v0.1 surface exposes only **four hot-changeable** keys; any other write
target raises :class:`ConfigFieldReadonlyError` (-32010). Values are stored
in ``~/.raven/config.json`` using dotted-path nesting (``tui.theme`` →
``{"tui": {"theme": "..."}}``) so that the same file is loadable by the legacy
``raven.config.raven_loader`` without any schema gymnastics.

Validation
----------

Per-key validators reject:

* ``agent.thinking_budget``: must be a non-negative integer.
* ``agent.temperature``: must be a number (int/float) in the closed range
  ``[0.0, 2.0]``.
* ``tui.theme``: must be a non-empty string matching ``[A-Za-z0-9_-]+``.
* ``tui.show_token_usage``: must be a boolean.

Anything else → :class:`ConfigValidationError` (-32011).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.cli._helpers import load_runtime_config, make_provider
from raven.providers import pin
from raven.providers.auth import MissingCredentialsError
from raven.providers.wire import stored_model_id
from raven.tui_rpc.errors import (
    ConfigFieldReadonlyError,
    ConfigValidationError,
    ModelNotAvailableError,
)

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods.session import AgentLoopFactory


_CONFIG_DIR_NAME = ".raven"
_CONFIG_FILENAME = "config.json"

# Default values returned by config.get when the on-disk config omits the key.
_DEFAULTS: dict[str, Any] = {
    "agent.thinking_budget": 0,
    "agent.temperature": 1.0,
    "tui.theme": "default",
    "tui.show_token_usage": True,
}


# ---------------------------------------------------------------------------
# Per-key validators
# ---------------------------------------------------------------------------


_THEME_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_thinking_budget(value: Any) -> int:
    # Booleans are a subclass of int — reject them explicitly so True doesn't
    # silently coerce to 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValidationError(
            "agent.thinking_budget must be a non-negative integer",
            data={"field": "agent.thinking_budget", "got": repr(value)},
        )
    if value < 0:
        raise ConfigValidationError(
            "agent.thinking_budget must be non-negative",
            data={"field": "agent.thinking_budget", "value": value},
        )
    return value


def _validate_temperature(value: Any) -> float:
    if isinstance(value, bool):  # bool is a subclass of int — reject upfront
        raise ConfigValidationError(
            "agent.temperature must be a number in [0, 2]",
            data={"field": "agent.temperature", "got": repr(value)},
        )
    if not isinstance(value, (int, float)):
        raise ConfigValidationError(
            "agent.temperature must be a number in [0, 2]",
            data={"field": "agent.temperature", "got": repr(value)},
        )
    if not (0.0 <= float(value) <= 2.0):
        raise ConfigValidationError(
            "agent.temperature out of range [0, 2]",
            data={"field": "agent.temperature", "value": value},
        )
    return float(value)


def _validate_theme(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigValidationError(
            "tui.theme must be a non-empty string",
            data={"field": "tui.theme", "got": repr(value)},
        )
    if not _THEME_NAME_RE.match(value):
        raise ConfigValidationError(
            "tui.theme must match [A-Za-z0-9_-]+",
            data={"field": "tui.theme", "value": value},
        )
    return value


def _validate_show_token_usage(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigValidationError(
            "tui.show_token_usage must be a boolean",
            data={"field": "tui.show_token_usage", "got": repr(value)},
        )
    return value


_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "agent.thinking_budget": _validate_thinking_budget,
    "agent.temperature": _validate_temperature,
    "tui.theme": _validate_theme,
    "tui.show_token_usage": _validate_show_token_usage,
}

# Public: the canonical writable-key set; consumers can iterate to enumerate
# defaults without mutating ``_DEFAULTS`` directly.
CONFIG_WRITABLE_KEYS: tuple[str, ...] = tuple(_VALIDATORS.keys())


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    return Path.home() / _CONFIG_DIR_NAME / _CONFIG_FILENAME


def _load_config() -> dict[str, Any]:
    """Load ``config.json`` for a read-modify-write (get/set/_set_model).

    Absent / empty -> ``{}`` (safe to create fresh). A present-but-unparseable
    file raises ConfigValidationError rather than the old empty-dict fallback:
    returning ``{}`` here and then ``_save_config`` would overwrite the user's
    whole config with just the changed key (data loss). The on-disk file is the
    source of truth; downstream loaders read the same file independently.
    """
    from raven.config.loader import ConfigReadError, read_raw_or_raise

    try:
        return read_raw_or_raise(_config_path())
    except ConfigReadError as exc:
        raise ConfigValidationError(str(exc)) from exc


def _save_config(payload: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _get_nested(payload: dict[str, Any], dotted_key: str) -> Any | None:
    """Return the value at the dotted path, or None if absent."""
    parts = dotted_key.split(".")
    cur: Any = payload
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_nested(payload: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    cur: dict[str, Any] = payload
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def config_get(params: dict) -> dict:
    """Return values for whitelisted keys.

    Spec §3.6: unknown keys are silently omitted (NOT an error).
    """
    requested_raw = params.get("keys") if isinstance(params, dict) else None
    if requested_raw is None:
        requested: list[str] = list(CONFIG_WRITABLE_KEYS)
    else:
        if not isinstance(requested_raw, list) or not all(isinstance(k, str) for k in requested_raw):
            raise ConfigValidationError(
                "config.get params.keys must be a list[str] if provided",
                data={"field": "keys", "got": repr(requested_raw)},
            )
        requested = requested_raw

    payload = _load_config()
    out: dict[str, Any] = {}
    for key in requested:
        if key not in _VALIDATORS:
            # Unknown / non-whitelisted key — silently omit per spec.
            continue
        value = _get_nested(payload, key)
        out[key] = value if value is not None else _DEFAULTS[key]
    return {"config": out}


async def config_set(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """Write a single whitelisted key. Returns ``{applied, previous}``.

    The special key ``"model"`` switches the live agent loop's provider/model
    (returns ``{applied, previous, value}``); see :func:`_set_model`.

    Raises:
        ConfigValidationError (-32011): params shape or value invalid.
        ConfigFieldReadonlyError (-32010): key not in writable whitelist.
    """
    if not isinstance(params, dict):
        raise ConfigValidationError(
            "config.set params must be an object",
            data={"got": type(params).__name__},
        )

    key = params.get("key")
    if not isinstance(key, str) or not key:
        raise ConfigValidationError(
            "config.set params.key is required and must be a non-empty string",
            data={"field": "key", "got": repr(key)},
        )
    if "value" not in params:
        raise ConfigValidationError(
            "config.set params.value is required",
            data={"field": "value"},
        )
    raw_value = params["value"]

    if key == "model":
        return _set_model(params, raw_value, agent_loop_factory)

    if key not in _VALIDATORS:
        raise ConfigFieldReadonlyError(
            f"key '{key}' is not in the v0.1 hot-changeable whitelist",
            data={"field": key, "writable": list(CONFIG_WRITABLE_KEYS)},
        )

    validated = _VALIDATORS[key](raw_value)

    payload = _load_config()
    previous = _get_nested(payload, key)
    _set_nested(payload, key, validated)
    _save_config(payload)

    return {"applied": True, "previous": previous}


def _set_model(
    params: dict,
    raw_value: Any,
    agent_loop_factory: "AgentLoopFactory | None",
) -> dict:
    """Switch the model this session runs on, or the default new ones start on.

    Two scopes, because they answer different questions. With a
    ``session_id`` (what the picker sends) the switch is scoped to that
    session: no other session moves, and ``agents.defaults`` is left alone so
    a new session still starts on the configured default. Pass
    ``scope="default"``, or omit ``session_id``, to change that default
    instead; sessions that already switched keep their own model.

    Either way the provider is built before anything is persisted or applied,
    so a rebuild failure aborts with the on-disk model untouched.

    A switch during a turn is not refused. The running turn holds the binding
    it started on for its whole tree, so the new model takes effect on the
    session's next turn -- which is what a user asking mid-answer means.
    """
    if not isinstance(raw_value, str) or not raw_value:
        raise ConfigValidationError(
            "config.set model value must be a non-empty string",
            data={"field": "value", "got": repr(raw_value)},
        )
    new_provider = params.get("provider")
    if new_provider is not None and not isinstance(new_provider, str):
        raise ConfigValidationError(
            "config.set model provider must be a string",
            data={"field": "provider", "got": repr(new_provider)},
        )
    # Bare `/model <name>` carries no provider; derive it from the model so a
    # previously-forced provider does not silently mis-route the new model. The
    # picker always sends one, so this is the hand-typed path. The rule itself is
    # `providers.pin`, which `raven provider use` asks too.
    if new_provider is None:
        new_provider = pin.resolve(raw_value, pinned=_get_nested(_load_config(), "agents.defaults.provider") or "")
        if new_provider is None:
            raise ConfigValidationError(
                f"cannot tell which provider serves {raw_value!r}; qualify it as <provider>/{raw_value}",
                data={"field": "value", "got": raw_value},
            )

    # Stored the way every other surface stores it -- naming its provider -- so
    # the three cannot disagree about what was chosen. A hand-typed bare id used
    # to be written raw here while the wizard qualified the same input, which is
    # the spelling drift the storage rule exists to end. `auto` names nobody,
    # so there is no prefix to add.
    if new_provider and new_provider != pin.AUTO:
        raw_value = stored_model_id(new_provider, raw_value)

    session_id = params.get("session_id")
    scope = params.get("scope")
    if scope not in (None, "session", "default"):
        raise ConfigValidationError(
            "config.set model scope must be 'session' or 'default'",
            data={"field": "scope", "got": repr(scope)},
        )
    has_session = isinstance(session_id, str) and bool(session_id)
    if scope == "session" and not has_session:
        # Never widen a scope the caller narrowed: falling through to the
        # default branch here would write agents.defaults and move every
        # session that never switched. The TUI sends a session_id that is null
        # until the first session.create resolves, so this is reachable.
        raise ConfigValidationError(
            "config.set model scope 'session' needs a session_id",
            data={"field": "session_id", "got": repr(session_id)},
        )
    session_scoped = scope != "default" and has_session

    loop = agent_loop_factory() if agent_loop_factory is not None else None
    binding = None
    if loop is not None:
        runtime = load_runtime_config(None, None)
        runtime.agents.defaults.model = raw_value
        if new_provider is not None:
            runtime.agents.defaults.provider = new_provider
        try:
            binding = _build_binding(loop, runtime, raw_value, new_provider)
        except MissingCredentialsError as exc:
            # Carried through as the sentence the user needs. `typer.Exit`
            # subclasses RuntimeError, so this used to land in the branch below
            # and `str(exc)` was the exit code -- the picker said
            # `cannot build provider ... error: "1"`.
            raise ModelNotAvailableError(
                exc.summary,
                data={"model": raw_value, "provider": exc.provider, "remedy": exc.remedy},
            ) from exc
        except (SystemExit, RuntimeError, ValueError) as exc:
            raise ModelNotAvailableError(
                f"cannot build provider for model {raw_value!r}",
                data={"model": raw_value, "error": str(exc)},
            ) from exc

    if session_scoped:
        if loop is None:
            # Nothing was built, so nothing was validated -- do not report a
            # switch that did not happen.
            return {"applied": False, "previous": None, "value": raw_value, "scope": "session"}
        previous = loop.session_model(session_id)
        loop.set_session_binding(session_id, binding)
        _remember_session_model(loop, session_id, raw_value, new_provider)
        return {
            "applied": True,
            "previous": previous,
            "value": raw_value,
            "scope": "session",
            "session_id": session_id,
            "applies_to_session": True,
        }

    # A default-scoped switch still moves the asking conversation when that
    # conversation never chose a model of its own, because it reads the
    # default. Answered here rather than inferred from the scope: the client
    # cannot see which sessions have their own binding.
    follows_default = None
    if loop is not None and has_session:
        has_own = getattr(loop, "has_session_binding", None)
        if callable(has_own):
            follows_default = not has_own(session_id)

    payload = _load_config()
    previous = _get_nested(payload, "agents.defaults.model")
    _set_nested(payload, "agents.defaults.model", raw_value)
    if new_provider is not None:
        _set_nested(payload, "agents.defaults.provider", new_provider)
    _save_config(payload)

    if loop is not None:
        # Not a two-attribute assignment: the subagent manager, the context
        # engine and the consolidator each hold a fallback for work that runs
        # outside a turn, and this is what re-points them.
        loop.set_default_binding(binding)

    return {
        "applied": True,
        "previous": previous,
        "value": raw_value,
        "scope": "default",
        "applies_to_session": follows_default,
    }


def _remember_session_model(loop: Any, session_key: str, model: str, provider_name: str | None) -> None:
    """Persist the choice on the session, so a restart does not undo it.

    Stored on the session record rather than in ``agents.defaults``: it is
    this conversation's model, and a new conversation must still start on the
    configured default.

    Written in memory unconditionally, saved only for a session that already
    has a file. ``session.create`` is lazy -- it mints a key and writes
    nothing until the session's first real save -- so saving here would
    manufacture a record with zero messages for anyone who runs ``/model``
    before saying anything, and ``/sessions list`` would grow an untitled row
    per switch. The choice still reaches disk: it rides the session's first
    real save. ``session.title`` guards the identical case the same way.
    """
    sessions = getattr(loop, "sessions", None)
    if sessions is None:
        return
    try:
        session = sessions.get_or_create(session_key)
        session.metadata["model"] = model
        if provider_name:
            session.metadata["provider"] = provider_name
        if sessions.exists(session_key):
            sessions.save(session)
    except Exception:
        logger.warning("could not persist the model on session {!r}", session_key)


def _build_binding(loop: Any, runtime: Any, model: str, provider_name: str | None) -> Any:
    """One provider per (vendor, model), reused across sessions and switches.

    Building one imports LiteLLM and writes vendor env vars, so a session
    flipping between two models must not pay for it twice. The pool is the
    loop's; without one (an older wiring, a test) fall back to building
    directly.
    """
    from raven.providers.binding import ModelBinding

    pool = getattr(loop, "provider_pool", None)
    if pool is not None:
        return pool.bind(model, provider_name)
    return ModelBinding(make_provider(runtime), model)


def register_config_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register ``config.get`` / ``config.set`` on a dispatcher instance."""

    async def _set(params: dict) -> dict:
        return await config_set(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("config.get", config_get)
    dispatcher.register("config.set", _set)


__all__ = [
    "config_get",
    "config_set",
    "register_config_methods",
    "CONFIG_WRITABLE_KEYS",
]
