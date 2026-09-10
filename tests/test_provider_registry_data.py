"""The bundled registry's reader: what it folds together, and what it refuses to.

The data itself is asserted in ``test_provider_catalog.py`` (it is the same three
files those contract tests police). This file is about the resolution: a
provider's row over the canonical model, the curated layer over both, and the
shortlist that comes out the other end.
"""

from __future__ import annotations

import json

import pytest

from raven.providers import registry_data
from raven.providers.registry_data import CAPABILITIES, MODALITIES, curated_for, provider_metadata, row_for


def test_a_reseller_reads_the_vendors_row_without_carrying_a_copy() -> None:
    """One model, one description, however many gateways front it.

    The single-file snapshot stored each reseller's copy separately, so the same
    model could be described two ways depending on which column it was picked
    from. The canonical id is what makes them one row.
    """
    direct = row_for("anthropic", "claude-opus-5")
    gateway = row_for("openrouter", "anthropic/claude-opus-5")
    assert direct is not None and gateway is not None
    assert direct.model_id == gateway.model_id == "anthropic/claude-opus-5"
    assert direct.name == gateway.name
    assert direct.capabilities == gateway.capabilities


def test_a_curated_row_lends_tags_to_a_model_its_provider_never_listed() -> None:
    """A Copilot seat publishes no catalogue; the model it serves is still known.

    The whole point of naming three ids: ``apiModelId`` is what the seat answers
    to, ``modelId`` is the model Anthropic describes, and the pairing is what a
    person had to state because no upstream does.
    """
    row = row_for("github_copilot", "claude-opus-4.5")
    assert row is not None
    assert row.model_id == "anthropic/claude-opus-4-5"
    assert "function-call" in row.capabilities
    assert row.owned_by == "anthropic"


def test_the_shortlist_keeps_the_spelling_its_metadata_is_filed_under() -> None:
    """Two providers declare their own underscored prefix, and it is load-bearing.

    ``github_copilot/gpt-4.1`` resolves a context window from LiteLLM's table;
    the canonical ``github-copilot/gpt-4.1`` resolves none, because that table is
    keyed by the underscored name. A shortlist that handed the picker the
    canonical spelling would silently drop the window off every row.
    """
    from raven.providers.common_models import common_models_for

    assert "github_copilot/gpt-4o" in common_models_for("github_copilot")
    assert "ollama_chat/llama3.1" in common_models_for("ollama_chat")
    # Everywhere else the derived spelling is the right one, prefix and all.
    assert common_models_for("openrouter")[0].startswith("openrouter/")
    assert "minimax-global/MiniMax-M3" in common_models_for("minimax_global")


def test_the_shortlist_comes_back_in_the_order_it_was_ranked() -> None:
    """The file is sorted for a stable diff; the shortlist is not sorted at all.

    Rank is the curation -- the first model in a provider's list is the one it
    recommends -- so a reader that took file order would quietly reorder every
    picker on the next refresh.
    """
    rows = curated_for("anthropic")
    assert [row.rank for row in rows] == sorted(row.rank for row in rows if row.rank is not None)
    assert rows[0].api_model_id == "claude-opus-5"


def test_every_provider_spec_default_model_is_in_its_own_shortlist() -> None:
    """A default nobody offers is a default the picker cannot show as chosen."""
    from raven.providers.common_models import common_models_for
    from raven.providers.registry import PROVIDERS

    for spec in PROVIDERS:
        shortlist = common_models_for(spec.name)
        if spec.default_model and shortlist:
            assert spec.default_model in shortlist, spec.name


