"""``settings.set`` — the GUI dialog's whitelisted hot-writable keys."""

from __future__ import annotations

import json
import tomllib
from types import SimpleNamespace

import pytest

from raven import home as raven_home
from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods import console as rpc_console

pytestmark = pytest.mark.asyncio


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


async def test_everos_get_masks_key(everos_toml):
    everos_toml.write_text(
        '[embedding]\nmodel = "openai/text-embedding-3-large"\napi_key = "sk-secret"\n'
        '[llm]\nmodel = "<pick-a-model>"\n',
        encoding="utf-8",
    )
    r = await rpc_console.settings_everos({})
    emb = r["sections"]["embedding"]
    assert emb["model"] == "openai/text-embedding-3-large"
    assert emb["api_key_set"] is True
    assert "sk-secret" not in str(r)
    assert r["sections"]["llm"]["model"] == ""


async def test_everos_set_merges_section(everos_toml):
    await rpc_console.settings_everos_set({"section": "rerank", "fields": {"model": "m1", "api_key": "k1"}})
    await rpc_console.settings_everos_set({"section": "rerank", "fields": {"base_url": "https://x/v1"}})
    import tomllib

    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["rerank"] == {"model": "m1", "api_key": "k1", "base_url": "https://x/v1"}


class TestEmbeddingCardFollowsTheEndpointHome:
    """The embedding endpoint is raven's, and everos.toml keeps an override.

    The card has to read and write whichever of the two is in force. Reading
    only the file left it blank for an install the wizard had just configured,
    and filling it in from there wrote a second endpoint that silently
    outranked the one a knowledge base goes on reading -- the divergence the
    move was meant to end, recreated through the settings page.
    """

    async def test_the_card_shows_the_endpoint_raven_holds(self, everos_toml, tmp_path, monkeypatch) -> None:
        import json

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "embedding": {"model": "Qwen/Qwen3-Embedding-4B", "provider": "deepinfra"},
                    "providers": {"deepinfra": {"apiKey": "sk-1", "apiBase": "https://e.test/v1"}},
                }
            ),
            encoding="utf-8",
        )
        # The process-wide pointer rather than two patched names: the pin is
        # read back through the loader, which binds `get_config_path` itself.
        raven_home.set_config_path(cfg)
        everos_toml.write_text('[llm]\nmodel = "m"\n', encoding="utf-8")

        card = (await rpc_console.settings_everos({}))["sections"]["embedding"]

        # The pair as stored -- the model spelled the way it was chosen, not
        # the way the vendor is addressed -- beside the address it resolves to.
        assert card["model"] == "Qwen/Qwen3-Embedding-4B"
        assert card["provider"] == "deepinfra"
        assert card["base_url"] == "https://e.test/v1"
        assert card["api_key_set"] is True

    async def test_saving_the_card_writes_where_the_card_reads(self, everos_toml, tmp_path, monkeypatch) -> None:
        import json
        import tomllib

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"providers": {"siliconflow": {"apiKey": "sk-2", "apiBase": "https://e.test/v1"}}}),
            encoding="utf-8",
        )
        # The process-wide pointer rather than two patched names: the pin is
        # read back through the loader, which binds `get_config_path` itself.
        raven_home.set_config_path(cfg)
        everos_toml.write_text('[llm]\nmodel = "m"\n', encoding="utf-8")

        await rpc_console.settings_everos_set(
            {"section": "embedding", "fields": {"model": "bge-m3", "provider": "siliconflow"}}
        )

        block = json.loads(cfg.read_text(encoding="utf-8"))["embedding"]
        assert block == {"model": "bge-m3", "provider": "siliconflow"}
        # And not into the file, where it would outrank what it just wrote.
        assert "embedding" not in tomllib.loads(everos_toml.read_text(encoding="utf-8"))

    async def test_an_address_is_refused_rather_than_dropped(self, everos_toml, tmp_path, monkeypatch) -> None:
        """Raven's block holds no credential -- it names the provider that has
        one. Accepting an address or a key and discarding it reads to the
        caller as a value that was stored."""
        import json

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"providers": {"siliconflow": {"apiKey": "sk", "apiBase": "https://sf/v1"}}}), encoding="utf-8"
        )
        # The process-wide pointer rather than two patched names: the pin is
        # read back through the loader, which binds `get_config_path` itself.
        raven_home.set_config_path(cfg)
        everos_toml.write_text('[llm]\nmodel = "m"\n', encoding="utf-8")

        with pytest.raises(ConfigValidationError, match="base_url"):
            await rpc_console.settings_everos_set(
                {
                    "section": "embedding",
                    "fields": {"model": "m", "provider": "siliconflow", "base_url": "https://elsewhere/v1"},
                }
            )

    async def test_the_address_the_card_showed_is_an_echo_not_an_instruction(
        self, everos_toml, tmp_path, monkeypatch
    ) -> None:
        """The row renders the resolved address as a `defaultValue`, so every
        save carries it back whether or not anyone touched it. Refusing that
        made the row unsaveable without first emptying a field nobody had
        edited -- changing only the model came back as an error."""
        import json

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "embedding": {"model": "old-model", "provider": "siliconflow"},
                    "providers": {"siliconflow": {"apiKey": "sk", "apiBase": "https://api.siliconflow.cn/v1"}},
                }
            ),
            encoding="utf-8",
        )
        raven_home.set_config_path(cfg)
        everos_toml.write_text('[llm]\nmodel = "m"\n', encoding="utf-8")

        r = await rpc_console.settings_everos_set(
            {
                "section": "embedding",
                "fields": {"model": "new-model", "base_url": "https://api.siliconflow.cn/v1"},
            }
        )

        assert r["applied"] is True
        assert json.loads(cfg.read_text(encoding="utf-8"))["embedding"] == {
            "model": "new-model",
            "provider": "siliconflow",
        }

        # An address that differs is an instruction, and there is nowhere to put it.
        with pytest.raises(ConfigValidationError, match="belongs to the provider"):
            await rpc_console.settings_everos_set(
                {"section": "embedding", "fields": {"base_url": "https://elsewhere.test/v1"}}
            )

    async def test_half_a_pin_is_refused_from_either_end(self, everos_toml, tmp_path, monkeypatch) -> None:
        """Both halves or neither. A model with nobody to serve it reads as
        configured on every screen while every reader resolves it to nothing;
        a provider with nothing to run is the same write from the other end,
        reported as saved and storing nothing anyone can use."""
        import json

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"providers": {"siliconflow": {"apiKey": "sk", "apiBase": "https://sf/v1"}}}), encoding="utf-8"
        )
        # The process-wide pointer rather than two patched names: the pin is
        # read back through the loader, which binds `get_config_path` itself.
        raven_home.set_config_path(cfg)
        everos_toml.write_text('[llm]\nmodel = "m"\n', encoding="utf-8")

        with pytest.raises(ConfigValidationError, match="needs the provider"):
            await rpc_console.settings_everos_set({"section": "embedding", "fields": {"model": "orphan"}})

        # The card's lender picker with the model box left blank sends exactly
        # this, and used to come back applied.
        with pytest.raises(ConfigValidationError, match="needs the model"):
            await rpc_console.settings_everos_set({"section": "embedding", "fields": {}, "borrow_from": "siliconflow"})

        assert "embedding" not in json.loads(cfg.read_text(encoding="utf-8"))

    async def test_the_card_survives_ravens_own_startup_binding(self, everos_toml, tmp_path, monkeypatch) -> None:
        """The page as a running install reaches it: after `backend.start()`.

        That call puts the host endpoint into this process, and a reader
        without provenance then saw three complete variables and told the card
        an operator owned them -- so the card went blank and the save was
        refused by naming variables nobody had set.
        """
        import json

        import raven_everos.config as ue

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "embedding": {"model": "ravens/model", "provider": "siliconflow"},
                    "providers": {"siliconflow": {"apiKey": "sk-raven", "apiBase": "https://ravens/v1"}},
                }
            ),
            encoding="utf-8",
        )
        # The process-wide pointer rather than two patched names: the pin is
        # read back through the loader, which binds `get_config_path` itself.
        raven_home.set_config_path(cfg)
        everos_toml.write_text('[llm]\nmodel = "m"\n', encoding="utf-8")
        ue.configure_embedding_env(
            SimpleNamespace(model="ravens/model", base_url="https://ravens/v1", api_key="sk-raven")
        )

        card = (await rpc_console.settings_everos({}))["sections"]["embedding"]
        assert card["model"] == "ravens/model", "the card must still show what raven holds"

        await rpc_console.settings_everos_set({"section": "embedding", "fields": {"model": "edited"}})
        assert json.loads(cfg.read_text(encoding="utf-8"))["embedding"]["model"] == "edited"

    async def test_borrowing_a_provider_records_its_name_rather_than_its_key(
        self, everos_toml, tmp_path, monkeypatch
    ) -> None:
        """The card's lender picker is the path that still works here. For the
        EverOS file a borrow copies the address and the key across; for raven's
        block the name is the whole point, and copying would produce two fields
        the block has no place for."""
        import json

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"providers": {"siliconflow": {"apiKey": "sk-sf", "apiBase": "https://sf/v1"}}}),
            encoding="utf-8",
        )
        raven_home.set_config_path(cfg)
        everos_toml.write_text('[llm]\nmodel = "m"\n', encoding="utf-8")

        await rpc_console.settings_everos_set(
            {"section": "embedding", "fields": {"model": "bge-m3"}, "borrow_from": "siliconflow"}
        )

        assert json.loads(cfg.read_text(encoding="utf-8"))["embedding"] == {
            "model": "bge-m3",
            "provider": "siliconflow",
        }

    async def test_moving_the_model_says_what_it_costs(self, everos_toml, tmp_path, monkeypatch) -> None:
        """Everything already embedded answers to the old model, and a query
        embedded with the new one lands somewhere unrelated in the same space.
        Said once, where the change is made."""
        import json

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "embedding": {"model": "bge-m3", "provider": "siliconflow"},
                    "providers": {"siliconflow": {"apiKey": "sk-sf", "apiBase": "https://sf/v1"}},
                }
            ),
            encoding="utf-8",
        )
        raven_home.set_config_path(cfg)
        everos_toml.write_text('[llm]\nmodel = "m"\n', encoding="utf-8")

        unchanged = await rpc_console.settings_everos_set(
            {"section": "embedding", "fields": {"model": "bge-m3", "provider": "siliconflow"}}
        )
        assert "warning" not in unchanged

        moved = await rpc_console.settings_everos_set(
            {"section": "embedding", "fields": {"model": "bge-large", "provider": "siliconflow"}}
        )
        assert "bge-m3" in moved["warning"] and "bge-large" in moved["warning"]

    async def test_a_save_the_environment_would_outrank_is_refused(self, everos_toml, tmp_path, monkeypatch) -> None:
        """The exported variables beat both files, so a save accepted here
        would be written and then ignored -- the silent no-op this card was
        just fixed for, arriving by the one route left."""
        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        # The process-wide pointer rather than two patched names: the pin is
        # read back through the loader, which binds `get_config_path` itself.
        raven_home.set_config_path(cfg)
        everos_toml.write_text('[llm]\nmodel = "m"\n', encoding="utf-8")
        for name, value in (("MODEL", "op/model"), ("BASE_URL", "https://op/v1"), ("API_KEY", "sk-op")):
            monkeypatch.setenv(f"EVEROS_EMBEDDING__{name}", value)

        with pytest.raises(ConfigValidationError, match="EVEROS_EMBEDDING__MODEL"):
            await rpc_console.settings_everos_set({"section": "embedding", "fields": {"model": "whatever"}})

    async def test_an_operators_own_section_keeps_both_halves(self, everos_toml, tmp_path, monkeypatch) -> None:
        """Someone who wrote [embedding] into everos.toml chose that endpoint
        for memory; the card stays on it for reading and for writing."""
        import json
        import tomllib

        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        # The process-wide pointer rather than two patched names: the pin is
        # read back through the loader, which binds `get_config_path` itself.
        raven_home.set_config_path(cfg)
        everos_toml.write_text('[embedding]\nmodel = "bge-own"\napi_key = "k"\n', encoding="utf-8")

        card = (await rpc_console.settings_everos({}))["sections"]["embedding"]
        assert card["model"] == "bge-own"

        await rpc_console.settings_everos_set({"section": "embedding", "fields": {"model": "bge-own-2"}})

        assert tomllib.loads(everos_toml.read_text(encoding="utf-8"))["embedding"]["model"] == "bge-own-2"
        assert "embedding" not in json.loads(cfg.read_text(encoding="utf-8"))


