"""The plugin's own config surface: the vendor table and the role pins.

The table used to live in ``onboard.py``, which meant only the wizard knew which
vendors can rerank, which request shape each one wants, and that DeepInfra serves
reranking from a different address than chat. The settings page knew none of it
and wrote configurations that could not work.
"""

from __future__ import annotations

import pytest

from raven_everos.config import (
    VENDORS,
    rerank_base_url,
    rerank_protocol,
    vendor,
    vendor_supports,
)


@pytest.fixture(autouse=True)
def _no_ambient_everos_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every case with no role exported and no provenance remembered.

    ``EVEROS_<ROLE>__*`` is a real input to ``role_is_env_managed``, and both
    the binding this module does and ``set_embedding_endpoint`` leave those in
    the process. So does a developer who exports one. Without this, a case that
    assumes nothing is exported reads one answer after some other case ran and
    another on its own -- which is not pinning anything.
    """
    from raven_everos import config as cf

    for role in ("LLM", "EMBEDDING", "RERANK", "MULTIMODAL"):
        for name in ("MODEL", "BASE_URL", "API_KEY", "DIMENSIONS", "PROVIDER"):
            monkeypatch.delenv(f"EVEROS_{role}__{name}", raising=False)
    monkeypatch.setattr(cf, "_BOUND_HERE", set())


class TestVendorTable:
    def test_deepinfra_serves_reranking_from_its_own_address(self) -> None:
        assert vendor_supports("deepinfra", "rerank") is True
        assert rerank_protocol("deepinfra") == "deepinfra"
        assert (
            rerank_base_url("deepinfra", "https://api.deepinfra.com/v1/openai")
            == "https://api.deepinfra.com/v1/inference"
        )

    def test_a_vendor_that_cannot_rerank_says_so(self) -> None:
        assert vendor_supports("deepseek", "rerank") is False
        assert vendor_supports("openai", "rerank") is False

    def test_siliconflow_wants_the_vllm_request_shape(self) -> None:
        assert rerank_protocol("siliconflow") == "vllm"

    def test_an_unknown_vendor_says_nothing_rather_than_guessing(self) -> None:
        assert vendor("not-a-vendor") is None
        assert rerank_protocol("not-a-vendor") is None
        assert rerank_base_url("not-a-vendor", "https://x/v1") == "https://x/v1"

    def test_a_vendor_without_its_own_rerank_address_falls_back(self) -> None:
        assert rerank_base_url("siliconflow", "https://api.siliconflow.cn/v1") == ("https://api.siliconflow.cn/v1")

    @pytest.mark.parametrize("row", VENDORS, ids=lambda r: r["name"])
    def test_every_row_that_claims_rerank_names_its_protocol(self, row: dict) -> None:
        """A row offering rerank without a protocol would silently take EverOS's
        default (deepinfra), which is the defect this table exists to prevent."""
        if "rerank" in (row.get("supports") or ()):
            assert row.get("rerank_provider"), row["name"]


class TestWhichVendorsAreOffered:
    """The table exists to know the rerank request shape, and nothing else.

    Using it as the capability map for all four roles took the settings slots
    from every vendor raven knows down to the curated handful -- groq, moonshot,
    volcengine and forty others became unpickable for the memory LLM, which was
    never the bug being fixed. The bug was the rerank slot offering OpenAI.
    """

    def test_every_provider_raven_knows_can_serve_the_three_ordinary_roles(self) -> None:
        from raven.providers.registry import PROVIDERS
        from raven_everos.config import vendors

        offered = {row["name"] for row in vendors() if "llm" in row["supports"]}

        assert {spec.name for spec in PROVIDERS} <= offered
        for name in ("groq", "moonshot", "volcengine", "openai"):
            assert vendor_supports(name, "llm"), name
            assert vendor_supports(name, "multimodal"), name

    def test_rerank_is_offered_only_where_the_shape_can_be_named(self) -> None:
        """A vendor with no known request shape would take EverOS's default and
        post the wrong path -- silently, looking exactly like a bad model."""
        from raven_everos.config import rerank_protocol, vendors

        for row in vendors():
            if "rerank" not in row["supports"]:
                continue
            named = rerank_protocol(row["name"]) or row.get("self_host")
            assert named, f"{row['name']} offers rerank with no way to name its shape"

        assert vendor_supports("openai", "rerank") is False
        assert vendor_supports("groq", "rerank") is False


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    """A raven config raven owns, with two providers that can actually serve.

    Written as a file and pointed at through the process-wide pointer rather
    than patched per name: the resolver reads back through raven's own loader,
    which binds ``get_config_path`` itself.
    """
    import json

    from raven import home as raven_home
    from raven_everos import config as cf

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "providers": {
                    "deepinfra": {"apiKey": "sk-di", "apiBase": "https://api.deepinfra.com/v1/openai"},
                    "deepseek": {"apiKey": "sk-ds", "apiBase": "https://api.deepseek.com/v1"},
                },
                "embedding": {},
                "plugins": {"config": {"everos-memory": {"owned": True, "root": str(tmp_path / ".everos")}}},
            }
        ),
        encoding="utf-8",
    )
    raven_home.set_config_path(cfg)
    monkeypatch.setattr(cf, "everos_owned", lambda: True)
    yield cfg
    raven_home.set_config_path(None)


class TestRolePins:
    def test_a_role_pin_holds_no_credential(self, pinned) -> None:
        import json

        from raven_everos.config import set_role

        set_role("llm", model="deepseek-chat", provider="deepseek")
        slice_ = json.loads(pinned.read_text())["plugins"]["config"]["everos-memory"]
        assert slice_["llm"] == {"model": "deepseek-chat", "provider": "deepseek"}

    def test_embedding_is_written_where_it_is_read_from(self, pinned) -> None:
        """Its pin is raven's own top-level block, because a knowledge base embeds
        with it too. Writing it into the plugin slice would save and never take."""
        import json

        from raven_everos.config import role_pin, set_role

        set_role("embedding", model="BAAI/bge-m3", provider="deepinfra")
        raw = json.loads(pinned.read_text())
        assert raw["embedding"]["model"] == "BAAI/bge-m3"
        assert "embedding" not in raw["plugins"]["config"]["everos-memory"]
        assert role_pin("embedding") == ("BAAI/bge-m3", "deepinfra")

    def test_rerank_resolves_to_the_rerank_address_not_the_chat_one(self, pinned) -> None:
        from raven_everos.config import resolve_role, set_role

        set_role("rerank", model="BAAI/bge-reranker-v2-m3", provider="deepinfra")
        ep = resolve_role("rerank")
        assert ep is not None
        assert ep.base_url == "https://api.deepinfra.com/v1/inference"

    def test_a_role_pinned_to_a_provider_with_no_credential_resolves_to_none(self, pinned) -> None:
        from raven_everos.config import resolve_role, set_role

        set_role("llm", model="m", provider="groq")
        assert resolve_role("llm") is None

    def test_an_unpinned_role_resolves_to_none(self, pinned) -> None:
        from raven_everos.config import resolve_role

        assert resolve_role("multimodal") is None

    def test_clearing_a_role_removes_the_block(self, pinned) -> None:
        import json

        from raven_everos.config import clear_role, role_pin, set_role

        set_role("rerank", model="m", provider="deepinfra")
        clear_role("rerank")
        assert role_pin("rerank") is None
        assert "rerank" not in json.loads(pinned.read_text())["plugins"]["config"]["everos-memory"]

    def test_an_unknown_role_is_refused_by_name(self, pinned) -> None:
        from raven_everos.config import resolve_role, set_role

        with pytest.raises(KeyError, match="memory"):
            set_role("memory", model="m", provider="deepseek")
        with pytest.raises(KeyError):
            resolve_role("memory")


class TestOwnershipGuard:
    def test_a_write_to_a_root_raven_does_not_own_is_refused_by_name(self, pinned, monkeypatch, tmp_path) -> None:
        """The guard sits at the write primitive so a new caller cannot opt out --
        moving these writes out of everos.toml was very nearly that new caller."""
        from raven_everos import config as cf
        from raven_everos.config import EverosRootNotOwnedError, set_role

        monkeypatch.setattr(cf, "everos_owned", lambda: False)
        monkeypatch.setattr(cf, "everos_root", lambda: tmp_path / "theirs")
        with pytest.raises(EverosRootNotOwnedError) as excinfo:
            set_role("llm", model="m", provider="deepseek")
        assert str(tmp_path / "theirs") in str(excinfo.value)


class TestEverosEnv:
    def test_every_role_is_emitted_even_when_raven_holds_none(self, pinned) -> None:
        """Emitted empty, not omitted. A role raven does not hold must actively
        suppress whatever section survives in everos.toml, because raven no longer
        edits that file and a stale section would otherwise come back into force."""
        from raven_everos.config import everos_env

        env = everos_env()
        for section in ("LLM", "EMBEDDING", "RERANK", "MULTIMODAL"):
            assert f"EVEROS_{section}__MODEL" in env
            assert env[f"EVEROS_{section}__MODEL"] == ""
            assert env[f"EVEROS_{section}__API_KEY"] == ""

    def test_a_held_role_carries_its_resolved_pair(self, pinned) -> None:
        from raven_everos.config import everos_env, set_role

        set_role("llm", model="deepseek-chat", provider="deepseek")
        env = everos_env()
        assert env["EVEROS_LLM__MODEL"] == "deepseek-chat"
        assert env["EVEROS_LLM__BASE_URL"] == "https://api.deepseek.com/v1"
        assert env["EVEROS_LLM__API_KEY"] == "sk-ds"
        assert env["EVEROS_RERANK__MODEL"] == ""

    def test_rerank_carries_the_request_shape_its_vendor_wants(self, pinned) -> None:
        """EverOS's rerank.provider names a client implementation, not a vendor.
        Left unset it defaults to deepinfra, which posts to {base}/{model} -- wrong
        for every vendor that wants {base}/rerank."""
        from raven_everos.config import everos_env, set_role

        set_role("rerank", model="BAAI/bge-reranker-v2-m3", provider="deepinfra")
        env = everos_env()
        assert env["EVEROS_RERANK__PROVIDER"] == "deepinfra"
        assert env["EVEROS_RERANK__BASE_URL"] == "https://api.deepinfra.com/v1/inference"

    def test_an_unheld_rerank_says_nothing_about_the_protocol(self, pinned) -> None:
        """Saying nothing leaves the file's value. There is no sensible empty
        request shape, so this key is the one that is omitted rather than blanked."""
        from raven_everos.config import everos_env

        assert "EVEROS_RERANK__PROVIDER" not in everos_env()

    def test_an_embedding_width_travels_and_an_unpinned_one_does_not(self, pinned, tmp_path) -> None:
        """The width is the store's, not the model's: a vector written at 1024
        cannot be searched by one written at 4096. Pinned, it has to reach the
        service; unpinned, EverOS asks the endpoint rather than being told a
        number raven guessed.
        """
        import json

        from raven_everos.config import everos_env

        raw = json.loads(pinned.read_text(encoding="utf-8"))
        raw["embedding"] = {"model": "m", "provider": "deepinfra", "dimensions": 1024}
        pinned.write_text(json.dumps(raw), encoding="utf-8")
        assert everos_env()["EVEROS_EMBEDDING__DIMENSIONS"] == "1024"

        raw["embedding"] = {"model": "m", "provider": "deepinfra"}
        pinned.write_text(json.dumps(raw), encoding="utf-8")
        # Absent, not empty. This asserted `== ""` until a real spawn refused to
        # start on it: the field is an int, and to a typed field an empty string
        # is not "unset", it is invalid -- EverOS raised ValidationError and
        # exited before serving. Nothing below a real spawn could have said so.
        assert "EVEROS_EMBEDDING__DIMENSIONS" not in everos_env()

    def test_a_role_the_operator_exported_is_skipped_whole(self, pinned, monkeypatch) -> None:
        """Not emitted, not blanked. A spec non-goal promises an operator who
        exports EVEROS_* outranks raven, and blanking would break that promise
        more quietly than overwriting would."""
        from raven_everos import config as cf
        from raven_everos.config import everos_env, set_role

        set_role("llm", model="deepseek-chat", provider="deepseek")
        for key, value in (
            ("EVEROS_LLM__MODEL", "theirs"),
            ("EVEROS_LLM__BASE_URL", "https://theirs/v1"),
            ("EVEROS_LLM__API_KEY", "sk-theirs"),
        ):
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(cf, "_BOUND_HERE", set())

        env = everos_env()
        assert "EVEROS_LLM__MODEL" not in env
        assert env["EVEROS_RERANK__MODEL"] == ""

    def test_a_binding_raven_made_itself_is_not_mistaken_for_an_export(self, pinned, monkeypatch) -> None:
        """`raven gateway --restart` goes through os.execv, which keeps the
        environment. Without provenance the restarted gateway reads its own
        binding as somebody else's export and never binds again."""
        from raven_everos import config as cf
        from raven_everos.config import everos_env, set_role

        set_role("llm", model="deepseek-chat", provider="deepseek")
        keys = ("EVEROS_LLM__MODEL", "EVEROS_LLM__BASE_URL", "EVEROS_LLM__API_KEY")
        for key in keys:
            monkeypatch.setenv(key, "bound-earlier")
        monkeypatch.setattr(cf, "_BOUND_HERE", set(keys))

        assert everos_env()["EVEROS_LLM__MODEL"] == "deepseek-chat"


