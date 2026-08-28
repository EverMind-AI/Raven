"""Tests for the ``playbooks.*`` RPC handlers.

Three things the page depends on and nothing else asserts:

* the list carries each graph's *shape* (a card draws a diagram per row, and a
  card that fetched its own graph would make opening the page N round trips);
* a file that will not parse comes back as a row with ``error`` set, not as a
  failed call -- the library is two directories of hand-edited text;
* the detail keeps a blank field blank. ``subagent`` / ``node_summary`` /
  ``prompt_template`` are the three an author may leave for the caller to fill,
  and a wire that omitted them would read as "no such field".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.playbook import NodeSpec, ParamSpec, PlaybookSpec, PlaybookStore, Triggers
from raven.rpc.errors import RpcError
from raven.rpc.methods import playbooks as mod
from raven.rpc.models import METHOD_MODELS


def _spec(name: str = "competitor-scan") -> PlaybookSpec:
    return PlaybookSpec(
        name=name,
        description="research one competitor's market and tech sides, then merge",
        task_summary="research the named competitor and report what was found",
        mode="dag",
        triggers=Triggers(keywords=["competitor"]),
        params={"target": ParamSpec(type="string", required=True, description="which competitor to scan")},
        nodes=[
            NodeSpec(id="market", subagent="Raven", node_summary="the market side", prompt_template="market"),
            NodeSpec(id="tech", subagent="Raven", node_summary="the tech side", prompt_template="tech"),
            NodeSpec(
                id="merge",
                subagent="Raven",
                node_summary="merge both",
                prompt_template="merge {{ market.output }} {{ tech.output }}",
                depends_on=["market", "tech"],
                instance="w1",
                skills=["web-research"],
            ),
        ],
    )


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlaybookStore:
    """A two-layer store on tmp, installed as the one the handlers resolve.

    The builtin layer is pinned empty so these assertions stay true when the
    package ships builtin playbooks.
    """
    store = PlaybookStore(tmp_path / "user", builtin_root=tmp_path / "builtin")
    monkeypatch.setattr(mod, "_store", lambda: store)
    monkeypatch.setattr(mod, "_disabled", lambda: set())
    return store


async def test_list_carries_the_graph_shape(library: PlaybookStore) -> None:
    library.save(_spec())
    rows = (await mod.playbooks_list({}))["playbooks"]
    assert [r["name"] for r in rows] == ["competitor-scan"]
    row = rows[0]
    assert row["error"] == ""
    assert row["mode"] == "dag"
    assert [(n["id"], n["depends_on"]) for n in row["nodes"]] == [
        ("market", []),
        ("tech", []),
        ("merge", ["market", "tech"]),
    ]
    # Shape only: the card has no use for prompts, and the library would
    # otherwise ship every template on page open.
    assert set(row["nodes"][0]) == {"id", "depends_on"}
    METHOD_MODELS["playbooks.list"][1].model_validate({"playbooks": rows})


async def test_unreadable_file_is_a_row_not_a_failure(library: PlaybookStore) -> None:
    library.save(_spec())
    broken = library.path_for("broken-one")
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("not a playbook at all\n", encoding="utf-8")

    rows = {r["name"]: r for r in (await mod.playbooks_list({}))["playbooks"]}
    assert rows["competitor-scan"]["error"] == ""
    assert rows["broken-one"]["error"]
    assert rows["broken-one"]["nodes"] == []
    METHOD_MODELS["playbooks.list"][1].model_validate({"playbooks": list(rows.values())})


async def test_get_answers_one_whole_spec(library: PlaybookStore) -> None:
    library.save(_spec())
    got = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]
    assert got["keywords"] == ["competitor"]
    assert got["params"]["target"]["required"] is True
    assert got["params"]["target"]["description"] == "which competitor to scan"
    merge = [n for n in got["nodes"] if n["id"] == "merge"][0]
    assert merge["depends_on"] == ["market", "tech"]
    assert merge["instance"] == "w1"
    assert merge["skills"] == ["web-research"]
    # Not written by the author: the key is absent, which is how the contract
    # spells it and what the typed client expects. Told apart from `[]`.
    assert "mcps" not in merge
    assert got["path"].endswith("competitor-scan/playbook.md")
    METHOD_MODELS["playbooks.get"][1].model_validate({"playbook": got})


async def test_get_reports_the_version_the_file_declares(library: PlaybookStore) -> None:
    """Read from the spec, not pinned to the constant the writer happened to use."""
    spec = _spec()
    spec.version = 7
    library.save(spec, overwrite=True)

    got = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]
    assert got["version"] == 7
    METHOD_MODELS["playbooks.get"][1].model_validate({"playbook": got})


async def test_get_keeps_a_blank_field_blank(library: PlaybookStore) -> None:
    spec = _spec()
    # A blank is an empty string on the model; the wire keeps it that way.
    spec.nodes.append(NodeSpec(id="angle"))
    library.save(spec, overwrite=True)

    got = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]
    angle = [n for n in got["nodes"] if n["id"] == "angle"][0]
    assert angle["subagent"] == ""
    assert angle["node_summary"] == ""
    assert angle["prompt_template"] == ""
    METHOD_MODELS["playbooks.get"][1].model_validate({"playbook": got})


async def test_get_refuses_an_unknown_name(library: PlaybookStore) -> None:
    with pytest.raises(RpcError):
        await mod.playbooks_get({"name": "nope"})
    with pytest.raises(RpcError):
        await mod.playbooks_get({})
