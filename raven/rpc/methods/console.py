"""Console handlers: ext.list / cron.* / settings.* / channels.status / fs.*.

The management surface a front end needs beside the transcript -- what is
installed, what is scheduled, what is configured, which channels are up, and
what is in the workspace. Nothing here is specific to one client.

Every handler reads and writes the same sources the CLI does (the CronService
store, LocalSkillCatalog, PluginDiscovery, config.json), so a change made from
any surface is the change every other surface sees.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.rpc import LOCAL_CHANNEL
from raven.rpc.errors import ConfigValidationError

if TYPE_CHECKING:
    from raven.rpc.methods import AgentLoopFactory


_SECRET_HINTS = ("key", "token", "secret", "password", "credential")


def _safe_loop(factory) -> Any:
    if factory is None:
        return None
    try:
        return factory()
    except Exception:
        return None


def _hub_marker_name() -> str | None:
    """Filename marking a hub-installed skill, or ``None`` without the market.

    The skill market is an optional install, so its absence is a normal state
    rather than a failure: with no market nothing is hub-installed, and every
    skill correctly reports ``hub=false``.
    """
    try:
        from raven.rpc.methods.skillhub import MARKER
    except ImportError:
        logger.debug("ext.list: skill market not installed; reporting every skill as hub=false")
        return None
    return MARKER


# ---------------------------------------------------------------------------
# ext.list
# ---------------------------------------------------------------------------


async def ext_list(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    from raven.cli._plugin_stack import plugin_discovery_sources
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config
    from raven.plugin.discover import PluginDiscovery

    loop = _safe_loop(agent_loop_factory)
    ec = load_raven_config()

    skills: list[dict] = []
    catalog = getattr(getattr(loop, "context", None), "skills", None)
    if catalog is None:
        try:
            from raven.memory_engine.skill_forge.catalog import LocalSkillCatalog

            config = load_config()
            catalog = LocalSkillCatalog(
                config.workspace_path,
                config=getattr(ec, "skill_forge", None),
                start_watcher=False,
            )
        except Exception:
            catalog = None
    if catalog is not None:
        # Hub-installed skills carry a marker file next to SKILL.md, and only
        # those can be uninstalled through skillhub.remove. The market is an
        # optional install, so its absence is a normal state, not a failure:
        # without it no skill is hub-installed and every entry reports
        # hub=false. Kept out of the loop's try so a missing market costs the
        # two hub fields, never the skill list itself.
        marker_name = _hub_marker_name()
        try:
            for m in catalog.gather_all_skills():
                path = getattr(m, "path", None)
                hub_id = ""
                marker = path.parent / marker_name if (path and marker_name) else None
                if marker is not None and marker.is_file():
                    try:
                        hub_id = str(json.loads(marker.read_text()).get("id") or "")
                    except Exception:
                        hub_id = ""
                skills.append(
                    {
                        "name": m.name,
                        "description": (m.description or "")[:200],
                        "source": str(m.source),
                        "always": bool(getattr(m, "always", False)),
                        "hub": marker is not None and marker.is_file(),
                        "hub_id": hub_id,
                    }
                )
        except Exception:
            logger.exception("ext.list: skill enumeration failed")

    plugins: list[dict] = []
    try:
        disabled = set(ec.plugins.disabled)
        for dp in PluginDiscovery(**plugin_discovery_sources()).discover():
            mf = dp.manifest
            plugins.append(
                {
                    "id": mf.id,
                    "display_name": mf.display_name,
                    "version": mf.version,
                    "enabled": mf.id not in disabled and mf.enabled_by_default,
                    "bundled": mf.bundled,
                }
            )
    except Exception:
        logger.exception("ext.list: plugin discovery failed")

    tools: list[dict] = []
    mcp: list[dict] = []
    if loop is not None:
        # Ownership comes from the registry's origin index, which is where a
        # namespaced tool records the server it came from. It used to be guessed
        # from the name: match the longest configured server name that prefixes
        # it. That reads as careful and is not -- a server name is sanitised
        # into the tool name, so ``context7.io`` produces ``mcp_context7_io_*``
        # and no configured key prefixes it, and the server showed as connected
        # with zero tools while the agent was calling them. The cap and the
        # collision suffix rewrite the tail for the same reason: the name is a
        # one-way function of the pair, so nothing can read it backwards.
        servers: dict[str, Any] = {}
        try:
            servers = dict(load_config().tools.mcp_servers or {})
        except Exception:
            logger.exception("ext.list: could not read the configured mcp servers")
        tool_owner: dict[str, str] = {}
        owned: dict[str, int] = {}
        for name in loop.tools.tool_names:
            ref = loop.tools.origin_of(name)
            if ref is not None:
                tool_owner[name] = ref.server
                owned[ref.server] = owned.get(ref.server, 0) + 1
        # MCP connects lazily (first turn / install kick), so between a restart
        # and the first use a declared server has registered nothing yet. It is
        # listed with what is actually known -- its tools, if any -- rather than
        # omitted, so an installed plugin never vanishes from the caller's view.
        try:
            from raven.mcp.client import resolve_transport

            for name, sc in servers.items():
                count = owned.get(name, 0)
                mcp.append(
                    {
                        "name": name,
                        "transport": resolve_transport(sc) or "unknown",
                        # Connected means "its tools are registered and callable",
                        # which is what the caller draws. The loop's own
                        # `_mcp_connected` only says the one-shot connect ran, so
                        # a server that failed inside it would read as connected.
                        "state": "connected" if count else "disconnected",
                        "connected": bool(count),
                        "tool_count": count,
                        "error": None,
                        "enabled": bool(getattr(sc, "enabled", True)),
                    }
                )
        except Exception:
            logger.exception("ext.list: config mcp merge failed")
        for name in loop.tools.tool_names:
            tool = loop.tools.get(name)
            tools.append(
                {
                    "name": name,
                    "description": (getattr(tool, "description", "") or "")[:200],
                    "enabled": True,
                    "mcp_server": tool_owner.get(name),
                    "needs": None,
                }
            )
        tools.extend(_gated_tools({t["name"] for t in tools}))

    return {"skills": skills, "plugins": plugins, "tools": tools, "mcp": mcp}


# Tools the loop withholds from the model until a key exists, and the config
# key that unlocks each. Withholding is right -- offered without a key, the
# model reaches for it and relays a failure naming a config path to whoever is
# on the other end of a channel -- but the same decision also erased them from
# the user's view, so the feature looked deleted rather than unconfigured.
_KEY_GATED_TOOLS: tuple[tuple[str, str, str], ...] = (("web_search", "tools.web.search.apiKey", "SERPER_API_KEY"),)


def _gated_tools(registered: set[str]) -> list[dict]:
    """Rows for key-gated tools that are not registered, so the page can offer
    the field instead of showing nothing at all."""
    rows: list[dict] = []
    for name, setting, env in _KEY_GATED_TOOLS:
        if name in registered:
            continue
        rows.append(
            {
                "name": name,
                "description": "",
                # Not enabled, and not a lie either: the model genuinely cannot
                # call it. The page reads `needs` to draw the setup affordance.
                "enabled": False,
                "mcp_server": None,
                "needs": {"setting": setting, "env": env},
            }
        )
    return rows


# ---------------------------------------------------------------------------
# cron.*
# ---------------------------------------------------------------------------


def _cron_service(loop):
    svc = getattr(loop, "cron_service", None) if loop is not None else None
    if svc is not None:
        return svc
    from raven.config.paths import get_cron_dir
    from raven.proactive_engine.schedulers.cron.service import CronService

    return CronService(get_cron_dir() / "jobs.json", allowed_channels=None)


def _job_info(j) -> dict:
    return {
        "id": j.id,
        "name": j.name,
        "enabled": j.enabled,
        "kind": j.schedule.kind,
        "expr": j.schedule.expr,
        "every_ms": j.schedule.every_ms,
        "at_ms": j.schedule.at_ms,
        "tz": j.schedule.tz,
        "message": j.payload.message,
        "next_run_at_ms": j.state.next_run_at_ms,
        "last_run_at_ms": j.state.last_run_at_ms,
        "last_status": j.state.last_status,
        "last_error": j.state.last_error,
    }


async def cron_list(params: dict, *, agent_loop_factory=None) -> dict:
    svc = _cron_service(_safe_loop(agent_loop_factory))
    return {"jobs": [_job_info(j) for j in svc.list_jobs(include_disabled=True)]}


async def cron_save(params: dict, *, agent_loop_factory=None) -> dict:
    from datetime import datetime

    from raven.proactive_engine.schedulers.cron.types import CronSchedule

    svc = _cron_service(_safe_loop(agent_loop_factory))
    kind = params.get("kind")
    tz = params.get("tz")
    if kind == "cron":
        if not params.get("expr"):
            raise ConfigValidationError("expr is required for kind=cron")
        schedule = CronSchedule(kind="cron", expr=params["expr"], tz=tz)
    elif kind == "every":
        secs = params.get("every_seconds")
        if not isinstance(secs, int) or secs <= 0:
            raise ConfigValidationError("every_seconds must be a positive integer")
        schedule = CronSchedule(kind="every", every_ms=secs * 1000)
    elif kind == "at":
        at_iso = params.get("at_iso")
        if not at_iso:
            raise ConfigValidationError("at_iso is required for kind=at")
        try:
            dt = datetime.fromisoformat(at_iso)
            if dt.tzinfo is None and tz:
                from zoneinfo import ZoneInfo

                dt = dt.replace(tzinfo=ZoneInfo(tz))
            elif dt.tzinfo is None:
                dt = dt.astimezone()
        except Exception as e:
            raise ConfigValidationError(f"invalid at_iso: {e}") from None
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        raise ConfigValidationError("kind must be cron | every | at")

    name = str(params.get("name") or "")[:30]
    message = str(params.get("message") or "")
    if not name or not message:
        raise ConfigValidationError("name and message are required")

    # An edit names the job it edits and `add_job` updates that job in place.
    # Removing it first, as this did, meant a rejected edit had already
    # committed the deletion -- so a bad schedule did not fail the edit, it
    # deleted the job.
    old_id = str(params.get("id") or "")
    try:
        job = svc.add_job(
            name=name,
            schedule=schedule,
            message=message,
            channel=LOCAL_CHANNEL,
            to="default",
            delete_after_run=kind == "at",
            # Keep the id across an edit: run history lives in the
            # cron:<id> session and would orphan under a fresh id.
            job_id=old_id or None,
        )
    except ValueError as e:
        raise ConfigValidationError(str(e)) from None
    return {"job": _job_info(job)}


async def cron_delete(params: dict, *, agent_loop_factory=None) -> dict:
    svc = _cron_service(_safe_loop(agent_loop_factory))
    return {"deleted": svc.remove_job(str(params.get("id", "")))}


async def cron_set_enabled(params: dict, *, agent_loop_factory=None) -> dict:
    svc = _cron_service(_safe_loop(agent_loop_factory))
    job = svc.enable_job(str(params.get("id", "")), enabled=bool(params.get("enabled", True)))
    if job is None:
        raise ConfigValidationError("unknown job id")
    return {"enabled": job.enabled}


async def cron_run_now(params: dict, *, agent_loop_factory=None) -> dict:
    svc = _cron_service(_safe_loop(agent_loop_factory))
    ok = await svc.run_job(str(params.get("id", "")), force=True)
    return {"ok": bool(ok)}


def _msg_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


async def cron_runs(params: dict, *, agent_loop_factory=None) -> dict:
    """Run history for one job, derived from its ``cron:<id>`` session.

    Every fire writes the job prompt as a user message into that session, so
    one user message = one run; a run counts as ok once its turn produced a
    non-empty assistant reply. The job state's last_error names the newest
    failure (per-run errors are not stored anywhere else).
    """
    from datetime import datetime

    from raven.config.loader import load_config
    from raven.rpc.methods.session import _manager_for, _safe_invoke_factory

    job_id = str(params.get("id", ""))
    svc = _cron_service(_safe_loop(agent_loop_factory))
    job = next((j for j in svc.list_jobs(include_disabled=True) if j.id == job_id), None)
    if job is None:
        raise ConfigValidationError("unknown job id")

    session_key = f"cron:{job_id}"
    try:
        mgr = _manager_for(_safe_invoke_factory(agent_loop_factory), load_config())
        raw = mgr.peek(session_key)
        messages = raw.messages if raw is not None else []
    except Exception:
        messages = []

    def _iso_ms(v: Any) -> int | None:
        try:
            return int(datetime.fromisoformat(str(v)).timestamp() * 1000)
        except (ValueError, TypeError):
            return None

    runs: list[dict] = []
    cur: dict | None = None
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user":
            cur = {"at_ms": _iso_ms(m.get("timestamp")), "ok": False, "preview": ""}
            runs.append(cur)
        elif role == "assistant" and cur is not None:
            text = _msg_text(m.get("content")).strip()
            if text:
                cur["ok"] = True
                cur["preview"] = text[:140]
    runs.reverse()
    if job.state.last_status == "error" and job.state.last_error:
        top = runs[0] if runs else None
        if top is not None and not top["ok"]:
            top["preview"] = str(job.state.last_error)[:140]
        else:
            runs.insert(
                0,
                {"at_ms": job.state.last_run_at_ms, "ok": False, "preview": str(job.state.last_error)[:140]},
            )
    return {"runs": runs[:50], "session_id": session_key}


# ---------------------------------------------------------------------------
# settings.*
# ---------------------------------------------------------------------------


def _mask_secrets(node: Any, key_hint: str = "") -> Any:
    if isinstance(node, dict):
        return {k: _mask_secrets(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_secrets(v, key_hint) for v in node]
    if isinstance(node, str) and node and any(h in key_hint.lower() for h in _SECRET_HINTS):
        return "••••••" + node[-4:] if len(node) > 8 else "••••••"
    return node


async def settings_get(params: dict, *, agent_loop_factory=None) -> dict:
    from importlib.metadata import version as pkg_version

    from raven.config.loader import get_config_path, read_raw_or_raise

    path = get_config_path()
    try:
        raw = read_raw_or_raise(path)
    except Exception as e:
        raise ConfigValidationError(f"config unreadable: {e}") from None
    try:
        ver = pkg_version("raven")
    except Exception:
        ver = "unknown"
    return {"settings": _mask_secrets(raw), "config_path": str(path), "raven_version": ver}


def _write_config_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


async def settings_set(params: dict, *, agent_loop_factory=None) -> dict:
    """Whitelisted dotted-path config writes for a settings surface.

    Channel enables and typed keys delegate to the existing update_* writers;
    raw list keys (plugins.disabled / tools.disabledTools) go through
    read_raw_or_raise + atomic replace so a malformed config never gets
    clobbered.
    """

    key = str(params.get("key", ""))
    value = params.get("value")

    if key == "language":
        if value not in ("en", "zh"):
            raise ConfigValidationError("language must be en | zh")
        from raven.config.update import set_language

        prev = set_language(value)
        return {"applied": True, "previous": prev}

    if key in ("cron.defaultTimezone", "cron.forwardChannels"):
        from raven.config.update import update_cron_config

        sub = key.split(".", 1)[1]
        if sub == "defaultTimezone":
            if not isinstance(value, str) or not value:
                raise ConfigValidationError("defaultTimezone must be a non-empty string")
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(value)
            except Exception:
                raise ConfigValidationError(f"unknown timezone: {value}") from None
        elif not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ConfigValidationError("forwardChannels must be a list of strings")
        prev = update_cron_config(sub, value)
        return {"applied": True, "previous": prev}

    if key.startswith("channels.") and key.endswith(".enabled"):
        from raven.config.update_channels import _channel_names, disable_channel, enable_channel

        name = key.split(".")[1]
        if name not in _channel_names():
            raise ConfigValidationError(f"unknown channel: {name}")
        if not isinstance(value, bool):
            raise ConfigValidationError("enabled must be a boolean")
        if value:
            enable_channel(name)
        else:
            disable_channel(name)
        return {"applied": True, "previous": not value}

    if key in ("plugins.disabled", "tools.disabledTools"):
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ConfigValidationError(f"{key} must be a list of strings")
        return _write_raw_key(key, value)

    checker = _SETTINGS_SIMPLE_KEYS.get(key)
    if checker is not None:
        return _write_raw_key(key, checker(value))

    raise ConfigValidationError(f"key not writable via settings.set: {key}")


def _write_raw_key(key: str, value: Any) -> dict:
    """Dotted-path read-modify-write into config.json, atomic replace."""
    from raven.config.loader import get_config_path, read_raw_or_raise

    path = get_config_path()
    try:
        raw = read_raw_or_raise(path)
    except Exception as e:
        raise ConfigValidationError(f"config unreadable: {e}") from None
    node = raw
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):
            raise ConfigValidationError(f"config path {key} blocked by non-object")
    prev = node.get(parts[-1])
    node[parts[-1]] = value
    _write_config_atomic(path, raw)
    return {"applied": True, "previous": prev}


def _chk_bool(key: str):
    def chk(v: Any) -> bool:
        if not isinstance(v, bool):
            raise ConfigValidationError(f"{key} must be a boolean")
        return v

    return chk


def _chk_int(key: str, lo: int, hi: int):
    def chk(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ConfigValidationError(f"{key} must be an integer")
        if not lo <= v <= hi:
            raise ConfigValidationError(f"{key} must be between {lo} and {hi}")
        return v

    return chk


def _chk_enum(key: str, *allowed: Any):
    def chk(v: Any) -> Any:
        if v not in allowed:
            raise ConfigValidationError(f"{key} must be one of {allowed}")
        return v

    return chk


def _chk_str(key: str, max_len: int = 500):
    def chk(v: Any) -> str:
        if not isinstance(v, str):
            raise ConfigValidationError(f"{key} must be a string")
        if len(v) > max_len:
            raise ConfigValidationError(f"{key} too long (max {max_len})")
        return v

    return chk


# Low-risk hot-writable keys a settings surface may offer. Each entry is a
# validator that returns the value to store; anything not listed here (or in
# the special cases above) stays editable only through the config file.
#
# Three keys are deliberately absent, and adding them back needs an argument
# rather than a line. `tools.restrictToWorkspace` and `tools.sandbox.backend`
# are the containment controls themselves -- one call to either turns a
# sandboxed agent into an unsandboxed one -- and `tools.web.proxy` would route
# every WebSearch and WebFetch, API keys and all, through a chosen host. This
# whitelist is reachable from any RPC client with no confirmation step, so it
# must not contain the settings that decide what an attacker who reaches it can
# then do. Editing the config file for those is the friction, and it is the
# point.
_SETTINGS_SIMPLE_KEYS: dict[str, Any] = {
    "tools.exec.timeout": _chk_int("tools.exec.timeout", 5, 3600),
    "tools.web.search.apiKey": _chk_str("tools.web.search.apiKey", 200),
    "tools.web.jinaApiKey": _chk_str("tools.web.jinaApiKey", 200),
    "tools.media.image.apiKey": _chk_str("tools.media.image.apiKey", 200),
    "tools.deepResearch.apiKey": _chk_str("tools.deepResearch.apiKey", 200),
    "channels.sendProgress": _chk_bool("channels.sendProgress"),
    "channels.sendToolHints": _chk_bool("channels.sendToolHints"),
    "memory.memoryTopK": _chk_int("memory.memoryTopK", 1, 50),
    "agents.defaults.enablePersonalization": _chk_bool("agents.defaults.enablePersonalization"),
    "agents.defaults.reasoningEffort": _chk_enum("agents.defaults.reasoningEffort", "minimal", "low", "medium", "high"),
}


# ---------------------------------------------------------------------------
# settings.usage — every API the agent burns: LLM calls and tool calls
# ---------------------------------------------------------------------------


async def settings_usage(params: dict, *, agent_loop_factory=None) -> dict:
    """Aggregate API usage for the settings page.

    LLM side reads the UsageTracker telemetry files
    (``~/.raven/telemetry/usage-YYYY-MM-DD.jsonl``, one JSON row per call);
    tool side counts ``tool_calls`` entries across session transcripts whose
    file mtime falls inside the window. Both scans are read-only and bounded
    by ``days`` (default 30, max 90).
    """
    from datetime import date, datetime, timedelta

    from raven.config.loader import load_config

    days = params.get("days")
    days = days if isinstance(days, int) and not isinstance(days, bool) else 30
    days = max(1, min(days, 90))

    models: dict[str, dict[str, Any]] = {}
    total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0}
    # The same resolution the writer uses (usage_tracker._default_telemetry_dir):
    # both used to hardcode ~/.raven, which held together only until RAVEN_HOME
    # moved one of them.
    from raven.config.loader import raven_home

    tel_dir = raven_home() / "telemetry"
    today = date.today()
    for i in range(days):
        p = tel_dir / f"usage-{(today - timedelta(days=i)).isoformat()}.jsonl"
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            name = str(row.get("model") or "?")
            acc = models.setdefault(
                name,
                {
                    "model": name,
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cost_usd": 0.0,
                },
            )
            inp = int(row.get("input_tokens") or 0)
            out = int(row.get("output_tokens") or 0)
            cr = int(row.get("cache_read_tokens") or 0)
            cost = float(row.get("estimated_cost_usd") or 0.0)
            acc["calls"] += 1
            acc["input_tokens"] += inp
            acc["output_tokens"] += out
            acc["cache_read_tokens"] += cr
            acc["cost_usd"] += cost
            total["calls"] += 1
            total["input_tokens"] += inp
            total["output_tokens"] += out
            total["cache_read_tokens"] += cr
            total["cost_usd"] += cost

    tools: dict[str, int] = {}
    tool_total = 0
    try:
        sess_root = Path(load_config().workspace_path) / "sessions"
        cutoff = (datetime.now() - timedelta(days=days)).timestamp()
        for p in sess_root.glob("*/*.jsonl"):
            try:
                if p.stat().st_mtime < cutoff:
                    continue
                lines = p.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                for tc in msg.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    name = tc.get("name") or (tc.get("function") or {}).get("name")
                    if name:
                        tools[str(name)] = tools.get(str(name), 0) + 1
                        tool_total += 1
    except Exception:
        logger.exception("settings.usage: tool scan failed")

    return {
        "days": days,
        "llm": {
            "total": total,
            "models": sorted(models.values(), key=lambda m: -m["cost_usd"]),
        },
        "tools": {
            "total": tool_total,
            "counts": sorted(({"name": k, "count": v} for k, v in tools.items()), key=lambda t: -t["count"]),
        },
    }


# ---------------------------------------------------------------------------
# settings.everos — the EverOS model roles behind long-term memory
# ---------------------------------------------------------------------------

_EVEROS_FIELDS = ("model", "api_key", "base_url", "provider")
_EVEROS_REQUIRED = ("llm", "embedding")


async def settings_everos(params: dict, *, agent_loop_factory=None) -> dict:
    """Current EverOS model sections, api_key reduced to a set/unset flag."""
    from raven.config.update_everos import (
        WRITABLE_SECTIONS,
        get_everos_config_path,
        load_everos_config,
    )

    data = load_everos_config()
    sections = {}
    for sec in WRITABLE_SECTIONS:
        cur = data.get(sec) or {}
        model = str(cur.get("model") or "")
        # The shipped template seeds placeholder "<...>" model names.
        if model.startswith("<"):
            model = ""
        sections[sec] = {
            "model": model,
            "base_url": str(cur.get("base_url") or ""),
            "provider": str(cur.get("provider") or ""),
            "api_key_set": bool(cur.get("api_key")),
        }
    return {"sections": sections, "config_path": str(get_everos_config_path())}


async def settings_everos_set(params: dict, *, agent_loop_factory=None) -> dict:
    """Merge fields into one EverOS section, or clear an optional section."""
    from raven.config.update_everos import (
        WRITABLE_SECTIONS,
        clear_everos_section,
        set_everos_section,
    )

    section = str(params.get("section", ""))
    if section not in WRITABLE_SECTIONS:
        raise ConfigValidationError(f"unknown everos section: {section}")

    if params.get("clear") is True:
        if section in _EVEROS_REQUIRED:
            raise ConfigValidationError(f"{section} is required for EverOS memory and cannot be cleared")
        clear_everos_section(section)
        return {"applied": True}

    fields = params.get("fields")
    if not isinstance(fields, dict):
        raise ConfigValidationError("fields must be an object")
    clean: dict[str, str] = {}
    for k, v in fields.items():
        if k not in _EVEROS_FIELDS:
            raise ConfigValidationError(f"field not writable: {k}")
        if not isinstance(v, str):
            raise ConfigValidationError(f"{k} must be a string")
        v = v.strip()
        if len(v) > 500:
            raise ConfigValidationError(f"{k} too long (max 500)")
        if v:
            clean[k] = v
    borrow = params.get("borrow_from")
    if borrow is not None:
        if not isinstance(borrow, str) or not borrow.strip():
            raise ConfigValidationError("borrow_from must be a provider name")
        from raven.config.update_everos import borrow_provider_credentials

        try:
            lent = borrow_provider_credentials(borrow.strip())
        except KeyError as exc:
            raise ConfigValidationError(f"no such provider: {borrow}") from exc
        except ValueError as exc:
            raise ConfigValidationError(str(exc)) from exc
        # The borrowed values win over anything the client sent for the same
        # keys: the page cannot read a stored key -- `model.endpoints` redacts
        # it -- so a client-sent api_key alongside a borrow is the redaction
        # itself being echoed back, which would overwrite a real key with the
        # word for one.
        clean.update(lent)

    if not clean:
        raise ConfigValidationError("fields must carry at least one non-empty value")
    set_everos_section(section, clean)
    return {"applied": True}


# ---------------------------------------------------------------------------
# channels.status
# ---------------------------------------------------------------------------


async def channels_status(params: dict, *, agent_loop_factory=None) -> dict:
    from raven.config.loader import load_config
    from raven.config.update_channels import _channel_names, channel_field_specs

    config = load_config()
    items = []
    for name in _channel_names():
        model = getattr(config.channels, name, None)
        enabled = bool(getattr(model, "enabled", False))
        missing: list[str] = []
        # Every field the channel takes, so a client can render its configure
        # form instead of naming the gaps and sending the user to a terminal.
        # Values never ride along -- only whether one is set -- because the row
        # crosses the wire on every status poll and secrets among them.
        fields: list[dict] = []
        try:
            for field, spec in channel_field_specs(name).items():
                if field == "enabled":
                    continue
                # A walk, not one getattr: three of the twelve channels spec
                # nested keys (`dm.policy`, `mention.require_in_groups`), and a
                # flat lookup returns None for those -- so a configured slack DM
                # policy drew as an empty box in the configure form, forever.
                current: Any = model
                for part in field.split("."):
                    current = getattr(current, part, None)
                    if current is None:
                        break
                is_set = current not in (None, "", []) and current != spec.get("default")
                if spec.get("required") and current in (None, "", []):
                    missing.append(field)
                fields.append(
                    {
                        "key": field,
                        "label": str(spec.get("description") or field),
                        "required": bool(spec.get("required")),
                        "secret": bool(spec.get("is_secret")),
                        "set": bool(is_set),
                    }
                )
        except Exception:
            pass
        items.append(
            {
                "name": name,
                "enabled": enabled,
                "configured": not missing,
                "missing": missing,
                "fields": fields,
            }
        )

    gateway_running = False
    try:
        import time

        from raven.cli._gateway_lock import read_status

        gateway_running = read_status(time.time()) is not None
    except Exception:
        pass

    # `enabled` is what the config asks for; these say what is actually
    # happening. They stay absent when no gateway answered, so a client can
    # tell "not connected" from "nobody could say" -- collapsing the second
    # into the first is how a working channel came to read as broken.
    live = None
    try:
        from raven.channels.live_probe import channel_liveness

        live = await channel_liveness()
    except Exception:
        live = None
    if live:
        for item in items:
            state = live.get(item["name"])
            if not isinstance(state, dict):
                continue
            item["running"] = bool(state.get("running"))
            if state.get("connected") is not None:
                item["connected"] = bool(state["connected"])
            item["qr_login"] = bool(state.get("qr_login"))

    return {"channels": items, "gateway_running": gateway_running}


async def channels_configure(params: dict, *, agent_loop_factory=None) -> dict:
    """Patch one channel's credential fields, validated against its schema.

    The dedicated writer, not a widening of ``settings.set``: field names are
    checked against the channel's own spec map here, so an arbitrary dotted
    path can never ride a credential write into the rest of the config.
    Empty-string values are skipped rather than written -- the form sends every
    box it showed, and a blank one means "left as is", not "erase".
    """
    from raven.config.update_channels import (
        _channel_names,
        channel_field_specs,
        disable_channel,
        enable_channel,
        set_channel_fields,
    )

    name = str(params.get("name") or "")
    if name not in _channel_names():
        raise ConfigValidationError(f"unknown channel: {name}")
    raw = params.get("fields")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigValidationError("fields must be an object")
    enabled = params.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigValidationError("enabled must be a boolean")
    if not raw and enabled is None:
        raise ConfigValidationError("nothing to apply: give fields, enabled, or both")
    specs = channel_field_specs(name)
    payload: dict[str, Any] = {}
    for key, value in raw.items():
        # `enabled` is not a credential and does not travel in the patch: it is
        # its own argument, routed through the enable/disable writers.
        if key == "enabled" or key not in specs:
            raise ConfigValidationError(f"unknown field for channel {name}: {key}")
        if isinstance(value, str) and not value.strip():
            continue
        payload[key] = value
    if not payload and enabled is None:
        return {"applied": False}
    try:
        # Credentials first, then the switch, so a channel is never left on
        # without the values it was turned on for.
        if enabled is True:
            enable_channel(name, payload or None)
        else:
            if payload:
                set_channel_fields(name, payload)
            if enabled is False:
                disable_channel(name)
    except Exception as e:
        raise ConfigValidationError(str(e)) from None
    # The switch is config; the adapter is the gateway's. Writing the flag used
    # to be the whole of "connect", so a channel turned on here did nothing at
    # all until the next launch -- for a scan-login entrance that meant no QR
    # could ever appear, and the page said "reopen Raven App" instead of
    # signing anyone in. Ask the gateway to start (or stop) it now.
    if enabled is not None:
        from raven.channels.live_probe import channel_start, reset_cache

        try:
            await channel_start(name, enabled=enabled)
        except Exception:
            # Nobody answered, or the gateway refused: the config write stands
            # and the next launch honours it, which is what the page already
            # says while an adapter is not up.
            pass
        # The liveness cache is three seconds old at most, but the poll right
        # after this write is exactly the one that should see the new adapter.
        reset_cache()
    return {"applied": True}


async def channels_qr(params: dict) -> dict:
    """The pending login QR for one channel, read off the live adapter.

    A pass-through to the gateway, which is the only process holding the
    adapter. Empty when no gateway answered -- the same shape as "nothing
    pending", because to this surface the two are the same: there is no code to
    show either way, and inventing a distinction here would only add a state the
    dialog has to explain.
    """
    name = str(params.get("name") or "")
    try:
        from raven.channels.live_probe import channel_qr

        answer = await channel_qr(name)
    except Exception:
        answer = None
    answer = answer or {}
    return {
        "qr": answer.get("qr") or None,
        "qr_text": answer.get("qr_text") or None,
        "connected": bool(answer.get("connected")),
        "running": bool(answer.get("running")),
    }


# ---------------------------------------------------------------------------
# fs.*
# ---------------------------------------------------------------------------

_FS_MAX_ENTRIES = 500
_FS_DEFAULT_MAX_BYTES = 200_000


def _workspace_root(loop, session_key: str = "") -> Path:
    """The directory the page's file panel is rooted at.

    The working directory the session's turns run in, not agent home: home is
    ``~/.raven/workspace`` by default, which holds the agent's own memory and
    is not where a launch-directory turn reads or writes anything. Resolved
    per session because a session can be pinned to its own directory (the
    persisted ``workdir`` override); an empty key falls through to the
    resolver's policy default, which for the gateway is the launch directory.
    """
    if loop is not None:
        peek = getattr(loop, "peek_session_workdir", None)
        if peek is not None:
            try:
                return Path(peek(session_key)).resolve()
            except ValueError:
                pass
    ws = getattr(loop, "workspace", None) if loop is not None else None
    if ws:
        return Path(ws).resolve()
    from raven.config.loader import load_config

    return Path(load_config().workspace_path).resolve()


def _resolve_inside(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root``, refusing anything outside it -- or inside
    raven's own state directory.

    Containment alone was enough while the root was agent home, a *sibling* of
    the state dir. It is not now: the root is the session's working directory,
    which under the launch-directory policy is wherever the gateway was started
    from. Started in the reader's home -- the ordinary place to type a command
    -- the state dir sits *under* the root, and `fs.list` even un-hides it. That
    hands the page ``serve.json``, whose token mints unlimited nonces and so
    outlives the cookie, and ``config.json`` with the provider keys.

    The same fence the viewer and `fs.reveal` apply -- literally the same
    function, because a second copy of it drifted immediately: the first one
    here lacked the workspace carve-out, so with the root at the default
    ``~/.raven/workspace`` it refused every file the agent had written, which is
    this bug mirrored.
    """
    from raven.rpc.files import in_state_dir

    p = (root / rel.lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise ConfigValidationError("path escapes workspace")
    if in_state_dir(p, root):
        raise ConfigValidationError(f"{p} is inside raven's state directory")
    return p


async def fs_list(params: dict, *, agent_loop_factory=None) -> dict:
    root = _workspace_root(_safe_loop(agent_loop_factory), str(params.get("session") or ""))
    rel = str(params.get("path") or "")
    target = _resolve_inside(root, rel)
    if not target.is_dir():
        raise ConfigValidationError(f"not a directory: {rel or '/'}")
    entries = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as e:
        raise ConfigValidationError(str(e)) from None
    for child in children[:_FS_MAX_ENTRIES]:
        # Hidden entries stay hidden. `.raven` used to be the one exception,
        # from when the root was agent home and the state dir was elsewhere;
        # under the launch-directory policy it can be a child of the root, and
        # every path inside it is now refused -- so listing it offers a folder
        # that answers nothing.
        if child.name.startswith("."):
            continue
        try:
            entries.append(
                {
                    "name": child.name,
                    "dir": child.is_dir(),
                    "size": 0 if child.is_dir() else child.stat().st_size,
                }
            )
        except OSError:
            continue
    return {"root": str(root), "path": rel, "entries": entries}


_FS_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_UPLOAD_DIR = "uploads"


def _safe_name(name: str) -> str:
    """Strip directory parts and anything that could escape or confuse a shell."""
    base = Path(str(name or "file")).name
    cleaned = "".join(c for c in base if c.isalnum() or c in "._- ()[]").strip()
    return (cleaned or "file")[:120]


def _upload_root() -> Path:
    """Where an uploaded file is deposited: agent home, not the session workdir.

    The relative path this handler returns rides back to ``turn.send``, which
    resolves an attachment against agent home and fences it there, so a file
    parked anywhere else is dropped from the turn without an error. Agent home
    is also the one root always writable: a session's working directory is
    wherever the engine was launched from, and an engine started by the desktop
    shell inherits ``/``, where creating the directory cannot succeed.

    Browsing is the other way round on purpose -- ``fs.list`` / ``fs.read`` /
    ``fs.reveal`` stay rooted at the session's working directory, because the
    panel shows where this session's turns read and write.
    """
    from raven.config.loader import load_config

    return Path(load_config().workspace_path).expanduser().resolve()


async def fs_upload(params: dict, *, agent_loop_factory=None) -> dict:
    """Store an uploaded file under ``<agent home>/uploads`` and return its path.

    The caller hands the agent a path, not bytes: every tool that reads files is
    already workspace-scoped, so an upload is just a file appearing in the
    workspace. Collisions get a numeric suffix rather than overwriting.

    ``session`` is still accepted (the page sends it) but does not select the
    root -- see :func:`_upload_root`.
    """
    import base64

    root = _upload_root()
    raw = params.get("content_b64") or ""
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        raise ConfigValidationError("content_b64 is not valid base64") from None
    if not data:
        raise ConfigValidationError("empty file")
    if len(data) > _FS_MAX_UPLOAD_BYTES:
        raise ConfigValidationError(f"file exceeds {_FS_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")

    target_dir = root / _UPLOAD_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Guarded like the write below: unguarded, an unwritable root reached
        # the page as a bare -32603 ``internal_error`` with no reason in it.
        raise ConfigValidationError(f"cannot create {target_dir}: {e}") from None
    name = _safe_name(params.get("name", ""))
    target = target_dir / name
    stem, suffix = target.stem, target.suffix
    n = 1
    while target.exists():
        target = target_dir / f"{stem}-{n}{suffix}"
        n += 1
    try:
        target.write_bytes(data)
    except OSError as e:
        raise ConfigValidationError(f"write failed: {e}") from None
    return {
        "path": f"{_UPLOAD_DIR}/{target.name}",
        "abs_path": str(target),
        "size": len(data),
    }


async def fs_reveal(params: dict, *, agent_loop_factory=None) -> dict:
    """Show one file in the host's file manager, selected.

    The gateway's host is where the file lives, so the reveal happens there --
    for the localhost page that is the reader's own desktop, which is the whole
    use. Fenced exactly like the viewer (``resolve_readable``): a path the page
    may not render is not one it may pop a Finder window on either.
    """
    import subprocess
    import sys

    from raven.rpc.files import resolve_readable

    raw = str(params.get("path") or "").strip()
    if not raw:
        raise ConfigValidationError("path is required")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        # The session's working directory first, since that is what the file
        # panel lists; agent home second, by handing the relative path to
        # ``resolve_readable``, which roots it there. Both are needed because
        # the page sends one string to two surfaces: the panel's own rows are
        # relative to the workdir, while an uploaded attachment's chip carries
        # ``uploads/<name>``, and only agent home holds that.
        listed = _workspace_root(_safe_loop(agent_loop_factory), str(params.get("session") or "")) / raw
        p = listed if listed.exists() else Path(raw)
    try:
        target = resolve_readable(str(p))
    except (ValueError, PermissionError, FileNotFoundError, IsADirectoryError, OSError) as e:
        raise ConfigValidationError(str(e)) from None
    if sys.platform == "darwin":
        argv = ["open", "-R", str(target)]
    elif sys.platform.startswith("win"):
        argv = ["explorer", f"/select,{target}"]
    else:
        # No cross-desktop "select this file" verb exists, so the containing
        # folder is the best any Linux file manager can be asked for.
        argv = ["xdg-open", str(target.parent)]
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ConfigValidationError(f"reveal failed: {e}") from None
    return {"ok": True}


# An application NAME, and nothing that could be anything else. The check is a
# whitelist rather than a blacklist because this string becomes an argv element
# next to a resolved path: letters, digits, spaces, and the four punctuation
# marks real application names use. That rejects a separator (no arbitrary
# binary by path), a leading dash (no flag smuggled into `open`), and every
# shell metacharacter -- which cannot reach a shell anyway, since nothing here
# runs one, but a name that looks like a command is a name worth refusing.
_APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$")


async def fs_open(params: dict, *, agent_loop_factory=None) -> dict:
    """Hand one file to the host's own application for it.

    The other half of the viewer: a kind the page can render opens in the page,
    and a kind it cannot -- a pptx, a spreadsheet, an archive -- opens in
    whatever the reader picked for it. Fenced exactly like ``fs.reveal``
    (``resolve_readable``): a path the page may not render is not one it may
    launch an application on either.

    Like ``fs.reveal``, this runs on the GATEWAY's host, which is the reader's
    own desktop for a localhost page and somebody else's machine otherwise. The
    page is what decides whether to offer it.

    ``app`` is placed as a single argv element, never concatenated into a
    command line and never passed to a shell.
    """
    import subprocess
    import sys

    from raven.rpc.files import resolve_readable

    raw = str(params.get("path") or "").strip()
    if not raw:
        raise ConfigValidationError("path is required")
    app = params.get("app")
    app = str(app).strip() if app is not None else ""
    if app and not _APP_NAME.match(app):
        raise ConfigValidationError("app must be an application name, not a path or a command")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        # Same two roots as fs.reveal, for the same reason: the panel's rows are
        # relative to the session's working directory, an uploaded attachment's
        # chip carries `uploads/<name>` and only agent home holds that.
        listed = _workspace_root(_safe_loop(agent_loop_factory), str(params.get("session") or "")) / raw
        p = listed if listed.exists() else Path(raw)
    try:
        target = resolve_readable(str(p))
    except (ValueError, PermissionError, FileNotFoundError, IsADirectoryError, OSError) as e:
        raise ConfigValidationError(str(e)) from None
    if sys.platform == "darwin":
        argv = ["open", "-a", app, str(target)] if app else ["open", str(target)]
    elif sys.platform.startswith("win"):
        # `start` is a shell builtin, so the launcher is cmd itself -- with the
        # empty string standing in for the window title `start` would otherwise
        # read the quoted path as. An application choice has no portable form
        # here, so the file goes to its own default.
        argv = ["cmd", "/c", "start", "", str(target)]
    else:
        argv = [app, str(target)] if app else ["xdg-open", str(target)]
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise ConfigValidationError(f"open failed: {e}") from None
    return {"ok": True, **({"app": app} if app else {})}


async def fs_read(params: dict, *, agent_loop_factory=None) -> dict:
    root = _workspace_root(_safe_loop(agent_loop_factory), str(params.get("session") or ""))
    rel = str(params.get("path") or "")
    target = _resolve_inside(root, rel)
    if not target.is_file():
        raise ConfigValidationError(f"not a file: {rel}")
    # Read the window, not the file. `read_bytes()[:limit]` materialised the
    # whole thing to throw almost all of it away: an agent-written multi-gigabyte
    # log in the workspace meant allocating all of it to return 200 KB, on the
    # event loop, stalling every other client on the shared socket.
    #
    # The bound is on the read, not on the file. `fh.read(limit)` allocates at
    # most `limit` whatever the file's size, so a size ceiling would add nothing
    # here -- it would only make the very file this windowing exists for
    # unreadable. `/file` needs one because it serves the whole file to a
    # renderer; this returns a window and says so, in `truncated`.
    from raven.rpc.files import MAX_VIEW_BYTES

    limit = params.get("max_bytes")
    if not isinstance(limit, int) or limit <= 0:
        limit = _FS_DEFAULT_MAX_BYTES
    # A caller-supplied ceiling is still a ceiling: without this, `max_bytes`
    # was an unbounded allocation request from the wire.
    limit = min(limit, MAX_VIEW_BYTES)
    size = target.stat().st_size
    with target.open("rb") as fh:
        data = await asyncio.to_thread(fh.read, limit)
    return {
        "content": data.decode("utf-8", errors="replace"),
        "truncated": size > limit,
        "size": size,
    }


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


async def deliverables_list(params: dict, *, agent_loop_factory=None) -> dict:
    """``deliverables.list`` -- what this conversation has handed over.

    The registry, not the transcript. A client can read the manifests off the
    turn events as they arrive, and one that was open the whole time will have
    them all -- but only that one. A reconnect re-opens the session from disk
    and a turn still in flight has not been written yet; a compaction archives
    the tool messages that carried the manifests; a second client was never
    told. The registry outlives all three, because it is what the download
    token resolves against.

    ``missing`` is stat'd here rather than pruned: a delivered file that has
    since been deleted is still something this conversation handed over, and
    saying so is more use than dropping the row.
    """
    session_key = str(params.get("session_key") or "")
    loop = _safe_loop(agent_loop_factory)
    store = getattr(loop, "deliverables", None)
    if store is None or not session_key:
        return {"files": []}
    from urllib.parse import quote

    files = [
        {
            "path": rec.path,
            "name": rec.name,
            "title": rec.title,
            "description": rec.description,
            "size": rec.size,
            "media_type": rec.media_type,
            "download_path": f"/files/download?token={quote(rec.token)}",
            "created_at": rec.created_at,
            "missing": not os.path.isfile(rec.path),
        }
        for rec in store.for_conversation(session_key)
    ]
    return {"files": files}


def register_console_methods(dispatcher, *, agent_loop_factory=None) -> None:
    """Register the console handlers with the loop factory pre-bound."""

    def bind(fn):
        async def _h(params: dict) -> dict:
            return await fn(params, agent_loop_factory=agent_loop_factory)

        return _h

    dispatcher.register("ext.list", bind(ext_list))
    dispatcher.register("cron.list", bind(cron_list))
    dispatcher.register("cron.save", bind(cron_save))
    dispatcher.register("cron.delete", bind(cron_delete))
    dispatcher.register("cron.set_enabled", bind(cron_set_enabled))
    dispatcher.register("cron.run_now", bind(cron_run_now))
    dispatcher.register("cron.runs", bind(cron_runs))
    dispatcher.register("settings.get", bind(settings_get))
    dispatcher.register("settings.set", bind(settings_set))
    dispatcher.register("settings.usage", bind(settings_usage))
    dispatcher.register("settings.everos", bind(settings_everos))
    dispatcher.register("settings.everosSet", bind(settings_everos_set))
    dispatcher.register("channels.status", bind(channels_status))
    dispatcher.register("channels.configure", bind(channels_configure))
    dispatcher.register("channels.qr", channels_qr)
    dispatcher.register("fs.list", bind(fs_list))
    dispatcher.register("fs.read", bind(fs_read))
    dispatcher.register("fs.upload", bind(fs_upload))
    dispatcher.register("fs.reveal", bind(fs_reveal))
    dispatcher.register("fs.open", bind(fs_open))
    dispatcher.register("deliverables.list", bind(deliverables_list))


__all__ = ["register_console_methods"]
