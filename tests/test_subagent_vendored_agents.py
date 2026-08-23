"""Discovering the ``subagents/`` tree as agent rows (raven/agent/subagent/vendored_agents.py).

The four vendored raven builds are registered by scanning, not by a written list,
so what needs guarding is the scan's edges: where it looks, what it does with a
folder that is not ready, and that removing a folder removes its row. The last
one is the whole point of scanning rather than storing -- a stored row would
outlive the folder it launches.

Every test builds its own tree under ``tmp_path``. The suite's autouse
``no_vendored_subagents`` fixture points discovery at nothing, so these opt back
in by patching the root, which is also what keeps them independent of whether the
developer running them has built the real folders' venvs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.agent.subagent import vendored_agents as va
from raven.agent.subagent.registry import AgentRegistry

_REAL_SUBAGENTS_ROOT = va.subagents_root
"""Captured at import, before the autouse pin replaces the module attribute.

``TestWhereTheTreeIs`` is the one place that tests the real lookup rather than
standing on it, and the fixture that keeps the rest of the suite deterministic
would otherwise leave it asserting against its own stub -- a test that passes
whatever the function does."""

_MANIFEST = {
    "name": "Raven-Probe",
    "kind": "cli",
    "description": "a vendored build",
    "enabled": True,
    "command": "{PYTHON} {SUBAGENT_DIR}/run.py --prompt-file {prompt_file} --session {agent_id}",
    "resumeCommand": "{PYTHON} {SUBAGENT_DIR}/run.py --prompt-file {prompt_file} --session {agent_id}",
    "idSource": "provisioned",
    "readsLocalFiles": True,
    "transcriptFormat": "text",
}


def _folder(root: Path, name: str, *, manifest: dict | None = None, venv: bool = False) -> Path:
    """One folder shaped the way the real ones are: manifest, installer, checkout."""
    folder = root / name
    (folder / "Build").mkdir(parents=True)
    (folder / "subagent.json").write_text(json.dumps(manifest or _MANIFEST), encoding="utf-8")
    (folder / "config.json").write_text("{}", encoding="utf-8")
    (folder / "install.py").write_text("", encoding="utf-8")
    (folder / "Build" / "pyproject.toml").write_text("", encoding="utf-8")
    if venv:
        launcher = folder / "Build" / ".venv" / "bin" / "raven"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)
    return folder


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr(va, "subagents_root", lambda: root)
    # A lendable host key by default: the launchers inherit the host's provider
    # block, so this is the ordinary machine, not a special case.
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: True)
    return root


def test_a_folder_becomes_a_cli_row_with_its_placeholders_resolved(tree: Path) -> None:
    """``{SUBAGENT_DIR}`` and ``{PYTHON}`` only, and only in the command fields.

    The same pair each folder's ``install.py`` substitutes. A row whose command
    still held a literal ``{SUBAGENT_DIR}`` would be handed to a shell as that
    text and fail in a way that names no cause.
    """
    _folder(tree, "raven-probe", venv=True)

    (row,) = va.discover_vendored_rows()

    assert row.name == "Raven-Probe" and row.kind == "cli"
    assert "{SUBAGENT_DIR}" not in row.command and "{PYTHON}" not in row.command
    assert str(tree / "raven-probe") in row.command
    # The task-time placeholders are *not* resolved here -- the cli backend
    # substitutes those per dispatch.
    assert "{prompt_file}" in row.command and "{agent_id}" in row.command


def test_the_manifests_name_wins_over_the_folders(tree: Path) -> None:
    """So a row written by the folder's own ``install.py`` collides with the
    discovered one and overrides it, instead of the table carrying both."""
    _folder(tree, "raven-probe", venv=True)

    (row,) = va.discover_vendored_rows()

    assert row.name == "Raven-Probe"


def test_an_unbuilt_venv_lists_the_row_disabled_rather_than_dropping_it(tree: Path) -> None:
    """Present-but-not-set-up is what the operations view has to show.

    Disabled keeps it off the roster the dispatching model reads, which is the
    point: a name the model can pick and then fail on is worse than no name.
    Dropping the row instead would also hide the folder from the human who needs
    to know it is there and needs building.
    """
    _folder(tree, "raven-probe", venv=False)

    (row,) = va.discover_vendored_rows()

    assert row.enabled is False


def test_no_credential_anywhere_disables_the_row(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A built venv is not enough when nothing can pay for the model."""
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)
    _folder(tree, "raven-probe", venv=True)

    (row,) = va.discover_vendored_rows()

    assert row.enabled is False


