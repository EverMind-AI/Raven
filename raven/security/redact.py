"""Strip credentials from Raven's own published tool and protocol content.

An ACP payload is a publishing surface: a tool title, a permission prompt and an
error message are all rendered in an editor, and some of them are persisted in
its transcript. Four channels carry secrets into that surface today, all of them
measured rather than imagined:

* a tool's arguments go on the wire verbatim, and for ``exec`` that is the whole
  command line -- ``curl -H "Authorization: Bearer sk-..."`` included;
* ``read_file`` has no fence by default (``restrict_to_workspace`` is false), so
  its result preview can be ``~/.aws/credentials``;
* every internal dispatcher error returns a ``traceback_tail``, whose last
  twelve lines carry absolute paths and sometimes argument values;
* an ``mcpServers`` entry carries an ``env`` dict.

What this is not: a general secret scanner. Only the capture group is replaced,
so the shape of the text survives and a person can still tell *what* was run --
``curl -H "Authorization: Bearer [redacted]"`` is a readable line, and a wholesale
match replacement would leave a row nobody can act on. The pattern list is the
same size as the one openclaw uses for the same job (about sixteen), and
deliberately not the 409-line RFC-7235 header scanner sitting next to it.

This table applies to Raven's own publishing surfaces. Content received from
another agent retains that agent's redaction policy.

Redaction is defence in depth, not the mechanism. A tool whose output must never
leave the process should not be put on the wire in the first place.
"""

from __future__ import annotations

import os
import re
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Any

REPLACEMENT = "[redacted]"

# Each pattern captures exactly the secret, never the label. Ordered
# roughly by how specific they are, though order does not change the result:
# every pattern is applied, and a string already redacted no longer matches.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Authorization / proxy headers, in either the -H "..." or the raw form.
    (
        "http-auth",
        re.compile(
            r"((?:Authorization|Proxy-Authorization)\s*+:\s*+(?:Bearer|Basic|Token)\s++)([\w\-.~+/=]{8,}+)",
            re.IGNORECASE,
        ),
    ),
    # An assignment to anything whose name says secret. Quoted or bare.
    (
        "named-secret",
        re.compile(
            r"((?:api[_-]?key|secret|token|password|passwd|pwd|credential|private[_-]?key|access[_-]?key|auth)"
            r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&|)]{6,})",
            re.IGNORECASE,
        ),
    ),
    # Command-line flags that take a credential.
    ("cli-flag", re.compile(r"(--(?:token|password|api-key|secret|with-token)[= ])([^\s\"']{6,})", re.IGNORECASE)),
    # A URL with an inline password: scheme://user:secret@host.
    ("url-userinfo", re.compile(r"(://[^\s/:@]+:)([^\s/@]{3,})(?=@)")),
    # Vendor-shaped keys, recognisable without a label because their prefix is
    # the label. The one class worth matching bare: a leaked key is a leaked key
    # whether or not somebody wrote "api_key" next to it.
    ("openai", re.compile(r"()(sk-(?:proj-|ant-|or-)?[A-Za-z0-9_-]{16,})")),
    ("github", re.compile(r"()(gh[pousr]_[A-Za-z0-9]{16,})")),
    ("gitlab", re.compile(r"()(glpat-[A-Za-z0-9_-]{16,})")),
    ("slack", re.compile(r"()(xox[abposr]-[A-Za-z0-9-]{10,})")),
    ("google", re.compile(r"()(AIza[0-9A-Za-z_-]{30,})")),
    ("aws-access-key", re.compile(r"()((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16})")),
    ("anthropic-legacy", re.compile(r"()(sk_live_[A-Za-z0-9]{16,})")),
    (
        "jwt",
        re.compile(r"()(?<![A-Za-z0-9_-])(eyJ[A-Za-z0-9_-]{10,}+\.[A-Za-z0-9_-]{10,}+\.[A-Za-z0-9_-]{10,}+)"),
    ),
    # A scan or call-record cap may have removed the closing armour.
    ("pem", re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)([\s\S]+?)(?=-----END|\Z)")),
    # A heredoc or echo piping a credential into a file.
    ("env-export", re.compile(r"((?:export|set)\s+[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=)(\S+)")),
    # Deliberately NOT here: a pattern for credential *paths*
    # (``~/.aws/credentials``, ``~/.ssh/id_rsa``). It was written and removed --
    # the path is not a secret, and hiding it takes away the one thing the reader
    # most needs, which is that the agent read their credential file. What must
    # not escape is the file's *contents*, and those arrive through a result
    # preview where the named-secret and vendor patterns above catch them.
)

