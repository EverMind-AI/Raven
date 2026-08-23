"""Round-trip fidelity for the ``playbook.md`` three-region layout.

The load side parses the frontmatter region with ``yaml.safe_load``, so the save
side has to emit YAML rather than interpolate strings into it. ``description``
is model-written prose, which makes the characters YAML reserves -- a colon, a
leading ``#``, quotes, a newline -- ordinary content rather than edge cases.
Each of those produced a file that could not be read back, so each gets a case
here. CJK strings appear as unicode escapes to keep the source ASCII; they pin
that non-ASCII prose survives the trip unmangled.
"""

from pathlib import Path

import pytest
from loguru import logger

from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.playbook import NodeSpec, ParamSpec, PlaybookExistsError, PlaybookSpec, PlaybookStore, Triggers

_CJK_WORD = "\u7ade\u54c1"  # jing pin: "competitor"


def _store(tmp_path) -> PlaybookStore:
    """A store pinned to an empty builtin layer, so these single-layer
    assertions stay true when the package grows real builtin playbooks."""
    return PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")


def _spec(description: str) -> PlaybookSpec:
    return PlaybookSpec(
        name="competitor-scan",
        description=description,
        mode="dag",
        triggers=Triggers(keywords=[_CJK_WORD]),
        nodes=[NodeSpec(id="scan", subagent="research-raven", prompt_template="research ${params.target}")],
        params={"target": ParamSpec(required=True, description="which competitor should be scanned?")},
    )


#: label -> description. Every entry is a character YAML reserves but prose
#: uses freely; ``at_cap`` guards the other end -- the contract caps the field at
#: 200 characters, and a value sitting on the cap must not be folded or trimmed.
_DESCRIPTIONS = {
    "colon": "for due diligence: scan broadly first, then dig into open doubts",
    "full_width_colon": "trigger\uff1awhen the user wants a quick company briefing",
    "leading_hash": "#1 priority: competitor comparison",
    "inline_hash": "benchmark A/B # excluding the pricing page",
    "double_quotes": 'matches when the user says "check the competitors"',
    "single_quotes": "matches when the user says 'benchmark this'",
    "newline": "first line of the intent\nsecond line with details",
    "yaml_keywords": "null true false ~ - [] {} @ ` |",
    "cjk_prose": "\u5c3d\u8c03\u65f6\u5148\u5e7f\u5ea6\u626b\u63cf",
    "at_cap": "scan" * 50,
}


@pytest.mark.parametrize("label", list(_DESCRIPTIONS), ids=list(_DESCRIPTIONS))
def test_description_survives_round_trip(tmp_path, label):
    description = _DESCRIPTIONS[label]
    store = _store(tmp_path)
    spec = _spec(description)
    store.save(spec)

    loaded = store.load(spec.name)
    assert loaded.description == description, label
    # The block region has to come back intact too: a frontmatter that fails to
    # terminate would swallow the body and the fenced block with it.
    assert loaded.nodes is not None and [n.id for n in loaded.nodes] == ["scan"]
    assert loaded.triggers.keywords == [_CJK_WORD]
    assert loaded.params["target"].required is True


