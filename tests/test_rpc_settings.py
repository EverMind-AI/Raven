"""``settings.set`` — the GUI dialog's whitelisted hot-writable keys."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from raven import home as raven_home
from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods import console as rpc_console

pytestmark = pytest.mark.asyncio


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
    import raven_everos.config as _ue

    # Provenance is process-global; a case that bound the host endpoint would
    # otherwise tell the next one that raven had already written those.
    _ue._BOUND_HERE.clear()


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    # And the process-wide pointer: writers that `from ... import
    # get_config_path` hold the original function object, which the patch above
    # does not reach. Reset per test by the suite's home fixture.
    raven_home.set_config_path(path)
    return path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


async def test_bool_key_writes_through(cfg):
    r = await rpc_console.settings_set({"key": "channels.sendProgress", "value": True})
    assert r["applied"] is True
    assert _read(cfg)["channels"]["sendProgress"] is True


async def test_bool_key_rejects_non_bool(cfg):
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "channels.sendProgress", "value": "yes"})


@pytest.mark.parametrize(
    "key,value",
    [
        ("tools.restrictToWorkspace", False),
        ("tools.sandbox.backend", "none"),
        ("tools.web.proxy", "http://attacker.example:8080"),
    ],
)
async def test_the_containment_controls_are_not_writable_here(cfg, key, value):
    """These three decide what a caller who reaches this endpoint can then do.

    ``settings.set`` is reachable from any RPC client with no confirmation step,
    so the first two -- the workspace sandbox and the exec sandbox -- must not be
    switchable through it, and the third would route every WebSearch and
    WebFetch, API keys and all, through a host of the caller's choosing. Editing
    the config file is the friction, and it is deliberate.
    """
    with pytest.raises(ConfigValidationError, match="not writable"):
        await rpc_console.settings_set({"key": key, "value": value})
    assert key.split(".")[-1] not in str(_read(cfg)), "nothing may have been written"


async def test_int_key_bounds(cfg):
    r = await rpc_console.settings_set({"key": "tools.exec.timeout", "value": 120})
    assert r["applied"] is True
    assert _read(cfg)["tools"]["exec"]["timeout"] == 120
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "tools.exec.timeout", "value": 0})
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "tools.exec.timeout", "value": True})


async def test_enum_key(cfg):
    r = await rpc_console.settings_set({"key": "agents.defaults.reasoningEffort", "value": "high"})
    assert r["applied"] is True
    assert _read(cfg)["agents"]["defaults"]["reasoningEffort"] == "high"
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "agents.defaults.reasoningEffort", "value": "extreme"})


async def test_memory_backend_not_writable(cfg):
    """The backend is not a user choice - long-term memory is EverOS."""
    with pytest.raises(ConfigValidationError, match="not writable"):
        await rpc_console.settings_set({"key": "memory.backend", "value": None})


@pytest.fixture()
def everos_toml(tmp_path, monkeypatch):
    path = tmp_path / "everos.toml"
    monkeypatch.setattr("raven_everos.config.everos_root", lambda: path.parent)
    # Upstream gates the write primitives on root ownership; a tmp root is not
    # one raven created, so declare it owned for the test.
    monkeypatch.setattr("raven_everos.config.everos_owned", lambda: True)
    return path


@pytest.fixture()
def everos_cfg(everos_toml, tmp_path):
    """raven's own config, which is where the role pins live now.

    Returns a builder so a case states only what it cares about. The
    process-wide pointer rather than a patched name: the pin is read back
    through raven's loader, which binds ``get_config_path`` itself.
    """
    import json

    def build(*, providers=None, embedding=None, roles=None):
        cfg = tmp_path / "config.json"
        raw = {"providers": providers if providers is not None else {}}
        if embedding is not None:
            raw["embedding"] = embedding
        if roles:
            raw["plugins"] = {"config": {"everos-memory": dict(roles)}}
        cfg.write_text(json.dumps(raw), encoding="utf-8")
        raven_home.set_config_path(cfg)
        return cfg

    yield build
    raven_home.set_config_path(None)


async def test_everos_get_never_carries_a_key(everos_cfg):
    """No key was ever on this wire and none is now -- but the shape changed, so
    the case is worth keeping: the card reads a pin, and the credential it
    reports on lives with the provider."""
    everos_cfg(
        embedding={"model": "openai/text-embedding-3-large", "provider": "openai"},
        providers={"openai": {"apiKey": "sk-secret", "apiBase": "https://o.test/v1"}},
    )
    r = await rpc_console.settings_everos({})
    emb = r["sections"]["embedding"]
    assert emb["model"] == "openai/text-embedding-3-large"
    assert emb["api_key_set"] is True
    assert "sk-secret" not in json.dumps(r)
    assert r["sections"]["llm"]["model"] == ""


async def test_everos_set_replaces_the_pin_rather_than_merging_fields(everos_cfg):
    """A role is a pair, so a save states both halves. The old shape merged
    loose fields, which let a section end up holding a model from one save and
    an address from another."""
    import json as _json

    cfg = everos_cfg(providers={"deepinfra": {"apiKey": "k", "apiBase": "https://d/v1"}})
    await rpc_console.settings_everos_set({"section": "rerank", "model": "m1", "provider": "deepinfra"})
    await rpc_console.settings_everos_set({"section": "rerank", "model": "m2", "provider": "deepinfra"})
    slice_ = _json.loads(cfg.read_text(encoding="utf-8"))["plugins"]["config"]["everos-memory"]
    # `protocol` rides along on rerank and is blanked here on purpose: deepinfra
    # is a vendor the table can answer for, so a shape left over from a
    # self-hosted box must not survive the switch.
    assert slice_["rerank"] == {"model": "m2", "provider": "deepinfra", "protocol": ""}


async def test_a_pin_naming_a_provider_with_no_key_is_refused(everos_cfg):
    """A role is only as real as the credential behind it.

    Self-hosted slots made this reachable: `ollama_chat` counts as configured
    for the chat model with nothing but an address, so the picker will offer it
    here too. Accepted, the save would look fine and the role would read back
    "not configured" -- indistinguishable from the save being lost.
    """
    everos_cfg(providers={"ollama_chat": {"apiBase": "http://localhost:11434/v1"}})

    with pytest.raises(ConfigValidationError, match="no usable credential"):
        await rpc_console.settings_everos_set({"section": "rerank", "model": "bge-reranker", "provider": "ollama_chat"})


async def test_a_pin_naming_a_provider_that_does_not_exist_is_refused(everos_cfg):
    everos_cfg(providers={})

    with pytest.raises(ConfigValidationError, match="unknown provider"):
        await rpc_console.settings_everos_set({"section": "rerank", "model": "m", "provider": "not-a-vendor"})


async def test_a_self_hosted_endpoint_records_the_shape_it_serves(everos_cfg):
    """The one thing a pair cannot answer for.

    A curated vendor's request shape comes from the vendor table. Nothing knows
    what somebody's own box runs, so the operator says -- and it has to reach
    the spawn, or EverOS falls back to its default shape and posts to the wrong
    path. That fallback is the defect this whole change started from.
    """
    everos_cfg(providers={"custom": {"apiKey": "k", "apiBase": "http://box.lan:8000/v1"}})

    await rpc_console.settings_everos_set(
        {"section": "rerank", "model": "bge-reranker", "provider": "custom", "protocol": "vllm"}
    )

    from raven_everos.config import everos_env

    assert everos_env()["EVEROS_RERANK__PROVIDER"] == "vllm"


async def test_reranking_on_a_vendor_the_table_cannot_name_is_refused(everos_cfg):
    """Told nothing, EverOS falls back to its own default shape and posts a
    deepinfra-shaped request wherever it is pointed. That silence is the defect
    this change started from, so the save refuses and names the choices instead.
    """
    everos_cfg(providers={"custom": {"apiKey": "k", "apiBase": "http://box.lan:8000/v1"}})

    with pytest.raises(ConfigValidationError, match="needs its request shape named"):
        await rpc_console.settings_everos_set({"section": "rerank", "model": "bge-reranker", "provider": "custom"})


async def test_a_self_hosted_rerank_stays_editable_after_it_is_set(everos_cfg):
    """The shape is asked once, not on every edit.

    The guard read only the vendor table, so a rerank role the wizard had
    configured against somebody's own box was refused from the page forever --
    not even a model-only change went through.
    """
    everos_cfg(providers={"custom": {"apiKey": "k", "apiBase": "http://box.lan:8000/v1"}})
    await rpc_console.settings_everos_set(
        {"section": "rerank", "model": "bge-reranker", "provider": "custom", "protocol": "vllm"}
    )

    await rpc_console.settings_everos_set({"section": "rerank", "model": "bge-reranker-v2", "provider": "custom"})

    from raven_everos.config import everos_env, role_pin

    assert role_pin("rerank") == ("bge-reranker-v2", "custom")
    assert everos_env()["EVEROS_RERANK__PROVIDER"] == "vllm", "the shape was lost on the second save"


async def test_an_unknown_rerank_shape_is_refused(everos_cfg):
    everos_cfg(providers={"custom": {"apiKey": "k", "apiBase": "http://box.lan:8000/v1"}})

    with pytest.raises(ConfigValidationError, match="unknown rerank protocol"):
        await rpc_console.settings_everos_set(
            {"section": "rerank", "model": "m", "provider": "custom", "protocol": "cohere"}
        )


async def test_default_permission_mode_is_a_settings_key(cfg):
    r = await rpc_console.settings_set({"key": "permissions.mode", "value": "smart"})
    assert r["applied"] is True
    assert _read(cfg)["permissions"]["mode"] == "smart"
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "permissions.mode", "value": "yolo"})


async def test_secret_key_writes_string(cfg):
    r = await rpc_console.settings_set({"key": "tools.web.search.apiKey", "value": "sk-x"})
    assert r["applied"] is True
    assert _read(cfg)["tools"]["web"]["search"]["apiKey"] == "sk-x"


async def test_unknown_key_rejected(cfg):
    with pytest.raises(ConfigValidationError, match="not writable"):
        await rpc_console.settings_set({"key": "providers.openai.apiKey", "value": "x"})


async def test_previous_value_returned(cfg):
    await rpc_console.settings_set({"key": "channels.sendProgress", "value": False})
    r = await rpc_console.settings_set({"key": "channels.sendProgress", "value": True})
    assert r["previous"] is False


class TestTheSaveRestartsTheService:
    """A pin is inert until the process that reads it is replaced.

    EverOS builds its model clients at startup, so before this a save wrote the
    new model to disk and the running server kept the old one -- with nothing on
    screen saying the change had not taken.
    """

    @staticmethod
    def _loop(frames: list) -> Any:
        class _Loop:
            def _emit_mcp_event(self, method: str, params: dict) -> None:
                frames.append((method, params))

        return _Loop()

    @staticmethod
    def _seeded(everos_cfg):
        return everos_cfg(providers={"deepinfra": {"apiKey": "k", "apiBase": "https://d/v1"}})

    async def test_no_loop_means_a_synchronous_refusal(self, everos_cfg) -> None:
        """The outcome only reaches the page as a pushed frame. Started with
        nowhere to push, a restart would report to nobody while the caller read
        `applied` as "this configuration is running"."""
        self._seeded(everos_cfg)

        out = await rpc_console.settings_everos_set({"section": "llm", "model": "m", "provider": "deepinfra"})

        assert out["applied"] is False
        assert "next start" in (out["warning"] or "")

    async def test_success_pushes_ok_true_so_the_banner_can_clear(
        self, everos_cfg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`install.ts` clears the standing banner on a success frame and on
        nothing else, so a chain that reported only failures would leave one
        failed restart on screen for the rest of the session."""
        self._seeded(everos_cfg)
        frames: list = []

        async def _chain(root, base_url, *, on_result):
            on_result(True, None)

        monkeypatch.setattr("raven_everos.server.restart_for_config_change", _chain)

        out = await rpc_console.settings_everos_set(
            {"section": "llm", "model": "m", "provider": "deepinfra"},
            agent_loop_factory=lambda: self._loop(frames),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert out["applied"] is True
        assert frames[-1] == ("memory.health", {"ok": True, "error": None})

    async def test_a_session_that_queued_a_restart_hears_how_it_went(
        self, everos_cfg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second session saving mid-restart is waiting on that restart too.

        The outcome went only to whichever session happened to start the run, so
        the other read `applied` as success and never got a frame -- its banner
        kept whatever was there, including through a failure.
        """
        self._seeded(everos_cfg)
        a: list = []
        b: list = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def _chain(root, base_url, *, on_result):
            started.set()
            await release.wait()
            on_result(False, "boom")

        monkeypatch.setattr("raven_everos.server.restart_for_config_change", _chain)

        pin = {"section": "llm", "model": "m", "provider": "deepinfra"}
        await rpc_console.settings_everos_set(dict(pin), agent_loop_factory=lambda: self._loop(a))
        await started.wait()
        await rpc_console.settings_everos_set({**pin, "model": "m2"}, agent_loop_factory=lambda: self._loop(b))
        release.set()
        for _ in range(12):
            await asyncio.sleep(0)

        assert a, "the session that started the run heard nothing"
        assert b, "the session that queued the second run heard nothing"

    async def test_one_dead_session_does_not_silence_the_others(
        self, everos_cfg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sessions come and go while a restart runs. A tab closed mid-flight
        raises on its own emit, and the loop reporting to it must not take the
        outcome away from everyone still watching."""
        self._seeded(everos_cfg)
        alive: list = []

        class _Dead:
            def _emit_mcp_event(self, method: str, params: dict) -> None:
                raise RuntimeError("that socket is gone")

        async def _chain(root, base_url, *, on_result):
            on_result(True, None)

        monkeypatch.setattr("raven_everos.server.restart_for_config_change", _chain)
        pin = {"section": "llm", "model": "m", "provider": "deepinfra"}

        await rpc_console.settings_everos_set(dict(pin), agent_loop_factory=lambda: _Dead())
        for _ in range(8):
            await asyncio.sleep(0)
        await rpc_console.settings_everos_set({**pin, "model": "m2"}, agent_loop_factory=lambda: self._loop(alive))
        for _ in range(8):
            await asyncio.sleep(0)

        assert alive, "the live session heard nothing after a dead one raised"

    async def test_a_chain_that_raises_reports_the_failure(self, everos_cfg, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anything the chain did not catch still has to reach the banner --
        dropped, the page keeps whatever the last run put there."""
        self._seeded(everos_cfg)
        frames: list = []

        async def _chain(root, base_url, *, on_result):
            raise RuntimeError("the port moved under us")

        monkeypatch.setattr("raven_everos.server.restart_for_config_change", _chain)

        await rpc_console.settings_everos_set(
            {"section": "llm", "model": "m", "provider": "deepinfra"},
            agent_loop_factory=lambda: self._loop(frames),
        )
        for _ in range(10):
            await asyncio.sleep(0)

        assert frames, "a chain that raised reported nothing"
        assert frames[-1][1]["ok"] is False
        assert "port moved" in (frames[-1][1]["error"] or "")

    async def test_a_cancelled_restart_still_reports(self, everos_cfg, monkeypatch: pytest.MonkeyPatch) -> None:
        """A gateway shutting down mid-restart. Silent, the page keeps whatever
        the last run put on its banner, and the next session inherits a claim
        nobody can check."""
        self._seeded(everos_cfg)
        frames: list = []

        async def _chain(root, base_url, *, on_result):
            raise asyncio.CancelledError

        monkeypatch.setattr("raven_everos.server.restart_for_config_change", _chain)

        await rpc_console.settings_everos_set(
            {"section": "llm", "model": "m", "provider": "deepinfra"},
            agent_loop_factory=lambda: self._loop(frames),
        )
        for _ in range(10):
            await asyncio.sleep(0)

        assert frames, "a cancelled restart reported nothing"
        assert frames[-1][1]["ok"] is False
        assert "interrupted" in (frames[-1][1]["error"] or "")

    async def test_two_saves_run_one_after_the_other_and_the_last_one_wins(
        self, everos_cfg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Overlapping chains race for the same port, and the loser reports a
        startup failure for a configuration that is in force. One extra run is
        queued rather than one per save: what matters is ending at the last
        thing written, not replaying every step to it."""
        self._seeded(everos_cfg)
        frames: list = []
        live = {"now": 0, "max": 0}
        models: list = []

        async def _chain(root, base_url, *, on_result):
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            from raven_everos.config import role_pin

            models.append((role_pin("llm") or ("", ""))[0])
            await asyncio.sleep(0)
            live["now"] -= 1
            on_result(True, None)

        monkeypatch.setattr("raven_everos.server.restart_for_config_change", _chain)
        factory = lambda: self._loop(frames)  # noqa: E731 - one expression, named for the kwarg

        await rpc_console.settings_everos_set(
            {"section": "llm", "model": "first", "provider": "deepinfra"}, agent_loop_factory=factory
        )
        await rpc_console.settings_everos_set(
            {"section": "llm", "model": "second", "provider": "deepinfra"}, agent_loop_factory=factory
        )
        for _ in range(12):
            await asyncio.sleep(0)

        assert live["max"] == 1, "two restart chains overlapped"
        assert models[-1] == "second", "the last run did not read the last configuration"


async def test_an_unknown_role_is_refused_by_name(everos_cfg):
    everos_cfg(providers={})

    with pytest.raises(ConfigValidationError, match="unknown everos role"):
        await rpc_console.settings_everos_set({"section": "not-a-role", "model": "m", "provider": "p"})


async def test_an_absurdly_long_pair_is_refused(everos_cfg):
    """Length is checked before anything is written, because both halves end up
    in a config file somebody has to be able to read afterwards."""
    everos_cfg(providers={"deepinfra": {"apiKey": "k", "apiBase": "https://d/v1"}})

    with pytest.raises(ConfigValidationError, match="under 500 characters"):
        await rpc_console.settings_everos_set({"section": "llm", "model": "m" * 501, "provider": "deepinfra"})


async def test_a_pair_that_cannot_embed_is_refused_readably(everos_cfg):
    """`set_embedding_endpoint` refuses a chat model in the embedding slot. Left
    as its own exception it reaches the dispatcher, which renders any non-RpcError
    as internal_error plus a traceback -- and the sentence naming the model, the
    only useful thing it carries, never reaches the page."""
    everos_cfg(providers={"openai": {"apiKey": "sk-1"}})

    with pytest.raises(ConfigValidationError, match="not an embedding model"):
        await rpc_console.settings_everos_set({"section": "embedding", "model": "gpt-4o", "provider": "openai"})


async def test_a_write_to_a_root_the_user_manages_is_refused_readably(everos_cfg, monkeypatch):
    """Same shape, different exception: the refusal carries the path of the root
    somebody else manages, which is the one thing the reader needs."""
    everos_cfg(providers={"deepinfra": {"apiKey": "k", "apiBase": "https://d/v1"}})
    # Past the fixture's own patch, which declares the throwaway root owned so
    # every other case can write to it.
    monkeypatch.setattr("raven_everos.config.everos_owned", lambda: False)
    monkeypatch.setattr("raven_everos.config.everos_root", lambda: Path("/somewhere/theirs"))

    with pytest.raises(ConfigValidationError, match="/somewhere/theirs"):
        await rpc_console.settings_everos_set({"section": "llm", "model": "m", "provider": "deepinfra"})


async def test_the_page_may_clear_embedding_but_not_llm(everos_cfg):
    """Clearing llm turns long-term memory off outright, so the page's clear
    button refuses it. Embedding is optional -- recall falls back to keyword
    search -- and a click on its clear button is a person asking for exactly
    that, so it goes; the wizard's skip still cannot take it (see
    test_everos_config)."""
    from raven_everos.config import role_pin

    everos_cfg(providers={}, embedding={"model": "bge-m3", "provider": "deepinfra"})
    assert role_pin("embedding") is not None

    with pytest.raises(ConfigValidationError, match="cannot be cleared"):
        await rpc_console.settings_everos_set({"section": "llm", "clear": True})

    await rpc_console.settings_everos_set({"section": "embedding", "clear": True})
    assert role_pin("embedding") is None
    assert (await rpc_console.settings_everos({}))["required"] == ["llm"]


class TestTheRoleCardReadsAndWritesRavensOwnRecord:
    """The four role slots read a pin raven stores and write one back.

    What used to be here was the embedding card reconciling two homes -- raven's
    block and an everos.toml override -- because the endpoint had moved and the
    file still won. All four roles live in raven's config now and the file is
    not read for them at all, so most of that reconciliation is structurally
    gone rather than merely untested: the wire carries no `base_url` to refuse,
    and no `api_key` to echo back over a working one.

    What survives is every case where something outside raven still outranks it.
    """

    async def test_the_card_shows_the_pin_raven_holds(self, everos_cfg) -> None:
        cfg = everos_cfg(
            embedding={"model": "Qwen/Qwen3-Embedding-4B", "provider": "deepinfra"},
            providers={"deepinfra": {"apiKey": "sk-1", "apiBase": "https://e.test/v1"}},
        )
        del cfg
        card = (await rpc_console.settings_everos({}))["sections"]["embedding"]

        # The pair as stored: the model spelled the way it was chosen, not the
        # way the vendor is addressed, or saving the row back would store a
        # spelling nobody picked.
        assert card["model"] == "Qwen/Qwen3-Embedding-4B"
        assert card["provider"] == "deepinfra"
        assert card["api_key_set"] is True

    async def test_a_provider_with_no_key_reads_as_not_set(self, everos_cfg) -> None:
        """One rule for the card and the gate: a pin whose vendor cannot serve
        is not configuration, however complete it looks."""
        everos_cfg(
            embedding={"model": "m", "provider": "deepinfra"},
            providers={"deepinfra": {"apiBase": "https://e.test/v1"}},
        )
        card = (await rpc_console.settings_everos({}))["sections"]["embedding"]
        assert card["model"] == "m"
        assert card["api_key_set"] is False

    async def test_saving_writes_where_the_card_reads(self, everos_cfg) -> None:
        import json

        cfg = everos_cfg(providers={"deepinfra": {"apiKey": "sk-1", "apiBase": "https://e.test/v1"}})
        await rpc_console.settings_everos_set({"section": "embedding", "model": "BAAI/bge-m3", "provider": "deepinfra"})
        raw = json.loads(cfg.read_text(encoding="utf-8"))
        assert raw["embedding"] == {"model": "BAAI/bge-m3", "provider": "deepinfra"}
        card = (await rpc_console.settings_everos({}))["sections"]["embedding"]
        assert card["model"] == "BAAI/bge-m3"

    async def test_the_other_three_write_the_plugins_own_slice(self, everos_cfg) -> None:
        import json

        cfg = everos_cfg(providers={"deepseek": {"apiKey": "sk-1", "apiBase": "https://d.test/v1"}})
        await rpc_console.settings_everos_set({"section": "llm", "model": "deepseek-chat", "provider": "deepseek"})
        raw = json.loads(cfg.read_text(encoding="utf-8"))
        assert raw["plugins"]["config"]["everos-memory"]["llm"] == {
            "model": "deepseek-chat",
            "provider": "deepseek",
        }
        assert "llm" not in raw.get("embedding", {})

    async def test_no_credential_reaches_the_config(self, everos_cfg) -> None:
        """The pair is stored, not the key. What used to be `borrow_from` --
        copy this lender's key across -- is just the provider's name now, so
        there is nothing to copy and nothing to leak into a second file."""
        cfg = everos_cfg(providers={"deepseek": {"apiKey": "sk-do-not-copy", "apiBase": "https://d.test/v1"}})
        await rpc_console.settings_everos_set({"section": "llm", "model": "deepseek-chat", "provider": "deepseek"})
        assert "sk-do-not-copy" not in cfg.read_text(encoding="utf-8").replace('"apiKey": "sk-do-not-copy"', "")

    async def test_half_a_pin_is_refused_from_either_end(self, everos_cfg) -> None:
        everos_cfg(providers={"deepseek": {"apiKey": "sk-1", "apiBase": "https://d.test/v1"}})
        for params in (
            {"section": "llm", "model": "m"},
            {"section": "llm", "provider": "deepseek"},
        ):
            with pytest.raises(ConfigValidationError, match="both model and provider"):
                await rpc_console.settings_everos_set(params)

    @pytest.mark.parametrize("section", ["llm", "embedding", "rerank", "multimodal"])
    async def test_a_save_the_environment_would_outrank_is_refused(self, everos_cfg, monkeypatch, section) -> None:
        """An operator who exports the endpoint outranks raven, and a save that
        would be written and then ignored is worse than a refusal that names the
        variables.

        All four roles, because `everos_env` skips an env-managed role whole.
        The guard read `embedding_is_env_managed` alone until this case ran the
        other three: they were accepted, restarted the service, and the exported
        value went on winning.

        Set through the real environment rather than a stub, so what is being
        asked is the predicate the spawn uses.
        """
        from raven_everos import config as cf

        everos_cfg(providers={"deepinfra": {"apiKey": "sk-1", "apiBase": "https://e.test/v1"}})
        prefix = f"EVEROS_{section.upper()}__"
        for name, value in (("MODEL", "theirs"), ("BASE_URL", "https://theirs/v1"), ("API_KEY", "sk-theirs")):
            monkeypatch.setenv(f"{prefix}{name}", value)
        monkeypatch.setattr(cf, "_BOUND_HERE", set())

        with pytest.raises(ConfigValidationError, match=f"{prefix}MODEL"):
            await rpc_console.settings_everos_set({"section": section, "model": "m", "provider": "deepinfra"})

    async def test_an_exported_role_reads_as_managed_elsewhere(self, everos_cfg, monkeypatch) -> None:
        """raven cannot edit a shell, so the slot says so rather than offering
        an edit that would not take."""
        from raven_everos import config as cf

        everos_cfg(providers={})
        for key, value in (
            ("EVEROS_RERANK__MODEL", "theirs"),
            ("EVEROS_RERANK__BASE_URL", "https://theirs/v1"),
            ("EVEROS_RERANK__API_KEY", "sk-theirs"),
        ):
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(cf, "_BOUND_HERE", set())

        card = (await rpc_console.settings_everos({}))["sections"]["rerank"]
        assert card["env_managed"] is True
        assert card["api_key_set"] is True

    async def test_the_response_says_who_owns_the_root(self, everos_cfg, monkeypatch) -> None:
        """The page disables the slots on a root raven does not manage, rather
        than letting a person discover the refusal by hitting it."""
        everos_cfg(providers={})
        monkeypatch.setattr("raven_everos.config.everos_owned", lambda: False)
        assert (await rpc_console.settings_everos({}))["owned"] is False

    async def test_the_response_says_which_vendors_can_serve_which_role(self, everos_cfg) -> None:
        """So the rerank slot stops offering vendors that cannot rerank."""
        everos_cfg(providers={})
        supports = (await rpc_console.settings_everos({}))["supports"]
        assert "rerank" in supports["deepinfra"]
        assert "rerank" not in supports["deepseek"]

    async def test_the_response_no_longer_carries_a_request_shape_as_provider(self, everos_cfg) -> None:
        """`provider` named two different things across this change: a vendor
        here, and EverOS's rerank client implementation on the wire. The
        protocol is derived from the vendor table now and is nobody's choice."""
        everos_cfg(providers={})
        sections = json.dumps((await rpc_console.settings_everos({}))["sections"])
        # `vllm` is the one value that can only be a protocol: it is not a vendor
        # name in the table, unlike `dashscope`, which is both and legitimately
        # appears under `supports`.
        assert "vllm" not in sections


class TestTheCronTimezoneControl:
    """The settings page's timezone box, from the wire spelling to the loader.

    ``settings.set`` is handed the key as the page sends it -- the JSON
    spelling ``cron.defaultTimezone`` -- while ``update_cron_config`` validates
    against ``CronConfig.model_fields``, which holds Python field names. This
    branch is the only place in the endpoint where the two conventions meet, so
    a missing conversion here is a save that raises instead of writing.
    """

    async def test_the_timezone_round_trips_through_the_writer_and_the_loader(self, cfg):
        from raven.config.loader import load_config

        r = await rpc_console.settings_set({"key": "cron.defaultTimezone", "value": "Asia/Tokyo"})

        assert r["applied"] is True
        assert _read(cfg)["cron"]["defaultTimezone"] == "Asia/Tokyo"
        # Landing in the file is half of it: the writer spells the key one way
        # and the loader has to read that same spelling back. Deliberately not a
        # claim about scheduling -- ``CronConfig.default_timezone`` has exactly
        # one consumer today, the ``raven cron config get`` display path, and
        # ``_compute_next_run`` falls back to the machine's local zone rather
        # than to this field.
        assert load_config(cfg).cron.default_timezone == "Asia/Tokyo"

    async def test_the_previous_timezone_comes_back(self, cfg):
        await rpc_console.settings_set({"key": "cron.defaultTimezone", "value": "Asia/Tokyo"})
        r = await rpc_console.settings_set({"key": "cron.defaultTimezone", "value": "UTC"})
        assert r["previous"] == "Asia/Tokyo"

    async def test_an_unknown_zone_is_refused_before_anything_is_written(self, cfg):
        with pytest.raises(ConfigValidationError, match="unknown timezone"):
            await rpc_console.settings_set({"key": "cron.defaultTimezone", "value": "Mars/Olympus"})
        assert "cron" not in _read(cfg)

    async def test_an_empty_timezone_is_refused(self, cfg):
        with pytest.raises(ConfigValidationError):
            await rpc_console.settings_set({"key": "cron.defaultTimezone", "value": ""})


async def test_the_retired_forward_channels_key_is_not_writable(cfg):
    """``forward_channels`` left ``CronConfig`` when delivery became fire-at-origin.

    The loader strips both spellings out of a config file on the way in, so no
    field stands behind this key any more. It has to be refused the way every
    other unwritable key is -- typed, and naming itself -- rather than reaching
    a writer whose only possible answer is to raise.
    """
    with pytest.raises(ConfigValidationError, match="not writable"):
        await rpc_console.settings_set({"key": "cron.forwardChannels", "value": ["telegram"]})
    assert "cron" not in _read(cfg)


async def test_extension_pin_writes_roundtrip_through_raven_loader(cfg):
    from raven.config.raven import load_raven_config
    from raven.config.update_providers import set_provider_fields

    # The embedding pin is checked against the provider before it is stored, so
    # the provider has to be one this config actually holds a key for.
    set_provider_fields("openai", {"api_key": "sk-openai"})

    await rpc_console.settings_set({"key": "translate.model", "value": "openai/gpt-5-mini"})
    await rpc_console.settings_set({"key": "translate.provider", "value": "openai"})
    await rpc_console.settings_set(
        {
            "key": "embedding",
            "value": {"model": "openai/text-embedding-3-small", "provider": "openai"},
        }
    )

    loaded = load_raven_config(cfg)

    assert loaded.translate.model == "openai/gpt-5-mini"
    assert loaded.translate.provider == "openai"
    assert loaded.embedding.model == "openai/text-embedding-3-small"
    assert loaded.embedding.provider == "openai"


class TestAPinIsWrittenAsOneThing:
    """Both halves of a model pin land in one write, or neither does.

    A pair written a key at a time has two ways to end up mismatched, and the
    surface cannot close either from its side: a connection dropping between
    the writes leaves a new model beside the old provider, with the repair
    write having to travel the connection that just failed; and two surfaces
    saving at once interleave into a pair neither of them chose, with every
    individual write succeeding. One key is one ``atomic_update``.
    """

    async def test_the_pair_lands_together(self, cfg):
        from raven.config.update_providers import set_provider_fields

        set_provider_fields("openai", {"api_key": "sk-openai"})
        r = await rpc_console.settings_set(
            {
                "key": "embedding",
                "value": {"model": "openai/text-embedding-3-large", "provider": "openai"},
            }
        )

        assert r["applied"] is True
        block = _read(cfg)["embedding"]
        assert block["model"] == "openai/text-embedding-3-large"
        assert block["provider"] == "openai"

    async def test_a_refused_half_writes_neither(self, cfg):
        """The whole point. Validation runs over the pair before the file is
        touched, so the half that would have passed is not left behind."""
        with pytest.raises(ConfigValidationError):
            await rpc_console.settings_set(
                {
                    "key": "knowledge",
                    "value": {"embeddingModel": "openai/text-embedding-3-large", "embeddingProvider": "nosuchvendor"},
                }
            )

        assert "knowledge" not in _read(cfg)

    async def test_half_a_pair_is_not_a_pin(self, cfg):
        with pytest.raises(ConfigValidationError):
            await rpc_console.settings_set({"key": "knowledge", "value": {"embeddingModel": "openai/x"}})

        assert "knowledge" not in _read(cfg)

    async def test_the_rest_of_the_block_survives_the_write(self, cfg):
        """``sessionTitle`` carries enabled, the timeout and the width gate
        beside its pin. A replacing write would drop all three."""
        cfg.write_text(
            json.dumps({"sessionTitle": {"enabled": True, "timeoutSeconds": 8.0, "budget": 24}}),
            encoding="utf-8",
        )

        await rpc_console.settings_set(
            {"key": "sessionTitle", "value": {"model": "openai/gpt-5.4-mini", "provider": "openai"}}
        )

        block = _read(cfg)["sessionTitle"]
        assert block["model"] == "openai/gpt-5.4-mini"
        assert block["provider"] == "openai"
        assert (block["enabled"], block["timeoutSeconds"], block["budget"]) == (True, 8.0, 24)

    async def test_clearing_a_pin_clears_both_halves(self, cfg):
        cfg.write_text(
            json.dumps({"translate": {"model": "openai/gpt-5.5", "provider": "openai"}}),
            encoding="utf-8",
        )

        await rpc_console.settings_set({"key": "translate", "value": {"model": "", "provider": ""}})

        block = _read(cfg)["translate"]
        assert block["model"] is None and block["provider"] is None


class TestAPinCanNameAnyProviderThisConfigHolds:
    """Raven carries no spec for every vendor LiteLLM can reach.

    Checking a pin's provider against the registry alone made a working
    endpoint uneditable on the page that exists to edit it: the wizard stored
    `provider: deepinfra`, every reader resolved it, and this surface called it
    a provider that does not exist.
    """

    async def test_a_section_in_the_config_is_proof_the_vendor_exists(self, cfg):
        from raven.config.update_providers import set_provider_fields
        from raven.providers.registry import find_by_name

        assert find_by_name("deepinfra") is None, "the point of this test is a vendor with no spec"
        set_provider_fields("deepinfra", {"api_key": "sk-di", "api_base": "https://api.deepinfra.com/v1/openai"})

        r = await rpc_console.settings_set(
            {"key": "embedding", "value": {"model": "Qwen/Qwen3-Embedding-8B", "provider": "deepinfra"}}
        )

        assert r["applied"] is True
        assert _read(cfg)["embedding"] == {"model": "Qwen/Qwen3-Embedding-8B", "provider": "deepinfra"}

    async def test_a_name_nothing_holds_is_still_a_typo(self, cfg):
        """The check still earns its keep: a misspelling would otherwise
        surface as a silent fallback to the conversation's model."""
        with pytest.raises(ConfigValidationError, match="no provider named"):
            await rpc_console.settings_set({"key": "embedding", "value": {"model": "m", "provider": "deepinfr"}})


class TestClearingTheEmbeddingPin:
    """The picker's "inherit" option sends both halves empty.

    Dropping empty values before the write made that a no-op the caller was
    told had applied: the picker snapped back to the old pair on the next load,
    and editing the file by hand was the only way to unset it.
    """

    async def test_both_halves_empty_removes_the_block(self, cfg):
        from raven.config.update_providers import set_provider_fields

        set_provider_fields("openai", {"api_key": "sk-openai"})
        await rpc_console.settings_set(
            {"key": "embedding", "value": {"model": "text-embedding-3-small", "provider": "openai"}}
        )

        r = await rpc_console.settings_set({"key": "embedding", "value": {"model": "", "provider": ""}})

        assert r["applied"] is True
        assert "embedding" not in _read(cfg)
        assert r["previous"] == {"model": "text-embedding-3-small", "provider": "openai"}

    async def test_clearing_what_was_never_set_is_not_an_error(self, cfg):
        r = await rpc_console.settings_set({"key": "embedding", "value": {"model": "", "provider": ""}})

        assert r["applied"] is True
        assert "embedding" not in _read(cfg)


class TestTheEmbeddingPinHasOneWayIn:
    """Two writers for one block is one writer that checks and one that does
    not. The settings page's pin row wrote raw -- no provider check, and no
    word about what a changed model costs -- while the wizard and the memory
    card went through the endpoint writer and got both."""

    async def test_the_page_cannot_store_a_pin_that_cannot_embed(self, cfg):
        from raven.config.update_providers import set_provider_fields

        set_provider_fields("openai", {"api_key": "sk-openai"})

        with pytest.raises(ConfigValidationError, match="no usable credential"):
            await rpc_console.settings_set(
                {"key": "embedding", "value": {"model": "text-embedding-3-small", "provider": "siliconflow"}}
            )

        assert "embedding" not in _read(cfg)

    async def test_the_page_says_what_moving_the_model_costs(self, cfg):
        from raven.config.update_providers import set_provider_fields

        set_provider_fields("openai", {"api_key": "sk-openai"})
        first = await rpc_console.settings_set(
            {"key": "embedding", "value": {"model": "text-embedding-3-small", "provider": "openai"}}
        )
        assert "warning" not in first, "nothing was replaced, so there is nothing to warn about"

        moved = await rpc_console.settings_set(
            {"key": "embedding", "value": {"model": "text-embedding-3-large", "provider": "openai"}}
        )

        assert "text-embedding-3-small" in moved["warning"]
        assert "text-embedding-3-large" in moved["warning"]
        assert "rebuild" in moved["warning"]
        # Says what has to happen, not which product does it. Whose memory
        # backend is installed is not the host's business to assume, and a host
        # message printing one backend's command is the coupling the seam
        # exists to remove.
        assert "everos" not in moved["warning"].casefold()


class TestAWriteFollowsTheSpellingTheConfigAlreadyUses:
    """Every block and field is accepted under two spellings.

    The models set ``alias_generator=to_camel`` with ``populate_by_name=True``,
    so a hand-written config may hold ``session_title`` or ``sessionTitle`` and
    both are valid. A write that always used its own spelling did not update
    such a config, it added a second key beside the first -- and because the
    models forbid extras, the block that was already there became an extra
    input and the whole config stopped loading. Every case here is a config
    that loaded before the write and has to load after it.
    """

    @staticmethod
    def _loads(cfg) -> bool:
        from raven.config.raven import load_raven_config

        try:
            load_raven_config()
        except Exception:
            return False
        return True

    async def test_a_snake_case_block_is_updated_not_duplicated(self, cfg):
        cfg.write_text(json.dumps({"session_title": {"enabled": True, "model": "old/m"}}), encoding="utf-8")

        await rpc_console.settings_set(
            {"key": "sessionTitle", "value": {"model": "openai/gpt-5.4-mini", "provider": "openai"}}
        )

        raw = _read(cfg)
        assert "sessionTitle" not in raw, "a second spelling of the block is what breaks the load"
        assert raw["session_title"]["model"] == "openai/gpt-5.4-mini"
        assert raw["session_title"]["provider"] == "openai"
        assert raw["session_title"]["enabled"] is True
        assert self._loads(cfg)

    async def test_snake_case_leaves_are_updated_not_duplicated(self, cfg):
        cfg.write_text(json.dumps({"memory": {"memory_top_k": 3}}), encoding="utf-8")

        await rpc_console.settings_set({"key": "memory.memoryTopK", "value": 7})

        block = _read(cfg)["memory"]
        assert set(block) == {"memory_top_k"}, "a second spelling of the leaf is what breaks the load"
        assert block["memory_top_k"] == 7
        assert self._loads(cfg)

    async def test_a_single_leaf_write_follows_the_block_too(self, cfg):
        """Not only the pair: the leaf keys address the same block and grew the
        same duplicate."""
        cfg.write_text(json.dumps({"session_title": {"enabled": True}}), encoding="utf-8")

        await rpc_console.settings_set({"key": "sessionTitle.model", "value": "openai/x"})

        raw = _read(cfg)
        assert "sessionTitle" not in raw
        assert raw["session_title"]["model"] == "openai/x"
        assert self._loads(cfg)

    async def test_a_camel_case_config_is_left_in_its_own_spelling(self, cfg):
        cfg.write_text(json.dumps({"sessionTitle": {"enabled": True, "model": "old"}}), encoding="utf-8")

        await rpc_console.settings_set({"key": "sessionTitle", "value": {"model": "openai/new", "provider": "openai"}})

        raw = _read(cfg)
        assert "session_title" not in raw
        assert raw["sessionTitle"]["model"] == "openai/new"
        assert self._loads(cfg)

    async def test_a_block_that_is_not_there_yet_is_written_camel(self, cfg):
        """No existing spelling to follow, so the alias the models generate."""
        await rpc_console.settings_set({"key": "memory.memoryTopK", "value": 7})

        assert set(_read(cfg)["memory"]) == {"memoryTopK"}
        assert self._loads(cfg)


class TestClearingAPinThroughItsLeafKeys:
    """``None`` clears a half, the same as the empty string.

    A surface that has no value to send sends ``null`` rather than inventing
    one, and "follow the conversation" is the documented unset state -- so this
    is a way back to it, not a malformed write to refuse.
    """

    async def test_null_clears_the_model_half(self, cfg):
        cfg.write_text(json.dumps({"translate": {"model": "openai/gpt-5.5"}}), encoding="utf-8")

        await rpc_console.settings_set({"key": "translate.model", "value": None})

        assert _read(cfg)["translate"]["model"] is None

    async def test_null_clears_the_provider_half(self, cfg):
        cfg.write_text(json.dumps({"translate": {"provider": "openai"}}), encoding="utf-8")

        await rpc_console.settings_set({"key": "translate.provider", "value": None})

        assert _read(cfg)["translate"]["provider"] is None

    async def test_whitespace_is_not_a_provider(self, cfg):
        """Trimmed to nothing reads as unset rather than as a provider named
        with spaces, which no registry lookup would match."""
        await rpc_console.settings_set({"key": "translate.provider", "value": "   "})

        assert _read(cfg)["translate"]["provider"] is None


async def test_everos_rpcs_name_the_missing_distribution(everos_toml):
    """Without the plugin, both handlers say what to install.

    The module they import ships with ``everos-memory``, so on an install
    without it the import used to reach the client as a generic internal error
    carrying a traceback -- a page cannot act on that, and its own catch turns
    it into a row that reads "not set", which is what a configured-but-empty
    section looks like too.
    """


async def test_reading_without_the_plugin_says_there_is_nothing_to_configure(everos_toml):
    """The page used to render four "not set" rows here -- identical to an
    install where the plugin is present and merely unconfigured -- so a person
    could fill in a model and a key and have nothing happen, with no way to
    learn why."""
    from tests._everos_presence import everos_plugin_absent

    with everos_plugin_absent():
        out = await rpc_console.settings_everos({})

    assert out["available"] is False
    assert out["sections"] == {}
    assert "everos-memory" in out["note"]


async def test_writing_without_the_plugin_is_a_typed_error(everos_toml):
    """A save has somewhere to fail, unlike a read: the page surfaces a write
    error. What it must not be is a traceback wrapped as an internal error."""
    from tests._everos_presence import everos_plugin_absent

    with everos_plugin_absent(), pytest.raises(ConfigValidationError) as caught:
        await rpc_console.settings_everos_set({"section": "llm", "fields": {"model": "m"}})

    assert "everos-memory" in str(caught.value)


@pytest.mark.parametrize(
    "key,good,bad,path",
    [
        ("agents.defaults.maxToolIterations", 120, 0, ("agents", "defaults", "maxToolIterations")),
        ("agents.defaults.contextWindowTokens", 65536, 512, ("agents", "defaults", "contextWindowTokens")),
        ("context.curatorModel", "deepseek-chat", 7, ("context", "curatorModel")),
        ("context.curatorProvider", "deepseek", 7, ("context", "curatorProvider")),
        ("sessionTitle.model", "deepseek-chat", 7, ("sessionTitle", "model")),
        ("sessionTitle.provider", "deepseek", 7, ("sessionTitle", "provider")),
        ("skillForge.llmGateModel", "deepseek-chat", 7, ("skillForge", "llmGateModel")),
        ("skillForge.llmGateProvider", "deepseek", 7, ("skillForge", "llmGateProvider")),
        ("sessions.autoArchiveAfterDays", 30, 0, ("sessions", "autoArchiveAfterDays")),
    ],
)
async def test_settings_set_new_scalar_keys_write_and_refuse(cfg, key, good, bad, path):
    r = await rpc_console.settings_set({"key": key, "value": good})
    assert r["applied"] is True
    node = _read(cfg)
    for part in path:
        node = node[part]
    assert node == good
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": key, "value": bad})


@pytest.mark.parametrize(
    "key",
    [
        "agents.defaults.contextWindowTokens",
        "context.curatorModel",
        "sessionTitle.provider",
        "sessions.autoArchiveAfterDays",
    ],
)
async def test_settings_set_nullable_keys_accept_null(cfg, key):
    r = await rpc_console.settings_set({"key": key, "value": None})
    assert r["applied"] is True
    node = _read(cfg)
    for part in key.split(".")[:-1]:
        node = node[part]
    assert node[key.split(".")[-1]] is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("context.curatorModel", "m"),
        ("context", {"curatorModel": "m", "curatorProvider": "deepseek"}),
        ("skillForge.llmGateModel", "m"),
        ("skillForge", {"llmGateModel": "m", "llmGateProvider": "deepseek"}),
        ("agents.defaults.maxToolIterations", 40),
        ("agents.defaults.contextWindowTokens", 4096),
        ("sessionTitle.model", "m"),
    ],
)
async def test_no_settings_write_asks_for_a_reload_any_more(cfg, key, value):
    """Every one of these is read where it is used, so the write is the whole
    change.

    The first five were reload-only because the value was copied onto the
    objects the loop was built with: the curator's and the gate's pins are
    resolved per call now, the cap and the window are read per turn. Telling
    the operator to restart would be telling them to do nothing, so the whole
    reload-only set is gone -- ``warning`` now belongs to the one writer that
    still has something to say (an embedding model change).
    """
    r = await rpc_console.settings_set({"key": key, "value": value})
    assert r["applied"] is True
    assert "warning" not in r


async def test_settings_set_blocklist_is_a_raw_list(cfg):
    await rpc_console.settings_set({"key": "skillForge.blocklist", "value": ["codeword"]})
    assert _read(cfg)["skillForge"]["blocklist"] == ["codeword"]
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "skillForge.blocklist", "value": "codeword"})