async def test_everos_set_rejects_bad_input(everos_toml):
    with pytest.raises(ConfigValidationError, match="unknown everos section"):
        await rpc_console.settings_everos_set({"section": "memory", "fields": {"model": "m"}})
    with pytest.raises(ConfigValidationError, match="not writable"):
        await rpc_console.settings_everos_set({"section": "llm", "fields": {"dimensions": "1024"}})
    with pytest.raises(ConfigValidationError, match="non-empty"):
        await rpc_console.settings_everos_set({"section": "llm", "fields": {"model": "  "}})


@pytest.fixture()
def lender(tmp_path, monkeypatch):
    """A provider written into a real config file, in whichever of the three
    shapes a section may take. Reading the file rather than stubbing the reader
    is the point: the two shapes that broke this are precedence rules inside
    `provider_endpoints`, and a stub would answer for neither."""

    def _write(section: dict, name: str = "openrouter") -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"providers": {name: section}}), encoding="utf-8")
        monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)

    return _write


async def test_everos_set_borrows_a_connected_provider(everos_toml, lender):
    """The page cannot read a stored key, so the server resolves it. What lands
    in the file is the real key, copied -- not the provider's name."""
    lender({"apiKey": "sk-lent", "apiBase": "https://lender.example/v1"})
    await rpc_console.settings_everos_set(
        {"section": "rerank", "fields": {"model": "qwen/qwen3-embedding-8b"}, "borrow_from": "openrouter"}
    )
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["rerank"] == {
        "model": "qwen/qwen3-embedding-8b",
        "api_key": "sk-lent",
        "base_url": "https://lender.example/v1",
    }


