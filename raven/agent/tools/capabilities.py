"""What each credential-bearing tool needs, in one place.

Providers have had a declarative credential model for a while --
``providers.auth`` names what each connection method requires and
``credential_status`` is the single authority on whether a provider is usable.
An AST invariant enforces that authority, because six surfaces once answered
the same question six ways and each looked reasonable alone.

Tools never got the equivalent. Three rules decide whether a tool is offered to
the model, one per family, each a different shape:

    web_search   a resolved key, asked of the built tool
    web_fetch    nothing -- always registered, a key only improves extraction
    media x3     an api_key *or* a model, either one counting as configured

Each rule is defensible where it sits. What is missing is anywhere to *read*
them. A deployer cannot ask what this install lacks, and since an unconfigured
tool stopped being registered there is no surface at all saying the capability
exists: the model is not offered it, no document lists it, and ``raven doctor``
reports on providers and memory but has never mentioned tools.

This module is that surface. It describes the rules rather than replacing them
-- ``is_configured`` mirrors the loop's judgement instead of inventing a second
one -- and ``tests/test_tool_capabilities.py`` pins the description against what
the loop actually registers, so the two cannot drift apart quietly. Drifting
descriptions of the same fact is the failure this exists to avoid repeating.

``deep_research`` is deliberately absent: it is moving to the sub-agent surface
and its tool is going away.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.config.schema import Config


class Need(Enum):
    """What stands between a capability and working."""

    #: Usable as shipped. A credential, if any, only improves it.
    NOTHING = "nothing"
    #: A credential this deployment already holds elsewhere; the capability
    #: needs switching on, not a new account.
    OWN_CREDENTIAL = "own_credential"
    #: An account and key the deployer has to go and obtain.
    NEW_ACCOUNT = "new_account"


@dataclass(frozen=True)
class Capability:
    """One credential-bearing tool, described for whoever decides what to set up."""

    #: The name the model sees, so a doctor row can be matched to a transcript.
    tool: str
    #: One line in the deployer's terms rather than the code's.
    summary: str
    need: Need
    #: Dotted config path the deployer would edit to switch this on.
    config_path: str = ""
    #: Environment variable accepted instead of the config field, if any.
    env_var: str = ""
    #: Where to go when ``need`` is NEW_ACCOUNT.
    obtain_from: str = ""
    #: Attribute on ``tools.media`` backing this tool, for the media family.
    media_attr: str = ""
    #: Stated before the deployer switches it on rather than after: these cost
    #: money per call, and one cannot run at all without prepaid credit.
    cost_note: str = ""


#: Ordered by how much the deployer has to do, least first, because that is the
#: order the question "what am I missing" wants answering in.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        tool="web_fetch",
        summary="Read a web page the agent already has the URL for",
        need=Need.NOTHING,
        config_path="tools.web.jinaApiKey",
    ),
    Capability(
        tool="image_generate",
        summary="Generate an image",
        need=Need.OWN_CREDENTIAL,
        config_path="tools.media.image.model",
        media_attr="image",
        cost_note="Billed per image.",
    ),
    Capability(
        tool="text_to_speech",
        summary="Generate speech from text",
        need=Need.OWN_CREDENTIAL,
        config_path="tools.media.speech.model",
        media_attr="speech",
        cost_note="Billed per call.",
    ),
    Capability(
        tool="video_generate",
        summary="Generate a video",
        need=Need.OWN_CREDENTIAL,
        config_path="tools.media.video.model",
        media_attr="video",
        cost_note="Billed per call; needs prepaid OpenRouter credit to run at all.",
    ),
    Capability(
        tool="web_search",
        summary="Search the web",
        need=Need.NEW_ACCOUNT,
        config_path="tools.web.search.apiKey",
        env_var="SERPER_API_KEY",
        obtain_from="https://serper.dev",
    ),
)


def _resolved_media(cap: Capability, config: "Config") -> Any:
    """This tool's media section with the OpenRouter borrow already applied."""
    return getattr(config.effective_media_config(), cap.media_attr)


def is_configured(cap: Capability, config: "Config") -> bool:
    """Whether this capability would be offered to the model right now.

    Delegates to the tool rather than deciding here. The rule for each family
    lives with the tool that owns the credential, so this module cannot become
    a second opinion about configured-ness -- which is the divergence
    ``providers.auth`` exists to prevent on the provider side, and the reason
    an AST invariant guards it there.
    """
    if cap.need is Need.NOTHING:
        return True
    if cap.media_attr:
        from raven.agent.tools.media_gen import _OpenRouterMediaTool

        return _OpenRouterMediaTool.is_configured(_resolved_media(cap, config))

    from raven.agent.tools.web import WebSearchTool

    return WebSearchTool.is_configured(config.tools.web.search.api_key)


def configured_from(cap: Capability, config: "Config") -> str:
    """Where a satisfied capability got its credential, for a doctor row.

    Reads keys to *report* on them, never to rule on whether anything is set
    up -- :func:`is_configured` answers that, and it asks the tools. The
    distinction matters to a deployer: "reusing the OpenRouter key you already
    have" and "needs a key" are different instructions, and a row that cannot
    tell them apart sends someone to create an account they already have.

    Empty when the capability is unconfigured, and for the ones needing nothing
    -- there is no credential to report on either.
    """
    if cap.need is Need.NOTHING or not is_configured(cap, config):
        return ""
    if cap.media_attr:
        # effective_media_config resolves the borrow, so a key present after it
        # but absent in the raw section came from the provider entry.
        raw = getattr(config.tools.media, cap.media_attr)
        if not raw.api_key and _resolved_media(cap, config).api_key:
            return "providers.openrouter.apiKey (borrowed)"
        return cap.config_path
    if config.tools.web.search.api_key:
        return cap.config_path
    return cap.env_var if os.environ.get(cap.env_var) else ""


__all__ = ["CAPABILITIES", "Capability", "Need", "configured_from", "is_configured"]
