"""The tests that bind the EverOS roles into this process leave it as they found it.

``bind_roles_here`` -- reached through ``EverosBackend.start()``, and through
any runtime built with the memory plugin on -- writes the four roles into
``os.environ`` directly, records them in ``raven_everos.config._BOUND_HERE``
and publishes that record as ``RAVEN_EVEROS_BOUND``. ``monkeypatch`` undoes
none of it: it records only what it changed itself, and ``delenv(...,
raising=False)`` on a name that is not there records nothing. So a binding made
during one test was still in the process when the next began, and which test
paid depended on how the suite was dealt into shards: a settings test reading a
saved endpoint was told the environment outranks it (``role_is_env_managed``)
and refused the write, in one shard and not in another.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from raven_everos import config as cf
from tests._everos_env import confined_everos_binding, forget_everos_binding

ROOT = Path(__file__).resolve().parents[1]

#: What a binding leaves behind, by name: the role variables and ``EVEROS_ROOT``
#: share the prefix; the provenance record has a name of its own.
_PROVENANCE = "RAVEN_EVEROS_BOUND"


def _is_binding(name: str) -> bool:
    return name.startswith("EVEROS_") or name == _PROVENANCE


@pytest.mark.slow
@pytest.mark.parametrize(
    "target",
    [
        # start() binds, and the file asserts on the binding
        "tests/test_everos_backend.py",
        # a runtime assembled with the memory plugin on binds, through the plugin's start
        "tests/test_core_runtime_eval_hooks.py",
        "tests/test_agents_ppt_launcher.py::test_the_products_tool_face_is_the_forks_config_intent_plus_the_deck",
    ],
)
def test_a_file_that_binds_the_roles_leaves_the_process_as_it_found_it(target: str) -> None:
    """Run the binding tests in an interpreter of their own, then look at what is left.

    A fixture's teardown is the one thing the test it wraps cannot observe, so
    the check is made from outside: a child interpreter started with no
    ``EVEROS_*`` variable runs ``target`` under the suite's own conftest --
    which is where the confinement lives (``_no_leaked_everos_binding``) --
    then reports every such variable, the provenance variable and the
    provenance set it still holds. Marked slow for the child's wall clock, not
    for a wait of its own: the parent does nothing but read the pipe.
    """
    child = textwrap.dedent(
        f"""
        import json, os, pytest
        rc = pytest.main(["-q", "-n", "0", "-p", "no:cacheprovider", "--no-header", {target!r}])
        from raven_everos import config as cf
        left = {{k: v for k, v in os.environ.items() if k.startswith("EVEROS_") or k == cf.PROVENANCE_ENV}}
        print("LEFT=" + json.dumps({{"rc": int(rc), "env": left, "bound": sorted(cf._BOUND_HERE)}}))
        """
    )
    # Started clean, so anything reported was made by ``target``; and outside the
    # parent's xdist worker and coverage tracer, which a child of a test is not.
    env = {k: v for k, v in os.environ.items() if not (_is_binding(k) or k.startswith(("PYTEST_", "COV_CORE_")))}
    done = subprocess.run(
        [sys.executable, "-c", child], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False
    )
    line = next((ln for ln in reversed(done.stdout.splitlines()) if ln.startswith("LEFT=")), None)
    assert line is not None, f"the child did not report:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}"
    left = json.loads(line[len("LEFT=") :])
    assert left["rc"] == 0, f"{target} did not pass on its own:\n{done.stdout[-3000:]}"
    assert left["env"] == {}, f"{target} left these in the environment: {left['env']}"
    assert left["bound"] == [], f"{target} left these in the provenance set: {left['bound']}"


def _binding_now() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if _is_binding(key)}


def test_the_block_undoes_a_binding_made_inside_it() -> None:
    env_before, bound_before = _binding_now(), set(cf._BOUND_HERE)

    with confined_everos_binding():
        cf.bind_roles_here()
        assert os.environ[_PROVENANCE] and cf._BOUND_HERE, "the block is not what stops the binding"

    assert _binding_now() == env_before
    assert cf._BOUND_HERE == bound_before


def test_the_block_puts_back_what_was_there_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator's export, and the record of a binding made earlier, are the state the block began in."""
    monkeypatch.setenv("EVEROS_LLM__MODEL", "the-operators-model")
    monkeypatch.setenv(_PROVENANCE, "EVEROS_RERANK__MODEL")
    monkeypatch.setattr(cf, "_BOUND_HERE", {"EVEROS_RERANK__MODEL"})

    with confined_everos_binding():
        cf.bind_roles_here()
        # One exported variable is not an endpoint, so the binding writes over it.
        assert os.environ["EVEROS_LLM__MODEL"] != "the-operators-model"
        assert "EVEROS_LLM__MODEL" in cf._BOUND_HERE

    assert os.environ["EVEROS_LLM__MODEL"] == "the-operators-model"
    assert os.environ[_PROVENANCE] == "EVEROS_RERANK__MODEL"
    assert cf._BOUND_HERE == {"EVEROS_RERANK__MODEL"}


def test_the_block_puts_the_process_back_when_the_block_raises() -> None:
    env_before, bound_before = _binding_now(), set(cf._BOUND_HERE)

    with pytest.raises(RuntimeError), confined_everos_binding():
        cf.bind_roles_here()
        raise RuntimeError("the test under the fixture failed")

    assert _binding_now() == env_before
    assert cf._BOUND_HERE == bound_before


def test_forgetting_leaves_nothing_exported_and_puts_it_back_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVEROS_RERANK__MODEL", "exported-by-the-operator")
    monkeypatch.setenv(_PROVENANCE, "EVEROS_RERANK__MODEL")
    monkeypatch.setattr(cf, "_BOUND_HERE", {"EVEROS_RERANK__MODEL"})

    with pytest.MonkeyPatch.context() as inner:
        forget_everos_binding(inner)
        assert not _binding_now()
        assert cf._BOUND_HERE == set()
        cf.bind_roles_here()  # a case's own binding lands in the swapped set

    assert os.environ["EVEROS_RERANK__MODEL"] == "exported-by-the-operator"
    assert os.environ[_PROVENANCE] == "EVEROS_RERANK__MODEL"
    assert cf._BOUND_HERE == {"EVEROS_RERANK__MODEL"}
