"""Availability checks for third-party subagents: a free probe, and (Task 3) a test.

Two tiers, deliberately separated by cost. ``probe_one`` spawns no agent process
and sends no chat completion, so the web UI can run it for every configured
agent and every preset on page load.

Neither tier raises. A page whose whole job is reporting availability must not
be blanked by one unreachable endpoint, so every failure is a return value.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import aiohttp
from loguru import logger

from raven.agent.subagent.backends import build_third_party_backend
from raven.agent.subagent.backends.env import login_shell_env
from raven.agent.subagent.instances import InstanceRegistry
from raven.agent.subagent.test_state import LastTest

ProbeStatus = Literal["ready", "attention", "missing", "unknown"]
Source = Literal["config", "preset"]

PROBE_PROMPT = "Reply with exactly: PONG"

# A probe is paid for in page-load latency, so it is bounded tightly -- unlike a
# real dispatch, which is deliberately unbounded.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)
_BODY_SNIPPET = 200

# A test must be bounded even though `timeout` defaults to None (no automatic
# limit): an unbounded button is a hang. Generous for a one-word answer, so a
# genuinely slow agent can report a test timeout while working for real tasks.
TEST_TIMEOUT_SECONDS = 120
_DETAIL_CAP = 2000


@dataclass(frozen=True)
class ProbeResult:
    """One subagent's free availability verdict.

    ``target`` is what was checked: the resolved absolute path for a cli agent
    that was found, the bare ``argv[0]`` as written when it was not (so a
    ``missing`` result still names it), or the base URL for an openai agent.
    When ``argv[0]`` contains a path separator, ``shutil.which`` resolves it
    relative to the gateway process's own cwd, while a real spawn resolves it
    relative to ``cfg.cwd or workspace`` -- so a relative command can resolve
    to a different place than what the probe checked.
    """

    name: str
    source: Source
    kind: str
    status: ProbeStatus
    detail: str
    target: str
    elapsed_ms: int
    last_test: LastTest | None = None
    """The remembered outcome of an explicit test, when one is still valid for this
    exact configuration. Attached by ``probe_all``; ``probe.py`` never reads or
    writes the store itself, which keeps file I/O out of this module."""

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "target": self.target,
            "elapsedMs": self.elapsed_ms,
            "lastTest": None
            if self.last_test is None
            else {
                "ok": self.last_test.ok,
                "detail": self.last_test.detail,
                "testedAtMs": self.last_test.tested_at_ms,
            },
        }


@dataclass(frozen=True)
class TestResult:
    """One subagent's explicit verdict.

    ``kind`` is ``None`` only for the unknown-name failure, which has no config
    to read a kind from. ``reply`` is the agent's own answer for a cli test and
    always ``None`` for openai, which sends no completion.
    """

    name: str
    source: Source
    kind: str | None
    ok: bool
    detail: str
    reply: str | None
    elapsed_ms: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "kind": self.kind,
            "ok": self.ok,
            "detail": self.detail,
            "reply": self.reply,
            "elapsedMs": self.elapsed_ms,
        }


def _login_path() -> str:
    return login_shell_env().get("PATH", "")


async def _captured_login_path() -> str:
    """The login shell's PATH, or ``""`` if it cannot be captured.

    Guarded because both entry points promise never to raise, and a bare
    ``to_thread(_login_path)`` in ``probe_all`` would propagate out of the
    batch and blank the page - the one failure this module exists to prevent.
    """
    try:
        return await asyncio.to_thread(_login_path)
    except Exception as exc:  # noqa: BLE001 - no PATH is a degraded probe, not a crash
        logger.warning("login shell PATH capture failed ({}); cli probes will report missing", exc)
        return ""


def _probe_cli(cfg: Any, *, source: Source, path: str | None) -> ProbeResult:
    def done(status: ProbeStatus, detail: str, target: str = "") -> ProbeResult:
        return ProbeResult(cfg.name, source, "cli", status, detail, target, 0)

    command = (cfg.command or "").strip()
    if not command:
        return done("unknown", "command is empty")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return done("unknown", f"command cannot be parsed: {exc}")
    if not argv:
        return done("unknown", "command is empty")
    exe = argv[0]
    if "{" in exe:
        return done("unknown", f"the command's first token is a placeholder ({exe})", exe)
    # CliAgentBackend._exec builds the child's env as {**login_shell_env(), **self.env}
    # (cli_agent.py:139-140), so a configured PATH overrides the login shell's for a
    # real spawn; resolving against the login PATH alone here would report an agent
    # missing that a real spawn finds just fine.
    cfg_path = (getattr(cfg, "env", None) or {}).get("PATH")
    resolved = shutil.which(exe, path=cfg_path or path)
    if resolved is None:
        return done("missing", f"{exe} is not on the login shell PATH", exe)
    return done("ready", f"installed at {resolved}", resolved)


def _model_ids(payload: Any) -> list[str] | None:
    """``data[].id`` from an OpenAI model list, or ``None`` if that is not the shape."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    return [item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)]


