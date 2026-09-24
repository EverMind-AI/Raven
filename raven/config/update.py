"""Minimal in-place updates for ~/.raven/config.json.

Unlike ``save_config`` which re-serializes the entire Pydantic model (and
would bake every runtime default back into the file), these helpers read
the raw JSON, patch a small set of fields, and rewrite through the locked
read-modify-write transaction in ``raven.utils.atomic_io``. Used by
``raven cron config set`` and the onboarding wizard so the change persists
across restarts without touching unrelated fields.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic.alias_generators import to_camel

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.config.schema import CronConfig
from raven.utils.atomic_io import atomic_update

# Shared Skill Hub endpoint seeded into a fresh config's skillForge.router.hub.
# Kept here (not as the HubSourceConfig schema default) so non-onboard /
# programmatic loads stay Hub-disabled until a config opts in, while an
# onboarded config shows the live endpoint. apiKey is NOT seeded — the user
# supplies their own Bearer token.
_DEFAULT_SKILL_HUB_ENDPOINT = "https://skillhub.evermind.ai"


def update_cron_config(
    key: str,
    value: Any,
    *,
    config_path: Path | None = None,
) -> Any:
    """Patch a single CronConfig field on-disk.

    Returns the previous raw value (None if absent). Raises ``KeyError`` if
    ``key`` is not a CronConfig field — defensive only; CLI ``_KEY_HANDLERS``
    already validates before reaching here. Type validation of ``value`` is
    the caller's responsibility (CLI parsers handle it).
    """
    if key not in CronConfig.model_fields:
        raise KeyError(f"Unknown cron config key: {key!r}. Supported: {sorted(CronConfig.model_fields)}")
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        cron_section = data.setdefault("cron", {})
        camel_key = to_camel(key)
        prev = cron_section.get(camel_key)
        cron_section[camel_key] = value
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: cron.{} set to {!r} (was {!r})", key, value, prev)
    return prev


def allow_exec_pattern(pattern: str, *, config_path: Path | None = None) -> bool:
    """Add one ``permissions.tools.exec`` allow rule on disk.

    Returns False when the pattern was already there. Raises ``ValueError``
    when ``exec`` is set to a plain tier string rather than a table: that is
    the user's own setting, a prompt does not overwrite it, and the caller has
    to know nothing was written.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, bool]:
        data = read_raw_or_raise(path)
        tools = data.setdefault("permissions", {}).setdefault("tools", {})
        table = tools.setdefault("exec", {})
        if not isinstance(table, dict):
            raise ValueError(f"permissions.tools.exec is {table!r}, a tier for the whole tool, not a table of patterns")
        added = table.get(pattern) != "allow"
        table[pattern] = "allow"
        return json.dumps(data, indent=2, ensure_ascii=False), added

    added = atomic_update(path, _apply)
    if added:
        logger.info("config/update: permissions.tools.exec[{!r}] = allow", pattern)
    return added


def remove_exec_pattern(pattern: str, *, config_path: Path | None = None) -> bool:
    """Take back one ``permissions.tools.exec`` allow rule; False when none was there.

    Only an ``allow`` entry goes: a ``deny`` or ``ask`` under the same pattern is
    the user's own rule, not one a prompt wrote, and a prompt does not undo it.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, bool]:
        data = read_raw_or_raise(path)
        table = data.get("permissions", {}).get("tools", {}).get("exec")
        removed = isinstance(table, dict) and table.get(pattern) == "allow"
        if removed:
            del table[pattern]
        return json.dumps(data, indent=2, ensure_ascii=False), removed

    removed = atomic_update(path, _apply)
    if removed:
        logger.info("config/update: permissions.tools.exec[{!r}] removed", pattern)
    return removed


def reset_cron_config(*, config_path: Path | None = None) -> None:
    """Remove the entire ``cron`` section from on-disk config.

    Schema defaults (``default_timezone="Asia/Shanghai"``) take effect on
    next load. Stays consistent with the file's "never bake defaults to
    disk" principle.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        removed = data.pop("cron", None)
        return json.dumps(data, indent=2, ensure_ascii=False), removed

    removed = atomic_update(path, _apply)
    logger.info("config/update: cron section reset (was {!r})", removed)


