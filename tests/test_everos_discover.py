"""Discovery of EverOS roots: what exists, who owns it, and what is serving it.

The states are kept apart on purpose because they fail independently, and the one
that used to be invisible -- data already served, but not at the address it
declares -- is the state that produced a 30s startup timeout blaming a missing
install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.plugin.memory.everos import _discover


def _write_root(root: Path, *, api: tuple[str, int] | None = ("127.0.0.1", 18791), key: str = "k") -> None:
    root.mkdir(parents=True, exist_ok=True)
    body = f'[llm]\nmodel = "m"\napi_key = "{key}"\n'
    if api is not None:
        body += f'[api]\nhost = "{api[0]}"\nport = {api[1]}\n'
    (root / "everos.toml").write_text(body, encoding="utf-8")


@pytest.fixture
def _quiet_probes(monkeypatch: pytest.MonkeyPatch):
    """Default every observation to "nothing there"; tests opt into each fact."""
    monkeypatch.setattr(_discover, "_probe_health", lambda _u: False)
    monkeypatch.setattr(_discover, "ome_lock_held", lambda _r: False)
    return monkeypatch


class TestDescribeOneRoot:
    def test_the_address_is_read_from_the_root(self, tmp_path: Path, _quiet_probes) -> None:
        root = tmp_path / "everos"
        _write_root(root)

        state = _discover._describe(root)

        assert state.declared_url == "http://127.0.0.1:18791"
        assert state.configured is True
        assert state.exists is True

    def test_a_root_without_an_api_section_declares_nothing(self, tmp_path: Path, _quiet_probes) -> None:
        """No declared address means there is nothing to probe -- not that a
        server is down."""
        root = tmp_path / "everos"
        _write_root(root, api=None)

        state = _discover._describe(root)

        assert state.declared_url is None
        assert state.alive is False

    def test_an_empty_api_key_reads_as_unconfigured(self, tmp_path: Path, _quiet_probes) -> None:
        """The shipped template carries a model with no key; that is half-built,
        and no server started from it can finish starting."""
        root = tmp_path / "everos"
        _write_root(root, key="")

        assert _discover._describe(root).configured is False

    def test_an_absent_root_is_described_without_raising(self, tmp_path: Path, _quiet_probes) -> None:
        state = _discover._describe(tmp_path / "nope")

        assert state.exists is False
        assert state.configured is False
        assert state.declared_url is None

    def test_unparseable_toml_is_treated_as_empty(self, tmp_path: Path, _quiet_probes) -> None:
        root = tmp_path / "everos"
        root.mkdir()
        (root / "everos.toml").write_text("this is not toml {{{", encoding="utf-8")

        assert _discover._describe(root).configured is False

    def test_locked_but_silent_is_its_own_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Data served somewhere other than where it says -- the state that
        cannot be fixed by starting anything, since one directory admits one
        engine."""
        root = tmp_path / "everos"
        _write_root(root)
        monkeypatch.setattr(_discover, "_probe_health", lambda _u: False)
        monkeypatch.setattr(_discover, "ome_lock_held", lambda _r: True)

        state = _discover._describe(root)

        assert state.busy_elsewhere is True
        assert state.serving is False

    def test_serving_means_reachable_where_it_declares(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "everos"
        _write_root(root)
        monkeypatch.setattr(_discover, "_probe_health", lambda _u: True)
        monkeypatch.setattr(_discover, "ome_lock_held", lambda _r: True)

        state = _discover._describe(root)

        assert state.serving is True
        assert state.busy_elsewhere is False


class TestDiscoveryOrder:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from raven.config import update_everos as ue

        self.default = tmp_path / "data" / "everos"
        self.legacy = tmp_path / "home" / ".everos" / "raven"
        monkeypatch.setattr(ue, "default_everos_root", lambda: self.default)
        monkeypatch.setattr(ue, "legacy_everos_root", lambda: self.legacy)
        # Discovery only offers the legacy root to the installation that could
        # have created it; these cases are about ordering, so say which we are.
        monkeypatch.setattr(ue, "_is_default_installation", lambda: True)
        monkeypatch.setattr(ue, "_recorded_slice", dict)
        monkeypatch.setattr(_discover, "_probe_health", lambda _u: False)
        monkeypatch.setattr(_discover, "ome_lock_held", lambda _r: False)
        return monkeypatch

    def test_the_recorded_root_comes_first(self, tmp_path: Path, _isolate) -> None:
        """Switching roots behind the user's back would change which memories
        raven has, so a recorded root wins even over a healthier candidate."""
        from raven.config import update_everos as ue

        recorded = tmp_path / "recorded"
        _write_root(recorded)
        _write_root(self.default)
        _isolate.setattr(ue, "_recorded_slice", lambda: {"root": str(recorded), "owned": True})

        assert _discover.discover()[0].root == recorded
        assert _discover.pick(_discover.discover()).root == recorded

    def test_the_legacy_root_is_found_when_the_new_default_is_empty(self, _isolate) -> None:
        _write_root(self.legacy)

        picked = _discover.pick(_discover.discover())

        assert picked is not None
        assert picked.root == self.legacy

    def test_nothing_configured_picks_nothing(self, _isolate) -> None:
        assert _discover.pick(_discover.discover()) is None

    def test_a_half_built_root_is_not_picked(self, _isolate) -> None:
        _write_root(self.default, key="")

        assert _discover.pick(_discover.discover()) is None

    def test_the_users_own_root_is_never_offered(self, _isolate) -> None:
        """Discovery scans raven's own roots and nothing else.

        Offering the user's ``~/.everos`` made the wizard propose a decision the
        user had not come to make, and the answer it invited -- "reuse it" --
        was one raven could only honour by never touching the thing it had just
        adopted. Choosing to point raven at an EverOS of one's own is now an
        explicit turn in the wizard, with an address typed by the person who
        knows it.
        """
        bare = self.legacy.parent
        _write_root(bare)

        assert not [s for s in _discover.discover() if s.root == bare]

        _write_root(self.default)
        assert not [s for s in _discover.discover() if s.root == bare]

    def test_every_discovered_root_is_ravens_own(self, _isolate) -> None:
        _write_root(self.default)
        _write_root(self.legacy)

        roots = {s.root for s in _discover.discover()}
        assert roots <= {self.default, self.legacy}

    def test_discovery_says_nothing_about_ownership(self, tmp_path: Path, _isolate) -> None:
        """A recorded root is a candidate whatever the config recorded about it.

        Ownership used to be carried on the candidate, so a root recorded as the
        user's was offered as read-only and one of raven's was adopted before any
        question appeared. It is now decided by the lane the user picks in the
        wizard, and this module has no field for it.
        """
        from raven.config import update_everos as ue

        recorded = tmp_path / "recorded"
        _write_root(recorded)
        _isolate.setattr(ue, "_recorded_slice", lambda: {"root": str(recorded), "owned": False})

        state = _discover.discover()[0]

        assert state.root == recorded
        assert not hasattr(state, "owned")

    def test_no_duplicate_candidates(self, _isolate) -> None:
        from raven.config import update_everos as ue

        _write_root(self.default)
        _isolate.setattr(ue, "_recorded_slice", lambda: {"root": str(self.default), "owned": True})

        roots = [s.root for s in _discover.discover()]
        assert len(roots) == len(set(roots))


def test_a_new_root_does_not_declare_the_everos_default_port() -> None:
    """8000 is among the most commonly occupied ports on a developer machine; it
    only ever appeared in raven's roots because raven overrode it on the command
    line and never wrote the file."""
    assert "8000" not in _discover.default_new_root_url()
