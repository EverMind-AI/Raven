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
match replacement would leave a row nobody can act on.

The table is the four rules Codex uses for the same job
(``codex-rs/secrets/src/sanitizer.rs``) -- an OpenAI key, an AWS access key id,
a bearer token, and an assignment to a name that says secret -- plus one rule
Codex does not carry: PEM-armoured private key material. It is not one rule per
credential vendor: a vendor prefix is matched only when the prefix alone
identifies the value, and only for the two vendors that rule covers. Everything
else that carries a secret reaches this table through its label -- which is why
an ``aws_secret_access_key`` line and a ``~/.raven/beta.json`` token are caught
without either being named here.

``pem`` is the fifth rule and the only place this table goes beyond Codex. It is
structural rather than a vendor enumeration: a PEM block's opening delimiter is a
delimiter the way ``Bearer`` is a scheme word and ``password=`` is a label, so it
catches every key format those brackets wrap without naming a vendor. Codex can
leave it out because Codex has no ``agent/loop/failure_text.py``, the path that
persists a dispatcher traceback tail to disk here: a key cut in half by that cap
has no label left, and only the armour identifies it.

Two deviations from Codex's rules, both load-bearing:

* the assignment rule has no leading ``\\b`` and includes ``access_key``.
  ``~/.aws/credentials`` writes ``aws_secret_access_key = ...``, and a ``\\b``
  before ``secret`` cannot match after the ``_`` that precedes it, so the literal
  copy of Codex's rule misses the one file that makes this module worth having.
* the OpenAI value class keeps ``-`` (Codex's does not), so ``sk-ant-...`` and
  ``sk-proj-...`` are matched whole rather than not at all.

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

# Each pattern captures exactly the secret, never the label. Every pattern is
# applied, and a string already redacted no longer matches, so the order is not
# load-bearing; the bearer rule is first because that is where Codex puts it.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Codex rule 3. Matched anywhere rather than behind an ``Authorization``
    # label, so both ``Authorization: Bearer <token>`` and a bare ``Bearer
    # <token>`` lose the value. ``Basic`` and ``Token`` are not matched: neither
    # is in Codex's table, and a Basic value is base64 of ``user:password`` with
    # no label inside it to recognise.
    ("bearer", re.compile(r"(\bBearer[ \t]+)([A-Za-z0-9._~+/-]{16,}=*)", re.IGNORECASE)),
    # Codex rule 1, with ``-`` admitted in the value so the provider-prefixed
    # forms (``sk-ant-``, ``sk-proj-``) match whole. ``sk_live_`` is Stripe and
    # is not matched: ``_`` is not in the value class, and this repository has
    # no Stripe credential.
    ("openai", re.compile(r"()(sk-[A-Za-z0-9_-]{16,})")),
    # Codex rule 2, narrowed to the two prefixes a credentials file writes:
    # ``AKIA`` for a long-term key, ``ASIA`` for a temporary one.
    ("aws-access-key", re.compile(r"()(\b(?:AKIA|ASIA)[A-Z0-9]{16}\b)")),
    # Codex rule 4, with the leading ``\b`` dropped and ``access_key`` added --
    # see the module docstring. Eight characters, as in Codex: below that the
    # value is a placeholder or a word, not a credential. ``[`` is excluded from
    # the value so a replacement this module or ``held_secrets`` already wrote --
    # ``[redacted]``, ``[redacted: providers...apiKey]`` -- cannot be rewritten
    # again inside a labelled slot; no real credential begins with a bracket.
    (
        "named-secret",
        re.compile(
            r"((?:api[_-]?key|token|secret|password|access[_-]?key)"
            r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&|)\[]{8,})",
            re.IGNORECASE,
        ),
    ),
    # The fifth rule, and the one Codex does not have. A delimiter rather than a
    # vendor prefix: any PEM block whose header label ends in the key words opens
    # it, so every key format those brackets carry -- RSA, EC, DSA, OPENSSH,
    # ENCRYPTED, bare -- is one rule. Only the body goes; the armour stays, so the
    # reader can still tell a key was there.
    #
    # ``(?=-----END|\Z)``: the ``\Z`` arm is not optional. A body a cap cut in
    # half has no closing armour, and it is exactly that half-slain key that
    # ``agent/loop/failure_text.py`` writes to disk; ``-----END`` alone would
    # leave it whole.
    ("pem", re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)([\s\S]+?)(?=-----END|\Z)")),
    # Deliberately NOT here: a pattern for credential *paths*
    # (``~/.aws/credentials``, ``~/.ssh/id_rsa``). It was written and removed --
    # the path is not a secret, and hiding it takes away the one thing the reader
    # most needs, which is that the agent read their credential file. What must
    # not escape is the file's *contents*, and those arrive through a result
    # preview where the label and vendor patterns above catch them.
)

