# Subagent availability probe and test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show on `/subagents` whether each subagent is actually usable, add a per-agent Test that reaches a real verdict, and stop advertising the two openai capabilities that no mechanism can deliver.

**Architecture:** One new backend module (`raven/agent/subagent/probe.py`) holds both cost tiers: a free `probe_one` (cli = `which` on the login-shell PATH, openai = `GET {baseUrl}/models`) and an explicit `run_test` (cli = a real one-word dispatch through the same backend a spawn uses, openai = the same free call, never a completion). Tasks 1-3 build it, Task 4 exposes it over RPC + REST, Tasks 5-8 render it and remove the two unsupported fields.

**Tech Stack:** Python 3.13 + pydantic v2 + aiohttp + pytest (`uv run`); React 19 + Vite + Tailwind v4 + shadcn/Radix + i18next (`pnpm`, from `ui-webui/`).

**Spec:** `docs/specs/2026-08-06-subagents-probe-and-test-design.md`. Read the "Ground truth measured before designing" section before Task 1 - the probe targets `/models` rather than the base URL because the base URL answers `404`.

## Global Constraints

- Branch is `feat/subagents_preset_config` (already checked out). Do not commit on `main`.
- Committing on this feature branch is authorized for this plan's execution: one commit per task. Never commit on `main`, never `git push`, never `git commit --amend` (a fix round is a new commit, per `AGENTS.md` section 3.4).
- Commit messages: Conventional Commits, all-English, ASCII-only, header <= 100 chars, and a `Co-authored-by: Claude (<model-id>) <noreply@anthropic.com>` trailer. Per `AGENTS.md` section 3.3, `<model-id>` is the **actual model writing the commit**, not a fixed string: that is what keeps per-model contribution distinguishable, and this branch already carries a mix (`claude-sonnet-5`, `claude-opus-5`, `claude-haiku-4-5`). The sample messages below spell `claude-opus-5`; substitute your own model id.
- Python package manager is `uv` only. Never `pip`. Run tests as `uv run pytest`.
- JS package manager is `pnpm` only, run from `ui-webui/`.
- Code comments: English, and only where the logic is non-obvious or a constraint is hidden. Do not annotate edits.
- Prettier for the frontend is tabs, width 4, single quotes, semicolons, print width 100.
- i18n: edit `src/i18n/locales/{en,zh}.json` with targeted edits only. Never rewrite the file with `json.dump` - it reformats compact objects into diff noise.
- **There is no JS unit-test runner in this repo.** For frontend tasks the gate is `pnpm -C frontend lint` (0 errors; 17 pre-existing warnings are expected) plus `pnpm -C frontend build`. Do not invent a test framework.
- `pytest` runs with `asyncio_mode = "auto"`, so an `async def test_*` needs no decorator.
- Two tests fail at this branch's base and are **not** yours to fix: `tests/test_cli_theme.py::test_bold_accent_renders_styled_not_bare` and `tests/test_skill_ops.py::test_read_local_body_by_name`.
- The status vocabulary is exactly `ready` | `attention` | `missing` | `unknown`. Do not add a fifth.
- Nothing in this plan may send a chat completion to an openai-kind endpoint. That is the user's explicit cost constraint.

---

### Task 1: `probe.py` - the free probe

The zero-cost tier. No subprocess, no completion, and it never raises: the page runs this for every configured agent and every preset on load, so one unreachable endpoint must not blank it.

**Files:**
- Create: `raven/agent/subagent/probe.py`
- Test: `tests/test_subagent_probe.py`

**Interfaces:**
- Consumes: `login_shell_env()` from `raven/agent/subagent/backends/env.py`.
- Produces, for Tasks 3 and 4:
  - `ProbeStatus = Literal["ready", "attention", "missing", "unknown"]`
  - `Source = Literal["config", "preset"]`
  - `ProbeResult` (frozen dataclass): `name: str`, `source: Source`, `kind: str`, `status: ProbeStatus`, `detail: str`, `target: str`, `elapsed_ms: int`, plus `to_wire() -> dict`
  - `async probe_one(cfg: Any, *, source: Source, path: str | None = None) -> ProbeResult`
  - `async probe_all(entries: Sequence[tuple[Any, Source]]) -> list[ProbeResult]`
  - module constant `PROBE_PROMPT = "Reply with exactly: PONG"` (Task 3 uses it)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_subagent_probe.py`:

```python
"""Free availability probe for third-party subagents (cli PATH + openai /models)."""

from __future__ import annotations

import socket
from contextlib import closing
from pathlib import Path

import pytest
from aiohttp import web

import raven.agent.subagent.probe as probe_mod
from raven.agent.subagent.probe import probe_all, probe_one
from raven.config.schema import ThirdPartyCliSubagentConfig, ThirdPartyOpenAISubagentConfig


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cli(command: str, name: str = "agent") -> ThirdPartyCliSubagentConfig:
    return ThirdPartyCliSubagentConfig(name=name, command=command)


def _openai(base_url: str, model: str = "m1", api_key: str = "k") -> ThirdPartyOpenAISubagentConfig:
    return ThirdPartyOpenAISubagentConfig(name="api", base_url=base_url, model=model, api_key=api_key)


# --- cli probe -----------------------------------------------------------


async def test_cli_probe_reports_the_resolved_absolute_path(tmp_path: Path) -> None:
    exe = tmp_path / "faux-agent"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    res = await probe_one(_cli("faux-agent -p {prompt}"), source="config", path=str(tmp_path))
    assert res.status == "ready"
    assert res.target == str(exe)
    assert res.kind == "cli"
    assert res.source == "config"


async def test_cli_probe_reports_missing_and_still_names_what_it_looked_for(tmp_path: Path) -> None:
    res = await probe_one(_cli("definitely-not-installed {prompt}"), source="config", path=str(tmp_path))
    assert res.status == "missing"
    # A `missing` result still names the executable, or the user cannot tell
    # which of several tokens in the command was the one not found.
    assert res.target == "definitely-not-installed"
    assert "definitely-not-installed" in res.detail


async def test_cli_probe_reports_unparseable_command_as_unknown() -> None:
    res = await probe_one(_cli("claude -p 'unbalanced {prompt}"), source="config", path="/usr/bin")
    assert res.status == "unknown"
    assert "cannot be parsed" in res.detail


async def test_cli_probe_reports_a_placeholder_executable_as_unknown() -> None:
    # `{prompt}` as argv[0] names no executable, so "not installed" would be a
    # wrong diagnosis of a malformed command.
    res = await probe_one(_cli("{prompt} --go"), source="config", path="/usr/bin")
    assert res.status == "unknown"
    assert "placeholder" in res.detail


async def test_cli_probe_resolves_on_the_login_shell_path_not_ravens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point: `CliAgentBackend._exec` runs the child under
    # `login_shell_env()`, so a probe reading os.environ would report an agent
    # missing while real dispatches find it (codex lives on the login PATH only).
    exe = tmp_path / "login-only-agent"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    res = await probe_one(_cli("login-only-agent {prompt}"), source="config")
    assert res.status == "ready"


# --- openai probe --------------------------------------------------------


async def _serve(handler, route: str = "/v1/models", method: str = "GET") -> tuple[str, web.AppRunner, list[str]]:
    """Start a stub endpoint; returns (base_url, runner, recorded request paths)."""
    seen: list[str] = []

    async def wrapped(request: web.Request):
        seen.append(request.path)
        return await handler(request)

    port = _free_port()
    app = web.Application()
    app.router.add_route(method, route, wrapped)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return f"http://127.0.0.1:{port}/v1", runner, seen


async def test_openai_probe_ready_when_the_model_is_listed() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": "m1"}, {"id": "m2"}]})

    base, runner, _ = await _serve(handler)
    try:
        res = await probe_one(_openai(base, model="m1"), source="config")
        assert res.status == "ready"
        assert res.target == base
    finally:
        await runner.cleanup()


async def test_openai_probe_flags_a_model_absent_from_the_list() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "m1"}, {"id": "m2"}]})

    base, runner, _ = await _serve(handler)
    try:
        res = await probe_one(_openai(base, model="typo-model"), source="config")
        assert res.status == "attention"
        assert "typo-model" in res.detail
    finally:
        await runner.cleanup()