def test_every_provider_key_in_the_data_is_spelled_the_way_a_lookup_asks_for_it() -> None:
    """No shipped key may differ from its own normalised form.

    Every reader reaches this data through `canonical_provider_name`, which
    underscores, so a hyphen in a key is a row nothing can ever find. 593
    nano-gpt rows shipped that way -- a quarter of the file, all of them priced
    -- and nothing caught it, because the provider tests iterate `ProviderSpec`
    slugs, which by construction contain no hyphen.

    Asserted against the files rather than the loaded index so it holds whatever
    the reader does with the keys afterwards.
    """
    from raven.providers.registry import normalize_provider_name

    offenders: list[str] = []
    root = registry_data.DATA

    doc = json.loads((root / "provider-models.json").read_text(encoding="utf-8"))
    for array in ("overrides", "curated"):
        for row in doc.get(array) or []:
            key = row.get("providerId") or ""
            if key and normalize_provider_name(key) != key:
                offenders.append(f"provider-models.json:{array}:{key}")

    doc = json.loads((root / "providers.json").read_text(encoding="utf-8"))
    for row in doc.get("providers") or []:
        key = row.get("id") or ""
        if key and normalize_provider_name(key) != key:
            offenders.append(f"providers.json:{key}")

    doc = json.loads((root / "models.json").read_text(encoding="utf-8"))
    for row in doc.get("models") or []:
        key = row.get("providerId") or ""
        if key and normalize_provider_name(key) != key:
            offenders.append(f"models.json:{key}")

    assert not offenders, f"keys no lookup can produce: {sorted(set(offenders))[:10]}"


def test_a_hyphenated_vendor_still_reaches_its_rows() -> None:
    """The case that went wrong, named. nano-gpt is the one vendor in the file
    whose upstream name carries a hyphen, and it is reachable only because the
    key is written underscored."""
    from raven.providers.registry import canonical_provider_name

    assert registry_data.catalogue_for(canonical_provider_name("nano-gpt"))
    assert registry_data.provider_metadata(canonical_provider_name("nano-gpt")).get("name")


def test_an_unknown_tag_is_dropped_rather_than_handed_to_a_surface() -> None:
    """A vocabulary the UIs cannot draw is filtered at the door.

    The alternative is a name reaching an icon table that has no entry for it,
    which renders as a gap the reader reads as "no capability" -- a wrong fact
    rather than a missing one.
    """
    row = registry_data._row(
        "acme",
        "m",
        "acme/m",
        {"capabilities": ["function-call", "telepathy"], "inputModalities": ["text", "smell"]},
        None,
    )
    assert row.capabilities == ("function-call",)
    assert row.input_modalities == ("text",)


def test_tags_come_back_in_the_vocabularys_order_not_the_files() -> None:
    """Two models with the same tags must draw the same icon row.

    Ordered by the file, a refresh that re-sorted one row's array would reshuffle
    its icons against an identical neighbour's, which reads as a difference.
    """
    row = registry_data._row(
        "acme",
        "m",
        "acme/m",
        {"capabilities": ["file-input", "reasoning", "function-call"]},
        None,
    )
    assert row.capabilities == ("function-call", "reasoning", "file-input")


def test_a_price_is_flattened_to_the_shape_its_callers_already_read() -> None:
    from raven.providers.catalog import model_cost

    cost = model_cost("anthropic/claude-opus-5")
    assert cost and cost["input"] > 0 and cost["output"] > 0