def test_the_folders_own_key_is_enough_without_a_host_key(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)
    folder = _folder(tree, "raven-probe", venv=True)
    (folder / ".env").write_text("PROBE_API_KEY=sk-real\n", encoding="utf-8")

    (row,) = va.discover_vendored_rows()

    assert row.enabled is True


def test_a_scaffolded_env_with_no_value_does_not_count_as_a_key(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``write_key`` copies ``.env.example`` before a key is supplied, so a file
    of bare ``NAME=`` lines is an unconfigured folder, not a configured one."""
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)
    folder = _folder(tree, "raven-probe", venv=True)
    (folder / ".env").write_text("PROBE_API_KEY=\nOTHER=keep\n", encoding="utf-8")

    (row,) = va.discover_vendored_rows()

    assert row.enabled is False


def test_removing_a_folder_removes_its_row(tree: Path) -> None:
    """The reason this is a scan and not a stored list.

    A registration written into config would outlive the folder that launches it,
    and the row would stay on the table pointing at a path that no longer exists.
    """
    import shutil

    _folder(tree, "raven-one", manifest={**_MANIFEST, "name": "One"}, venv=True)
    _folder(tree, "raven-two", manifest={**_MANIFEST, "name": "Two"}, venv=True)
    assert {r.name for r in va.discover_vendored_rows()} == {"One", "Two"}

    shutil.rmtree(tree / "raven-two")

    assert {r.name for r in va.discover_vendored_rows()} == {"One"}


def test_a_folder_without_an_installer_is_not_a_subagent_folder(tree: Path) -> None:
    """Same judgement ``discover`` makes: both files or neither. A directory that
    merely happens to hold a ``subagent.json`` is not an installed agent."""
    folder = _folder(tree, "raven-probe", venv=True)
    (folder / "install.py").unlink()

    assert va.discover_vendored_rows() == []


def test_one_unreadable_manifest_does_not_take_the_others_down(tree: Path) -> None:
    _folder(tree, "raven-good", manifest={**_MANIFEST, "name": "Good"}, venv=True)
    bad = _folder(tree, "raven-bad", venv=True)
    (bad / "subagent.json").write_text("{ not json", encoding="utf-8")

    assert [r.name for r in va.discover_vendored_rows()] == ["Good"]


def test_no_tree_is_no_rows_rather_than_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wheel case, which is most installs: the table is exactly what it was
    before discovery existed."""
    monkeypatch.setattr(va, "subagents_root", lambda: None)

    assert va.discover_vendored_rows() == []


class TestWhereTheTreeIs:
    """``subagents_root`` prefers the writable copy, and why."""

    def test_the_raven_home_copy_wins_over_the_packaged_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A venv lives inside its folder's checkout, so a tree under
        site-packages loses every venv when the wheel is replaced. Preferring the
        home copy is what keeps the four agents in the roster across an upgrade.
        """
        home = tmp_path / "home"
        (home / "subagents").mkdir(parents=True)
        package = tmp_path / "site-packages" / "raven"
        (package / "subagents").mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.setenv("RAVEN_HOME", str(home))
        monkeypatch.setattr("raven.__file__", str(package / "__init__.py"))

        assert _REAL_SUBAGENTS_ROOT() == home / "subagents"

    def test_the_checkout_tree_is_found_beside_the_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        (clone / "subagents").mkdir(parents=True)
        (clone / "raven").mkdir()
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "empty-home"))
        monkeypatch.setattr("raven.__file__", str(clone / "raven" / "__init__.py"))

        assert _REAL_SUBAGENTS_ROOT() == clone / "subagents"

    def test_a_wheel_with_the_tree_gets_it_installed_out_to_the_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The packaged copy is a source, not the place it is used from.

        Looking it up is what triggers the copy, so the answer is the home path
        even on the very first call -- which is the point: whatever venvs get
        built next are built somewhere an upgrade does not delete.
        """
        package = tmp_path / "site-packages" / "raven"
        _folder(package / "subagents", "raven-probe")
        home = tmp_path / "home"
        monkeypatch.setenv("RAVEN_HOME", str(home))
        monkeypatch.setattr("raven.__file__", str(package / "__init__.py"))

        assert _REAL_SUBAGENTS_ROOT() == home / "subagents"
        assert (home / "subagents" / "raven-probe" / "subagent.json").is_file()

    def test_an_uncopyable_packaged_tree_is_still_used_where_it_lies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-only home degrades to the packaged tree rather than to nothing.

        The agents then work until the next upgrade wipes their venvs, which is
        worse than the copied-out arrangement and better than no agents at all.
        """
        import shutil as real_shutil

        package = tmp_path / "site-packages" / "raven"
        _folder(package / "subagents", "raven-probe")
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "unwritable"))
        monkeypatch.setattr("raven.__file__", str(package / "__init__.py"))
        monkeypatch.setattr(real_shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

        assert _REAL_SUBAGENTS_ROOT() == package / "subagents"


class TestOnTheTable:
    """What the registry does with the discovered rows."""

    def test_a_ready_folder_reaches_the_roster_and_the_enum_as_cli(self, tree: Path) -> None:
        """The end the whole feature exists for: a dag node can name it.

        Both views are asserted because they are built from different methods and
        one has been wrong while the other was right -- ``names`` filters to
        enabled rows, ``roster_text`` renders descriptions and capability tags.
        """
        _folder(tree, "raven-probe", venv=True)
        registry = AgentRegistry()

        registry.apply([])

        assert "Raven-Probe" in registry.names()
        assert "Raven-Probe" in registry.roster_text()
        # cli carries no live event stream, and the roster says so rather than
        # leaving the model to assume one.
        assert "no-progress" in registry.roster_text()
        assert registry.get("Raven-Probe").kind == "cli"

    def test_an_unready_folder_is_on_the_table_but_not_in_the_enum(self, tree: Path) -> None:
        _folder(tree, "raven-probe", venv=False)
        registry = AgentRegistry()

        registry.apply([])

        assert "Raven-Probe" in [row.name for row in registry.rows()]
        assert "Raven-Probe" not in registry.names()

    def test_the_builtin_row_survives_discovery(self, tree: Path) -> None:
        """Discovery adds; it does not replace. The in-process agent is what a
        dag node uses to run raven itself, and what stored direct-chat records
        resolve against."""
        _folder(tree, "raven-probe", venv=True)
        registry = AgentRegistry()

        registry.apply([])

        assert "raven" in registry.names()

    def test_a_config_row_of_the_same_name_overrides_the_discovered_one(self, tree: Path) -> None:
        """The user's edit wins, and does not appear as a second row of one name.

        Whole-row rather than field-wise: a stored row for one of these folders
        was written by that folder's own ``install.py`` from the same manifest, so
        it is a complete entry -- taking half of each would produce a command line
        neither file contains.
        """
        from raven.config.schema import ThirdPartyCliSubagentConfig

        _folder(tree, "raven-probe", venv=True)
        mine = ThirdPartyCliSubagentConfig(name="Raven-Probe", command="my-own {prompt}", description="mine")
        registry = AgentRegistry()

        registry.apply([mine])

        rows = [row for row in registry.rows() if row.name == "Raven-Probe"]
        assert len(rows) == 1
        assert rows[0].config.command == "my-own {prompt}"

    def test_discovery_order_holds_when_a_row_is_overridden(self, tree: Path) -> None:
        """So the roster does not reshuffle because one folder got configured."""
        from raven.config.schema import ThirdPartyCliSubagentConfig

        for name in ("raven-a", "raven-b", "raven-c"):
            _folder(tree, name, manifest={**_MANIFEST, "name": name.upper()}, venv=True)
        registry = AgentRegistry()

        registry.apply([ThirdPartyCliSubagentConfig(name="RAVEN-B", command="x {prompt}")])

        assert [row.name for row in registry.rows()] == ["raven", "RAVEN-A", "RAVEN-B", "RAVEN-C"]


def test_the_lending_test_reads_the_file_the_launcher_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One credential, one answer.

    The launchers are standard-library-only scripts that accept exactly one shape:
    a literal ``apiKey`` on some provider section of the host's ``config.json``.
    Asking ``providers.auth`` instead would call an OAuth-signed-in host lendable
    and advertise an agent that dies at its first dispatch.
    """
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))

    assert va.host_can_lend_a_key() is False

    (tmp_path / "config.json").write_text(json.dumps({"providers": {"openrouter": {"apiKey": ""}}}), encoding="utf-8")
    assert va.host_can_lend_a_key() is False, "an empty key is not a key"

    (tmp_path / "config.json").write_text(json.dumps({"providers": {"custom": {"apiKey": "sk-x"}}}), encoding="utf-8")
    assert va.host_can_lend_a_key() is True, "any provider section, not openrouter specifically"