async def test_borrowing_reads_an_endpoints_section(everos_toml, lender):
    """`endpoints` replaces the flat pair outright, so a section written that way
    has an empty `api_key` while the provider serves traffic. Reading the flat
    field offered it as a lender and then refused to lend."""
    lender(
        {
            "endpoints": [
                {"label": "one", "apiKey": "sk-from-endpoint", "apiBase": "https://ep.example/v1"},
            ]
        }
    )
    await rpc_console.settings_everos_set({"section": "rerank", "fields": {"model": "m"}, "borrow_from": "openrouter"})
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["rerank"]["api_key"] == "sk-from-endpoint"
    assert data["rerank"]["base_url"] == "https://ep.example/v1"


async def test_borrowing_reads_an_api_key_list_section(everos_toml, lender):
    """The other shape the precedence exists for: Gemini's rotation list never
    populates the flat field either."""
    lender({"apiKeyList": ["k-gem-1", "k-gem-2"], "apiBase": "https://gem.example/v1"}, name="gemini")
    await rpc_console.settings_everos_set({"section": "rerank", "fields": {"model": "m"}, "borrow_from": "gemini"})
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["rerank"]["api_key"] == "k-gem-1"


async def test_a_borrowed_key_beats_the_redaction_the_page_echoes(everos_toml, lender):
    """`model.endpoints` hands the page `****set****`, and a form that submits
    what it was shown would write that string over a working key."""
    lender({"apiKey": "sk-lent", "apiBase": "https://lender.example/v1"})
    await rpc_console.settings_everos_set(
        {
            "section": "rerank",
            "fields": {"model": "m", "api_key": "****set****", "base_url": "https://stale/v1"},
            "borrow_from": "openrouter",
        }
    )
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["rerank"]["api_key"] == "sk-lent"
    assert data["rerank"]["base_url"] == "https://lender.example/v1"


