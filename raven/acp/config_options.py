"""The model selector, as the protocol's stable configuration surface.

``session/set_config_option`` is the channel, and the important thing about that
sentence is what it replaces: ``session/set_model`` does not exist in the stable
schema, and neither does ``models.availableModels``. Both appear in older
material and in some clients' expectations; an agent that waited for either would
never be asked to switch a model. The stable form is a generic option list where
one entry carries ``category: "model"``.

Two facts about raven shape what is offered here, and both are stated in the
option's own ``description`` rather than only in a document, because the schema
declares that field as text for the client to display:

* **The switch is process-wide.** ``config.set`` writes
  ``agents.defaults.model`` and reassigns the live loop's provider. There is no
  per-session model, so a connection with two sessions open changes both. The
  protocol's shape says otherwise and honesty about it belongs where a person
  will read it.
* **It is refused during a turn.** ``config.set`` guards on ``is_session_busy``
  and raises, rather than swapping the provider under a running request. That
  refusal is passed through with its own code instead of being flattened.

Only ``category: "model"`` is exposed. Raven has other hot-changeable config, but
a selector for each would put a settings panel in an editor's session menu, and
the ones worth exposing there are the ones a person changes mid-conversation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

MODEL_OPTION_ID = "model"

# Said in the option itself, not just in the compatibility matrix: the schema
# declares ``description`` as text for the client to display, and this is the
# caveat a person needs at the moment they pick.
MODEL_DESCRIPTION = (
    "The model this agent answers with. Raven has one model setting per installation "
    "rather than per session, so changing it here affects every session on this "
    "connection, and it cannot be changed while a turn is running."
)

# A dropdown built from every configured provider's catalogue. Past this the list
# stops being a menu; providers with hundreds of ids exist, and a client
# rendering all of them is a client nobody can pick from.
MAX_MODELS_PER_PROVIDER = 40

Call = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def model_option(call: Call) -> dict[str, Any] | None:
    """The ``SessionConfigOption`` for the model, or ``None`` if there is none.

    ``None`` rather than an empty selector when no provider is configured: an
    option whose list is empty is a dropdown a person can open and not choose
    from, which reads as a broken menu rather than as "set this up first".
    """
    try:
        options = await call("model.options", {})
    except Exception as exc:
        # A missing or failing model surface is not a reason to fail the
        # handshake or the session it was asked during.
        logger.debug("acp: the model catalogue is unavailable: {}", exc)
        return None

    current = _current_value(options)
    groups = _groups(options, current_provider=_provider_of(options))
    if current and not _contains(groups, current):
        # A dropdown whose current value is not among its options renders with
        # nothing selected. That happens for real reasons -- a model configured by
        # hand, one newer than the bundled catalogue -- so it is added rather than
        # hidden.
        groups.insert(0, {"group": "current", "name": "Current", "options": [{"value": current, "name": current}]})
    if not groups:
        # Nothing configured and nothing in use. Checked *after* the current value
        # is considered, because a working installation whose credentials come
        # from the environment reports no provider as "authenticated" -- returning
        # early on the group list alone hid the model that was actually running.
        return None
    return {
        "id": MODEL_OPTION_ID,
        "name": "Model",
        "description": MODEL_DESCRIPTION,
        "category": "model",
        "type": "select",
        "currentValue": current,
        "options": groups,
    }


async def set_model(call: Call, *, session_id: str, value: Any) -> None:
    """Apply a model selection, letting the runtime's own refusals through.

    ``session_id`` is passed so ``config.set`` can refuse a switch during that
    session's turn. Swapping the provider under a running request is the failure
    the guard exists for, and this layer must not route around it.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("the model value must be a non-empty string")
    await call("config.set", {"key": MODEL_OPTION_ID, "value": value, "session_id": session_id})


def _provider_of(options: Any) -> str:
    if not isinstance(options, dict):
        return ""
    provider = options.get("provider")
    return provider if isinstance(provider, str) else ""


def _current_value(options: Any) -> str:
    if not isinstance(options, dict):
        return ""
    model = options.get("model")
    provider = options.get("provider")
    if not isinstance(model, str) or not model:
        return ""
    return _qualified(provider, model)


def _groups(options: Any, *, current_provider: str = "") -> list[dict[str, Any]]:
    """One group per usable provider, each holding its models.

    "Usable" is ``authenticated`` **or** being the provider currently in use.
    The second half is not a courtesy: ``authenticated`` reports whether raven's
    own config holds a credential, and a working installation can be running on
    one from the environment -- measured, on a machine where every provider
    reported ``authenticated: false`` while ``anthropic/claude-opus-4-5`` was
    answering. Filtering on that flag alone hid every model that worked.

    A provider that is neither is left out: its ids would be selectable and every
    selection would fail on a missing credential, which is a dropdown that lies
    about what it can do.
    """
    if not isinstance(options, dict):
        return []
    groups: list[dict[str, Any]] = []
    for entry in options.get("providers") or ():
        if not isinstance(entry, dict):
            continue
        if not entry.get("authenticated") and entry.get("slug") != current_provider:
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        labels = entry.get("model_labels") if isinstance(entry.get("model_labels"), dict) else {}
        models = [m for m in (entry.get("models") or ()) if isinstance(m, str) and m][:MAX_MODELS_PER_PROVIDER]
        if not models:
            continue
        groups.append(
            {
                "group": slug,
                "name": entry.get("name") if isinstance(entry.get("name"), str) and entry.get("name") else slug,
                "options": [_option(slug, model, labels.get(model)) for model in models],
            }
        )
    return groups


def _qualified(provider: str, model: str) -> str:
    """A model id naming its provider, without naming it twice.

    Measured, not defensive: the catalogue's own ids are *already* qualified
    (``anthropic/claude-opus-5``), so prefixing unconditionally produced
    ``anthropic/anthropic/claude-opus-5`` -- a value ``config.set`` would refuse
    and a dropdown a person could not use. The fixtures used bare ids, so only the
    real catalogue showed it.

    Qualified because that is how every other surface stores a selection, so the
    value a client sends back is one ``config.set`` already understands.
    """
    if not provider or "/" in model:
        return model
    return f"{provider}/{model}"


def _option(slug: str, model: str, label: Any) -> dict[str, Any]:
    # The label is the visible name, and the bare tail is the fallback: showing
    # ``anthropic/claude-opus-5`` inside a group already headed "Anthropic" says
    # the same word twice.
    option: dict[str, Any] = {"value": _qualified(slug, model), "name": model.rpartition("/")[2] or model}
    if isinstance(label, str) and label and label != model:
        option["description"] = label
    return option


def _contains(groups: list[dict[str, Any]], value: str) -> bool:
    return any(option.get("value") == value for group in groups for option in group.get("options", ()))


__all__ = ["MAX_MODELS_PER_PROVIDER", "MODEL_DESCRIPTION", "MODEL_OPTION_ID", "model_option", "set_model"]
