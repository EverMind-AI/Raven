"""Discovering the ``agents/`` product tree as agent rows (raven/agent/subagent/vendored_agents.py).

The shipped agent products are registered by scanning, not by a written list,
so what needs guarding is the scan's edges: where it looks, what it does with a
folder that is not ready, and that removing a folder removes its row. The last
one is the whole point of scanning rather than storing -- a stored row would
outlive the folder it launches.

Every test builds its own tree under ``tmp_path``. The suite's autouse
``no_discovered_products`` fixture points discovery at nothing, so these opt
back in by patching the root, which is also what keeps them independent of
which engine wheels the developer running them has installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.agent.subagent import vendored_agents as va
from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.registry import AgentRegistry

_REAL_AGENTS_ROOT = va.agents_root
"""Captured at import, before the autouse pin replaces the module attribute.

``TestWhereTheTreeIs`` is the one place that tests the real lookup rather than
standing on it, and the fixture that keeps the rest of the suite deterministic
would otherwise leave it asserting against its own stub -- a test that passes
whatever the function does."""

_MISSING_ENGINE = "raven_probe_engine_that_is_not_installed"
"""An import name no environment carries, for the engine-absent branch."""

_MANIFEST = {
    "name": "Raven-Probe",
    "kind": "cli",
    "description": "a shipped product",
    "enabled": True,
    "command": "{PYTHON} {SUBAGENT_DIR}/run.py --prompt-file {prompt_file} --session {agent_id}",
    "resumeCommand": "{PYTHON} {SUBAGENT_DIR}/run.py --prompt-file {prompt_file} --session {agent_id}",
    "idSource": "provisioned",
    "readsLocalFiles": True,
    "transcriptFormat": "text",
}


_ACP_MANIFEST = {
    "name": "Raven-Probe-Acp",
    "kind": "acp",
    "description": "a shipped product served over acp",
    "enabled": True,
    "command": "{PYTHON} {SUBAGENT_DIR}/run.py",
    "cwd": "{SUBAGENT_DIR}",
    "readyTimeoutMs": 60000,
    "timeout": 2400,
}


def _product(root: Path, name: str, *, manifest: dict | None = None, launcher: bool = True) -> Path:
    """One folder shaped the way the real ones are: manifest, launcher, profile."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "subagent.json").write_text(json.dumps(manifest or _MANIFEST), encoding="utf-8")
    (folder / "config.json").write_text("{}", encoding="utf-8")
    if launcher:
        (folder / "run.py").write_text("", encoding="utf-8")
    return folder


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(va, "agents_root", lambda: root)
    return root


def test_a_folder_becomes_a_cli_row_with_its_placeholders_resolved(tree: Path) -> None:
    """``{SUBAGENT_DIR}`` and ``{PYTHON}`` only, and only in the command fields.

    The same pair each product's ``install.py`` substitutes. A row whose command
    still held a literal ``{SUBAGENT_DIR}`` would be handed to a shell as that
    text and fail in a way that names no cause.
    """
    _product(tree, "raven-probe")

    (row,) = va.discover_product_rows()

    assert row.name == "Raven-Probe" and row.kind == "cli"
    assert "{SUBAGENT_DIR}" not in row.command and "{PYTHON}" not in row.command
    assert str(tree / "raven-probe") in row.command
    # The task-time placeholders are *not* resolved here -- the cli backend
    # substitutes those per dispatch.
    assert "{prompt_file}" in row.command and "{agent_id}" in row.command
    # resumeCommand materializes with command: the cli backend executes
    # `resume_command or command`, so a template left in only one of them is a
    # resume that hands the shell a literal {PYTHON}.
    assert "{SUBAGENT_DIR}" not in row.resume_command and "{PYTHON}" not in row.resume_command
    assert str(tree / "raven-probe") in row.resume_command
    assert "{prompt_file}" in row.resume_command and "{agent_id}" in row.resume_command


def test_an_acp_manifest_becomes_an_acp_row_with_its_cwd_resolved(tree: Path) -> None:
    """The manifest's own ``kind`` picks the schema. Validating an acp manifest
    as cli would reject it on the ``kind`` literal and the folder would silently
    vanish from the roster.

    ``cwd`` resolves like the command fields, and pinning it is load-bearing: an
    acp entry with no ``cwd`` falls back to the calling task's workspace, which
    is part of the pool's launch key -- every new workspace would relaunch the
    server and kill the sessions the old one was serving.
    """
    _product(tree, "raven-probe", manifest=_ACP_MANIFEST)

    (row,) = va.discover_product_rows()

    assert row.kind == "acp"
    assert row.name == "Raven-Probe-Acp"
    assert "{SUBAGENT_DIR}" not in row.command and "{PYTHON}" not in row.command
    assert row.cwd == str(tree / "raven-probe")
    assert row.ready_timeout_ms == 60000


def test_an_unready_acp_folder_is_listed_disabled_like_a_cli_one(tree: Path) -> None:
    _product(tree, "raven-probe", manifest=_ACP_MANIFEST, launcher=False)

    (row,) = va.discover_product_rows()

    assert row.kind == "acp" and row.enabled is False


def test_the_manifests_name_wins_over_the_folders(tree: Path) -> None:
    """So a row written by the folder's own ``install.py`` collides with the
    discovered one and overrides it, instead of the table carrying both."""
    _product(tree, "raven-probe")

    (row,) = va.discover_product_rows()

    assert row.name == "Raven-Probe"


def test_a_missing_launcher_lists_the_row_disabled_rather_than_dropping_it(tree: Path) -> None:
    """Present-but-not-set-up is what the operations view has to show.

    Disabled keeps it off the roster the dispatching model reads, which is the
    point: a name the model can pick and then fail on is worse than no name.
    Dropping the row instead would also hide the folder from the human who
    needs to know it is there and broken.
    """
    _product(tree, "raven-probe", launcher=False)

    (row,) = va.discover_product_rows()

    assert row.enabled is False


def test_a_missing_engine_wheel_disables_the_row(tree: Path) -> None:
    """A launcher in place is not enough when the engine it serves is absent.

    The launcher's own refusal (``run.py`` probes ``find_spec`` and exits) is
    loud but late -- at dispatch; this keeps the name out of the roster the
    model picks from, while the row stays visible with the wheel named.
    """
    _product(
        tree,
        "raven-probe",
        manifest={**_ACP_MANIFEST, "engine": {"package": _MISSING_ENGINE, "wheel": "probe-engine"}},
    )

    (row,) = va.discover_product_rows()

    assert row.enabled is False
    verdict = va.product_state()["Raven-Probe-Acp"]
    assert verdict.kind == "engine"
    assert "probe-engine" in verdict.detail


def test_an_importable_engine_leaves_the_row_enabled(tree: Path) -> None:
    """The probe is presence where raven runs, nothing more: an importable
    package answers it, and the row is as ready as one with no engine at all."""
    _product(
        tree,
        "raven-probe",
        manifest={**_ACP_MANIFEST, "engine": {"package": "json", "wheel": "probe-engine"}},
    )

    (row,) = va.discover_product_rows()

    assert row.enabled is True