async def test_settings_set_media_speech_selection_merges(cfg):
    await rpc_console.settings_set({"key": "tools.media.speech", "value": {"model": "tts-1", "quality": "high"}})
    await rpc_console.settings_set({"key": "tools.media.speech", "value": {"model": "tts-2", "quality": ""}})
    assert _read(cfg)["tools"]["media"]["speech"] == {"model": "tts-2", "quality": ""}
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "tools.media.video", "value": {"model": "v"}})


@pytest.mark.parametrize(
    "parent,model_field,provider_field",
    [("context", "curatorModel", "curatorProvider"), ("skillForge", "llmGateModel", "llmGateProvider")],
)
async def test_settings_set_pin_pairs_write_as_one_merged_object(cfg, parent, model_field, provider_field):
    cfg.write_text(json.dumps({parent: {"keep": True}}), encoding="utf-8")
    r = await rpc_console.settings_set(
        {"key": parent, "value": {model_field: "deepseek-chat", provider_field: "deepseek"}}
    )
    assert r["applied"] is True
    block = _read(cfg)[parent]
    assert block == {"keep": True, model_field: "deepseek-chat", provider_field: "deepseek"}
    await rpc_console.settings_set({"key": parent, "value": {model_field: None, provider_field: None}})
    assert _read(cfg)[parent] == {"keep": True, model_field: None, provider_field: None}
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": parent, "value": {model_field: "m"}})


