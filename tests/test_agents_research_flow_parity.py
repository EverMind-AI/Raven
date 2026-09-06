"""The launcher's own config is the flow we measured, minus a written-down allowlist.

The fork's ``tests/test_shipped_flow_parity.py`` holds every config the fork
ships to the arm that produced published numbers. The config the deployed
launcher actually serves is not one of them: ``subagents/raven-research/run.py``
defaults to the parent folder's ``config.json``, and the fork's ``acp.modes``
compose the ``modes/*.json`` overlays over it per session. That is the product's
fast baseline plus its two deeper modes, and until this file none of the three
was under any parity guard - the fork's suite cannot see its caller, and this
trunk's CI does not run the fork's suite.

Effective values, not written keys, from the fork's own schema: the trunk has a
``raven`` package of the same name, so the comparison runs in a subprocess with
the fork's checkout first on the path. Modes go through the fork's own
``build_session_modes`` - the by-alias dump, ``_deep_merge`` and revalidation the
ACP server performs - rather than a re-implementation here, so a divergence
between the launcher's catalogue and the server's merge is a failure, not a
gap between two copies of the merge.

The same probe reports the fork's class defaults and retired-label tables, so
the trunk twin (``agents/raven-research/plugins/research-flow``) is held to the
"same fields, same defaults" its docstring promises - the launcher file's slice
test cannot see a default underneath a written value.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FORK = REPO / "subagents" / "raven-research"
RAVEN_X = FORK / "Raven-X"
LAUNCHER = FORK / "run.py"
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"

sys.path.insert(0, str(PLUGIN_DIR))
from research_flow.config import (  # noqa: E402
    _RETIRED_KEYS,
    SUPERSEDED_PROFILES,
    SUPERSEDED_VERSIONS,
    FlowConfig,
)

#: Differences every mode of the launcher may carry. Prefix match; the value is the
#: reason. The fork's UNIVERSAL table (answer shape, backend split, the one raisable
#: verify field) applies first and is read from the fork, not copied.
PRODUCT: dict[str, str] = {
    "identity_override": (
        "the product identity; a measured arm runs the stock prompt section and has "
        "none. Its text is pinned to agents/raven-research/soul.md by the launcher "
        "test, and a change to it moves the drFlow.version suffix - that label, not "
        "this table, is how its distribution is tracked"
    ),
    "ask_user.": (
        "the clarify round is a product surface, resolved against conversation.enabled; "
        "a one-message bench arm cannot reach it"
    ),
    "conversation.": (
        "multi-turn is a product surface, turn two onwards only; a bench arm sends one "
        "message per question and never reaches it, so its gate budget is sized for a "
        "live follow-up and was never measured"
    ),
    "tools_allowlist": (
        "the product exposes the local-file tools beside the two web tools, because "
        "questions here may concern files on the host; the measured arm's questions "
        "were web-only. This is the tool fence, not the web path's research behaviour"
    ),
    "fetch_gate.enabled": (
        "priced hygiene breaker (the fork's examples/README.md five-delta table): "
        "withholds web_search after k searches with no page opened. It cuts a spiral, "
        "not depth, and the measured arm predates it"
    ),
    "search.saturation.": (
        "priced hygiene ladder: repeated searches are removed and 'widen' broadens the "
        "query family instead of stopping. The measured arm ran without the ladder"
    ),
    "sufficiency.": (
        "the first-round sufficiency gate is this checkout's own patch "
        "(dr_sufficiency_gate); the measured arm predates it. It appends a note the "
        "model may disregard and removes no tools. Its floors and timeouts move with "
        "each mode's budget, and ultra switches it off, where they are inert"
    ),
}

#: Knobs one mode legitimately moves. Every mode the launcher catalogues must have
#: an entry, empty or not, so a new mode is covered the day it ships.
PER_MODE: dict[str, dict[str, str]] = {
    "fast": {
        "max_iterations": (
            "the fast baseline caps a turn so a default question answers in minutes. "
            "NOT behaviour-inert: a question still researching at the cap ends on the "
            "exhaustion path (the turn is marked interrupted and synthesized on "
            "exhaustion) where the measured arm, uncapped at this level, ran on. "
            "Accepted as the fast mode's promise, with the window named; deep raises "
            "the cap and ultra removes it"
        ),
        "budget_note.warn_ratio": (
            "the fast mode's depth knob, paired with its cap: the converge push lands "
            "early in the turn instead of near its end"
        ),
        # Two LEAVES, not the ``verify.`` prefix: the reason below permits a shorter
        # budget and nothing else. A prefix would also have excused turning the
        # reviewer or its measured rubrics off, and a reviewer's mutant showed the
        # guard staying green through exactly that.
        "verify.timeout_seconds": (
            "fast mode shortens the reviewer's budget. NOT inert: a review that times "
            "out fails open as 'unavailable', and the answer ships unreviewed wearing "
            "the banner. Accepted as the fast mode's latency promise; deep and ultra "
            "restore the measured budgets"
        ),
        "verify.attempt_timeout_seconds": ("the per-attempt half of the same shortened budget, same window as above"),
    },
    "deep": {
        "max_iterations": (
            "deep raises the fast cap rather than removing it. The same exhaustion "
            "window as fast, named rather than excused"
        ),
    },
    "ultra": {},
}

#: Runs inside the fork's checkout. Prints one JSON object: the measured arm, each
#: mode's effective flow, a deliberately broken baseline, and the fork's own
#: UNIVERSAL / INERT keys.
_PROBE = r"""
import importlib.util, json, sys, tempfile
from pathlib import Path