# Redacting a very large string costs real time on a streaming path, and the
# strings that reach here are titles, previews and messages -- not file bodies.
# Past the cap the text is truncated rather than scanned, because a half-scanned
# string is worse than an honestly shortened one.
MAX_SCAN_CHARS = 256 * 1024

# At a known cut, a token may be too short for the ordinary patterns or have
# lost a closing delimiter. Keep ordinary unlabelled text, but remove a tail
# whose label or prefix already identifies it as a credential. Each pattern is
# paired with the characters its value is made of, which say where its search
# can start (_tail_search_start).
_TRUNCATED_SECRETS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (
        re.compile(
            r"()(?<![A-Za-z0-9_-])((?:sk-|gh[pousr]_|glpat-|xox[abposr]-|AIza|A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA|eyJ)"
            r"[A-Za-z0-9_.-]*+)\Z"
        ),
        re.compile(r"[A-Za-z0-9_.-]*+"),
    ),
    (re.compile(r"(://[^\s/:@]++:)([^\s/@]++)\Z"), re.compile(r"[^\s/@]*+")),
    (
        re.compile(
            r"((?:api[_-]?key|secret|token|password|passwd|pwd|credential|private[_-]?key|access[_-]?key|auth)"
            r"[\"']?\s*+[:=]\s*+[\"']?)([^\s\"',;&|)]++)\Z",
            re.IGNORECASE,
        ),
        re.compile(r"[^\s\"',;&|)]*+"),
    ),
    (
        re.compile(
            r"((?:Proxy-)?Authorization[\"']?\s*+[:=]\s*+[\"']?(?:(?:Bearer|Basic|Token)\s++)?)"
            r"([\w\-.~+/=]++)\Z",
            re.IGNORECASE,
        ),
        re.compile(r"[\w\-.~+/=]*+"),
    ),
    (
        re.compile(r"(--(?:token|password|api-key|secret|with-token)[= ])([^\s\"']++)\Z", re.IGNORECASE),
        re.compile(r"[^\s\"']*+"),
    ),
)

# What may stand between a label's name and its value: quotes, whitespace, a
# colon or an equals sign, and the scheme word an Authorization label can carry.
_LABEL_GAP = re.compile(r"[\s\"':=]*+")
_LABEL_SCHEME = re.compile(r"[A-Za-z]{0,6}+")
# Room for the name itself; the longest, Proxy-Authorization, is 19 characters.
_LABEL_NAME_CHARS = 32


def _tail_search_start(text: str, value: re.Pattern[str]) -> int:
    """The earliest position a tail pattern's match can begin at in ``text``.

    The match's value is the run of ``value``'s characters that ends the text, or
    lies inside it; its label's name ends where the gap, scheme word and gap in
    front of that run do. Each step matches the reversed text from where the last
    one stopped, so the walk reads those characters once, however wide a gap is.
    """
    reverse = text[::-1]
    end = value.match(reverse).end()
    for step in (_LABEL_GAP, _LABEL_SCHEME, _LABEL_GAP):
        end = step.match(reverse, end).end()
    return max(0, len(text) - end - _LABEL_NAME_CHARS)


def _redact_cut_tail(text: str) -> str:
    """``text`` with a credential that ends it replaced, for a text a cut ended.

    Each search starts where its match can (:func:`_tail_search_start`), and finds
    the match a search of the whole text finds. ``re.sub`` over the whole text
    retries every position instead, and a tail pattern's value runs to the end, so
    each retry before the final run walks on to the character that stops it:
    64,000 characters of ``pwd=`` ending in a comma take 10.8 seconds. A regex
    search for where that run starts retries every position too, 32 seconds for
    31,000 spaces before the last two words of a failure.
    """
    for pattern, value in _TRUNCATED_SECRETS:
        found = pattern.search(text, _tail_search_start(text, value))
        if found is not None:
            text = text[: found.start()] + found.group(1) + REPLACEMENT
    return text