async def _probe_openai(cfg: Any, *, source: Source) -> ProbeResult:
    started = time.monotonic()
    base_url = (cfg.base_url or "").strip()

    def done(status: ProbeStatus, detail: str) -> ProbeResult:
        return ProbeResult(
            cfg.name, source, "openai", status, detail, base_url, int((time.monotonic() - started) * 1000)
        )

    if not base_url:
        return done("unknown", "no base URL configured")
    has_key = bool((cfg.api_key or "").strip())
    if not has_key and source == "preset":
        # A preset is a template, so a request certain to 401 tells nobody
        # anything. A *configured* entry with no key still gets one: a keyless
        # endpoint (a local vLLM) is legitimate, and short-circuiting would
        # report a working agent as broken.
        return done("attention", "api key not set")
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if has_key else {}
    try:
        # trust_env mirrors OpenAIApiBackend.run: where the provider is only
        # reachable through a proxy, a direct attempt returns whatever the
        # provider says to an unexpected origin (mirothinker: HTTP 451), so a
        # probe without it would report a working agent unreachable.
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT, trust_env=True) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status in (401, 403):
                    suffix = "not set or rejected" if not has_key else "rejected"
                    return done("attention", f"api key {suffix} (HTTP {resp.status})")
                if resp.status == 404:
                    return done(
                        "attention",
                        "reachable, but this endpoint has no /models (HTTP 404); key and model are unverified",
                    )
                if resp.status != 200:
                    body = (await resp.text())[:_BODY_SNIPPET].strip()
                    return done("attention", f"HTTP {resp.status}: {body}")
                # content_type=None: an endpoint that answers with text/plain is
                # still readable, and a ContentTypeError here would be reported
                # as unreachable, which it is not.
                payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        return done("missing", f"unreachable: {exc}")
    except Exception as exc:  # noqa: BLE001 - an unreadable body is a finding, not a crash
        return done("attention", f"reachable, but the model list could not be read: {exc}")

    ids = _model_ids(payload)
    if ids is None:
        return done("ready", "reachable; key accepted; model list unavailable, so the model name is unverified")
    model = (cfg.model or "").strip()
    if model and model not in ids:
        return done("attention", f"reachable, but model {model} is not in its list ({len(ids)} available)")
    return done("ready", f"reachable; key accepted; model available ({len(ids)} listed)")


async def probe_one(cfg: Any, *, source: Source, path: str | None = None) -> ProbeResult:
    """Free availability check for one subagent config. Never raises.

    ``path`` is the PATH a cli probe resolves against; omitted, it is captured
    from the login shell. Pass it when probing a batch.
    """
    try:
        if getattr(cfg, "kind", None) == "openai":
            return await _probe_openai(cfg, source=source)
        if path is None:
            path = await _captured_login_path()
        return _probe_cli(cfg, source=source, path=path)
    except Exception as exc:  # noqa: BLE001 - a raising probe would blank the page
        name = getattr(cfg, "name", "") or ""
        logger.warning("subagent probe for {!r} failed unexpectedly: {}", name, exc)
        return ProbeResult(name, source, getattr(cfg, "kind", "") or "", "unknown", f"probe failed: {exc}", "", 0)


