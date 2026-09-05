"""Catalog-shape tests for the LLM provider registry and backend classes.

These pin the *current* shape so that adding / removing a provider spec or a
concrete backend class trips a test instead of silently drifting.
"""

from __future__ import annotations

import pytest

from raven.providers.base import LLMProvider
from raven.providers.common_models import common_models_for
from raven.providers.registry import PROVIDERS, find_by_name

# The Confluence "Providers" page claims 19 providers. This pins the current
# registry so any drift (add/remove a ProviderSpec) is caught here.
EXPECTED_PROVIDER_NAMES = {
    "custom",
    "azure_openai",
    "openrouter",
    "orcarouter",
    "aihubmix",
    "siliconflow",
    "volcengine",
    "anthropic",
    "openai",
    "openai_codex",
    "github_copilot",
    "deepseek",
    "gemini",
    "zai",
    "dashscope",
    "moonshot",
    "minimax",
    "minimax_global",
    "minimax_cn",
    "hosted_vllm",
    "ollama_chat",
    "groq",
}


def test_registry_has_exactly_22_providers() -> None:
    assert len(PROVIDERS) == 22
    assert len(EXPECTED_PROVIDER_NAMES) == 22


def test_registry_provider_name_set_is_pinned() -> None:
    assert {spec.name for spec in PROVIDERS} == EXPECTED_PROVIDER_NAMES


def test_provider_names_are_unique() -> None:
    names = [spec.name for spec in PROVIDERS]
    assert len(names) == len(set(names))


def test_a_specs_route_prefix_is_a_provider_litellm_knows() -> None:
    """The prefix decides where LiteLLM sends the request.

    Defaulting it to our own name only works while that name is one LiteLLM
    carries; a name it does not know would route every request nowhere.
    """
    import litellm

    known = {str(getattr(p, "value", p)) for p in litellm.provider_list}
    for spec in PROVIDERS:
        if spec.model_prefix:
            assert spec.model_prefix in known, spec.name


def test_via_driver_names_another_vendor_that_litellm_actually_speaks() -> None:
    """A borrowed driver must be somebody else's, and must exist.

    Naming your own driver is a no-op that invites the two to drift apart;
    naming a vendor LiteLLM has never heard of routes nowhere. Neither can be
    caught by reading the field -- only by checking it against LiteLLM.
    """
    from raven.providers.litellm_setup import import_litellm

    known = {str(getattr(p, "value", p)) for p in import_litellm().provider_list}
    for spec in PROVIDERS:
        if not spec.via_driver:
            continue
        assert spec.via_driver not in spec.route_names, f"{spec.name}: via_driver is one of its own names"
        assert spec.via_driver in known, f"{spec.name}: LiteLLM does not know driver {spec.via_driver!r}"


def test_a_providers_name_is_litellms_spelling_whenever_litellm_has_one() -> None:
    """One name per provider: ours is LiteLLM's wherever LiteLLM has one.

    This is what removed the reconciliation layer. A spec whose own name is
    absent from LiteLLM while an alias of it is present means the rename went
    the wrong way, and every prefix comparison downstream inherits two answers.
    """
    from raven.providers.litellm_setup import import_litellm

    known = {str(getattr(p, "value", p)) for p in import_litellm().provider_list}
    for spec in PROVIDERS:
        stale = [a for a in spec.name_aliases if a in known and spec.name not in known]
        assert not stale, f"{spec.name}: LiteLLM knows {stale} but not {spec.name!r} -- adopt LiteLLM's spelling"


def test_registry_and_schema_declare_the_same_providers() -> None:
    # A provider needs both a ProviderSpec (env keys, prefixes, detection) and a
    # ProvidersConfig field (where its credentials live). Miss either half and it
    # is unconfigurable or unreachable, with nothing else failing.
    from raven.config.schema import ProvidersConfig

    assert {spec.name for spec in PROVIDERS} == set(ProvidersConfig.model_fields)


# Direct providers seeded in the model picker (issue #100). Each must expose a
# non-empty default_model drawn from its curated shortlist, so the onboarding
# fallback and the picker stay in sync and no provider defaults to empty.
_SEEDED_DIRECT_PROVIDERS = [
    "deepseek",
    "openai",
    "anthropic",
    "gemini",
    "zai",
    "dashscope",
    "groq",
    "minimax_global",
    "minimax_cn",
]


@pytest.mark.parametrize("slug", _SEEDED_DIRECT_PROVIDERS)
def test_seeded_provider_default_model_in_shortlist(slug: str) -> None:
    default = find_by_name(slug).default_model
    assert default, f"{slug} has no default_model"
    assert default in common_models_for(slug)


