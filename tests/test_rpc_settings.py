"""``settings.set`` — the GUI dialog's whitelisted hot-writable keys."""

from __future__ import annotations

import json
import tomllib

import pytest

from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods import console as rpc_console

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
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
    monkeypatch.setattr("raven.config.update_everos.everos_root", lambda: path.parent)
    # Upstream gates the write primitives on root ownership; a tmp root is not
    # one raven created, so declare it owned for the test.
    monkeypatch.setattr("raven.config.update_everos.everos_owned", lambda: True)
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
    await rpc_console.settings_everos_set({"section": "embedding", "fields": {"model": "m1", "api_key": "k1"}})
    await rpc_console.settings_everos_set({"section": "embedding", "fields": {"base_url": "https://x/v1"}})
    import tomllib

    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["embedding"] == {"model": "m1", "api_key": "k1", "base_url": "https://x/v1"}


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
        {"section": "embedding", "fields": {"model": "qwen/qwen3-embedding-8b"}, "borrow_from": "openrouter"}
    )
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["embedding"] == {
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
    await rpc_console.settings_everos_set(
        {"section": "embedding", "fields": {"model": "m"}, "borrow_from": "openrouter"}
    )
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["embedding"]["api_key"] == "sk-from-endpoint"
    assert data["embedding"]["base_url"] == "https://ep.example/v1"


async def test_borrowing_reads_an_api_key_list_section(everos_toml, lender):
    """The other shape the precedence exists for: Gemini's rotation list never
    populates the flat field either."""
    lender({"apiKeyList": ["k-gem-1", "k-gem-2"], "apiBase": "https://gem.example/v1"}, name="gemini")
    await rpc_console.settings_everos_set({"section": "embedding", "fields": {"model": "m"}, "borrow_from": "gemini"})
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["embedding"]["api_key"] == "k-gem-1"


async def test_a_borrowed_key_beats_the_redaction_the_page_echoes(everos_toml, lender):
    """`model.endpoints` hands the page `****set****`, and a form that submits
    what it was shown would write that string over a working key."""
    lender({"apiKey": "sk-lent", "apiBase": "https://lender.example/v1"})
    await rpc_console.settings_everos_set(
        {
            "section": "embedding",
            "fields": {"model": "m", "api_key": "****set****", "base_url": "https://stale/v1"},
            "borrow_from": "openrouter",
        }
    )
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["embedding"]["api_key"] == "sk-lent"
    assert data["embedding"]["base_url"] == "https://lender.example/v1"


async def test_borrowing_keeps_the_section_url_when_the_lender_has_none(everos_toml, lender):
    """A provider with no address of its own must not blank an address the
    reader typed: only the key is certain to be worth copying."""
    lender({"apiKey": "sk-lent"}, name="custom")
    await rpc_console.settings_everos_set(
        {"section": "embedding", "fields": {"base_url": "https://mine/v1"}, "borrow_from": "custom"}
    )
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["embedding"]["api_key"] == "sk-lent"
    # `custom` carries a registry default address, so the borrow does have one to
    # give and it wins -- which is the same rule as every other lender. What the
    # section keeps on its own is covered by the endpoints case above, where the
    # entry supplies the address.
    assert data["embedding"]["base_url"]


async def test_borrowing_finds_a_provider_stored_under_another_spelling(everos_toml, lender):
    """`ProvidersConfig.get` is spelling-insensitive and attribute access is not.
    A provider raven carries no spec for is stored under whatever key its writer
    used, which is exactly the case the invariant in
    `test_provider_resolution_invariants` exists to keep working."""
    lender({"apiKey": "sk-hyphen", "apiBase": "https://hyph.example/v1"}, name="some-vendor")
    await rpc_console.settings_everos_set(
        {"section": "embedding", "fields": {"model": "m"}, "borrow_from": "some-vendor"}
    )
    data = tomllib.loads(everos_toml.read_text(encoding="utf-8"))
    assert data["embedding"]["api_key"] == "sk-hyphen"


async def test_borrowing_refuses_what_it_cannot_lend(everos_toml, lender):
    """Usable and lendable are different questions: a keyless local address
    satisfies the first and has nothing to answer the second with."""
    lender({"apiKey": "sk-lent"})
    with pytest.raises(ConfigValidationError, match="no such provider"):
        await rpc_console.settings_everos_set(
            {"section": "embedding", "fields": {"model": "m"}, "borrow_from": "nobody"}
        )
    lender({"apiBase": "http://127.0.0.1:11434/v1"}, name="ollama")
    with pytest.raises(ConfigValidationError, match="no api key to lend"):
        await rpc_console.settings_everos_set(
            {"section": "embedding", "fields": {"model": "m"}, "borrow_from": "ollama"}
        )


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


async def test_extension_pin_writes_roundtrip_through_raven_loader(cfg):
    from raven.config.raven import load_raven_config

    await rpc_console.settings_set({"key": "translate.model", "value": "openai/gpt-5-mini"})
    await rpc_console.settings_set({"key": "translate.provider", "value": "openai"})
    await rpc_console.settings_set({"key": "knowledge.embeddingModel", "value": "openai/text-embedding-3-small"})
    await rpc_console.settings_set({"key": "knowledge.embeddingProvider", "value": "openai"})

    loaded = load_raven_config(cfg)

    assert loaded.translate.model == "openai/gpt-5-mini"
    assert loaded.translate.provider == "openai"
    assert loaded.knowledge.embedding_model == "openai/text-embedding-3-small"
    assert loaded.knowledge.embedding_provider == "openai"


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
        r = await rpc_console.settings_set(
            {
                "key": "knowledge",
                "value": {"embeddingModel": "openai/text-embedding-3-large", "embeddingProvider": "openai"},
            }
        )

        assert r["applied"] is True
        block = _read(cfg)["knowledge"]
        assert block["embeddingModel"] == "openai/text-embedding-3-large"
        assert block["embeddingProvider"] == "openai"

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
        cfg.write_text(
            json.dumps({"knowledge": {"embedding_model": "openai/old", "embedding_provider": "openai"}}),
            encoding="utf-8",
        )

        await rpc_console.settings_set(
            {"key": "knowledge", "value": {"embeddingModel": "openai/new", "embeddingProvider": "openai"}}
        )

        block = _read(cfg)["knowledge"]
        assert set(block) == {"embedding_model", "embedding_provider"}
        assert block["embedding_model"] == "openai/new"
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
        await rpc_console.settings_set(
            {"key": "knowledge", "value": {"embeddingModel": "openai/new", "embeddingProvider": "openai"}}
        )

        assert set(_read(cfg)["knowledge"]) == {"embeddingModel", "embeddingProvider"}
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
