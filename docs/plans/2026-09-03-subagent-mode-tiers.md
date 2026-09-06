# Sub-agent mode tiers - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give raven a session-scoped tier - `medium`, `high`, `max` - that it applies to the sub-agents it dispatches, settable over ACP `session/set_mode` and from `/mode` in the terminal, with raven's own behaviour identical in every tier.

**Architecture:** The delivery pipe already exists end to end (measure a sub-agent's modes from its handshake, offer them to the model, resolve one per dispatch, re-send on every route into its session). This plan adds the three things it lacks: a declared vocabulary (a schema default on `AcpConfig.modes`), a place for a person to state a standing choice (the loop's existing `SessionPolicy.mode`, reached by a new `session.set_mode` RPC and by `/mode` in the main conversation), and a layer in `resolve_mode` that applies that choice to a dispatch, clamped to what the target agent actually offers.

**Tech Stack:** Python 3.12+, pydantic v2, pytest (via `uv run`), TypeScript/React (Ink) for the terminal, OpenRPC schema codegen.

**Spec:** `docs/specs/2026-09-03-subagent-mode-tiers-design.md`

## Global Constraints

- Base branch is `origin/refactor/raven_v0_2_0`, not `origin/main`. Work happens on `feat/subagent_mode_tiers` in the worktree `.claude/worktrees/subagent_mode_tiers`.
- **Do not commit without the user's explicit word** (AGENTS.md 3.4). The commit step in each task is the point at which to stop and ask, not a licence.
- All source comments, docstrings, commit messages and this repo's docs are **English and ASCII only** (AGENTS.md 1.2, 3.1.1). No em-dash, curly quotes or ellipsis - use `--` and `...`.
- Do not add a comment unless the logic is non-obvious or there is a hidden constraint (AGENTS.md 1.1). Match the density of the surrounding lines.
- Python packages are managed with `uv` only. Run tests as `uv run pytest ...`, never bare `pytest` (AGENTS.md 4, 5.4).
- `uv run pytest` needs `--all-extras`, and **the flag belongs to `uv run`, so it goes BEFORE `pytest`**: `uv run --all-extras pytest ...`. Placed after pytest's arguments it is rejected as an unrecognized argument. Without it the run silently skips roughly 4300 ppt tests while still printing green, and reports spurious `ppt-engine` / `everos-memory` plugin failures.
- Never rename or restructure an existing test file; extend it (AGENTS.md 5.4).
- Two generated artefacts are drift-checked by `make` and by nothing in the merge-request pipeline: `ui-tui/src/rpc/generated.ts` from `rpc-schema/openrpc.json`, and `ui-tui/src/i18n/messages.generated.ts` from the repository-root `i18n/messages.json`. Regenerate in the same commit as the source change.
- The terminal test suite flakes above ~100 files under default worker parallelism. Run it serially.

## File Structure

| File | Responsibility |
|---|---|
| `raven/config/schema.py` | Declares the three tiers as the `AcpConfig.modes` default, and `TIER_LADDER` as their ordering. Config owns its own defaults. |
| `raven/agent/subagent/mode_tiers.py` (new) | `clamp_tier` - a pure function from (tier, what an agent offers) to the rung to send. Imports `TIER_LADDER` from config; imports nothing else. |
| `raven/agent/subagent/manager.py` | `resolve_mode` gains the tier layer and the two log lines. Takes an injected tier reader. |
| `raven/agent/loop/main.py` | Builds the tier reader and hands it to `SubagentManager`. |
| `raven/rpc/methods/session.py` | `session.set_mode` - the terminal's half of the same switch the ACP surface already serves. |
| `rpc-schema/openrpc.json` | Declares `session.set_mode` so the terminal gets a generated type for it. |
| `ui-tui/src/app/slash/commands/core.ts` | `/mode` gains its main-conversation branch, replacing the refusal. |

---

### Task 1: The built-in catalogue

**Files:**
- Modify: `raven/config/schema.py:649-655` (`AcpConfig`)
- Test: `tests/test_acp_modes.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `raven.config.schema.TIER_LADDER: tuple[str, ...]` = `("medium", "high", "max")`; `AcpConfig.modes` defaulting to three `AcpModeConfig` entries keyed `medium` / `high` / `max`; `AcpConfig.default_mode` defaulting to `"high"`.

- [ ] **Step 1: Rewrite the test that states the contract this task moves**

`tests/test_acp_modes.py:21` asserts that an untouched `Config()` serves no mode surface. That stops being true. The test's intent - an empty catalogue serves nothing - survives by building the empty catalogue explicitly. Replace the existing `test_no_declared_modes_means_no_surface` with:

```python
def test_an_explicitly_empty_catalogue_means_no_surface():
    config = Config()
    config.acp = AcpConfig(modes={})
    modes = build_session_modes(config)
    assert not modes.enabled
    assert modes.state("s1") is None
    assert modes.profile("s1") is None