def _concrete_provider_subclasses() -> set[type]:
    """All non-abstract LLMProvider subclasses defined in raven.providers."""
    # Import each backend module so its subclass is registered on LLMProvider.
    import raven.providers.azure_openai_provider  # noqa: F401
    import raven.providers.endpoint_rotor  # noqa: F401
    import raven.providers.litellm_provider  # noqa: F401
    import raven.providers.minimax_oauth_provider  # noqa: F401
    import raven.providers.openai_codex_provider  # noqa: F401
    import raven.providers.per_model_provider  # noqa: F401

    seen: set[type] = set()
    stack = list(LLMProvider.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if getattr(cls, "__abstractmethods__", frozenset()):
            continue
        # LazyProvider is a proxy over a real backend, not a backend itself, and
        # is not imported above -- so its presence via __subclasses__ depends on
        # ambient imports from other tests. Skip it to keep the set deterministic.
        if cls.__module__ == "raven.providers.lazy":
            continue
        if cls.__module__.startswith("raven.providers"):
            seen.add(cls)
    return seen


def test_exactly_six_concrete_backend_classes() -> None:
    # This asserts class existence only, not the dispatch wiring.
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.endpoint_rotor import EndpointRotorProvider
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.providers.minimax_oauth_provider import MiniMaxOAuthProvider
    from raven.providers.openai_codex_provider import OpenAICodexProvider
    from raven.providers.per_model_provider import PerModelProvider

    expected = {
        LiteLLMProvider,
        AzureOpenAIProvider,
        OpenAICodexProvider,
        MiniMaxOAuthProvider,
        PerModelProvider,
        # Multi-endpoint rotation/failover wrapper: a real backend in dispatch
        # terms -- make_provider returns it for a section that resolves to
        # more than one endpoint.
        EndpointRotorProvider,
    }
    assert _concrete_provider_subclasses() == expected
    for cls in expected:
        assert issubclass(cls, LLMProvider)


def test_every_gateway_prefixes_the_models_it_routes() -> None:
    """The prefix is what sends the request to the gateway rather than the vendor.

    Reading the raw registry field instead of the resolved prefix dropped it for
    any gateway whose name is already LiteLLM's, so a stored "anthropic/claude-x"
    went out unprefixed and LiteLLM handed the gateway's key to Anthropic.
    """
    from raven.providers.litellm_provider import LiteLLMProvider

    for spec in PROVIDERS:
        if not spec.is_gateway:
            continue
        provider = LiteLLMProvider(api_key="K", provider_name=spec.name, default_model="probe-model")
        resolved = provider._resolve_model("probe-model")
        assert resolved.startswith(f"{spec.model_prefix}/"), f"{spec.name}: {resolved}"


def test_orcarouter_gateway_is_detected_by_key_and_base() -> None:
    """OrcaRouter is a gateway: detected by its key prefix and base keyword."""
    from raven.providers.registry import find_gateway

    by_key = find_gateway(api_key="sk-orca-abc123")
    assert by_key is not None and by_key.name == "orcarouter"

    by_base = find_gateway(api_base="https://api.orcarouter.ai/v1")
    assert by_base is not None and by_base.name == "orcarouter"

    by_name = find_gateway(provider_name="orcarouter")
    assert by_name is not None and by_name.name == "orcarouter"


def test_orcarouter_wire_id_reaches_litellm_openai_driver() -> None:
    """The stored "orcarouter/<ns>/<model>" id must resolve through LiteLLM.

    LiteLLM carries no "orcarouter" provider, so the wire form goes out under the
    OpenAI driver with the full namespace intact -- the gateway then routes on
    that namespace. This is what makes the request reach the right upstream.
    """
    import pytest
    from litellm import get_llm_provider

    # A bare stored id is not routable on its own: LiteLLM has no "orcarouter"
    # provider, so it raises rather than sending the request somewhere.
    with pytest.raises(Exception):
        get_llm_provider(model="orcarouter/openai/gpt-5.5")

    # The wire form, by contrast, resolves -- the OpenAI driver accepts the
    # namespaced id and the gateway receives the full namespace. (This version of
    # LiteLLM reports the resolved route in the provider slot; the point is that
    # it resolves at all, which the bare stored id cannot.)
    for wired in (
        "openai/openai/gpt-5.5",
        "openai/anthropic/claude-haiku-4.5",
        "openai/orcarouter/auto",
    ):
        provider, _, _, _ = get_llm_provider(model=wired)
        assert provider != "orcarouter", f"{wired}: did not route through OpenAI driver"


def test_a_failed_catalogue_read_is_not_cached_for_the_life_of_the_process() -> None:
    """The index is cached; a failure to build it must not be.

    ``lru_cache`` remembers whatever the call returned, so an empty result from a
    transient import failure would leave every provider that has no curated
    shortlist showing no models at all, for as long as the process runs, with no
    way to retry. Eleven providers depend on this index for their candidates.
    """
    from functools import lru_cache

    from raven.providers import common_models

    calls = {"n": 0}
    real = common_models._cached_chat_models_by_provider

    # Wrapped in a genuine lru_cache rather than a stub with an inert cache_clear:
    # what is under test is that the next call retries, and a stubbed clear made
    # the test pass either way. For this case the retry comes from lru_cache
    # storing only successful returns -- the guarantee the code leans on.
    @lru_cache(maxsize=1)
    def _fail_then_succeed() -> dict[str, tuple[str, ...]]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient import failure")
        return {"deepseek": ("deepseek/deepseek-chat",)}

    common_models._cached_chat_models_by_provider = _fail_then_succeed  # type: ignore[assignment]
    try:
        assert common_models._litellm_chat_models_by_provider() == {}
        assert common_models._litellm_chat_models_by_provider() == {"deepseek": ("deepseek/deepseek-chat",)}
        assert calls["n"] == 2, "the second read never happened, so the failure was cached"
    finally:
        common_models._cached_chat_models_by_provider = real  # type: ignore[assignment]
        real.cache_clear()


def test_an_empty_catalogue_is_not_cached_either() -> None:
    """An import that succeeds but yields nothing is the same hazard.

    It is what a LiteLLM whose table has not loaded yet returns, and caching it
    is indistinguishable from caching the failure above.
    """
    from functools import lru_cache

    from raven.providers import common_models

    calls = {"n": 0}
    real = common_models._cached_chat_models_by_provider

    @lru_cache(maxsize=1)
    def _empty_then_full() -> dict[str, tuple[str, ...]]:
        calls["n"] += 1
        return {} if calls["n"] == 1 else {"deepseek": ("deepseek/deepseek-chat",)}

    common_models._cached_chat_models_by_provider = _empty_then_full  # type: ignore[assignment]
    try:
        assert common_models._litellm_chat_models_by_provider() == {}
        assert common_models._litellm_chat_models_by_provider() != {}
        assert calls["n"] == 2, "the second read never happened, so the empty result was cached"
    finally:
        common_models._cached_chat_models_by_provider = real  # type: ignore[assignment]
        real.cache_clear()


def test_a_model_family_quirk_is_declared_not_branched_on_in_the_factory() -> None:
    """The factory builds providers; it does not know which models need what.

    OpenRouter's qwen routing flag lived as an `if` there, because a fact about
    one model family behind one gateway had nowhere else to go. A second such
    fact would have meant a second branch.
    """
    from pathlib import Path

    from raven.cli import _helpers
    from raven.providers.capabilities import wire_overrides

    assert wire_overrides("openrouter", "openrouter/qwen/qwen3.7-max") == {"reasoning": {"enabled": False}}
    assert wire_overrides("openrouter", "openrouter/anthropic/claude-opus-4-8") == {}
    assert wire_overrides("anthropic", "anthropic/qwen-lookalike") == {}, "another provider must not inherit it"

    source = Path(_helpers.__file__).read_text(encoding="utf-8")
    assert "qwen" not in source, "the model-family branch is back in the factory"


def test_the_bundled_label_snapshot_is_packaged() -> None:
    """A data file the wheel omits is missing only for installed users.

    The build include list is a whitelist of patterns, so a new non-Python asset
    is absent from the wheel by default -- and every test here runs from a source
    checkout, where it is present either way.
    """
    import fnmatch
    import tomllib
    from pathlib import Path

    from raven.providers.catalog import SNAPSHOT

    root = Path(__file__).resolve().parents[1]
    assert SNAPSHOT.exists(), "the snapshot itself is missing; run scripts/refresh_models_dev_snapshot.py"

    patterns = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]["include"]
    relative = str(SNAPSHOT.relative_to(root))
    assert any(fnmatch.fnmatch(relative, p) for p in patterns), f"{relative} matches no build include pattern"