def test_no_credential_check_happens_at_discovery(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyless folder on a keyless host is still advertised.

    Deliberate: the launcher inherits the host's provider block when the folder
    holds no key, and refuses loudly at dispatch when there is truly nothing --
    a discovery-time credential verdict would be a second reader of that fact,
    free to disagree with the one that rules.
    """
    monkeypatch.setattr(va, "host_can_lend_a_key", lambda: False)
    _product(tree, "raven-probe")

    (row,) = va.discover_product_rows()

    assert row.enabled is True


def test_removing_a_folder_removes_its_row(tree: Path) -> None:
    """The reason this is a scan and not a stored list.

    A registration written into config would outlive the folder that launches it,
    and the row would stay on the table pointing at a path that no longer exists.
    """
    import shutil

    _product(tree, "raven-one", manifest={**_MANIFEST, "name": "One"})
    _product(tree, "raven-two", manifest={**_MANIFEST, "name": "Two"})
    assert {r.name for r in va.discover_product_rows()} == {"One", "Two"}

    shutil.rmtree(tree / "raven-two")

    assert {r.name for r in va.discover_product_rows()} == {"One"}


def test_one_unreadable_manifest_does_not_take_the_others_down(tree: Path) -> None:
    _product(tree, "raven-good", manifest={**_MANIFEST, "name": "Good"})
    bad = _product(tree, "raven-bad")
    (bad / "subagent.json").write_text("{ not json", encoding="utf-8")

    assert [r.name for r in va.discover_product_rows()] == ["Good"]


def test_no_tree_is_no_rows_rather_than_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sdist-wheel case: the table is exactly what it was before discovery
    existed."""
    monkeypatch.setattr(va, "agents_root", lambda: None)

    assert va.discover_product_rows() == []


class TestWhereTheTreeIs:
    """``agents_root`` prefers the writable copy, and why."""

    def test_the_raven_home_copy_wins_over_the_packaged_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A product's ``.env`` lives inside its folder, so a tree under
        site-packages loses every key when the wheel is replaced. Preferring the
        home copy is what keeps the keys across an upgrade.
        """
        home = tmp_path / "home"
        (home / "agents").mkdir(parents=True)
        package = tmp_path / "site-packages" / "raven"
        (package / "agents").mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.setenv("RAVEN_HOME", str(home))
        monkeypatch.setattr("raven.__file__", str(package / "__init__.py"))

        assert _REAL_AGENTS_ROOT() == home / "agents"

    def test_the_checkout_tree_is_found_beside_the_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = tmp_path / "clone"
        (clone / "agents").mkdir(parents=True)
        (clone / "raven").mkdir()
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "empty-home"))
        monkeypatch.setattr("raven.__file__", str(clone / "raven" / "__init__.py"))

        assert _REAL_AGENTS_ROOT() == clone / "agents"

    def test_a_wheel_with_the_tree_gets_it_installed_out_to_the_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The packaged copy is a source, not the place it is used from.

        Looking it up is what triggers the copy, so the answer is the home path
        even on the very first call -- which is the point: whatever keys get
        written next are written somewhere an upgrade does not delete.
        """
        package = tmp_path / "site-packages" / "raven"
        _product(package / "agents", "raven-probe")
        home = tmp_path / "home"
        monkeypatch.setenv("RAVEN_HOME", str(home))
        monkeypatch.setattr("raven.__file__", str(package / "__init__.py"))

        assert _REAL_AGENTS_ROOT() == home / "agents"
        assert (home / "agents" / "raven-probe" / "subagent.json").is_file()

    def test_an_uncopyable_packaged_tree_is_still_used_where_it_lies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-only home degrades to the packaged tree rather than to nothing.

        The agents then work until the next upgrade drops their keys, which is
        worse than the copied-out arrangement and better than no agents at all.
        """
        import shutil as real_shutil

        package = tmp_path / "site-packages" / "raven"
        _product(package / "agents", "raven-probe")
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "unwritable"))
        monkeypatch.setattr("raven.__file__", str(package / "__init__.py"))
        monkeypatch.setattr(real_shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

        assert _REAL_AGENTS_ROOT() == package / "agents"


class TestOnTheTable:
    """What the registry does with the discovered rows."""

    def test_a_ready_folder_reaches_the_roster_and_the_enum_as_cli(self, tree: Path) -> None:
        """The end the whole feature exists for: a dag node can name it.

        Both views are asserted because they are built from different methods and
        one has been wrong while the other was right -- ``names`` filters to
        enabled rows, ``roster_text`` renders descriptions and capability tags.
        """
        _product(tree, "raven-probe")
        registry = AgentRegistry()

        registry.apply([])

        assert "Raven-Probe" in registry.names()
        assert "Raven-Probe" in registry.roster_text()
        # cli carries no live event stream, and the roster says so rather than
        # leaving the model to assume one.
        assert "no-progress" in registry.roster_text()
        assert registry.get("Raven-Probe").kind == "cli"

    def test_a_ready_acp_folder_reaches_the_roster_as_acp(self, tree: Path) -> None:
        """An acp row builds an acp backend and the roster tags follow the kind:
        the live event stream is a property of the transport, so the row has to
        reach the table as acp for the model to be told about it."""
        _product(tree, "raven-probe", manifest=_ACP_MANIFEST)
        registry = AgentRegistry()

        registry.apply([])

        assert "Raven-Probe-Acp" in registry.names()
        assert registry.get("Raven-Probe-Acp").kind == "acp"
        assert "live-progress" in registry.roster_text()

    def test_an_unready_folder_is_on_the_table_but_not_in_the_enum(self, tree: Path) -> None:
        _product(tree, "raven-probe", launcher=False)
        registry = AgentRegistry()

        registry.apply([])

        assert "Raven-Probe" in [row.name for row in registry.rows()]
        assert "Raven-Probe" not in registry.names()

    def test_the_builtin_row_survives_discovery(self, tree: Path) -> None:
        """Discovery adds; it does not replace. The in-process agent is what a
        dag node uses to run raven itself, and what stored direct-chat records
        resolve against."""
        _product(tree, "raven-probe")
        registry = AgentRegistry()

        registry.apply([])

        assert GENERIC_AGENT in registry.names()

    def test_a_config_row_of_the_same_name_overrides_the_discovered_one(self, tree: Path) -> None:
        """The user's edit wins, and does not appear as a second row of one name.

        Whole-row rather than field-wise: a stored row for one of these folders
        was written by that folder's own ``install.py`` from the same manifest, so
        it is a complete entry -- taking half of each would produce a command line
        neither file contains.
        """
        from raven.config.schema import ThirdPartyCliSubagentConfig

        _product(tree, "raven-probe")
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
            _product(tree, name, manifest={**_MANIFEST, "name": name.upper()})
        registry = AgentRegistry()

        registry.apply([ThirdPartyCliSubagentConfig(name="RAVEN-B", command="x {prompt}")])

        assert [row.name for row in registry.rows()] == [GENERIC_AGENT, "RAVEN-A", "RAVEN-B", "RAVEN-C"]


def test_the_lending_test_reads_the_file_the_launcher_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One credential, one answer.

    The launchers' ``inherit_llm`` accepts exactly one shape: a literal
    ``apiKey`` on some provider section of the host's ``config.json``. Asking
    ``providers.auth`` instead would call an OAuth-signed-in host lendable and
    advertise an agent that dies at its first dispatch.
    """
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))

    assert va.host_can_lend_a_key() is False

    (tmp_path / "config.json").write_text(json.dumps({"providers": {"openrouter": {"apiKey": ""}}}), encoding="utf-8")
    assert va.host_can_lend_a_key() is False, "an empty key is not a key"

    (tmp_path / "config.json").write_text(json.dumps({"providers": {"custom": {"apiKey": "sk-x"}}}), encoding="utf-8")
    assert va.host_can_lend_a_key() is True, "any provider section, not openrouter specifically"


