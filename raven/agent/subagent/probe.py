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

from raven.agent.subagent.backends import acp_snapshot_for, build_third_party_backend
from raven.agent.subagent.backends.env import login_shell_env
from raven.agent.subagent.instances import InstanceRegistry
from raven.agent.subagent.presets import install_hint_for, shim_requirement_for, third_party_subagent_presets
from raven.agent.subagent.probe_state import LastTest

ProbeStatus = Literal["ready", "attention", "missing", "unknown"]
Source = Literal["config", "preset", "vendored"]

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

_ENABLE_PING_TIMEOUT_SECONDS = 60
"""How long the readiness prompt may take before it is called a failure.

Half the explicit-Test budget, because this one is on the path of a settings
switch a person is waiting on. Two measured agents hang rather than refuse, so
the cap is what turns "no answer" into an answer.

Handed to the backend as well as to the ``wait_for`` around it, so one number
means one thing: a backend allowed the longer Test budget could only ever be
cancelled from outside, never reach its own timeout and describe the failure.
"""


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
    to read a kind from. ``reply`` is the agent's own answer for a cli test, the
    model menu its handshake advertised for acp, and ``None`` for openai, whose
    prompt is sent but whose answer is not carried back.
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


def _missing_exe_detail(cfg: Any, exe: str) -> str:
    """Why a row cannot start, plus what to install when this repo knows.

    ``shutil.which`` can only answer with the executable it looked for, and that
    is not the package that ships it -- ``qodercli`` does not spell
    ``@qoder-ai/qodercli``. This probe is the surface a person reads when a row is
    absent, so it is where the install belongs. It previously sat in the roster
    ``description``, whose only reader is the dispatching model, which cannot act
    on an install at all.

    Both callers share this rather than spelling the message twice: the two
    absent-executable branches are the same event on two transports, and one of
    them growing the hint alone is the shape a reader would trust and be wrong
    about on the other.
    """
    return _missing_detail(exe, install_hint_for(cfg))


def _missing_detail(exe: str, hint: str | None) -> str:
    detail = f"{exe} is not on the login shell PATH"
    return f"{detail}; install with {hint}" if hint else detail


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
        return done("missing", _missing_exe_detail(cfg, exe), exe)
    return done("ready", f"installed at {resolved}", resolved)