args = json.loads(sys.argv[1])
import raven
assert Path(raven.__file__).resolve().parent.parent == Path.cwd().resolve(), raven.__file__

spec = importlib.util.spec_from_file_location("shipped_flow_parity", args["parity_module"])
parity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parity)
from raven.acp.modes import build_session_modes
from raven.config import load_raven_config
from raven.config.loader import load_config
from raven.config.raven import DRFlowConfig

tmp = Path(tempfile.mkdtemp())
launcher_spec = importlib.util.spec_from_file_location("research_launcher", args["launcher"])
launcher = importlib.util.module_from_spec(launcher_spec)
launcher_spec.loader.exec_module(launcher)

source = json.loads(Path(args["config"]).read_text(encoding="utf-8"))
catalogue = launcher.mode_catalogue()
source.setdefault("acp", {})["modes"] = catalogue
source["acp"]["defaultMode"] = launcher.BASELINE_MODE
served = tmp / "config.json"
served.write_text(json.dumps(source), encoding="utf-8")
modes = build_session_modes(load_config(served), load_raven_config(served))
# The path a session takes: session/set_mode, then the profile its next engine is built from.
flows = {}
for mode_id in modes.ids():
    modes.set("probe", mode_id)
    flows[mode_id] = parity._flat(modes.profile("probe").dr_flow)

fork_defaults = DRFlowConfig()
broken_doc = json.loads(Path(args["config"]).read_text(encoding="utf-8"))
broken_doc["drFlow"].setdefault("spinBreaker", {})["enabled"] = False
broken_doc["drFlow"].setdefault("digest", {})["verbatimHeadChars"] = 0
broken = tmp / "broken.json"
broken.write_text(json.dumps(broken_doc), encoding="utf-8")