def set_sentinel_enabled(
    enabled: bool,
    *,
    config_path: Path | None = None,
) -> bool | None:
    """Patch ``sentinel.enabled`` on the on-disk config. Returns the previous
    raw value (None if absent).

    The Sentinel master switch is read once at process start
    (``build_sentinel_stack`` skips building the runner entirely when it is
    False), so this change takes effect on the next agent/gateway start, not
    on a running process.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str | None, tuple[Any, bool]]:
        data = read_raw_or_raise(path)
        section = data.setdefault("sentinel", {})
        prev = section.get("enabled")
        # No-op when already in the desired state (absent defaults to False) —
        # don't rewrite the file just to set the same value.
        if bool(prev) == enabled:
            return None, (prev, False)
        section["enabled"] = enabled
        return json.dumps(data, indent=2, ensure_ascii=False), (prev, True)

    prev, wrote = atomic_update(path, _apply)
    if wrote:
        logger.info("config/update: sentinel.enabled set to {!r} (was {!r})", enabled, prev)
    return prev


def set_sentinel_nudge_quota(
    *,
    per_hour: int | None = None,
    per_day: int | None = None,
    config_path: Path | None = None,
) -> dict[str, tuple[Any, int]]:
    """Patch ``sentinel.nudge_policy`` per-hour / per-day nudge quotas on-disk.

    Returns ``{field: (prev, new)}`` for each field changed. Effective on the
    next NudgePolicy load (agent/gateway start). Respects whichever key casing
    (camelCase / snake_case) the file already uses — the loader accepts both,
    but writing a second casing for a field already present would duplicate it.
    """
    if per_hour is None and per_day is None:
        raise ValueError("specify at least one of per_hour / per_day")
    for label, val in (("per_hour", per_hour), ("per_day", per_day)):
        if val is not None and val < 1:
            raise ValueError(f"{label} must be >= 1 (got {val})")

    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str | None, dict[str, tuple[Any, int]]]:
        data = read_raw_or_raise(path)
        sentinel = data.setdefault("sentinel", {})
        np_key = "nudge_policy" if "nudge_policy" in sentinel else "nudgePolicy"
        np = sentinel.setdefault(np_key, {})
        snake_block = np_key == "nudge_policy"

        def _patch(camel: str, snake: str, value: int, changed: dict) -> None:
            # Reuse an existing key as-is; for a new field follow the block's
            # casing convention so we never mix snake + camel within one block.
            if snake in np:
                key = snake
            elif camel in np:
                key = camel
            else:
                key = snake if snake_block else camel
            prev = np.get(key)
            if prev == value:
                return  # already at the target — leave it out of `changed`
            np[key] = value
            changed[snake] = (prev, value)

        changed: dict[str, tuple[Any, int]] = {}
        if per_hour is not None:
            _patch("maxNudgesPerHour", "max_nudges_per_hour", per_hour, changed)
        if per_day is not None:
            _patch("maxNudgesPerDay", "max_nudges_per_day", per_day, changed)

        # Only touch the file when something actually changed.
        if not changed:
            return None, changed
        return json.dumps(data, indent=2, ensure_ascii=False), changed

    changed = atomic_update(path, _apply)
    if changed:
        logger.info("config/update: sentinel nudge quota patched {!r}", changed)
    return changed


def set_skill_blocked(
    name: str,
    blocked: bool,
    *,
    config_path: Path | None = None,
) -> list[str]:
    """Add/remove a skill name on ``skillForge.blocklist``; returns the new
    list. Matching is case-insensitive; adding an already-listed name or
    removing an absent one is a no-op (the file is still not rewritten).

    The blocklist is read at process start (AgentLoop / context engine
    construction), so a change takes effect on the next agent/gateway
    start, not on a running process.

    Respects whichever key casing (camelCase / snake_case) the file already
    uses for the block itself: the loader accepts both spellings, but writing
    the second one alongside the first leaves the config unloadable.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str | None, tuple[list[str], bool]]:
        data = read_raw_or_raise(path)
        # Reuse whichever spelling the file already carries. The loader accepts
        # both, but a second block under the other one is an extra input to a
        # model that forbids extras: the whole config then stops loading, and
        # the blocklist we just read came from the empty block we created.
        if isinstance(data.get("skillForge"), dict):
            sf_key = "skillForge"
        elif isinstance(data.get("skill_forge"), dict):
            sf_key = "skill_forge"
        else:
            sf_key = "skillForge"
        section = data.setdefault(sf_key, {})
        current = [str(x) for x in (section.get("blocklist") or [])]
        lowered = {x.casefold() for x in current}
        if blocked:
            if name.casefold() in lowered:
                return None, (current, False)
            current.append(name)
        else:
            if name.casefold() not in lowered:
                return None, (current, False)
            current = [x for x in current if x.casefold() != name.casefold()]
        section["blocklist"] = current
        return json.dumps(data, indent=2, ensure_ascii=False), (current, True)

    current, wrote = atomic_update(path, _apply)
    if wrote:
        logger.info(
            "config/update: skill blocklist now {!r} ({} {!r})",
            current,
            "blocked" if blocked else "unblocked",
            name,
        )
    return current