def test_the_interpreter_is_this_one_unless_the_environment_names_another() -> None:
    """``install.py``'s own default order, minus its ``--python`` flag. The
    launchers import raven, and this process's interpreter has it by
    definition."""
    import sys

    assert va._resolved_python() == sys.executable

    os.environ["SUBAGENT_PYTHON"] = "/opt/python3"
    try:
        assert va._resolved_python() == "/opt/python3"
    finally:
        del os.environ["SUBAGENT_PYTHON"]


class TestInstallingTheWheelsOwnTree:
    """``_install_packaged_tree`` -- why a wheel's tree is copied out at all.

    A product's ``.env`` is written into its own folder, and a wheel install is
    replaced wholesale on upgrade. Left under site-packages the keys die every
    time and every agent falls back to inheriting the host's LLM until someone
    types them again. These pin the states that follow from copying it out
    under a version stamp.
    """

    def _packaged(self, tmp_path: Path) -> Path:
        packaged = tmp_path / "site-packages" / "raven" / "agents"
        packaged.mkdir(parents=True)
        for name in ("raven-one", "raven-two"):
            _product(packaged, name, manifest={**_MANIFEST, "name": name})
        # A root-level file the way the real tree ships one (its README); the
        # copy-out is folder-shaped and leaves it behind.
        (packaged / "README.md").write_text("the tree\n", encoding="utf-8")
        return packaged

    def test_a_fresh_install_lands_every_folder_in_the_raven_home(self, tmp_path: Path) -> None:
        installed = tmp_path / "home" / "agents"

        va._install_packaged_tree(self._packaged(tmp_path), installed)

        assert {p.name for p in installed.iterdir() if p.is_dir()} == {"raven-one", "raven-two"}
        assert (installed / "raven-one" / "run.py").is_file()

    def test_only_the_product_folders_are_copied_out(self, tmp_path: Path) -> None:
        """The folders are the products; the tree's root files are packaging
        residue here (the fork tree's ``install.sh`` rationale retired with the
        venvs it built)."""
        installed = tmp_path / "home" / "agents"

        va._install_packaged_tree(self._packaged(tmp_path), installed)

        assert not (installed / "README.md").exists()

    def test_a_folder_deleted_under_the_same_version_stays_deleted(self, tmp_path: Path) -> None:
        """Otherwise the scan's whole point is undone: a user removes an agent,
        and the next start quietly puts it back."""
        import shutil

        packaged = self._packaged(tmp_path)
        installed = tmp_path / "home" / "agents"
        va._install_packaged_tree(packaged, installed)

        shutil.rmtree(installed / "raven-two")
        va._install_packaged_tree(packaged, installed)

        assert {p.name for p in installed.iterdir() if p.is_dir()} == {"raven-one"}

    def test_an_upgrade_restores_what_the_release_ships_without_losing_a_key(self, tmp_path: Path) -> None:
        """The ``.env`` is the part only the user can produce, and a new release
        of the same product does not invalidate it -- the packaged tree never
        carries one, so the refresh has nothing to overwrite it with."""
        import shutil

        packaged = self._packaged(tmp_path)
        installed = tmp_path / "home" / "agents"
        va._install_packaged_tree(packaged, installed)
        env = installed / "raven-one" / ".env"
        env.write_text("ONE_API_KEY=sk-typed-by-hand\n", encoding="utf-8")
        shutil.rmtree(installed / "raven-two")

        (installed / ".raven-version").write_text("0.0.0-older", encoding="utf-8")
        va._install_packaged_tree(packaged, installed)

        assert {p.name for p in installed.iterdir() if p.is_dir()} == {"raven-one", "raven-two"}
        assert env.read_text(encoding="utf-8") == "ONE_API_KEY=sk-typed-by-hand\n"

    def test_an_unwritable_home_is_a_warning_rather_than_a_failed_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not having the products is a smaller problem than not starting."""
        import shutil as real_shutil

        def _explode(*_args, **_kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(real_shutil, "copytree", _explode)

        va._install_packaged_tree(self._packaged(tmp_path), tmp_path / "home" / "agents")


class TestAStoredRowThatWentStale:
    """A config row wins over a discovered one -- unless it cannot run.

    Each folder's ``install.py`` bakes an absolute path into the row it writes, so
    a row written while the tree sat under site-packages keeps naming that path
    after an upgrade replaced the wheel. Without this, "upgrade" would mean
    "every agent now fails at dispatch with file-not-found", and the discovered row
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

        _product(tree, "raven-probe")
        stale = ThirdPartyCliSubagentConfig(
            name="Raven-Probe",
            command=f"{sys.executable} /gone/site-packages/raven/agents/raven-probe/run.py {{prompt}}",
        )

        merged = va.merge_product_seeds([stale], va.discover_product_rows())

        assert len(merged) == 1
        assert str(tree) in merged[0].command

    def test_a_row_whose_launcher_is_there_still_wins(self, tree: Path) -> None:
        """The override has to keep working; this guard is narrow on purpose."""
        import sys

        from raven.config.schema import ThirdPartyCliSubagentConfig

        folder = _product(tree, "raven-probe")
        mine = ThirdPartyCliSubagentConfig(
            name="Raven-Probe", command=f"{sys.executable} {folder / 'run.py'} --mine {{prompt}}"
        )

        merged = va.merge_product_seeds([mine], va.discover_product_rows())

        assert "--mine" in merged[0].command

    def test_a_command_with_no_absolute_path_is_never_called_stale(self, tree: Path) -> None:
        """A hand-written row invoking something on ``PATH`` has no path to check,
        and guessing that it is broken would delete a working registration."""
        from raven.config.schema import ThirdPartyCliSubagentConfig

        _product(tree, "raven-probe")
        on_path = ThirdPartyCliSubagentConfig(name="Raven-Probe", command="my-agent --run {prompt}")

        merged = va.merge_product_seeds([on_path], va.discover_product_rows())

        assert merged[0].command == "my-agent --run {prompt}"