class TestGate:
    """`everos_role_configured` decides whether this install has long-term
    memory. Nine callers share it, and a wrong answer is memory switched off
    without a word."""

    def test_a_model_and_a_working_provider_is_configured(self, pinned) -> None:
        from raven_everos.config import everos_role_configured, set_role

        set_role("llm", model="deepseek-chat", provider="deepseek")
        assert everos_role_configured("llm") is True

    def test_a_provider_with_no_usable_credential_is_not_configured(self, pinned) -> None:
        """A model pinned to a keyless vendor is exactly the state that would
        spawn a server doomed to die building its LLM client."""
        from raven_everos.config import everos_role_configured, set_role

        set_role("llm", model="m", provider="groq")
        assert everos_role_configured("llm") is False

    def test_a_provider_that_no_longer_exists_reads_as_unconfigured(self, pinned) -> None:
        """Not as an exception. A pin outliving its provider is ordinary enough
        that it must not take down every caller of this gate."""
        import json

        from raven_everos.config import everos_role_configured, resolve_role

        raw = json.loads(pinned.read_text())
        raw["plugins"]["config"]["everos-memory"]["llm"] = {"model": "m", "provider": "went-away"}
        pinned.write_text(json.dumps(raw), encoding="utf-8")
        assert resolve_role("llm") is None
        assert everos_role_configured("llm") is False

    def test_nothing_pinned_is_not_configured(self, pinned) -> None:
        from raven_everos.config import everos_role_configured

        assert everos_role_configured("rerank") is False

    def test_a_role_the_operator_exported_counts_as_configured(self, pinned, monkeypatch) -> None:
        """raven cannot read their shell, but it can see the endpoint is there."""
        from raven_everos import config as cf
        from raven_everos.config import everos_role_configured

        for key, value in (
            ("EVEROS_RERANK__MODEL", "theirs"),
            ("EVEROS_RERANK__BASE_URL", "https://theirs/v1"),
            ("EVEROS_RERANK__API_KEY", "sk-theirs"),
        ):
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(cf, "_BOUND_HERE", set())
        assert everos_role_configured("rerank") is True