def _probe_acp(cfg: Any, *, source: Source, path: str | None) -> ProbeResult:
    """Free availability check for an acp agent: on PATH, and already verified?

    Both halves are needed, and the second is the point. ``shutil.which`` on the
    launch command answers "is the executable there", which for an ACP server is a
    far weaker claim than for a cli agent: the process existing says nothing about
    whether it speaks the protocol, has a usable credential, or (for a bridge like
    ``openclaw acp``) can reach the thing it bridges to. Reporting ``ready`` off
    ``which`` alone would be a green light for an agent that cannot run a task --
    so an unverified entry is ``attention``, with the recorded verdict taking over
    once one exists.

    For a shim-launched preset the executable asked after is the agent the shim
    drives, not ``argv[0]``: an ``npx`` command resolves on any machine with
    node, so on its own it would report Pi installed wherever ``pi`` is not, and
    the connect that follows would fail a minute later inside the adapter with
    the sentence this probe can say up front.
    """
    name = getattr(cfg, "name", "") or ""

    def done(status: ProbeStatus, detail: str, target: str = "") -> ProbeResult:
        return ProbeResult(name, source, "acp", status, detail, target, 0)

    command = (getattr(cfg, "command", None) or "").strip()
    if not command:
        return done("unknown", "command is empty")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return done("unknown", f"command cannot be parsed: {exc}")
    if not argv:
        return done("unknown", "command is empty")
    exe = argv[0]
    cfg_path = (getattr(cfg, "env", None) or {}).get("PATH")
    resolved = shutil.which(exe, path=cfg_path or path)
    if resolved is None:
        return done("missing", _missing_exe_detail(cfg, exe), exe)
    requirement = shim_requirement_for(cfg)
    if requirement is not None:
        agent_exe, install = requirement
        if shutil.which(agent_exe, path=cfg_path or path) is None:
            return done("missing", _missing_detail(agent_exe, install), agent_exe)

    snapshot = acp_snapshot_for(cfg)
    if snapshot is None:
        return done(
            "attention",
            f"installed at {resolved}, but its ACP capabilities have not been recorded yet -- run a test",
            resolved,
        )
    if snapshot.stale:
        # The capabilities are still used (see `SnapshotStore.load`); the status
        # is not. A verdict measured against a command, cwd or env the entry no
        # longer has is not evidence about the entry as it stands now, and this
        # row is the one surface that says so.
        return done(
            "attention",
            f"installed at {resolved}, but its launch config changed since the last test -- run a test",
            resolved,
        )
    if not getattr(snapshot, "model_menu_measured", True):
        # A row recorded before the menu was: its "ready" predates a capability
        # the sheet now draws from, and reporting it would put a disabled
        # "managed by itself" pill on an agent that may well offer a menu. The
        # boot-time backfill re-measures such rows; until one succeeds, the row
        # says what is missing rather than claiming a verdict it does not have.
        return done(
            "attention",
            f"installed at {resolved}, but its model menu has not been measured yet -- run a test",
            resolved,
        )
    return done(snapshot.status, snapshot.detail, resolved)


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
        kind = getattr(cfg, "kind", None)
        if kind == "builtin":
            # Nothing to reach: it is raven's own loop, in this process. There is no
            # command to resolve on PATH and no endpoint to call, so a probe could
            # only ever report "unknown" after spending its per-entry budget.
            return ProbeResult(getattr(cfg, "name", "") or "", source, "builtin", "ready", "in-process", "", 0)
        if kind == "openai":
            return await _probe_openai(cfg, source=source)
        if path is None:
            path = await _captured_login_path()
        if kind == "acp":
            return _probe_acp(cfg, source=source, path=path)
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

    acp: the same bar, reached the same way, after a free handshake that can
    refuse first. **Also spends the agent's own quota** -- see `_test_acp` for
    why the handshake alone could not stand in for it.

    openai: the same bar, after the free ``/models`` probe, which decides alone
    only when nothing is listening. **Otherwise spends the endpoint's own
    quota**, like the two above -- a reachable endpoint is settled by asking it,
    because the probe cannot tell a rejected key from an endpoint that simply
    serves no model list, and the second of those works.

    The verdict is "exited 0 and returned something", not "the reply contains
    PONG": asserting content would flake on an agent that answers with a
    preamble, while an empty reply is itself the signal (openclaw answering its
    workspace bootstrap instead of the task).
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    kind = getattr(cfg, "kind", None)
    if kind == "builtin":
        # Nothing to verify. A built-in agent is this process: there is no command
        # to launch, no endpoint to authenticate against, and no transcript parser
        # to exercise -- the three things a test exists to catch. Refused rather
        # than run, because falling through to the cli branch reported a failure
        # whose detail read "unknown third-party subagent kind" and whose `kind`
        # said "cli", and the caller then *recorded* that verdict, so the row
        # showed a failed test forever.
        return TestResult(
            getattr(cfg, "name", ""),
            source,
            "builtin",
            False,
            "a built-in agent runs in this process; there is nothing to test",
            None,
            elapsed(),
        )
    if kind == "acp":
        return await _test_acp(cfg, source=source, elapsed=elapsed)
    probe = await probe_one(cfg, source=source)
    if kind == "openai":
        # The free probe runs first but decides alone only when it has proved
        # there is nothing to send to. `/models` is optional -- the backend only
        # ever POSTs `/chat/completions` -- so "reachable, but no model list" is
        # a working agent, and it shares the `attention` verdict with a rejected
        # key. Vetoing on that verdict would fail a Test that Connect accepts,
        # which is the disagreement this whole gate exists to remove, so
        # anything reachable is settled by asking it. A rejected key then costs
        # one POST the endpoint refuses before it infers anything.
        if probe.status == "missing":
            return TestResult(cfg.name, source, "openai", False, probe.detail, None, elapsed())
        answered = await ping_agent(cfg)
        detail = probe.detail if answered.ok else f"{answered.detail}; {probe.detail}"
        return TestResult(cfg.name, source, "openai", answered.ok, detail, None, elapsed())
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