#: Providers whose models the snapshot labels today. A refresh that drops one is
#: a regression no total can show: the catalogue grew from 16 providers to 37
#: while a criterion change silently took every label off three gateways, and
#: both the provider count and the model count still went up. Taking a name off
#: this list means arguing the vendor is gone, not noticing a test went red.
LABELLED_PROVIDERS = frozenset(
    {
        "aihubmix",
        "anthropic",
        "azure_openai",
        "dashscope",
        "deepseek",
        "gemini",
        "github_copilot",
        "groq",
        "minimax",
        "minimax_cn",
        "minimax_global",
        "moonshot",
        "openai",
        "openrouter",
        "siliconflow",
        "zai",
    }
)


def _packaged_snapshot() -> dict:
    import json

    from raven.providers.catalog import SNAPSHOT

    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_the_snapshot_labels_every_provider_it_labelled_before() -> None:
    snapshot = _packaged_snapshot()
    missing = sorted(LABELLED_PROVIDERS - set(snapshot))
    assert not missing, f"the refresh dropped labels for: {missing}"

    empty = sorted(name for name in LABELLED_PROVIDERS if not snapshot[name].get("models"))
    assert not empty, f"present but carrying no models: {empty}"

    # Present, non-empty, and every row unlabelled renders exactly like being
    # absent -- the id as its own label -- so presence alone is not the property.
    unlabelled = sorted(
        name for name in LABELLED_PROVIDERS if not any(model.get("name") for model in snapshot[name]["models"].values())
    )
    assert not unlabelled, f"present but no model carries a name: {unlabelled}"


