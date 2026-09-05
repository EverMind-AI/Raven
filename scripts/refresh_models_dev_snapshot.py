"""Regenerate the bundled models.dev snapshot.

Raven ships a trimmed copy of the models.dev catalogue so a fresh install can
label models without a network round trip, and so tests never depend on one.
This script is how that copy is produced -- editing it by hand would leave no
way to tell what it was trimmed from.

    uv run python scripts/refresh_models_dev_snapshot.py

Source: the project's own repository rather than its ``/api.json`` endpoint.
Both are models.dev; the repository is the one that can be pinned. api.json is
a rendering of these files served by a small project's app, and an outage there
is the failure this snapshot exists to survive -- so the refresh should not
depend on it either. Pinning also makes a refresh reproducible: the recorded
commit sha says exactly which catalogue a snapshot came from, where "whatever
the site returned that day" said nothing.

What is kept and why:

* only the providers Raven can reach -- either LiteLLM maps the vendor (so a
  prefixed id routes) or Raven carries a ``ProviderSpec`` for it. The full
  catalogue is 181 providers, most of which Raven has no way to talk to, and the
  repository rejects additions over 1 MiB;
* only the fields a person reads when choosing a model, plus per-model cost.
  Cost is reporting -- it prices a call after the fact and never shapes a
  request -- so a stale figure costs an inaccurate total, not a wrong call.
  Everything that does shape a request (context windows, capability flags) comes
  from LiteLLM's own table, which ships with the dependency. That split is
  deliberate: a community-maintained file that goes stale or wrong should cost a
  label or a decimal, never a mis-sent request.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

REPO = "anomalyco/models.dev"
REF = "dev"
TARBALL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{REF}"
COMMIT_API = f"https://api.github.com/repos/{REPO}/commits/{REF}"

SNAPSHOT = Path(__file__).resolve().parents[1] / "raven" / "providers" / "data" / "models_dev.json"

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
}

#: Model fields carried over. ``name``/``description`` are what a picker renders;
#: ``cost`` prices a finished call. Deliberately absent: ``limit`` (a context
#: window sizes trimming, so it must come from the one table that also prices the
#: request) and the capability flags (``ProviderSpec`` answers those about the
#: wire format, which is a different question than what a model can do).
KEEP = ("name", "description", "cost")


def upstream_name(provider: str) -> str:
    return PROVIDER_ALIASES.get(provider, provider)


def reachable_providers(catalogue: dict[str, dict]) -> set[str]:
    """Raven-side names worth carrying labels for.

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
        raven = inverse.get(upstream, upstream)
        # Either spelling counts: the alias table exists because the two sources
        # name the same vendor differently, and LiteLLM sides with either one.
        if raven in known or upstream in known:
            wanted.add(raven)
    return wanted


def build(providers: dict[str, dict], *, wanted: set[str]) -> dict:
    out: dict[str, dict] = {}
    for name in sorted(wanted):
        upstream = providers.get(upstream_name(name))
        if not upstream:
            continue
        models = {
            model_id: {k: entry[k] for k in KEEP if k in entry}
            for model_id, entry in (upstream.get("models") or {}).items()
        }
        if models:
            out[name] = {"models": models}
    return out


def read_catalogue(archive: bytes) -> dict[str, dict]:
    """Parse the repository's ``providers/<name>/`` tree into api.json's shape.

    One ``provider.toml`` names the vendor; everything under ``models/`` is one
    row keyed by its path, because the id a vendor publishes can itself contain a
    slash -- a gateway files ``moonshotai/Kimi-K2.6`` two directories deep, and
    reading only the flat level drops every one of them (siliconflow and
    openrouter are entirely nested, so both came back empty).
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
            elif parts[3] == "models" and len(parts) > 4:
                vendor["models"]["/".join(parts[4:]).removesuffix(".toml")] = data

    resolve_inheritance(providers, canonical)
    return providers


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


def _fetch(url: str, *, accept: str = "*/*") -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "raven-model-catalog-refresh", "Accept": accept})  # noqa: S310
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - a pinned https URL
        return response.read()


def main() -> int:
    print(f"resolving {REPO}@{REF} ...")
    sha = json.loads(_fetch(COMMIT_API, accept="application/vnd.github+json"))["sha"]
    print(f"fetching {TARBALL} ({sha[:12]}) ...")
    providers = read_catalogue(_fetch(TARBALL))
    print(f"  catalogue: {len(providers)} providers")

    wanted = reachable_providers(providers)
    snapshot = build(providers, wanted=wanted)
    missing = sorted(w for w in wanted if w not in snapshot)

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    # Sorted: the upstream returns models in an unstable order, so an unsorted
    # dump makes every refresh a diff of the whole file with no change in it.
    # The sha rides along so a snapshot can be traced to the commit it came from.
    payload = {"_source": {"repo": REPO, "ref": REF, "sha": sha}, **snapshot}
    SNAPSHOT.write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    size_mib = SNAPSHOT.stat().st_size / 1048576
    models = sum(len(v["models"]) for v in snapshot.values())
    print(f"wrote {SNAPSHOT.relative_to(Path.cwd())}: {len(snapshot)} providers, {models} models, {size_mib:.3f} MiB")
    if missing:
        print(f"  reachable but not in the catalogue ({len(missing)}): {', '.join(missing)}")
    if size_mib > 1:
        print("ERROR: over the 1 MiB gate; trim KEEP or PROVIDER_ALIASES", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