class TestAWindowsShapedCommand:
    """Drive-letter paths are absolute paths to both readiness probes.

    ``token.startswith("/")`` matched no token of a native-Windows command
    line (``C:\\Python312\\python.exe ...``), so ``_launcher_missing`` checked
    no file and reported ready -- a product with no launcher on disk went onto
    the roster as exactly the name the module docstring promises the
    dispatching model will not be handed. The pure-path judgment reads the
    token's shape, not the host's flavor, which is why these run on POSIX: a
    Windows-shaped token here fails the existence check and reads as missing,
    the fail-closed direction.
    """

    _WIN_COMMAND = r"C:\Python312\python.exe C:\raven\agents\raven-win\run.py --prompt-file {prompt_file}"

    def test_a_missing_windows_launcher_disables_the_row_with_the_token_named(self, tree: Path) -> None:
        manifest = {
            "name": "Raven-Win",
            "kind": "cli",
            "description": "a manifest resolved on native windows",
            "command": self._WIN_COMMAND,
        }
        _product(tree, "raven-win", manifest=manifest, launcher=False)

        (row,) = va.discover_product_rows()
        verdict = va.product_state()["Raven-Win"]

        assert row.enabled is False
        assert verdict.kind == "launcher"
        assert r"C:\Python312\python.exe" in verdict.detail

    def test_a_stored_windows_row_whose_launcher_is_gone_loses_to_the_discovered_one(self, tree: Path) -> None:
        """The same shape through ``_launcher_is_gone``: a POSIX host cannot
        run a drive-letter command either way, so the discovered row that does
        work is the one that must win."""
        from raven.config.schema import ThirdPartyCliSubagentConfig

        _product(tree, "raven-probe")
        stale = ThirdPartyCliSubagentConfig(name="Raven-Probe", command=self._WIN_COMMAND)

        assert va._launcher_is_gone(stale) is True
        merged = va.merge_product_seeds([stale], va.discover_product_rows())
        assert len(merged) == 1
        assert str(tree) in merged[0].command

    def test_both_drive_letter_spellings_are_checked(self) -> None:
        """Backslash and forward-slash drive paths are one shape to the pure
        classes, and a token with no absolute shape at all stays unchecked."""
        assert va._launcher_missing({"command": r"C:\gone\python.exe"}) == r"C:\gone\python.exe"
        assert va._launcher_missing({"command": "C:/gone/python.exe"}) == "C:/gone/python.exe"
        assert va._launcher_missing({"command": "my-agent --run {prompt}"}) == ""


class TestTheEdgesThatDegrade:
    """Paths that exist so a broken tree cannot stop raven from starting.

    Each one returns "not there / not ready" instead of raising, and each is
    reachable from a real state -- a half-copied folder, an unreadable file, a
    manifest holding something other than an object. Asserted because a
    degradation nobody exercises is a degradation nobody knows the shape of.
    """

    def test_no_tree_anywhere_reads_as_no_tree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        clone = tmp_path / "clone"
        (clone / "raven").mkdir(parents=True)
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "no-home"))
        monkeypatch.setattr("raven.__file__", str(clone / "raven" / "__init__.py"))

        assert _REAL_AGENTS_ROOT() is None

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

    def test_a_manifest_holding_a_list_is_skipped_not_crashed(self, tree: Path) -> None:
        folder = _product(tree, "raven-probe")
        (folder / "subagent.json").write_text("[]", encoding="utf-8")

        assert va.discover_product_rows() == []

    def test_a_malformed_engine_block_reads_as_no_engine(self, tree: Path) -> None:
        """A manifest typo degrades to the launcher's own loud refusal at
        dispatch rather than a scan crash or a silently dropped folder."""
        _product(tree, "raven-probe", manifest={**_ACP_MANIFEST, "engine": "not-a-mapping"})

        (row,) = va.discover_product_rows()

        assert row.enabled is True

    def test_a_nameless_row_on_either_side_is_dropped(self, tree: Path) -> None:
        """Both merge loops skip a row with no name, because a row that cannot be
        addressed cannot be dispatched to or overridden."""

        class _Nameless:
            name = ""
            command = "x"

        merged = va.merge_product_seeds([_Nameless()], [_Nameless()])

        assert merged == []

    def test_a_packaged_folder_without_a_manifest_is_not_copied_out(self, tmp_path: Path) -> None:
        """The install-out step uses the same "is this a product folder" test the
        scan does, so a stray directory in the wheel does not land in the home."""
        packaged = tmp_path / "packaged"
        _product(packaged, "raven-real")
        (packaged / "docs").mkdir()
        installed = tmp_path / "home" / "agents"

        va._install_packaged_tree(packaged, installed)

        assert {p.name for p in installed.iterdir() if p.is_dir()} == {"raven-real"}

    def test_the_verdict_names_the_reason_kind(self, tree: Path) -> None:
        """The page branches on the kind rather than matching a sentence, so the
        two kinds and their order (a folder with both problems reports the
        launcher first -- no engine matters until something can be started) are
        the contract."""
        _product(tree, "raven-gone", manifest={**_MANIFEST, "name": "Gone"}, launcher=False)
        _product(
            tree,
            "raven-engineless",
            manifest={**_ACP_MANIFEST, "name": "Engineless", "engine": {"package": _MISSING_ENGINE}},
        )
        _product(
            tree,
            "raven-both",
            manifest={**_ACP_MANIFEST, "name": "Both", "engine": {"package": _MISSING_ENGINE}},
            launcher=False,
        )

        state = va.product_state()

        assert {name: v.kind for name, v in state.items()} == {
            "Gone": "launcher",
            "Engineless": "engine",
            "Both": "launcher",
        }
        assert not any(v.ready for v in state.values())
        # An undeclared wheel name falls back to the import name, so the detail
        # still tells the reader what to install.
        assert _MISSING_ENGINE in state["Engineless"].detail

    def test_a_ready_folder_reports_ready_with_no_reason(self, tree: Path) -> None:
        _product(tree, "raven-probe")

        (verdict,) = va.product_state().values()

        assert verdict.ready is True and verdict.kind == "" and verdict.detail == ""

    def test_the_folder_behind_a_name_is_looked_up_not_derived(self, tree: Path) -> None:
        """The manifest names itself (``Raven-Probe``) and the folder is spelled
        differently (``raven-probe``); a mapping guessed from either spelling
        breaks on the next product."""
        folder = _product(tree, "raven-probe")

        assert va.product_folder("Raven-Probe") == folder
        assert va.product_folder("raven-probe") is None

    def test_a_config_row_naming_no_folder_is_simply_appended(self, tree: Path) -> None:
        """The ordinary case for every agent a user registers themselves: it is
        not a discovered product, so it joins the list rather than overriding
        one."""
        from raven.config.schema import ThirdPartyCliSubagentConfig

        _product(tree, "raven-probe")
        mine = ThirdPartyCliSubagentConfig(name="my-codex", command="codex {prompt}")

        merged = va.merge_product_seeds([mine], va.discover_product_rows())

        assert [getattr(r, "name") for r in merged] == ["Raven-Probe", "my-codex"]


def test_a_manifest_that_declares_itself_off_stays_off(tree: Path) -> None:
    """`"enabled": false` is a field the manifest has; ignoring it accepted a
    declaration and did the opposite. Both conditions hold: a folder that says it
    is off stays off, and one that cannot start is off whatever it says."""
    _product(tree, "raven-probe", manifest={**_MANIFEST, "enabled": False})

    (row,) = va.discover_product_rows()

    assert row.enabled is False