# Redacting a very large string costs real time on a streaming path, and the
# strings that reach :func:`redact` are titles, previews and messages -- not file
# bodies. Past the cap the text is truncated rather than scanned, because a
# half-scanned string is worse than an honestly shortened one. Tool results do
# not go through this cap: they have their own ceiling (1,000,000 characters)
# and :func:`redact_tool_result` scans them whole, so this one cannot silently
# cut a legitimate read short.
MAX_SCAN_CHARS = 256 * 1024

# At a known cut, a token may be too short for the ordinary patterns or have
# lost a closing delimiter. Keep ordinary unlabelled text, but remove a tail
# whose label or prefix already identifies it as a credential.
#
# One entry per surviving pattern -- the vendor prefixes share one because they
# share a value class -- so this is not a second credential enumeration. Each
# entry is paired with the characters its value is made of, which say where its
# search can start (_tail_search_start).
_TRUNCATED_SECRETS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (
        re.compile(r"()(?<![A-Za-z0-9_-])((?:sk-|AKIA|ASIA)[A-Za-z0-9_.-]*+)\Z"),
        re.compile(r"[A-Za-z0-9_.-]*+"),
    ),
    (
        re.compile(
            r"((?:api[_-]?key|token|secret|password|access[_-]?key)"
            r"[\"']?\s*+[:=]\s*+[\"']?)([^\s\"',;&|)\[]++)\Z",
            re.IGNORECASE,
        ),
        re.compile(r"[^\s\"',;&|)\[]*+"),
    ),
    (
        re.compile(r"(\bBearer[ \t]++)([A-Za-z0-9._~+/-]++)\Z", re.IGNORECASE),
        re.compile(r"[A-Za-z0-9._~+/-]*+"),
    ),
)

# What may stand between a label's name and its value: quotes, whitespace, a
# colon or an equals sign, and the scheme word an Authorization label can carry.
_LABEL_GAP = re.compile(r"[\s\"':=]*+")
_LABEL_SCHEME = re.compile(r"[A-Za-z]{0,6}+")
# Room for the name itself; the longest label in the table is ``access_key``.
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
    64,000 characters of ``token=`` ending in a comma take 10.8 seconds. A regex
    search for where that run starts retries every position too, 32 seconds for
    31,000 spaces before the last two words of a failure.
    """
    for pattern, value in _TRUNCATED_SECRETS:
        found = pattern.search(text, _tail_search_start(text, value))
        if found is not None:
            text = text[: found.start()] + found.group(1) + REPLACEMENT
    return text


def _scrub(text: str) -> str:
    """Every pattern applied to ``text`` whole. The caller owns its size."""
    for _, pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REPLACEMENT, text)
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
    text = _scrub(text)
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


# Raven's own token files name their credentials with words that mean nothing on
# their own: ``save_token`` writes ``asdict(MiniMaxOAuthToken)``, whose fields are
# ``access`` and ``refresh``. Adding those two names to ``_PATTERNS`` would be the
# cheap fix and the wrong one -- it rewrites "refresh the page" and "access the
# network" in every tool result. So this pair is fenced twice over: the call must
# have reached Raven's OAuth directory (below), and the value must look like a
# token rather than a word -- sixteen characters or more, no whitespace, no
# quote. Both conditions together are what the label patterns cannot express for
# a name this ordinary.
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
    ignores case, as the filesystem does; on POSIX a separator is one or more
    ``/`` and case counts. Between two segments a separator may repeat and a
    ``.`` segment may sit between them, as ``.raven\\.\\oauth`` is the same
    directory as ``.raven\\oauth``; the arguments are matched as the call wrote
    them, and no caller normalizes a path first. A store not under the home
    (``RAVEN_HOME`` elsewhere) has no abbreviation to add and is matched by its
    absolute path alone, which is the only spelling a call can use for it.

    This is the only path check left in this module. The pattern table is applied
    to every tool result regardless of path, so there is no "which directories
    count as credentials" list to keep current.
    """
    from raven.home import oauth_home

    style = _path_style()
    flavour, sep, flags = _host_paths(style)
    store = flavour(str(oauth_home()))
    home_dir = flavour(os.path.expanduser("~"))
    gap = sep if style == _WINDOWS else "/+"
    gap += r"(?:\." + gap + r")*"
    roots = [_spelled(store, gap)]
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
        roots += [re.escape(prefix) + gap + _spelled(rel, gap) for prefix in ("~", "$HOME", "${HOME}")]
    return re.compile(r"(?:" + "|".join(dict.fromkeys(roots)) + r")(?:[/\\]|$|[\"'\s])", flags)


