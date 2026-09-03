"""Bundle collector — one trajectory packed into a self-contained directory.

A bundle is the offline form of a trajectory: everything needed to inspect
(and later report or replay) one attempt, collected out of the live tracing
store into a single directory that survives log rotation, artifact cleanup,
and being copied to another machine::

    <out>/<attempt_id>/
      manifest.json     # format version, ids, time range, counts, environment summary
      spans.jsonl       # every span of the attempt, in write order
      artifacts/        # every file the spans' *.artifact_path attrs referenced
      session.jsonl     # the session's conversation record (omitted if missing)
      verdicts.jsonl    # every verdict recorded for the attempt

Artifact references inside ``spans.jsonl`` are rewritten to bundle-relative
paths (``artifacts/<name>``) so the bundle reads offline; missing artifact
files are skipped and listed in the manifest rather than failing the pack.
A trace id addressing a multi-turn attempt is resolved to the canonical
attempt id first, so the bundle always holds the whole trajectory. The
bundle is built in a staging directory and swapped in whole, so a re-pack
never leaves stale files from a previous pack behind. Bundling declares the
trajectory corpus, so the id is auto-pinned.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from raven import __version__
from raven.config.paths import get_workspace_path
from raven.tracing import config as tracing_config
from raven.tracing.spans import SCHEMA_VERSION
from raven.trajectory.store import attempt_alias_ids, iter_spans, pin, pin_attempt, resolve_attempt_id
from raven.trajectory.verdict import read_verdicts

BUNDLE_FORMAT_VERSION = 1

_ARTIFACTS_DIR = "artifacts"


def _import_artifact(
    source: str,
    artifacts_dir: Path,
    copied: dict[str, str],
    missing: list[str],
    names: dict[str, str],
) -> str | None:
    """Copy one referenced file into the bundle; return its relative path.

    Deduped per source path. Distinct sources sharing a basename get a
    counter prefix so neither overwrites the other. Returns None (and records
    the source in ``missing``) when the file no longer exists.
    """
    if source in copied:
        return copied[source]
    if source in missing:
        return None
    src = Path(source)
    if not src.is_file():
        missing.append(source)
        return None
    name = src.name
    counter = 1
    while name in names and names[name] != source:
        name = f"{counter}-{src.name}"
        counter += 1
    names[name] = source
    shutil.copy2(src, artifacts_dir / name)
    rel = f"{_ARTIFACTS_DIR}/{name}"
    copied[source] = rel
    return rel


def _default_workspace() -> Path:
    """The configured agent workspace; the stock default when config is unreadable."""
    try:
        from raven.config.loader import load_config

        return load_config().workspace_path
    except Exception:
        return get_workspace_path()


def _session_source(session_key: str, workspace: Path | None) -> Path:
    from raven.session.manager import SessionManager

    return SessionManager(workspace or _default_workspace())._get_session_path(session_key)


def _git_revision(package_dir: Path | None = None) -> str | None:
    """HEAD of the source checkout ``package_dir`` (the raven package) belongs to.

    None unless the checkout's toplevel is the package directory's own
    parent — a wheel under site-packages resolves ``git rev-parse`` to
    whatever repository happens to contain the virtualenv, and that host
    project's HEAD (possibly a private repo's) must not leak into a report.
    """
    import raven

    git = shutil.which("git")
    if git is None:
        return None
    package = (package_dir or Path(raven.__path__[0])).resolve()

    def run(*args: str) -> str | None:
        result = subprocess.run(
            [git, "-C", str(package.parent), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None

    toplevel = run("rev-parse", "--show-toplevel")
    if toplevel is None or Path(toplevel).resolve() != package.parent:
        return None
    return run("rev-parse", "HEAD")


def _configured_provider_names() -> list[str]:
    """Names of providers `credential_status` deems usable, canonicalized.

    Delegates the judgement entirely to ``providers.auth.credential_status``
    (the single authority — duplicating the rule here is what once made a
    Gemini section holding only ``api_key_list`` invisible to routing while
    ``provider list`` showed it as configured). ``include_external=True``
    mirrors ``provider list``: an OAuth token held outside the config counts.
    """
    from raven.config.loader import load_config
    from raven.providers.auth import credential_status
    from raven.providers.registry import canonical_provider_name

    providers = load_config().providers
    candidates = set(type(providers).model_fields) | {str(name) for name in (providers.model_extra or {})}
    configured: set[str] = set()
    for name in candidates:
        section = providers.get(name)
        if section is None:
            continue
        try:
            ok = credential_status(name, section, include_external=True).ok
        except Exception:  # noqa: BLE001 — one unresolvable section must not hide the rest
            continue
        if ok:
            configured.add(canonical_provider_name(name))
    return sorted(configured)


def _enabled_channel_names() -> list[str]:
    from raven.config.loader import load_config

    return sorted(load_config().channels.enabled_channel_names())


def _mcp_server_names() -> list[str]:
    from raven.config.loader import load_config

    return sorted(str(name) for name in load_config().tools.mcp_servers)


def _discovered_plugins() -> list[dict[str, Any]]:
    """Discovered plugin manifests — discovery, not activation.

    File-based sources are pure TOML reads; entry-point manifests are read
    from distribution metadata so no plugin code runs (the live scan's
    resource resolution imports the package, which a report path must never
    do first). ``admitted`` says the manifest passes the registry's
    admission gate (not user-disabled, enabled by default); whether its
    factory actually imported at boot is unknowable here without running
    plugin code, so the field deliberately claims discovery only.
    """
    from raven.config.raven import load_raven_config
    from raven.plugin.bootstrap import default_discovery_sources
    from raven.plugin.discover import PluginDiscovery, entry_point_manifests_without_import

    sources = default_discovery_sources()
    group = sources.pop("entry_points_group")
    disabled = set(load_raven_config().plugins.disabled)
    by_id = {p.manifest.id: p for p in entry_point_manifests_without_import(group)}
    # File-based sources win over entry points, matching live discovery priority.
    by_id.update({p.manifest.id: p for p in PluginDiscovery(**sources).discover()})
    return [
        {
            "id": p.manifest.id,
            "version": p.manifest.version,
            "admitted": p.manifest.id not in disabled and p.manifest.enabled_by_default,
        }
        for _, p in sorted(by_id.items())
    ]


def _config_structure() -> dict[str, Any]:
    from raven.config.loader import get_config_path

    path = get_config_path()
    keys = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            keys = sorted(str(k) for k in data)
    return {"path": str(path), "exists": path.exists(), "top_level_keys": keys}


def _collect_environment() -> dict[str, Any]:
    """Structure-only environment snapshot for the manifest.

    Records names, versions, and structural keys — never a config leaf value
    (no tokens, endpoints, or header values). Every probe is independent and
    best-effort: a failing field becomes null; this block must never fail
    the pack.
    """

    def probe(fn: Any) -> Any:
        try:
            return fn()
        except Exception:  # noqa: BLE001 — any probe failure degrades to null
            return None

    return {
        "raven_version": probe(lambda: __version__),
        "git_revision": probe(_git_revision),
        "python_version": probe(platform.python_version),
        "os": probe(platform.system),
        "arch": probe(platform.machine),
        "providers": probe(_configured_provider_names),
        "channels": probe(_enabled_channel_names),
        "mcp_servers": probe(_mcp_server_names),
        "discovered_plugins": probe(_discovered_plugins),
        "config": probe(_config_structure),
        "tracing_schema": SCHEMA_VERSION,
        "bundle_format": BUNDLE_FORMAT_VERSION,
    }


def collect_bundle(
    id_: str,
    out_dir: Path | None = None,
    state_dir: Path | None = None,
    *,
    workspace: Path | None = None,
) -> Path:
    """Pack the trajectory addressed by ``id_`` (attempt id or trace id).

    Reads spans through :func:`raven.trajectory.store.iter_spans` (rotation-
    transparent, both id kinds). A trace id belonging to a multi-turn attempt
    is resolved to that attempt's canonical id, and the whole attempt (every
    turn, its verdicts, the pin) is collected under that id. Writes the bundle
    under ``out_dir`` (default: ``<state_dir>/bundles``) and pins the id.
    Raises ``LookupError`` when no span matches ``id_``.
    """
    if not id_:
        raise ValueError("id is required")
    resolved_state = state_dir or tracing_config.state_dir()
    # A turn's trace id may address a multi-turn attempt; the canonical
    # attempt id owns the whole trajectory, so collect and name by it.
    attempt_id = resolve_attempt_id(id_, resolved_state)
    if attempt_id is None:
        raise LookupError(f"no spans found for id {id_!r}")
    spans = list(iter_spans(resolved_state, attempt_id=attempt_id))
    if not spans:
        raise LookupError(f"no spans found for attempt {attempt_id!r}")

    out_root = (out_dir or resolved_state / "bundles").resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    # Attempt ids come from span records or the hand-editable attempts.json,
    # so refuse any id that would escape out_root as a directory name.
    bundle_dir = (out_root / attempt_id).resolve()
    if bundle_dir.parent != out_root:
        raise ValueError(f"id {attempt_id!r} cannot be used as a bundle directory name")

    # Build in a staging dir and swap in whole, so a re-pack never inherits
    # stale files (e.g. an artifact whose source was purged since last pack).
    staging = Path(tempfile.mkdtemp(prefix=f".{bundle_dir.name}-", dir=out_root))
    try:
        artifacts_dir = staging / _ARTIFACTS_DIR
        artifacts_dir.mkdir()

        copied: dict[str, str] = {}
        missing: list[str] = []
        names: dict[str, str] = {}
        rewritten = 0
        out_spans: list[dict[str, Any]] = []
        session_key: str | None = None
        start: str | None = None
        end: str | None = None
        for span in spans:
            # Historical records are unvalidated JSON: a truthy non-object
            # attributes value or non-string key/timestamp degrades per-field.
            raw_attrs = span.get("attributes")
            attrs = dict(raw_attrs) if isinstance(raw_attrs, dict) else {}
            if session_key is None and isinstance(attrs.get("session.key"), str) and attrs["session.key"]:
                session_key = attrs["session.key"]
            span_start, span_end = span.get("startTime"), span.get("endTime")
            if isinstance(span_start, str) and span_start and (start is None or span_start < start):
                start = span_start
            if isinstance(span_end, str) and span_end and (end is None or span_end > end):
                end = span_end
            for key, value in attrs.items():
                if not key.endswith(".artifact_path") or not isinstance(value, str) or not value:
                    continue
                rel = _import_artifact(value, artifacts_dir, copied, missing, names)
                if rel is not None:
                    attrs[key] = rel
                    rewritten += 1
            out_spans.append({**span, "attributes": attrs})

        (staging / "spans.jsonl").write_text(
            "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in out_spans), encoding="utf-8"
        )

        verdicts = read_verdicts(resolved_state, attempt_ids=attempt_alias_ids(attempt_id, resolved_state))
        (staging / "verdicts.jsonl").write_text(
            "".join(json.dumps(asdict(v), ensure_ascii=False) + "\n" for v in verdicts), encoding="utf-8"
        )

        session_included = False
        if session_key:
            source = _session_source(session_key, workspace)
            if source.is_file():
                shutil.copyfile(source, staging / "session.jsonl")
                session_included = True

        manifest = {
            "format_version": BUNDLE_FORMAT_VERSION,
            "attempt_id": attempt_id,
            "session_key": session_key,
            "time_range": {"start": start, "end": end},
            "span_count": len(out_spans),
            "artifact_count": len(copied),
            "rewritten_artifact_paths": rewritten,
            "missing_artifacts": missing,
            "session_included": session_included,
            "verdict_count": len(verdicts),
            "raven_version": __version__,
            "environment": _collect_environment(),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        staging.rename(bundle_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    try:
        pin_attempt(attempt_id, reason="bundled", state_dir=resolved_state)
    except LookupError:
        # The attempt address vanished mid-pack (a concurrent split/merge);
        # protect exactly the traces that were bundled instead of leaving
        # them purgeable behind a dangling id.
        for member in dict.fromkeys(s.get("traceId") for s in out_spans if s.get("traceId")):
            pin(member, reason="bundled", state_dir=resolved_state)
    return bundle_dir