async def probe_all(
    entries: Sequence[tuple[Any, Source]],
    *,
    verdicts: Mapping[str, LastTest] | None = None,
) -> list[ProbeResult]:
    """Probe many configs concurrently, returning results in the order given.

    The login-shell PATH is captured once here rather than inside each cli
    probe: ``login_shell_env`` shells out to ``bash -lic`` and can block for
    real seconds on its first call, which would serialise the whole batch.

    ``verdicts`` is keyed ``"source:name"``; a missing entry simply leaves
    ``last_test`` as ``None``, so the caller needs no per-result branching.
    """
    path = await _captured_login_path()
    results = list(await asyncio.gather(*(probe_one(cfg, source=src, path=path) for cfg, src in entries)))
    if verdicts is None:
        return results
    return [replace(r, last_test=verdicts.get(f"{r.source}:{r.name}")) for r in results]


async def run_test(cfg: Any, *, source: Source) -> TestResult:
    """Reach a real availability verdict for one subagent. Never raises.

    cli: dispatches ``PROBE_PROMPT`` through the same backend a real spawn uses,
    so the run exercises argv construction, the login-shell environment, the
    transcript parser and the CLI's own auth. **This spends the agent's own
    quota**, which is why it is only ever reached by an explicit request.

    openai: runs the same free ``/models`` probe and sends no completion, so
    nothing is billed.

    The verdict is "exited 0 and returned something", not "the reply contains
    PONG": asserting content would flake on an agent that answers with a
    preamble, while an empty reply is itself the signal (openclaw answering its
    workspace bootstrap instead of the task).
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    probe = await probe_one(cfg, source=source)
    kind = getattr(cfg, "kind", None)
    if kind == "openai":
        return TestResult(cfg.name, source, "openai", probe.status == "ready", probe.detail, None, elapsed())
    if probe.status != "ready":
        return TestResult(cfg.name, source, "cli", False, probe.detail, None, elapsed())

    try:
        with tempfile.TemporaryDirectory(prefix="raven_subagent_test_") as tmp:
            backend = build_third_party_backend(
                cfg,
                # A stateful create commits a handle binding; a test must not leave
                # that in the file the running gateway reads.
                registry=InstanceRegistry(path=Path(tmp) / "probe_instances.json"),
                timeout=min(getattr(cfg, "timeout", None) or TEST_TIMEOUT_SECONDS, TEST_TIMEOUT_SECONDS),
            )
            reply = await backend.run(
                PROBE_PROMPT, task_id=f"test-{uuid.uuid4().hex[:8]}", workspace=Path(tmp), executor=None
            )
    except Exception as exc:  # noqa: BLE001 - every failure is the answer, not a crash
        # Covers an unwritable TMPDIR or an unrecognised kind too, neither reachable
        # through the validated config path today but still owed by "never raises".
        return TestResult(cfg.name, source, "cli", False, str(exc)[:_DETAIL_CAP], None, elapsed())

    text = (reply or "").strip()
    if not text:
        return TestResult(cfg.name, source, "cli", False, "the command exited 0 but returned nothing", None, elapsed())
    return TestResult(cfg.name, source, "cli", True, "the agent ran and replied", text[:_DETAIL_CAP], elapsed())


__all__ = [
    "PROBE_PROMPT",
    "ProbeResult",
    "ProbeStatus",
    "Source",
    "TEST_TIMEOUT_SECONDS",
    "TestResult",
    "probe_all",
    "probe_one",
    "run_test",
]