async def test_openai_probe_is_ready_but_honest_when_the_body_has_no_model_list() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    base, runner, _ = await _serve(handler)
    try:
        res = await probe_one(_openai(base), source="config")
        assert res.status == "ready"
        assert "unverified" in res.detail
    finally:
        await runner.cleanup()


async def test_openai_probe_reports_a_rejected_key() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"error": "invalid api key"}, status=401)

    base, runner, _ = await _serve(handler)
    try:
        res = await probe_one(_openai(base), source="config")
        assert res.status == "attention"
        assert "401" in res.detail
    finally:
        await runner.cleanup()


async def test_openai_probe_reports_a_missing_models_endpoint_as_unverified() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=404)

    base, runner, _ = await _serve(handler)
    try:
        res = await probe_one(_openai(base), source="config")
        assert res.status == "attention"
        assert "unverified" in res.detail
    finally:
        await runner.cleanup()


async def test_openai_probe_reports_an_unreachable_endpoint_as_missing() -> None:
    res = await probe_one(_openai(f"http://127.0.0.1:{_free_port()}/v1"), source="config")
    assert res.status == "missing"
    assert "unreachable" in res.detail


async def test_openai_probe_honors_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same guarantee `test_openai_backend_honors_env_proxy` pins for the backend,
    # and it must hold here too: aiohttp ignores HTTP_PROXY unless trust_env is
    # set, so on a host that can only reach the provider through a proxy a probe
    # without it would report a working agent unreachable.
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "m1"}]})

    proxy_base, runner, _ = await _serve(handler, route="/{tail:.*}")
    dead_port = _free_port()
    for var in ("no_proxy", "NO_PROXY", "https_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("http_proxy", proxy_base.removesuffix("/v1"))
    try:
        res = await probe_one(_openai(f"http://127.0.0.1:{dead_port}/v1", model="m1"), source="config")
        assert res.status == "ready"
    finally:
        await runner.cleanup()


async def test_openai_probe_sends_nothing_for_a_keyless_preset() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": []})

    base, runner, seen = await _serve(handler)
    try:
        res = await probe_one(_openai(base, api_key=""), source="preset")
        assert res.status == "attention"
        assert seen == []  # a template must not fire a request that is certain to 401
    finally:
        await runner.cleanup()


async def test_openai_probe_does_send_for_a_keyless_configured_entry() -> None:
    # A keyless endpoint is legitimate (a local vLLM), so short-circuiting a
    # configured entry would report a working agent as broken.
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "m1"}]})

    base, runner, seen = await _serve(handler)
    try:
        res = await probe_one(_openai(base, api_key=""), source="config")
        assert res.status == "ready"
        assert seen == ["/v1/models"]
    finally:
        await runner.cleanup()


# --- batch ---------------------------------------------------------------


async def test_probe_all_preserves_order_and_captures_the_path_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def counted() -> str:
        calls.append(1)
        return str(tmp_path)

    monkeypatch.setattr(probe_mod, "_login_path", counted)
    entries = [(_cli("a {prompt}", name="a"), "config"), (_cli("b {prompt}", name="b"), "preset")]
    results = await probe_all(entries)
    assert [r.name for r in results] == ["a", "b"]
    assert [r.source for r in results] == ["config", "preset"]
    # login_shell_env shells out to `bash -lic` and can block for seconds on its
    # first call; per-agent capture would serialise the whole batch behind it.
    assert len(calls) == 1


