"""What each source permits, and the decision made before a call leaves the machine.

The registry exists because the sources a research agent reaches for are not
interchangeable. One lets you read its data and build on it. One lets you read
it and asks for its name to travel with the result. One hands it over under a
licence. One does not let you have it at all unless you say why you should.

So the rule is consulted *before* the call, and a refusal says which rule was
hit and what would change the answer. Refusing after the fact would be
useless: the data would already have left the machine.

A source that is not in the table is refused. That is the safe direction to
be wrong in, because a source added to the code without a row here has not
been looked at, and looking at it is the whole point of the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from raven.sources.tiers import SourceTier

__all__ = [
    "SOURCE_POLICIES",
    "SourcePolicy",
    "SourcePolicyError",
    "SourceRefusedError",
    "consult",
    "policy_for",
]


class SourcePolicyError(RuntimeError):
    """A source could not be used the way the caller asked."""


class SourceRefusedError(SourcePolicyError):
    """The source's terms do not permit this use.

    Carries the source, the use, the rule that was hit and what would change
    the answer, so that the refusal can be shown to the operator and acted on
    rather than merely reported.
    """

    def __init__(self, source: str, use: str, reason: str, remedy: str) -> None:
        super().__init__(f"{source} may not be used for {use}: {reason}. {remedy}")
        self.source = source
        self.use = use
        self.reason = reason
        self.remedy = remedy


@dataclass(frozen=True)
class SourcePolicy:
    """What one source's terms permit.

    ``restrictions`` carries the uses the source does not permit as it
    stands, each with the reason. A use that appears there is refused unless
    the operator has recorded why this deployment may make it.
    """

    source: str
    label: str
    tier: SourceTier
    citation: str
    cache_allowed: bool = True
    store_allowed: bool = True
    attribution_required: bool = False
    derivation_allowed: bool = True
    commercial_use: bool = True
    retention: str = "as long as the operator keeps the deployment"
    restrictions: tuple[tuple[str, str], ...] = ()

    def restriction_for(self, use: str) -> str | None:
        """Why this use is refused, or None when it is not refused."""
        for restricted_use, reason in self.restrictions:
            if restricted_use == use:
                return reason
        return None

    def to_dict(self) -> dict[str, Any]:
        """The row shape, with the unset parts left out."""
        return {
            "source": self.source,
            "label": self.label,
            "tier": self.tier.value,
            "citation": self.citation,
            "cache_allowed": self.cache_allowed,
            "store_allowed": self.store_allowed,
            "attribution_required": self.attribution_required,
            "derivation_allowed": self.derivation_allowed,
            "commercial_use": self.commercial_use,
            "retention": self.retention,
            "restrictions": {use: reason for use, reason in self.restrictions},
        }


def _licensed(
    source: str,
    label: str,
    citation: str,
    *,
    attribution: bool = True,
    cache: bool = False,
    **kwargs: Any,
) -> SourcePolicy:
    """A source handed over under a licence the operator holds.

    Search and enrichment services are cached never: a search result is a
    snapshot of what a vendor thought a week ago, and storing it as a fact
    would let a stale reading answer a question later as though it were
    current.
    """
    return SourcePolicy(
        source=source,
        label=label,
        tier=SourceTier.LICENSED,
        citation=citation,
        cache_allowed=cache,
        attribution_required=attribution,
        **kwargs,
    )


SOURCE_POLICIES: dict[str, SourcePolicy] = {
    # -- the services the runtime calls ---------------------------------
    "serper": _licensed("serper", "Serper", "https://serper.dev/"),
    "serpapi": _licensed("serpapi", "SerpApi", "https://serpapi.com/"),
    "anysearch": _licensed("anysearch", "AnySearch", "https://anysearch.com/"),
    "tavily": _licensed("tavily", "Tavily", "https://tavily.com/"),
    "exa": _licensed("exa", "Exa", "https://exa.ai/"),
    "brave": _licensed("brave", "Brave Search", "https://brave.com/search/api"),
    "serply": _licensed("serply", "Serply", "https://serply.io/"),
    "tinyfish": _licensed("tinyfish", "TinyFish Search", "https://www.tinyfish.ai/terms"),
    "firecrawl": _licensed("firecrawl", "Firecrawl", "https://firecrawl.dev/"),
    "jina": SourcePolicy(
        source="jina",
        label="Jina Reader",
        tier=SourceTier.ATTRIBUTED,
        citation="https://jina.ai/reader",
        cache_allowed=False,
        attribution_required=True,
    ),
    # -- open data -------------------------------------------------------
    "sec_edgar": SourcePolicy(
        source="sec_edgar",
        label="SEC EDGAR full-text search",
        tier=SourceTier.PUBLIC,
        citation="https://www.sec.gov/edgar/full-text-search",
        attribution_required=False,
        retention="public domain, no end date",
    ),
    "opencorporates": SourcePolicy(
        source="opencorporates",
        label="OpenCorporates",
        tier=SourceTier.PUBLIC,
        citation="https://opencorporates.com/",
        attribution_required=True,
        restrictions=(
            (
                "closed_database",
                "the data is under the Open Database Licence, so a database derived "
                "from it has to stay open",
            ),
        ),
    ),
    "gdelt": SourcePolicy(
        source="gdelt",
        label="GDELT",
        tier=SourceTier.PUBLIC,
        citation="https://www.gdeltproject.org/",
        attribution_required=False,
    ),
    "wikidata": SourcePolicy(
        source="wikidata",
        label="Wikidata",
        tier=SourceTier.PUBLIC,
        citation="https://www.wikidata.org/wiki/Wikidata:Licensing",
        attribution_required=False,
    ),
    "tranco": SourcePolicy(
        source="tranco",
        label="Tranco list",
        tier=SourceTier.PUBLIC,
        citation="https://tranco-list.eu/",
    ),
    "wayback": SourcePolicy(
        source="wayback",
        label="Wayback Machine",
        tier=SourceTier.PUBLIC,
        citation="https://web.archive.org/",
    ),
    "overpass": SourcePolicy(
        source="overpass",
        label="OpenStreetMap Overpass API",
        tier=SourceTier.PUBLIC,
        citation="https://www.openstreetmap.org/copyright",
        attribution_required=True,
    ),
    # -- attributed data ------------------------------------------------
    "trustpilot": SourcePolicy(
        source="trustpilot",
        label="Trustpilot",
        tier=SourceTier.ATTRIBUTED,
        citation="https://www.trustpilot.com/",
        attribution_required=True,
    ),
    "similarweb": SourcePolicy(
        source="similarweb",
        label="Similarweb",
        tier=SourceTier.ATTRIBUTED,
        citation="https://www.similarweb.com/",
        attribution_required=True,
    ),
    # -- licensed data ---------------------------------------------------
    "ahrefs": _licensed("ahrefs", "Ahrefs", "https://ahrefs.com/", attribution=True),
    "apollo": _licensed("apollo", "Apollo", "https://www.apollo.io/"),
    "clay": _licensed("clay", "Clay", "https://www.clay.com/"),
    "hunter": _licensed("hunter", "Hunter", "https://hunter.io/"),
    "wappalyzer": _licensed("wappalyzer", "Wappalyzer", "https://www.wappalyzer.com/"),
    "builtwith": _licensed("builtwith", "BuiltWith", "https://builtwith.com/"),
    "g2": _licensed(
        "g2",
        "G2 Data API",
        "https://data.g2.com/api/v1/docs",
        restrictions=(
            (
                "no_contract",
                "the G2 Data API is entitlement-gated: an account without a "
                "contract cannot reach the endpoints at all",
            ),
        ),
    ),
    "crunchbase": _licensed(
        "crunchbase",
        "Crunchbase",
        "https://data.crunchbase.com/",
        restrictions=(
            (
                "generative_pipeline",
                "the Crunchbase terms forbid using the data to train models and "
                "forbid using it on behalf of a third party in a way that cannot "
                "be expunged; a generative pipeline needs a licence that says so",
            ),
        ),
    ),
    # -- sources that are not usable as they stand ------------------------
    "linkedin": SourcePolicy(
        source="linkedin",
        label="LinkedIn",
        tier=SourceTier.RESTRICTED,
        citation="https://www.linkedin.com/developers",
        cache_allowed=False,
        store_allowed=False,
        derivation_allowed=False,
        attribution_required=True,
        restrictions=(("profile_data", "LinkedIn does not license its member data for collection"),),
    ),
    "capterra": SourcePolicy(
        source="capterra",
        label="Capterra",
        tier=SourceTier.RESTRICTED,
        citation="https://www.capterra.com/",
        cache_allowed=False,
        store_allowed=False,
        derivation_allowed=False,
        restrictions=(
            (
                "reviews",
                "Capterra has no public API; the data is a Gartner Digital Markets "
                "product reachable only under a commercial feed agreement",
            ),
        ),
    ),
    "amazon_paapi": SourcePolicy(
        source="amazon_paapi",
        label="Amazon Product Advertising API",
        tier=SourceTier.RESTRICTED,
        citation="https://webservices.amazon.com/paapi5/documentation/",
        cache_allowed=False,
        store_allowed=False,
        derivation_allowed=False,
        restrictions=(
            ("api", "the Product Advertising API v5 is deprecated in favour of the Creators API"),
        ),
    ),
    "google_cse": SourcePolicy(
        source="google_cse",
        label="Google Custom Search JSON API",
        tier=SourceTier.RESTRICTED,
        citation="https://developers.google.com/custom-search/v1/overview",
        cache_allowed=False,
        store_allowed=False,
        derivation_allowed=False,
        restrictions=(
            ("api", "the Custom Search JSON API is closed to new customers"),
        ),
    ),
    "nominatim": SourcePolicy(
        source="nominatim",
        label="OpenStreetMap Nominatim",
        tier=SourceTier.RESTRICTED,
        citation="https://operations.osmfoundation.org/policies/nominatim/",
        cache_allowed=False,
        store_allowed=False,
        derivation_allowed=False,
        restrictions=(
            (
                "platform",
                "the Nominatim usage policy forbids no-code, low-code and "
                "vibe-coding platforms from building on the public API, which is "
                "what a harness is; Overpass is the supported way to read the "
                "same data",
            ),
        ),
    ),
    "google_places": SourcePolicy(
        source="google_places",
        label="Google Places",
        tier=SourceTier.LICENSED,
        citation="https://cloud.google.com/maps-platform/terms",
        cache_allowed=False,
        store_allowed=False,
        derivation_allowed=False,
        attribution_required=True,
        restrictions=(
            (
                "derivation",
                "the Maps Platform terms forbid creating content from Maps "
                "content",
            ),
            (
                "store",
                "only the place id may be stored; names, addresses and reviews "
                "may not be kept",
            ),
        ),
    ),
    "cloudflare_radar": SourcePolicy(
        source="cloudflare_radar",
        label="Cloudflare Radar",
        tier=SourceTier.ATTRIBUTED,
        citation="https://developers.cloudflare.com/radar/",
        attribution_required=True,
        commercial_use=False,
        restrictions=(
            (
                "commercial",
                "the Radar data is published under CC BY-NC 4.0, which permits "
                "non-commercial use only",
            ),
        ),
    ),
}


def policy_for(source: str) -> SourcePolicy | None:
    """The policy for one source, or None when the source is not in the table."""
    return SOURCE_POLICIES.get(source)


def consult(source: str, *, use: str, has_opt_in: bool = False) -> SourcePolicy:
    """The policy for one source, or a refusal.

    Refused when the source is not in the table, when its tier is restricted
    and the operator has not recorded why, or when the particular use is one
    the source does not permit. An operator who has recorded why is allowed
    through: the point of the record is that the operator, not the registry,
    is the one who decides.
    """
    policy = policy_for(source)
    if policy is None:
        raise SourceRefusedError(
            source=source,
            use=use,
            reason="the source is not in the registry, so its terms have not been looked at",
            remedy=(
                f"add a row for {source} to raven/sources/policy.py before using it, "
                "or pick a source that is already registered"
            ),
        )
    if policy.tier is SourceTier.RESTRICTED and not has_opt_in:
        raise SourceRefusedError(
            source=source,
            use=use,
            reason="the source's terms do not permit the use as it stands",
            remedy=(
                f"record why this deployment may use {source} "
                f"(raven sources optin {source} --basis \"...\"), or pick another source"
            ),
        )
    restricted = policy.restriction_for(use)
    if restricted is not None and not has_opt_in:
        raise SourceRefusedError(
            source=source,
            use=use,
            reason=restricted,
            remedy=(
                f"record why this deployment may use {source} for {use} "
                f"(raven sources optin {source} --basis \"...\"), or pick another source"
            ),
        )
    return policy
