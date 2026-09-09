"""Invariants of provider resolution, and guards that keep them single-sourced.

Provider resolution answers a handful of questions -- how a model id splits into
a route prefix, which names refer to a provider, whether a prefix or a keyword
decides. Each question used to be answered independently at several call sites,
the answers drifted, and fixes then landed at one site while the others kept the
old behavior. That produced a routing defect where a gateway's key was sent to
the vendor named in the model id.

So these tests come in two kinds. The sweeps assert each invariant holds for
*every* registered provider rather than for the case that was last fixed.

The source guards are line scans, and they are tripwires rather than proofs: a
deliberate rewrite (`.partition("/")`, a spliced attribute name, an aliased
import) walks past them. What they catch is the shape that actually recurred --
the same spelling copied to a new call site -- and they name the single-source
function in the failure message, which is the part a future reader needs.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from raven.config.schema import Config
from raven.providers.registry import (
    PROVIDERS,
    ProviderSpec,
    canonical_provider_name,
    find_by_model,
    normalize_provider_name,
    split_model_id,
)

RAVEN_ROOT = Path(__file__).resolve().parents[1] / "raven"
REGISTRY = RAVEN_ROOT / "providers" / "registry.py"


def _production_files() -> list[Path]:
    return sorted(p for p in RAVEN_ROOT.rglob("*.py") if p != REGISTRY)


def _rel(path: Path) -> str:
    return str(path.relative_to(RAVEN_ROOT.parent))


# ---------------------------------------------------------------------------
# Sweeps: every provider, not just the last one fixed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_every_route_name_resolves_back_to_its_own_provider(spec: ProviderSpec) -> None:
    """Each prefix a provider answers to must resolve to that same provider.

    A prefix that resolves elsewhere means a request carrying it is served with
    another vendor's credentials.
    """
    # Non-empty first: `for prefix in spec.route_names` with an empty set runs
    # the body zero times, which pytest scores as a pass -- so gutting
    # `route_names` entirely would leave this and three sibling sweeps green.
    assert spec.route_names, f"{spec.name}: answers to no prefix at all"
    for prefix in spec.route_names:
        assert find_by_model(f"{prefix}/probe-model") is spec, f"{spec.name}: prefix {prefix!r} resolved elsewhere"


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_the_prefix_litellm_is_actually_sent_resolves_somewhere(spec: ProviderSpec) -> None:
    """The prefix that goes on the wire must resolve to a provider.

    Iterating `route_names` alone cannot catch that set shrinking -- the loop
    just gets shorter. This anchors on `model_prefix`, the string LiteLLM is
    handed, so dropping it from `route_names` leaves an id nobody claims: the
    shape of the defect where "hosted_vllm/..." was vLLM's model to the config
    matcher and a stranger's to the registry.
    """
    if not spec.model_prefix:
        pytest.skip("provider bypasses LiteLLM and takes no route prefix")
    resolved = find_by_model(f"{spec.model_prefix}/probe-model")
    assert resolved is not None, f"{spec.name}: nothing claims {spec.model_prefix!r}"
    if spec.via_driver:
        # The wire prefix is the driver's vendor, so it must resolve to THAT
        # vendor -- never back to this one, whose key would then answer for it.
        assert resolved is not spec, f"{spec.name}: claims its borrowed driver {spec.via_driver!r}"
        assert normalize_provider_name(resolved.name) == spec.model_prefix
    else:
        assert resolved is spec, f"{spec.model_prefix!r} resolved to {resolved.name}"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("hosted_vllm/llama3", "hosted_vllm"),
        ("vllm/llama3", "hosted_vllm"),
        ("ollama_chat/qwen", "ollama_chat"),
        ("ollama/qwen", "ollama_chat"),
        ("zhipu/glm-4.6", "zai"),
        ("zai/glm-4.6", "zai"),
        ("openai/gpt-4o", "openai"),
        ("anthropic/claude-opus-4-5", "anthropic"),
    ],
)
def test_litellm_spellings_and_former_names_resolve_to_their_owner(model: str, expected: str) -> None:
    """Concrete ids whose resolution the sweeps cannot pin by themselves.

    "openai/..." must stay OpenAI's even though it is also the driver
    SiliconFlow and AiHubMix are reached through.
    """
    resolved = find_by_model(model)
    assert resolved is not None and resolved.name == expected, f"{model} -> {resolved and resolved.name}"


def test_a_prefix_naming_a_specless_vendor_resolves_to_nobody() -> None:
    """Not to the vendor whose name happens to appear later in the id.

    LiteLLM serves DeepInfra without Raven carrying a spec for it, and the id it
    routes on mentions DeepSeek. Answering "DeepSeek" would put DeepInfra's key
    in DEEPSEEK_API_KEY and rewrite the model id.
    """
    assert find_by_model("deepinfra/deepseek-ai/DeepSeek-V3") is None


def test_route_names_never_overlap_between_providers() -> None:
    """No prefix may name two providers, or resolution order would decide.

    LiteLLM's name for a vendor is shared by every provider reached through that
    vendor's driver ("openai" fronts SiliconFlow and AiHubMix alike), which is
    why `route_names` claims it only when it is not another provider's name.
    """
    owners: dict[str, str] = {}
    clashes = []
    for spec in PROVIDERS:
        for prefix in spec.route_names:
            if prefix in owners:
                clashes.append(f"{prefix!r}: {owners[prefix]} vs {spec.name}")
            owners[prefix] = spec.name
    assert not clashes, "prefixes claimed by more than one provider: " + "; ".join(clashes)


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_route_names_are_normalized_and_include_the_provider_name(spec: ProviderSpec) -> None:
    assert normalize_provider_name(spec.name) in spec.route_names
    assert spec.route_names, f"{spec.name}: answers to no prefix at all"
    for prefix in spec.route_names:
        assert prefix == normalize_provider_name(prefix), f"{spec.name}: {prefix!r} is not normalized"


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_a_prefixed_id_is_claimed_only_by_the_provider_it_names(spec: ProviderSpec) -> None:
    """A prefix decides alone: no other provider may claim the id via keywords.

    This is the rule whose duplication caused the original defect. A model id
    that mentions two vendors ("deepinfra/deepseek-ai/DeepSeek-V3") belongs to
    the one in the prefix; matching on the keyword would post DeepInfra's key
    to DeepSeek's endpoint.
    """
    assert spec.route_names, f"{spec.name}: answers to no prefix at all"
    for prefix in spec.route_names:
        model = f"{prefix}/deepseek-ai/kimi-claude-gpt-glm"
        claimants = [s.name for s in PROVIDERS if s.claims(model)]
        assert claimants == [spec.name], f"{model!r} claimed by {claimants}"


def test_match_provider_decides_by_asking_the_spec_not_by_re_deriving_the_rule(monkeypatch) -> None:
    """The config matcher must route its decision through `ProviderSpec.claims`.

    Comparing final answers cannot detect a second implementation here: the
    matcher has enough downstream recovery (alias-aware section lookup,
    passthrough, local fallback) to reach the right provider even when its
    prefix rule is wrong, so an inlined `prefix == spec.name` scores green on
    every provider. What actually needs asserting is that the rule is consulted
    rather than restated -- so this watches the call.
    """
    observed: list[tuple[str, str]] = []
    original = ProviderSpec.claims

    def spy(self: ProviderSpec, model: str) -> bool:
        observed.append((self.name, model))
        return original(self, model)

    monkeypatch.setattr(ProviderSpec, "claims", spy)
    config = Config.model_validate({"providers": {"anthropic": {"apiKey": "sk-probe"}}})
    config._match_provider("anthropic/claude-opus-4-5")

    assert observed, "_match_provider re-derived the prefix rule instead of asking the spec"
    assert ("anthropic", "anthropic/claude-opus-4-5") in observed


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_a_configured_provider_answers_for_every_prefix_it_owns(spec: ProviderSpec) -> None:
    """Outcome check: whatever prefix a provider answers to must reach it."""
    assert spec.route_names, f"{spec.name}: answers to no prefix at all"
    for prefix in spec.route_names:
        model = f"{prefix}/probe-model"
        raw = {"apiKey": "sk-probe"} if not spec.is_oauth else {}
        if spec.is_local:
            raw = {"apiBase": "http://localhost:8000/v1"}
        config = Config.model_validate({"providers": {spec.name: raw}})
        _, matched = config._match_provider(model)
        assert matched == spec.name, f"{model!r} -> {matched}, registry says {spec.name}"


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_each_gateway_puts_its_own_prefix_on_a_bare_model_id(spec: ProviderSpec) -> None:
    """A gateway must put its own prefix on a bare model id.

    Without it LiteLLM routes on whatever the id names, so the gateway's key
    goes to that vendor instead.
    """
    if not spec.is_gateway:
        pytest.skip("not a gateway")
    from raven.providers.litellm_provider import LiteLLMProvider

    provider = LiteLLMProvider(api_key="K", provider_name=spec.name, default_model="probe-model")
    resolved = provider._resolve_model("probe-model")
    assert resolved.startswith(f"{spec.model_prefix}/"), f"{spec.name}: {resolved}"


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_a_named_but_unconfigured_vendor_never_falls_back_to_a_direct_vendor(spec: ProviderSpec) -> None:
    """An id naming vendor A must not be served by direct vendor B's key.

    Only a gateway or a local deployment may answer for a vendor it does not
    name, because those route whatever they are handed.
    """
    if spec.is_gateway or spec.is_local or spec.is_oauth or not spec.env_key:
        pytest.skip("gateways, local deployments and OAuth providers are not direct-vendor fallbacks")
    config = Config.model_validate({"providers": {spec.name: {"apiKey": "sk-probe"}}})
    for other in PROVIDERS:
        if other is spec or other.is_gateway or other.is_local or other.is_oauth:
            continue
        # Both ways an id can name a vendor. Testing only the prefixed form
        # leaves the keyword door untested -- and a bare "kimi-k2.5" named
        # Moonshot just as plainly while being served by whoever had a key.
        ids = [f"{sorted(other.route_names)[0]}/probe-model"]
        ids += [kw for kw in other.keywords if not spec.matches_keywords(kw)]
        for model_id in ids:
            _, matched = config._match_provider(model_id)
            assert matched != spec.name, f"{model_id!r} was served by {spec.name}'s credentials"


# ---------------------------------------------------------------------------
# Source guards: the invariant stays implemented in exactly one place
# ---------------------------------------------------------------------------


def test_only_the_registry_reads_the_raw_via_driver_field() -> None:
    """`via_driver` names the vendor whose API is spoken, not this provider.

    It is an input to the wire prefix, and `model_prefix` is the answer. A caller
    reading the raw field sees "" for the sixteen providers that speak for
    themselves, and treating that as "no prefix" is how a gateway's key came to
    be posted to the vendor named in the model id. The benchmark harness is
    exempt: it has to know whether an endpoint of our own is required, which is
    exactly what a borrowed driver decides.
    """
    exempt = {"benchmarks/pinchbench/direct/raven_executor.py"}
    offenders = [
        f"{_rel(path)}:{i}"
        for path in _production_files()
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if "via_driver" in line and not line.lstrip().startswith("#") and _rel(path) not in exempt
    ]
    assert not offenders, "read spec.model_prefix instead of the raw field: " + ", ".join(offenders)


def test_no_module_outside_the_registry_splits_a_model_id_by_hand() -> None:
    """Prefix parsing lives in `split_model_id`.

    Hand-rolled splits disagreed on case and on the hyphen/underscore spelling,
    so an id written one way missed a provider configured the other way.
    """
    offenders = []
    for path in _production_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#") or 'split("/", 1)[0]' not in stripped:
                continue
            if "model" not in line.lower():
                continue  # splitting a URL or a mime type, not a model id
            offenders.append(f"{_rel(path)}:{i}")
    assert not offenders, "use split_model_id(): " + ", ".join(offenders)


def test_no_module_outside_the_registry_rebuilds_a_providers_name_set() -> None:
    """Which names refer to a provider is `spec.route_names`.

    Rebuilt sets drifted: some counted LiteLLM's name for the vendor, some did
    not, so "hosted_vllm/..." was vLLM's model to one caller and a stranger's
    to another.
    """
    offenders = [
        f"{_rel(path)}:{i}"
        for path in _production_files()
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if "name_aliases" in line and "*" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, "use spec.route_names: " + ", ".join(offenders)


def test_find_by_keywords_is_imported_only_where_it_cannot_place_credentials() -> None:
    """`find_by_keywords` ignores the prefix, so the spec it returns may be a
    vendor other than the one the request is routed to. It is safe for asking
    what a model can do (prompt caching, token accounting) and unsafe for
    deciding where a key or an endpoint goes. Widening this list means arguing
    the new caller does not place credentials.
    """
    allowed = {
        "raven/providers/litellm_provider.py",  # caching + param quirks, after routing is settled
        "raven/token_wise/cache_optimizer.py",
        "raven/token_wise/system_and_tail_cache.py",
        "raven/config/schema.py",  # asks only whether an id names a vendor at all
    }
    importers = {
        _rel(path)
        for path in _production_files()
        if any(
            isinstance(node, ast.ImportFrom) and any(alias.name == "find_by_keywords" for alias in node.names)
            for node in ast.walk(ast.parse(path.read_text()))
        )
    }
    assert importers <= allowed, f"unreviewed credential-unsafe callers: {sorted(importers - allowed)}"


def test_provider_config_is_looked_up_only_through_get() -> None:
    """`ProvidersConfig.get` is the only spelling-insensitive lookup.

    A provider configured under an extra key, or under a camelCase or
    hyphenated spelling, is invisible to attribute access -- and providers Raven
    carries no spec for are exactly the ones stored that way.
    """
    offenders = []
    for path in _production_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if "getattr(" in line and ".providers," in line:
                offenders.append(f"{_rel(path)}:{i}")
    assert not offenders, "use config.providers.get(name): " + ", ".join(offenders)


# ---------------------------------------------------------------------------
# The primitives themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("openai", "openai"),
        ("nano-gpt", "nano_gpt"),
        ("  OpenRouter  ", "openrouter"),
        ("GITHUB-COPILOT", "github_copilot"),
    ],
)
def test_normalize_provider_name_folds_case_and_hyphens(raw: str, expected: str) -> None:
    assert normalize_provider_name(raw) == expected


def test_normalize_provider_name_leaves_capitals_joined() -> None:
    """Splitting on capitals would mangle a one-word name.

    "OpenRouter" is one vendor whose field is `openrouter`, while "azureOpenai"
    is `azure_openai` -- nothing in the string distinguishes them, so camelCase
    keys are matched by camelCasing the snake name instead (`ProvidersConfig.get`).
    """
    assert normalize_provider_name("OpenRouter") == "openrouter"
    assert normalize_provider_name("azureOpenai") == "azureopenai"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic/claude-opus-4-5", ("anthropic", "claude-opus-4-5")),
        ("openrouter/anthropic/claude-sonnet-4-5", ("openrouter", "anthropic/claude-sonnet-4-5")),
        ("nano-gpt/gpt-4o", ("nano_gpt", "gpt-4o")),
        ("kimi-k2.5", ("", "kimi-k2.5")),
        ("", ("", "")),
    ],
)
def test_split_model_id_normalizes_the_prefix_and_keeps_the_rest(model: str, expected: tuple[str, str]) -> None:
    assert split_model_id(model) == expected


def test_canonical_provider_name_normalizes_names_it_does_not_know() -> None:
    """A vendor with no spec still has to match its config section, which is
    written in the underscored form."""
    assert canonical_provider_name("zhipu") == "zai"
    assert canonical_provider_name("nano-gpt") == "nano_gpt"


@pytest.mark.parametrize(
    "spelling",
    ["nano_gpt", "nanoGpt", "nano-gpt"],
)
def test_a_passthrough_section_is_found_under_any_spelling(spelling: str) -> None:
    config = Config.model_validate({"providers": {spelling: {"apiKey": "sk-probe"}}})
    assert config.providers.get("nano_gpt") is not None
    _, matched = config._match_provider("nano_gpt/gpt-4o")
    assert matched == "nano_gpt"


def test_the_litellm_name_snapshot_matches_the_installed_litellm() -> None:
    """The snapshot exists so reporting need not import LiteLLM; it must be true.

    Drift in either direction is a defect, which is why this asserts equality
    rather than containment. A name the snapshot has and LiteLLM does not would
    let a typo through as a configurable vendor; a name LiteLLM has and the
    snapshot does not would hide a working provider from `provider list` and
    from the startup gate, sending a configured user back into the wizard.

    Regenerate the snapshot when a LiteLLM bump fails this.
    """
    from raven.providers.litellm_provider_names import LITELLM_PROVIDER_NAMES
    from raven.providers.litellm_setup import import_litellm

    installed = {str(getattr(p, "value", p)) for p in import_litellm().provider_list}
    assert LITELLM_PROVIDER_NAMES == installed, (
        f"snapshot is stale: missing {sorted(installed - LITELLM_PROVIDER_NAMES)}, "
        f"extra {sorted(LITELLM_PROVIDER_NAMES - installed)}"
    )


def test_reporting_paths_answer_provider_names_without_importing_litellm() -> None:
    """`provider list` and the startup gate must not pay the LiteLLM import.

    Importing it costs about two seconds, and these paths only render what is
    already configured. This is the property the snapshot exists to provide, so
    it is asserted directly rather than inferred from the snapshot's contents.
    """
    import subprocess
    import sys

    probe = (
        "import sys, json, pathlib, tempfile\n"
        "d = pathlib.Path(tempfile.mkdtemp())\n"
        "cfg = d / 'config.json'\n"
        "cfg.write_text(json.dumps({'providers': {'mistral': {'apiKey': 'k'}, 'typovendor': {'apiKey': 'k'}}}))\n"
        "from raven.config.update_providers import list_providers\n"
        "names = [row['name'] for row in list_providers(config_path=cfg)]\n"
        "print(json.dumps({'litellm': 'litellm' in sys.modules, 'names': names}))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["litellm"] is False, "list_providers imported litellm"
    assert "mistral" in result["names"], "a LiteLLM vendor went missing from the report"
    assert "typovendor" not in result["names"], "a typo was reported as a provider"


def test_every_provider_construction_site_passes_the_users_model_overrides() -> None:
    """A per-model override must apply on whichever path builds the provider.

    `model_overrides` was added to `LiteLLMProvider` and wired into two of the
    three places that construct one, which is how a user setting a mandated
    temperature found it honoured by the agent and ignored by the evolver. The
    per-endpoint builder is exempt: it inherits the fallback's overrides rather
    than reading config itself.
    """
    exempt = {"raven/providers/per_model_provider.py"}
    offenders = []
    for path in _production_files():
        if _rel(path) in exempt:
            continue
        source = path.read_text()
        if "LiteLLMProvider(" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "LiteLLMProvider":
                continue
            if not any(kw.arg == "model_overrides" for kw in node.keywords):
                offenders.append(f"{_rel(path)}:{node.lineno}")
    assert not offenders, "pass model_overrides from config here: " + ", ".join(offenders)


@pytest.mark.parametrize(
    ("configured", "sent"),
    [
        ("anthropic/claude-opus-4-5", "openai/claude-opus-4-5"),
        ("deepseek-ai/DeepSeek-V3", "openai/DeepSeek-V3"),
        ("claude-3", "openai/claude-3"),
        # The vendor's own id contains a slash; only the routing segment goes.
        ("groq/openai/gpt-oss-120b", "openai/gpt-oss-120b"),
        ("openrouter/anthropic/claude-x", "openai/anthropic/claude-x"),
    ],
)
def test_a_prefix_stripping_gateway_drops_one_segment_not_all_but_the_last(configured: str, sent: str) -> None:
    """AiHubMix wants the vendor's bare id, which is not the last path segment.

    Keeping only the tail truncated any id that carried a slash of its own, so
    the gateway was asked for a model that does not exist under that name.
    """
    from raven.providers.litellm_provider import LiteLLMProvider

    provider = LiteLLMProvider(api_key="K", provider_name="aihubmix", default_model="probe")
    assert provider._resolve_model(configured) == sent