def set_language(
    language: str,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Patch the top-level ``language`` on the on-disk config. Returns previous value.

    Set by the onboarding wizard's language screen. Read by the CLI/wizard copy
    (via ``_t``) and injected into the agent's system prompt so replies use the
    chosen language.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        prev = data.get("language")
        data["language"] = language
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: language set to {!r} (was {!r})", language, prev)
    return prev


def set_default_model(
    model: str,
    *,
    provider: str | None = None,
    config_path: Path | None = None,
) -> str | None:
    """Patch ``agents.defaults.model`` on the on-disk config. Returns previous value.

    Used by the onboarding wizard after the user picks a provider: the wizard
    needs to swap the default model to one that matches the chosen provider
    (otherwise ``raven agent`` would still route to whatever the freshly
    created ``Config()`` baked in, which is typically a different vendor).

    ``provider`` writes ``agents.defaults.provider`` in the same patch. That field
    overrides what a model id says, so leaving it behind lets a stale configured provider route
    the new model to the old vendor -- with the old vendor's key -- while the
    write that was just reported as successful changes nothing. Callers that do
    not know which provider serves the model pass None and leave it alone.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        defaults = data.setdefault("agents", {}).setdefault("defaults", {})
        prev = defaults.get("model")
        defaults["model"] = model
        if provider is not None:
            defaults["provider"] = provider
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: default model set to {} (was {}), provider={}", model, prev, provider)
    return prev


def set_sandbox_backend(
    backend: str,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Patch ``sandbox.backend`` on the on-disk config. Returns previous value.

    Used by the onboarding wizard's run-location step. ``backend`` must be one
    of ``SandboxConfig``'s literal values (``none`` / ``auto`` / ``boxlite``);
    the loader validates on next read.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        # sandbox lives under tools (Config.tools.sandbox), not at the root — the
        # root Config forbids extras, so a top-level "sandbox" key fails schema
        # validation on the next load.
        section = data.setdefault("tools", {}).setdefault("sandbox", {})
        prev = section.get("backend")
        section["backend"] = backend
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: tools.sandbox.backend set to {!r} (was {!r})", backend, prev)
    return prev


def set_plugin_config_fields(
    plugin_id: str,
    fields: dict[str, Any],
    *,
    remove: tuple[str, ...] | None = None,
    config_path: Path | None = None,
) -> None:
    """Merge ``fields`` into ``plugins.config[plugin_id]`` on the on-disk config.

    A merge rather than a replace: the slice holds several independent decisions
    (which EverOS root, whether raven owns it, its cached address) written at
    different moments, and a replacing write would drop whichever the caller did
    not happen to be carrying.

    ``remove`` names keys that no longer apply, for the case a merge cannot
    express. Switching to an EverOS the user runs has to retract the recorded
    root, not merely stop updating it: left behind, it is still exported as
    ``EVEROS_ROOT`` and still points raven at a directory it has just promised
    to leave alone.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, None]:
        data = read_raw_or_raise(path)
        slice_ = data.setdefault("plugins", {}).setdefault("config", {}).setdefault(plugin_id, {})
        slice_.update(fields)
        for key in remove or ():
            slice_.pop(key, None)
        return json.dumps(data, indent=2, ensure_ascii=False), None

    atomic_update(path, _apply)
    logger.info(
        "config/update: plugins.config.{} updated ({}{})",
        plugin_id,
        ", ".join(fields),
        f"; removed {', '.join(remove)}" if remove else "",
    )