def test_a_manifest_can_declare_what_its_agent_owns(tree: Path) -> None:
    from raven.agent.subagent.vendored_agents import discover_product_rows

    _product(tree, "scribe", manifest={**_MANIFEST, "name": "Scribe", "owns": "owns decks."})
    row = next(r for r in discover_product_rows() if r.name == "Scribe")
    assert row.owns == "owns decks."


def test_a_stored_row_written_before_owns_existed_still_gets_it(tree: Path) -> None:
    """The trap this closes: config rows win over discovered ones whole, so an
    install whose config predates the field would never see it and the feature
    would be silently inert until every agent was reinstalled. ``owns`` is a
    manifest fact about what the agent is for, not a user preference, so an
    unset one is filled from the folder."""
    from raven.agent.subagent.vendored_agents import discover_product_rows, merge_product_seeds

    _product(tree, "scribe", manifest={**_MANIFEST, "name": "Scribe", "owns": "owns decks."})
    discovered = discover_product_rows()
    stored = next(r for r in discovered if r.name == "Scribe").model_copy(
        update={"owns": None, "description": "edited by the user"}
    )

    merged = merge_product_seeds([stored], discovered)
    row = next(r for r in merged if r.name == "Scribe")
    assert row.owns == "owns decks."
    # Proves the fill was field-level: a wholesale swap would take the
    # manifest's description back too.
    assert row.description == "edited by the user"


def test_a_stored_row_takes_hidden_and_routes_from_its_folder(tree: Path) -> None:
    """Both are facts about how the folder's agent is reached, and a stored
    ``False`` cannot be told from a row written before the fields existed, so
    the manifest's answer always wins -- field-level, like ``owns``."""
    from raven.agent.subagent.vendored_agents import discover_product_rows, merge_product_seeds

    _product(tree, "deck", manifest={**_ACP_MANIFEST, "name": "Deck", "hidden": True})
    _product(tree, "design", manifest={**_ACP_MANIFEST, "name": "Design", "routes": [{"to": "Deck"}]})
    discovered = discover_product_rows()
    stored = [
        next(r for r in discovered if r.name == "Deck").model_copy(update={"hidden": False}),
        next(r for r in discovered if r.name == "Design").model_copy(update={"routes": [], "description": "edited"}),
    ]

    merged = {r.name: r for r in merge_product_seeds(stored, discovered)}
    assert merged["Deck"].hidden is True
    assert [r.to for r in merged["Design"].routes] == ["Deck"]
    assert merged["Design"].description == "edited"


class TestARouteWhoseNoteIsAFile:
    """Prose long enough to be worth writing does not belong in a JSON string.

    The manifest names a file beside it and discovery reads it, so the route
    every consumer downstream sees is the route it always was.
    """

    @staticmethod
    def _design(tree: Path, route: dict) -> Path:
        return _product(tree, "design", manifest={**_ACP_MANIFEST, "name": "Design", "routes": [route]})

    def test_the_named_file_is_read_into_the_note(self, tree: Path) -> None:
        from raven.agent.subagent.vendored_agents import discover_product_rows

        folder = self._design(tree, {"to": "Deck", "owes": ".pptx", "noteFile": "route-deck.md"})
        (folder / "route-deck.md").write_text("Hand back a .pptx.\n\nAnd look at the page.\n", encoding="utf-8")
        _product(tree, "deck", manifest={**_ACP_MANIFEST, "name": "Deck"})

        (route,) = {r.name: r for r in discover_product_rows()}["Design"].routes
        assert route.note == "Hand back a .pptx.\n\nAnd look at the page."
        assert route.owes == ".pptx"
        # The pointer does not survive into the row: one field carries the note,
        # whichever form the author wrote it in.
        assert route.note_file == ""

    def test_a_note_file_that_is_not_there_leaves_the_row_up_and_the_note_empty(self, tree: Path) -> None:
        """The row without its note is an agent running with one requirement
        unstated; the row disabled is the agent gone. The first is the smaller
        failure, and it is loud in the log."""
        from raven.agent.subagent.vendored_agents import discover_product_rows

        self._design(tree, {"to": "Deck", "noteFile": "gone.md"})
        _product(tree, "deck", manifest={**_ACP_MANIFEST, "name": "Deck"})

        rows = {r.name: r for r in discover_product_rows()}
        assert rows["Design"].enabled is True
        ((route,)) = rows["Design"].routes
        # Consumed and found empty, not ignored: a row that never knew about the
        # pointer would answer with the same empty note.
        assert route.note_file == "" and route.note == ""

    def test_a_note_file_outside_the_folder_is_refused(self, tree: Path) -> None:
        """The folder is what gets copied, installed and packaged, so a note
        above it is a note that does not travel with the agent."""
        from raven.agent.subagent.vendored_agents import discover_product_rows

        (tree / "elsewhere.md").write_text("read me", encoding="utf-8")
        self._design(tree, {"to": "Deck", "noteFile": "../elsewhere.md"})
        _product(tree, "deck", manifest={**_ACP_MANIFEST, "name": "Deck"})

        ((route,)) = {r.name: r for r in discover_product_rows()}["Design"].routes
        assert route.note_file == "" and route.note == ""

    def test_declaring_the_note_twice_refuses_the_row_rather_than_picking_one(self, tree: Path) -> None:
        """Two sources for one sentence is a manifest that will disagree with
        itself the first time only one of them is edited."""
        from raven.agent.subagent.vendored_agents import discover_product_rows

        folder = self._design(tree, {"to": "Deck", "note": "inline", "noteFile": "route-deck.md"})
        (folder / "route-deck.md").write_text("from the file", encoding="utf-8")
        _product(tree, "deck", manifest={**_ACP_MANIFEST, "name": "Deck"})

        assert [r.name for r in discover_product_rows()] == ["Deck"]


class TestAFrontingRowIsOnlyAsReadyAsItsRoutes:
    """Its roster line claims the targets' work, and its routing entry drops a
    target that is not on the table; a product with nobody behind its door
    must not report ready, or the missing half's work runs on the wrong
    implementation with nothing anywhere to say so."""

    @staticmethod
    def _design(tree: Path, to: str = "Deck") -> None:
        _product(tree, "design", manifest={**_ACP_MANIFEST, "name": "Design", "routes": [{"to": to}]})

    def test_a_target_whose_engine_is_missing_takes_the_row_down_with_it(self, tree: Path) -> None:
        from raven.agent.subagent.vendored_agents import discover_product_rows, product_state

        self._design(tree)
        _product(
            tree,
            "deck",
            manifest={**_ACP_MANIFEST, "name": "Deck", "engine": {"package": _MISSING_ENGINE, "wheel": "probe-engine"}},
        )

        rows = {r.name: r for r in discover_product_rows()}
        assert rows["Design"].enabled is False and rows["Deck"].enabled is False
        verdict = product_state()["Design"]
        assert verdict.kind == "route"
        assert "'Deck'" in verdict.detail and "probe-engine" in verdict.detail

    def test_a_target_that_is_no_product_here(self, tree: Path) -> None:
        from raven.agent.subagent.vendored_agents import discover_product_rows, product_state

        self._design(tree, to="Ghost")

        assert {r.name: r.enabled for r in discover_product_rows()} == {"Design": False}
        assert product_state()["Design"] == ("route", "routes to 'Ghost', which is not a product here")

    def test_a_target_its_own_manifest_switches_off(self, tree: Path) -> None:
        from raven.agent.subagent.vendored_agents import discover_product_rows, product_state

        self._design(tree)
        _product(tree, "deck", manifest={**_ACP_MANIFEST, "name": "Deck", "enabled": False})

        assert {r.name: r.enabled for r in discover_product_rows()} == {"Deck": False, "Design": False}
        assert product_state()["Design"].kind == "route"

    def test_ready_targets_leave_the_row_ready(self, tree: Path) -> None:
        from raven.agent.subagent.vendored_agents import discover_product_rows, product_state

        self._design(tree)
        _product(tree, "deck", manifest={**_ACP_MANIFEST, "name": "Deck"})

        assert {r.name: r.enabled for r in discover_product_rows()} == {"Deck": True, "Design": True}
        assert product_state()["Design"].ready


