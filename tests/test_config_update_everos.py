"""Full coverage for ``raven_everos.config``.

The onboard memory step writes EverOS model settings to
``~/.everos/raven/everos.toml`` through these ops. EverOS reads that file back
via its own pydantic-settings loader, so a malformed / mislocated write silently
breaks memory at runtime — hence the thorough round-trip + section-preservation
coverage here.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

import pytest
from everos.entrypoints.cli.commands.init_cmd import _EVEROS_TEMPLATE

from raven_everos import config as ue


@pytest.fixture(autouse=True)
def _no_ambient_embedding_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the operator's documented override out of the ambient shell.

    ``EVEROS_EMBEDDING__*`` is a real input to ``role_is_env_managed``,
    so a developer who exports it turns every case here that assumes no
    override into a different case -- silently, and only on their machine. A
    case that reads one answer on one machine and another elsewhere is not
    pinning anything. Cases that are about the override set it themselves,
    after this has run.
    """
    for name in ("MODEL", "BASE_URL", "API_KEY", "DIMENSIONS"):
        monkeypatch.delenv(f"EVEROS_EMBEDDING__{name}", raising=False)
    # Provenance is process-global; a case that bound the host endpoint would
    # otherwise tell the next one that raven had already written those.
    ue._BOUND_HERE.clear()


@pytest.fixture
def everos_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the ops library at a throwaway root that raven owns."""
    root = tmp_path / ".everos"
    monkeypatch.setattr(ue, "everos_root", lambda: root)
    monkeypatch.setattr(ue, "everos_owned", lambda: True)
    return root / "everos.toml"


def _read(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def _seed(path: Path, text: str) -> None:
    """Write everos.toml the way an operator does -- by hand.

    ``[api]`` is the one section raven writes now, so a case that needs another
    one present has to put it there itself. That is also the realistic shape:
    every section except the address arrives from somebody editing the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# get_everos_config_path / load_everos_config
# ---------------------------------------------------------------------------


def test_config_path_follows_the_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ue, "everos_root", lambda: tmp_path / "somewhere")
    assert ue.get_everos_config_path() == tmp_path / "somewhere" / "everos.toml"


def test_load_absent_returns_empty(everos_home: Path) -> None:
    assert ue.load_everos_config() == {}


# ---------------------------------------------------------------------------
# root resolution + ownership
# ---------------------------------------------------------------------------


def test_recorded_root_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        ue,
        "_recorded_slice",
        lambda: {"root": str(tmp_path / "recorded"), "owned": True},
    )
    assert ue.everos_root() == tmp_path / "recorded"
    assert ue.everos_owned() is True


def test_legacy_root_is_kept_when_it_holds_a_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An install from before the move must not be pointed at an empty dir."""
    legacy = tmp_path / ".everos" / "raven"
    legacy.mkdir(parents=True)
    (legacy / "everos.toml").write_text("[llm]\n", encoding="utf-8")
    monkeypatch.setattr(ue, "_recorded_slice", dict)
    monkeypatch.setattr(ue, "legacy_everos_root", lambda: legacy)
    # State the premise: the legacy root is only a candidate for the
    # installation that could have created it, and set_config_path is a
    # process global another test may have moved.
    monkeypatch.setattr(ue, "_is_default_installation", lambda: True)

    assert ue.everos_root() == legacy


def test_fresh_install_uses_the_raven_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ue, "_recorded_slice", dict)
    monkeypatch.setattr(ue, "legacy_everos_root", lambda: tmp_path / "absent")
    monkeypatch.setattr(ue, "default_everos_root", lambda: tmp_path / "data" / "everos")

    assert ue.everos_root() == tmp_path / "data" / "everos"


def test_ownership_of_an_unrecorded_foreign_root_is_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Absent the field, anything raven did not create is treated as not ours."""
    monkeypatch.setattr(ue, "_recorded_slice", lambda: {"root": str(tmp_path / "theirs")})
    monkeypatch.setattr(ue, "default_everos_root", lambda: tmp_path / "ours")
    monkeypatch.setattr(ue, "legacy_everos_root", lambda: tmp_path / "legacy")

    assert ue.everos_owned() is False