def set_playbook_disabled(
    name: str,
    disabled: bool,
    *,
    config_path: Path | None = None,
) -> bool:
    """Add/remove one playbook name on the ``playbooks.disabled`` deny list.

    Returns True when the file changed (False = already in the desired
    state). The list is the only per-machine playbook state: playbook.md is
    the distribution unit and carries no switch, so disable adds the name
    here and enable removes it — for builtin and user playbooks alike. The
    runtime reads the list on every model call (``config.live``), so a change
    applies to the next one rather than to the next process.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str | None, bool]:
        data = read_raw_or_raise(path)
        section = data.setdefault("playbooks", {})
        deny = list(section.get("disabled") or [])
        if disabled == (name in deny):
            return None, False
        section["disabled"] = sorted(set(deny) | {name}) if disabled else [n for n in deny if n != name]
        return json.dumps(data, indent=2, ensure_ascii=False), True

    wrote = atomic_update(path, _apply)
    if wrote:
        logger.info("config/update: playbooks.disabled {} {!r}", "added" if disabled else "removed", name)
    return wrote


def set_memory_backend(
    backend: str | None,
    *,
    config_path: Path | None = None,
) -> str | None:
    """Patch ``memory.backend`` on the on-disk config. Returns previous value.

    The name is a ``memory_backends`` contribution name from an activated
    plugin (e.g. ``"everos"``); ``None`` disables backend-driven memory --
    recall and storage simply stop happening. The onboarding wizard's memory
    step writes this from the plugin step's ``StepOutcome``: ``CONFIGURED``
    records the contribution's own name, anything else clears it to ``None``.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        section = data.setdefault("memory", {})
        prev = section.get("backend")
        section["backend"] = backend
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: memory.backend set to {!r} (was {!r})", backend, prev)
    return prev


class EmbeddingPinError(ValueError):
    """The pair asked for cannot embed anything, so it is not written.

    Refused here rather than at the first index: a base built against a pin
    that cannot answer fails with a stack of retries and a message about
    vectors, while the thing to fix is two fields on a settings page.
    """


REQUIRED_EMBEDDING_DIMENSIONS = 1024
"""The width of every vector column in the memory index.

EverOS truncates a wider vector to this on the way in, so a model that returns
more is fine. One that returns fewer cannot fill the column: every store and
every search then answers 500 about a mismatched width, and nothing on the
settings page says why.
"""


def probe_embedding_dimensions(url: str, headers: dict[str, str], model: str) -> int | str:
    """The width ``model`` answers with at ``url``, or why it could not be asked.

    Asks with ``dimensions=REQUIRED_EMBEDDING_DIMENSIONS`` first -- the API-level
    truncation MRL-capable models honour -- and falls back to the native width.
    An int is a measured width; a str is a transport or shape failure, which is
    not a verdict about the model.
    """
    import httpx

    def _try(client: httpx.Client, body: dict[str, Any]) -> int | str:
        try:
            resp = client.post(url, json=body, headers=headers)
            if resp.status_code != 200:
                return f"HTTP {resp.status_code}"
            items = resp.json().get("data", [])
            if not items:
                return "empty response"
            first = items[0]
            if not isinstance(first, dict):
                return "unexpected response format"
            return len(first.get("embedding", []))
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            return str(exc)

    with httpx.Client(timeout=15) as client:
        result = _try(
            client, {"model": model, "input": ["dimension check"], "dimensions": REQUIRED_EMBEDDING_DIMENSIONS}
        )
        if result == REQUIRED_EMBEDDING_DIMENSIONS:
            return result
        return _try(client, {"model": model, "input": ["dimension check"]})


def set_embedding_endpoint(
    fields: dict[str, Any],
    *,
    config_path: "Path | None" = None,
) -> dict[str, Any]:
    """Merge ``model`` / ``provider`` / ``dimensions`` into ``embedding``.

    One endpoint for everything that embeds -- a knowledge base and the memory
    backend both read it, and two would mean two vector spaces that cannot be
    compared. The pair names what to call and who serves it; the address and
    key stay with the provider, so this block never holds a secret and
    rotating a key is one edit somewhere else.

    Raises :class:`EmbeddingPinError` when the pair cannot embed: an unknown
    provider, one with no usable credential, a model that provider's catalogue
    describes as something other than an embedding model, or one whose vectors
    are narrower than the memory index.

    Returns the previous block, so a caller can say what changed -- including
    that the model moved, which invalidates every vector stored under the old
    one.
    """
    path = config_path or get_config_path()
    allowed = {"model", "provider", "dimensions"}
    given = {k: v for k, v in fields.items() if k in allowed}
    clean = {k: v for k, v in given.items() if v not in (None, "")}
    # A pin cleared rather than changed. The page's "inherit" option sends both
    # halves empty, and dropping empties before the write made that a no-op the
    # caller was told had applied -- the picker snapped back to the old pair on
    # the next load, with no way to unset it but editing the file.
    if given and not clean:
        return _clear_embedding_pin(path)
    if not clean:
        return {}

    # Against the block this would leave behind, not against the fields handed
    # in: a run that changes only the model is still pinned to the provider
    # already recorded, and judging the fields alone refused it.
    stored = {}
    try:
        stored = dict(read_raw_or_raise(path).get("embedding") or {})
    except Exception:  # noqa: BLE001 - an unreadable config raises from atomic_update below
        pass
    _refuse_a_pin_that_cannot_embed({**stored, **clean}, path=path)

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        section = data.setdefault("embedding", {})
        prev = dict(section)
        # The retired shape carried the address and the key. Left behind they
        # would read as current, and `Config` forbids them now.
        for retired in ("baseUrl", "base_url", "apiKey", "api_key"):
            section.pop(retired, None)
        section.update(clean)
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: embedding endpoint set ({})", ", ".join(sorted(clean)))
    return prev or {}