def test_an_explicit_empty_owns_is_the_users_opt_out(tree: Path) -> None:
    """Absent and explicitly-blank are different answers: blank means the user
    took the agent out of the delegation section on purpose."""
    from raven.agent.subagent.vendored_agents import discover_product_rows, merge_product_seeds

    _product(tree, "scribe", manifest={**_MANIFEST, "name": "Scribe", "owns": "owns decks."})
    discovered = discover_product_rows()
    stored = next(r for r in discovered if r.name == "Scribe").model_copy(update={"owns": ""})

    merged = merge_product_seeds([stored], discovered)
    assert next(r for r in merged if r.name == "Scribe").owns == ""


class TestTheShippedManifests:
    """The real ``subagent.json`` files, validated as what they declare.

    Every other test here stands on a synthetic tree, which is what keeps them
    independent of which engine wheels the developer has installed. That leaves
    the shipped manifests themselves unguarded, and they are the one part a
    user cannot fix: a manifest that fails validation drops its folder from the
    roster with a log line nobody reads, and one that carries a field its own
    ``kind`` does not support is rejected outright by the install-time write
    path (``update_subagents.reject_unsupported_acp_fields``) -- so the folder
    installs on some paths and not others.

    Read from the repository rather than through ``agents_root()``: the point
    is the files this commit ships, not whichever tree the machine running the
    tests happens to resolve to.
    """

    @staticmethod
    def _manifests() -> list[tuple[str, dict]]:
        root = Path(__file__).resolve().parent.parent / "agents"
        found = [
            (p.parent.name, json.loads(p.read_text(encoding="utf-8"))) for p in sorted(root.glob("*/subagent.json"))
        ]
        assert found, f"no product manifests under {root}"
        return found

    def test_every_manifest_validates_under_the_schema_its_kind_names(self) -> None:
        from raven.config.schema import ThirdPartyAcpSubagentConfig, ThirdPartyCliSubagentConfig

        for folder, entry in self._manifests():
            model = ThirdPartyAcpSubagentConfig if entry.get("kind") == "acp" else ThirdPartyCliSubagentConfig
            try:
                model.model_validate(entry)
            except Exception as exc:  # noqa: BLE001 - the folder name is the whole point of the message
                pytest.fail(f"{folder}: manifest does not validate as kind {entry.get('kind')!r}: {exc}")

    def test_every_manifest_ships_its_launcher(self) -> None:
        """The readiness probe reads the command's file tokens, so a manifest
        whose ``run.py`` is missing would ship a permanently disabled row."""
        root = Path(__file__).resolve().parent.parent / "agents"
        for folder, _entry in self._manifests():
            assert (root / folder / "run.py").is_file(), f"{folder}: no run.py beside the manifest"

    def test_every_declared_engine_resolves_in_the_dev_environment(self) -> None:
        """The engine wheels are workspace dev dependencies, so a declaration
        with a typo in its import name fails here rather than shipping a
        product that is disabled on every install with the engine present."""
        import importlib.util

        declared = [
            (folder, entry["engine"]) for folder, entry in self._manifests() if isinstance(entry.get("engine"), dict)
        ]
        assert {f for f, _ in declared} == {"raven-design", "raven-ppt"}, "the two engine-backed products"
        for folder, engine in declared:
            assert engine.get("wheel"), f"{folder}: an engine declaration must name its wheel for the hint"
            package = engine.get("package") or ""
            assert importlib.util.find_spec(package) is not None, f"{folder}: engine package {package!r} not found"

    def test_no_acp_manifest_declares_a_cli_only_field(self) -> None:
        """These are rejected on the write path, not merely warned about, so a
        manifest carrying one installs through discovery and fails through
        ``install.py``."""
        from pydantic.alias_generators import to_camel

        from raven.config.schema import ACP_UNSUPPORTED_FIELDS

        for folder, entry in self._manifests():
            if entry.get("kind") != "acp":
                continue
            declared = [s for f in ACP_UNSUPPORTED_FIELDS for s in (f, to_camel(f)) if s in entry]
            assert not declared, f"{folder}: cli-only field(s) on an acp manifest: {declared}"

    def test_no_acp_command_carries_a_per_task_placeholder(self) -> None:
        """An acp ``command`` starts a server once per connection, so a task
        placeholder reaches the child as a literal argv token and the handshake
        fails."""
        from raven.config.schema import ACP_PROMPT_PLACEHOLDERS

        for folder, entry in self._manifests():
            if entry.get("kind") != "acp":
                continue
            left = [p for p in ACP_PROMPT_PLACEHOLDERS if p in str(entry.get("command", ""))]
            assert not left, f"{folder}: task placeholder(s) in an acp command: {left}"

    def test_a_pinned_acp_cwd_is_the_folder_placeholder(self) -> None:
        """Whether to pin ``cwd`` is per folder, but the value is not.

        Pinning keeps the pool's launch key constant, so one server serves every
        workspace. What is asserted is only that a folder which does pin it
        names its own directory rather than a literal path, since an absolute
        path baked into a manifest is wrong on every machine but the one it was
        written on.
        """
        for folder, entry in self._manifests():
            if entry.get("kind") != "acp" or "cwd" not in entry:
                continue
            assert entry["cwd"] == "{SUBAGENT_DIR}", f"{folder}: a pinned acp cwd must be the folder placeholder"

    def test_no_manifest_ships_an_env_block(self) -> None:
        """``env`` is per-install: the ACP child is built from the login shell's
        environment, so a value like ``RAVEN_HOME`` is needed but cannot be a
        shipped constant. Leaving it out is what makes the default correct."""
        for folder, entry in self._manifests():
            assert not entry.get("env"), f"{folder}: manifest must not ship an env block"

    def test_each_installer_resolves_every_placeholder_field_its_manifest_uses(self) -> None:
        """Discovery and ``install.py`` must agree on the resolved row.

        ``_PLACEHOLDER_FIELDS`` is documented as "exactly the set each product's
        own install.py substitutes", and a field resolved in one but not the
        other means a discovered row and an installed row disagreeing -- for
        ``cwd``, one with a real path and one with the literal
        ``{SUBAGENT_DIR}``, which is a launch key that never matches.

        Asserted per folder against the fields its own manifest actually carries,
        not against the whole tuple: a folder whose manifest declares no ``cwd``
        is not made wrong by an installer that would not have resolved one.
        """
        root = Path(__file__).resolve().parent.parent / "agents"
        for folder, entry in self._manifests():
            source = (root / folder / "install.py").read_text(encoding="utf-8")
            for field in va._PLACEHOLDER_FIELDS:
                if "{SUBAGENT_DIR}" not in str(entry.get(field, "")) and "{PYTHON}" not in str(entry.get(field, "")):
                    continue
                assert f'"{field}"' in source, f"{folder}: manifest uses {field!r} but install.py does not resolve it"