def redact(text: str, *, truncated: bool = False) -> str:
    """Return ``text`` with every recognised credential replaced.

    Idempotent: the replacement token matches none of the patterns, so a string
    that has already been through here is unchanged by a second pass. That
    matters because the same text can be redacted at more than one layer.
    ``truncated`` says a caller already cut the input, so recognized credential
    tails must be removed even without their usual length or closing delimiter.
    """
    if not text:
        return text
    scan_cut = len(text) > MAX_SCAN_CHARS
    if scan_cut:
        text = text[:MAX_SCAN_CHARS]
    if truncated or scan_cut:
        text = _redact_cut_tail(text)
    for _, pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REPLACEMENT, text)
    if scan_cut:
        text += "\n[truncated before scanning for credentials]"
    return text


_POSIX, _WINDOWS = "posix", "windows"


def _path_style() -> str:
    """The path spelling this host compares by. A seam, so a test can drive the
    other host's branch: a Linux run cannot read a Windows path."""
    return _WINDOWS if os.name == "nt" else _POSIX


def _host_paths(style: str) -> tuple[type[PurePath], str, int]:
    """The path flavour, the separator pattern and the regex flags ``style`` compares by.

    Windows takes either slash, repeated -- a path quoted inside a script
    argument arrives with ``json.dumps``' doubled backslashes -- and ignores
    case, because its filesystem does. POSIX keeps ``/`` and its case.
    """
    if style == _WINDOWS:
        return PureWindowsPath, r"[/\\]+", re.IGNORECASE
    return PurePosixPath, "/", 0


def _spelled(path: PurePath, sep: str) -> str:
    """``path`` as a pattern, segment by segment, joined by ``sep``.

    Escaping ``str(path)`` whole would freeze the separators inside the path
    to the one the host prints; matching by segment lets every separator in
    it take the host's alternatives.
    """
    parts = [re.escape(part) for part in path.parts[1:]] if path.anchor else [re.escape(p) for p in path.parts]
    if not path.anchor:
        return sep.join(parts)
    head = re.escape(path.anchor.rstrip("/\\"))
    if path.anchor.endswith(("/", "\\")):
        head += sep
    return head + sep.join(parts)


def _argument_strings(arguments: Any, *, depth: int = 0) -> list[str]:
    """Every string in a call's arguments, decoded, as the caller wrote it.

    Matched one by one rather than as ``json.dumps`` output, which escapes
    every backslash and so hides a Windows path from a pattern built from the
    path itself. Keys are included: a tool may take paths as mapping keys.
    """
    if depth > 12:
        return []
    if isinstance(arguments, str):
        return [arguments]
    if isinstance(arguments, dict):
        found: list[str] = []
        for key, value in arguments.items():
            found += _argument_strings(key, depth=depth + 1) + _argument_strings(value, depth=depth + 1)
        return found
    if isinstance(arguments, (list, tuple)):
        return [text for item in arguments for text in _argument_strings(item, depth=depth + 1)]
    return [] if arguments is None else [str(arguments)]


#: The subdirectories of Raven's home a coding turn reads its own source from.
#: Exempt from the home-config pass. Not a claim that the rest of the home holds
#: no source-shaped text: it is the set known to hold source a turn edits, and a
#: new directory of that kind has to be added here when one appears. The
#: neighbours that are left out are each left out on purpose, with the reason in
#: :func:`_home_config`: ``bridge`` (a compiled install of the shipped bridge, not
#: the user's tree), ``traces`` and ``telemetry`` (written by the tracing and usage
#: code), ``subagent_sessions`` (per-product runtime state), ``playbooks`` (which
#: resolves under ``workspace`` by default and so is covered by the first entry).
_OWN_SOURCE_DIRS = ("workspace", "tmp", "plugins", "agents")