@dataclass(frozen=True)
class PingResult:
    """Whether one agent answered a prompt, and what to tell the operator if not."""

    ok: bool
    detail: str


async def ping_agent(cfg: Any) -> PingResult:
    """Send one prompt and report whether the agent answered. Never raises.

    The second of two layers. The first -- `which` plus, for acp, the handshake --
    answers whether the agent is installed and has ACP switched on, and it is free.
    It cannot answer whether the agent can *work*: ACP has no authenticated-state
    field, so an agent that defers its credential to the first model call opens a
    session happily and fails afterwards. Six of thirteen registry agents measured
    on 2026-09-07 did exactly that.

    So this spends one call on the agent's own quota, which is why it is reached
    only from an explicit switch-on and never from a listing.

    It runs on a pool of its own, closed on the way out. The shared pool keys a
    connection on its launch arguments and ``cwd`` falls back to the caller's
    workspace, which here is a fresh temporary directory per call -- so a ping on
    the shared pool never matches a held connection and ``acquire`` retires every
    connection of that agent before opening its own. Measured 2026-09-20 against
    a live adapter: a second Connect pressed while the first was still running
    took the first one's connection down, and the first came back "did not answer
    a test message" for a failure raven had caused; a ping fired while that agent
    was serving a real run would have taken that run's connection with it.
    """
    # Function-level like the rest of this module's acp imports: the client family
    # is future shelf cargo and must not be named at import time.
    from raven.acp_client import pool as acp_pool

    pool = acp_pool.AcpConnectionPool()
    try:
        with tempfile.TemporaryDirectory(prefix="raven_subagent_ping_") as tmp:
            backend = build_third_party_backend(
                cfg,
                # A stateful create commits a handle binding; a ping must not leave
                # that in the file the running gateway reads.
                registry=InstanceRegistry(path=Path(tmp) / "ping_instances.json"),
                timeout=min(
                    getattr(cfg, "timeout", None) or _ENABLE_PING_TIMEOUT_SECONDS,
                    _ENABLE_PING_TIMEOUT_SECONDS,
                ),
                ready_timeout_ms=min(
                    getattr(cfg, "ready_timeout_ms", None) or _ENABLE_PING_TIMEOUT_SECONDS * 1000,
                    _ENABLE_PING_TIMEOUT_SECONDS * 1000,
                ),
                pool=pool,
            )
            reply = await asyncio.wait_for(
                backend.run(PROBE_PROMPT, task_id=f"ping-{uuid.uuid4().hex[:8]}", workspace=Path(tmp), executor=None),
                timeout=_ENABLE_PING_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        return PingResult(False, f"it did not answer within {_ENABLE_PING_TIMEOUT_SECONDS}s")
    except Exception as exc:  # noqa: BLE001 - every failure is the answer, not a crash
        return PingResult(False, str(exc)[:_DETAIL_CAP])
    finally:
        # The pool is this call's alone, so nothing else will ever close it, and a
        # pool left open holds the child process it launched for the rest of the
        # gateway's life -- one per press.
        await pool.close_all()

    text = (reply or "").strip()
    if not text:
        return PingResult(False, "it started and then answered nothing")
    return PingResult(True, "it ran and replied")


async def _test_acp(cfg: Any, *, source: Source, elapsed: Any) -> TestResult:
    """Run an acp agent to reach its verdict, and remember what its handshake said.

    Two measurements, in that order, because they answer different questions.

    The handshake -- ``initialize`` plus ``session/new`` -- answers whether the
    agent is installed, speaks ACP and will open a session, and it is free. So it
    goes first, and a refusal there is the verdict: no prompt is spent on an
    agent that cannot take one.

    What it cannot answer is whether the agent *works*. ACP carries no
    authenticated-state field, so one that defers its credential to the first
    model call opens a session happily and fails afterwards; six of thirteen
    registry agents measured on 2026-09-07 did exactly that, and this test called
    every one of them ready. So the verdict is the agent's own answer to
    ``PROBE_PROMPT`` -- the bar `cli` has always had, and the one the enable gate
    already holds a connect to, which is what makes a green Test and a successful
    Connect mean the same thing.

    Two consequences worth stating. It **spends one call on the agent's own
    quota**, which this test did not before; it is reached only from an explicit
    press, since `subagents.test` is `run_test`'s one caller, and never from a
    listing or the boot backfill. And it launches the agent twice, because the
    handshake and the ping have different reuse rules -- the ping runs on a pool
    of its own so that it cannot retire a connection a real run is holding.

    Always connects live, deliberately skipping the probe: the probe reports the
    *recorded* snapshot, so consulting it here would replay a stale failure (one
    slow first `npx` download) as this test's verdict forever. A truly absent
    executable still fails fast -- launch raises before any timeout waits.
    """
    # A preset's snapshot is recorded too, and has to be: the page draws a
    # preset row's verdict from it -- a recorded credential refusal is what puts
    # "Unauthorized" there -- so Test is that row's only way back. The store
    # keys on a fingerprint of the launch fields, so a record under a preset's
    # name is returned only to a config that launches the same way. Recorded off
    # the handshake either way, and never off the ping: the ping answers one
    # minute, while the menu and the statefulness it holds are launch facts.
    snapshot = await record_capabilities(cfg)
    reply = ", ".join(snapshot.available_models[:5]) or None
    if not snapshot.usable:
        return TestResult(cfg.name, source, "acp", False, snapshot.detail, reply, elapsed())
    answered = await ping_agent(cfg)
    # Verdict first on a failure, the handshake after it: "it connected and then
    # said nothing" is what went wrong, and the half that succeeded is the
    # context that separates it from an agent that is not installed.
    detail = snapshot.detail if answered.ok else f"{answered.detail}; {snapshot.detail}"
    return TestResult(cfg.name, source, "acp", answered.ok, detail, reply, elapsed())


async def record_capabilities(cfg: Any) -> Any:
    """Measure an acp entry's capabilities live and write them down; the snapshot.

    The one writer the manual test and the connect share. A connect proves the
    agent answers (``ping_agent``) but records nothing, so until now a row
    connected from the page stayed "capabilities not recorded -- run a test",
    stateless and menuless, until someone pressed Test or the gateway restarted
    into the boot backfill: no ``instance`` for it, no model pill, an attention
    dot on an agent that had just replied. The handshake this records costs no
    tokens, so the connect can afford it.
    """
    from raven.acp_client.capabilities import SnapshotStore, verify_agent

    snapshot = await verify_agent(cfg)
    _note_menu_re_measured(snapshot, getattr(cfg, "name", "") or "")
    store = SnapshotStore()
    store.record(_test_record(snapshot, store.load([cfg], allow_stale=True).get(cfg.name)))
    return snapshot


#: Raven's own rows this process has already re-measured for a menu and been
#: given none again. The reason below is the one re-measure reason nothing
#: invalidates, so it is the one that has to remember it has been spent.
_MENULESS_OWN_RE_MEASURED: set[str] = set()


def _measured_no_menu(snapshot: Any) -> bool:
    """A ready snapshot of one of raven's own agents that advertised no model."""
    return (
        snapshot is not None
        and getattr(snapshot, "agent_name", "") == "raven"
        and getattr(snapshot, "status", "") == "ready"
        and bool(getattr(snapshot, "model_menu_measured", False))
        and not getattr(snapshot, "model_choices", ())
    )


def _host_has_a_usable_provider() -> bool:
    """Does raven itself hold one provider credential?

    ``credential_status`` with ``include_external``, which is the predicate the
    model picker's own "configured" flag reads (``Config._provider_is_configured``):
    a sign-in lives in a token file, and asking without it reports every OAuth
    vendor usable on a host that has never been connected to anything -- exactly
    the host this bound is here to spare.
    """
    from raven.config.loader import load_config
    from raven.providers.auth import credential_status
    from raven.providers.registry import find_by_name

    providers = load_config().providers
    names = [*type(providers).model_fields, *(providers.model_extra or {})]
    sections = ((name, providers.get(name)) for name in names)
    return any(
        section is not None and credential_status(name, section, spec=find_by_name(name), include_external=True).ok
        for name, section in sections
    )


def _own_row_missing_its_menu(snapshot: Any, name: str) -> bool:
    """A ready snapshot of one of raven's own agents that measured no model menu,
    worth spending a handshake on again.

    Raven's own acp agents run on this raven's provider catalogue, so a menu
    measured empty there is a handshake taken before that catalogue reached
    them, not a fact about the agent -- and nothing invalidates it: the launch
    config it was measured against has not moved, so the row would go on
    offering nothing for as long as the file survives. Only raven's own: a third
    party that really offers none would be relaunched at every boot to be told
    so again.

    Twice bounded, because a child raven genuinely advertises no model while the
    catalogue is empty (``acp.config_options``), and this reason re-arms itself
    on the record it writes. So: only once this raven has a credential of its
    own, which is the whole premise of the fallback, and only once per row per
    process, so a boot that is told "none" again does not go on paying for the
    same answer at every connect after it.
    """
    if not _measured_no_menu(snapshot) or name in _MENULESS_OWN_RE_MEASURED:
        return False
    return _host_has_a_usable_provider()


def _note_menu_re_measured(snapshot: Any, name: str) -> None:
    """Spend this row's one re-measure when the live answer is menuless again."""
    if _measured_no_menu(snapshot):
        _MENULESS_OWN_RE_MEASURED.add(name)


def capabilities_wanted(cfg: Any) -> bool:
    """Does this acp entry lack a fresh, complete capability record?

    The same four cases the boot backfill re-measures: no snapshot, one whose
    launch config has changed, one written before the model menu was recorded,
    or one of raven's own whose menu came back empty -- that last one bounded as
    ``_own_row_missing_its_menu`` bounds it. A row with a complete record keeps
    it -- a connect must not spend a handshake re-measuring what is already
    known.
    """
    snapshot = acp_snapshot_for(cfg)
    if snapshot is None or snapshot.stale or not getattr(snapshot, "model_menu_measured", True):
        return True
    return _own_row_missing_its_menu(snapshot, getattr(cfg, "name", "") or "")


def _test_record(snapshot: Any, previous: Any) -> Any:
    """What a manual test writes down: its verdict always, its capabilities only
    when it reached them.

    A verify that fails before ``session/new`` -- a cold shim start, a machine
    under load, a login that lapsed -- carries no menu and no statefulness, and
    recorded whole it would cost the row both until the next success, silently:
    the verdict is visible, the loss of the previous measurement is not. So an
    unusable result keeps the previous record's capabilities under its own
    status and detail. Same reasoning as ``SnapshotStore.load`` gives for a
    stale row: the old capabilities are the weaker claim and self-heal, since a
    session that really cannot be loaded fails at ``session/load`` and the
    backend starts fresh. ``previous`` is the last record for this agent, stale
    or not -- the roster reads a stale row's capabilities too, so a failed test
    after a config edit must not erase what it was trusting -- or ``None``,
    with nothing to keep. The record takes this test's fingerprint: it is a
    measurement of the config as it stands now, whatever it kept.
    """
    if snapshot.usable or previous is None:
        return snapshot
    return replace(
        previous,
        fingerprint=snapshot.fingerprint,
        stale=False,
        status=snapshot.status,
        detail=snapshot.detail,
        measured_at_ms=snapshot.measured_at_ms,
        elapsed_ms=snapshot.elapsed_ms,
    )


_SCHEDULED = False
"""One auto-verify per process. Manual Test runs and probe=true listings are
peer paths to this backfill, not causes to run it again."""

_VERIFY_TASKS: set[asyncio.Task] = set()
"""The running backfill task, if any. Kept by reference: asyncio holds only a
weak reference to a task it did not create, and an unrefed task can be collected
mid-run with a "Task was destroyed but it is pending!" warning at exit."""


class _PresetRow:
    """A registry-row shape around a preset, so one backfill serves both halves.

    The registry holds configured agents only. Everything the reader has not
    connected yet is therefore invisible to it -- which is exactly the set whose
    Connect button is about to promise something the agent will refuse.
    """

    __slots__ = ("config", "enabled", "kind", "name")

    def __init__(self, config: Any) -> None:
        self.config = config
        self.name = getattr(config, "name", "")
        self.kind = "acp"
        self.enabled = False


def _unconfigured_acp_preset_rows(configured: set[str], *, path: str | None) -> list[Any]:
    """Shipped acp presets this machine could actually answer for.

    Three filters, each for its own reason. A configured name is the registry
    half's already. A non-acp preset has no handshake to record -- an openai
    row's credential is settled by the free ``/models`` probe and a cli row is
    never handshaken at all. And a command that does not resolve is one whose
    verdict the free probe already reached for nothing: launching it to learn
    the same thing is the cost this filter exists to refuse.

    ``path`` is the login shell's, captured once by the caller -- the same PATH
    ``_probe_acp`` resolves against. Reading this process's instead would answer
    a different question from the one the row on screen was answered with, and
    skip an agent the page is reporting as installed.
    """
    from raven.config.schema import SubagentsConfig

    wanted = [
        preset
        for preset in third_party_subagent_presets()
        if preset.get("kind") == "acp" and preset.get("name") not in configured
    ]
    here = []
    for preset in wanted:
        try:
            argv = shlex.split((preset.get("command") or "").strip())
        except ValueError:
            continue
        if argv and shutil.which(argv[0], path=path or None) is not None:
            here.append(preset)
    if not here:
        return []
    try:
        # Validated as one list, the way the row builder reads the same table:
        # these are shipped entries, so a failure here is a packaging fault, not
        # a user's typo, and it must not take the boot with it.
        return [_PresetRow(cfg) for cfg in SubagentsConfig(agents=here).agents]
    except Exception as exc:  # noqa: BLE001
        logger.warning("acp presets cannot be read for auto-verify: {}", exc)
        return []


def schedule_snapshot_verification(manager: Any) -> asyncio.Task | None:
    """Verify every enabled acp agent that has no fresh capability snapshot.

    Before this, the snapshot -- the record an acp row's ``stateful`` and
    ``can_resume`` are read from -- was written only by the UI Test button and a
    ``probe: true`` listing, so a fresh install reported zero stateful rows and
    the ``/new-instance`` picker hid those agents until someone opened a page
    and clicked. Backgrounded so a slow adapter (a first ``npx`` fetch) cannot
    delay startup; fire-and-forget, because a failure leaves the row stateless
    with the snapshot's own detail rather than breaking the boot. Verification
    costs no model tokens (handshake plus a throwaway ``session/new``).
    """
    global _SCHEDULED
    if _SCHEDULED:
        return None
    registry = getattr(manager, "registry", None)
    if registry is None:
        # A mounted stack's subagents implementation is not the manager this
        # backfill was written against (tests substitute a stub without one);
        # scheduling nothing is the correct degradation, not a missing feature.
        return None
    _SCHEDULED = True
    live = list(registry.rows())
    rows = [row for row in live if getattr(row, "kind", None) == "acp" and getattr(row, "enabled", False)]
    # No early return on an empty list any more: the shipped presets are the
    # other half of the work, and whether any of them is on this machine cannot
    # be answered here -- that needs the login shell's PATH, and this is the
    # synchronous side.
    task = asyncio.create_task(
        _verify_missing_snapshots(manager, rows, configured={getattr(row, "name", "") for row in live})
    )
    _VERIFY_TASKS.add(task)
    task.add_done_callback(_VERIFY_TASKS.discard)
    return task


async def _verify_missing_snapshots(manager: Any, rows: list[Any], *, configured: set[str] | None = None) -> None:
    """One verification per missing or stale row, sequentially, never raising.

    Sequential, not concurrent: every acp verify spawns a child process, and a
    machine with a slow adapter among several would otherwise launch them all at
    once at boot. After the run the table is refreshed so the rows rebuilt at
    startup pick up the snapshots this task just recorded -- the alternative is
    a roster that reports an agent stateful and dispatches it stateless until
    the next restart or hot-apply.
    """
    from raven.acp_client.capabilities import SnapshotStore, verify_agent

    # After the configured rows, never before: those are the ones a run can
    # dispatch to this minute, and a slow preset adapter ahead of them would hold
    # the roster's own capabilities back behind an agent nobody has asked for yet.
    if configured is not None:
        rows = [*rows, *_unconfigured_acp_preset_rows(configured, path=await _captured_login_path())]
    store = SnapshotStore()
    # Optional: a caller's own stand-in (tests substitute a bare `record`-only
    # object) may not carry it, and its absence must not itself force a
    # re-verify -- see `SnapshotStore.has_model_menu` for what it detects.
    has_model_menu = getattr(store, "has_model_menu", None)
    recorded = False
    for row in rows:
        cfg = getattr(row, "config", None)
        if cfg is None:
            continue
        try:
            snapshot = acp_snapshot_for(cfg)
            name = getattr(row, "name", "") or ""
            outdated_menu = snapshot is not None and has_model_menu is not None and not has_model_menu(name)
            # Three reasons to re-measure besides staleness: a record written
            # from before the model menu (above), a credential refusal, and one
            # of raven's own that came back with an empty menu. Staleness asks
            # whether the launch config moved, and neither signing in nor
            # configuring a provider moves it -- so a recorded refusal never goes
            # stale, and the row it came from would go on saying "Unauthorized"
            # across every restart after the sign-in that cured it.
            refused = getattr(snapshot, "needs_auth", False)
            menuless_own = _own_row_missing_its_menu(snapshot, name)
            if snapshot is not None and not snapshot.stale and not outdated_menu and not refused and not menuless_own:
                continue
            result = await verify_agent(cfg)
            _note_menu_re_measured(result, name)
            # A pass, or a refusal the agent explained. Every other failure stays
            # unrecorded on purpose: a timeout or a crashed adapter is a fact
            # about this minute, and a snapshot of one would label a working
            # agent broken until somebody happened to press Test.
            if result.status == "ready" or getattr(result, "needs_auth", False):
                store.record(result)
                recorded = True
            logger.info("acp agent {!r}: auto-verify {}", name, result.status)
        except Exception as exc:  # noqa: BLE001 - a failed verify must not sink the rest
            logger.warning("acp agent {!r}: auto-verify failed: {}", getattr(row, "name", ""), exc)
    if recorded:
        refresh = getattr(manager, "refresh_agents", None)
        if refresh is not None:
            try:
                refresh()
            except Exception as exc:  # noqa: BLE001 - a refresh failure is not worth the boot
                logger.warning("acp: refreshing the agent table after auto-verify failed: {}", exc)


__all__ = [
    "capabilities_wanted",
    "record_capabilities",
    "PROBE_PROMPT",
    "PingResult",
    "ProbeResult",
    "ProbeStatus",
    "Source",
    "TEST_TIMEOUT_SECONDS",
    "TestResult",
    "ping_agent",
    "probe_all",
    "probe_one",
    "run_test",
    "schedule_snapshot_verification",
]
