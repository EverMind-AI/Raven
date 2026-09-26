"""Composition preserves source identity and includes all known uses of selected children."""

import pytest

from experimental.curator.composition.context import merge_sources
from experimental.curator.composition.run import _groups


@pytest.mark.parametrize("reverse", [False, True])
def test_shared_uses_are_context_without_enrolling_unrequested_children(reverse):
    nodes = {
        "support/revise": {
            "node": {"subagent": "Shared", "prompt_template": "Revise confirmed work"},
            "requirements": [],
        },
        "sales/prepare": {
            "node": {"subagent": "Shared", "prompt_template": "Prepare new work"},
            "requirements": [{"behavior": "Collect missing inputs"}],
        },
        "other/prepare": {"node": {"subagent": "Other"}, "requirements": []},
    }
    if reverse:
        nodes = dict(reversed(nodes.items()))
    grouped = _groups(nodes, {"Shared"})
    assert set(grouped) == {"Shared"}
    assert grouped["Shared"] == {key: nodes[key] for key in ("support/revise", "sales/prepare")}
    assert nodes["support/revise"]["requirements"] == []
    nodes["sales/prepare"]["requirements"] = []
    assert _groups(nodes, {"Shared"}) == {}
    nodes["other/prepare"]["requirements"] = [{"behavior": "Collect missing inputs"}]
    with pytest.raises(ValueError, match="unprepared child Harness: Other"):
        _groups(nodes, {"Shared"})


def test_shared_protocols_reuse_sources_but_child_implementations_keep_their_own_names():
    protocol = {"path": "/repo/protocol.py", "digest": "same", "start": 1, "end": 40}
    sources = {"protocol": protocol}
    incoming = {
        "strategy.protocol": dict(protocol),
        "strategy.impl": {"path": "/child/impl.py", "digest": "own", "start": 1, "end": 20},
        "strategy.other_excerpt": {**protocol, "start": 20},
    }
    aliases = merge_sources(sources, incoming, "child.Research")
    assert aliases["strategy.protocol"] == "protocol"
    assert sources[aliases["strategy.impl"]] == incoming["strategy.impl"]
    assert aliases["strategy.other_excerpt"] != "protocol"
    assert len(sources) == 3
    with pytest.raises(ValueError, match="collision"):
        merge_sources(sources, {"strategy.impl": {**incoming["strategy.impl"], "digest": "changed"}}, "child.Research")


def test_only_dag_playbooks_contribute_nodes_that_can_hold_requirements(tmp_path):
    from types import SimpleNamespace

    from experimental.curator.raven_adapter.inspection.runtime import playbook_nodes

    specs = {
        "work": SimpleNamespace(nodes=[SimpleNamespace(id="draft", model_dump=lambda mode: {"id": "draft"})]),
        "composed": SimpleNamespace(nodes=None),
    }
    library = SimpleNamespace(names=lambda: list(specs), store=SimpleNamespace(load=specs.__getitem__))
    runtime = SimpleNamespace(loop=SimpleNamespace(_playbooks=library))
    baseline = SimpleNamespace(config=SimpleNamespace(workspace_path=tmp_path))
    assert playbook_nodes(runtime, baseline) == {
        "work/draft": {"playbook": "work", "node": {"id": "draft"}, "requirements": []}
    }