def test_recorded_ownership_overrides_the_path_guess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A user-managed root cannot be classified by its path, so the record wins."""
    monkeypatch.setattr(
        ue,
        "_recorded_slice",
        lambda: {"root": str(tmp_path / "ours"), "owned": False},
    )
    monkeypatch.setattr(ue, "default_everos_root", lambda: tmp_path / "ours")

    assert ue.everos_owned() is False


# ---------------------------------------------------------------------------
# configure_everos_env
# ---------------------------------------------------------------------------


def test_configure_everos_env_points_at_the_recorded_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ue, "everos_root", lambda: tmp_path / "recorded")
    monkeypatch.delenv("EVEROS_ROOT", raising=False)

    ue.configure_everos_env()

    assert os.environ["EVEROS_ROOT"] == str(tmp_path / "recorded")


def test_configure_everos_env_overrides_an_ambient_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The recorded root wins over the environment, not the other way round.

    This reverses the old contract deliberately. Following an ambient
    EVEROS_ROOT pointed raven at a root nothing had recorded, so the next run
    without the variable reported no memories while they sat on disk. Operators
    who want another root record it in the config.
    """
    monkeypatch.setattr(ue, "everos_root", lambda: tmp_path / "recorded")
    monkeypatch.setenv("EVEROS_ROOT", "/custom/root")

    ue.configure_everos_env()

    assert os.environ["EVEROS_ROOT"] == str(tmp_path / "recorded")


def test_configure_everos_env_accepts_an_explicit_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ue, "everos_root", lambda: tmp_path / "recorded")

    ue.configure_everos_env(tmp_path / "explicit")

    assert os.environ["EVEROS_ROOT"] == str(tmp_path / "explicit")


def test_load_round_trips_written_content(everos_home: Path) -> None:
    ue.set_everos_section("api", {"host": "127.0.0.1", "port": 18791})
    assert ue.load_everos_config()["api"] == {"host": "127.0.0.1", "port": 18791}


# ---------------------------------------------------------------------------
# set_everos_section
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("section", ue.WRITABLE_SECTIONS)
def test_set_each_writable_section(everos_home: Path, section: str) -> None:
    ue.set_everos_section(section, {"model": "m"})
    assert _read(everos_home)[section] == {"model": "m"}


def test_set_creates_file_and_parent_dir(everos_home: Path) -> None:
    assert not everos_home.parent.exists()
    ue.set_everos_section("api", {"host": "127.0.0.1", "port": 18791})
    assert everos_home.exists()
    assert _read(everos_home)["api"] == {"host": "127.0.0.1", "port": 18791}


def test_set_drops_none_values(everos_home: Path) -> None:
    ue.set_everos_section("api", {"host": "127.0.0.1", "port": 18791, "workers": None})
    assert _read(everos_home)["api"] == {"host": "127.0.0.1", "port": 18791}


def test_set_all_none_writes_empty_section(everos_home: Path) -> None:
    ue.set_everos_section("api", {"host": None, "port": None})
    assert _read(everos_home)["api"] == {}


def test_set_empty_fields_writes_empty_section(everos_home: Path) -> None:
    ue.set_everos_section("api", {})
    assert _read(everos_home)["api"] == {}


def test_set_preserves_non_writable_sections(everos_home: Path) -> None:
    # EverOS ships [memory]/[sqlite]/... — the address write must not clobber them.
    _seed(everos_home, '[memory]\nroot = "~/.everos"\n\n[sqlite]\njournal_mode = "WAL"\n')
    ue.set_everos_section("api", {"host": "127.0.0.1"})
    data = _read(everos_home)
    assert data["memory"] == {"root": "~/.everos"}
    assert data["sqlite"] == {"journal_mode": "WAL"}
    assert data["api"] == {"host": "127.0.0.1"}


def test_set_merges_into_existing_section(everos_home: Path) -> None:
    ue.set_everos_section("api", {"host": "127.0.0.1", "port": 1})
    ue.set_everos_section("api", {"port": 18791})
    assert _read(everos_home)["api"] == {"host": "127.0.0.1", "port": 18791}