out = {
    "reference": parity._flat(load_raven_config(parity.REFERENCE).dr_flow),
    "modes": flows,
    "broken": parity._flat(load_raven_config(broken).dr_flow),
    "universal": sorted(parity.UNIVERSAL),
    "inert": sorted(parity.INERT),
    "fork_defaults": parity._flat(fork_defaults),
    "fork_superseded_profiles": dict(fork_defaults._SUPERSEDED_PROFILES),
    "fork_superseded_versions": list(fork_defaults._SUPERSEDED_VERSIONS),
}
print(json.dumps(out, default=str))
"""


@pytest.fixture(scope="module")
def probe() -> dict:
    args = {
        "parity_module": str(RAVEN_X / "tests" / "test_shipped_flow_parity.py"),
        "launcher": str(LAUNCHER),
        "config": str(FORK / "config.json"),
    }
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(args)],
        cwd=RAVEN_X,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _excused(path: str, mode: str, universal: list[str]) -> str | None:
    for table in (dict.fromkeys(universal, "fork UNIVERSAL"), PRODUCT, PER_MODE.get(mode, {})):
        for key, why in table.items():
            if path == key or path.startswith(key):
                return why
    return None


def _drift(flow: dict, reference: dict, mode: str, probe: dict) -> list[str]:
    bad = []
    for key in sorted(flow):
        if key in probe["inert"] or flow[key] == reference.get(key):
            continue
        if _excused(key, mode, probe["universal"]) is None:
            bad.append(f"{key}: shipped={flow[key]!r} measured={reference.get(key)!r}")
    return bad


def test_every_mode_the_launcher_serves_is_the_measured_flow_or_says_why(probe):
    """What the deployed launcher runs has to be what we measured, per mode.

    A difference in research behaviour has two possible causes - the product is
    missing a mechanism we measured and kept, or it runs a combination nobody
    tested - and both belong here, in red, with the mode named.
    """
    assert set(probe["modes"]) == set(PER_MODE), (
        "the launcher catalogues modes this table does not cover (or vice versa); "
        "every served mode needs a PER_MODE entry, even an empty one"
    )
    drift = {mode: _drift(flow, probe["reference"], mode, probe) for mode, flow in probe["modes"].items()}
    drift = {mode: lines for mode, lines in drift.items() if lines}
    assert not drift, (
        "the launcher drifts from the measured arm on research behaviour:\n"
        + "\n".join(f"  [{mode}] {line}" for mode, lines in drift.items() for line in lines)
        + "\n\nEither set it to the measured value, or add it to PRODUCT / PER_MODE "
        "together with a REASON."
    )


def test_losing_the_reviewer_is_not_a_shorter_budget(probe):
    """The fast excuses permit a shorter verify budget and nothing else.

    A reviewer switched off, or either measured rubric dropped, is a research
    behaviour change on the fast path, and the guard has to say so even though
    the two timeout leaves beside those fields are excused. Mutated on the
    served fast flow, the way the reviewer who found the hole did it.
    """
    fast = dict(probe["modes"]["fast"])
    reference = probe["reference"]
    for field in ("verify.enabled", "verify.constraint_rubric", "verify.strict_reject_only"):
        assert fast[field] == reference[field] is True, field
        fast[field] = False
    drift = _drift(fast, reference, "fast", probe)
    assert sorted(line.split(":")[0] for line in drift) == [
        "verify.constraint_rubric",
        "verify.enabled",
        "verify.strict_reject_only",
    ], drift


def test_the_check_can_actually_fail(probe):
    """A baseline with a dropped backstop must be caught, and an excused field must not.

    Without the first half this file would also pass if the flattener returned
    nothing; without the second it would redden on everything, which is as
    useless as reddening on nothing.
    """
    drift = _drift(probe["broken"], probe["reference"], "fast", probe)
    assert any(line.startswith("spin_breaker.enabled:") for line in drift), drift
    assert any(line.startswith("digest.verbatim_head_chars:") for line in drift), drift
    assert not any(line.startswith("final_shape.") for line in drift), drift


def test_every_allowlist_entry_names_something_real(probe):
    """A typo'd excuse silently waves a genuine difference through."""
    known = set(probe["reference"])
    tables = [(PRODUCT, "PRODUCT")] + [(v, f"PER_MODE[{k}]") for k, v in PER_MODE.items()]
    for table, label in tables:
        for path in table:
            hit = path in known or any(k.startswith(path) for k in known)
            assert hit, f"{label} names {path!r}, which is not a DRFlowConfig field"


def test_the_probe_reads_the_launcher_the_host_actually_spawns():
    """The manifest's command and this file's probe must name the same launcher."""
    manifest = json.loads((FORK / "subagent.json").read_text(encoding="utf-8"))
    assert "run.py" in manifest["command"], manifest["command"]
    spec = importlib.util.spec_from_file_location("research_launcher", LAUNCHER)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    assert launcher.DEFAULT_CONFIG == FORK / "config.json"
    assert set(launcher.mode_catalogue()) == set(PER_MODE)


def _flat(model, prefix: str = "") -> dict[str, object]:
    """The fork's flattener, for the trunk side of the comparison (same shape)."""
    out: dict[str, object] = {}
    for name in type(model).model_fields:
        value = getattr(model, name)
        if hasattr(type(value), "model_fields"):
            out.update(_flat(value, prefix + name + "."))
        else:
            out[prefix + name] = value
    return out


def test_the_twins_defaults_are_the_forks(probe):
    """Class defaults, not the slice: what a profile gets when it does not pin a knob.

    The trunk twin's own docstring promises the fork's defaults, and the slice
    test in the launcher file cannot see them - a value the config writes hides
    the default underneath. The one difference the twin declares is the four
    retired LOOP knobs, which is exactly the set of fork fields it may lack.
    """
    twin = json.loads(json.dumps(_flat(FlowConfig()), default=str))
    fork = probe["fork_defaults"]
    retired = {snake for snake, _owner in _RETIRED_KEYS.values()}
    missing = {key for key in fork if key not in twin}
    assert {key.split(".", 1)[0] for key in missing} == retired, missing
    assert not set(twin) - set(fork), set(twin) - set(fork)
    drift = {key: (twin[key], fork[key]) for key in twin if twin[key] != fork[key]}
    assert not drift, "twin default != fork default (twin, fork): " + repr(drift)


def test_the_twins_retired_labels_are_the_forks(probe):
    """Both launchers refuse the same labels, or a stale one stamps an old distribution."""
    assert SUPERSEDED_PROFILES == probe["fork_superseded_profiles"]
    assert list(SUPERSEDED_VERSIONS) == probe["fork_superseded_versions"]