async def test_a_borrow_hands_over_an_address_as_well_as_a_key(everos_toml, lender):
    """The lender's address wins, whichever tier it comes from.

    Named for what it pins now: `custom` carries a registry default, so what
    this asserts is that the borrow supplies an address -- not that the
    section kept its own, which this PR deliberately stopped doing."""
    lender({"apiKey": "sk-lent"}, name="custom")
    await rpc_console.settings_everos_set(
        {"section": "rerank", "fields": {"base_url": "https://mine/v1"}, "borrow_from": "custom"}
    )
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["rerank"]["api_key"] == "sk-lent"
    # `custom` carries a registry default address, so the borrow does have one to
    # give and it wins -- which is the same rule as every other lender. What the
    # section keeps on its own is covered by the endpoints case above, where the
    # entry supplies the address.
    assert data["rerank"]["base_url"]


async def test_the_borrowed_address_replaces_the_lenders_predecessor(everos_toml, lender):
    """The incident, end to end: a role moved from an OpenRouter model to a
    DeepSeek one kept DeepSeek's key at OpenRouter's address, which the far end
    refuses. What the section must end up with is DeepSeek's own address -- not
    the one it was reached at before, and not nothing, which the reader refuses
    just as flatly."""
    everos_toml.write_text(
        '[llm]\nmodel = "openrouter/anthropic/claude-3.5-sonnet"\n'
        'api_key = "sk-openrouter"\nbase_url = "https://openrouter.ai/api/v1"\n',
        encoding="utf-8",
    )
    lender({"apiKey": "sk-deepseek"}, name="deepseek")
    await rpc_console.settings_everos_set(
        {"section": "llm", "fields": {"model": "deepseek/deepseek-chat"}, "borrow_from": "deepseek"}
    )
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["llm"] == {
        "model": "deepseek/deepseek-chat",
        "api_key": "sk-deepseek",
        "base_url": "https://api.deepseek.com/v1",
    }


