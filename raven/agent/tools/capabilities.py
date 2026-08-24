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

For the media family, being offered to the model and being usable are two
different questions: a section naming only a model is registered, because a
model alone counts as asking for the tool, and then every call fails on a
missing key. :func:`is_configured` answers the first and the tools' ``has_key``
answers the second -- collapsing them is how a report ends up ticking a
capability that cannot run.

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

#: The credential the media family falls back to, named once so what a doctor
#: row tells the deployer to set cannot drift from what is read here.
_OPENROUTER_KEY = "providers.openrouter.apiKey"


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
    #: Dotted config path the deployer would edit to switch this on. For the
    #: media family this is the *model* field, not a credential -- see
    #: :attr:`key_path`.
    config_path: str = ""
    #: Environment variable accepted instead of this capability's credential.
    env_var: str = ""
    #: Where to go when ``need`` is NEW_ACCOUNT.
    obtain_from: str = ""
    #: Attribute on ``tools.media`` backing this tool, for the media family.
    media_attr: str = ""
    #: Stated before the deployer switches it on rather than after: these cost
    #: money per call, and one cannot run at all without prepaid credit.
    cost_note: str = ""

    @property
    def key_path(self) -> str:
        """Dotted path to this capability's own credential field.

        Distinct from :attr:`config_path`, which for the media family is the
        model field: naming a model path as where a key came from sends the
        deployer to edit a line that holds no credential.
        """
        return f"tools.media.{self.media_attr}.apiKey" if self.media_attr else self.config_path


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
        env_var="OPENROUTER_API_KEY",
        cost_note="Billed per image.",
    ),
    Capability(
        tool="text_to_speech",
        summary="Generate speech from text",
        need=Need.OWN_CREDENTIAL,
        config_path="tools.media.speech.model",
        media_attr="speech",
        env_var="OPENROUTER_API_KEY",
        cost_note="Billed per call.",
    ),
    Capability(
        tool="video_generate",
        summary="Generate a video",
        need=Need.OWN_CREDENTIAL,
        config_path="tools.media.video.model",
        media_attr="video",
        env_var="OPENROUTER_API_KEY",
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
    """Whether this capability's credential gate is satisfied.

    Delegates to the tool rather than deciding here. The rule for each family
    lives with the tool that owns the credential, so this module cannot become
    a second opinion about configured-ness -- which is the divergence
    ``providers.auth`` exists to prevent on the provider side, and the reason
    an AST invariant guards it there.

    Not the same question as "is it offered", which :func:`is_offered` answers:
    a deployment can switch a fully credentialed tool off. See
    :func:`is_disabled`.
    """
    if cap.need is Need.NOTHING:
        return True
    if cap.media_attr:
        from raven.agent.tools.media_gen import _OpenRouterMediaTool

        return _OpenRouterMediaTool.is_configured(_resolved_media(cap, config))

    from raven.agent.tools.web import WebSearchTool

    return WebSearchTool.is_configured(config.tools.web.search.api_key)


def is_disabled(cap: Capability, config: "Config") -> bool:
    """Whether the deployment has switched this tool off by name.

    A separate state from unconfigured, and reported as one: a switched-off
    tool usually has its credential set, and calling it unconfigured would send
    the deployer to set a key that is already there.

    ``tools.disabledTools`` is applied after registration
    (``AgentLoop._apply_disabled_tools``), so this is the only thing standing
    between a satisfied credential gate and a tool the agent actually holds.
    """
    return cap.tool in (config.tools.disabled_tools or [])


def is_offered(cap: Capability, config: "Config") -> bool:
    """Whether the agent ends up holding this tool: credentialed and not off.

    The predicate that matches the final registry, which is what a report about
    available capabilities has to agree with.
    """
    return is_configured(cap, config) and not is_disabled(cap, config)


def has_credential(cap: Capability, config: "Config") -> bool:
    """Whether a credential actually resolves for this capability.

    A different question from :func:`is_configured`, which answers whether the
    deployment asked for the tool. They come apart for the media family: a
    section naming only a model is registered and offered to the model, and
    every call then fails on a missing key. A report that collapses the two
    ticks a capability that cannot run, which is the one thing it must not do.

    The same answer twice for the other families -- web_search is configured by
    a resolved key and nothing else, and web_fetch needs none -- so this only
    ever diverges where the rule itself does.
    """
    if cap.need is Need.NOTHING:
        return True
    if cap.media_attr:
        from raven.agent.tools.media_gen import _OpenRouterMediaTool

        return _OpenRouterMediaTool.has_key(_resolved_media(cap, config))
    return is_configured(cap, config)


def configured_from(cap: Capability, config: "Config") -> str:
    """Where a satisfied capability got its credential, for a doctor row.

    Reads keys to *report* on them, never to rule on whether anything is set
    up -- :func:`is_configured` answers that, and it asks the tools. The
    distinction matters to a deployer: "reusing the OpenRouter key you already
    have" and "needs a key" are different instructions, and a row that cannot
    tell them apart sends someone to create an account they already have.

    Empty when the capability is unconfigured, when it needs nothing, and --
    for the media family only -- when it is registered with no credential at
    all: a section naming just a model is offered to the model and fails on
    every call, so there is no source to name.

    Names a source; it does not rule on whether one exists. Callers wanting that
    answer ask :func:`has_credential`, which asks the tools -- inferring it from
    an empty string here would make the two facts one, and they are not.
    """
    if cap.need is Need.NOTHING or not is_configured(cap, config):
        return ""
    if cap.media_attr:
        if getattr(config.tools.media, cap.media_attr).api_key:
            return cap.key_path
        # effective_media_config resolves the borrow, so a key present after it
        # but absent in the raw section came from the provider entry.
        if _resolved_media(cap, config).api_key:
            return f"borrowed: {_OPENROUTER_KEY}"
    elif config.tools.web.search.api_key:
        return cap.key_path
    return cap.env_var if os.environ.get(cap.env_var) else ""


def borrowable_credential(cap: Capability, config: "Config") -> str:
    """Where an unconfigured media capability would get its key once switched on.

    "Reuse the OpenRouter key you already have" and "get a key as well" are
    different instructions, and only this tells them apart. Stating the first
    unconditionally is worse than saying nothing: the deployer sets a model,
    the tool is registered because a model alone counts, and every call then
    fails on a credential they were told they already had.

    Empty for the other families, whose own rows already name what to set.
    """
    if not cap.media_attr:
        return ""
    openrouter = config.providers.get("openrouter")
    if openrouter and openrouter.api_key:
        return _OPENROUTER_KEY
    return cap.env_var if os.environ.get(cap.env_var) else ""


__all__ = [
    "CAPABILITIES",
    "Capability",
    "Need",
    "borrowable_credential",
    "configured_from",
    "has_credential",
    "is_configured",
]