def test_an_untouched_config_serves_the_three_built_in_tiers():
    modes = build_session_modes(Config())
    assert modes.enabled
    assert modes.ids() == ("medium", "high", "max")
    assert modes.default == "high"


def test_the_built_in_tiers_are_inert_for_raven_itself():
    profile = build_session_modes(Config()).profile("s1")
    assert profile.max_iterations is None
    assert profile.overlay == {}


def test_a_declared_catalogue_replaces_the_built_in_one():
    config = Config()
    config.acp = AcpConfig(modes={"turbo": AcpModeConfig(name="Turbo")})
    modes = build_session_modes(config)
    assert modes.ids() == ("turbo",)
    assert modes.default == "turbo", "an unknown default degrades to the first declared"
```

The last case is why no `default_mode` validator may be added: `default_mode` is now `"high"` by default, and a product declaring its own catalogue without naming a default must degrade, not fail at startup.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_acp_modes.py -v`
Expected: `test_an_untouched_config_serves_the_three_built_in_tiers` FAILS on `assert modes.enabled`; `test_the_built_in_tiers_are_inert_for_raven_itself` FAILS with `AttributeError: 'NoneType' object has no attribute 'max_iterations'`; `test_a_declared_catalogue_replaces_the_built_in_one` FAILS on `modes.default == "turbo"` only if a validator was wrongly added - it should pass already.

- [ ] **Step 3: Declare the ladder and the catalogue**

In `raven/config/schema.py`, immediately above `class AcpModeConfig` (line 630):

```python
TIER_LADDER: tuple[str, ...] = ("medium", "high", "max")
"""The built-in tiers, cheapest first. The order is what makes a clamp possible,
so it is a constant here rather than something read back off the catalogue."""

> Amended after review: shipped as `_TIER_TEXTS`, one sentence per rung saying only
> what differs, with the scope stated once per surface and the text carried as a
> `raven.i18n` message id. See the Risks entries in the design doc.

_TIER_TEXT = "Sub-agents run at their {tier} tier. Raven's own effort is the same in every mode."
```

Then replace the body of `AcpConfig` (lines 649-655) with:

```python
def _builtin_modes() -> dict[str, "AcpModeConfig"]:
    return {
        tier: AcpModeConfig(name=tier.capitalize(), description=_TIER_TEXT.format(tier=tier)) for tier in TIER_LADDER
    }


class AcpConfig(Base):
    """The ACP surface's session modes.

    The three built-in tiers move what raven asks of its SUB-AGENTS, not what
    raven does: every one leaves ``maxToolIterations`` inherited and ``overlay``
    empty. A deployment that declares its own catalogue replaces this one whole;
    one that writes ``"modes": {}`` turns the surface off entirely.
    """

    modes: dict[str, AcpModeConfig] = Field(default_factory=_builtin_modes)
    default_mode: str | None = "high"
    """Which mode a new session starts in. Deliberately unvalidated: a product
    that declares its own catalogue without naming a default must degrade to its
    first entry (``SessionModes.__init__``), not be failed at startup by a value
    it never chose."""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_acp_modes.py -v`
Expected: all cases PASS.

- [ ] **Step 5: Confirm the defaults never reach a user's config file**

Run:

```bash
uv run python -c "
from raven.config.schema import Config
print(Config().model_dump(by_alias=True, exclude_defaults=True).get('acp', 'absent'))"
```

Expected: `absent`. `save_config` (`raven/config/loader.py:474`) writes with `exclude_defaults=True`, so the three tiers must not materialise on disk.

- [ ] **Step 6: Check nothing else assumed an empty catalogue**

Run: `uv run --all-extras pytest tests/ -k "acp or config" -q`
Expected: no new failures against a baseline taken on the unmodified branch. Any test that constructs a bare `Config()` and asserts no modes is the same contract change as Step 1 - fix it the same way, do not weaken the assertion.

- [ ] **Step 7: Commit** (stop and ask first - AGENTS.md 3.4)

```bash
git add raven/config/schema.py tests/test_acp_modes.py
git commit -m "feat(config): declare medium/high/max as the built-in acp mode catalogue"
```

---

### Task 2: The clamp

