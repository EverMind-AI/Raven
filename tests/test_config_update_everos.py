"""Full coverage for ``raven_everos.config``.

The onboard memory step writes EverOS model settings to
``~/.everos/raven/everos.toml`` through these ops. EverOS reads that file back
via its own pydantic-settings loader, so a malformed / mislocated write silently
breaks memory at runtime — hence the thorough round-trip + section-preservation
coverage here.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven_everos import config as ue


@pytest.fixture(autouse=True)
def _no_ambient_embedding_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the operator's documented override out of the ambient shell.

    ``EVEROS_EMBEDDING__*`` is a real input to ``everos_has_own_embedding``,
    so a developer who exports it turns every case here that assumes no
    override into a different case -- silently, and only on their machine. A
    case that reads one answer on one machine and another elsewhere is not
    pinning anything. Cases that are about the override set it themselves,
    after this has run.
    """
    for name in ("MODEL", "BASE_URL", "API_KEY", "DIMENSIONS"):
        monkeypatch.delenv(f"EVEROS_EMBEDDING__{name}", raising=False)


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
    ue.set_everos_section("llm", {"model": "m", "api_key": "k", "base_url": "u"})
    assert ue.load_everos_config()["llm"] == {"model": "m", "api_key": "k", "base_url": "u"}


# ---------------------------------------------------------------------------
# set_everos_section
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("section", ue.WRITABLE_SECTIONS)
def test_set_each_writable_section(everos_home: Path, section: str) -> None:
    ue.set_everos_section(section, {"model": "m"})
    assert _read(everos_home)[section] == {"model": "m"}


def test_set_creates_file_and_parent_dir(everos_home: Path) -> None:
    assert not everos_home.parent.exists()
    ue.set_everos_section("llm", {"model": "gpt-4o-mini", "api_key": "k", "base_url": "u"})
    assert everos_home.exists()
    assert _read(everos_home)["llm"] == {"model": "gpt-4o-mini", "api_key": "k", "base_url": "u"}


def test_set_drops_none_values(everos_home: Path) -> None:
    ue.set_everos_section("rerank", {"provider": "vllm", "model": "m", "api_key": None})
    assert _read(everos_home)["rerank"] == {"provider": "vllm", "model": "m"}


def test_set_all_none_writes_empty_section(everos_home: Path) -> None:
    ue.set_everos_section("llm", {"model": None, "api_key": None})
    assert _read(everos_home)["llm"] == {}


def test_set_empty_fields_writes_empty_section(everos_home: Path) -> None:
    ue.set_everos_section("llm", {})
    assert _read(everos_home)["llm"] == {}


def test_set_preserves_other_writable_sections(everos_home: Path) -> None:
    ue.set_everos_section("llm", {"model": "a"})
    ue.set_everos_section("embedding", {"model": "b"})
    data = _read(everos_home)
    assert data["llm"] == {"model": "a"}
    assert data["embedding"] == {"model": "b"}


def test_set_preserves_non_writable_sections(everos_home: Path) -> None:
    # EverOS ships [memory]/[sqlite]/... — a model-section write must not clobber them.
    everos_home.parent.mkdir(parents=True)
    everos_home.write_text(
        '[memory]\nroot = "~/.everos"\n\n[sqlite]\njournal_mode = "WAL"\n',
        encoding="utf-8",
    )
    ue.set_everos_section("llm", {"model": "a"})
    data = _read(everos_home)
    assert data["memory"] == {"root": "~/.everos"}
    assert data["sqlite"] == {"journal_mode": "WAL"}
    assert data["llm"] == {"model": "a"}


def test_set_merges_into_existing_section(everos_home: Path) -> None:
    ue.set_everos_section("llm", {"model": "a", "api_key": "old"})
    ue.set_everos_section("llm", {"api_key": "new"})
    assert _read(everos_home)["llm"] == {"model": "a", "api_key": "new"}


def test_set_preserves_mixed_value_types(everos_home: Path) -> None:
    # rerank carries ints (timeout_seconds/batch_size) alongside strings.
    ue.set_everos_section(
        "rerank",
        {"provider": "deepinfra", "model": "m", "base_url": "u", "timeout_seconds": 30, "batch_size": 16},
    )
    got = _read(everos_home)["rerank"]
    assert got == {"provider": "deepinfra", "model": "m", "base_url": "u", "timeout_seconds": 30, "batch_size": 16}
    assert isinstance(got["timeout_seconds"], int)


def test_set_unknown_section_rejected(everos_home: Path) -> None:
    # ``api`` is writable now (the address lives there); the data-layout
    # sections EverOS owns still are not.
    for bad in ("sqlite", "memory", "lancedb", ""):
        with pytest.raises(KeyError):
            ue.set_everos_section(bad, {"x": 1})