def _redact_oauth_store(text: str) -> str:
    """Redact the credential fields of Raven's own token store file.

    Only the value is replaced, so the file's shape still says what was read --
    the same trade the pattern table makes for another program's settings.
    """
    return _OAUTH_TOKEN_FIELD.sub(lambda m: m.group(1) + REPLACEMENT, text)


def redact_tool_result(arguments: Any, text: str) -> str:
    """``text`` from a tool call, with every recognised credential replaced.

    The pattern table runs on every tool result, whatever path the call named.
    That is the point: the alternative is a list of directories that hold
    credentials, and such a list fails silently -- a credential file it does not
    name reaches the model in plaintext, and no test goes red. A list was tried
    and dropped for that reason (``~/.raven/beta.json``, whose ``token`` field is
    a deploy token, was in the gap).

    What the unconditional pass costs is the ability to read a file that
    contains a label-shaped line -- ``token = self.get_token(request)`` in a
    project's source, for instance -- without that line being rewritten. With
    four patterns and an eight-character floor the false-positive surface is
    small, and it is the price of not maintaining a list.

    The one path-sensitive pass left is Raven's own OAuth store, for the token
    fields whose names (``access``, ``refresh``) are ordinary words: see
    :data:`_OAUTH_TOKEN_FIELD`. It is gated on the read reaching that directory,
    so it costs nothing anywhere else, and prose is safe for a reason narrower
    than the gate: the field shape is required (a quoted ``access`` or
    ``refresh``, a colon, then sixteen characters with no space or quote), so a
    sentence saying "refresh the page" is untouched, while a passage that quotes
    a token sample is rewritten with the rest of the passage intact.

    ``text`` is scanned whole rather than through :data:`MAX_SCAN_CHARS`: a tool
    result may legitimately be a megabyte, and that cap exists for ACP frames.
    """
    if not text:
        return text
    strings = _argument_strings(arguments)
    store = _own_oauth_store()
    if any(store.search(value) for value in strings):
        text = _redact_oauth_store(text)
    return _scrub(text)


# A mapping key that names its value a secret. The structure carries the label
# here, which a per-string pass cannot see: in ``{"AWS_SECRET_ACCESS_KEY": "wJal..."}``
# the value on its own is indistinguishable from a hash. This is the ``mcpServers``
# ``env`` dict channel, which is one of the reasons this module exists. The same
# five names as ``named-secret``, for the same reason it has them.
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|access[_-]?key)", re.IGNORECASE)


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


__all__ = ["MAX_SCAN_CHARS", "REPLACEMENT", "pattern_names", "redact", "redact_tool_result", "redact_value"]
