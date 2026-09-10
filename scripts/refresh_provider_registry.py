"""Regenerate the bundled provider registry: providers, models, provider-models.

Raven ships a trimmed copy of the models.dev catalogue so a fresh install can
label models and show what they can do without a network round trip, and so
tests never depend on one. This script is how that copy is produced -- editing
the generated files by hand would leave no way to tell what they were trimmed
from.

    uv run python scripts/refresh_provider_registry.py

Source: the project's own repository rather than its ``/api.json`` endpoint.
Both are models.dev; the repository is the one that can be pinned. api.json is
a rendering of these files served by a small project's app, and an outage there
is the failure this snapshot exists to survive -- so the refresh should not
depend on it either. Pinning also makes a refresh reproducible: the recorded
commit sha says exactly which catalogue a snapshot came from, where "whatever
the site returned that day" said nothing.

Three files rather than one, following the shape Cherry Studio's provider
registry settled on, because the catalogue answers three questions that drift
apart:

* ``models.json`` -- what a model *is*, once, keyed by the vendor's canonical
  id. Reselling gateways share these rows instead of copying them; the previous
  single-file snapshot stored OpenRouter's copy of Claude Opus separately from
  Anthropic's, which is how a description could differ between two spellings of
  the same model.
* ``provider-models.json`` -- who serves it, under which id on the wire, at
  which price, and where it sits in the curated shortlist. A gateway's price is
  its own: OpenRouter bills what OpenRouter bills, whoever built the model.
* ``providers.json`` -- how a provider reads to a person, plus the links a
  settings page needs.

What is deliberately absent from all three: the context window. A window sizes
trimming, so it shapes the *next* request, and only the tables that also route
may answer it (``providers/rates.py``). Everything these files carry is read
for display -- a stale figure costs a wrong label or an inaccurate total, never
a mis-sent request. ``tests/test_provider_catalog.py`` asserts that boundary.

Hand-written facts survive a refresh. ``provider-models.json`` holds two arrays:
``overrides`` is regenerated from scratch every run, and ``curated`` is copied
through untouched. That is where the shortlist ranks live, and the rows for
models no upstream carries -- a Copilot seat's catalogue, an Ollama pull. The
split is what makes a refresh idempotent: mixed into the generated rows, a
derived value reads back as a hand-written one on the next run and no upstream
correction can ever reach it. Delete the file before refreshing and the curation
is gone; edit ``curated`` in place instead.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

from raven.providers.registry import normalize_provider_name

REPO = "anomalyco/models.dev"
REF = "dev"
TARBALL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{REF}"
GIT_URL = f"https://github.com/{REPO}.git"

DATA = Path(__file__).resolve().parents[1] / "raven" / "providers" / "data"
MODELS = DATA / "models.json"
PROVIDER_MODELS = DATA / "provider-models.json"
PROVIDERS_FILE = DATA / "providers.json"

#: Raven's provider name -> models.dev's name for the same vendor. Only the ones
#: that differ; a matching name needs no entry. Absent vendors (VolcEngine, a
#: local Ollama) and Raven-only sections (custom, hosted_vllm) have no upstream
#: row by nature, not by oversight.
PROVIDER_ALIASES: dict[str, str] = {
    "gemini": "google",
    "dashscope": "alibaba",
    "moonshot": "moonshotai",
    "azure_openai": "azure",
    "github_copilot": "github-copilot",
    "minimax_global": "minimax",
    "minimax_cn": "minimax-cn",
    "minimax_cn_api": "minimax-cn",
    "nvidia_nim": "nvidia",
}

#: The closed capability vocabulary. Cherry Studio's set, adopted rather than
#: invented so a tag means the same thing in both registries -- and closed
#: because an icon table cannot render a string nobody has drawn.
#: ``embedding``, ``rerank`` and ``computer-use`` are derivable from nothing
#: upstream publishes; they exist for hand-written rows.
CAPABILITIES = (
    "function-call",
    "reasoning",
    "structured-output",
    "image-recognition",
    "audio-recognition",
    "video-recognition",
    "file-input",
    "image-generation",
    "audio-generation",
    "video-generation",
    "embedding",
    "rerank",
    "computer-use",
)

#: models.dev files PDF support as an input modality; Cherry files it as a
#: capability, and the icon row reads better for it -- "takes files" is what a
#: person is looking for, and it is the same answer for a PDF and a CSV.
MODALITY_TO_CAPABILITY = {
    ("input", "image"): "image-recognition",
    ("input", "audio"): "audio-recognition",
    ("input", "video"): "video-recognition",
    ("input", "pdf"): "file-input",
    ("output", "image"): "image-generation",
    ("output", "audio"): "audio-generation",
    ("output", "video"): "video-generation",
}

#: Modalities that survive into the rendered lists. ``pdf`` becomes a capability
#: above; ``vector`` has no upstream source and exists for hand-written rows.
KEPT_MODALITIES = ("text", "image", "video", "audio")


def upstream_name(provider: str, catalogue: dict[str, dict] | None = None) -> str:
    """The upstream vendor key for one of raven's slugs.

    The alias table covers the vendors the two sources give different names.
    The catalogue lookup covers the ones they only spell differently: raven
    slugs are underscored, upstream ones are not always ("nano-gpt"), and since
    the slug is normalised at the point it is adopted, a hyphenated vendor would
    otherwise resolve to nothing here and be dropped without a word.
    """
    if provider in PROVIDER_ALIASES:
        return PROVIDER_ALIASES[provider]
    if catalogue is None or provider in catalogue:
        return provider
    for key in catalogue:
        if normalize_provider_name(key) == provider:
            return key
    return provider


def reachable_providers(catalogue: dict[str, dict]) -> set[str]:
    """Raven-side names worth carrying rows for.

    Two ways to be reachable and neither contains the other: LiteLLM maps ~130
    vendors Raven has no spec for (a prefixed id routes to them with only a key),
    and Raven carries specs for gateways and regional instances LiteLLM has never
    heard of (aihubmix, siliconflow, minimax_cn). Taking only the first silently
    drops those three -- 123 models -- while every total in the summary still
    goes up, which is why ``tests/test_provider_catalog.py::LABELLED_PROVIDERS``
    asserts a labelled provider list rather than a total.
    """
    from raven.providers.registry import PROVIDERS

    try:
        from raven.providers.litellm_setup import import_litellm

        # ``provider_list`` holds ``LlmProviders`` members, whose ``str()`` is
        # "LlmProviders.OPENAI" -- comparing that to a vendor name matches nothing
        # and silently keeps the union empty.
        known = {getattr(p, "value", str(p)) for p in getattr(import_litellm(), "provider_list", [])}
    except Exception:  # pragma: no cover - litellm ships with the project
        known = set()

    inverse = {v: k for k, v in PROVIDER_ALIASES.items()}
    wanted = {spec.name for spec in PROVIDERS}
    for upstream in catalogue:
        # Normalised at adoption, because this is the name the row is keyed by
        # and every reader looks it up through `canonical_provider_name`, which
        # underscores. A hyphen written here can never be matched again: 593
        # nano-gpt rows shipped under a spelling no lookup could produce.
        raven = normalize_provider_name(inverse.get(upstream, upstream))
        # Either spelling counts: the alias table exists because the two sources
        # name the same vendor differently, and LiteLLM sides with either one.
        if raven in known or upstream in known:
            wanted.add(raven)
    return wanted


def tags_of(entry: dict[str, Any]) -> dict[str, Any]:
    """The display tags a catalogue entry implies, in the registry's vocabulary."""
    modalities = entry.get("modalities") or {}
    capabilities: set[str] = set()
    if entry.get("tool_call"):
        capabilities.add("function-call")
    if entry.get("reasoning"):
        capabilities.add("reasoning")
    if entry.get("structured_output"):
        capabilities.add("structured-output")
    if entry.get("attachment"):
        capabilities.add("file-input")

    lists: dict[str, list[str]] = {}
    for side in ("input", "output"):
        raw = modalities.get(side)
        if not isinstance(raw, list):
            continue
        for modality in raw:
            mapped = MODALITY_TO_CAPABILITY.get((side, modality))
            if mapped:
                capabilities.add(mapped)
        kept = [m for m in KEPT_MODALITIES if m in raw]
        if kept:
            lists[f"{side}Modalities"] = kept

    tags: dict[str, Any] = {"capabilities": sorted(capabilities)}
    tags.update(lists)
    return tags