def test_the_snapshot_records_where_it_came_from() -> None:
    """Provenance a reader can act on, not a date they have to trust.

    The catalogue is refreshed from someone else's repository; without the commit
    it was built at, "the snapshot is stale" is unanswerable and a regenerated
    file is unreviewable.
    """
    source = _packaged_snapshot().get("_source")
    assert source, "no _source; regenerate with scripts/refresh_models_dev_snapshot.py"
    assert source.keys() >= {"repo", "ref", "sha"}, source
    assert len(source["sha"]) == 40, source["sha"]


def test_the_snapshot_carries_labels_and_cost_and_nothing_that_shapes_a_request() -> None:
    """The split this file's module docstring rests on, asserted.

    Cost prices a finished call and a stale figure costs an inaccurate total.
    A context window sizes trimming and a capability flag picks a wire shape, so
    both must come from the table that also routes -- carrying them here would
    make a community-maintained file able to cause a wrong request.
    """
    fields: set[str] = set()
    for name, entry in _packaged_snapshot().items():
        if name.startswith("_"):
            continue
        assert set(entry) == {"models"}, f"{name} carries more than models: {sorted(entry)}"
        for model in entry["models"].values():
            fields |= set(model)
    assert fields <= {"name", "description", "cost"}, f"snapshot carries request-shaping fields: {sorted(fields)}"


def test_a_model_in_the_snapshot_is_described_and_one_outside_it_still_renders() -> None:
    from raven.providers.catalog import describe

    known = describe("anthropic", "claude-sonnet-4-6")
    assert known.described
    assert known.label == "Claude Sonnet 4.6"
    assert known.ref == "anthropic/claude-sonnet-4-6"

    # A local deployment serves whatever the user put there; no catalogue can
    # know it, and the picker must still have something to show.
    unknown = describe("hosted_vllm", "my-finetune-v3")
    assert not unknown.described
    assert unknown.label == "my-finetune-v3"
    assert unknown.ref == "hosted-vllm/my-finetune-v3"


def test_a_stored_id_round_trips_through_describe() -> None:
    """Describing an already-qualified id must not re-qualify it."""
    from raven.providers.catalog import describe

    assert describe("anthropic", "anthropic/claude-sonnet-4-6").label == "Claude Sonnet 4.6"


def test_vendor_id_normalizes_provider_spelling_like_merge_key() -> None:
    """``_vendor_id``'s fallback branch used to compare ``head == provider``
    with ``head`` already normalized by ``split_model_id`` but ``provider``
    passed through as-is -- unlike ``wire.merge_key``, which normalizes both
    sides of the same comparison. A provider spelled with a hyphen against a
    model prefix spelled with an underscore diverged: the strip was skipped
    and the whole model string came back as if it named no vendor at all.
    """
    from raven.providers.catalog import _vendor_id
    from raven.providers.wire import merge_key

    provider, model = "nano-gpt", "nano_gpt/DeepSeek-V3"
    assert _vendor_id(provider, model) == "DeepSeek-V3"
    assert merge_key(provider, model) == "nano_gpt::deepseek-v3"