def test_a_damaged_file_costs_labels_and_not_startup(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The failure mode that matters: a bad install must still run.

    Every loader folds a read error into an empty answer, so the picker falls
    back to ids. Raising here would take down the gateway over a data file that
    only decides how a list reads.
    """
    broken = tmp_path / "models.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(registry_data, "MODELS_FILE", broken)
    monkeypatch.setattr(registry_data, "PROVIDER_MODELS_FILE", tmp_path / "absent.json")
    registry_data.reset_cache()
    try:
        assert row_for("anthropic", "claude-opus-5") is None
        assert curated_for("anthropic") == ()
    finally:
        registry_data.reset_cache()


def test_the_provider_row_carries_links_and_no_way_to_reach_the_endpoint() -> None:
    """``ProviderSpec`` owns addressing; this file owns what a person clicks.

    A base URL here would be a second answer to "where do requests go", which is
    the one question a display file must never be able to answer.
    """
    row = provider_metadata("anthropic")
    assert row["name"]
    assert row["metadata"]["website"]["docs"]
    assert not {"api", "apiBase", "baseUrl", "env"} & set(row)


def test_the_vocabularies_the_reader_enforces_match_the_generator() -> None:
    """One list, two files. A generator that learns a tag the reader drops emits
    data no surface can draw, and the mismatch is invisible in both files."""
    import ast
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "refresh_provider_registry.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    literals = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"CAPABILITIES", "KEPT_MODALITIES"}
    }
    assert set(literals["CAPABILITIES"]) == set(CAPABILITIES)
    assert set(literals["KEPT_MODALITIES"]) <= set(MODALITIES)


def test_regenerating_would_not_rewrite_the_curated_layer() -> None:
    """The array a person maintains is copied through, not derived.

    Folded into the generated rows, the first refresh reads its own deltas back
    as hand-written ones and no upstream correction can reach them again -- which
    is exactly what the first attempt at this did.
    """
    from raven.providers.registry_data import PROVIDER_MODELS_FILE

    payload = json.loads(PROVIDER_MODELS_FILE.read_text(encoding="utf-8"))
    assert payload["curated"], "the shortlist is the curated layer; an empty one is a lost file"
    generated_keys = {(row["providerId"], row["apiModelId"]) for row in payload["overrides"]}
    for row in payload["curated"]:
        assert "rank" in row or "modelId" in row, row
        # A curated row for a pairing upstream does carry states only the extra.
        if (row["providerId"], row["apiModelId"]) in generated_keys:
            assert not {"capabilities", "inputModalities", "outputModalities"} & set(row), row


def test_an_embedding_model_is_recognised_from_its_name_when_nothing_publishes_it() -> None:
    """The one class of model no catalogue marks.

    models.dev carries no flag for either kind: BGE M3 arrives with
    ``modalities.output = ["text"]`` and ``tool_call = false``, which is
    indistinguishable from a small chat model. So a gateway's whole embedding
    shelf reached the surfaces untagged and filed under Text -- and curation
    cannot cover it, because a live fetch returns names nobody wrote down.
    """
    from raven.providers.registry_data import inferred_tags

    assert inferred_tags("BAAI/bge-m3") == ("embedding",)
    assert inferred_tags("Qwen/Qwen3-Embedding-8B") == ("embedding",)
    assert inferred_tags("text-embedding-3-large") == ("embedding",)
    assert inferred_tags("netease-youdao/bce-embedding-base_v1") == ("embedding",)
    # Rerankers are named after the embedding family they rerank for, so the
    # other order would file every one of them as an embedder.
    assert inferred_tags("BAAI/bge-reranker-v2-m3") == ("rerank",)
    assert inferred_tags("gte-rerank-v2") == ("rerank",)
    # And nothing else is touched.
    assert inferred_tags("anthropic/claude-opus-5") == ()
    assert inferred_tags("deepseek-v4-pro") == ()


def test_no_chat_model_in_the_bundled_catalogue_is_mistaken_for_an_embedder() -> None:
    """The name rule, measured against every model Raven ships rather than
    against the examples it was written from.

    A guess is only worth making if it is this narrow: anything it marks must
    really be an embedder or a reranker, because the icon it draws is a claim.
    """
    from raven.providers.registry_data import _models, inferred_tags

    marked = {mid: inferred_tags(mid) for mid in _models() if inferred_tags(mid)}
    assert marked, "the corpus has embedding models; the rule matching none of them is the other failure"
    for mid in marked:
        bare = mid.rsplit("/", 1)[-1].lower()
        assert any(hint in bare for hint in ("embed", "rerank", "bge", "gte", "e5", "m3e", "text2vec", "uae")), mid


def test_a_catalogued_capability_is_never_argued_with_by_a_guess() -> None:
    """Added where the catalogue is silent, never substituted for what it says.

    A multimodal embedder really does read images: ``cohere-embed-v4`` has to
    keep ``image-recognition`` and gain ``embedding``, not trade one for the
    other.
    """
    from raven.providers.catalog import describe

    row = describe("azure_openai", "cohere-embed-v-4-0")
    assert "embedding" in row.capabilities
    assert "image-recognition" in row.capabilities

    # A model the catalogue describes fully is left exactly as it is.
    opus = describe("anthropic", "claude-opus-5")
    assert "embedding" not in opus.capabilities
    assert "function-call" in opus.capabilities


def test_a_model_no_catalogue_carries_still_lands_in_the_right_bucket() -> None:
    """The case that started this: a live fetch from a gateway.

    SiliconFlow serves hundreds of these under ids the bundled files have never
    seen, so the row comes back with no label and no tags -- and an untagged
    embedder sits in the Text filter, which is where they were.
    """
    from raven.providers.catalog import describe
    from raven.providers.registry_data import kind_of, row_for

    assert row_for("siliconflow", "BAAI/bge-m3") is None, "the premise: the registry does not carry it"
    row = describe("siliconflow", "BAAI/bge-m3")
    assert row.capabilities == ("embedding",)
    assert kind_of(row.capabilities, row.output_modalities) == "embedding"


def test_a_model_its_provider_does_not_list_is_still_known_from_elsewhere() -> None:
    """Upstream files models per provider, and its coverage is uneven.

    SiliconFlow gets twelve rows and none of them are the image models it
    actually serves -- while the same models are described in full under
    another provider. Keyed by name, a model the registry knows anywhere is a
    model it knows everywhere, which is the difference between an Image filter
    with four models in it and one with none.
    """
    from raven.providers.catalog import describe
    from raven.providers.registry_data import kind_of, row_for

    assert row_for("siliconflow", "Qwen/Qwen-Image") is None, "the premise: not in this provider's own rows"
    row = describe("siliconflow", "Qwen/Qwen-Image")
    assert row.label == "Qwen Image"
    assert "image-generation" in row.capabilities
    assert kind_of(row.capabilities, row.output_modalities) == "image"


def test_a_borrowed_row_lends_its_description_and_never_its_price() -> None:
    """The price on another provider's row is that provider's price.

    Everything else about a model is the same wherever it is served, which is
    what makes the borrow safe; what it costs is the one fact that is not.
    """
    from raven.providers.catalog import describe, model_cost

    assert describe("siliconflow", "black-forest-labs/FLUX.1-dev").capabilities == ("image-generation",)
    assert model_cost("siliconflow/black-forest-labs/FLUX.1-dev") is None


def test_a_name_two_providers_disagree_about_borrows_nothing() -> None:
    """Seven names in the catalogue are filed under two different kinds -- one
    reseller lists GPT-5.1 as answering with images. Filing a chat model under
    Image is a worse answer than filing it under nothing, so a name whose rows
    disagree is left alone."""
    import json
    import re

    from raven.providers.registry_data import MODELS_FILE, kind_of, row_by_name

    rows = json.loads(MODELS_FILE.read_text(encoding="utf-8"))["models"]
    groups: dict[str, set[str]] = {}
    for row in rows:
        key = re.sub(r"[^a-z0-9]", "", row["id"].rsplit("/", 1)[-1].lower())
        groups.setdefault(key, set()).add(kind_of(row.get("capabilities") or [], row.get("outputModalities") or []))
    contested = [key for key, kinds in groups.items() if len(kinds) > 1]
    assert contested, "the guard is only worth having while the catalogue still disagrees somewhere"
    for key in contested:
        assert row_by_name(key) is None, key


def test_the_curated_layer_answers_for_a_model_no_catalogue_carries() -> None:
    """The escape hatch, on the model that needed it.

    Kolors is served by several gateways and described by none of them; a
    curated row is the one place to say so. Written against a provider, it
    reaches every provider through the name index -- and it survives a refresh,
    which is the whole point of keeping the hand layer in its own array.
    """
    from raven.providers.catalog import describe

    for slug in ("siliconflow", "openrouter"):
        row = describe(slug, "Kwai-Kolors/Kolors")
        assert row.label == "Kolors"
        assert row.capabilities == ("image-generation",)


def test_a_model_tagged_from_its_endpoint_reaches_the_same_bucket_as_a_catalogued_one() -> None:
    """The two spellings of one fact have to agree.

    The generator derives ``image-generation`` from ``outputModalities``, so a
    catalogued model states both -- but a model tagged from the endpoint it was
    served at has only the capability. Reading just the modality put 35 of
    OpenRouter's 50 image models in the Text bucket.
    """
    from raven.providers.registry_data import kind_of

    assert kind_of(["image-generation"], []) == "image"
    assert kind_of([], ["image"]) == "image"
    assert kind_of(["audio-generation"], []) == "audio"
    assert kind_of(["video-generation"], []) == "video"
    # Reading pictures is still something a text model does.
    assert kind_of(["image-recognition"], ["text"]) == "text"