**Files:**
- Create: `raven/agent/subagent/mode_tiers.py`
- Test: `tests/test_subagent_mode_resolution.py` (create)

**Interfaces:**
- Consumes: `raven.config.schema.TIER_LADDER` from Task 1.
- Produces: `clamp_tier(tier: str, offered: Iterable[str]) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_subagent_mode_resolution.py`:

```python
"""The session tier as one dispatch sees it: the clamp, then the precedence chain."""

from __future__ import annotations

import pytest

from raven.agent.subagent.mode_tiers import clamp_tier


@pytest.mark.parametrize(
    ("tier", "offered", "expected"),
    [
        ("high", ("medium", "high", "max"), "high"),
        ("max", ("medium", "high", "max"), "max"),
        ("max", ("medium", "high"), "high"),
        ("max", ("medium",), "medium"),
        ("high", ("medium",), "medium"),
        ("medium", ("high", "max"), "high"),
        ("high", ("max",), "max"),
        ("high", (), None),
        ("high", ("deep", "ultra"), None),
        ("deep", ("fast", "deep", "ultra"), None),
        ("", ("medium", "high", "max"), None),
        ("high", ("high", "medium", "max"), "high"),
    ],
)
def test_the_clamp_lands_on_the_nearest_rung_preferring_cheaper(tier, offered, expected):
    assert clamp_tier(tier, offered) == expected


def test_a_tier_outside_the_ladder_is_never_approximated():
    """`deep` is not "nearest" to `medium`; it is a word from another vocabulary.

    Guarding this is the whole reason the ladder is a constant rather than the
    catalogue's key order: a deployment that renamed its modes must fall through
    to the agent's own default, not be clamped onto an unrelated id.
    """
    assert clamp_tier("ultra", ("medium", "high", "max")) is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_subagent_mode_resolution.py -v`
Expected: collection error - `ModuleNotFoundError: No module named 'raven.agent.subagent.mode_tiers'`.

- [ ] **Step 3: Write the implementation**

Create `raven/agent/subagent/mode_tiers.py`:

```python
"""Fitting raven's session tier onto whatever rungs one sub-agent actually has.

The tier is raven's word; the menu is the agent's, measured from its own ACP
handshake. The two need not match, and every mismatch has a defined answer here
rather than at each call site.
"""

from __future__ import annotations

from typing import Iterable

from raven.config.schema import TIER_LADDER


> Amended after the pre-submit sweep: the shipped `clamp_tier` gates the
> climb below on the whole menu being rankable, which the block here does not.
> MR !472 gives `raven-code` a `low` / `max` catalogue, and `low` is illegible
> from the ladder -- so "the cheapest thing on offer" was resolving a `medium`
> session onto `max`. See the Risks entry in the design doc.

def clamp_tier(tier: str, offered: Iterable[str]) -> str | None:
    """The rung to ask this agent for, or ``None`` to leave it on its default.

    Nearest at or below, and the cheapest thing on offer when nothing is below.
    ``None`` in the two cases where a choice would be a guess: a tier outside the
    ladder (another vocabulary, where "nearest" has no meaning), and an agent
    sharing no rung with it at all.
    """
    if tier not in TIER_LADDER:
        return None
    have = [rung for rung in TIER_LADDER if rung in set(offered)]
    if not have:
        return None
    if tier in have:
        return tier
    below = [rung for rung in have if TIER_LADDER.index(rung) < TIER_LADDER.index(tier)]
    return below[-1] if below else have[0]
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_subagent_mode_resolution.py -v`
Expected: 13 PASSED.

- [ ] **Step 5: Commit** (stop and ask first)

```bash
git add raven/agent/subagent/mode_tiers.py tests/test_subagent_mode_resolution.py
git commit -m "feat(subagent): clamp a session tier onto the rungs one agent offers"
```

---

### Task 3: The tier layer in `resolve_mode`

**Files:**
- Modify: `raven/agent/subagent/manager.py:164-180` (constructor), `:958-986` (`resolve_mode`)
- Modify: `raven/agent/loop/main.py:419-431` (manager construction)
- Test: `tests/test_subagent_mode_resolution.py` (extend)

**Interfaces:**
- Consumes: `clamp_tier` from Task 2; `AgentLoop.session_policy(session_key)` returning a `SessionPolicy` with a `.mode: str`; `SubagentManager.agent_modes(agent)` returning objects with an `.id`.
- Produces: `SubagentManager(..., session_tier: Callable[[str | None], str] | None = None)`; `resolve_mode` unchanged in signature, changed in behaviour.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_mode_resolution.py`:

```python
from types import SimpleNamespace