def _clear_embedding_pin(path: "Path") -> dict[str, Any]:
    """Remove the block, and answer with what it held."""

    def _apply(_text: str | None) -> tuple[str, Any]:
        data = read_raw_or_raise(path)
        prev = dict(data.pop("embedding", None) or {})
        return json.dumps(data, indent=2, ensure_ascii=False), prev

    prev = atomic_update(path, _apply)
    logger.info("config/update: embedding endpoint cleared")
    return prev or {}


def _refuse_a_pin_that_cannot_embed(clean: dict[str, Any], *, path: "Path") -> None:
    """Check the pair before it is stored. Raises, or returns quietly.

    First what can be answered without a network call: whether the provider is
    one this install has, whether it has a credential to call with, and whether
    its catalogue says the model embeds. A model the catalogue has never heard
    of passes -- a local deployment or a release newer than the snapshot is not
    a mistake, and refusing it would make this a gate on the snapshot's age.

    Then one request, for the one fact only the model can answer: how wide its
    vectors are. Narrower than the index is refused; a probe that could not
    reach the provider is not a verdict, so the pin is written and the width
    left to the first real call.
    """
    provider = str(clean.get("provider") or "")
    model = str(clean.get("model") or "")
    # Both halves or neither. A model with nobody to serve it reads as
    # configured on every screen while every reader resolves it to nothing --
    # which is how a wizard came to report semantic memory over a service
    # running on keywords alone. A provider with nothing to run is the same
    # write from the other end: accepted, reported as saved, and storing
    # nothing anyone can use.
    if not provider:
        if model:
            raise EmbeddingPinError("an embedding model needs the provider that serves it")
        return
    if not model:
        raise EmbeddingPinError("an embedding provider needs the model to run on it")

    from raven.config.update_providers import resolve_provider_credentials

    try:
        resolved = resolve_provider_credentials(provider, config_path=path)
    except KeyError as exc:
        raise EmbeddingPinError(f"no such provider: {provider}") from exc
    if resolved is None:
        raise EmbeddingPinError(f"provider {provider!r} has no usable credential, so nothing could be embedded with it")

    from raven.providers import catalog

    row = catalog.describe(provider, model)
    caps = tuple(getattr(row, "capabilities", ()) or ())
    if caps and "embedding" not in caps:
        raise EmbeddingPinError(
            f"{model!r} is not an embedding model on {provider} (it is described as "
            f"{', '.join(caps)}); pick one that returns vectors"
        )

    from raven.providers.wire import wire_model

    base_url, api_key = resolved
    width = probe_embedding_dimensions(
        base_url.rstrip("/") + "/embeddings",
        {"Authorization": f"Bearer {api_key}"} if api_key else {},
        wire_model(model, client_provider=provider),
    )
    if isinstance(width, int) and width < REQUIRED_EMBEDDING_DIMENSIONS:
        raise EmbeddingPinError(
            f"{model!r} on {provider} returns {width}-dimension vectors and the memory index is "
            f"{REQUIRED_EMBEDDING_DIMENSIONS} wide; pick a model that can return {REQUIRED_EMBEDDING_DIMENSIONS}"
        )
    if not isinstance(width, int):
        logger.warning(
            "config/update: could not measure the width {!r} returns on {} ({}); pin written unchecked",
            model,
            provider,
            width,
        )


_EMBEDDING_MODEL_CHANGED = (
    "Embedding model changed from {was} to {now}. Anything already indexed was built with the "
    "old one and cannot be searched with the new one: rebuild each knowledge base, and re-index "
    "whatever the memory backend has stored."
)
"""The one wording, as a catalogue key. Surfaces ask for it rather than rewording."""


