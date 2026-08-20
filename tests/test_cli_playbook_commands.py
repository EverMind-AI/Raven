"""CLI tests for ``raven playbook``.

Each subcommand is covered against a temp two-layer library: a tmp user layer
via ``playbooks.dir`` and a tmp builtin layer via the module constant. The
generator and the run machinery are faked at the seams the commands import
through (``raven.playbook`` attributes, ``make_provider``), so no test needs
a provider or spawns anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path
from raven.playbook import ExecutionPlan, GeneratedPlaybook, NodeSpec, ParamSpec, PlaybookSpec, Triggers

runner = CliRunner()


def _write_md(root: Path, name: str, description: str) -> None:
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


@pytest.fixture
def library(tmp_path: Path, monkeypatch):
    """A config file wired to tmp layers; returns the paths for seeding."""
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    user_root.mkdir()
    builtin_root.mkdir()
    monkeypatch.setattr("raven.playbook.store.BUILTIN_ROOT", builtin_root)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"playbooks": {"dir": str(user_root)}}), encoding="utf-8")
    set_config_path(cfg)
    yield {"user": user_root, "builtin": builtin_root, "config": cfg}
    set_config_path(None)  # type: ignore[arg-type]


def _disabled_in(cfg: Path) -> list[str]:
    return json.loads(cfg.read_text(encoding="utf-8")).get("playbooks", {}).get("disabled", [])


def test_playbook_help_lists_all_subcommands():
    r = runner.invoke(app, ["playbook", "--help"])
    assert r.exit_code == 0
    for sub in ("list", "get", "validate", "create", "enable", "disable", "run", "delete"):
        assert sub in r.stdout, f"missing subcommand in --help: {sub}"


def test_list_shows_both_layers_with_origin_and_state(library):
    _write_md(library["builtin"], "briefing", "the shipped one")
    _write_md(library["user"], "mine", "the user one")
    library["config"].write_text(
        json.dumps({"playbooks": {"dir": str(library["user"]), "disabled": ["mine"]}}), encoding="utf-8"
    )

    r = runner.invoke(app, ["playbook", "list"])
    assert r.exit_code == 0
    assert "briefing" in r.stdout and "builtin" in r.stdout
    assert "mine" in r.stdout and "user" in r.stdout
    assert "disabled" in r.stdout


def test_get_prints_the_raw_file(library):
    _write_md(library["user"], "mine", "the user one")
    r = runner.invoke(app, ["playbook", "get", "mine"])
    assert r.exit_code == 0
    assert "```yaml playbook-spec" in r.stdout
    assert "description: the user one" in r.stdout


def test_get_unknown_name_fails(library):
    r = runner.invoke(app, ["playbook", "get", "ghost"])
    assert r.exit_code == 1


def test_validate_accepts_a_good_file_by_path_and_by_name(library):
    _write_md(library["user"], "mine", "fine")
    by_name = runner.invoke(app, ["playbook", "validate", "mine"])
    assert by_name.exit_code == 0, by_name.stdout
    by_path = runner.invoke(app, ["playbook", "validate", str(library["user"] / "mine" / "playbook.md")])
    assert by_path.exit_code == 0, by_path.stdout
    assert "OK" in by_path.stdout


def test_validate_reports_errors_with_the_file_position(library):
    target = library["user"] / "broken"
    target.mkdir()
    (target / "playbook.md").write_text(
        "---\nname: broken\ndescription: bad mode\n---\n\nbody\n\n"
        "```yaml playbook-spec\n"
        "version: 1\nmode: nonsense\nconfirm: true\n"
        "triggers:\n  keywords: [broken]\n"
        "prompts: whatever\n"
        "```\n",
        encoding="utf-8",
    )
    r = runner.invoke(app, ["playbook", "validate", "broken"])
    assert r.exit_code == 1
    # The full path, not its suffix: a soft-wrap regression folds the line at
    # a position that depends on the tmp path's length, so a suffix check
    # only catches it when the fold happens to land inside those characters.
    assert str(target / "playbook.md") in r.stdout
    assert "mode" in r.stdout


class _FakeGenerator:
    def __init__(self, *args, **kwargs):
        pass

    async def generate(self, user_input: str, skills=None) -> GeneratedPlaybook:
        spec = PlaybookSpec(
            name="placeholder",
            description="generated from: " + user_input.splitlines()[0][:40],
            mode="dag",
            triggers=Triggers(keywords=["weekly scan"]),
            nodes=[NodeSpec(id="scan", agent="research-raven", prompt_template="scan ${params.target}")],
            params={"target": ParamSpec(required=True, description="what to scan?")},
        )
        return GeneratedPlaybook(spec=spec, notes=["Assumption: weekly cadence", "Missing capability: mcp[fs]"])


def test_create_lands_in_the_user_layer_disabled(library, monkeypatch):
    monkeypatch.setattr("raven.cli._helpers.make_provider", lambda config: object())
    monkeypatch.setattr("raven.playbook.PlaybookGenerator", _FakeGenerator)

    r = runner.invoke(app, ["playbook", "create", "weekly-scan", "--input", "scan competitors weekly"])
    assert r.exit_code == 0, r.stdout
    assert (library["user"] / "weekly-scan" / "playbook.md").exists()
    assert _disabled_in(library["config"]) == ["weekly-scan"]
    text = (library["user"] / "weekly-scan" / "playbook.md").read_text(encoding="utf-8")
    assert "name: weekly-scan" in text
    assert "Assumption: weekly cadence" in text
    assert "enable weekly-scan" in r.stdout
    # The bracketed capability name is the diagnostic: Rich must not eat it.
    assert "mcp[fs]" in r.output


def test_create_refuses_an_existing_name(library, monkeypatch):
    monkeypatch.setattr("raven.cli._helpers.make_provider", lambda config: object())
    monkeypatch.setattr("raven.playbook.PlaybookGenerator", _FakeGenerator)
    _write_md(library["builtin"], "weekly-scan", "already shipped")

    r = runner.invoke(app, ["playbook", "create", "weekly-scan", "--input", "whatever"])
    assert r.exit_code == 1


def test_enable_and_disable_edit_the_config_list(library):
    _write_md(library["builtin"], "briefing", "the shipped one")

    r = runner.invoke(app, ["playbook", "disable", "briefing"])
    assert r.exit_code == 0
    assert _disabled_in(library["config"]) == ["briefing"]

    r = runner.invoke(app, ["playbook", "enable", "briefing"])
    assert r.exit_code == 0
    assert _disabled_in(library["config"]) == []


def test_enable_unknown_name_fails(library):
    r = runner.invoke(app, ["playbook", "enable", "ghost"])
    assert r.exit_code == 1


class _FakeRuntime:
    """Captures construction and answers ``load`` without any machinery."""

    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).last = self

    async def load(self, name, params, fills=None, *, allow_disabled=False) -> ExecutionPlan | None:
        self.ran = (name, params, fills, allow_disabled)
        return ExecutionPlan(kind="dag", reply="graph finished: all nodes ok")


class _FakeProvider:
    def get_default_model(self) -> str:
        return "fake-model"


def test_run_executes_by_name_with_kv_params(library, monkeypatch):
    _write_md(library["user"], "mine", "the user one")
    monkeypatch.setattr("raven.cli._helpers.make_provider", lambda config: _FakeProvider())
    monkeypatch.setattr("raven.playbook.PlaybookRuntime", _FakeRuntime)

    r = runner.invoke(app, ["playbook", "run", "mine", "target=acme"])
    assert r.exit_code == 0, r.stdout
    assert "graph finished" in r.stdout
    # `allow_disabled`: this entry is the user's own hand, and disabling only
    # takes a playbook out of what the model is offered.
    assert _FakeRuntime.last.ran == ("mine", {"target": "acme"}, {}, True)
    # The explicit entry runs synchronously: the executor is built foreground.
    assert _FakeRuntime.last.kwargs["executor"]._background is False
    # And it composes a prompt-mode graph itself -- there is no model in the room
    # to hand the guidance to, so without this the CLI could not run one at all.
    assert _FakeRuntime.last.kwargs["executor"]._compose_prompt_mode is True


def test_run_takes_fills_for_a_field_the_playbook_left_blank(library, monkeypatch):
    """The CLI's answer to a playbook that expects values at run time.

    Without ``--fill`` such a playbook simply cannot run here: nobody is present
    to write the missing prompt, and dispatching a blank step would hand a
    sub-agent nothing to do.
    """
    _write_md(library["user"], "mine", "the user one")
    monkeypatch.setattr("raven.cli._helpers.make_provider", lambda config: _FakeProvider())
    monkeypatch.setattr("raven.playbook.PlaybookRuntime", _FakeRuntime)

    r = runner.invoke(
        app,
        ["playbook", "run", "mine", "--fill", "draft.promptTemplate=write it up"],
    )
    assert r.exit_code == 0, r.stdout
    assert _FakeRuntime.last.ran[2] == {"draft": {"promptTemplate": "write it up"}}


def test_run_rejects_a_malformed_fill(library):
    _write_md(library["user"], "mine", "the user one")
    r = runner.invoke(app, ["playbook", "run", "mine", "--fill", "no-dot-here=x"])
    assert r.exit_code == 1


def test_run_rejects_malformed_params(library):
    _write_md(library["user"], "mine", "the user one")
    r = runner.invoke(app, ["playbook", "run", "mine", "not-a-pair"])
    assert r.exit_code == 1


def test_delete_refuses_builtin_and_points_at_disable(library):
    _write_md(library["builtin"], "briefing", "the shipped one")
    r = runner.invoke(app, ["playbook", "delete", "briefing", "--yes"])
    assert r.exit_code == 1
    assert (library["builtin"] / "briefing" / "playbook.md").exists()


def test_delete_removes_user_playbook_and_its_switch_entry(library):
    _write_md(library["user"], "mine", "the user one")
    runner.invoke(app, ["playbook", "disable", "mine"])
    assert _disabled_in(library["config"]) == ["mine"]

    r = runner.invoke(app, ["playbook", "delete", "mine", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert not (library["user"] / "mine").exists()
    assert _disabled_in(library["config"]) == []


def test_delete_unshadows_the_builtin(library):
    _write_md(library["builtin"], "briefing", "the shipped one")
    _write_md(library["user"], "briefing", "my override")

    r = runner.invoke(app, ["playbook", "delete", "briefing", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert "builtin of the same name" in r.stdout
    assert not (library["user"] / "briefing").exists()
    assert (library["builtin"] / "briefing" / "playbook.md").exists()


def test_create_refuses_a_non_kebab_name(library, monkeypatch):
    monkeypatch.setattr("raven.cli._helpers.make_provider", lambda config: object())
    monkeypatch.setattr("raven.playbook.PlaybookGenerator", _FakeGenerator)
    r = runner.invoke(app, ["playbook", "create", "../escape", "--input", "whatever"])
    assert r.exit_code == 1
    assert not (library["user"].parent / "escape").exists()
    # The refusal must show the actual pattern: NAME_RE contains square
    # brackets, which Rich reads as markup and swallows unless escaped --
    # live regression saw the message render as "kebab-case (^*$)".
    assert "[a-z0-9]" in r.output


def test_list_keeps_brackets_in_hand_placed_names_and_diagnostics(library):
    """Names on the read side are whatever directory holds a playbook.md --
    not NAME_RE-filtered -- and a broken row's cell carries a pydantic
    message whose brackets are the diagnostic. Rich table cells parse
    markup, so both must be escaped or they render silently truncated.

    The bracket content must open with [a-z#/@] or Rich's tag regex never
    fires: "weird[fs]" pins the escape, "weird[1]" would pin nothing."""
    target = library["user"] / "weird[fs]"
    target.mkdir()
    (target / "playbook.md").write_text(
        '---\nname: "weird[fs]"\ndescription: hand placed\n---\n\nbody\n\n'
        "```yaml playbook-spec\nversion: 1\nmode: prompt\nconfirm: true\n"
        "triggers:\n  keywords: [weird]\nprompts: x\n```\n",
        encoding="utf-8",
    )
    # A wide console keeps every cell on one physical line, so the counts
    # below cannot be split by wrapping.
    # A readable playbook whose description carries brackets: the loadable
    # path escapes the description cell, and nothing else pins that escape.
    _write_md(library["user"], "bracket-desc", "mounts [fs] volumes safely")
    r = runner.invoke(app, ["playbook", "list"], env={"COLUMNS": "300"})
    assert r.exit_code == 0
    assert "[fs] volumes" in r.output
    # The diagnostic cell: unescaped, the pattern renders as '^*$'.
    assert "[a-z0-9]" in r.output
    # The name cell: the diagnostic also carries the name (input_value=...),
    # so presence alone cannot pin add_row's escape -- the count can. Escaped
    # it appears twice (name cell + diagnostic); with add_row unescaped, once.
    assert r.output.count("weird[fs]") == 2