# The shape measured on the author's machine 2026-09-21, keys redacted. Written
# from the real file rather than imagined: the rerank section's address is the
# reason the vendor lookup needs a host level at all, and an invented fixture
# would have had it match the chat address and proved nothing.
_LEGACY_TOML = """
[llm]
model = "anthropic/claude-sonnet-4-5"
api_key = "sk-or-legacy"
base_url = "https://openrouter.ai/api/v1"

[embedding]
model = "Qwen/Qwen3-Embedding-4B"
api_key = "sk-di-legacy"
base_url = "https://api.deepinfra.com/v1/openai"
timeout_seconds = 30.0
batch_size = 10

[rerank]
provider = "deepinfra"
model = "Qwen/Qwen3-Reranker-4B"
api_key = "sk-di-legacy"
base_url = "https://api.deepinfra.com/v1/inference"
timeout_seconds = 30.0

[multimodal]
model = "deepseek/deepseek-v4-flash"
api_key = "sk-ds-legacy"
base_url = "https://api.deepseek.com/v1"

[memory]
root = "~/.everos"
"""


@pytest.fixture
def legacy(tmp_path):
    """An install as it stands before the move: everything in the toml.

    raven has openrouter and deepseek configured and **no deepinfra row at
    all**, which is the state measured on the author's machine and the one A11
    is about. Returns ``(config_path, write_toml)``.
    """
    import json

    from raven import home as raven_home

    root = tmp_path / ".everos"
    root.mkdir()

    def write(text: str = _LEGACY_TOML) -> None:
        (root / "everos.toml").write_text(text, encoding="utf-8")

    write()
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "providers": {
                    "openrouter": {"apiKey": "sk-or-legacy", "apiBase": "https://openrouter.ai/api/v1"},
                    "deepseek": {"apiKey": "sk-ds-legacy", "apiBase": "https://api.deepseek.com/v1"},
                },
                "plugins": {"config": {"everos-memory": {"owned": True, "root": str(root)}}},
            }
        ),
        encoding="utf-8",
    )
    raven_home.set_config_path(cfg)
    yield cfg, write
    raven_home.set_config_path(None)