def test_set_leaves_no_tmp_file(everos_home: Path) -> None:
    # Atomic write goes through a sibling .tmp + os.replace; nothing should linger.
    ue.set_everos_section("llm", {"model": "a"})
    leftovers = [p.name for p in everos_home.parent.iterdir() if p.name != "everos.toml"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# clear_everos_section
# ---------------------------------------------------------------------------


def test_clear_removes_section_keeps_siblings(everos_home: Path) -> None:
    ue.set_everos_section("multimodal", {"model": "m"})
    ue.set_everos_section("llm", {"model": "a"})
    ue.clear_everos_section("multimodal")
    data = _read(everos_home)
    assert "multimodal" not in data
    assert data["llm"] == {"model": "a"}


def test_clear_absent_section_is_noop_no_file(everos_home: Path) -> None:
    # No file yet → clearing must not create one.
    ue.clear_everos_section("rerank")
    assert not everos_home.exists()


def test_clear_absent_section_with_existing_file_preserves_it(everos_home: Path) -> None:
    ue.set_everos_section("llm", {"model": "a"})
    ue.clear_everos_section("rerank")  # rerank not present
    assert _read(everos_home)["llm"] == {"model": "a"}


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
        ue.set_everos_section("llm", {"model": "m", "api_key": "k"})
    assert not (_unowned / "everos.toml").exists()


def test_clearing_a_section_of_an_unowned_root_is_refused(_unowned: Path) -> None:
    with pytest.raises(ue.EverosRootNotOwnedError):
        ue.clear_everos_section("rerank")


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
    ue.set_everos_section("llm", {"model": "m", "api_key": "k"})
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
    ue.set_everos_section("llm", {"model": "gpt-5", "api_key": "k"})
    ue.set_everos_section("llm", {"base_url": "https://api.test"})

    assert ue.everos_section("llm") == {
        "model": "gpt-5",
        "api_key": "k",
        "base_url": "https://api.test",
    }


def test_a_section_nobody_wrote_reads_as_empty(everos_home: Path) -> None:
    ue.set_everos_section("llm", {"model": "gpt-5"})

    assert ue.everos_section("embedding") == {}
    assert ue.everos_section("llm") != {}


class TestEmbeddingHasTwoHomes:
    """The embedding endpoint became raven's, and the toml kept its claim.

    Every reader of "is embedding configured" -- doctor's `configured:` line,
    the wizard's keep/reconfigure menu and its recap, the warning that recall
    has fallen back to keyword matching -- asks one predicate, so that
    predicate has to know both places or all four go quiet at once.
    """

    @staticmethod
    def _raven_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: dict | None) -> None:
        import json

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"embedding": block} if block else {}), encoding="utf-8")
        monkeypatch.setattr("raven.home.get_config_path", lambda: path)

    def test_an_endpoint_only_raven_holds_still_counts_as_configured(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ue.set_everos_section("llm", {"model": "gpt-5", "api_key": "k"})
        self._raven_config(
            tmp_path,
            monkeypatch,
            {"model": "Qwen/Qwen3-Embedding-4B", "baseUrl": "https://api.siliconflow.cn/v1", "apiKey": "sk-sf"},
        )

        assert ue.everos_role_configured("embedding") is True

    def test_a_half_written_block_is_not_an_endpoint(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Three values or nothing: the same bar configure_embedding_env sets
        # before it binds. Two of them would report configured and then serve
        # a request that cannot be made.
        self._raven_config(tmp_path, monkeypatch, {"model": "Qwen/Qwen3-Embedding-4B", "apiKey": "sk-sf"})

        assert ue.everos_role_configured("embedding") is False
        assert ue.host_embedding_section() == {}

    def test_no_block_anywhere_is_not_configured(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._raven_config(tmp_path, monkeypatch, None)

        assert ue.everos_role_configured("embedding") is False

    def test_the_second_home_is_embeddings_alone(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No other role moved, so none of them may start reading raven's block."""
        self._raven_config(
            tmp_path,
            monkeypatch,
            {"model": "m", "baseUrl": "https://e.test/v1", "apiKey": "k"},
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

        assert ue.everos_has_own_embedding() is False

        self._raven_config(
            tmp_path,
            monkeypatch,
            {"model": "Qwen/Qwen3-Embedding-4B", "baseUrl": "https://api.deepinfra.com/v1/openai", "apiKey": "sk-di"},
        )
        env = ue.host_embedding_env()
        assert env["EVEROS_EMBEDDING__MODEL"] == "Qwen/Qwen3-Embedding-4B"
        assert env["EVEROS_EMBEDDING__API_KEY"] == "sk-di"

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
        self._raven_config(
            tmp_path, monkeypatch, {"model": "ravens/model", "baseUrl": "https://ravens/v1", "apiKey": "sk-raven"}
        )

        assert ue.everos_has_own_embedding() is True
        assert ue.host_embedding_env() == {}
        endpoint = SimpleNamespace(model="ravens/model", base_url="https://ravens/v1", api_key="sk-raven")
        assert ue.configure_embedding_env(endpoint) is False
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
        self._raven_config(
            tmp_path, monkeypatch, {"model": "ravens/model", "baseUrl": "https://ravens/v1", "apiKey": "sk-raven"}
        )

        assert ue.everos_has_own_embedding() is False
        assert ue.host_embedding_env()["EVEROS_EMBEDDING__MODEL"] == "ravens/model"

    def test_the_toml_keeps_precedence_when_it_has_one(
        self, everos_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who wrote [embedding] chose that endpoint for memory;
        raven's block fills a gap rather than taking over."""
        ue.set_everos_section("embedding", {"model": "bge-m3", "api_key": "k-own"})
        self._raven_config(
            tmp_path,
            monkeypatch,
            {"model": "Qwen/Qwen3-Embedding-4B", "baseUrl": "https://api.siliconflow.cn/v1", "apiKey": "sk-sf"},
        )

        assert ue.everos_role_configured("embedding") is True
        assert ue.everos_section("embedding")["model"] == "bge-m3"
        for name in ("MODEL", "BASE_URL", "API_KEY", "DIMENSIONS"):
            monkeypatch.delenv(f"EVEROS_EMBEDDING__{name}", raising=False)
        endpoint = SimpleNamespace(
            model="Qwen/Qwen3-Embedding-4B", base_url="https://api.siliconflow.cn/v1", api_key="sk-sf"
        )
        assert ue.configure_embedding_env(endpoint) is False, "the toml's own endpoint must not be overridden"
        assert "EVEROS_EMBEDDING__MODEL" not in os.environ