def test_a_stored_row_of_the_old_kind_loses_to_the_folders_new_one(tree: Path) -> None:
    """The upgrade path for a folder that changes transport.

    ``install.py`` writes a complete row, so a stored row wins on name -- which
    means a folder's move from cli to acp would never reach an install that had
    already registered it. The launcher still exists here, so this can only pass
    through the kind check rather than the stale-path one.
    """
    from raven.agent.subagent.vendored_agents import discover_product_rows, merge_product_seeds
    from raven.config.schema import ThirdPartyCliSubagentConfig

    folder = _product(tree, "scribe", manifest={**_ACP_MANIFEST, "name": "Scribe"})
    discovered = discover_product_rows()
    assert next(r for r in discovered if r.name == "Scribe").kind == "acp"

    stored = ThirdPartyCliSubagentConfig(
        name="Scribe",
        command=f"{folder / 'run.py'} --prompt-file {{prompt_file}}",
        description="the row install.py wrote before the folder moved to acp",
    )
    assert not va._launcher_is_gone(stored), "the launcher must exist, or this passes for the wrong reason"

    row = next(r for r in merge_product_seeds([stored], discovered) if r.name == "Scribe")
    assert row.kind == "acp"
    assert row.cwd == str(folder)


def test_a_stored_row_of_the_same_kind_still_wins(tree: Path) -> None:
    """The boundary on the check above: a same-kind row is the user's edit of the
    baseline and keeps winning, which is what makes the config editable at all."""
    from raven.agent.subagent.vendored_agents import discover_product_rows, merge_product_seeds

    _product(tree, "scribe", manifest={**_ACP_MANIFEST, "name": "Scribe"})
    discovered = discover_product_rows()
    stored = next(r for r in discovered if r.name == "Scribe").model_copy(
        update={"description": "edited by the user", "ready_timeout_ms": 90000}
    )

    row = next(r for r in merge_product_seeds([stored], discovered) if r.name == "Scribe")
    assert row.description == "edited by the user"
    assert row.ready_timeout_ms == 90000


def test_a_disabled_stored_row_of_the_old_kind_keeps_its_no(tree: Path) -> None:
    """A stale row is two things at once: a definition to drop and a switch to keep.

    Fork-era installs wrote the operator's off onto the full definition row --
    there was no stub to hold it -- so discarding the row whole on a transport
    change silently re-enabled every agent an operator had switched off.
    """
    from raven.agent.subagent.vendored_agents import discover_product_rows, merge_product_seeds
    from raven.config.schema import ThirdPartyCliSubagentConfig

    folder = _product(tree, "scribe", manifest={**_ACP_MANIFEST, "name": "Scribe"})
    stored = ThirdPartyCliSubagentConfig(
        name="Scribe",
        command=f"{folder / 'run.py'} --prompt-file {{prompt_file}}",
        description="the row install.py wrote, switched off by the operator",
        enabled=False,
    )
    assert not va._launcher_is_gone(stored), "the launcher must exist, or the kind check is never reached"

    row = next(r for r in merge_product_seeds([stored], discover_product_rows()) if r.name == "Scribe")
    assert row.kind == "acp"
    assert row.enabled is False


def test_a_disabled_stored_row_with_a_gone_launcher_keeps_its_no(tree: Path) -> None:
    """The other stale branch carries the flag the same way: the discovered
    command replaces the dead one, and the operator's off stays on the row."""
    import sys

    from raven.config.schema import ThirdPartyCliSubagentConfig

    _product(tree, "raven-probe")
    stale = ThirdPartyCliSubagentConfig(
        name="Raven-Probe",
        command=f"{sys.executable} /gone/site-packages/raven/agents/raven-probe/run.py {{prompt}}",
        enabled=False,
    )

    (row,) = va.merge_product_seeds([stale], va.discover_product_rows())
    assert str(tree) in row.command
    assert row.enabled is False


def test_a_stored_true_on_a_stale_row_never_overrides_readiness(tree: Path) -> None:
    """Only the "no" travels. A stored true is not carried onto the discovered
    row: enabled there is the folder's readiness verdict, and a flag written
    against the old transport says nothing about whether the new one can start.
    """
    from raven.agent.subagent.vendored_agents import discover_product_rows, merge_product_seeds
    from raven.config.schema import ThirdPartyCliSubagentConfig

    ready = _product(tree, "scribe", manifest={**_ACP_MANIFEST, "name": "Scribe"})
    engineless = _product(
        tree,
        "quill",
        manifest={**_ACP_MANIFEST, "name": "Quill", "engine": {"package": _MISSING_ENGINE, "wheel": "probe-engine"}},
    )
    stored = [
        ThirdPartyCliSubagentConfig(name="Scribe", command=f"{ready / 'run.py'} {{prompt}}", enabled=True),
        ThirdPartyCliSubagentConfig(name="Quill", command=f"{engineless / 'run.py'} {{prompt}}", enabled=True),
    ]

    merged = {r.name: r for r in merge_product_seeds(stored, discover_product_rows())}
    assert merged["Scribe"].kind == "acp" and merged["Scribe"].enabled is True
    assert merged["Quill"].kind == "acp" and merged["Quill"].enabled is False


def test_a_disabled_stored_row_for_an_unready_folder_stays_off(tree: Path) -> None:
    """Both say no: the operator's flag and the folder's readiness agree, and
    carrying the flag onto an already-disabled row must not flip anything."""
    from raven.agent.subagent.vendored_agents import discover_product_rows, merge_product_seeds
    from raven.config.schema import ThirdPartyCliSubagentConfig

    folder = _product(
        tree,
        "scribe",
        manifest={**_ACP_MANIFEST, "name": "Scribe", "engine": {"package": _MISSING_ENGINE, "wheel": "probe-engine"}},
    )
    stored = ThirdPartyCliSubagentConfig(name="Scribe", command=f"{folder / 'run.py'} {{prompt}}", enabled=False)

    row = next(r for r in merge_product_seeds([stored], discover_product_rows()) if r.name == "Scribe")
    assert row.kind == "acp"
    assert row.enabled is False