def _read_cfg(path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


class TestMigratingTheRolesOutOfTheToml:
    """An upgrade has to move these without being asked.

    Until it runs, raven holds no pin for any role and the environment it sends
    the memory service blanks all four -- long-term memory stops. A remedy the
    user has to know to run (`doctor --fix`) is not a migration.
    """

    def test_a_role_whose_vendor_raven_already_holds_is_pinned_to_it(self, legacy) -> None:
        from raven_everos.config import migrate_roles, role_pin

        cfg, _ = legacy
        migrate_roles()

        assert role_pin("llm") == ("anthropic/claude-sonnet-4-5", "openrouter")
        assert _read_cfg(cfg)["plugins"]["config"]["everos-memory"]["llm"]["provider"] == "openrouter"

    def test_a_vendor_raven_has_no_row_for_gets_one(self, legacy) -> None:
        """DeepInfra is in neither raven's registry nor its schema, and the
        wizard let a key be typed for it anyway. Two of four roles on this
        machine -- so a migration that could only match existing rows would drop
        half of them."""
        from raven_everos.config import migrate_roles, role_pin

        cfg, _ = legacy
        assert "deepinfra" not in _read_cfg(cfg)["providers"], "the fixture must start without it"

        migrate_roles()

        assert _read_cfg(cfg)["providers"]["deepinfra"]["apiKey"] == "sk-di-legacy"
        assert role_pin("rerank") == ("Qwen/Qwen3-Reranker-4B", "deepinfra")

    def test_a_vendor_named_only_by_the_table_still_resolves(self, legacy) -> None:
        """Nothing raven holds names api.deepinfra.com until that row exists, so
        the wizard's own table is the last thing that can -- and the rerank
        section's /v1/inference is not even the address the table lists first."""
        from raven_everos.config import migrate_roles, role_pin

        migrate_roles()

        assert role_pin("rerank") == ("Qwen/Qwen3-Reranker-4B", "deepinfra")

    def test_a_row_that_already_holds_a_key_is_not_overwritten(self, legacy) -> None:
        """The key on raven's row is the live one; the old file's copy is
        whatever it was before the last rotation."""
        import json

        from raven_everos.config import migrate_roles

        cfg, _ = legacy
        raw = _read_cfg(cfg)
        raw["providers"]["deepinfra"] = {
            "apiKey": "sk-di-rotated",
            "apiBase": "https://api.deepinfra.com/v1/openai",
        }
        cfg.write_text(json.dumps(raw), encoding="utf-8")

        migrate_roles()

        assert _read_cfg(cfg)["providers"]["deepinfra"]["apiKey"] == "sk-di-rotated"

    def test_embedding_lands_in_ravens_own_block(self, legacy) -> None:
        """Not this plugin's slice: a knowledge base embeds with the same
        endpoint and never speaks to the memory service."""
        from raven_everos.config import migrate_roles

        cfg, _ = legacy
        migrate_roles()

        raw = _read_cfg(cfg)
        assert raw["embedding"]["model"] == "Qwen/Qwen3-Embedding-4B"
        assert raw["embedding"]["provider"] == "deepinfra"
        assert "embedding" not in raw["plugins"]["config"]["everos-memory"]

    def test_running_it_twice_changes_nothing(self, legacy) -> None:
        """It runs on every start, so "again" has to mean nothing -- and a second
        pass that rewrote a pin would undo a model the person changed since."""
        import json

        from raven_everos.config import migrate_roles

        cfg, _ = legacy
        migrate_roles()
        once = json.dumps(_read_cfg(cfg), sort_keys=True)

        migrate_roles()

        assert json.dumps(_read_cfg(cfg), sort_keys=True) == once

    def test_an_unnameable_endpoint_leaves_the_role_unset_and_says_so(self, legacy) -> None:
        """A guessed vendor sends memory's traffic to the wrong endpoint, which
        is worse than a slot somebody fills in once."""
        from raven_everos.config import migrate_roles, role_pin

        _cfg, write = legacy
        write('[llm]\nmodel = "m"\napi_key = "k"\nbase_url = "https://nobody-knows-this.example/v1"\n')

        notices = migrate_roles()

        assert role_pin("llm") is None
        assert any("llm" in n and "nobody-knows-this" in n for n in notices)

    def test_a_template_section_with_no_key_is_not_a_configuration(self, legacy) -> None:
        """The shipped template seeds every role with a real model name and an
        empty key. Migrating that reports a role as configured that has never
        worked."""
        from raven_everos.config import migrate_roles, role_pin

        _cfg, write = legacy
        write('[llm]\nmodel = "qwen/qwen3.8-flash"\napi_key = ""\nbase_url = ""\n')

        migrate_roles()

        assert role_pin("llm") is None

    def test_one_role_that_cannot_be_written_does_not_take_the_others_down(self, legacy) -> None:
        """The writers validate, and an old file is exactly where a bad pair
        lives. `set_embedding_endpoint` refuses a model that cannot embed, and
        letting that escape left llm migrated, rerank and multimodal not, and the
        binding that follows never run -- a half-migrated install with long-term
        memory off and nothing saying why.
        """
        from raven_everos.config import migrate_roles, role_pin

        _cfg, write = legacy
        write(_LEGACY_TOML.replace('model = "Qwen/Qwen3-Embedding-4B"', 'model = "anthropic/claude-sonnet-4-5"'))

        notices = migrate_roles()

        assert role_pin("embedding") is None
        assert any("embedding" in n for n in notices), notices
        # The point of the case: the roles after it still landed.
        assert role_pin("llm") is not None
        assert role_pin("rerank") is not None
        assert role_pin("multimodal") is not None

    def test_a_root_the_user_manages_is_left_alone(self, legacy) -> None:
        """raven records that root's address and never edits its config, so the
        sections in it are theirs and stay where they are."""
        import json

        from raven_everos.config import migrate_roles, role_pin

        cfg, _ = legacy
        raw = _read_cfg(cfg)
        raw["plugins"]["config"]["everos-memory"]["owned"] = False
        cfg.write_text(json.dumps(raw), encoding="utf-8")

        assert migrate_roles() == []
        assert role_pin("llm") is None
        assert "deepinfra" not in _read_cfg(cfg)["providers"]

    def test_a_self_hosted_rerank_keeps_the_shape_the_old_file_named(self, legacy) -> None:
        """`rerank.provider` in the toml was never a vendor -- it is the request
        shape. For a vendor the table can answer for it is dropped; for somebody's
        own box it is the only record of what that box serves."""
        import json

        from raven_everos.config import everos_env, migrate_roles

        cfg, write = legacy
        raw = _read_cfg(cfg)
        raw["providers"]["custom"] = {"apiKey": "k-box", "apiBase": "http://box.lan:8000/v1"}
        cfg.write_text(json.dumps(raw), encoding="utf-8")
        write(
            '[rerank]\nprovider = "vllm"\nmodel = "bge-reranker"\n'
            'api_key = "k-box"\nbase_url = "http://box.lan:8000/v1"\n'
        )

        migrate_roles()

        assert everos_env()["EVEROS_RERANK__PROVIDER"] == "vllm"


class TestTheEnvironmentEverosCanActuallyLoad:
    """`everos_env()` has one consumer that matters: EverOS's own settings.

    Every case above reads the dict raven builds. None of them builds what EverOS
    builds from it -- and a value can be perfectly reasonable as a dict entry and
    still refuse to load. It did: `EVEROS_EMBEDDING__DIMENSIONS=""` for a role
    with no width pinned is an empty string handed to an int field, and the
    server exited with a ValidationError before serving anything, while the unit
    test asserting `== ""` stayed green.

    So this case builds the real Settings, in a subprocess because
    `load_settings` is cached process-wide and the environment is global.
    """

    @staticmethod
    def _load_in_subprocess(env: dict[str, str], root) -> tuple[int, str]:
        import os
        import subprocess
        import sys

        program = (
            "import sys\n"
            "from everos.config.settings import load_settings\n"
            "load_settings.cache_clear()\n"
            "try:\n"
            "    s = load_settings()\n"
            "except Exception as exc:\n"
            "    print(f'{type(exc).__name__}: {exc}'.replace(chr(10), ' ')[:300])\n"
            "    sys.exit(1)\n"
            "print(f'ok llm={s.llm.model!r} emb={s.embedding.model!r} dims={s.embedding.dimensions!r}')\n"
        )
        base = {k: v for k, v in os.environ.items() if not k.startswith("EVEROS_")}
        out = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            env={**base, "EVEROS_ROOT": str(root), **env},
        )
        return out.returncode, (out.stdout + out.stderr).strip()

    def _root(self, tmp_path):
        import shutil

        from everos.entrypoints.cli.commands.init_cmd import _EVEROS_TEMPLATE

        root = tmp_path / "everos"
        root.mkdir()
        shutil.copy2(_EVEROS_TEMPLATE, root / "everos.toml")
        return root

    def test_a_role_with_no_width_pinned_still_loads(self, pinned, tmp_path) -> None:
        """The failure that got here: an unpinned width used to travel as an
        empty string, which is not "unset" to an int field."""
        import json

        from raven_everos.config import everos_env

        raw = json.loads(pinned.read_text(encoding="utf-8"))
        raw["embedding"] = {"model": "bge-m3", "provider": "deepinfra"}
        pinned.write_text(json.dumps(raw), encoding="utf-8")

        code, output = self._load_in_subprocess(everos_env(), self._root(tmp_path))

        assert code == 0, output
        assert "emb='bge-m3'" in output, output

    def test_a_pinned_width_reaches_the_settings(self, pinned, tmp_path) -> None:
        import json

        from raven_everos.config import everos_env

        raw = json.loads(pinned.read_text(encoding="utf-8"))
        raw["embedding"] = {"model": "bge-m3", "provider": "deepinfra", "dimensions": 1024}
        pinned.write_text(json.dumps(raw), encoding="utf-8")

        code, output = self._load_in_subprocess(everos_env(), self._root(tmp_path))

        assert code == 0, output
        assert "dims=1024" in output, output

    def test_every_role_raven_holds_nothing_for_still_loads(self, pinned, tmp_path) -> None:
        """The blanking that clears a role has to be loadable too, or clearing
        one in the UI takes the whole service down instead."""
        from raven_everos.config import everos_env

        code, output = self._load_in_subprocess(everos_env(), self._root(tmp_path))

        assert code == 0, output