async def test_probe_result_wire_shape_is_camel_case(tmp_path: Path) -> None:
    res = await probe_one(_cli("nope {prompt}"), source="config", path=str(tmp_path))
    assert set(res.to_wire()) == {"name", "source", "kind", "status", "detail", "target", "elapsedMs"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_probe.py -q`
Expected: collection error - `ModuleNotFoundError: No module named 'raven.agent.subagent.probe'`.

- [ ] **Step 3: Write the implementation**

Create `raven/agent/subagent/probe.py`:

```python
"""Availability checks for third-party subagents: a free probe, and (Task 3) a test.

Two tiers, deliberately separated by cost. ``probe_one`` spawns no process and
sends no chat completion, so the web UI can run it for every configured agent
and every preset on page load.

Neither tier raises. A page whose whole job is reporting availability must not
be blanked by one unreachable endpoint, so every failure is a return value.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp
from loguru import logger

from raven.agent.subagent.backends.env import login_shell_env

ProbeStatus = Literal["ready", "attention", "missing", "unknown"]
Source = Literal["config", "preset"]

PROBE_PROMPT = "Reply with exactly: PONG"

# A probe is paid for in page-load latency, so it is bounded tightly -- unlike a
# real dispatch, which is deliberately unbounded.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)
_BODY_SNIPPET = 200


@dataclass(frozen=True)
class ProbeResult:
    """One subagent's free availability verdict.

    ``target`` is what was checked: the resolved absolute path for a cli agent
    that was found, the bare ``argv[0]`` as written when it was not (so a
    ``missing`` result still names it), or the base URL for an openai agent.
    """

    name: str
    source: Source
    kind: str
    status: ProbeStatus
    detail: str
    target: str
    elapsed_ms: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "target": self.target,
            "elapsedMs": self.elapsed_ms,
        }


def _login_path() -> str:
    return login_shell_env().get("PATH", "")


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
    resolved = shutil.which(exe, path=path)
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
            path = await asyncio.to_thread(_login_path)
        return _probe_cli(cfg, source=source, path=path)
    except Exception as exc:  # noqa: BLE001 - a raising probe would blank the page
        name = getattr(cfg, "name", "") or ""
        logger.warning("subagent probe for {!r} failed unexpectedly: {}", name, exc)
        return ProbeResult(name, source, getattr(cfg, "kind", "") or "", "unknown", f"probe failed: {exc}", "", 0)


async def probe_all(entries: Sequence[tuple[Any, Source]]) -> list[ProbeResult]:
    """Probe many configs concurrently, returning results in the order given.

    The login-shell PATH is captured once here rather than inside each cli
    probe: ``login_shell_env`` shells out to ``bash -lic`` and can block for
    real seconds on its first call, which would serialise the whole batch.
    """
    path = await _captured_login_path()
    return list(await asyncio.gather(*(probe_one(cfg, source=src, path=path) for cfg, src in entries)))
```

`probe_one` and `probe_all` must share one guarded capture, so the "never
raises" guarantee holds for both entry points rather than only the one whose
body happens to sit inside a `try`:

```python
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
```

and `probe_one`'s own capture becomes `path = await _captured_login_path()`.

Close the module with:

```python
__all__ = ["PROBE_PROMPT", "ProbeResult", "ProbeStatus", "Source", "probe_all", "probe_one"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_probe.py -q`
Expected: PASS, all tests.

Run: `uv run ruff check raven/ tests/` and `uv run ruff format --check raven/ tests/`
Expected: both clean.

- [ ] **Step 5: Commit**

```bash
git add raven/agent/subagent/probe.py tests/test_subagent_probe.py
git commit -m "$(cat <<'EOF'
feat(agent): add a free availability probe for third-party subagents

A subagent's configuration said nothing about whether it works, so every
failure surfaced later inside a real task. Resolve a cli agent's executable on
the same login-shell PATH a spawn searches, and ask an openai endpoint for its
model list, which separates unreachable from a bad key from a mistyped model
name at no cost. Neither path raises: a page reporting availability must not be
blanked by one unreachable endpoint.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: refuse `readsLocalFiles` on an openai agent

`reads_local_files` is not inert decoration: `format_agent_listing`
(`raven/agent/subagent/backends/__init__.py:78`) renders it into the **spawn and
`run_subagent_dag` tool descriptions** as a `local-files` tag, which is the
dispatching model's licence to hand that agent a path. `OpenAIApiBackend.run`
posts one chat message and offers no channel through which a path could be
opened, so the tag is a claim the runtime cannot honour.

**Files:**
- Modify: `raven/config/schema.py` (`ThirdPartyOpenAISubagentConfig`: the `reads_local_files` docstring, and a new validator beside `_reject_declared_stateful`)
- Test: `tests/test_subagent_probe.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ThirdPartyOpenAISubagentConfig` raises `ValidationError` when `reads_local_files` is `True`. Task 8 removes the checkbox that could set it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_probe.py`, and add `from pydantic import ValidationError` to its imports:

```python
# --- capability limits ---------------------------------------------------


def test_openai_config_refuses_declared_local_file_access() -> None:
    # `reads_local_files` is rendered into the spawn / DAG tool descriptions as a
    # `local-files` tag, so accepting it here would licence the dispatching model
    # to hand this agent a path that nothing can open.
    with pytest.raises(ValidationError, match="readsLocalFiles"):
        ThirdPartyOpenAISubagentConfig(name="api", base_url="http://x/v1", model="m", reads_local_files=True)


def test_openai_config_accepts_false_and_omitted() -> None:
    assert ThirdPartyOpenAISubagentConfig(name="a", base_url="http://x/v1", model="m").reads_local_files is False
    explicit = ThirdPartyOpenAISubagentConfig(
        name="b", base_url="http://x/v1", model="m", reads_local_files=False
    )
    assert explicit.reads_local_files is False


def test_cli_config_still_accepts_local_file_access() -> None:
    # A cli agent is a local subprocess, so the declaration is real there.
    assert ThirdPartyCliSubagentConfig(name="c", command="echo {prompt}", reads_local_files=True).reads_local_files
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_probe.py -k "local_file" -v`
Expected: `test_openai_config_refuses_declared_local_file_access` FAILS - `DID NOT RAISE`. The other two pass already.

- [ ] **Step 3: Rewrite the field docstring**

In `raven/config/schema.py`, in `ThirdPartyOpenAISubagentConfig`, replace:

```python
    reads_local_files: bool = False
    """Whether this agent can open paths on this machine. Defaults to ``false``
    because the endpoint is normally remote; set ``true`` for one served from
    this host, which can therefore read the run directory."""
```

with:

```python
    reads_local_files: bool = False
    """Always ``false`` for this kind; ``true`` is rejected below.

    ``OpenAIApiBackend.run`` posts a single chat message, so no channel exists
    through which the endpoint could open a path -- being served from this host
    does not change that. The value is not inert: ``format_agent_listing``
    renders it into the spawn / DAG tool descriptions as a ``local-files`` tag,
    which is the dispatching model's licence to hand this agent a path."""
```

- [ ] **Step 4: Add the validator**

In the same class, directly after `_reject_declared_stateful`:

```python
    @model_validator(mode="after")
    def _reject_declared_local_file_access(self) -> "ThirdPartyOpenAISubagentConfig":
        if self.reads_local_files:
            raise ValueError(
                "readsLocalFiles is not supported for kind 'openai' (the backend posts one chat "
                "message, so nothing can open a path on this machine); remove it or set it false"
            )
        return self
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_probe.py -k "local_file" -v`
Expected: PASS, 3 tests.

Run: `uv run pytest tests/test_subagent_third_party.py tests/test_update_subagents.py tests/test_web_rpc_config.py tests/test_subagent_dag_runner.py -q`
Expected: PASS. If any fixture builds an openai config with `reads_local_files=True`, that fixture was asserting the behaviour this task removes - report it rather than quietly rewriting the assertion.

Run: `uv run ruff check raven/ tests/` and `uv run ruff format --check raven/ tests/`
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add raven/config/schema.py tests/test_subagent_probe.py
git commit -m "$(cat <<'EOF'
fix(config): refuse local-file access on an openai subagent

readsLocalFiles is rendered into the spawn and run_subagent_dag tool
descriptions as a local-files tag, which tells the dispatching model it may
hand that agent a path. The openai backend posts a single chat message and has
no channel for opening one, on localhost or anywhere else, so the tag was a
claim the runtime could not honour. Reject it the way a declared stateful
already is: no mechanism can deliver it.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `run_test` - the explicit verdict

The paid tier, for cli only. It goes through the real `CliAgentBackend` on
purpose: that is what exercises argv construction, the login-shell environment,
the transcript parser and the CLI's own auth - the layers where every failure we
have actually hit lives (`openclaw`'s missing provider credential, its hard node
version gate). An openai test stays on the free path and sends no completion.

**Files:**
- Modify: `raven/agent/subagent/backends/__init__.py` (`build_third_party_backend` gains two overrides)
- Modify: `raven/agent/subagent/probe.py` (add `TestResult`, `run_test`, `TEST_TIMEOUT_SECONDS`)
- Test: `tests/test_subagent_probe.py`

**Interfaces:**
- Consumes: `ProbeResult`, `probe_one`, `PROBE_PROMPT`, `Source` from Task 1.
- Produces, for Task 4:
  - `TestResult` (frozen dataclass): `name: str`, `source: Source`, `kind: str | None`, `ok: bool`, `detail: str`, `reply: str | None`, `elapsed_ms: int`, plus `to_wire() -> dict`
  - `async run_test(cfg: Any, *, source: Source) -> TestResult`
  - `TEST_TIMEOUT_SECONDS = 120`
  - `build_third_party_backend(cfg, *, registry=None, timeout=None)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_probe.py`. Add one import and **merge** the other
into the existing `probe` import rather than writing a second statement for the
same module (ruff's isort rule flags the duplicate):

```python
from raven.agent.subagent import instances as instances_mod
from raven.agent.subagent.probe import probe_all, probe_one, run_test
```

and this autouse fixture, directly below `_free_port`:

```python
@pytest.fixture(autouse=True)
def _isolated_instance_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here may touch the real ~/.raven/subagent_instances.json.

    Same reasoning as the fixture of this name in
    `tests/test_subagent_third_party.py`: a stateful create commits a handle
    binding through the process-wide `get_registry()` singleton, so without
    this every run would accumulate junk rows in the user's real file.
    """
    monkeypatch.setattr(
        instances_mod, "_registry", instances_mod.InstanceRegistry(path=tmp_path / "_autouse_inst.json")
    )
```

Then the tests:

```python
# --- explicit test -------------------------------------------------------


async def test_test_of_a_missing_cli_fails_without_building_a_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed probe must short-circuit: there is nothing to execute, so no
    # backend is built and no process is spawned.
    def must_not_build(*args, **kwargs):
        raise AssertionError("a failed probe must not reach the backend")

    monkeypatch.setattr(probe_mod, "build_third_party_backend", must_not_build)
    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    res = await run_test(_cli("not-installed-at-all {prompt}"), source="config")
    assert res.ok is False
    assert res.kind == "cli"
    assert "not on the login shell PATH" in res.detail


async def test_test_of_a_working_cli_returns_the_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "echo-agent"
    exe.write_text("#!/bin/sh\necho PONG\n")
    exe.chmod(0o755)
    monkeypatch.setattr(probe_mod, "_login_path", lambda: f"{tmp_path}:/usr/bin:/bin")
    res = await run_test(_cli("echo-agent {prompt}"), source="config")
    assert res.ok is True
    assert res.reply == "PONG"
    assert res.elapsed_ms >= 0


async def test_test_of_a_cli_that_returns_nothing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Exit 0 with an empty answer is exactly the openclaw symptom of replying to
    # its workspace bootstrap instead of the task, so it is not a pass.
    exe = tmp_path / "silent-agent"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(probe_mod, "_login_path", lambda: f"{tmp_path}:/usr/bin:/bin")
    res = await run_test(_cli("silent-agent {prompt}"), source="config")
    assert res.ok is False
    assert "returned nothing" in res.detail


async def test_test_of_a_failing_cli_reports_its_error_rather_than_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "broken-agent"
    exe.write_text("#!/bin/sh\necho 'ProviderAuthError' >&2\nexit 1\n")
    exe.chmod(0o755)
    monkeypatch.setattr(probe_mod, "_login_path", lambda: f"{tmp_path}:/usr/bin:/bin")
    res = await run_test(_cli("broken-agent {prompt}"), source="config")
    assert res.ok is False
    assert "ProviderAuthError" in res.detail


async def test_stateful_cli_test_leaves_the_real_registry_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "stateful-agent"
    exe.write_text("#!/bin/sh\necho PONG\n")
    exe.chmod(0o755)
    monkeypatch.setattr(probe_mod, "_login_path", lambda: f"{tmp_path}:/usr/bin:/bin")
    cfg = ThirdPartyCliSubagentConfig(
        name="stateful",
        command="stateful-agent {prompt} --session-id {agent_id}",
        resume_command="stateful-agent {prompt} --resume {agent_id}",
    )
    res = await run_test(cfg, source="config")
    assert res.ok is True
    # The create commits a handle binding; it must land in the probe's throwaway
    # registry, not in the one the running gateway reads.
    assert instances_mod._registry.list_instances() == []


async def test_openai_test_never_sends_a_completion() -> None:
    async def models(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "m1"}]})

    seen: list[str] = []
    port = _free_port()
    app = web.Application()

    async def record_models(request: web.Request) -> web.Response:
        seen.append(request.path)
        return await models(request)

    async def record_completion(request: web.Request) -> web.Response:
        seen.append(request.path)
        return web.json_response({"choices": [{"message": {"content": "billed!"}}]})

    app.router.add_get("/v1/models", record_models)
    app.router.add_post("/v1/chat/completions", record_completion)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        res = await run_test(_openai(f"http://127.0.0.1:{port}/v1", model="m1"), source="config")
        assert res.ok is True
        assert res.reply is None
        assert seen == ["/v1/models"]
    finally:
        await runner.cleanup()


async def test_test_result_wire_shape_is_camel_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    wire = (await run_test(_cli("nope {prompt}"), source="config")).to_wire()
    assert set(wire) == {"name", "source", "kind", "ok", "detail", "reply", "elapsedMs"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_probe.py -k "test_test or stateful or openai_test" -v`
Expected: collection error - `cannot import name 'run_test'`.

- [ ] **Step 3: Give the backend factory two overrides**

In `raven/agent/subagent/backends/__init__.py`, change the signature and docstring of `build_third_party_backend` and thread the two values through:

```python
def build_third_party_backend(cfg: Any, *, registry: Any = None, timeout: int | None = None) -> SubagentBackend:
    """Build a third-party backend from a config object (duck-typed on ``kind``).

    Accepts ThirdPartyCliSubagentConfig / ThirdPartyOpenAISubagentConfig.

    ``registry`` and ``timeout`` override the config for one call and exist for
    the availability test in :mod:`raven.agent.subagent.probe`, which has to
    bound a run whose config declares no timeout and has to keep a stateful
    create's handle binding out of the user's real instance file. Building the
    backend here rather than in the probe keeps one field list: a duplicated one
    would drift the moment a field is added, and the test would then silently
    exercise a different command than a real spawn. ``registry`` is ignored for
    kind ``openai``, which has no session store.
    """
    kind = getattr(cfg, "kind", None)
    if kind == "cli":
        return CliAgentBackend(
            name=cfg.name,
            command=cfg.command,
            resume_command=cfg.resume_command,
            id_source=cfg.id_source,
            session_id_pattern=cfg.session_id_pattern,
            output_pattern=cfg.output_pattern,
            transcript_format=cfg.transcript_format,
            cwd=cfg.cwd,
            env=dict(cfg.env),
            timeout=cfg.timeout if timeout is None else timeout,
            max_output_chars=cfg.max_output_chars,
            registry=registry,
        )
    if kind == "openai":
        return OpenAIApiBackend(
            name=cfg.name,
            base_url=cfg.base_url,
            model=cfg.model,
            api_key=cfg.api_key,
            system_prompt=cfg.system_prompt,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.timeout if timeout is None else timeout,
            max_output_chars=cfg.max_output_chars,
        )
    raise ValueError(f"unknown third-party subagent kind: {kind!r}")
```

`CliAgentBackend.__init__` already falls back to `get_registry()` when
`registry` is `None`, so the default call site is unchanged.

- [ ] **Step 4: Add `TestResult` and `run_test`**

In `raven/agent/subagent/probe.py`, extend the imports:

```python
import tempfile
import uuid
from pathlib import Path
```

and

```python
from raven.agent.subagent.backends import build_third_party_backend
from raven.agent.subagent.backends.env import login_shell_env
from raven.agent.subagent.instances import InstanceRegistry
```

Add the constant beside `PROBE_PROMPT`:

```python
# A test must be bounded even though `timeout` defaults to None (no automatic
# limit): an unbounded button is a hang. Generous for a one-word answer, so a
# genuinely slow agent can report a test timeout while working for real tasks.
TEST_TIMEOUT_SECONDS = 120
_DETAIL_CAP = 2000
```

Add after `ProbeResult`:

```python
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
```

Add at the end, before `__all__`:

```python
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

    with tempfile.TemporaryDirectory(prefix="raven_subagent_test_") as tmp:
        backend = build_third_party_backend(
            cfg,
            # A stateful create commits a handle binding; a test must not leave
            # that in the file the running gateway reads.
            registry=InstanceRegistry(path=Path(tmp) / "probe_instances.json"),
            timeout=min(getattr(cfg, "timeout", None) or TEST_TIMEOUT_SECONDS, TEST_TIMEOUT_SECONDS),
        )
        try:
            reply = await backend.run(
                PROBE_PROMPT, task_id=f"test-{uuid.uuid4().hex[:8]}", workspace=Path(tmp), executor=None
            )
        except Exception as exc:  # noqa: BLE001 - every failure is the answer, not a crash
            return TestResult(cfg.name, source, "cli", False, str(exc)[:_DETAIL_CAP], None, elapsed())

    text = (reply or "").strip()
    if not text:
        return TestResult(cfg.name, source, "cli", False, "the command exited 0 but returned nothing", None, elapsed())
    return TestResult(cfg.name, source, "cli", True, "the agent ran and replied", text[:_DETAIL_CAP], elapsed())
```

Extend `__all__` with `"TEST_TIMEOUT_SECONDS"`, `"TestResult"`, `"run_test"`, keeping it sorted.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_probe.py -q`
Expected: PASS, all tests.

Run: `uv run pytest tests/test_subagent_third_party.py -q`
Expected: PASS - the factory's default behaviour is unchanged.

Run: `uv run ruff check raven/ tests/` and `uv run ruff format --check raven/ tests/`
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/subagent/probe.py raven/agent/subagent/backends/__init__.py \
        tests/test_subagent_probe.py
git commit -m "$(cat <<'EOF'
feat(agent): add an explicit availability test for third-party subagents

A cli agent's real failures all live past which: a missing provider credential,
a hard runtime-version gate, a reply that answers the workspace bootstrap
instead of the task. Dispatch one short task through the same backend a spawn
uses so the test exercises argv building, the login-shell environment, the
transcript parser and the CLI's own auth. Bound it at 120s and give it a
throwaway instance registry, so a test cannot hang and cannot leave a handle
binding behind. An openai test stays on the free model-list path and sends no
completion, because a completion is billed.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: expose both over RPC and REST

Both live in the gateway process, the only one holding the live config and the
login-shell environment. `test` takes a **name**, never a command: `PUT
/raven/subagents` already lets a client persist a command the runtime will run,
but a POST carrying its own command would execute one immediately with nothing
persisted, which is a wider hole than the config plane on a service whose
authentication story is still open.

**Files:**
- Modify: `raven/web_rpc/methods_config.py` (two handlers + two `dispatcher.register` calls + the docstring bullet list)
- Modify: `ui-webui/service/raven_config_routes.py:90` (two routes, after `subagent_presets`)
- Test: `tests/test_web_rpc_config.py`

**Interfaces:**
- Consumes: `probe_all`, `run_test`, `TestResult` from Tasks 1 and 3.
- Produces, for Task 5: `raven.subagents.probe` -> `{"results": [ProbeResult wire, ...]}`; `raven.subagents.test` with params `{name, source}` -> `{"result": TestResult wire}`; `GET /raven/subagents/probe`; `POST /raven/subagents/test`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_rpc_config.py`:

```python
async def test_subagents_probe_covers_config_and_presets(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import raven.agent.subagent.probe as probe_mod

    # A real capture shells out to `bash -lic` (up to 15s) and would make the
    # result depend on what happens to be installed on the test machine.
    monkeypatch.setattr(probe_mod, "_login_path", lambda: "/nonexistent-probe-path")
    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    await _dispatch(d, "raven.subagents.set", {"agents": [{"name": "mine", "kind": "cli", "command": "nope {prompt}"}]})

    resp = await _dispatch(d, "raven.subagents.probe", {})
    assert "error" not in resp, resp
    results = resp["result"]["results"]
    by_key = {(r["source"], r["name"]): r for r in results}
    assert by_key[("config", "mine")]["status"] == "missing"
    # Every built-in preset is probed too, so the Presets group can show what is
    # installed before the user commits to configuring it.
    assert ("preset", "claude_code") in by_key
    # A keyless openai preset is a template: reported, but never requested.
    assert by_key[("preset", "mirothinker")]["status"] == "attention"
    assert set(results[0]) == {"name", "source", "kind", "status", "detail", "target", "elapsedMs"}


async def test_subagents_test_reports_an_unknown_name_without_raising(cfg_path: Path) -> None:
    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    resp = await _dispatch(d, "raven.subagents.test", {"name": "ghost", "source": "config"})
    assert "error" not in resp, resp
    result = resp["result"]["result"]
    assert result["ok"] is False
    assert result["kind"] is None
    assert "no such subagent" in result["detail"]


async def test_subagents_test_rejects_an_unknown_source(cfg_path: Path) -> None:
    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    resp = await _dispatch(d, "raven.subagents.test", {"name": "x", "source": "wherever"})
    assert "error" in resp
    # A bare `"error" in resp` also passes when the method is not registered at
    # all, so it cannot tell a rejected source from any other failure. Pin both
    # ends: the error is not method_not_found, it names the offending field, and
    # the same call with a valid source does not error at all -- which is what
    # attributes the rejection to `source` rather than to anything else.
    assert resp["error"]["message"] != "method_not_found"
    assert "source" in resp["error"].get("data", {}).get("traceback_tail", "")
    ok_resp = await _dispatch(d, "raven.subagents.test", {"name": "x", "source": "preset"})
    assert "error" not in ok_resp, ok_resp
    assert ok_resp["result"]["result"]["ok"] is False


async def test_subagents_test_runs_the_saved_entry(cfg_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    import raven.agent.subagent.backends.env as env_mod
    import raven.agent.subagent.probe as probe_mod

    exe = tmp_path / "rpc-agent"
    exe.write_text("#!/bin/sh\necho PONG\n")
    exe.chmod(0o755)
    monkeypatch.setattr(probe_mod, "_login_path", lambda: f"{tmp_path}:/usr/bin:/bin")
    # This test actually spawns, and `CliAgentBackend._exec` builds the child's
    # environment from `login_shell_env()` rather than from anything the probe
    # passes down, so patching only `_login_path` steers the probe and leaves the
    # spawn looking at the real PATH. Same pattern as
    # `_patch_login_env_for_spawn` in tests/test_subagent_probe.py.
    monkeypatch.setattr(
        env_mod,
        "_LOGIN_ENV",
        {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/root")},
    )
    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    await _dispatch(
        d, "raven.subagents.set", {"agents": [{"name": "runme", "kind": "cli", "command": "rpc-agent {prompt}"}]}
    )
    resp = await _dispatch(d, "raven.subagents.test", {"name": "runme", "source": "config"})
    assert "error" not in resp, resp
    assert resp["result"]["result"]["ok"] is True
    assert resp["result"]["result"]["reply"] == "PONG"
```

Note: `tests/test_web_rpc_config.py` already has the `cfg_path` fixture, `_FakeAgent`, `_dispatch` and a `pytest` import; do not redefine them.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_rpc_config.py -k "probe or subagents_test" -v`
Expected: FAIL - the dispatcher answers with a "method not found" error, so `"error" not in resp` fails.

- [ ] **Step 3: Register the two RPC methods**

In `raven/web_rpc/methods_config.py`, add to the deferred import block at the top of `register_config_methods` (beside the existing `from raven.agent.subagent.presets import third_party_subagent_presets`):

```python
    from raven.agent.subagent.probe import TestResult, probe_all, run_test
```

Add the two handlers directly after `_presets`:

```python
    def _as_configs(entries: list[dict]) -> list[Any]:
        return list(SubagentsConfig(third_party=entries).third_party)

    async def _probe(params: dict) -> dict:
        # Read config from disk rather than from the live AgentLoop: a save
        # lands there, so the next probe reflects it with nothing to invalidate.
        entries = [(cfg, "config") for cfg in _as_configs(get_third_party_subagents())]
        entries += [(cfg, "preset") for cfg in _as_configs(third_party_subagent_presets())]
        return {"results": [r.to_wire() for r in await probe_all(entries)]}

    async def _test(params: dict) -> dict:
        name = params.get("name") or ""
        source = params.get("source") or "config"
        if source not in ("config", "preset"):
            raise ValueError("source must be 'config' or 'preset'")
        # By name only, never a command from the params: a command here would be
        # executed immediately with nothing persisted, which is a wider surface
        # than the config plane that at least leaves a record of what can run.
        pool = third_party_subagent_presets() if source == "preset" else get_third_party_subagents()
        match = next((e for e in pool if e.get("name") == name), None)
        if match is None:
            return {"result": TestResult(name, source, None, False, "no such subagent", None, 0).to_wire()}
        return {"result": (await run_test(_as_configs([match])[0], source=source)).to_wire()}
```

Register them beside the others:

```python
    dispatcher.register("raven.subagents.probe", _probe)
    dispatcher.register("raven.subagents.test", _test)
```

Extend the first docstring bullet so the method list stays accurate. Replace:

```
    - ``raven.subagents.{list,set,presets,instances}`` — third-party sub-agents; ``set``
```

with:

```
    - ``raven.subagents.{list,set,presets,instances,probe,test}`` — third-party
      sub-agents. ``probe`` is the free availability check over every configured
      agent and every preset (no subprocess, no chat completion); ``test``
      reaches a real verdict for one entry **by name** — it never accepts a
      command, which would be executed with nothing persisted. ``set``
```

- [ ] **Step 4: Add the two REST routes**

In `ui-webui/service/raven_config_routes.py`, directly after the
`subagent_presets` route (line 90):

```python
    @router.get("/subagents/probe")
    async def probe_subagents() -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.subagents.probe", {})

    @router.post("/subagents/test")
    async def test_subagent(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        return await client.call(
            "raven.subagents.test",
            {"name": body.get("name") or "", "source": body.get("source") or "config"},
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_rpc_config.py -q`
Expected: PASS.

Run: `uv run pytest tests/test_subagent_probe.py tests/test_subagent_third_party.py -q`
Expected: PASS.

Run: `uv run ruff check raven/ ui-webui/service/raven_config_routes.py tests/` and
`uv run ruff format --check raven/ ui-webui/service/raven_config_routes.py tests/`
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add raven/web_rpc/methods_config.py ui-webui/service/raven_config_routes.py \
        tests/test_web_rpc_config.py
git commit -m "$(cat <<'EOF'
feat(web_rpc): expose subagent probe and test over the config plane

The probe covers every configured agent and every built-in preset in one batch
call, reading config from disk so a save needs no cache invalidation. The test
takes a name and looks the entry up in the live config or in presets.py: a
command in the request body would be executed immediately with nothing
persisted, a wider surface than the config plane it would bypass.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: TS types, api client, and the hook

**Files:**
- Modify: `ui-webui/frontend/src/api/ravenConfig.ts` (two interfaces after `RavenOpenAISubagent`; two methods after `presets`)
- Modify: `ui-webui/frontend/src/hooks/useRavenSubagents.ts`
- Modify: `ui-webui/frontend/src/api/index.ts` **only if** it re-exports types explicitly rather than with `export *` - check before editing

**Interfaces:**
- Consumes: the two wire shapes from Task 4.
- Produces, for Tasks 6 and 7:
  - `RavenSubagentProbe { name, source, kind, status, detail, target, elapsedMs }`
  - `RavenSubagentTest { name, source, kind, ok, detail, reply, elapsedMs }`
  - `ravenConfigApi.probeSubagents()`, `ravenConfigApi.testSubagent({name, source})`
  - `useRavenSubagents()` additionally returns `probes: Record<string, RavenSubagentProbe>`, `probing: boolean`, `reprobe: () => Promise<void>`

- [ ] **Step 1: Add the two interfaces**

In `ui-webui/frontend/src/api/ravenConfig.ts`, after the `RavenOpenAISubagent`
interface:

```ts
/** One subagent's free availability check (`GET /raven/subagents/probe`).
 *  `target` is the resolved executable path for a cli agent, or the base URL
 *  for an openai one. */
export interface RavenSubagentProbe {
	name: string;
	source: 'config' | 'preset';
	kind: string;
	status: 'ready' | 'attention' | 'missing' | 'unknown';
	detail: string;
	target: string;
	elapsedMs: number;
}

/** One subagent's explicit test (`POST /raven/subagents/test`). `reply` carries
 *  the agent's own answer for a cli test and is always null for openai, which
 *  sends no completion. `kind` is null only when the name matched nothing. */
export interface RavenSubagentTest {
	name: string;
	source: 'config' | 'preset';
	kind: 'cli' | 'openai' | null;
	ok: boolean;
	detail: string;
	reply: string | null;
	elapsedMs: number;
}
```

- [ ] **Step 2: Add the two client methods**

In the same file, in `ravenConfigApi`, directly after `presets`:

```ts
	probeSubagents: () => client.get<{ results: RavenSubagentProbe[] }>('/raven/subagents/probe'),

	testSubagent: (body: { name: string; source: 'config' | 'preset' }) =>
		client.post<{ result: RavenSubagentTest }>('/raven/subagents/test', body),
```

- [ ] **Step 3: Check the barrel export**

Run: `grep -n "RavenOpenAISubagent\|export \*" ui-webui/frontend/src/api/index.ts`

If `index.ts` re-exports named types one by one, add `RavenSubagentProbe` and
`RavenSubagentTest` alongside them. If it uses `export *`, change nothing.

- [ ] **Step 4: Extend the hook**

Rewrite `ui-webui/frontend/src/hooks/useRavenSubagents.ts` as:

```ts
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenSubagentProbe, RavenThirdPartySubagent } from '@/api';

/**
 * Raven's third-party subagent config. Writes replace the whole list: the
 * gateway validates it, persists it to ~/.raven, and hot-applies it to the
 * running AgentLoop.
 *
 * Availability probes are a second, deliberately decoupled fetch: they are keyed
 * `<source>:<name>` because a configured agent and a preset can share a name,
 * and they are never awaited by `reload`, so a slow endpoint cannot hold up the
 * list render.
 */
export function useRavenSubagents() {
	const [agents, setAgents] = useState<RavenThirdPartySubagent[]>([]);
	const [presets, setPresets] = useState<RavenThirdPartySubagent[]>([]);
	const [probes, setProbes] = useState<Record<string, RavenSubagentProbe>>({});
	const [loading, setLoading] = useState(true);
	const [probing, setProbing] = useState(false);

	const reprobe = useCallback(async () => {
		setProbing(true);
		try {
			const res = await ravenConfigApi.probeSubagents();
			const next: Record<string, RavenSubagentProbe> = {};
			for (const r of res.results ?? []) next[`${r.source}:${r.name}`] = r;
			setProbes(next);
		} catch {
			// client.ts already toasts. Keep the previous statuses rather than
			// blanking them: a stale dot beats no dot at all.
		} finally {
			setProbing(false);
		}
	}, []);

	const reload = useCallback(async () => {
		setLoading(true);
		try {
			const [a, p] = await Promise.all([
				ravenConfigApi.listSubagents(),
				ravenConfigApi.presets(),
			]);
			setAgents(a.agents ?? []);
			setPresets(p.presets ?? []);
			void reprobe();
		} catch (e) {
			toast.error(`Failed to load subagents: ${e instanceof Error ? e.message : String(e)}`);
			setAgents([]);
			setPresets([]);
		} finally {
			setLoading(false);
		}
	}, [reprobe]);

	useEffect(() => {
		void reload();
	}, [reload]);

	const save = useCallback(
		async (next: RavenThirdPartySubagent[]) => {
			await ravenConfigApi.setSubagents(next);
			setAgents(next);
			// Re-probe after a save so fixing a key or a model name updates the
			// status without a page reload.
			void reprobe();
		},
		[reprobe],
	);

	return { agents, presets, probes, loading, probing, reload, reprobe, save };
}
```

- [ ] **Step 5: Run the frontend gate**

From `ui-webui/`:

Run: `pnpm -C frontend lint`
Expected: 0 errors (17 pre-existing warnings are acceptable).

Run: `pnpm -C frontend build`
Expected: success.

- [ ] **Step 6: Commit**

```bash
git add ui-webui/frontend/src/api/ravenConfig.ts ui-webui/frontend/src/hooks/useRavenSubagents.ts
git commit -m "$(cat <<'EOF'
feat(ui-webui): fetch subagent availability probes alongside the config

Probes are a second fetch that reload never awaits, so a slow endpoint cannot
hold up the list render, and they re-run after a save so fixing a key updates
the status without a reload. Keyed by source and name together because a
configured agent and a preset can share a name.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: render the status - one component, two shapes

**Files:**
- Create: `ui-webui/frontend/src/components/SubagentStatus.tsx`
- Modify: `ui-webui/frontend/src/pages/subagent/index.tsx` (dots on both lists; pass `probe` into the form)
- Modify: `ui-webui/frontend/src/pages/subagent/SubagentForm.tsx` (accept and render `probe`)
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `zh.json`

**Interfaces:**
- Consumes: `RavenSubagentProbe`, `useRavenSubagents().probes/probing/reprobe` from Task 5.
- Produces, for Task 7: `SubagentStatusDot`, `SubagentStatusLine`; `SubagentFormProps` gains `probe: RavenSubagentProbe | null`, `probing: boolean`, `onRefreshProbe: () => void`.

- [ ] **Step 1: Create the component**

Create `ui-webui/frontend/src/components/SubagentStatus.tsx`:

```tsx
import type { RavenSubagentProbe } from '@/api';
import { Button } from '@/components/ui/button';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import Refresh from '~icons/solar/refresh-linear';

/** Status colour and label live together in one map so the dot in the sidebar
 *  and the line in the pane can never disagree about what a status means. */
const DOT: Record<string, string> = {
	ready: 'bg-emerald-500',
	attention: 'bg-amber-500',
	missing: 'bg-destructive',
	unknown: 'bg-muted-foreground',
};

const LABEL_KEY: Record<string, string> = {
	ready: 'subagent-sidebar.statusReady',
	attention: 'subagent-sidebar.statusAttention',
	missing: 'subagent-sidebar.statusMissing',
	unknown: 'subagent-sidebar.statusUnknown',
};

function label(status: string, t: (k: string) => string): string {
	return t(LABEL_KEY[status] ?? LABEL_KEY.unknown);
}

/** The sidebar affordance: a dot, with the full verdict on hover. Renders
 *  nothing until the probe lands, so rows never jump. */
export function SubagentStatusDot({ probe }: { probe?: RavenSubagentProbe }) {
	const { t } = useTranslation();
	if (!probe) return null;
	return (
		<span
			className={cn('size-1.5 shrink-0 rounded-full', DOT[probe.status] ?? DOT.unknown)}
			title={`${label(probe.status, t)} - ${probe.detail}`}
			aria-label={label(probe.status, t)}
		/>
	);
}

interface SubagentStatusLineProps {
	probe: RavenSubagentProbe | null;
	probing: boolean;
	onRefresh: () => void;
	/** cli only: the probe checks the command's executable, not the agent. */
	showCliHint?: boolean;
}

/** The pane affordance: the verdict in words, plus what was checked. */
export function SubagentStatusLine({
	probe,
	probing,
	onRefresh,
	showCliHint,
}: SubagentStatusLineProps) {
	const { t } = useTranslation();
	return (
		<div className="rounded-lg border px-3 py-2">
			<div className="flex items-center gap-x-2">
				<span
					className={cn(
						'size-1.5 shrink-0 rounded-full',
						DOT[probe?.status ?? 'unknown'] ?? DOT.unknown,
					)}
				/>
				<span className="text-sm font-medium">
					{probe ? label(probe.status, t) : t('subagent-sidebar.statusChecking')}
				</span>
				<Button
					size="icon-sm"
					variant="ghost"
					className="ml-auto"
					onClick={onRefresh}
					disabled={probing}
					title={t('subagent-sidebar.statusRefresh')}
				>
					<Refresh className={cn('size-3.5', probing && 'animate-spin')} />
				</Button>
			</div>
			{probe && <p className="text-muted-foreground mt-1 text-[11px]">{probe.detail}</p>}
			{showCliHint && (
				<p className="text-muted-foreground mt-1 text-[11px]">
					{t('subagent-sidebar.statusCliHint')}
				</p>
			)}
		</div>
	);
}
```

- [ ] **Step 2: Add the i18n keys**

In `ui-webui/frontend/src/i18n/locales/en.json`, inside `subagent-sidebar`,
after `"statefulBadge"`:

```json
		"statusReady": "Ready",
		"statusAttention": "Needs attention",
		"statusMissing": "Not available",
		"statusUnknown": "Unknown",
		"statusChecking": "Checking...",
		"statusRefresh": "Check again",
		"statusCliHint": "This checks the command's executable on the login shell PATH. Use Test to run the agent itself.",
```

In `zh.json`, at the same place:

```json
		"statusReady": "就绪",
		"statusAttention": "需要检查",
		"statusMissing": "不可用",
		"statusUnknown": "未知",
		"statusChecking": "检测中...",
		"statusRefresh": "重新检测",
		"statusCliHint": "这里只检查命令的可执行文件是否在登录 shell 的 PATH 上。要验证 agent 本身，请用测试。",
```

Match the surrounding indentation exactly. Do not reformat anything else.

- [ ] **Step 3: Wire the dots and pass the probe down (`index.tsx`)**

Add the imports:

```tsx
import { SubagentStatusDot } from '@/components/SubagentStatus';
```

Take the new values from the hook:

```tsx
	const { agents, presets, probes, loading, probing, reprobe, save } = useRavenSubagents();
```

Add a probe key helper beside `rowLabel`:

```tsx
	// A configured agent and a preset can share a name (a preset keeping its own
	// default name is both), so the source is part of the key.
	const probeOf = (source: 'config' | 'preset', name: string) =>
		probes[`${source}:${name}`] ?? null;
```

In the Configured rows, put the dot immediately after `<SubagentIcon .../>`:

```tsx
													<SubagentStatusDot
														probe={probeOf('config', sa.name) ?? undefined}
													/>
```

In the Presets rows, the same, after that group's `<SubagentIcon .../>`:

```tsx
													<SubagentStatusDot
														probe={
															probeOf('preset', preset.name) ??
															undefined
														}
													/>
```

Pass the pane's probe into the form, next to the existing props:

```tsx
							probe={
								editingName
									? probeOf('config', editingName)
									: preset
										? probeOf('preset', preset.name)
										: null
							}
							probing={probing}
							onRefreshProbe={() => void reprobe()}
```

- [ ] **Step 4: Render the line in the pane (`SubagentForm.tsx`)**

Add to the imports:

```tsx
import type { RavenSubagentProbe } from '@/api';
import { SubagentStatusLine } from '@/components/SubagentStatus';
```

Add to `SubagentFormProps`:

```tsx
	probe: RavenSubagentProbe | null;
	probing: boolean;
	onRefreshProbe: () => void;
```

Destructure them in the parameter list, and render the line directly below the
preset description paragraph (above the Name label) — gated on there being
something probeable at all:

```tsx
			{(editingName || preset) && (
				<SubagentStatusLine
					probe={probe}
					probing={probing}
					onRefresh={onRefreshProbe}
					showCliHint={form.kind === 'cli'}
				/>
			)}
```

The gate is not cosmetic. A brand-new draft has no `config:<name>` or
`preset:<name>` probe key and never will, so an ungated line would show
`statusChecking` forever — indistinguishable from the legitimate "existing entry,
probe still in flight" state, and a claim that work is in progress when none is.
`editingName || preset` is exactly the "something exists to probe" signal, and it
keeps `statusChecking` for the case that really is pending.

- [ ] **Step 5: Run the frontend gate**

From `ui-webui/`:

Run: `pnpm -C frontend lint`
Expected: 0 errors.

Run: `pnpm -C frontend build`
Expected: success.

Run: `python3 -c "import json; [json.load(open(f'frontend/src/i18n/locales/{n}.json')) for n in ('en','zh')]; print('both parse')"`
Expected: `both parse`.

- [ ] **Step 6: Commit**

```bash
git add ui-webui/frontend/src/components/SubagentStatus.tsx \
        ui-webui/frontend/src/pages/subagent/index.tsx \
        ui-webui/frontend/src/pages/subagent/SubagentForm.tsx \
        ui-webui/frontend/src/i18n/locales/en.json \
        ui-webui/frontend/src/i18n/locales/zh.json
git commit -m "$(cat <<'EOF'
feat(ui-webui): show each subagent's availability in the list and the pane

One component renders both shapes from a single status-to-colour map, so a dot
in the sidebar and the line in the pane cannot disagree about what a status
means. The cli hint says the check covers the command's executable rather than
the agent, so "installed" is not read as a stronger claim than it is.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: the Test button

`index.tsx` owns the call and the result state; `SubagentForm` stays
presentational, matching how it already receives `onSubmit` / `onClose` /
`onDelete` rather than calling the api itself.

Which entry gets tested:

| pane | enabled | `source` | `name` |
|---|---|---|---|
| editing a saved agent | yes | `config` | `editingName` |
| a preset clicked from Presets, not yet saved | yes | `preset` | `preset.name` |
| a brand-new custom agent | **no** | - | - |

A preset is safe to test unsaved because it is defined in Python, not supplied
by the client. A new custom agent has nothing saved to test, which is the whole
reason the button is disabled there.

**Files:**
- Modify: `ui-webui/frontend/src/pages/subagent/index.tsx`
- Modify: `ui-webui/frontend/src/pages/subagent/SubagentForm.tsx`
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `zh.json`

**Interfaces:**
- Consumes: `ravenConfigApi.testSubagent`, `RavenSubagentTest` from Task 5.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Add the i18n keys**

In `en.json`, inside `subagent-sidebar`, after the `status*` block from Task 6:

```json
		"testButton": "Test",
		"testRunning": "Testing...",
		"testPassed": "Test passed",
		"testFailed": "Test failed",
		"testReplyLabel": "Reply",
		"testCliWarning": "Runs this agent for real with a one-word task, spending its own quota. Capped at 120 seconds.",
		"testOpenaiNote": "Checked the model list only. No conversation was started, so nothing was billed.",
		"testSavedHint": "Tests the saved configuration. Save your edits first to test them.",
		"testNewHint": "Save this subagent before testing it.",
```

In `zh.json`, at the same place:

```json
		"testButton": "测试",
		"testRunning": "测试中...",
		"testPassed": "测试通过",
		"testFailed": "测试失败",
		"testReplyLabel": "回复",
		"testCliWarning": "会用一句极短的任务真实调用该 agent，消耗它自己的额度。上限 120 秒。",
		"testOpenaiNote": "只检查了模型列表，没有发起对话，不产生费用。",
		"testSavedHint": "测试的是已保存的配置。要测试当前修改，请先保存。",
		"testNewHint": "请先保存这个子智能体，然后再测试。",
```

- [ ] **Step 2: Own the call in `index.tsx`**

Add the imports:

```tsx
import { ravenConfigApi } from '@/api';
import type { RavenSubagentTest, RavenThirdPartySubagent } from '@/api';
```

(the file already imports the `RavenThirdPartySubagent` type; merge rather than
duplicating the import).

Add state beside `submitting`:

```tsx
	const [testing, setTesting] = useState(false);
	const [testResult, setTestResult] = useState<RavenSubagentTest | null>(null);
```

Clear the result whenever the pane changes, in all four openers - add
`setTestResult(null);` to `openCreate`, `openEdit`, `close` and `addFromPreset`.
A stale verdict from another agent is worse than none.

Add the handler after `submit`:

```tsx
	// Tested by name against what is saved (or against the Python-defined
	// preset), never by posting a command: see the gateway handler for why.
	const testTarget: { name: string; source: 'config' | 'preset' } | null = editingName
		? { name: editingName, source: 'config' }
		: presetName
			? { name: presetName, source: 'preset' }
			: null;

	const runTest = async () => {
		if (!testTarget) return;
		setTesting(true);
		setTestResult(null);
		try {
			const res = await ravenConfigApi.testSubagent(testTarget);
			setTestResult(res.result);
		} catch {
			// client.ts toasts
		} finally {
			setTesting(false);
		}
	};
```

Pass them to the form, beside the Task 6 props:

```tsx
							testing={testing}
							testResult={testResult}
							canTest={testTarget !== null}
							onTest={() => void runTest()}
```

- [ ] **Step 3: Render the button and the result (`SubagentForm.tsx`)**

Add to the imports:

```tsx
import type { RavenSubagentProbe, RavenSubagentTest } from '@/api';
```

(merge with the `RavenSubagentProbe` import added in Task 6.)

Add to `SubagentFormProps`:

```tsx
	testing: boolean;
	testResult: RavenSubagentTest | null;
	canTest: boolean;
	onTest: () => void;
```

Destructure them, then replace the existing Save button block:

```tsx
			<Button
				disabled={!canSubmit || submitting}
				onClick={onSubmit}
				className="mt-1 self-start"
			>
				{submitting && <Loader2 className="size-3.5 animate-spin" />}
				{t('common.save')}
			</Button>
```

with a row that carries both actions plus the result:

```tsx
			<div className="mt-1 flex items-center gap-x-2">
				<Button disabled={!canSubmit || submitting} onClick={onSubmit}>
					{submitting && <Loader2 className="size-3.5 animate-spin" />}
					{t('common.save')}
				</Button>
				<Button variant="outline" disabled={!canTest || testing} onClick={onTest}>
					{testing && <Loader2 className="size-3.5 animate-spin" />}
					{testing ? t('subagent-sidebar.testRunning') : t('subagent-sidebar.testButton')}
				</Button>
			</div>
			<p className="text-muted-foreground text-[11px]">
				{!canTest
					? t('subagent-sidebar.testNewHint')
					: form.kind === 'cli'
						? t('subagent-sidebar.testCliWarning')
						: t('subagent-sidebar.testSavedHint')}
			</p>
			{testResult && (
				<div className="rounded-lg border px-3 py-2">
					<p
						className={
							testResult.ok
								? 'text-sm font-medium text-emerald-600 dark:text-emerald-400'
								: 'text-destructive text-sm font-medium'
						}
					>
						{testResult.ok
							? t('subagent-sidebar.testPassed')
							: t('subagent-sidebar.testFailed')}
						<span className="text-muted-foreground ml-2 font-normal tabular-nums">
							{(testResult.elapsedMs / 1000).toFixed(1)}s
						</span>
					</p>
					<p className="text-muted-foreground mt-1 text-[11px] break-words whitespace-pre-wrap">
						{testResult.detail}
					</p>
					{testResult.reply && (
						<p className="mt-2 text-xs break-words whitespace-pre-wrap">
							<span className="text-muted-foreground">
								{t('subagent-sidebar.testReplyLabel')}:{' '}
							</span>
							{testResult.reply}
						</p>
					)}
					{testResult.kind === 'openai' && (
						<p className="text-muted-foreground mt-1 text-[11px]">
							{t('subagent-sidebar.testOpenaiNote')}
						</p>
					)}
				</div>
			)}
```

The cli warning replaces the saved-config hint rather than stacking with it:
for a cli agent the quota cost is the more important of the two, and two hint
lines under one button is noise.

- [ ] **Step 4: Run the frontend gate**

From `ui-webui/`:

Run: `pnpm -C frontend lint`
Expected: 0 errors.

Run: `pnpm -C frontend build`
Expected: success.

Run: `python3 -c "import json; [json.load(open(f'frontend/src/i18n/locales/{n}.json')) for n in ('en','zh')]; print('both parse')"`
Expected: `both parse`.

- [ ] **Step 5: Walk your own diff and state the behaviour**

In the task report, state what the Test button does in each of these panes, and
why: (a) a saved `Coder` cli agent, (b) `Claude Code` clicked from Presets and
not yet saved, (c) a brand-new Custom CLI agent, (d) the saved
`DeepResearcher` openai agent.

- [ ] **Step 6: Commit**

```bash
git add ui-webui/frontend/src/pages/subagent/index.tsx \
        ui-webui/frontend/src/pages/subagent/SubagentForm.tsx \
        ui-webui/frontend/src/i18n/locales/en.json \
        ui-webui/frontend/src/i18n/locales/zh.json
git commit -m "$(cat <<'EOF'
feat(ui-webui): add a per-subagent test button

A cli test runs the agent for real, so the button says it spends that agent's
quota and names the 120s cap. An openai test says it only read the model list
and started no conversation, so a pass cannot be misread as a billed request
having succeeded. Disabled for a never-saved agent, because the test runs what
is saved rather than what is typed.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: stop offering the two unsupported fields

`systemPrompt` is dropped by mirothinker's server (verified by controlled
experiment: the same instruction obeyed in the `user` role is ignored in the
`system` role), and `readsLocalFiles` is now rejected by the schema for every
openai agent (Task 2). Both must leave the form, or the pane offers a control
whose only outcomes are silence and a validation error.

**Files:**
- Modify: `ui-webui/frontend/src/pages/subagent/catalog.ts` (`PresetDisplay.unsupported`)
- Modify: `ui-webui/frontend/src/pages/subagent/SubagentForm.tsx` (skip unsupported; drop the openai checkbox)

**Interfaces:**
- Consumes: `PRESET_DISPLAY` from the existing `catalog.ts`.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Declare the unsupported field in the catalog**

In `ui-webui/frontend/src/pages/subagent/catalog.ts`, extend the interface:

```ts
export interface PresetDisplay {
	labelKey: string;
	basicFields: BasicField[];
	/** Fields this preset's provider accepts and then ignores, so the form must
	 *  not offer them. Provider-specific, which is why it lives here rather than
	 *  in the config schema: a rule keyed to one preset name would put a vendor
	 *  quirk in the config layer. */
	unsupported?: string[];
}
```

and give mirothinker the declaration:

```ts
	mirothinker: {
		labelKey: 'subagent-sidebar.presetMirothinker',
		basicFields: ['apiKey', 'baseUrl', 'model'],
		// The server drops a system-role message: the same instruction is obeyed
		// in the user role and ignored in the system role. Offering the field
		// would promise behaviour the endpoint does not deliver.
		unsupported: ['systemPrompt'],
	},
```

- [ ] **Step 2: Skip unsupported fields and drop the openai checkbox**

In `ui-webui/frontend/src/pages/subagent/SubagentForm.tsx`, beside the existing
`catalogBasicFields` computation:

```tsx
	const unsupported = preset ? (PRESET_DISPLAY[preset.name]?.unsupported ?? []) : [];
```

Wrap the system-prompt field in the openai branch of the `Advanced` disclosure:

```tsx
								{!unsupported.includes('systemPrompt') && (
									<>
										<Label className="text-xs">
											{t('subagent-sidebar.systemPromptLabel')}
										</Label>
										<Textarea
											value={form.systemPrompt}
											onChange={set('systemPrompt')}
											rows={2}
										/>
									</>
								)}
```

Then **delete** the `readsLocalFilesOpenai` checkbox block entirely - the whole
`<div className="mt-1 flex items-start gap-2">…</div>` that carries
`id="readsLocalFilesOpenai"`. The cli branch's `readsLocalFiles` checkbox stays:
there the declaration is real, because a CLI agent running in a container or on
a remote host genuinely cannot see this filesystem.

- [ ] **Step 3: Confirm nothing still sends the dropped value**

Run: `grep -n "readsLocalFiles" ui-webui/frontend/src/pages/subagent/*.ts*`

`form.ts` may still carry `readsLocalFiles` in `FormState` and write it in
`toEntry`'s openai branch. If `toEntry` sets it for the openai branch, that is
now a value the gateway will reject whenever it is `true`: change that branch to
omit the field entirely, and state in your report which line you changed. If
`toEntry` already omits it for openai, change nothing and say so.

- [ ] **Step 4: Run the frontend gate**

From `ui-webui/`:

Run: `pnpm -C frontend lint`
Expected: 0 errors.

Run: `pnpm -C frontend build`
Expected: success.

- [ ] **Step 5: Commit**

```bash
git add ui-webui/frontend/src/pages/subagent/catalog.ts \
        ui-webui/frontend/src/pages/subagent/SubagentForm.tsx
git commit -m "$(cat <<'EOF'
feat(ui-webui): stop offering subagent fields the endpoint ignores

MiroThinker's server drops a system-role message, and local-file access is
refused for every openai agent now that nothing can deliver it. A control whose
only outcomes are silence or a validation error is worse than no control, so
both leave the form. The cli local-files checkbox stays, where a container or a
remote host makes the declaration real.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

## Final gate (after Task 8)

- [ ] `uv run pytest -q` - expect only the two pre-existing failures named in Global Constraints.
- [ ] `uv run ruff check raven/ ui-webui/service/ tests/` and `uv run ruff format --check raven/ ui-webui/service/ tests/`
- [ ] From `ui-webui/`: `pnpm -C frontend lint` and `pnpm -C frontend build`
- [ ] `git log --oneline` on the branch, and `grep -nP "[^\x00-\x7F]" ` over each new commit message body to confirm ASCII-only.
- [ ] **Report to the user that browser verification needs the gateway restarted.** The gateway holds the RPC table in memory, so `GET /raven/subagents/probe` 404s until then. Do not restart the user's live stack without asking.