def pricing_of(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Published cost per million tokens, in the registry's shape.

    Only input and output. The catalogue also publishes cache read and write
    rates, and nothing has ever read them: ``token_wise/pricing.py`` derives a
    cached-token rate from the prompt rate instead. Carrying two unread numbers
    per row is most of a hundred kilobytes for nothing.
    """
    cost = entry.get("cost")
    if not isinstance(cost, dict):
        return None
    out: dict[str, Any] = {}
    for side in ("input", "output"):
        value = cost.get(side)
        if isinstance(value, (int, float)):
            # 5 and 5.0 are the same price and a different JSON literal, and one
            # vendor writes each. Left alone, every row whose reseller happened
            # to use the other spelling emitted a delta of its own price.
            number = float(value)
            out[side] = {"currency": "USD", "perMillionTokens": int(number) if number.is_integer() else number}
    return out or None


def canonical_ref(upstream_provider: str, model_id: str, raw: dict[str, Any]) -> str:
    """Which row in ``models.json`` this provider entry is a copy of.

    A reseller states only what differs and points ``base_model`` at the vendor's
    definition; that ref is the shared identity, and it is what makes one row
    serve every gateway fronting the same model. An entry with no base is its
    own canonical row, filed under the provider that publishes it.
    """
    base = raw.get("base_model")
    if isinstance(base, str) and base:
        return base
    return f"{upstream_provider}/{model_id}"


def origin_entry(
    ref: str,
    providers: dict[str, dict],
    canonical: dict[str, dict],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """The definition a canonical row should be built from.

    The vendor's own provider row first, because that is the model as the party
    that made it describes it -- and because the alternative, whichever reseller
    sorted first, made aihubmix the authority on every Anthropic model. The
    shared ``models/`` definition second: it is the shape a reseller inherits,
    but it is deliberately thinner than a provider row (it omits
    ``structured_output``, which every provider row states), so taking it as the
    canonical answer put a spurious delta on all 2134 rows.
    """
    owner, _, rest = ref.partition("/")
    own = (providers.get(owner) or {}).get("models", {}).get(rest)
    if isinstance(own, dict):
        return own
    return canonical.get(ref) or fallback


def build_models(
    providers: dict[str, dict],
    canonical: dict[str, dict],
    used: dict[str, dict],
) -> list[dict[str, Any]]:
    """One row per canonical model, as the vendor that publishes it describes it.

    ``used`` carries the resolved provider entry each ref was reached through, so
    a ref naming neither a shared definition nor a vendor row -- a gateway's own
    listing -- still produces a row rather than a hole the readers must tolerate.
    """
    rows: list[dict[str, Any]] = []
    for ref in sorted(used):
        entry = origin_entry(ref, providers, canonical, used[ref])
        owner = ref.split("/", 1)[0]
        row: dict[str, Any] = {"id": ref, "ownedBy": owner}
        if name := entry.get("name"):
            row["name"] = name
        if description := entry.get("description"):
            row["description"] = description
        if family := entry.get("family"):
            row["family"] = family
        row.update(tags_of(entry))
        limit = entry.get("limit")
        if isinstance(limit, dict) and isinstance(limit.get("output"), int):
            row["maxOutputTokens"] = limit["output"]
        if entry.get("open_weights"):
            row["openWeights"] = True
        if pricing := pricing_of(entry):
            row["pricing"] = pricing
        rows.append(row)
    return rows


def delta(row: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """What this provider's copy states differently from the canonical row."""
    return {key: value for key, value in row.items() if base.get(key) != value}


def build_provider_models(
    providers: dict[str, dict],
    canonical: dict[str, dict],
    *,
    wanted: set[str],
) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    """One thin row per (provider, model), plus the canonical rows they reach.

    The row names three ids on purpose. ``providerId`` is Raven's slug, because
    that is what a config section is keyed by; ``apiModelId`` is what goes on the
    wire, which a gateway spells its own way; ``modelId`` is the shared identity,
    which is how two spellings of one model share a description.
    """
    rows: list[dict[str, Any]] = []
    used: dict[str, dict] = {}
    for slug in sorted(wanted):
        upstream = providers.get(upstream_name(slug, providers))
        if not upstream:
            continue
        for model_id, entry in sorted((upstream.get("models") or {}).items()):
            ref = canonical_ref(upstream_name(slug, providers), model_id, entry)
            used.setdefault(ref, entry)
            row: dict[str, Any] = {"providerId": slug, "modelId": ref, "apiModelId": model_id}
            base_entry = origin_entry(ref, providers, canonical, used[ref])
            base_row = {**tags_of(base_entry)}
            if name := base_entry.get("name"):
                base_row["name"] = name
            own = {**tags_of(entry)}
            if name := entry.get("name"):
                own["name"] = name
            row.update(delta(own, base_row))
            if pricing := pricing_of(entry):
                if pricing != pricing_of(base_entry):
                    row["pricing"] = pricing
            rows.append(row)
    return rows, used


def build_providers(providers: dict[str, dict], *, wanted: set[str]) -> list[dict[str, Any]]:
    """Display facts about each provider, and nothing that reaches an endpoint.

    ``ProviderSpec`` stays the only authority on how a request is addressed --
    base URLs, environment keys, route names, wire quirks. What lands here is
    what a settings page renders: a name, and the links a person clicks to get a
    key or read the docs.
    """
    from raven.providers.registry import PROVIDERS

    specs = {spec.name: spec for spec in PROVIDERS}
    rows: list[dict[str, Any]] = []
    for slug in sorted(wanted):
        spec = specs.get(slug)
        upstream = providers.get(upstream_name(slug, providers)) or {}
        name = (spec.display_name if spec else "") or upstream.get("name") or slug
        row: dict[str, Any] = {"id": slug, "name": name}
        website: dict[str, str] = {}
        if spec and spec.homepage:
            website["official"] = spec.homepage
        if doc := upstream.get("doc"):
            website["docs"] = doc
        if website:
            row["metadata"] = {"website": website}
        rows.append(row)
    return rows


def read_catalogue(archive: bytes) -> tuple[dict[str, dict], dict[str, dict]]:
    """Parse the repository's tree into the provider rows and the shared models.

    One ``provider.toml`` names the vendor; everything under ``models/`` is one
    row keyed by its path, because the id a vendor publishes can itself contain a
    slash -- a gateway files ``moonshotai/Kimi-K2.6`` two directories deep, and
    reading only the flat level drops every one of them (siliconflow and
    openrouter are entirely nested, so both came back empty).

    Both halves are returned: the shared definitions under ``models/`` are what
    ``models.json`` is built from, and folding them into the provider rows -- as
    the single-file snapshot did -- is what made every reseller carry its own
    copy of the same description.
    """
    providers: dict[str, dict] = {}
    #: ``models/<vendor>/<id>.toml`` -- the vendor's own definition of a model,
    #: shared by every provider that resells it. A provider row states only what
    #: differs and points ``base_model`` here for the rest.
    canonical: dict[str, dict] = {}

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".toml"):
                continue
            parts = member.name.split("/")
            if len(parts) < 4 or parts[1] not in {"providers", "models"}:
                continue
            handle = tar.extractfile(member)
            if handle is None:  # pragma: no cover - directories filtered above
                continue
            try:
                data: dict[str, Any] = tomllib.loads(handle.read().decode("utf-8"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
                print(f"  skipping {member.name}: {exc}", file=sys.stderr)
                continue

            if parts[1] == "models":
                canonical["/".join(parts[2:]).removesuffix(".toml")] = data
                continue

            vendor = providers.setdefault(parts[2], {"models": {}})
            if parts[3] == "provider.toml":
                vendor["name"] = data.get("name") or parts[2]
                if doc := data.get("doc"):
                    vendor["doc"] = doc
            elif parts[3] == "models" and len(parts) > 4:
                vendor["models"]["/".join(parts[4:]).removesuffix(".toml")] = data

    resolve_inheritance(providers, canonical)
    return providers, canonical


def resolve_inheritance(providers: dict[str, dict], canonical: dict[str, dict]) -> None:
    """Fill in what a row inherits from the model it declares as its base.

    A provider reselling someone else's model states only what differs -- its own
    price -- and points ``base_model`` at the vendor's definition for the name and
    the description. 2915 of the catalogue's rows are written that way, including
    every one of github_copilot's and most of azure's. The published api.json
    resolves this before serving; reading the files directly does not, and the
    failure is quiet: every row is present and every total looks right, the rows
    just have no names. That is why ``tests/test_provider_catalog.py`` asserts a
    label per provider and not a count.

    ``base_model`` is left on the resolved row: it is the canonical identity the
    three-file split is keyed by, and dropping it here would make every reseller
    look like the origin of what it resells.
    """
    resolved: dict[str, dict] = {}

    def entry_for(ref: str) -> dict | None:
        # The shared definition first: `base_model = "anthropic/claude-opus-5"`
        # names the vendor's model, which is a different file from that vendor's
        # own provider row and is the only place some of them exist.
        if ref in canonical:
            return canonical[ref]
        provider, _, model_id = ref.partition("/")
        return providers.get(provider, {}).get("models", {}).get(model_id)

    def resolve(ref: str, seen: frozenset[str]) -> dict:
        if ref in resolved:
            return resolved[ref]
        entry = entry_for(ref)
        if entry is None:
            return {}
        base_ref = entry.get("base_model")
        # A base can itself derive (alibaba/qwen3.8-max does), so this recurses;
        # `seen` stops a cycle from doing it forever.
        merged = entry if not base_ref or ref in seen else {**resolve(str(base_ref), seen | {ref}), **entry}
        resolved[ref] = merged
        return merged

    for provider, vendor in providers.items():
        for model_id in list(vendor["models"]):
            ref = f"{provider}/{model_id}"
            # A provider row and the shared definition can share a ref; the row
            # is the one being resolved, so it seeds `seen` rather than being
            # looked up through `entry_for`, which would return the other file.
            entry = vendor["models"][model_id]
            base_ref = entry.get("base_model")
            if base_ref:
                entry = {**resolve(str(base_ref), frozenset({ref})), **entry}
            vendor["models"][model_id] = entry


def read_curated(path: Path) -> list[dict[str, Any]]:
    """The hand-written layer of the existing file, copied through untouched.

    A separate array rather than fields mixed into the generated rows, because
    the two have to be told apart to regenerate at all: fold them together and
    the first refresh reads its own derived deltas back as if a person had
    written them, and they can never be corrected by a later upstream fix.

    What lives here is what no upstream can answer -- the shortlist ranks, and
    the models a Copilot seat or an Ollama pull serves. It is applied over the
    generated rows at read time, so a curated row for a model upstream does
    carry states only the difference.
    """
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("curated") or [])
    except (OSError, ValueError):
        return []


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "raven-model-catalog-refresh"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - a pinned https URL
        return response.read()


def resolve_sha() -> str:
    """The commit ``dev`` points at, over git rather than api.github.com.

    The API answers the same question and refuses to on the fifth run: it rate
    limits unauthenticated callers, which turned a refresh into a 403 with the
    tarball already downloaded. ``ls-remote`` speaks the git protocol the
    tarball itself comes from, needs no credential and has no such ceiling --
    the same argument this module makes for reading the repository instead of
    the project's app.
    """
    result = subprocess.run(  # noqa: S603 - argv is built here, not from input
        ["git", "ls-remote", GIT_URL, f"refs/heads/{REF}"],  # noqa: S607 - git off PATH, as the rest of the repo invokes it
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    sha = result.stdout.split(maxsplit=1)[0].strip()
    if len(sha) != 40:
        raise RuntimeError(f"could not resolve {REF}: {result.stdout!r}")
    return sha


def _write(path: Path, payload: dict[str, Any]) -> float:
    # Sorted and minified: the upstream returns models in an unstable order, so
    # an unsorted dump makes every refresh a diff of the whole file with no
    # change in it, and an indented one costs a third of the size for nothing a
    # reviewer reads -- these are generated files, diffed by regenerating.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path.stat().st_size / 1048576


def main() -> int:
    print(f"resolving {REPO}@{REF} ...")
    sha = resolve_sha()
    print(f"fetching {TARBALL} ({sha[:12]}) ...")
    providers, canonical = read_catalogue(_fetch(TARBALL))
    print(f"  catalogue: {len(providers)} providers, {len(canonical)} shared model definitions")

    wanted = reachable_providers(providers)
    curated = read_curated(PROVIDER_MODELS)
    rows, used = build_provider_models(providers, canonical, wanted=wanted)
    models = build_models(providers, canonical, used)
    provider_rows = build_providers(providers, wanted=wanted)

    source = {"repo": REPO, "ref": REF, "sha": sha}
    sizes = {
        MODELS: _write(MODELS, {"_source": source, "version": sha, "models": models}),
        PROVIDER_MODELS: _write(
            PROVIDER_MODELS,
            {"_source": source, "version": sha, "curated": curated, "overrides": rows},
        ),
        PROVIDERS_FILE: _write(PROVIDERS_FILE, {"_source": source, "version": sha, "providers": provider_rows}),
    }

    served = {row["providerId"] for row in rows}
    missing = sorted(w for w in wanted if w not in served)
    for path, size in sizes.items():
        print(f"wrote {path.name}: {size:.3f} MiB")
    print(
        f"  {len(provider_rows)} providers, {len(models)} models, "
        f"{len(rows)} provider-model rows, {len(curated)} curated"
    )
    if missing:
        print(f"  reachable but not in the catalogue ({len(missing)}): {', '.join(missing)}")
    over = [p.name for p, size in sizes.items() if size > 1]
    if over:
        print(f"ERROR: over the 1 MiB gate: {', '.join(over)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