class TestWhatTheTomlStillSays:
    """Two sentences an operator looking at everos.toml is owed.

    A section raven now serves is dead text that still reads like configuration:
    editing it changes nothing and there is nowhere to find that out. A section
    raven has no record of has not moved yet, which happens at the next memory
    start -- the difference between "wait" and "reconfigure".

    Reported through the backend's health rather than written by doctor: the
    move belongs to the start path, and doctor may not write.
    """

    @staticmethod
    def _notes(monkeypatch, *, in_file, pinned):
        from raven_everos import config as cf

        monkeypatch.setattr(cf, "everos_section", lambda s: {"model": "m"} if s in in_file else {})
        monkeypatch.setattr(cf, "role_pin", lambda s: ("m", "openrouter") if s in pinned else None)
        return cf.everos_toml_role_notes()

    def test_a_section_raven_now_serves_is_named_as_overridden(self, monkeypatch) -> None:
        out = self._notes(monkeypatch, in_file={"llm"}, pinned={"llm"})

        assert any("overrides it" in n and "[llm]" in n for n in out), out

    def test_a_section_not_moved_yet_says_when_it_will_be(self, monkeypatch) -> None:
        out = self._notes(monkeypatch, in_file={"rerank"}, pinned=set())

        assert any("next time long-term memory starts" in n and "[rerank]" in n for n in out), out

    def test_a_file_with_no_role_sections_says_nothing(self, monkeypatch) -> None:
        assert self._notes(monkeypatch, in_file=set(), pinned={"llm"}) == []