def embedding_model_change(previous: dict[str, Any], fields: dict[str, Any]) -> str:
    """One sentence when the model moved, empty when it did not.

    Everything already embedded answers to the old model, and a query embedded
    with the new one lands somewhere unrelated in the same space. The stores
    each notice on their own -- a knowledge base refuses, the memory index
    degrades -- so this only has to say it once, where the change is made.

    Names no backend and no command of one. Which memory backend is installed
    is not this module's business, and the host printing a particular one's
    command is the coupling the seam exists to remove.
    """
    was = str(previous.get("model") or "")
    now = str(fields.get("model") or "")
    if not was or not now or was == now:
        return ""
    from raven.i18n import t

    # Through the catalogue, like every other sentence a person reads. Built by
    # interpolating the two names into a finished string instead, it was the one
    # English paragraph on an otherwise translated screen.
    return t(_EMBEDDING_MODEL_CHANGED, was=was, now=now)


def initialize_a2a_server(*, config_path: Path | None = None) -> str | None:
    """Mint this install's inbound A2A credential, leaving the face closed.

    Returns the token it generated, or None when the install already had one.

    No schema default can carry this. The token is per-install secret material,
    so the field ships empty and is materialized here instead -- the same reason
    the Skill Hub endpoint is seeded at onboard time rather than declared. An
    empty token is not a weak credential but a closed door: ``a2a/auth.py``
    refuses every caller while it is empty, so a face switched on without one
    would advertise a capability that answers nobody.

    ``enabled`` is not written here at all. The inbound face is a second network
    surface, and finishing an unrelated install is not consent to open one --
    ``set_a2a_server_enabled`` is, reached through ``raven a2a enable``. Leaving
    the key absent rather than writing ``false`` also keeps a repeat onboard
    from closing a face the operator opened by hand.

    A present token is the "already initialized" mark, and it is never rotated.

    The minting write is the one that fixes the file to owner-only, and it does
    so from the temp file's first byte rather than with a chmod afterwards.
    Nothing else narrows this config -- ``save_config`` creates it under the
    process umask, 0644 under the common one -- and it already holds provider
    API keys, so the narrowing is owed either way. A run that mints nothing
    writes nothing, and so leaves whatever mode the operator chose.
    """
    path = config_path or get_config_path()

    def _apply(_text: str | None) -> tuple[str | None, str | None]:
        data = read_raw_or_raise(path)
        server = data.setdefault("a2a", {}).setdefault("server", {})
        if server.get("token"):
            return None, None
        minted = secrets.token_urlsafe(32)
        server["token"] = minted
        return json.dumps(data, indent=2, ensure_ascii=False), minted

    token = atomic_update(path, _apply, mode=0o600)
    if token is not None:
        # The value itself never reaches the log: it is the credential.
        logger.info("config/update: a2a.server initialized (token minted, face left closed)")
    return token


def set_a2a_server_enabled(enabled: bool, *, config_path: Path | None = None) -> str | None:
    """Open or close the inbound A2A face, minting the credential it needs.

    Returns the token it had to mint, or None when one was already on disk.

    Opening runs the minting pass first, so the two never disagree: a face that
    is on with an empty token answers every caller with a 401, which looks like
    a broken deployment rather than the closed door it is.

    Minting is what narrows the file to owner-only; the flip that follows asks
    for no mode. By then the credential is already on disk, so a later toggle
    adds no secret material and has no claim on a mode the operator chose.
    """
    path = config_path or get_config_path()
    minted = initialize_a2a_server(config_path=path) if enabled else None

    def _apply(_text: str | None) -> tuple[str, None]:
        data = read_raw_or_raise(path)
        data.setdefault("a2a", {}).setdefault("server", {})["enabled"] = enabled
        return json.dumps(data, indent=2, ensure_ascii=False), None

    atomic_update(path, _apply)
    logger.info("config/update: a2a.server.enabled set to {}", enabled)
    return minted


__all__ = [
    "EmbeddingPinError",
    "embedding_model_change",
    "update_cron_config",
    "reset_cron_config",
    "set_sentinel_enabled",
    "set_sentinel_nudge_quota",
    "set_default_model",
    "set_sandbox_backend",
    "set_embedding_endpoint",
    "set_memory_backend",
    "set_skill_blocked",
    "set_playbook_disabled",
    "initialize_a2a_server",
    "set_a2a_server_enabled",
]
