"""Characterization baseline for the storage-form to wire-form conversion.

A stored model id is not what goes on the wire. That rule used to live in seven
places -- two branches of ``LiteLLMProvider._resolve_model``, Codex's own prefix
strip, Azure's URL builder, and three copies outside the providers package. It
now lives only in ``providers.wire``; this file is the baseline that made the
collapse safe, by pinning the wire output across it.

Regenerated deliberately since, for fixes the storage contract required rather
than for drift. Eleven of the 149 entries moved when this landed, in three groups -- stated in full because "the wire output does
not move" is this file's whole claim, and a regeneration whose reasons cover only
part of the delta is that claim quietly weakened:

* **Azure (3)** -- every stored id now names its provider, and that name has to
  come off before the id becomes a URL path segment;
* **local deployments (4)** -- the same change made "hosted-vllm/x" and
  "ollama/x" real stored ids, and the gateway branch double-prefixed them;
* **providers reached through another vendor's driver (4)** -- `custom/x` and
  `siliconflow/x` used to go out as "openai/custom/x", carrying a segment the
  upstream does not serve. Canonicalizing the gateway branch fixed this as a side
  effect, which is why it was not in the first two reasons: it was found by
  diffing against `main`, not by predicting it.

So these tests are a snapshot, not a specification. They assert that today's
answer for every provider and every shape of model id is byte-for-byte what it
was before the refactor -- including the answers that are arguably wrong. A
defect found here gets reported, not fixed: a fix and a refactor in the same
step leave nothing to bisect against, which is how the previous attempt at this
came to be rolled back.

The corpus is synthetic and derived from each spec's own fields rather than from
LiteLLM's catalogue, so a dependency bump cannot churn the baseline.

Regenerate deliberately, never to make a red test green::

    RAVEN_UPDATE_WIRE_BASELINE=1 uv run pytest tests/test_provider_wire_model.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from raven.providers.registry import PROVIDERS, ProviderSpec, public_model_prefix

BASELINE = Path(__file__).parent / "data" / "wire_model_baseline.json"

#: Stand-in for a vendor's own model id. Deliberately free of any keyword that
#: the registry matches on, so a case only exercises the prefix rule under test.
PLAIN = "zz-probe-1"


def _corpus(spec: ProviderSpec) -> list[str]:
    """Every shape of stored model id this provider has to answer for.

    Built from the spec's own fields so the set grows with the registry rather
    than needing to be maintained beside it.
    """
    ids = [
        PLAIN,
        f"{spec.name}/{PLAIN}",
        f"{public_model_prefix(spec)}/{PLAIN}",
        # A model id whose vendor part itself contains a slash: "openai/gpt-oss"
        # is Groq's own name for a model, and a gateway that strips one leading
        # segment must not keep only the tail.
        f"vendorx/{PLAIN}",
        f"{spec.name}/vendorx/{PLAIN}",
    ]
    ids += [f"{alias}/{PLAIN}" for alias in spec.name_aliases]
    if spec.via_driver:
        ids.append(f"{spec.via_driver}/{PLAIN}")
    ids += [f"{skip}{PLAIN}" for skip in spec.skip_prefixes]
    if spec.default_model:
        ids.append(spec.default_model)
    if spec.keywords:
        ids.append(f"{spec.keywords[0]}-probe")
    # Deduplicate while keeping the order stable across runs.
    return list(dict.fromkeys(ids))


def _litellm_answers() -> dict[str, dict[str, str]]:
    """What ``_resolve_model`` returns today, per provider, per stored id.

    The provider is built with an empty key on purpose: ``_setup_env`` is
    skipped, so snapshotting cannot leak a credential into ``os.environ`` and
    the answers stay independent of the machine running them.
    """
    from raven.providers.litellm_provider import LiteLLMProvider

    out: dict[str, dict[str, str]] = {}
    for spec in PROVIDERS:
        provider = LiteLLMProvider(api_key="", default_model=PLAIN, provider_name=spec.name)
        out[spec.name] = {model: provider._resolve_model(model) for model in _corpus(spec)}
    return out


def _codex_answers() -> dict[str, str]:
    from raven.providers.openai_codex_provider import _strip_model_prefix

    spec = next(s for s in PROVIDERS if s.name == "openai_codex")
    extra = ["openai-codex/gpt-5.3-codex", "openai_codex/gpt-5.3-codex"]
    return {model: _strip_model_prefix(model) for model in [*_corpus(spec), *extra]}


def _azure_answers() -> dict[str, str]:
    """The URL Azure builds, which embeds the model id as a deployment name.

    Azure used to take the id verbatim, so a stored id carrying a prefix put
    that prefix into the URL path. Once every stored id names its provider, that
    is every Azure id -- so the provider's own name comes off here. The baseline
    for this section was regenerated for that change; it is a fix the storage
    contract required, not drift.
    """
    from raven.providers.azure_openai_provider import AzureOpenAIProvider

    spec = next(s for s in PROVIDERS if s.name == "azure_openai")
    provider = AzureOpenAIProvider(api_key="k", api_base="https://example.openai.azure.com")
    return {model: provider._build_chat_url(model) for model in _corpus(spec)}


def _current() -> dict[str, Any]:
    return {
        "litellm_resolve_model": _litellm_answers(),
        "codex_strip_model_prefix": _codex_answers(),
        "azure_chat_url": _azure_answers(),
    }


@pytest.fixture(scope="module")
def baseline() -> dict[str, Any]:
    """The recorded answers. Regenerated only when explicitly asked.

    It used to regenerate whenever the file was missing, which made deleting it
    a way to turn this suite green: the snapshot would be rewritten from
    whatever the code currently does and then compared against itself. A
    characterization test that can be satisfied by removing its own evidence
    is not one.
    """
    if os.environ.get("RAVEN_UPDATE_WIRE_BASELINE"):
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(_current(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if not BASELINE.exists():
        pytest.fail(
            f"{BASELINE.relative_to(Path(__file__).parents[1])} is missing. It is the record this "
            "suite checks against, not an artifact it produces. Restore it from version control, or "
            "regenerate deliberately with RAVEN_UPDATE_WIRE_BASELINE=1 and say why in the diff."
        )
    return json.loads(BASELINE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_litellm_wire_form_is_unchanged(spec: ProviderSpec, baseline: dict[str, Any]) -> None:
    """Every stored id this provider answers for still resolves to the same wire id."""
    recorded = baseline["litellm_resolve_model"].get(spec.name)
    assert recorded is not None, f"no baseline for {spec.name}; regenerate deliberately"

    from raven.providers.litellm_provider import LiteLLMProvider

    provider = LiteLLMProvider(api_key="", default_model=PLAIN, provider_name=spec.name)
    current = {model: provider._resolve_model(model) for model in _corpus(spec)}
    assert current == recorded


def test_codex_wire_form_is_unchanged(baseline: dict[str, Any]) -> None:
    assert _codex_answers() == baseline["codex_strip_model_prefix"]


def test_azure_wire_form_is_unchanged(baseline: dict[str, Any]) -> None:
    assert _azure_answers() == baseline["azure_chat_url"]


def test_the_baseline_covers_every_registered_provider(baseline: dict[str, Any]) -> None:
    """A provider added without a baseline entry would refactor unobserved."""
    missing = [s.name for s in PROVIDERS if s.name not in baseline["litellm_resolve_model"]]
    assert not missing, f"regenerate the baseline for: {missing}"


def test_resolving_a_model_does_not_write_credentials_into_the_environment() -> None:
    """The snapshot must not depend on -- or alter -- the machine it runs on.

    ``_setup_env`` exports the provider's key under its env var, and a snapshot
    that tripped it would both leak and become order-dependent.
    """
    before = dict(os.environ)
    _litellm_answers()
    assert dict(os.environ) == before


# ---------------------------------------------------------------------------
# Inbound: one spelling written, any spelling removes it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_both_write_paths_store_the_same_id(spec: ProviderSpec) -> None:
    """The TUI and the wizard must write one model one way.

    They did not: for most providers the TUI stored a bare id while the wizard
    stored a qualified one, so picking the same model in the two places put it in
    the list twice.
    """
    from raven.cli.onboard_commands import _format_model_for_provider
    from raven.tui_rpc.methods.model import _stored_spelling

    assert _stored_spelling(spec.name, PLAIN) == _format_model_for_provider(spec.name, spec, PLAIN)


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_a_stored_id_always_names_its_provider(spec: ProviderSpec) -> None:
    """Written bare, an id is claimed by keyword matching instead of by its owner."""
    from raven.providers.wire import stored_model_id

    stored = stored_model_id(spec.name, PLAIN)
    assert "/" in stored, f"{spec.name}: stored {stored!r} with nothing naming the provider"


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_storing_an_already_qualified_id_is_idempotent(spec: ProviderSpec) -> None:
    """Re-storing what was stored must not stack a second prefix."""
    from raven.providers.wire import stored_model_id

    once = stored_model_id(spec.name, PLAIN)
    assert stored_model_id(spec.name, once) == once


@pytest.mark.parametrize("spec", PROVIDERS, ids=lambda s: s.name)
def test_a_bare_id_and_its_qualified_form_are_one_model(spec: ProviderSpec) -> None:
    """Configs written before ids carried a provider still match the new form."""
    from raven.providers.wire import merge_key, stored_model_id

    assert merge_key(spec.name, PLAIN) == merge_key(spec.name, stored_model_id(spec.name, PLAIN))