def test_saved_file_keeps_the_three_regions(tmp_path):
    store = _store(tmp_path)
    store.save(_spec("trigger: competitor comparison"))

    text = (tmp_path / "competitor-scan" / "playbook.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert text.count("---\n") >= 2
    assert "```yaml playbook-spec\n" in text
    # The human region names the playbook, so a reader who opens the file sees
    # what it is before any machine field.
    assert "# competitor-scan" in text


def test_save_writes_one_file_and_nothing_else(tmp_path):
    """One directory, one playbook.md: no sidecar travels with the file."""
    store = _store(tmp_path)
    store.save(_spec("no sidecar"))
    entries = sorted(p.name for p in (tmp_path / "competitor-scan").iterdir())
    assert entries == ["playbook.md"]


def test_notes_render_into_the_body_not_the_block(tmp_path):
    store = _store(tmp_path)
    store.save(_spec("with notes"), notes=["Open question: what is the scan for?", "Assumption: weekly cadence"])

    text = (tmp_path / "competitor-scan" / "playbook.md").read_text(encoding="utf-8")
    body, block = text.split("```yaml playbook-spec", 1)
    assert "## Open questions" in body
    assert "- Open question: what is the scan for?" in body
    assert "Assumption: weekly cadence" in body
    assert "Open question" not in block

    # The body is informational only: the notes do not come back as fields.
    loaded = store.load("competitor-scan")
    assert loaded.description == "with notes"


def test_hand_written_directory_loads(tmp_path):
    """A directory holding only a hand-written playbook.md is a playbook."""
    target = tmp_path / "hand-made"
    target.mkdir()
    (target / "playbook.md").write_text(
        "---\nname: hand-made\ndescription: written by hand\n---\n\n"
        "free-form body the machine never parses\n\n"
        "```yaml playbook-spec\n"
        "version: 1\nmode: prompt\nconfirm: true\n"
        "triggers:\n  keywords: [handmade]\n"
        "prompts: one research node, then one content node depending on it\n"
        "```\n",
        encoding="utf-8",
    )
    store = _store(tmp_path)
    assert store.list_ids() == ["hand-made"]
    spec = store.load("hand-made")
    assert spec.mode == "prompt" and spec.description == "written by hand"


# --- The two-layer library: a writable user root over the packaged builtin root.


def _write_md(root, name: str, description: str) -> None:
    target = root / name
    target.mkdir(parents=True)
    (target / "playbook.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n\n"
        "```yaml playbook-spec\n"
        "version: 1\nmode: prompt\nconfirm: true\n"
        f"triggers:\n  keywords: [{name}]\n"
        "prompts: one research node\n"
        "```\n",
        encoding="utf-8",
    )


def _layered(tmp_path) -> PlaybookStore:
    (tmp_path / "user").mkdir(exist_ok=True)
    (tmp_path / "builtin").mkdir(exist_ok=True)
    return PlaybookStore(tmp_path / "user", builtin_root=tmp_path / "builtin")


def test_list_ids_merges_both_layers(tmp_path):
    store = _layered(tmp_path)
    _write_md(tmp_path / "builtin", "briefing", "builtin briefing")
    _write_md(tmp_path / "builtin", "shared-name", "builtin flavour")
    _write_md(tmp_path / "user", "shared-name", "user flavour")
    _write_md(tmp_path / "user", "mine", "user only")

    assert store.list_ids() == ["briefing", "mine", "shared-name"]
    assert store.origin_of("briefing") == "builtin"
    assert store.origin_of("mine") == "user"
    assert store.origin_of("shared-name") == "user"
    assert store.origin_of("absent") is None


def test_user_layer_shadows_builtin_and_warns_once(tmp_path):
    store = _layered(tmp_path)
    _write_md(tmp_path / "builtin", "shared-name", "builtin flavour")
    _write_md(tmp_path / "user", "shared-name", "user flavour")

    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        first = store.load("shared-name")
        second = store.load("shared-name")
    finally:
        logger.remove(sink_id)

    assert first.description == "user flavour"
    assert second.description == "user flavour"
    shadow_warnings = [line for line in lines if "shadows the builtin" in line]
    assert len(shadow_warnings) == 1


def test_builtin_is_read_only_through_save(tmp_path):
    """No write path reaches the builtin layer: a name clash without
    ``overwrite`` refuses, and with it the write lands as a user shadow."""
    store = _layered(tmp_path)
    _write_md(tmp_path / "builtin", "competitor-scan", "the shipped one")
    before = (tmp_path / "builtin" / "competitor-scan" / "playbook.md").read_bytes()

    with pytest.raises(PlaybookExistsError):
        store.save(_spec("my own take"))

    saved_to = store.save(_spec("my own take"), overwrite=True)
    assert saved_to == tmp_path / "user" / "competitor-scan" / "playbook.md"
    assert (tmp_path / "builtin" / "competitor-scan" / "playbook.md").read_bytes() == before
    assert store.is_shadowing("competitor-scan")
    assert store.load("competitor-scan").description == "my own take"


def test_generated_playbook_lands_in_the_user_layer(tmp_path):
    store = _layered(tmp_path)
    path = store.save(_spec("fresh from the generator"))
    assert path == tmp_path / "user" / "competitor-scan" / "playbook.md"
    assert store.origin_of("competitor-scan") == "user"


def test_shipped_builtins_load_and_validate(tmp_path):
    """The packaged library is not exempt from its own rules: every shipped
    playbook loads, passes the field definition, and has a trigger vocabulary."""
    from raven.playbook.store import BUILTIN_ROOT
    from raven.playbook.validate import validate_structure

    store = PlaybookStore(tmp_path / "empty-user", builtin_root=BUILTIN_ROOT)
    names = store.list_ids()
    assert names == ["deep-dive", "topic-briefing"]
    modes = set()
    for name in names:
        spec = store.load(name)
        assert store.origin_of(name) == "builtin"
        assert validate_structure(spec) == [], name
        assert spec.triggers.keywords, name
        modes.add(spec.mode)
    # One of each mode, so both execution paths ship a worked example.
    assert modes == {"dag", "prompt"}


@pytest.mark.parametrize("name", ["../escape", "/tmp/absolute", "UPPER", "a b", ""])
def test_save_refuses_a_name_that_is_not_a_directory_name(tmp_path, name):
    """The name is the directory the file lands in, and both creation entries
    can reach save() with a value pydantic never checked (model_copy runs no
    validators) -- so the write is where the shape is enforced."""
    store = _store(tmp_path)
    spec = _spec("fine").model_copy(update={"name": name})
    with pytest.raises(ValueError):
        store.save(spec)
    assert list(tmp_path.rglob("playbook.md")) == []


def test_a_playbook_saved_by_the_previous_release_still_loads(tmp_path: Path) -> None:
    """The failure this guards was silent, which is what made it bad.

    `block_dump` is `exclude_none`, not `exclude_defaults`, so every playbook the
    previous release wrote carries `confirm: false` on each node -- a key the
    unified node model forbids. `PlaybookRuntime` catches a load error as a
    warning, so the user's saved procedure just stopped existing.

    The `skills: []` limb is the quieter half: it used to fold to "all skills" and
    now means "none", so such a file would have loaded and then run every step with
    an empty menu.
    """
    (tmp_path / "legacy-book").mkdir()
    (tmp_path / "legacy-book" / "playbook.md").write_text(
        "---\n"
        "name: legacy-book\n"
        "description: written by the previous release\n"
        "---\n\n"
        "Body.\n\n"
        "```yaml playbook-spec\n"
        "version: 1\n"
        "mode: dag\n"
        "confirm: true\n"
        "triggers:\n"
        "  keywords: [legacy run]\n"
        "params: {}\n"
        "nodes:\n"
        "- id: n1\n"
        "  agent: code-raven\n"
        "  promptTemplate: p\n"
        "  dependsOn: []\n"
        "  skills: []\n"
        "  mcps: []\n"
        "  confirm: false\n"
        "```\n",
        encoding="utf-8",
    )
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")

    spec = store.load("legacy-book")

    assert spec.nodes[0].skills is None, "an old empty list meant 'all', so it must read as unset"
    assert spec.nodes[0].mcps is None
    assert spec.confirm is True  # the graph-level gate is untouched


def test_an_empty_skills_list_written_today_is_left_alone(tmp_path: Path) -> None:
    """The migration is scoped by the marker that dates the file.

    A node-level `confirm` only ever came from the old writer, so its presence is
    what licenses rewriting the empty lists. Without that scoping, `skills: []` on
    a current playbook -- a deliberate "no skills at all" -- would be silently
    widened to the agent's whole menu.
    """
    (tmp_path / "current-book").mkdir()
    (tmp_path / "current-book" / "playbook.md").write_text(
        "---\n"
        "name: current-book\n"
        "description: written on this release\n"
        "---\n\n"
        "Body.\n\n"
        "```yaml playbook-spec\n"
        "version: 1\n"
        "mode: dag\n"
        "triggers:\n"
        "  keywords: [current run]\n"
        "nodes:\n"
        "- id: n1\n"
        "  agent: code-raven\n"
        "  promptTemplate: p\n"
        "  skills: []\n"
        "```\n",
        encoding="utf-8",
    )
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")

    assert store.load("current-book").nodes[0].skills == []


def test_a_step_naming_a_retired_builtin_agent_is_repointed_at_raven(tmp_path: Path) -> None:
    """Every playbook written before this release names one of four labels.

    ``research-raven`` / ``code-raven`` / ``data-raven`` / ``content-raven`` were
    rows on the agent table whose only content was a description: same provider,
    same model, same tools, same system prompt as ``raven``, and the sub-agent was
    never told which name it ran under. Removing them makes those steps fail
    validation ("agent not in the registry"), and the failure is silent -- the
    runtime logs a warning and the playbook drops out of the library, so a user's
    saved procedure just stops existing. The rewrite changes the target name and
    nothing about what runs it.
    """
    (tmp_path / "old-book").mkdir()
    (tmp_path / "old-book" / "playbook.md").write_text(
        "---\n"
        "name: old-book\n"
        "description: names the labels\n"
        "---\n\n"
        "Body.\n\n"
        "```yaml playbook-spec\n"
        "version: 1\n"
        "mode: dag\n"
        "triggers:\n"
        "  keywords: [old run]\n"
        "nodes:\n"
        "- id: scan\n"
        "  agent: research-raven\n"
        "  promptTemplate: p\n"
        "- id: brief\n"
        "  subagent: content-raven\n"
        "  promptTemplate: q\n"
        "  dependsOn: [scan]\n"
        "```\n",
        encoding="utf-8",
    )
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")

    spec = store.load("old-book")

    assert [node.subagent for node in spec.nodes] == [GENERIC_AGENT, GENERIC_AGENT]


def test_an_external_agent_whose_name_ends_in_raven_is_left_alone(tmp_path: Path) -> None:
    """The rewrite is by name, not by suffix.

    A user is free to register their own agent as ``myteam-raven``; it is a real
    agent on the table, and repointing its steps at the in-process loop would
    silently run someone else's work somewhere else.
    """
    (tmp_path / "mine").mkdir()
    (tmp_path / "mine" / "playbook.md").write_text(
        "---\n"
        "name: mine\n"
        "description: my own agent\n"
        "---\n\n"
        "Body.\n\n"
        "```yaml playbook-spec\n"
        "version: 1\n"
        "mode: dag\n"
        "triggers:\n"
        "  keywords: [mine]\n"
        "nodes:\n"
        "- id: n1\n"
        "  agent: myteam-raven\n"
        "  promptTemplate: p\n"
        "```\n",
        encoding="utf-8",
    )
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")

    assert store.load("mine").nodes[0].subagent == "myteam-raven"