def test_what_the_user_states_about_a_model_beats_the_catalogue() -> None:
    """The user naming their own deployment beats a catalogue that never heard of it.

    Only presentation. The overlay also carried `context`/`max_output` once,
    justified as fixing token accounting -- nothing read them, and that
    accounting already has `agents.defaults.contextWindowTokens`.
    """
    from raven.config.schema import ModelOverlay
    from raven.providers.catalog import SOURCE_OVERLAY, describe

    unknown = describe(
        "hosted_vllm",
        "my-finetune-v3",
        overlay=ModelOverlay(label="Our finetune", description="tuned on support tickets"),
    )
    assert unknown.described
    assert unknown.source == SOURCE_OVERLAY
    assert unknown.label == "Our finetune"

    # Stating one fact must not blank the others the catalogue knows.
    partial = describe("anthropic", "claude-sonnet-4-6", overlay=ModelOverlay(label="Sonnet (ours)"))
    assert partial.label == "Sonnet (ours)"
    assert partial.description


def test_an_overlay_written_bare_matches_the_qualified_id() -> None:
    """Overlays are matched by identity, so a pre-contract spelling still applies."""
    from raven.providers.wire import merge_key

    assert merge_key("anthropic", "claude-sonnet-4-6") == merge_key("anthropic", "anthropic/claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# LiteLLM checks us, not the other way round
# ---------------------------------------------------------------------------

#: Providers whose ``env_key`` deliberately differs from LiteLLM's, with the
#: argument. Adding a name here is a claim, not a way to make a test pass.
_ENV_KEY_EXEMPT: dict[str, str] = {
    # A gateway speaking OpenAI's API: its key travels in OPENAI_API_KEY because
    # that is the variable the driver handling the request reads. LiteLLM names
    # the vendor's own variable, which nothing here sets.
    "volcengine": "OPENAI_API_KEY",
    # OrcaRouter is reached through the OpenAI driver the same way; the key is
    # still declared under its own name and passed as an explicit api_key kwarg.
    "orcarouter": "ORCAROUTER_API_KEY",
    # A local deployment takes an address, not a key. LiteLLM answers with the
    # address variable, which is a different field of ours.
    "ollama_chat": "OLLAMA_API_KEY",
}


def _litellm_env_keys(spec) -> list[str]:
    """The variables LiteLLM would look for, or [] when it has no answer.

    Asked with the environment emptied of credentials, because the answer is
    phrased as *missing* keys: on a machine that already exports the variable,
    LiteLLM reports nothing missing and this test would quietly skip the provider
    it was written to check. Coverage must not depend on whose laptop it runs on.
    """
    import os
    from unittest import mock

    from raven.providers.litellm_setup import import_litellm

    stripped = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_API_BASE", "_KEY"))}
    try:
        with mock.patch.dict(os.environ, stripped, clear=True):
            info = import_litellm().validate_environment(model=f"{spec.model_prefix or spec.name}/probe-model")
    except Exception:
        return []
    return list(info.get("missing_keys") or [])


@pytest.mark.parametrize("spec", [s for s in PROVIDERS if s.env_key], ids=lambda s: s.name)
def test_our_env_key_is_the_one_litellm_will_read(spec) -> None:
    """The registry was written by copying LiteLLM; this makes LiteLLM check it.

    A vendor renaming its variable is a silent break otherwise -- the key is set,
    the request goes out without it, and the error is about authentication rather
    than about a stale table. Where LiteLLM has no answer there is nothing to
    compare and the case is skipped rather than assumed correct.
    """
    expected = _litellm_env_keys(spec)
    if not expected:
        pytest.skip("LiteLLM does not name an environment variable for this provider")

    if spec.name in _ENV_KEY_EXEMPT:
        assert spec.env_key == _ENV_KEY_EXEMPT[spec.name], (
            f"{spec.name}: exempted with a stated value that no longer matches the registry"
        )
        return

    assert spec.env_key in expected, f"{spec.name}: we set {spec.env_key!r}, LiteLLM reads one of {expected}"


def test_the_env_key_exemption_list_has_no_stale_entries() -> None:
    """An exemption whose divergence has gone away is a claim nobody rechecked."""
    stale = []
    for name, declared in _ENV_KEY_EXEMPT.items():
        spec = find_by_name(name)
        expected = _litellm_env_keys(spec)
        if expected and declared in expected:
            stale.append(f"{name}: LiteLLM now names {declared!r} too -- drop the exemption")
    assert not stale, "\n".join(stale)