# Raven's own token files name their credentials with words that mean nothing on
# their own: ``save_token`` writes ``asdict(MiniMaxOAuthToken)``, whose fields are
# ``access`` and ``refresh``. Adding those two names to ``_PATTERNS`` would be the
# cheap fix and the wrong one -- it rewrites "refresh the page" and "access the
# network" in every tool result, a coding turn's included. So this pair is fenced
# twice over: the call must have reached Raven's OAuth directory (below), and the
# value must look like a token rather than a word -- sixteen characters or more,
# no whitespace, no quote. Both conditions together are what the label patterns
# cannot express for a name this ordinary.
_OAUTH_TOKEN_FIELD = re.compile(r'("(?:access|refresh)"\s*:\s*")([^\s"\\]{16,})')


def _own_oauth_store() -> re.Pattern[str]:
    """A path into Raven's OAuth token directory, in the spellings calls have used.

    The directory is asked of ``raven/home.py``, which owns where the store
    sits under the home, rather than assembled here from ``raven_home()``: a
    store that moves has to move this pattern with it, or the pass keeps
    pointing at a directory no writer uses and every test built the same way
    stays green. The config shelf's own name for that path is
    ``config.paths.oauth_dir``, which answers with the same function, so the
    writers and this pattern cannot drift apart. ``oauth_home()`` and not
    ``get_oauth_dir()`` for the same reason that helper's docstring gives: the
    latter creates and chmods the directory, and a redaction pass must not have
    side effects on the filesystem it is reading about.

    One root covers every provider: the writers all derive from ``get_oauth_dir()``
    (``minimax_oauth.token_path``, ``chatgpt_token.token_dir``,
    ``litellm_setup._point_oauth_tokens_at_raven`` for Copilot), and each of those
    overrides its own directory by environment variable when the driver owns it,
    which is a read outside the home and was never this pass's to redact.

    The spellings are enumerated rather than claimed: the store's own path, and
    ``~`` / ``$HOME`` / ``${HOME}`` with the store's path relative to the home,
    each followed by ``/``, ``\\``, the end of the string or a quote -- never by
    more of a name, so ``oauth-other`` is not the store. Each root is built
    segment by segment in the host's terms (:func:`_host_paths`): on Windows
    every separator inside it is either slash, one or more, and the pattern
    ignores case, as the filesystem does; on POSIX a separator is ``/`` and
    case counts. A store not under the home (``RAVEN_HOME`` elsewhere) has no
    abbreviation to add and is matched by its absolute path alone, which is the
    only spelling a call can use for it.

    This is a second resolver beside :func:`_home_config`, not the same one: that
    one answers "is this any program's dot-directory of the home", this one "is
    this Raven's token store". They share ``raven_home()`` through
    ``home.oauth_home`` -- the one definition the config shelf's ``oauth_dir``
    also answers with -- and are asserted together wherever a change could make
    one wider than the other.
    """
    from raven.home import oauth_home

    flavour, sep, flags = _host_paths(_path_style())
    store = flavour(str(oauth_home()))
    home_dir = flavour(os.path.expanduser("~"))
    roots = [_spelled(store, sep)]
    # The shell spellings, derived from the store's own path rather than spelled
    # here. The old condition compared the store's *parent* with the home, which
    # held while the root was ``~/.raven`` and stopped holding the moment the
    # root became ``~/.raven/oauth`` -- the three abbreviations silently dropped
    # out of the pattern. Relative to the home is what the question was the
    # whole time.
    try:
        rel = store.relative_to(home_dir)
    except ValueError:
        rel = None
    if rel is not None and rel.parts:
        roots += [re.escape(prefix) + sep + _spelled(rel, sep) for prefix in ("~", "$HOME", "${HOME}")]
    return re.compile(r"(?:" + "|".join(dict.fromkeys(roots)) + r")(?:[/\\]|$|[\"'\s])", flags)


def _redact_oauth_store(text: str) -> str:
    """Redact the credential fields of Raven's own token store file.

    Only the value is replaced, so the file's shape still says what was read --
    the same trade the pattern table makes for another program's settings.
    """
    return _OAUTH_TOKEN_FIELD.sub(lambda m: m.group(1) + REPLACEMENT, text)