def test_set_preserves_mixed_value_types(everos_home: Path) -> None:
    # The address carries an int port alongside a string host.
    ue.set_everos_section("api", {"host": "127.0.0.1", "port": 18791})
    got = _read(everos_home)["api"]
    assert got == {"host": "127.0.0.1", "port": 18791}
    assert isinstance(got["port"], int)


def test_set_unknown_section_rejected(everos_home: Path) -> None:
    # ``api`` is the one section raven writes. The four model roles left for
    # raven's own config, and this guard is what stops a second home for them
    # growing back; the data-layout sections EverOS owns were never writable.
    for bad in ("llm", "embedding", "rerank", "multimodal", "sqlite", "memory", "lancedb", ""):
        with pytest.raises(KeyError):
            ue.set_everos_section(bad, {"x": 1})


def test_set_leaves_no_tmp_file(everos_home: Path) -> None:
    # Atomic write goes through a sibling .tmp + os.replace; nothing should linger.
    ue.set_everos_section("api", {"host": "127.0.0.1"})
    leftovers = [p.name for p in everos_home.parent.iterdir() if p.name != "everos.toml"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# clear_everos_section
# ---------------------------------------------------------------------------


def test_clear_removes_section_keeps_siblings(everos_home: Path) -> None:
    _seed(everos_home, '[memory]\nroot = "~/.everos"\n')
    ue.set_everos_section("api", {"host": "127.0.0.1"})
    ue.clear_everos_section("api")
    data = _read(everos_home)
    assert "api" not in data
    assert data["memory"] == {"root": "~/.everos"}


def test_clear_absent_section_is_noop_no_file(everos_home: Path) -> None:
    # No file yet → clearing must not create one.
    ue.clear_everos_section("api")
    assert not everos_home.exists()


def test_clear_absent_section_with_existing_file_preserves_it(everos_home: Path) -> None:
    _seed(everos_home, '[memory]\nroot = "~/.everos"\n')
    ue.clear_everos_section("api")  # api not present
    assert _read(everos_home)["memory"] == {"root": "~/.everos"}


def test_clear_unknown_section_rejected(everos_home: Path) -> None:
    with pytest.raises(KeyError):
        ue.clear_everos_section("sqlite")


# ---------------------------------------------------------------------------
# ownership as a write gate
# ---------------------------------------------------------------------------


@pytest.fixture
def _unowned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """An active root the user manages."""
    monkeypatch.setattr(ue, "everos_root", lambda: tmp_path / "theirs")
    monkeypatch.setattr(ue, "everos_owned", lambda: False)
    return tmp_path / "theirs"


def test_writing_a_section_of_an_unowned_root_is_refused(_unowned: Path) -> None:
    """The read-only promise is enforced at the write, not only at the callers
    that remember to check -- one rule kept in several places is the drift this
    whole change is about."""
    with pytest.raises(ue.EverosRootNotOwnedError, match="managed by the user"):
        ue.set_everos_section("api", {"host": "127.0.0.1", "port": 18791})
    assert not (_unowned / "everos.toml").exists()


def test_clearing_a_section_of_an_unowned_root_is_refused(_unowned: Path) -> None:
    with pytest.raises(ue.EverosRootNotOwnedError):
        ue.clear_everos_section("api")


def test_seeding_templates_into_an_unowned_root_is_refused(_unowned: Path) -> None:
    with pytest.raises(ue.EverosRootNotOwnedError):
        ue.ensure_everos_home()
    assert not _unowned.exists(), "created a directory inside a root the user manages"


def test_the_address_write_is_refused_too(_unowned: Path) -> None:
    """[api] goes through the same gate: the address is raven's to manage only on
    a root raven owns."""
    with pytest.raises(ue.EverosRootNotOwnedError):
        ue.set_everos_api(host="127.0.0.1", port=18791)


def test_a_root_raven_owns_may_be_written(everos_home: Path) -> None:
    ue.set_everos_section("api", {"host": "127.0.0.1", "port": 18791})
    assert everos_home.exists()


# ---------------------------------------------------------------------------
# owned_everos_root
# ---------------------------------------------------------------------------


def test_own_root_falls_back_when_the_active_one_is_the_users(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ "raven needs a root of its own" must never resolve to a root the user
    manages, or declining to share theirs would hand it over anyway."""
    monkeypatch.setattr(ue, "everos_root", lambda: tmp_path / "theirs")
    monkeypatch.setattr(ue, "everos_owned", lambda: False)
    monkeypatch.setattr(ue, "default_everos_root", lambda: tmp_path / "mine")

    assert ue.owned_everos_root() == tmp_path / "mine"


def test_own_root_keeps_the_active_one_when_raven_owns_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ue, "everos_root", lambda: tmp_path / "recorded")
    monkeypatch.setattr(ue, "everos_owned", lambda: True)
    monkeypatch.setattr(ue, "default_everos_root", lambda: tmp_path / "mine")

    assert ue.owned_everos_root() == tmp_path / "recorded"


def test_fallback_root_reads_no_raven_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The migration calls this while holding a config dict of its own; reading
    the globally-current config there would stamp one file with another's root."""
    monkeypatch.setattr(ue, "legacy_everos_root", lambda: tmp_path / "absent")
    monkeypatch.setattr(ue, "default_everos_root", lambda: tmp_path / "mine")

    def _boom() -> dict:
        raise AssertionError("fallback_everos_root read raven's config")

    monkeypatch.setattr(ue, "_recorded_slice", _boom)

    assert ue.fallback_everos_root() == tmp_path / "mine"


class TestTheLegacyRootBelongsToTheDefaultInstall:
    """``~/.everos/raven`` is one machine-wide path, not a per-instance one.

    Every other root raven uses is derived from its config directory, so moving
    the installation moves them. This one is a literal, which made it leak in
    two directions: it ignored the home the process was told to use, and an
    instance running from a moved config would adopt -- and converge, and
    rewrite the ``[api]`` of -- the default installation's root.
    """

    def test_it_follows_the_home_in_use(self, tmp_path, monkeypatch) -> None:
        """``Path("~/...").expanduser()`` reads $HOME directly, so it slipped
        past the ``Path.home`` redirection the test fixtures isolate with and
        reached the developer's own machine."""
        from raven_everos import config as ue

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        assert ue.legacy_everos_root() == tmp_path / ".everos" / "raven"

    def test_the_default_install_still_considers_it(self, tmp_path, monkeypatch) -> None:
        from raven_everos import config as ue

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr("raven.home.get_config_path", lambda: tmp_path / ".raven" / "config.json")

        assert ue.applicable_legacy_root() == tmp_path / ".everos" / "raven"

    def test_a_moved_install_does_not(self, tmp_path, monkeypatch) -> None:
        """The isolated instance never created this root, so treating it as a
        candidate would have one installation converge another's service."""
        from raven_everos import config as ue

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr("raven.home.get_config_path", lambda: tmp_path / "elsewhere" / "config.json")

        assert ue.applicable_legacy_root() is None

    def test_classification_is_unconditional(self, tmp_path, monkeypatch) -> None:
        """Selecting the root and recognising it are different questions. A
        config that already records it recorded a root raven created, whichever
        installation is reading now."""
        from raven_everos import config as ue

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr("raven.home.get_config_path", lambda: tmp_path / "elsewhere" / "config.json")

        assert ue.root_is_raven_owned(tmp_path / ".everos" / "raven") is True

    def test_the_fallback_skips_it_when_moved(self, tmp_path, monkeypatch) -> None:
        from raven_everos import config as ue

        legacy = tmp_path / ".everos" / "raven"
        legacy.mkdir(parents=True)
        (legacy / "everos.toml").write_text("", encoding="utf-8")
        mine = tmp_path / "elsewhere" / "everos"
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr("raven.home.get_config_path", lambda: tmp_path / "elsewhere" / "config.json")
        monkeypatch.setattr(ue, "default_everos_root", lambda: mine)

        assert ue.fallback_everos_root() == mine


def test_a_section_reads_back_as_the_table_that_was_written(everos_home: Path) -> None:
    ue.set_everos_section("api", {"host": "127.0.0.1", "port": 18791})
    ue.set_everos_section("api", {"workers": 4})

    assert ue.everos_section("api") == {
        "host": "127.0.0.1",
        "port": 18791,
        "workers": 4,
    }


def test_a_section_nobody_wrote_reads_as_empty(everos_home: Path) -> None:
    ue.set_everos_section("api", {"host": "127.0.0.1"})

    assert ue.everos_section("memory") == {}
    assert ue.everos_section("api") != {}


class TestWhereEmbeddingsEndpointLives:
    """The embedding endpoint is raven's, and everos.toml's claim survives in
    exactly one place.

    Every reader of "is embedding configured" -- doctor's `configured:` line,
    the wizard's keep/reconfigure menu and its recap, the warning that recall
    has fallen back to keyword matching -- asks one predicate, so that
    predicate has to answer for raven's record or all four go quiet at once.
    """

    @staticmethod
    def _raven_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: dict | None) -> None:
        """Point raven's config at a file this case owns, with a provider that
        can answer for the pin.

        Through ``raven.home._current_config_path``, which is what every read
        ultimately consults -- patching an imported ``get_config_path`` name
        loses to it, and the suite's own home-isolation fixture sets it.
        """
        import json

        path = tmp_path / "config.json"
        data: dict = {}
        if block:
            data["embedding"] = block
            data["providers"] = {"siliconflow": {"apiKey": "sk-sf"}}
        path.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr("raven.home._current_config_path", path)

    def test_an_endpoint_only_raven_holds_still_counts_as_configured(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._raven_config(
            tmp_path,
            monkeypatch,
            {"model": "Qwen/Qwen3-Embedding-4B", "provider": "siliconflow"},
        )

        assert ue.everos_role_configured("embedding") is True

    def test_a_half_written_block_is_not_an_endpoint(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Three values or nothing: the same bar `resolve_role` sets
        # before it binds. Two of them would report configured and then serve
        # a request that cannot be made.
        # A model with nobody to serve it cannot be called, so it is not an endpoint.
        self._raven_config(tmp_path, monkeypatch, {"model": "Qwen/Qwen3-Embedding-4B"})

        assert ue.everos_role_configured("embedding") is False
        assert ue.role_pin("embedding") is None

    def test_no_block_anywhere_is_not_configured(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._raven_config(tmp_path, monkeypatch, None)

        assert ue.everos_role_configured("embedding") is False

    def test_no_other_role_reads_embeddings_block(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All four roles read raven now, but not the same block: embedding's is
        raven's own top-level one, the other three live in this plugin's slice.
        A pinned embedding must not report the others configured."""
        self._raven_config(
            tmp_path,
            monkeypatch,
            {"model": "m", "provider": "siliconflow"},
        )

        assert ue.everos_role_configured("llm") is False
        assert ue.everos_role_configured("rerank") is False

    def test_the_shipped_template_is_not_an_operators_choice(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh managed root is the template, copied verbatim.

        Written from the template itself rather than a hand-built section: the
        template seeds every role with a real model name and an empty key, and
        a criterion of "has a model" reads that as a deliberate choice. It did
        -- a fresh install then bound nothing, and the service the wizard had
        just started ran keyword-only while the wizard said embedding was
        configured. A hand-built section with a key in it cannot catch that.
        """
        import shutil

        from everos.entrypoints.cli.commands.init_cmd import _EVEROS_TEMPLATE

        everos_home.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_EVEROS_TEMPLATE, everos_home)
        assert ue.everos_section("embedding").get("model"), "template must still seed a model, or this proves nothing"
        assert not ue.everos_section("embedding").get("api_key")

        self._raven_config(
            tmp_path,
            monkeypatch,
            {"model": "Qwen/Qwen3-Embedding-4B", "provider": "siliconflow"},
        )
        env = ue.everos_env()
        # Verbatim: "Qwen" is the org on HuggingFace and SiliconFlow serves the
        # id under that name. A head only comes off when it is the provider's
        # own -- the rule `raven.providers.wire` owns for every model id.
        assert env["EVEROS_EMBEDDING__MODEL"] == "Qwen/Qwen3-Embedding-4B"
        assert env["EVEROS_EMBEDDING__API_KEY"] == "sk-sf"

        self._raven_config(
            tmp_path,
            monkeypatch,
            {"model": "siliconflow/BAAI/bge-m3", "provider": "siliconflow"},
        )
        assert ue.everos_env()["EVEROS_EMBEDDING__MODEL"] == "BAAI/bge-m3"

    def test_the_documented_env_override_is_an_operators_choice(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """EVEROS_EMBEDDING__* outranks the file in EverOS's own resolution
        order, so an operator who exports all three has said which endpoint
        memory uses just as plainly as one who edits the toml.

        Before this, the host's block replaced all three: the documented
        override worked until Raven started, and then silently did not.
        """
        import shutil

        from everos.entrypoints.cli.commands.init_cmd import _EVEROS_TEMPLATE

        everos_home.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_EVEROS_TEMPLATE, everos_home)
        for name, value in (
            ("MODEL", "operators/model"),
            ("BASE_URL", "https://operator.example/v1"),
            ("API_KEY", "sk-operator"),
        ):
            monkeypatch.setenv(f"EVEROS_EMBEDDING__{name}", value)
        self._raven_config(tmp_path, monkeypatch, {"model": "ravens/model", "provider": "siliconflow"})

        assert ue.role_is_env_managed("embedding") is True
        # Skipped whole, not blanked: raven emits nothing for a role somebody
        # else exported, so the binding below leaves their values standing.
        assert "EVEROS_EMBEDDING__MODEL" not in ue.everos_env()
        ue.bind_roles_here()
        assert os.environ["EVEROS_EMBEDDING__MODEL"] == "operators/model"

    def test_two_of_the_three_env_values_are_not_an_endpoint(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fragment is not a choice. Filling the third value from the host
        would hand EverOS a mixture of two operators' intentions, so the host's
        complete endpoint replaces the fragment instead."""
        import shutil

        from everos.entrypoints.cli.commands.init_cmd import _EVEROS_TEMPLATE

        everos_home.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_EVEROS_TEMPLATE, everos_home)
        monkeypatch.setenv("EVEROS_EMBEDDING__MODEL", "operators/model")
        monkeypatch.delenv("EVEROS_EMBEDDING__BASE_URL", raising=False)
        monkeypatch.delenv("EVEROS_EMBEDDING__API_KEY", raising=False)
        self._raven_config(tmp_path, monkeypatch, {"model": "ravens/model", "provider": "siliconflow"})

        assert ue.role_is_env_managed("embedding") is False
        assert ue.everos_env()["EVEROS_EMBEDDING__MODEL"] == "ravens/model"

    def test_ravens_own_binding_is_not_an_operators_export(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The runtime sequence, in the order it actually happens.

        `bind_roles_here` puts raven's own pins into this process so
        the in-process imports and every child see it. From that moment all
        three variables are set and complete -- and a reader that asks only
        "are all three set" told the settings card that an operator had
        exported them, which then refused an edit by naming variables the
        person had never set. Clearing the variables before each case, which is
        what makes the other cases deterministic, is also what hides this one:
        it has to be the sequence, not a prepared state.
        """
        import shutil

        from everos.entrypoints.cli.commands.init_cmd import _EVEROS_TEMPLATE

        everos_home.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_EVEROS_TEMPLATE, everos_home)
        self._raven_config(tmp_path, monkeypatch, {"model": "ravens/model", "provider": "siliconflow"})

        assert ue.embedding_is_env_managed() is False, "nothing is exported yet"
        assert ue.bind_roles_here()["EVEROS_EMBEDDING__MODEL"] == "ravens/model"
        assert os.environ["EVEROS_EMBEDDING__MODEL"] == "ravens/model"

        # Everything below is asked after the binding, which is where a reader
        # without provenance changes its answer.
        assert ue.embedding_is_env_managed() is False
        assert ue.role_is_env_managed("embedding") is False
        assert ue.bind_roles_here()["EVEROS_EMBEDDING__MODEL"] == "ravens/model", "a second start must still bind"

    def test_provenance_outlives_the_restart_exec(self, tmp_path: Path) -> None:
        """`raven gateway --restart` re-launches through `os.execv`: the
        environment survives and every module object is rebuilt.

        A set held in the module was therefore empty in the restarted process
        while raven's own values were still exported, and the gateway read its
        own binding as somebody else's for the rest of its life. Run as a real
        second interpreter handed the first one's environment -- importing the
        module twice in one process would prove nothing, since the set would
        simply still be there.
        """
        import json
        import subprocess
        import sys

        root = tmp_path / "everos"
        root.mkdir()
        shutil.copy2(_EVEROS_TEMPLATE, root / "everos.toml")
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "embedding": {"model": "ravens/model", "provider": "siliconflow"},
                    "providers": {"siliconflow": {"apiKey": "sk-raven"}},
                }
            ),
            encoding="utf-8",
        )
        program = (
            "import json,os,pathlib,sys\n"
            "import raven.home as rh\n"
            f"rh.get_config_path = lambda: pathlib.Path({str(cfg)!r})\n"
            "import raven_everos.config as ue\n"
            f"ue.everos_root = lambda: pathlib.Path({str(root)!r})\n"
            f"ue.get_everos_config_path = lambda: pathlib.Path({str(root / 'everos.toml')!r})\n"
            "stage = sys.argv[1]\n"
            "if stage == 'first':\n"
            "    bound = bool(ue.bind_roles_here().get('EVEROS_EMBEDDING__MODEL'))\n"
            "    print(json.dumps({'bound': bound, 'env_managed': ue.embedding_is_env_managed(),\n"
            "                      'env': {k: v for k, v in os.environ.items() if k.startswith(('EVEROS_', 'RAVEN_EVEROS'))}}))\n"
            "else:\n"
            "    print(json.dumps({'env_managed': ue.embedding_is_env_managed(),\n"
            "                      'bound': bool(ue.bind_roles_here().get('EVEROS_EMBEDDING__MODEL'))}))\n"
        )
        base = {k: v for k, v in os.environ.items() if not k.startswith(("EVEROS_", "RAVEN_EVEROS"))}
        first = json.loads(
            subprocess.run(
                [sys.executable, "-c", program, "first"], capture_output=True, text=True, env=base, check=True
            ).stdout
        )
        assert first["bound"] is True and first["env_managed"] is False

        # Exactly what execv hands the replacement: the first process's environment.
        after = json.loads(
            subprocess.run(
                [sys.executable, "-c", program, "second"],
                capture_output=True,
                text=True,
                env={**base, **first["env"]},
                check=True,
            ).stdout
        )
        assert after["env_managed"] is False, "the restarted process must not read its own binding as external"
        assert after["bound"] is True, "and must still bind the host endpoint"

    def test_a_hand_written_section_wins_nowhere(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One answer, both paths.

        There were two: the spawn took raven's pin while the in-process binding
        deferred to whatever the file said, so ``understand_media`` and the
        memory service could run different models with nothing saying so. Both
        read ``everos_env`` now. The file still holds runtime knobs raven never
        sends -- those keep applying -- but it no longer decides which model a
        role runs.

        A shell an operator exported is the one thing that still outranks raven,
        and that is a different mechanism (``role_is_env_managed``).
        """
        _seed(everos_home, '[embedding]\nmodel = "bge-m3"\napi_key = "k-own"\n')
        self._raven_config(
            tmp_path,
            monkeypatch,
            {"model": "Qwen/Qwen3-Embedding-4B", "provider": "siliconflow"},
        )

        assert ue.everos_section("embedding")["model"] == "bge-m3", "the file still says what it said"

        assert ue.everos_env()["EVEROS_EMBEDDING__MODEL"] == "Qwen/Qwen3-Embedding-4B"
        assert ue.bind_roles_here()["EVEROS_EMBEDDING__MODEL"] == "Qwen/Qwen3-Embedding-4B"
        assert os.environ["EVEROS_EMBEDDING__MODEL"] == "Qwen/Qwen3-Embedding-4B"