async def test_a_vendor_the_table_knows_only_by_its_shown_address_still_lends_one(everos_toml, lender):
    """groq and zai were the remainder, and the remainder deleted the address.

    The earlier fix carried the fallback table through the lend, which closed
    deepseek, openai and openrouter and left every other keyed vendor
    answering "" -- a delete, on a section whose reader cannot tell it from a
    working one: `role_configured_in` and the page's LED both ask
    `model and api_key`, so a role with no address went on reporting itself
    set up until something called it.
    """
    everos_toml.write_text(
        '[llm]\nmodel = "openrouter/anthropic/claude-3.5-sonnet"\n'
        'api_key = "sk-openrouter"\nbase_url = "https://gateway.internal/v1"\n',
        encoding="utf-8",
    )
    lender({"apiKey": "gsk-groq"}, name="groq")
    await rpc_console.settings_everos_set(
        {"section": "llm", "fields": {"model": "groq/llama-3.3-70b"}, "borrow_from": "groq"}
    )
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["llm"]["api_key"] == "gsk-groq"
    assert data["llm"]["base_url"] == "https://api.groq.com/openai/v1"


async def test_borrowing_finds_a_provider_stored_under_another_spelling(everos_toml, lender):
    """`ProvidersConfig.get` is spelling-insensitive and attribute access is not.
    A provider raven carries no spec for is stored under whatever key its writer
    used, which is exactly the case the invariant in
    `test_provider_resolution_invariants` exists to keep working."""
    lender({"apiKey": "sk-hyphen", "apiBase": "https://hyph.example/v1"}, name="some-vendor")
    await rpc_console.settings_everos_set({"section": "rerank", "fields": {"model": "m"}, "borrow_from": "some-vendor"})
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["rerank"]["api_key"] == "sk-hyphen"