# ---------------------------------------------------------------------------
# settings.usage: date range and daily buckets
# ---------------------------------------------------------------------------


def _iso(days_ago: int) -> str:
    from datetime import date, timedelta

    return (date.today() - timedelta(days=days_ago)).isoformat()


def _telemetry_row(model: str, cost: float | None, *, cache_read: int = 0, cache_write: int | None = None) -> dict:
    return {
        "ts": "2026-09-01T00:00:00+00:00",
        "schema_version": 2,
        "model": model,
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cost_usd": cost,
        "session_key": "web:s1",
        "root_session_key": "web:s1",
    }


@pytest.fixture()
def telemetry(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    tel = tmp_path / "telemetry"
    tel.mkdir()

    def write(days_ago: int, rows: list[dict]) -> None:
        p = tel / f"usage-{_iso(days_ago)}.jsonl"
        with p.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    return write


async def test_usage_daily_buckets_cover_the_range_with_zero_days(telemetry):
    telemetry(1, [_telemetry_row("a", 1.0)])
    telemetry(
        3,
        [
            _telemetry_row("a", 2.0),
            {
                "_type": "tool_call",
                "schema_version": 2,
                "name": "exec",
                "tool_call_id": "c1",
                "session_key": "web:s1",
                "root_session_key": "web:s1",
            },
        ],
    )
    telemetry(6, [_telemetry_row("a", 9.0)])
    r = await rpc_console.settings_usage({"from": _iso(4), "to": _iso(1)})
    assert (r["from"], r["to"], r["days"]) == (_iso(4), _iso(1), 4)
    assert [d["date"] for d in r["daily"]] == [_iso(4), _iso(3), _iso(2), _iso(1)]
    assert [d["cost_usd"] for d in r["daily"]] == [None, 2.0, None, 1.0]
    assert [d["calls"] for d in r["daily"]] == [0, 1, 0, 1]
    assert r["llm"]["total"]["cost_usd"] == 3.0
    assert r["tools"]["counts"] == [{"name": "exec", "count": 1}]


def _tool_row(name: str, call_id: str) -> dict:
    return {
        "_type": "tool_call",
        "schema_version": 2,
        "name": name,
        "tool_call_id": call_id,
        "session_key": "web:s1",
        "root_session_key": "web:s1",
    }


def _transcript(home, name: str, days_ago: int, calls: list[str], channel: str = "tui") -> None:
    """One session file carrying tool calls, with its mtime set by ``days_ago``.

    Nothing in the reply may be counted off it -- the cases below are about a
    transcript NOT reaching the tallies -- so the mtime is what they vary.
    """
    import os
    from datetime import datetime, timedelta

    d = home / "workspace" / "sessions" / channel
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.jsonl"
    rows = [{"_type": "metadata", "key": f"{channel}:{name}", "metadata": {"title": name}}]
    rows.append({"role": "assistant", "tool_calls": [{"id": f"{name}-1", "name": c} for c in calls]})
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    when = (datetime.now() - timedelta(days=days_ago)).timestamp()
    os.utime(f, (when, when))


async def test_usage_counts_a_tool_call_on_the_day_it_was_recorded(telemetry, tmp_path):
    """One range read one way, tools included.

    The tool tally used to be topped up by reading session transcripts, and a
    transcript counted as in-range when its file mtime was -- which handed the
    range every tool call that conversation had ever made, while its model
    calls, dated per day, stayed outside it. A page showing thousands of tool
    calls beside no model calls at all reads as broken, and is: one reply
    cannot carry two readings of the same range.
    """
    telemetry(1, [_tool_row("exec", "in-1")])
    telemetry(40, [_tool_row("grep", "old-1")])
    # Touched today, so the old scan would have taken it, and full of tool
    # calls that belong to no day this reply reports.
    _transcript(tmp_path, "touched-today", 0, ["read_file", "read_file"])

    r = await rpc_console.settings_usage({"from": _iso(5), "to": _iso(0)})
    assert r["tools"]["counts"] == [{"name": "exec", "count": 1}]
    assert r["tools"]["total"] == 1

    # The day itself is what decides, not the file: widen the range and the
    # older call joins; the transcript still does not.
    r = await rpc_console.settings_usage({"from": _iso(41), "to": _iso(0)})
    assert sorted(c["name"] for c in r["tools"]["counts"]) == ["exec", "grep"]
    assert r["tools"]["total"] == 2


async def test_usage_names_the_sessions_the_range_saw(telemetry, tmp_path):
    """Titles come from the transcripts of the sessions telemetry named.

    A transcript is read for its title and for nothing else, and only when the
    range saw that session at all -- which the telemetry answers, so the file
    system never gets a vote on what the range covers.
    """
    telemetry(1, [_telemetry_row("a", 1.0)])
    _transcript(tmp_path, "s1", 0, [], channel="web")
    _transcript(tmp_path, "elsewhere", 0, [], channel="web")

    r = await rpc_console.settings_usage({"from": _iso(5), "to": _iso(0)})
    assert r["sessions"] == ["web:s1"]
    assert r["session_titles"] == {"web:s1": "s1"}


async def test_usage_from_is_clamped_and_reversed_range_refused(telemetry):
    r = await rpc_console.settings_usage({"from": _iso(400), "to": _iso(0)})
    assert r["from"] == _iso(89)
    assert r["days"] == 90
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_usage({"from": _iso(0), "to": _iso(1)})
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_usage({"from": "yesterday"})


async def test_usage_from_to_win_over_days(telemetry):
    telemetry(10, [_telemetry_row("a", 5.0)])
    r = await rpc_console.settings_usage({"days": 30, "from": _iso(2), "to": _iso(0)})
    assert r["llm"]["total"]["calls"] == 0
    assert r["days"] == 3
    r = await rpc_console.settings_usage({"days": 30})
    assert r["llm"]["total"]["calls"] == 1
    assert r["from"] == _iso(29)
