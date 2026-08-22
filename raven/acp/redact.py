"""Strip credentials out of anything on its way to the client.

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

Redaction is defence in depth, not the mechanism. A tool whose output must never
leave the process should not be put on the wire in the first place.
"""

from __future__ import annotations

import re
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
            r"((?:Authorization|Proxy-Authorization)\s*:\s*(?:Bearer|Basic|Token)\s+)([\w\-.~+/=]{8,})", re.IGNORECASE
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
    ("jwt", re.compile(r"()(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})")),
    # PEM blocks: the body, not the armour, so the reader still sees what it was.
    ("pem", re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)([\s\S]+?)(?=-----END)")),
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


def redact(text: str) -> str:
    """Return ``text`` with every recognised credential replaced.

    Idempotent: the replacement token matches none of the patterns, so a string
    that has already been through here is unchanged by a second pass. That
    matters because the same text can be redacted at more than one layer.
    """
    if not text:
        return text
    if len(text) > MAX_SCAN_CHARS:
        text = text[:MAX_SCAN_CHARS] + "\n[truncated before scanning for credentials]"
    for _, pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REPLACEMENT, text)
    return text


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