from raven.agent.subagent.manager import SubagentManager


def _manager(tier: str, menus: dict[str, tuple[str, ...]]) -> SubagentManager:
    """A manager with the tier reader bound and `agent_modes` stubbed.

    Built with `__new__`: the real constructor wants a provider and a workspace,
    and none of the resolution path touches either.
    """
    mgr = SubagentManager.__new__(SubagentManager)
    mgr._instance_modes = {}
    mgr._session_tier = lambda _key: tier
    mgr.agent_modes = lambda agent: tuple(  # type: ignore[method-assign]
        SimpleNamespace(id=rung) for rung in menus.get(agent, ())
    )
    return mgr


def test_the_tier_applies_to_a_spawn_that_names_no_instance():
    """The case the old short-circuit dropped: `instance` is falsy on most spawns."""
    mgr = _manager("max", {"coder": ("medium", "high", "max")})
    assert mgr.resolve_mode("s1", "coder", None) == "max"


def test_the_tier_is_clamped_to_what_the_agent_offers():
    mgr = _manager("max", {"coder": ("medium", "high")})
    assert mgr.resolve_mode("s1", "coder", None) == "high"


def test_an_agent_sharing_no_rung_runs_on_its_own_default():
    mgr = _manager("max", {"researcher": ("deep", "ultra")})
    assert mgr.resolve_mode("s1", "researcher", None) is None


def test_an_explicit_request_beats_the_tier():
    mgr = _manager("medium", {"coder": ("medium", "high", "max")})
    # Withdrawn after review: `requested` and the spawn tool's `mode` property are
    # both gone, so this assertion has no capability left to test. See the design
    # doc's "The per-dispatch pick is withdrawn rather than ranked."
    assert mgr.resolve_mode("s1", "coder", "inst", requested="max") == "max"


def test_an_instance_override_beats_the_tier():
    mgr = _manager("medium", {"coder": ("medium", "high", "max")})
    mgr._instance_modes[("s1", "coder", "inst")] = "max"
    assert mgr.resolve_mode("s1", "coder", "inst") == "max", "a narrower statement wins"


def test_a_manager_with_no_tier_reader_behaves_exactly_as_before():
    mgr = _manager("", {"coder": ("medium", "high", "max")})
    mgr._session_tier = None
    assert mgr.resolve_mode("s1", "coder", None) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_subagent_mode_resolution.py -v -k "tier or request or instance or reader"`
Expected: `test_the_tier_applies_to_a_spawn_that_names_no_instance` FAILS (returns `None` - the `instance` short-circuit); the two clamp cases FAIL the same way. `test_an_explicit_request_beats_the_tier` and `test_an_instance_override_beats_the_tier` PASS already, and must keep passing - they are the regression guard for the precedence order.

- [ ] **Step 3: Accept the tier reader in the manager**

In `raven/agent/subagent/manager.py`, add a parameter to `__init__` after `session_dir` (line 179):

```python
        session_tier: "Callable[[str | None], str] | None" = None,
```

and beside the other assignments in the constructor body:

```python
        # Reads the session's standing tier off the loop's SessionPolicy. Injected
        # rather than reached for: the manager has no loop reference, and a test
        # rig that passes none keeps the pre-tier behaviour exactly.
        self._session_tier = session_tier
```

- [ ] **Step 4: Add the layer to `resolve_mode`**

Replace the body of `resolve_mode` (`manager.py:982-986`, everything after the docstring) with:

```python
        if requested:
            return requested
        if instance:
            override = self.instance_mode(session_key, agent or "", instance)
            if override:
                return override
        return self._tier_for(session_key, agent or "")

    def _tier_for(self, session_key: str | None, agent: str) -> str | None:
        """The session's standing tier as this agent can take it, or ``None``.

        Runs whether or not a handle was named: a spawn that names no instance is
        the common case, and it is the one a fleet-wide tier exists for.
        """
        if self._session_tier is None:
            return None
        tier = self._session_tier(session_key)
        if not tier:
            return None
        offered = tuple(getattr(mode, "id", "") for mode in self.agent_modes(agent))
        landed = clamp_tier(tier, offered)
        seen = (agent, tier)
        if landed is None:
            if seen not in _TIER_MISS_SEEN:
                _TIER_MISS_SEEN.add(seen)
                logger.info(
                    "sub-agent {}: offers no tier from {}; running on its own default", agent, "/".join(TIER_LADDER)
                )
        elif landed != tier and seen not in _TIER_MISS_SEEN:
            _TIER_MISS_SEEN.add(seen)
            logger.info("sub-agent {}: tier {!r} not offered; running at {!r}", agent, tier, landed)
        return landed
