"""How a provider is connected to: what material it needs, and whether it is there.

Authentication used to be described by one boolean (``ProviderSpec.is_oauth``)
and one string (``env_key``). Underneath sit shapes those two cannot express:
Azure needs a key *and* an address; Gemini takes a key *or* a list of them;
Bedrock needs neither because the environment already holds AWS credentials;
four providers hold a token in a file, written by three unrelated flows.

Because the shape was not stated, "is this provider usable" was answered
independently wherever it was needed, and the answers diverged. Routing skipped
a Gemini section configured with ``api_key_list``; ``provider list`` showed the
same section as ready; startup refused to run on it. Azure with a key and no
address was accepted by routing and display and rejected at startup.

So the requirement is declared here, once, as **an AND of OR-groups**: every
group must be satisfied, and any member satisfies its group. That is the whole
grammar, and it is enough for all eight shapes -- "key and address" is two
groups, "key or key list" is one group with two members.

Callers ask :func:`credential_status`. What they must not do is re-derive the
answer from spec flags, which is what produced the divergence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.providers.endpoints import provider_endpoints

if TYPE_CHECKING:
    from raven.providers.registry import ProviderSpec

#: A credential arrives as a token file rather than a config field.
KIND_DEVICE_FLOW = "device_flow"
#: The environment already holds it (AWS credential chain, Google ADC).
KIND_AMBIENT = "ambient"
#: An address is all that is needed; there is no credential.
KIND_NONE = "none"
#: A key held in the config. Which fields count is the method's ``requires``.
KIND_API_KEY = "api_key"


@dataclass(frozen=True)
class Requirement:
    """One thing that must be present, satisfied by any of ``fields``.

    ``fields`` names config fields; an empty tuple means the material is not in
    the config at all and ``token_file`` or ``ambient`` decides instead.
    """

    fields: tuple[str, ...]
    label: str
    hint: str = ""
    #: A ``ProviderSpec`` attribute that also satisfies this requirement when
    #: truthy, even though the config carries nothing for it -- e.g. custom's
    #: shipped localhost address, a working default the user may still
    #: override. Empty for every requirement but ``_ADDRESS_OR_SPEC_DEFAULT``.
    spec_fallback: str = ""

    def satisfied_by(self, section: Any, spec: "ProviderSpec | None" = None) -> bool:
        if any(_present(section, name) for name in self.fields):
            return True
        return bool(self.spec_fallback and spec is not None and getattr(spec, self.spec_fallback, None))


@dataclass(frozen=True)
class AuthMethod:
    """One way of connecting to a provider. A provider may offer several."""

    kind: str
    requires: tuple[Requirement, ...] = ()
    #: Checked instead of ``requires`` when the credential is not in the config.
    checks_token_file: bool = False
    label: str = ""

    def missing(
        self,
        section: Any,
        provider: str,
        *,
        spec: "ProviderSpec | None" = None,
        include_external: bool,
    ) -> list[Requirement]:
        """What this method still needs.

        ``include_external`` decides whether material held outside the config is
        looked at. Routing asks without it: it is choosing which section serves a
        model id, it runs on every call, and an OAuth provider's section is
        legitimately empty -- a token file read there would put disk I/O on the
        hot path and make the choice depend on a sign-in that startup is the
        right place to require. Display and startup ask with it, because both
        report on what is true right now.
        """
        if self.checks_token_file:
            if not include_external:
                return []
            return [] if _token_present(provider) else [_SIGN_IN(provider)]
        return [req for req in self.requires if not req.satisfied_by(section, spec)]


class MissingCredentialsError(Exception):
    """A provider cannot be used because its credentials are absent.

    Raised where the gate is decided, not where it is reported. The check runs
    behind three entry points -- the CLI, the gateway, and the TUI -- and used to
    end in ``console.print`` plus ``typer.Exit``, which is one of them speaking.
    Through the other two the message went to a log nobody was reading and the
    user got ``internal_error`` with ``exception_message: "1"``: the exit code,
    stringified.
    """

    def __init__(self, summary: str, *, provider: str = "", remedy: str = ""):
        super().__init__(summary)
        self.summary = summary
        self.provider = provider
        #: The command that fixes it, when there is one to name.
        self.remedy = remedy


@dataclass(frozen=True)
class CredentialStatus:
    """The single answer to "can this provider be used right now"."""

    provider: str
    ok: bool
    kind: str
    missing: tuple[Requirement, ...] = ()
    #: Which declared method is satisfied, or the first one when none is.
    method: AuthMethod | None = field(default=None, compare=False)

    @property
    def summary(self) -> str:
        """What to tell the user, naming the field rather than "No API key"."""
        if self.ok:
            return f"{self.provider} is configured"
        if not self.missing:
            return f"{self.provider} is not configured"
        parts = [m.hint or m.label for m in self.missing]
        return f"{self.provider} needs {', '.join(parts)}"


def _present(section: Any, name: str) -> bool:
    """Is this field set to something usable, on a model or a plain dict?

    Sections reach here as both: the schema object on the routing path, a raw
    mapping on the display path.

    Consumes ``provider_endpoints`` rather than re-deriving its precedence:
    ``endpoints`` set means only the resolved list counts -- flat fields
    included, api_key never inherited -- so a flat key alongside a keyless
    endpoint must not count as present, and an endpoint missing only its own
    address still counts once the section's flat address fills it in. Only
    ``api_key_list`` has no counterpart on ``ResolvedEndpoint`` (it collapses
    into several per-key entries there), so it is read off the section
    directly, and is unsatisfiable once ``endpoints`` is set -- ignored
    outright, same as the flat key.
    """
    if section is None:
        return False
    endpoints = section.get("endpoints") if isinstance(section, dict) else getattr(section, "endpoints", None)
    # isinstance, not truthiness: sections reach here as raw mappings and as
    # arbitrary duck-typed objects (test doubles included), and only a real
    # list is the endpoints shape provider_endpoints reads.
    if isinstance(endpoints, (list, tuple)) and endpoints:
        if name not in ("api_key", "api_base"):
            return False
        return any(bool(getattr(ep, name, None)) for ep in provider_endpoints(section))
    value = section.get(name) if isinstance(section, dict) else getattr(section, name, None)
    if isinstance(value, (list, tuple)):
        return any(bool(v) for v in value)
    return bool(value)


def _token_present(provider: str) -> bool:
    from raven.config.update_providers import _oauth_credentials_present

    return _oauth_credentials_present(provider)


def credential_files(provider: str) -> list[Path]:
    """Every file a sign-in for this provider can leave behind.

    Asked of the module that writes each family rather than derived a second
    time: each has its own override for where it puts things, so a second
    derivation is wrong exactly when a user has taken one -- that is how
    ``openai_codex`` came to be written under one name and read under another.

    Returns a list because a sign-in is not always one file: Copilot exchanges
    its token for an API key with a longer life, and clearing the token alone
    leaves a working credential behind. The first entry is the one that stands
    for the credential when a single path is needed.
    """
    from raven.config.paths import get_oauth_dir

    if provider == "github_copilot":
        from raven.config.update_providers import _COPILOT_TOKEN_FILES, _copilot_token_dir

        return [_copilot_token_dir() / name for name in _COPILOT_TOKEN_FILES]

    if provider == "openai_codex":
        from raven.providers.chatgpt_token import auth_file

        return [auth_file()]

    if provider in {"minimax_global", "minimax_cn"}:
        from raven.providers.minimax_oauth import token_path

        return [token_path("global" if provider == "minimax_global" else "cn")]

    return [get_oauth_dir() / f"{provider}.json"]


def _SIGN_IN(provider: str) -> Requirement:  # noqa: N802 - a constructor, named for the constant it stands in for
    public = provider.replace("_", "-")
    return Requirement((), "a sign-in", f"a sign-in -- run `raven provider login {public}`")


_KEY = Requirement(("api_key",), "an API key", "an API key -- run `raven provider set {public} --api-key <key>`")
_KEY_OR_LIST = Requirement(
    ("api_key", "api_key_list"),
    "an API key",
    "an API key -- run `raven provider set {public} --api-key <key>` (or --api-key-list k1,k2)",
)
_ADDRESS = Requirement(("api_base",), "an address", "an address -- run `raven provider set {public} --api-base <url>`")
#: Same requirement, plus the spec's own working default -- read through
#: `usable_default_api_base`, the same property `Config.get_api_base` serves
#: from, so the gate can never accept a default the reader then refuses to
#: hand out. Only for `requires_api_base`: that flag means the *user's*
#: address is mandatory (Azure, a bespoke endpoint) -- unless the spec ships
#: one anyway (`custom`'s localhost gateway). `is_local` keeps the plain
#: `_ADDRESS`: a local deployment's spec default (Ollama's standard port)
#: must not make it look configured before the user has pointed it anywhere,
#: which is the bug `_has_credentials`'s docstring already names.
_ADDRESS_OR_SPEC_DEFAULT = Requirement(
    ("api_base",),
    "an address",
    "an address -- run `raven provider set {public} --api-base <url>`",
    spec_fallback="usable_default_api_base",
)


#: Declarations for the providers whose shape the spec flags cannot express.
#: Everyone else is derived by :func:`auth_methods` from the flags themselves,
#: so adding an ordinary key-based vendor still needs no entry here.
_DECLARED: dict[str, tuple[AuthMethod, ...]] = {
    # A key or a list of them. The plural field is the one that shipped
    # unreadable to the router while displaying as configured.
    "gemini": (AuthMethod(KIND_API_KEY, (_KEY_OR_LIST,), label="API key"),),
    # No entry for Azure or the generic endpoint: `requires_api_base` already
    # derives exactly this, and a declaration identical to what the flags produce
    # is a second statement of one fact -- the thing this module exists to end.
    # The AWS credential chain already holds these; asking for a key would be
    # asking for something the user does not have in this form.
    "bedrock": (AuthMethod(KIND_AMBIENT, (), label="AWS credentials"),),
}


def auth_methods(spec: "ProviderSpec | None", name: str = "") -> tuple[AuthMethod, ...]:
    """Every way this provider can be connected to, most preferred first."""
    provider = name or (spec.name if spec else "")
    declared = _DECLARED.get(provider)
    if declared:
        return declared
    if spec is None:
        # A vendor Raven carries no spec for is reached with a key, like most.
        return (AuthMethod(KIND_API_KEY, (_KEY,), label="API key"),)
    if spec.is_oauth:
        return (AuthMethod(KIND_DEVICE_FLOW, checks_token_file=True, label="sign-in"),)
    if spec.is_local:
        return (AuthMethod(KIND_NONE, (_ADDRESS,), label="address"),)
    if spec.requires_api_base:
        return (AuthMethod(KIND_API_KEY, (_KEY, _ADDRESS_OR_SPEC_DEFAULT), label="key and endpoint"),)
    return (AuthMethod(KIND_API_KEY, (_KEY,), label="API key"),)


def credential_status(
    name: str,
    section: Any,
    *,
    spec: "ProviderSpec | None" = None,
    include_external: bool = False,
) -> CredentialStatus:
    """Can this provider be used, and if not, what exactly is missing?

    ``section`` is the provider's config -- the schema object or the raw mapping,
    either way. A provider offering several methods is usable when any one of
    them is satisfied, which is what lets a vendor be reached by an ambient
    credential when no key is set.

    ``include_external`` extends the question to material held outside the
    config, which today means an OAuth token file. See ``AuthMethod.missing``
    for why routing deliberately asks without it.
    """
    from raven.providers.registry import canonical_provider_name, find_by_name

    name = canonical_provider_name(name)
    spec = spec if spec is not None else find_by_name(name)
    methods = auth_methods(spec, name)

    unsatisfied: list[tuple[AuthMethod, tuple[Requirement, ...]]] = []
    for method in methods:
        gap = method.missing(section, name, spec=spec, include_external=include_external)
        if not gap:
            return CredentialStatus(name, True, method.kind, (), method)
        unsatisfied.append((method, tuple(_localize(req, name, section=section) for req in gap)))

    # Report against the first declared method: it is the preferred one, so its
    # gap is the shortest path to a working provider.
    method, gap = unsatisfied[0]
    return CredentialStatus(name, False, method.kind, gap, method)


#: Vendors LiteLLM reaches by API key, that Raven carries no spec for, where a
#: bare key still cannot configure them -- the credential shape needs more than
#: the wizard's generic "paste a key" branch offers. Every other unspecced
#: vendor is in fact reached by a single key; this only lists the ones that
#: are not, so ``key_refusal`` can say what actually gets each one working
#: instead of the wizard writing down a key that 401s (or, for chatgpt, is
#: silently ignored) at the first call.
#:
#: Deliberately not registry entries: a spec exists to drive routing (default
#: model, client selection, the connectivity probe), and none of these six get
#: any of that from Raven -- adding a spec just to hold a rejection message
#: would build the scaffolding this module exists to avoid.
_KEY_CANNOT_CONFIGURE: dict[str, str] = {
    "chatgpt": (
        "ChatGPT is reached through its own OAuth device flow -- LiteLLM's "
        "chatgpt transformation ignores any api_key and authenticates through a "
        "stored browser session instead. Raven already has this path: run "
        '`raven provider login openai-codex` (or pick "OpenAI Codex (OAuth)" '
        "from this menu)."
    ),
    "bedrock": (
        "Bedrock is reached through the AWS credential chain -- an access key "
        "and secret plus a region, an AWS profile, or ambient credentials from "
        "the environment or instance role -- not a single api_key field. Set "
        "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION (or an AWS "
        "profile) in the environment instead."
    ),
    "sagemaker": (
        "SageMaker is reached through the same AWS credential chain as "
        "Bedrock -- an access key and secret plus a region, an AWS profile, or "
        "ambient credentials -- not a single api_key field. Set "
        "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION (or an AWS "
        "profile) in the environment instead."
    ),
    "vertex_ai": (
        "Vertex AI needs a project and a location, plus either a service "
        "account credentials JSON or ambient Application Default Credentials -- "
        "not a single api_key field. Set VERTEXAI_PROJECT / VERTEXAI_LOCATION "
        "and either GOOGLE_APPLICATION_CREDENTIALS or run `gcloud auth "
        "application-default login`."
    ),
    "azure": (
        "This is LiteLLM's native Azure vendor, which needs an api_base and an "
        "api_version plus either an api_key or Entra ID auth -- a bare key is "
        "not enough. Raven's own Azure path (`azure_openai`) already asks for "
        "the base URL and version; pick that instead."
    ),
    "cloudflare": (
        "Cloudflare Workers AI needs an api_key plus either an api_base or an "
        "account_id -- a bare key alone is not enough. Configure --base-url "
        "(or an account id) alongside the key."
    ),
}


def key_refusal(vendor: str) -> str | None:
    """Why a bare API key cannot configure this vendor, or ``None`` if a key works.

    Checked before the onboarding wizard's generic key-only branch writes a
    config section for a vendor Raven carries no spec for, so it can refuse
    with the reason rather than persist a key that will never authenticate.
    """
    from raven.providers.registry import normalize_provider_name

    return _KEY_CANNOT_CONFIGURE.get(normalize_provider_name(vendor))


def _localize(req: Requirement, name: str, section: Any = None) -> Requirement:
    """Put the provider's own name into the hint, so it can be pasted.

    The hint names a command, not a config path: telling someone to hand-edit
    `~/.raven/config.json` asks them to know a file layout the CLI exists to
    hide, and the OAuth hint already gave a command.

    When the section carries ``endpoints``, a key hint must name
    ``endpoint add``: the flat field the generic hint writes to is ignored the
    moment endpoints exist (see ``_present``), so following that hint changes
    nothing and the gate repeats itself.
    """
    hint = req.hint
    endpoints = section.get("endpoints") if isinstance(section, dict) else getattr(section, "endpoints", None)
    if "api_key" in req.fields and endpoints:
        hint = "an API key on an endpoint -- run `raven provider endpoint add {public} --label <label> --api-key <key>`"
    if "{public}" not in hint:
        return req
    return Requirement(req.fields, req.label, hint.replace("{public}", name.replace("_", "-")), req.spec_fallback)