class TestOneProductsOwnSettings:
    """``product_secret`` reads a folder's ``.env`` the way its launcher does.

    A caller asking whether a routed lane is equipped must read the file the
    lane will be configured from. The host's credentials answer for the host
    loop, and a product is free to be equipped by its folder alone, or
    differently -- a key for a vendor the host never selected.
    """

    def test_a_folders_own_env_answers_for_it(self, tree: Path) -> None:
        folder = _product(tree, "raven-deck", manifest={**_ACP_MANIFEST, "name": "Raven-Deck"})
        (folder / ".env").write_text("DECK_SERPER_API_KEY=sk-lane\n", encoding="utf-8")

        assert va.product_secret("Raven-Deck", "SERPER_API_KEY") == "sk-lane"

    def test_a_setting_the_folder_does_not_carry_is_empty(self, tree: Path) -> None:
        """Empty rather than an error: the caller's cue to fall back to whatever
        the launcher would inherit from the host."""
        folder = _product(tree, "raven-deck", manifest={**_ACP_MANIFEST, "name": "Raven-Deck"})
        (folder / ".env").write_text("DECK_API_KEY=sk-llm\n", encoding="utf-8")

        assert va.product_secret("Raven-Deck", "SERPER_API_KEY") == ""

    def test_a_name_that_is_not_a_product_is_empty(self, tree: Path) -> None:
        assert va.product_secret("Nobody", "SERPER_API_KEY") == ""

    def test_the_prefix_is_the_folders_and_not_the_rows(self, tree: Path) -> None:
        """The manifest names itself (``Raven-Deck``) and the folder is spelled
        differently; every setting is spelled with the folder's stem, so a
        prefix derived from the row name would read nothing."""
        folder = _product(tree, "raven-slide-deck", manifest={**_ACP_MANIFEST, "name": "Raven-Deck"})
        (folder / ".env").write_text("SLIDE_DECK_IMAGE_API_KEY=sk-lane\n", encoding="utf-8")

        assert va.env_prefix(folder.name) == "SLIDE_DECK"
        assert va.product_secret("Raven-Deck", "IMAGE_API_KEY") == "sk-lane"

    def test_the_process_environment_wins_over_the_file(self, tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``env_value``'s own order, which is how an operator overrides one
        setting without editing the file that holds the others."""
        folder = _product(tree, "raven-deck", manifest={**_ACP_MANIFEST, "name": "Raven-Deck"})
        (folder / ".env").write_text("DECK_SERPER_API_KEY=sk-file\n", encoding="utf-8")
        monkeypatch.setenv("DECK_SERPER_API_KEY", "sk-env")

        assert va.product_secret("Raven-Deck", "SERPER_API_KEY") == "sk-env"

    def test_the_shipped_deck_lane_is_read_under_the_names_its_launcher_reads(self) -> None:
        """The two settings the probe asks Raven-PPT about, against the launcher
        that resolves them. A rename on either side would leave the probe reading
        a name no ``.env`` ever carries, and every test above would still pass."""
        launcher = (Path(__file__).resolve().parent.parent / "agents" / "raven-ppt" / "run.py").read_text(
            encoding="utf-8"
        )

        for suffix in ("SERPER_API_KEY", "IMAGE_API_KEY"):
            assert f'"{va.env_prefix("raven-ppt")}_{suffix}"' in launcher


class TestWhatAFolderCanDrawWith:
    """``product_image_key`` mirrors a launcher's ``configure_image_generation``.

    The folder-side precedence, in one reader: the explicit image key, else the
    key paying for the words -- but only towards OpenRouter, which the launcher
    reads off both the image base pin and the gateway the provider blocks
    address. Empty means "only the host can supply one", not "cannot draw".
    """

    def _folder(self, tree: Path, providers: dict | None = None, **settings: str) -> Path:
        folder = _product(tree, "raven-deck", manifest={**_ACP_MANIFEST, "name": "Raven-Deck"})
        if providers is not None:
            (folder / "config.json").write_text(json.dumps({"providers": providers}), encoding="utf-8")
        lines = [f"DECK_{name}={value}" for name, value in settings.items()]
        (folder / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return folder

    def test_an_explicit_image_key_is_the_answer(self, tree: Path) -> None:
        self._folder(tree, IMAGE_API_KEY="sk-pictures", API_KEY="sk-own")

        assert va.product_image_key("Raven-Deck") == "sk-pictures"

    def test_the_llm_key_pays_when_its_own_base_is_openrouter(self, tree: Path) -> None:
        self._folder(tree, API_KEY="sk-own", API_BASE="https://openrouter.ai/api/v1")

        assert va.product_image_key("Raven-Deck") == "sk-own"

    def test_the_llm_key_pays_nothing_towards_another_gateway(self, tree: Path) -> None:
        self._folder(tree, API_KEY="sk-own", API_BASE="https://gateway.example/v1")

        assert va.product_image_key("Raven-Deck") == ""

    def test_an_image_base_pinned_elsewhere_declines_the_loan(self, tree: Path) -> None:
        self._folder(
            tree,
            API_KEY="sk-own",
            API_BASE="https://openrouter.ai/api/v1",
            IMAGE_API_BASE="https://pics.example/v1",
        )

        assert va.product_image_key("Raven-Deck") == ""

    def test_with_no_base_pinned_the_folders_own_block_names_the_gateway(self, tree: Path) -> None:
        self._folder(tree, providers={"deck": {"apiBase": "https://openrouter.ai/api/v1"}}, API_KEY="sk-own")

        assert va.product_image_key("Raven-Deck") == "sk-own"

    def test_a_block_with_no_base_is_read_through_the_registry(self, tree: Path) -> None:
        """The other half of how a block says which gateway it is. A block named
        for a known provider and carrying no apiBase is the inherited shape, and
        reading only the explicit field is what left that branch with no key."""
        self._folder(tree, providers={"openrouter": {"models": ["vendor/model"]}}, API_KEY="sk-own")

        assert va.product_image_key("Raven-Deck") == "sk-own"

    def test_a_block_for_somewhere_else_lends_nothing(self, tree: Path) -> None:
        self._folder(tree, providers={"deck": {"apiBase": "https://gateway.example/v1"}}, API_KEY="sk-own")

        assert va.product_image_key("Raven-Deck") == ""

    def test_a_folder_with_no_key_of_its_own_answers_empty(self, tree: Path) -> None:
        """Empty is the caller's cue to ask the host, which is what the launcher
        inherits on this branch."""
        self._folder(tree, providers={"deck": {"apiBase": "https://openrouter.ai/api/v1"}}, SERPER_API_KEY="sk-x")

        assert va.product_image_key("Raven-Deck") == ""

    def test_a_name_that_is_not_a_product_answers_empty(self, tree: Path) -> None:
        assert va.product_image_key("Nobody") == ""

    def test_the_shipped_deck_lane_reads_the_names_its_launcher_reads(self) -> None:
        """The four settings this reader asks Raven-PPT about, against the
        launcher that resolves them, plus the gateway both sides compare on."""
        ppt = Path(__file__).resolve().parent.parent / "agents" / "raven-ppt"
        launcher = (ppt / "run.py").read_text(encoding="utf-8")

        for suffix in ("IMAGE_API_KEY", "API_KEY", "IMAGE_API_BASE", "API_BASE"):
            assert f'"{va.env_prefix("raven-ppt")}_{suffix}"' in launcher
        assert f'_IMAGE_GATEWAY = "{va._IMAGE_GATEWAY}"' in launcher