def test_the_interpreter_is_this_one_unless_the_environment_names_another() -> None:
    """``install.py``'s own default order, minus its ``--python`` flag. Any
    python3 satisfies it: each launcher is standard-library only."""
    import sys

    assert va._resolved_python() == sys.executable

    os.environ["SUBAGENT_PYTHON"] = "/opt/python3"
    try:
        assert va._resolved_python() == "/opt/python3"
    finally:
        del os.environ["SUBAGENT_PYTHON"]


class TestInstallingTheWheelsOwnTree:
    """``_install_packaged_tree`` -- why a wheel's tree is copied out at all.

    Each folder's venv is built inside its own checkout, and a wheel install is
    replaced wholesale on upgrade. Left under site-packages the venvs die every
    time and all four agents fall out of the roster until something spends
    minutes rebuilding them. These pin the three states that follow from copying
    it out under a version stamp.
    """

    def _packaged(self, tmp_path: Path) -> Path:
        packaged = tmp_path / "site-packages" / "raven" / "subagents"
        packaged.mkdir(parents=True)
        for name in ("raven-one", "raven-two"):
            _folder(packaged, name, manifest={**_MANIFEST, "name": name})
        # The tree's own files, which live beside the folders rather than inside
        # any of them -- the shape the real tree has.
        (packaged / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (packaged / "install.sh").chmod(0o755)
        (packaged / "README.md").write_text("the tree\n", encoding="utf-8")
        return packaged

    def test_the_trees_own_files_come_with_it(self, tmp_path: Path) -> None:
        """``install.sh`` above all, and it is not inside any folder.

        It is what builds a folder's venv and it knows each folder's optional
        dependency extra, so a tree copied without it lists every agent and can
        build none. Measured on a real install: the page offered Install, the call
        answered "no install.sh", and that reached the reader as a bare "subagent
        not found" -- see the reason's own journey in ``subagents_build``.
        """
        installed = tmp_path / "home" / "subagents"

        va._install_packaged_tree(self._packaged(tmp_path), installed)

        assert (installed / "install.sh").is_file()
        assert os.access(installed / "install.sh", os.X_OK), "copied without its mode, so nothing can run it"
        assert (installed / "README.md").is_file()

    def test_every_folder_can_name_its_installer_afterwards(self, tmp_path: Path) -> None:
        """The end the copy exists for, asserted through the reader the page uses
        rather than on the file: `installer_for` returning None is the exact state
        that produced the unexplained failure."""
        installed = tmp_path / "home" / "subagents"
        va._install_packaged_tree(self._packaged(tmp_path), installed)

        for folder in sorted(p for p in installed.iterdir() if p.is_dir()):
            assert va.installer_for(folder) == installed / "install.sh", folder.name

    def test_a_fresh_install_lands_every_folder_in_the_raven_home(self, tmp_path: Path) -> None:
        installed = tmp_path / "home" / "subagents"

        va._install_packaged_tree(self._packaged(tmp_path), installed)

        assert {p.name for p in installed.iterdir() if p.is_dir()} == {"raven-one", "raven-two"}

    def test_a_folder_deleted_under_the_same_version_stays_deleted(self, tmp_path: Path) -> None:
        """Otherwise the scan's whole point is undone: a user removes an agent,
        and the next start quietly puts it back."""
        import shutil

        packaged = self._packaged(tmp_path)
        installed = tmp_path / "home" / "subagents"
        va._install_packaged_tree(packaged, installed)

        shutil.rmtree(installed / "raven-two")
        va._install_packaged_tree(packaged, installed)

        assert {p.name for p in installed.iterdir() if p.is_dir()} == {"raven-one"}

    def test_an_upgrade_restores_what_the_release_ships_without_losing_a_venv(self, tmp_path: Path) -> None:
        """The venv is the expensive part, and a new release of the same fork does
        not invalidate it -- so the refresh copies over the folder and leaves it."""
        import shutil

        packaged = self._packaged(tmp_path)
        installed = tmp_path / "home" / "subagents"
        va._install_packaged_tree(packaged, installed)
        launcher = installed / "raven-one" / "Build" / ".venv" / "bin" / "raven"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        shutil.rmtree(installed / "raven-two")

        (installed / ".raven-version").write_text("0.0.0-older", encoding="utf-8")
        va._install_packaged_tree(packaged, installed)

        assert {p.name for p in installed.iterdir() if p.is_dir()} == {"raven-one", "raven-two"}
        assert launcher.is_file(), "the upgrade must not throw away a built venv"

    def test_an_unwritable_home_is_a_warning_rather_than_a_failed_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not having the vendored agents is a smaller problem than not starting."""
        import shutil as real_shutil

        def _explode(*_args, **_kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(real_shutil, "copytree", _explode)

        va._install_packaged_tree(self._packaged(tmp_path), tmp_path / "home" / "subagents")


class TestAStoredRowThatWentStale:
    """A config row wins over a discovered one -- unless it cannot run.

    Each folder's ``install.py`` bakes an absolute path into the row it writes, so
    a row written while the tree sat under site-packages keeps naming that path
    after an upgrade replaced the wheel. Without this, "upgrade" would mean "all
    four agents now fail at dispatch with file-not-found", and the discovered row
    that *does* work would be the one being shadowed.
    """

    def test_a_row_whose_launcher_is_gone_loses_to_the_discovered_one(self, tree: Path) -> None:
        """The command shape the manifests actually emit: two absolute paths.

        Written first with one path, which is the shape no manifest produces --
        and with one path `any` and `all` agree, so the test passed while the
        guard was checking the wrong thing. Every real command names an
        interpreter *and* a launcher, and the interpreter is this raven's own, so
        it exists whatever happened to the tree.
        """
        import sys

        from raven.config.schema import ThirdPartyCliSubagentConfig

        _folder(tree, "raven-probe", venv=True)
        stale = ThirdPartyCliSubagentConfig(
            name="Raven-Probe",
            command=f"{sys.executable} /gone/site-packages/raven/subagents/raven-probe/run.py {{prompt}}",
        )

        merged = va.merge_vendored_seeds([stale], va.discover_vendored_rows())

        assert len(merged) == 1
        assert str(tree) in merged[0].command

    def test_a_row_whose_launcher_is_there_still_wins(self, tree: Path) -> None:
        """The override has to keep working; this guard is narrow on purpose."""
        import sys

        from raven.config.schema import ThirdPartyCliSubagentConfig

        folder = _folder(tree, "raven-probe", venv=True)
        (folder / "run.py").write_text("", encoding="utf-8")
        mine = ThirdPartyCliSubagentConfig(
            name="Raven-Probe", command=f"{sys.executable} {folder / 'run.py'} --mine {{prompt}}"
        )

        merged = va.merge_vendored_seeds([mine], va.discover_vendored_rows())

        assert "--mine" in merged[0].command

    def test_a_command_with_no_absolute_path_is_never_called_stale(self, tree: Path) -> None:
        """A hand-written row invoking something on ``PATH`` has no path to check,
        and guessing that it is broken would delete a working registration."""
        from raven.config.schema import ThirdPartyCliSubagentConfig

        _folder(tree, "raven-probe", venv=True)
        on_path = ThirdPartyCliSubagentConfig(name="Raven-Probe", command="my-agent --run {prompt}")

        merged = va.merge_vendored_seeds([on_path], va.discover_vendored_rows())

        assert merged[0].command == "my-agent --run {prompt}"


class TestTheEdgesThatDegrade:
    """Paths that exist so a broken tree cannot stop raven from starting.

    Each one returns "not there / not ready" instead of raising, and each is
    reachable from a real state -- a half-copied folder, an unreadable file, a
    manifest holding something other than an object. Asserted because a
    degradation nobody exercises is a degradation nobody knows the shape of.
    """

    def test_a_folder_with_two_checkouts_is_not_ready(self, tree: Path) -> None:
        """``checkout_of`` wants a lone ``pyproject.toml`` and says so; two of them
        is an ambiguity, and guessing which venv to check would be a coin flip."""
        folder = _folder(tree, "raven-probe", venv=True)
        (folder / "Second").mkdir()
        (folder / "Second" / "pyproject.toml").write_text("", encoding="utf-8")

        assert va.checkout_of(folder) is None
        (row,) = va.discover_vendored_rows()
        assert row.enabled is False

    def test_no_tree_anywhere_reads_as_no_tree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        clone = tmp_path / "clone"
        (clone / "raven").mkdir(parents=True)
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "no-home"))
        monkeypatch.setattr("raven.__file__", str(clone / "raven" / "__init__.py"))

        assert _REAL_SUBAGENTS_ROOT() is None

    def test_a_manifest_holding_a_list_is_skipped_not_crashed(self, tree: Path) -> None:
        folder = _folder(tree, "raven-probe", venv=True)
        (folder / "subagent.json").write_text("[]", encoding="utf-8")

        assert va.discover_vendored_rows() == []

    def test_an_unreadable_env_file_counts_as_no_key(self, tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A directory named ``.env`` is the reachable version of this: the read
        raises, and a raise here would take the whole table with it."""
        monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)
        folder = _folder(tree, "raven-probe", venv=True)
        (folder / ".env").mkdir()

        assert va.api_key_present(folder, "PROBE_API_KEY") is False
        assert va.discover_vendored_rows()[0].enabled is False

    def test_the_process_environment_supplies_a_key_too(self, tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same order the launcher reads in: environment first, then ``.env``."""
        monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)
        monkeypatch.setenv("PROBE_API_KEY", "sk-from-the-env")
        _folder(tree, "raven-probe", venv=True)

        assert va.discover_vendored_rows()[0].enabled is True

    def test_an_unparseable_host_config_is_nothing_to_lend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text("{ truncated", encoding="utf-8")

        assert va.host_can_lend_a_key() is False

    def test_a_host_config_that_is_not_an_object_is_nothing_to_lend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text('{"providers": "not-a-mapping"}', encoding="utf-8")

        assert va.host_can_lend_a_key() is False

    def test_a_nameless_row_on_either_side_is_dropped(self, tree: Path) -> None:
        """Both merge loops skip a row with no name, because a row that cannot be
        addressed cannot be dispatched to or overridden."""

        class _Nameless:
            name = ""
            command = "x"

        merged = va.merge_vendored_seeds([_Nameless()], [_Nameless()])

        assert merged == []

    def test_a_packaged_folder_without_a_manifest_is_not_copied_out(self, tmp_path: Path) -> None:
        """The install-out step uses the same "is this a subagent folder" test the
        scan does, so a stray directory in the wheel does not land in the home."""
        packaged = tmp_path / "packaged"
        _folder(packaged, "raven-real")
        (packaged / "docs").mkdir()
        installed = tmp_path / "home" / "subagents"

        va._install_packaged_tree(packaged, installed)

        assert {p.name for p in installed.iterdir() if p.is_dir()} == {"raven-real"}

    @pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not block root")
    def test_an_unreadable_env_file_is_not_a_crash(self, tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mode 000 is reachable: ``write_key`` chmods these to 600, and a file
        copied between accounts or restored from a backup can land unreadable.
        A raise here would take the whole agent table down with one folder.

        Skipped as root, which the CI image runs as: the mode is advisory there,
        the read succeeds and the key is found. Kept for the shape it documents,
        not for the coverage -- the two cases below hold the guard at every uid,
        because arranging for the read to be *denied* is what needs a non-root
        uid, while forcing it to *fail* does not."""
        monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)
        folder = _folder(tree, "raven-probe", venv=True)
        env = folder / ".env"
        env.write_text("PROBE_API_KEY=sk-real\n", encoding="utf-8")
        env.chmod(0o000)
        try:
            assert va.api_key_present(folder, "PROBE_API_KEY") is False
        finally:
            env.chmod(0o600)

    def test_an_env_holding_a_non_utf8_byte_disables_rather_than_raising(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``UnicodeDecodeError`` is a ``ValueError``, so an ``except OSError``
        here does not catch it.

        The whole chain is unguarded above this: ``credential_ready`` runs after
        ``_scan``'s per-folder ``except`` has closed, ``AgentRegistry.apply`` is
        called unguarded from ``SubagentManager.__init__``, and that from
        ``AgentLoop.__init__`` -- so one latin-1 byte in one folder's ``.env``
        stopped the loop from constructing. Asserted end to end, not just on the
        helper, because the helper returning False is not what was at risk.
        """
        monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)
        folder = _folder(tree, "raven-probe", venv=True)
        (folder / ".env").write_bytes(b"PROBE_API_KEY=sk-caf\xe9\n")

        assert va.api_key_present(folder, "PROBE_API_KEY") is False
        assert va.discover_vendored_rows()[0].enabled is False

    def test_a_read_that_fails_outright_disables_rather_than_raising(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``OSError`` half of the same guard, at any uid.

        The mode-000 case above is the real-world shape of this, but it can only
        assert it where the mode is enforced. Forcing the read to raise covers the
        branch as root too, which is where CI runs.
        """
        monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)
        folder = _folder(tree, "raven-probe", venv=True)
        (folder / ".env").write_text("PROBE_API_KEY=sk-real\n", encoding="utf-8")

        def _denied(*_args: object, **_kwargs: object) -> str:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "read_text", _denied)

        assert va.api_key_present(folder, "PROBE_API_KEY") is False

    def test_the_verdict_says_which_reasons_the_installer_can_fix(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the unbuilt venv. The page gates its Install button on this, so a
        reason it cannot fix must not read as one it can -- a credential is not
        something ``install.sh`` can mint, and an ambiguous checkout defeats the
        installer the same way it defeats us."""
        monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)
        _folder(tree, "raven-unbuilt", manifest={**_MANIFEST, "name": "Unbuilt"}, venv=False)
        keyless = _folder(tree, "raven-keyless", manifest={**_MANIFEST, "name": "Keyless"}, venv=True)
        muddled = _folder(tree, "raven-muddled", manifest={**_MANIFEST, "name": "Muddled"}, venv=True)
        (muddled / "Second").mkdir()
        (muddled / "Second" / "pyproject.toml").write_text("", encoding="utf-8")
        (keyless / ".env").write_text("PROBE_API_KEY=\n", encoding="utf-8")

        state = va.vendored_state()

        assert {name: v.kind for name, v in state.items()} == {
            "Unbuilt": "venv",
            "Keyless": "credential",
            "Muddled": "checkout",
        }
        assert {name: v.buildable for name, v in state.items()} == {
            "Unbuilt": True,
            "Keyless": False,
            "Muddled": False,
        }
        assert not any(v.ready for v in state.values())

    def test_a_ready_folder_reports_ready_with_no_reason(self, tree: Path) -> None:
        _folder(tree, "raven-probe", venv=True)

        (verdict,) = va.vendored_state().values()

        assert verdict.ready is True and verdict.kind == "" and verdict.detail == ""

    def test_a_config_row_naming_no_folder_is_simply_appended(self, tree: Path) -> None:
        """The ordinary case for every agent a user registers themselves: it is
        not a vendored folder, so it joins the list rather than overriding one."""
        from raven.config.schema import ThirdPartyCliSubagentConfig

        _folder(tree, "raven-probe", venv=True)
        mine = ThirdPartyCliSubagentConfig(name="my-codex", command="codex {prompt}")

        merged = va.merge_vendored_seeds([mine], va.discover_vendored_rows())

        assert [getattr(r, "name") for r in merged] == ["Raven-Probe", "my-codex"]


def test_a_manifest_that_declares_itself_off_stays_off(tree: Path) -> None:
    """`"enabled": false` is a field the manifest has; ignoring it accepted a
    declaration and did the opposite. Both conditions hold: a folder that says it
    is off stays off, and one that cannot start is off whatever it says."""
    _folder(tree, "raven-probe", manifest={**_MANIFEST, "enabled": False}, venv=True)

    (row,) = va.discover_vendored_rows()

    assert row.enabled is False


def test_a_manifest_can_declare_what_its_agent_owns(tree: Path) -> None:
    from raven.agent.subagent.vendored_agents import discover_vendored_rows

    _folder(tree, "scribe", manifest={**_MANIFEST, "name": "Scribe", "owns": "owns decks."}, venv=True)
    row = next(r for r in discover_vendored_rows() if r.name == "Scribe")
    assert row.owns == "owns decks."


def test_a_stored_row_written_before_owns_existed_still_gets_it(tree: Path) -> None:
    """The trap this closes: config rows win over discovered ones whole, so an
    install whose config predates the field would never see it and the feature
    would be silently inert until every agent was reinstalled. ``owns`` is a
    manifest fact about what the agent is for, not a user preference, so an
    unset one is filled from the folder."""
    from raven.agent.subagent.vendored_agents import discover_vendored_rows, merge_vendored_seeds

    folder = _folder(tree, "scribe", manifest={**_MANIFEST, "name": "Scribe", "owns": "owns decks."}, venv=True)
    # Without this the stored row's command names a launcher that is not there,
    # which sends the merge down the wholesale-replacement path and would let
    # this test pass without the field-level fill existing.
    (folder / "run.py").write_text("", encoding="utf-8")
    discovered = discover_vendored_rows()
    stored = next(r for r in discovered if r.name == "Scribe").model_copy(
        update={"owns": None, "description": "edited by the user"}
    )

    merged = merge_vendored_seeds([stored], discovered)
    row = next(r for r in merged if r.name == "Scribe")
    assert row.owns == "owns decks."
    # Proves the fill was field-level: a wholesale swap would take the
    # manifest's description back too.
    assert row.description == "edited by the user"


def test_an_explicit_empty_owns_is_the_users_opt_out(tree: Path) -> None:
    """Absent and explicitly-blank are different answers: blank means the user
    took the agent out of the delegation section on purpose."""
    from raven.agent.subagent.vendored_agents import discover_vendored_rows, merge_vendored_seeds

    folder = _folder(tree, "scribe", manifest={**_MANIFEST, "name": "Scribe", "owns": "owns decks."}, venv=True)
    (folder / "run.py").write_text("", encoding="utf-8")
    discovered = discover_vendored_rows()
    stored = next(r for r in discovered if r.name == "Scribe").model_copy(update={"owns": ""})

    merged = merge_vendored_seeds([stored], discovered)
    assert next(r for r in merged if r.name == "Scribe").owns == ""