def _home_config() -> re.Pattern[str]:
    """A path into a dot-directory of the home, Raven's own source directories excepted.

    Raven's home holds four directories a coding turn reads its own source from:
    the default workspace and the channels' scratch directories, the user plugin
    directory (``plugins``, ``raven/core/plugin_stack.py``) and the installed
    vendored agents (``agents``,
    ``raven/agent/subagent/vendored_agents.py``). Rewriting ``token = ...`` lines
    in that source breaks every edit built from them, so those four stay exempt.
    They are the set known to hold source a turn edits, not a survey of the home:
    a directory that starts holding such source has to join ``_OWN_SOURCE_DIRS``,
    which is why that list is not called exhaustive. The neighbours measured when
    this list was narrowed are outside it on purpose:

    * ``bridge`` -- the WhatsApp bridge as installed under the home
      (``raven/config/paths.py``:``get_bridge_install_dir``), a copy of the
      shipped ``bridge/`` built by ``_build_bridge`` (``shutil.copytree`` then
      ``npm install`` and ``tsc``). It does hold source-shaped text, and the
      ``private token: string`` annotation in its ``src/server.ts`` is rewritten
      to ``private token: [redacted]`` by this pass -- a known cost, taken
      because it is a build the next ``ensure_bridge_dir`` replaces, not a tree a
      turn's edit belongs in. Its credentials are not in there: the bridge token
      and the Baileys auth state are siblings, under ``whatsapp-auth``.
    * ``traces`` (``raven/tracing/config.py``) and ``telemetry``
      (``raven/token_wise/usage_tracker.py``) -- written by the tracing and usage
      code, never read back as source.
    * ``subagent_sessions`` (``raven/config/product_render.py``) -- per-product
      runtime state: rendered config, ``TOOLS.md``, ``agent_memory``, caches.
    * ``cache`` (``raven/sandbox/compat_bin.py``, ``raven/providers/codex_client_version.py``)
      and ``runtime`` (``raven/cli/tui_commands.py``, a node install) -- build and
      download caches, regenerated rather than edited.
    * ``playbooks`` -- the user layer resolves to ``<workspace>/playbooks``
      (``raven/rpc/methods/playbooks.py``) unless ``playbooks.dir`` overrides it,
      so the default is already inside the first entry; an override elsewhere in
      the home was never covered by this pass and is not a directory name this
      list can exempt without also exempting ``bridge``-style build output.

    The home and the four names are compared in the host's terms
    (:func:`_host_paths`): on Windows either slash separates and case is
    ignored, so ``C:\\Users\\a\\.raven\\Workspace`` is the ``workspace`` directory; on POSIX
    ``/`` separates and ``Workspace`` is not ``workspace``. Either way a name
    ends at a separator, a quote or the end, so ``pluginsfoo`` is not exempt.
    Before the name, separators may repeat and ``.`` segments may sit between
    them: the arguments are matched as the call wrote them, no caller
    normalizes them first, and ``.raven//workspace`` is the same directory.

    The OAuth token store (``oauth/<provider>/...``) is deliberately not among
    them: it holds no source to edit, and a read of it is a credential file like
    another program's settings, so it loses its tokens to this pass —
    :func:`redact_home_config_read` additionally applies
    :data:`_OAUTH_TOKEN_FIELD` there, for the token fields whose names are
    ordinary words.

    Why the store is not left to the pass that already existed: from #833 until
    this change the whole of Raven's home was exempt here, on the argument that
    Raven's own keys are taken out by the exact-match pass over ``config.json``
    (``raven/config/held_secrets.py``). That pass reads ``config.json`` only, and
    it finds a credential by the name on it -- ``api_key``, ``token``, ``secret``.
    ``access`` and ``refresh``, which is what ``save_token`` writes, are ordinary
    English words and name nothing, so nothing caught the token files at all. The
    argument held for the keys it was about and never held for the store, which is
    why the exemption is now this list instead of the whole home.
    """
    from raven.home import raven_home

    style = _path_style()
    flavour, sep, flags = _host_paths(style)
    home_dir = flavour(os.path.expanduser("~"))
    home = _spelled(home_dir, sep) if len(home_dir.parts) > 1 else re.escape(home_dir.anchor.rstrip("/\\"))
    own = flavour(str(raven_home()))
    skip = ""
    if own.parent == home_dir and own.name.startswith("."):
        name = re.escape(own.name[1:])
        boundary = r"(?:/|\\|$|[\"'\s])"
        lead = sep if style == _WINDOWS else "/+"
        lead += r"(?:\." + lead + r")*"
        source = r"(?:" + lead + r"(?:" + "|".join(_OWN_SOURCE_DIRS) + r"))" + boundary
        skip = r"(?!" + name + r"(?:" + source + r"|(?:$|[\"'\s])))"
    return re.compile(r"(?:~|\$HOME|\$\{HOME\}|" + home + r")" + sep + r"\." + skip + r"[A-Za-z0-9_-]", flags)


