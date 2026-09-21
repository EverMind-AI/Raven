"""Skills named on a node reach the agent that runs it, whichever kind it is."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from raven.agent.subagent.dag_graph import parse_dag_spec
from raven.agent.subagent.dag_skills import SECTION_CAP, SKILL_BODY_CAP, SKILLS_DIR, fold_skills, skills_section
from raven.agent.subagent.prompt_capabilities import AgentCapabilities


@dataclass
class _Skill:
    name: str
    description: str
    path: Path
    content: str
    source: str = "workspace"
    requires: dict = field(default_factory=dict)


class _Catalog:
    def __init__(self, *skills: _Skill) -> None:
        self._skills = list(skills)

    def list_all(self) -> list[_Skill]:
        return list(self._skills)


def _catalog() -> _Catalog:
    return _Catalog(
        _Skill("game-testing", "how to test a game build", Path("/w/skills/game-testing/SKILL.md"), "Run the demo.\n"),
        _Skill("hub", "the skill hub", Path("/w/skills/hub/SKILL.md"), "Search it.\n"),
    )


def _spec(*nodes: dict):
    return parse_dag_spec({"task_summary": "t", "nodes": [dict(node_summary="s", depends_on=[], **n) for n in nodes]})


def test_a_menu_less_agent_gets_the_same_menu_a_built_in_one_would() -> None:
    """Measured 2026-09-21 on a live stint: Raven-Code, named `skills:
    [game-testing]`, was told the list was ignored and saw only its own
    `local/weather`. The playbook specification had promised the fold all
    along -- and the fold is the menu, not the document: name, description and
    where SKILL.md is, read when the step decides it applies."""
    spec = _spec({"id": "a", "subagent": "coder", "prompt_template": "build it", "skills": ["game-testing"]})

    folded, notices = fold_skills(spec, {"coder": AgentCapabilities(injectable_skills=False)}, _catalog())

    prompt = folded.nodes[0].prompt_template
    assert prompt.startswith("build it")
    section = prompt.split("## Skills for this step")[1]
    assert "<name>game-testing</name>" in section
    assert "<description>how to test a game build</description>" in section
    # No working directory to place it in, so the catalog's own path is named.
    assert "<location>/w/skills/game-testing/SKILL.md</location>" in section
    assert "Read the SKILL.md" in section
    assert "Run the demo." not in section, "the body is read on demand, not pasted"
    assert "hub" not in section
    assert notices == []


def test_an_agent_that_cannot_read_this_filesystem_gets_the_body_instead() -> None:
    """For it the path is a dead end, so the skill is quoted -- the one case
    where progressive disclosure has nothing to disclose progressively."""
    spec = _spec({"id": "a", "subagent": "remote", "prompt_template": "go", "skills": ["game-testing"]})
    caps = {"remote": AgentCapabilities(injectable_skills=False, reads_local_files=False)}

    folded, _ = fold_skills(spec, caps, _catalog())

    section = folded.nodes[0].prompt_template.split("## Skills for this step")[1]
    assert "### game-testing" in section and "Run the demo." in section
    assert "File: /w/skills/game-testing/SKILL.md" in section


def test_an_agent_whose_menu_raven_narrows_is_left_alone() -> None:
    """The list reaches that loop as `skills_allow`; quoting it too would say the
    same thing twice at prompt-budget prices."""
    spec = _spec({"id": "a", "subagent": "raven", "prompt_template": "build it", "skills": ["game-testing"]})

    folded, notices = fold_skills(spec, {"raven": AgentCapabilities(injectable_skills=True)}, _catalog())

    assert folded is spec
    assert notices == []


def test_a_name_the_catalog_lacks_is_a_notice_and_the_rest_still_arrive() -> None:
    spec = _spec({"id": "a", "subagent": "coder", "prompt_template": "go", "skills": ["nope", "hub"]})

    folded, notices = fold_skills(spec, {"coder": AgentCapabilities(injectable_skills=False)}, _catalog())

    assert "<name>hub</name>" in folded.nodes[0].prompt_template
    assert len(notices) == 1 and "'nope'" in notices[0] and "'coder'" in notices[0]


def test_the_qualified_id_a_skill_tool_answers_with_is_accepted_too() -> None:
    section, missing = skills_section(["local/game-testing"], _catalog())

    assert "<name>game-testing</name>" in section and missing == []


def test_an_empty_list_means_no_skills_and_says_so() -> None:
    """`skills: []` hides a built-in loop's menu. The nearest an agent with a
    catalog of its own can be brought is being told not to reach for it."""
    spec = _spec({"id": "a", "subagent": "coder", "prompt_template": "go", "skills": []})

    folded, notices = fold_skills(spec, {"coder": AgentCapabilities(injectable_skills=False)}, _catalog())

    assert "This step uses no skills. Do not reach for your own catalog." in folded.nodes[0].prompt_template
    assert "<skills>" not in folded.nodes[0].prompt_template
    assert notices == []


def test_the_menu_is_the_step_s_whole_menu_not_an_addition() -> None:
    spec = _spec({"id": "a", "subagent": "coder", "prompt_template": "go", "skills": ["hub"]})

    folded, _ = fold_skills(spec, {"coder": AgentCapabilities(injectable_skills=False)}, _catalog())

    assert "the only ones: do not search your own catalog for others" in folded.nodes[0].prompt_template


def test_an_unknown_agent_and_a_node_naming_no_skills_are_untouched() -> None:
    spec = _spec(
        {"id": "a", "subagent": "ghost", "prompt_template": "go", "skills": ["hub"]},
        {"id": "b", "subagent": "coder", "prompt_template": "go"},
    )

    folded, _ = fold_skills(spec, {"coder": AgentCapabilities(injectable_skills=False)}, _catalog())

    assert folded is spec


def test_a_long_skill_is_cut_and_says_where_the_rest_is() -> None:
    catalog = _Catalog(_Skill("big", "a long one", Path("/w/skills/big/SKILL.md"), "x" * (SKILL_BODY_CAP + 500)))

    section, _ = skills_section(["big"], catalog, quote_bodies=True)

    assert "(cut here; the rest is in /w/skills/big/SKILL.md)" in section
    assert section.count("x") == SKILL_BODY_CAP


def test_the_section_as_a_whole_has_a_budget() -> None:
    many = [_Skill(f"s{i}", f"skill {i}", Path(f"/w/skills/s{i}/SKILL.md"), "y" * SKILL_BODY_CAP) for i in range(5)]

    section, _ = skills_section([s.name for s in many], _Catalog(*many), quote_bodies=True)

    assert len(section) < SECTION_CAP + SKILL_BODY_CAP
    assert "(not quoted: this step's skills already fill their budget)" in section
    assert all(f"### s{i}" in section for i in range(5)), "every named skill is at least pointed at"


def _real_catalog(tmp_path: Path) -> _Catalog:
    home = tmp_path / "home" / "skills" / "game-testing"
    home.mkdir(parents=True)
    (home / "SKILL.md").write_text("---\nname: game-testing\n---\nRun the demo.\n", encoding="utf-8")
    (home / "references").mkdir()
    (home / "references" / "gates.md").write_text("the gates\n", encoding="utf-8")
    (home / "__pycache__").mkdir()
    (home / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    return _Catalog(_Skill("game-testing", "how to test a game build", home / "SKILL.md", "Run the demo.\n"))


def test_the_skill_is_placed_inside_the_step_s_working_directory_and_the_menu_points_there(tmp_path: Path) -> None:
    """An agent confined to its working directory -- a raven peer with
    `restrictToWorkspace`, a cli agent's sandbox -- cannot open the host's
    skills tree, and the host cannot tell which agents are confined. Inside
    `.raven/` the copy dirties nothing: a stint's commits leave that directory
    out and its boundary pass does not grade it."""
    work = tmp_path / "project"
    work.mkdir()
    spec = _spec({"id": "a", "subagent": "coder", "prompt_template": "go", "skills": ["game-testing"]})

    folded, notices = fold_skills(
        spec, {"coder": AgentCapabilities(injectable_skills=False)}, _real_catalog(tmp_path), workdir=work
    )

    copy = work / SKILLS_DIR / "game-testing"
    assert (copy / "SKILL.md").read_text(encoding="utf-8").endswith("Run the demo.\n")
    assert (copy / "references" / "gates.md").is_file(), "what the skill refers to travels with it"
    assert not (copy / "__pycache__").exists()
    assert f"<location>{SKILLS_DIR}/game-testing/SKILL.md</location>" in folded.nodes[0].prompt_template
    assert notices == []


def test_a_second_round_refreshes_the_copy_rather_than_tripping_over_it(tmp_path: Path) -> None:
    work = tmp_path / "project"
    work.mkdir()
    catalog = _real_catalog(tmp_path)
    spec = _spec({"id": "a", "subagent": "coder", "prompt_template": "go", "skills": ["game-testing"]})
    caps = {"coder": AgentCapabilities(injectable_skills=False)}

    fold_skills(spec, caps, catalog, workdir=work)
    Path(catalog.list_all()[0].path).write_text("---\nname: game-testing\n---\nRun the demo twice.\n", encoding="utf-8")
    _, notices = fold_skills(spec, caps, catalog, workdir=work)

    assert "twice" in (work / SKILLS_DIR / "game-testing" / "SKILL.md").read_text(encoding="utf-8")
    assert notices == []


def test_a_copy_that_cannot_be_made_falls_back_to_the_catalog_path_and_says_so(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x", encoding="utf-8")
    spec = _spec({"id": "a", "subagent": "coder", "prompt_template": "go", "skills": ["game-testing"]})

    folded, notices = fold_skills(
        spec, {"coder": AgentCapabilities(injectable_skills=False)}, _real_catalog(tmp_path), workdir=not_a_dir
    )

    assert (
        f"<location>{tmp_path / 'home' / 'skills' / 'game-testing' / 'SKILL.md'}</location>"
        in folded.nodes[0].prompt_template
    )
    assert len(notices) == 1 and "could not be copied into the working directory" in notices[0]


def _on_disk(root: Path, folder: str, name: str, body: str = "body\n") -> _Skill:
    """A skill that really exists, so the copy has something to copy."""
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    skill = directory / "SKILL.md"
    skill.write_text(body, encoding="utf-8")
    return _Skill(name, "d", skill, body)


def test_a_skill_is_copied_under_its_own_directory_not_the_name_it_declares(tmp_path: Path) -> None:
    """A catalog entry's name is what its SKILL.md frontmatter says, and the
    registry falls back to the directory only when that field is absent. Built
    into the copy target it let the skill file choose where it was written:
    `../../..` walked out of the working directory and nothing said so."""
    home, work = tmp_path / "home", tmp_path / "project"
    work.mkdir()
    entry = _on_disk(home, "oddly-named", "../../../escaped-by-name")

    text, problems = skills_section(["../../../escaped-by-name"], _Catalog(entry), workdir=work)

    assert not (tmp_path / "escaped-by-name").exists(), "the copy stayed inside the working directory"
    assert (work / SKILLS_DIR / "oddly-named" / "SKILL.md").is_file()
    assert f"{SKILLS_DIR}/oddly-named/SKILL.md" in text
    assert problems == []


def test_a_skill_that_declares_an_absolute_name_cannot_choose_where_it_lands(tmp_path: Path) -> None:
    """`Path(base) / "/etc/x"` is `/etc/x`: an absolute name discarded the base
    entirely, which is sharper than walking out of it."""
    home, work = tmp_path / "home", tmp_path / "project"
    work.mkdir()
    entry = _on_disk(home, "abs-named", str(tmp_path / "pwned"))

    skills_section([str(tmp_path / "pwned")], _Catalog(entry), workdir=work)

    assert not (tmp_path / "pwned").exists()
    assert (work / SKILLS_DIR / "abs-named" / "SKILL.md").is_file()


def test_a_skill_needing_a_tool_no_subagent_gets_is_withheld_and_said(tmp_path: Path) -> None:
    """The third surface that renders skills into a prompt, held to the same
    gate as the other two. The DAG guide declares `requires.tools:
    [run_subagent_dag]` and `WITHHELD_FROM_SUBAGENT` withholds that tool from
    every sub-agent whatever backend runs it, so this is decidable here without
    the reader's tool list -- and all three deliveries have to close: the menu
    entry, the quoted body, and the file copied into the workdir."""
    home, work = tmp_path / "home", tmp_path / "project"
    work.mkdir()
    guide = _on_disk(home, "subagent-dag-orchestration", "subagent-dag-orchestration", "How to orchestrate.\n")
    guide.requires = {"tools": ["run_subagent_dag"]}
    ordinary = _on_disk(home, "tidy", "tidy")
    catalog = _Catalog(guide, ordinary)

    text, problems = skills_section(["subagent-dag-orchestration", "tidy"], catalog, workdir=work)

    assert "subagent-dag-orchestration" not in text
    assert "tidy" in text
    assert not (work / SKILLS_DIR / "subagent-dag-orchestration").exists()
    assert any("run_subagent_dag" in note and "subagent-dag-orchestration" in note for note in problems), problems

    quoted, quoted_problems = skills_section(["subagent-dag-orchestration", "tidy"], catalog, quote_bodies=True)

    assert "How to orchestrate." not in quoted
    assert any("run_subagent_dag" in note for note in quoted_problems)