```

Also update the docstring's first line to say what it now does, and add near the top of the module, beside the other module-level state:

```python
# Tier mismatches already reported, so a busy session logs one line per agent
# rather than one per dispatch. Same shape and reason as `_STALE_SNAPSHOT_SEEN`
# in raven/agent/subagent/backends/__init__.py.
_TIER_MISS_SEEN: set[tuple[str, str]] = set()
```

Add the imports at the top of `manager.py`:

```python
from raven.agent.subagent.mode_tiers import clamp_tier
from raven.config.schema import TIER_LADDER
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_mode_resolution.py -v`
Expected: all PASS, including the two precedence cases that passed before.

- [ ] **Step 6: Build the reader on the loop**

In `raven/agent/loop/main.py`, add before the `SubagentManager(` call at line 419:

```python
        # The catalogue's default stands in for a session that never set a tier:
        # `_apply_mode` stamps a policy only on the ACP turn path, so a terminal,
        # gateway or channel turn would otherwise resolve to no tier at all.
        try:
            from raven.acp.modes import build_session_modes
            from raven.config.loader import load_config

            self._default_tier = build_session_modes(load_config()).default
        except Exception:
            self._default_tier = ""
```

and pass it into the constructor call:

```python
            session_tier=lambda key: self.session_policy(key or "").mode or self._default_tier,
```

- [ ] **Step 7: Verify the wiring end to end**

Run: `uv run --all-extras pytest tests/test_subagent_mode_resolution.py tests/test_rpc_instances.py tests/test_subagent_acp.py -q`
Expected: all PASS. `test_rpc_instances.py` passing is the proof the per-instance override did not regress.

- [ ] **Step 8: Commit** (stop and ask first)

```bash
git add raven/agent/subagent/manager.py raven/agent/loop/main.py tests/test_subagent_mode_resolution.py
git commit -m "feat(subagent): apply the session tier to a dispatch, clamped per agent"
```

---

### Task 4: `session.set_mode`

**Files:**
- Modify: `raven/rpc/methods/session.py` (new handler + `register_session_methods` at `:1054`)
- Modify: `rpc-schema/openrpc.json`
- Regenerate: `ui-tui/src/rpc/generated.ts`
- Test: `tests/test_rpc_session.py` (EXISTS, 2455 lines at the branch base -- append, never recreate)

**Interfaces:**
- Consumes: `raven.acp.modes.build_session_modes`, `raven.config.loader.load_config`, `AgentLoop.set_session_policy` / `.session_policy`.
- Produces: RPC `session.set_mode`, params `{session_key: str, mode?: str, clear?: bool}`, result `{mode: str | null, availableModes: [{id, name, description}]}`. Generated TS type `SessionSetModeResult`.

- [ ] **Step 1: Write the failing test**

APPEND to `tests/test_rpc_session.py` (it already exists -- do not recreate it):

```python
"""session.set_mode: the terminal's half of the switch ACP already serves."""

from __future__ import annotations

import pytest

from raven.rpc.errors import ConfigValidationError

from raven.rpc.methods.session import session_set_mode


class _Loop:
    def __init__(self) -> None:
        self.policies: dict[str, str] = {}

    def set_session_policy(self, key, *, max_iterations=None, mode="", mode_overlay=None):
        self.policies[key] = mode

    def session_policy(self, key):
        from raven.agent.loop._shared import SessionPolicy

        return SessionPolicy(mode=self.policies.get(key, ""))


async def test_a_read_reports_the_default_and_the_whole_menu():
    loop = _Loop()
    result = await session_set_mode({"session_key": "tui:1"}, agent_loop_factory=lambda: loop)
    assert result["mode"] == "high"
    assert [m["id"] for m in result["availableModes"]] == ["medium", "high", "max"]


async def test_a_set_lands_on_the_loop_policy():
    loop = _Loop()
    result = await session_set_mode({"session_key": "tui:1", "mode": "max"}, agent_loop_factory=lambda: loop)
    assert result["mode"] == "max"
    assert loop.policies["tui:1"] == "max"


async def test_a_clear_returns_to_the_catalogue_default():
    loop = _Loop()
    await session_set_mode({"session_key": "tui:1", "mode": "medium"}, agent_loop_factory=lambda: loop)
    result = await session_set_mode({"session_key": "tui:1", "clear": True}, agent_loop_factory=lambda: loop)
    assert result["mode"] == "high"
    assert loop.policies["tui:1"] == "high"


async def test_an_unknown_tier_is_refused_naming_what_is_on_offer():
    loop = _Loop()
    with pytest.raises(ConfigValidationError) as exc:
        await session_set_mode({"session_key": "tui:1", "mode": "turbo"}, agent_loop_factory=lambda: loop)
    assert "medium" in str(exc.value) and "high" in str(exc.value) and "max" in str(exc.value)
    assert loop.policies == {}, "a refused set must not half-land"
```

`pytest-asyncio` runs in `asyncio_mode = "auto"` (`pyproject.toml:360`), so a bare `async def test_` needs no marker - which is what every `tests/test_rpc_*.py` does. `ConfigValidationError` is imported from `raven.rpc.errors`, as at `raven/rpc/methods/instances.py:43`.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --all-extras pytest tests/test_rpc_session.py -v`
Expected: the four new cases fail with `ImportError: cannot import name 'session_set_mode'`; the file's pre-existing cases still pass.

- [ ] **Step 3: Write the handler**

Add to `raven/rpc/methods/session.py`, beside the other handlers:

```python
async def session_set_mode(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """Report, set or clear this session's sub-agent tier (``session.set_mode``).

    The same switch ``session/set_mode`` serves over ACP, resolved against the
    same catalogue and landing on the same per-session policy, so the two
    surfaces cannot accept different words. Three calls, told apart by which
    fields are present rather than by a sentinel id.

    A tier moves what raven asks of the sub-agents it dispatches. Raven's own
    effort is the same in every tier.
    """
    from raven.acp.modes import build_session_modes
    from raven.config.loader import load_config

    modes = build_session_modes(load_config())
    menu = [{"id": m.id, "name": m.name, "description": m.description} for m in modes._profiles.values()]
    session_key = str(params.get("session_key") or "")
    raw = params.get("mode")
    wanted = str(raw) if isinstance(raw, str) and raw else None
    loop = None
    if agent_loop_factory is not None:
        try:
            loop = agent_loop_factory()
        except Exception:
            loop = None

    def _current() -> str | None:
        if loop is None:
            return modes.default or None
        return getattr(loop.session_policy(session_key), "mode", "") or modes.default or None

    if wanted is None and not params.get("clear"):
        return {"mode": _current(), "availableModes": menu}
    if loop is None:
        raise ConfigValidationError("no agent loop, so this session has no tier to set")
    tier = modes.default if params.get("clear") else wanted
    if tier not in modes.ids():
        raise ConfigValidationError(f"no mode {tier!r}; this build offers {', '.join(modes.ids()) or 'none'}")
    modes.set(session_key, tier)
    loop.set_session_policy(session_key, mode=tier, mode_overlay=modes.profile(session_key).overlay)
    return {"mode": tier, "availableModes": menu}
```

Reading `modes._profiles` from outside is the one wart. If `SessionModes` has no public iterator by the time this is written, add one to `raven/acp/modes.py` rather than reaching through the underscore:

```python
    def profiles(self) -> tuple[AcpModeProfile, ...]:
        return tuple(self._profiles.values())
```

and use `modes.profiles()` here.

- [ ] **Step 4: Register it**

In `register_session_methods` (`raven/rpc/methods/session.py:1054`), add beside the other closures:

```python
    async def _set_mode(params: dict) -> dict:
        return await session_set_mode(params, agent_loop_factory=agent_loop_factory)
```

and beside the other `dispatcher.register` lines:

```python
    dispatcher.register("session.set_mode", _set_mode)
```

Add `"session_set_mode"` to the module's `__all__`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --all-extras pytest tests/test_rpc_session.py -v`
Expected: the four new cases pass, and the file's pre-existing cases are unchanged and still pass.

- [ ] **Step 6: Declare it in the RPC schema and regenerate**

In `rpc-schema/openrpc.json`, add a method beside `subagents.instance.set_mode`, matching that entry's shape exactly:

```json
{
  "name": "session.set_mode",
  "summary": "Report, set or clear the sub-agent tier this session dispatches at. Neither field reports; clear=true returns to the configured default; mode=<id> switches from the next turn on. Raven's own effort is the same in every tier.",
  "params": [
    { "name": "session_key", "required": true, "schema": { "type": "string" } },
    { "name": "mode", "required": false, "schema": { "type": "string" } },
    { "name": "clear", "required": false, "schema": { "type": "boolean" } }
  ],
  "result": {
    "name": "SessionSetModeResult",
    "schema": {
      "type": "object",
      "additionalProperties": false,
      "required": [],
      "properties": {
        "mode": {
          "type": ["string", "null"],
          "description": "The tier now in force."
        },
        "availableModes": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["id"],
            "properties": {
              "id": { "type": "string" },
              "name": { "type": "string" },
              "description": { "type": "string" }
            }
          }
        }
      }
    }
  }
}
```

Then:

```bash
npm run gen:rpc --prefix ui-tui
npm run lint:rpc --prefix ui-tui
```

Expected: `generated.ts` gains `SessionSetModeParams` / `SessionSetModeResult` and `lint:rpc` reports no drift. Do not check the file's header comment against the count - it claims "37 RPC methods" against a schema of 160 and 388 exported interfaces, so it is already stale and the generator does not rewrite it.

- [ ] **Step 7: Confirm the contract test still holds**

Run: `uv run --all-extras pytest tests/test_rpc_contract_shapes.py tests/test_rpc_stubs.py -q`
Expected: PASS. These compare the schema against the registered handlers; a mismatch here means the params in Step 6 and the handler in Step 3 disagree.

- [ ] **Step 8: Commit** (stop and ask first)

```bash
git add raven/rpc/methods/session.py rpc-schema/openrpc.json ui-tui/src/rpc/generated.ts tests/test_rpc_session.py
git commit -m "feat(rpc): add session.set_mode for the session's sub-agent tier"
```

---

### Task 5: `/mode` in the main conversation

**Files:**
- Modify: `ui-tui/src/app/slash/commands/core.ts:262-330`
- Test: `ui-tui/src/__tests__/instanceModeCommand.test.ts` (extend)

**Interfaces:**
- Consumes: `SessionSetModeResult` from `../../../rpc/generated.js` (Task 4); the existing `RESET_WORDS` (`core.ts:57`) and `getDirectChat()`.
- Produces: no new exports - the command's behaviour in one more context.

- [ ] **Step 1: Write the failing tests**

Append to `ui-tui/src/__tests__/instanceModeCommand.test.ts`, inside a new `describe`:

```ts
describe('/mode in the main conversation', () => {
  beforeEach(() => {
    resetDirectChat()
  })

  it('reports the tier and the menu with no argument', async () => {
    const rpc = vi.fn(() => Promise.resolve({ availableModes: TIERS, mode: 'high' }))
    const h = run('', rpc)
    await settle()
    expect(rpc).toHaveBeenCalledWith('session.set_mode', { session_key: 's1' }, { quiet: true })
    expect(h.main.join('\n')).toContain('high')
  })

  it('sets the tier and says it lands next turn', async () => {
    const rpc = vi.fn(() => Promise.resolve({ availableModes: TIERS, mode: 'max' }))
    const h = run('max', rpc)
    await settle()
    expect(rpc).toHaveBeenCalledWith('session.set_mode', { mode: 'max', session_key: 's1' }, { quiet: true })
    expect(h.main.join('\n')).toContain('next message')
  })

  it('sends clear for a reset word', async () => {
    const rpc = vi.fn(() => Promise.resolve({ availableModes: TIERS, mode: 'high' }))
    const h = run('default', rpc)
    await settle()
    expect(rpc).toHaveBeenCalledWith('session.set_mode', { clear: true, session_key: 's1' }, { quiet: true })
  })

  it('no longer refuses outside a sub-agent chat', async () => {
    const h = run('', vi.fn(() => Promise.resolve({ availableModes: TIERS, mode: 'high' })))
    await settle()
    expect(h.main.join('\n')).not.toContain('applies to a sub-agent chat')
  })
})
```

Define beside the existing `MODES` fixture:

```ts
const TIERS = [
  { id: 'medium', name: 'Medium', description: 'sub-agents run at their medium tier' },
  { id: 'high', name: 'High', description: 'sub-agents run at their high tier' },
  { id: 'max', name: 'Max', description: 'sub-agents run at their max tier' }
]
```

The existing `run` helper enters no direct chat by default, so these cases exercise the main-conversation branch without extra setup. If the file's helper does not already expose a way to await the RPC promise, reuse whatever the existing direct-chat cases use rather than inventing a second mechanism.

- [ ] **Step 2: Run them to verify they fail**

Run: `npx vitest run src/__tests__/instanceModeCommand.test.ts --prefix ui-tui` (from `ui-tui/`: `npx vitest run src/__tests__/instanceModeCommand.test.ts`)
Expected: the three new cases FAIL - the command returns the refusal string and never calls the RPC.

- [ ] **Step 3: Replace the refusal with the session-tier branch**

In `ui-tui/src/app/slash/commands/core.ts`, change the command's `help` and `usage`:

```ts
    help: 'show or change the effort tier -- of this conversation, or of the sub-agent you are chatting with',
    name: 'mode',
    usage: '/mode [<id> | default]   -- default/reset/clear return to the default',
```

and replace the refusal block (lines 267-274) with a fork that picks the call, keeping everything below it as-is:

```ts
      const { active } = getDirectChat()

      if (!ctx.sid) {
        return ctx.transcript.sys('no active session')
      }

      const want = arg.trim()
      const clearing = RESET_WORDS.has(want.toLowerCase())
      // One command, two scopes: inside a direct chat it is that instance's
      // mode, and on the main conversation it is the tier raven dispatches its
      // sub-agents at. Both read as "the effort of whoever I am talking to".
      const method = active ? 'subagents.instance.set_mode' : 'session.set_mode'
      const params: Record<string, unknown> = active
        ? { agent: active.agent, handle: active.handle, session_key: ctx.sid }
        : { session_key: ctx.sid }
```

Then, in the `.then` block, make the two sentences name the right subject:

```ts
            const subject = active ? `${active.agent}/${active.handle}` : 'this conversation'
```

and use `subject` in place of the `${active.agent}/${active.handle}` interpolations, and `active ? active.agent : 'this build'` in place of the bare `${active.agent}` in the no-modes line.

The subject must be SINGULAR: it is spliced into the unchanged templates `${subject} is now on ...` and `${subject} is on ...`, which the direct-chat path shares. A plural subject renders "sub-agents in this conversation is now on max" -- and no substring assertion catches it, so assert the whole first line in the set case.

Update the `rpc` call site to `ctx.gateway.rpc<SubagentsInstanceSetModeResult | SessionSetModeResult>(method, params, { quiet: true })` and import `SessionSetModeResult` alongside the existing type on `core.ts:17`.

- [ ] **Step 4: Run the whole file to verify both branches pass**

Run (from `ui-tui/`): `npx vitest run src/__tests__/instanceModeCommand.test.ts`
Expected: the new cases PASS and every pre-existing direct-chat case still PASSES. A direct-chat regression here means the fork changed a path it should not have.

- [ ] **Step 5: Run the terminal suite serially**

This worktree has no `ui-tui/node_modules`, so nothing terminal-side runs until they exist. Install once with `npm ci --prefix ui-tui`; do NOT run a separate `npm ci` inside `ui-tui/packages/hermes-ink`, which pulls in a second React copy and breaks every render test.

Run (from `ui-tui/`): `npx vitest run --no-file-parallelism`
Expected: no new failures against a baseline taken on the unmodified branch. The suite flakes above ~100 files under default parallelism, so a parallel run's reds prove nothing. (`--no-file-parallelism` is the documented switch on the pinned vitest 4.1.3; `vitest.config.ts` sets no pool options of its own.)

- [ ] **Step 6: Commit** (stop and ask first)

```bash
git add ui-tui/src/app/slash/commands/core.ts ui-tui/src/__tests__/instanceModeCommand.test.ts
git commit -m "feat(tui): /mode sets this conversation's sub-agent tier"
```

---

## Final verification

- [ ] `uv run --all-extras pytest -q` - **take the baseline on `9bc06091` first**, before any edit, and compare against it. This tree's pre-existing failures have not been measured; do not assume a known set, and do not read a red as yours without the A/B.
- [ ] `make lint` - the merge-request pipeline runs only the Python unit suite, so lint, `lint:rpc` and `lint:i18n` are caught here or not at all. **`ruff format --check` is the half of `lint-python` that a task's own `ruff check` does NOT cover**, and this repo's pre-commit hooks are disabled (`core.hooksPath` points at a nonexistent directory), so nothing catches a formatting regression before CI. Run it per task, not only here.
- [ ] `git diff origin/refactor/raven_v0_2_0...HEAD` reviewed against the `mr-review-patterns` pre-submit sweep. This is mandatory before pushing.
- [ ] Confirm the wire actually changed: start `raven acp`, send `initialize` then `session/new`, and check the response carries `modes.currentModeId == "high"` with three `availableModes`.
- [ ] Confirm nothing changed for sub-agents yet. None of the five `subagents/*/config.json` carries an `acp` key; their menus are empty because their own vendored Raven trees predate the catalogue. So a spawn must still resolve to no mode and log the "offers no tier" line once per agent.