def redact_home_config_read(arguments: Any, text: str) -> str:
    """``text`` redacted when the call that produced it reached into a dot-directory of the home.

    Another program's settings (``~/.qwen/settings.json``, ``~/.openclaw``,
    ``~/.aws``) hold its keys, and a turn diagnosing that program reads them
    whole more often than it is told not to -- measured, a model asked to
    connect an agent ran ``read_file`` on its settings and read its key back.
    Only those calls: the patterns here would also rewrite placeholders in a
    project's source and tests, which a coding turn then fails to edit.

    Raven's own OAuth store is the one place a second pass runs, for the token
    fields whose names (``access``, ``refresh``) are ordinary words -- see
    :data:`_OAUTH_TOKEN_FIELD`. It is gated on the read reaching that directory,
    so it costs nothing anywhere else. Prose is safe for a reason narrower than
    that: the field shape is required (a quoted ``access`` or ``refresh``, a colon,
    then sixteen characters with no space or quote), so a sentence saying "refresh
    the page" is untouched, while a passage that quotes a token sample -- a note
    explaining the file format, with ``"refresh": "<40 hex>"`` in it -- is rewritten
    with the rest of the passage intact. The gate is on the path, not on whether
    the text is prose.
    """
    if not text:
        return text
    strings = _argument_strings(arguments)
    store = _own_oauth_store()
    if any(store.search(value) for value in strings):
        text = _redact_oauth_store(text)
    config = _home_config()
    return redact(text) if any(config.search(value) for value in strings) else text


# A mapping key that names its value a secret. The structure carries the label
# here, which a per-string pass cannot see: in ``{"AWS_SECRET_ACCESS_KEY": "wJal..."}``
# the value on its own is indistinguishable from a hash. This is the ``mcpServers``
# ``env`` dict channel, which is one of the reasons this module exists.
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|secret|token|password|passwd|pwd|credential|private[_-]?key|access[_-]?key|auth)", re.IGNORECASE
)


def redact_value(value: Any, *, depth: int = 0) -> Any:
    """Redact every string inside a JSON-shaped value, structure intact.

    For ``rawInput`` / ``rawOutput`` / error ``data``, where the client wants the
    shape and the shape is not the problem. Bounded depth because the value comes
    from a tool and may be deeply nested or self-referential; past the bound the
    subtree is dropped rather than half-scanned.

    A mapping key that names a secret redacts its whole value, however deep. That
    is the one thing :func:`redact` structurally cannot do: it sees one string at
    a time, and the label lives in a different string.
    """
    if depth > 12:
        return "[too deeply nested to scan for credentials]"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            key: REPLACEMENT
            if isinstance(key, str) and _SECRET_KEY.search(key) and not isinstance(value[key], (dict, list, tuple))
            else redact_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item, depth=depth + 1) for item in value]
    return value


def pattern_names() -> tuple[str, ...]:
    """The names of the patterns, for a test that pins the table's size."""
    return tuple(name for name, _ in _PATTERNS)


__all__ = ["MAX_SCAN_CHARS", "REPLACEMENT", "pattern_names", "redact", "redact_value"]