async def test_borrowing_refuses_what_it_cannot_lend(everos_toml, lender):
    """Usable and lendable are different questions: a keyless local address
    satisfies the first and has nothing to answer the second with."""
    lender({"apiKey": "sk-lent"})
    with pytest.raises(ConfigValidationError, match="no such provider"):
        await rpc_console.settings_everos_set({"section": "rerank", "fields": {"model": "m"}, "borrow_from": "nobody"})
    lender({"apiBase": "http://127.0.0.1:11434/v1"}, name="ollama")
    with pytest.raises(ConfigValidationError, match="no api key to lend"):
        await rpc_console.settings_everos_set({"section": "rerank", "fields": {"model": "m"}, "borrow_from": "ollama"})


async def test_everos_clear_optional_only(everos_toml):
    everos_toml.write_text('[rerank]\nmodel = "r1"\n[llm]\nmodel = "m1"\n', encoding="utf-8")
    r = await rpc_console.settings_everos_set({"section": "rerank", "clear": True})
    assert r["applied"] is True
    import tomllib

    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert "rerank" not in data
    with pytest.raises(ConfigValidationError, match="required"):
        await rpc_console.settings_everos_set({"section": "llm", "clear": True})


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


async def test_default_permission_mode_is_a_settings_key(cfg):
    r = await rpc_console.settings_set({"key": "permissions.mode", "value": "smart"})
    assert r["applied"] is True
    assert _read(cfg)["permissions"]["mode"] == "smart"
    with pytest.raises(ConfigValidationError):
        await rpc_console.settings_set({"key": "permissions.mode", "value": "yolo"})


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


async def test_settings_set_warns_only_for_reload_only_keys(cfg):
    warned = await rpc_console.settings_set({"key": "context.curatorModel", "value": "m"})
    assert "reload" in warned["warning"].lower()
    live = await rpc_console.settings_set({"key": "sessionTitle.model", "value": "m"})
    assert "warning" not in live


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
    assert "reload" in r["warning"].lower()
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


def _transcript(home, name: str, days_ago: int, calls: list[str]) -> None:
    """One session file whose tool calls the fallback scan may or may not count.

    The scan reads a transcript when its mtime is inside the window, so the
    mtime is what the case is about; the rows themselves are the same either
    way.
    """
    import os
    from datetime import datetime, timedelta

    d = home / "workspace" / "sessions" / "tui"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.jsonl"
    rows = [{"_type": "metadata", "key": f"tui:{name}", "metadata": {"title": name}}]
    rows.append({"role": "assistant", "tool_calls": [{"id": f"{name}-1", "name": c} for c in calls]})
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    when = (datetime.now() - timedelta(days=days_ago)).timestamp()
    os.utime(f, (when, when))


async def test_usage_tool_scan_is_bounded_at_both_ends(telemetry, tmp_path):
    """A transcript touched after `to` is outside the window the reply reports.

    The LLM and telemetry-tool tallies read the selected days' files only, so
    counting a transcript modified later made one reply disagree with itself:
    the tool total covered a wider range than the dates beside it.
    """
    _transcript(tmp_path, "inside", 4, ["exec"])
    _transcript(tmp_path, "after", 0, ["read_file", "read_file"])
    _transcript(tmp_path, "before", 40, ["grep"])

    r = await rpc_console.settings_usage({"from": _iso(5), "to": _iso(3)})
    assert r["tools"]["counts"] == [{"name": "exec", "count": 1}]
    assert r["tools"]["total"] == 1

    # And the same scan does count it once the range reaches that day.
    r = await rpc_console.settings_usage({"from": _iso(5), "to": _iso(0)})
    assert sorted(c["name"] for c in r["tools"]["counts"]) == ["exec", "read_file"]
    assert r["tools"]["total"] == 3


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
