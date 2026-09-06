"""Bundle redaction — a sanitized copy of a trajectory bundle, fit to leave the machine.

A bundle holds full model inputs/outputs and tool results, so it can carry
credentials (API keys, tokens) verbatim. Redaction produces a **copy** of the
bundle with those scrubbed — the original bundle is local corpus and is never
modified. Three layers, each catching what the previous one cannot:

1. **Known values** (:func:`collect_known_secrets`): the secret values this
   machine actually holds — secret-typed fields of the raven config (detected
   by the same name heuristics the config display faces use) plus
   credential-shaped environment variables — replaced everywhere by exact
   substring match, including their JSON-escaped spellings (a key quoted
   inside a JSON-in-JSON string). Config secrets are collected from **both**
   the validated model and the raw config JSON — the loader strips extension
   blocks (memory, skillForge, plugins, …) before validation, so only a raw
   walk sees the credentials those blocks carry — and from both the
   caller-named config file and the default one. Each value gets a stable,
   source-named placeholder such as
   ``[REDACTED:config.providers.anthropic.api_key]``.
2. **Pattern fallback** (:data:`PATTERNS`): regexes for common credential
   shapes (``sk-…``, ``Bearer …``, ``AKIA…``, ``ghp_…``, ``xoxb-…``, PEM
   private-key blocks, JWTs) — catches secrets layer 1 could not know about.
3. **Residual scan** (:func:`scan_residuals`): after redaction, flag
   suspicious leftovers (long high-entropy tokens) for a human to review
   before anything is shipped. This layer only reports; it never rewrites.

Binary policy: a file that does not decode as UTF-8 cannot be verified clean,
so it is **excluded** from the redacted copy and listed in the metadata —
today every bundle artifact is txt/json, so nothing is lost in practice.

The per-layer statistics, findings, and the binary policy are written to
``redaction.json`` in the redacted copy, so the report artifact documents its
own sanitization.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from pydantic import BaseModel

from raven import __version__

REDACTION_METADATA_FILE = "redaction.json"

_BINARY_POLICY = "non-UTF-8 files are excluded from the redacted copy (cannot be verified clean)"

# Floor for *suffix-inferred* environment variables only. Suffix matching runs
# over inherited shell state, where short non-secrets ("1", "true") are common
# and replacing them would corrupt the copy. Confirmed credentials carry no
# floor — neither config secret fields nor the explicitly listed env names —
# over-redacting ordinary text is more acceptable than leaking a short secret.
_ENV_MIN_SECRET_LEN = 6

# Values shorter than this are too unspecific for global substring matching.
# They are still redacted when delimited as standalone tokens, but must not
# rewrite ordinary identifiers such as ``session.key`` or ``marker``.
_MIN_UNBOUNDED_SECRET_LEN = 6

# "EMPTY" is the OpenAI-compatible ecosystem's dummy value for keyless local
# endpoints (vLLM/sglang; ModelEndpoint.api_key defaults to it). The exemption
# is scoped to api-key-named fields only — a password/token field literally
# set to "EMPTY" is a real credential and must be redacted.
_API_KEY_PLACEHOLDERS = {"EMPTY"}

# Same heuristics as config.update_providers/_channels `_is_secret_field`,
# duplicated here so the trajectory layer does not import CLI-facing modules;
# extended with names that appear as plain dict keys (headers, env blocks).
# Names are normalized (camelCase/hyphens -> snake_case) before matching, so
# the raw config spelling ("apiKey", "X-Api-Key") hits the same rules.
_SECRET_EXACT = {"token", "secret", "password", "api_key", "apikey", "authorization"}
_SECRET_SUFFIXES = ("_token", "_secret", "_key", "_password")
_KNOWN_SECRET_FIELDS = {"api_key_list"}

# Dict-valued fields whose *every* value is credential-bearing even though the
# keys are arbitrary (header names like "APP-Code"). Mirrors the provider
# display rule that redacts all extra_headers values while keeping keys.
_SECRET_DICT_FIELDS = {"extra_headers", "headers"}

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])([A-Z])")

# Environment variables collected as known secrets: a curated common set plus
# anything whose name ends in a credential suffix.
_ENV_EXACT = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
    "MOONSHOT_API_KEY",
    "MISTRAL_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HF_TOKEN",
    "SLACK_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
}
_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_ACCESS_KEY")

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # PEM blocks first: their base64 body must not be half-eaten by narrower
    # patterns. `.*?` crosses both real newlines and the literal `\n` escapes
    # a JSON-embedded PEM carries.
    (
        "private-key-block",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
    ),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-fine-grained-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer-token", re.compile(r"\bBearer +[A-Za-z0-9._~+/=-]{16,}")),
]

_CANDIDATE = re.compile(r"[A-Za-z0-9_\-+=]{20,}")
# Searched, not full-matched: artifact filenames embed the trace id
# (<seq>-<traceId>-<session>-<label>-<hash>.json), and every bundle references
# them — flagging those would put false positives in every report preview.
_TRACING_ID = re.compile(r"(?:trace|span|att)-[0-9a-f]{6,}")
# Provider-generated tool-call ids (``call_`` + alphanumerics) hit the entropy
# bar in every trajectory that carries a tool call — same nature as the
# tracing-id exemption above. Anchored full match: a credential merely
# containing ``call_`` does not qualify.
_CALL_ID = re.compile(r"^call_[A-Za-z0-9]+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PURE_DIGITS = re.compile(r"^[0-9]+$")
_HEX = re.compile(r"^[0-9a-f]+$|^[0-9A-F]+$")
# The span envelope stamps every artifact with its content checksum
# (``<key>.artifact_sha1``): 40-hex values that would otherwise flag in every
# bundle. Matched against the text right before a candidate — only the
# envelope's own field is excluded; hex in free text still flags.
_CHECKSUM_CONTEXT = re.compile(r"artifact_sha1\\?\"\s*:\s*\\?\"$")


def _flag_threshold(token: str) -> float:
    """Charset-aware entropy bar for flagging a residual candidate.

    Real credentials can be pure hex or pure letters, so neither shape may be
    skipped outright — instead each gets a bar tuned to its alphabet: hex
    caps at 4 bits/char, so random hex sits near 3.4-3.9 (bar 3.2); letter-only
    tokens get a higher bar (3.6) because long English identifiers reach ~3.5
    while random alpha starts near ~3.7. Mixed alnum keeps the default 3.5.
    """
    if _HEX.match(token):
        return 3.2
    if not any(ch.isdigit() for ch in token):
        return 3.6
    return 3.5


@dataclass(frozen=True)
class KnownSecret:
    """One sensitive value this machine holds, and where it came from."""

    label: str
    value: str


@dataclass
class ResidualFinding:
    """One suspicious token that survived redaction (layer 3 reports, never rewrites).

    ``token`` and ``occurrences`` live in memory only, for the review flow to
    adjudicate by value and render context; :meth:`RedactionReport.metadata`
    never serializes them — the plaintext and full source lines must not land
    in ``redaction.json``.
    """

    category: str
    sample: str
    file: str
    count: int = 1
    token: str = ""
    occurrences: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RedactionReport:
    """What one :func:`redact_bundle` run did, layer by layer."""

    bundle_dir: Path
    redacted_dir: Path
    exact: dict[str, int] = field(default_factory=dict)
    patterns: dict[str, int] = field(default_factory=dict)
    findings: list[ResidualFinding] = field(default_factory=list)
    skipped_binaries: list[str] = field(default_factory=list)
    config_loaded: bool = True

    @property
    def total_replacements(self) -> int:
        return sum(self.exact.values()) + sum(self.patterns.values())

    def metadata(self) -> dict[str, Any]:
        """The ``redaction.json`` payload shipped inside the redacted copy."""
        return {
            "redacted_at": datetime.now(timezone.utc).isoformat(),
            "raven_version": __version__,
            "exact_replacements": dict(self.exact),
            "pattern_replacements": dict(self.patterns),
            "residual_findings": [
                {"category": f.category, "sample": f.sample, "file": f.file, "count": f.count} for f in self.findings
            ],
            "skipped_binaries": list(self.skipped_binaries),
            "config_secrets_loaded": self.config_loaded,
            "binary_policy": _BINARY_POLICY,
        }


def _normalize_name(name: str) -> str:
    return _CAMEL_BOUNDARY.sub(r"_\1", name).replace("-", "_").lower()


def _name_is_secret(name: str) -> bool:
    normalized = _normalize_name(name)
    if normalized in _SECRET_EXACT or normalized in _KNOWN_SECRET_FIELDS:
        return True
    return any(normalized.endswith(suf) for suf in _SECRET_SUFFIXES)


def _name_is_secret_dict(name: str) -> bool:
    return _normalize_name(name) in _SECRET_DICT_FIELDS


def _field_is_secret(name: str, info: Any) -> bool:
    extra = getattr(info, "json_schema_extra", None)
    if isinstance(extra, dict) and extra.get("secret") is True:
        return True
    return _name_is_secret(name)


def _api_key_field(name: str) -> bool:
    normalized = _normalize_name(name)
    return normalized in {"api_key", "apikey"} or normalized.endswith("_api_key")


def _collectable(value: Any, default: Any = None, name: str = "") -> bool:
    """A string worth redacting: non-empty and not the schema default (a
    default is public knowledge, not this machine's secret). The "EMPTY"
    dummy is exempt only under api-key-named fields — see
    ``_API_KEY_PLACEHOLDERS``. No length floor — see ``_ENV_MIN_SECRET_LEN``."""
    if not isinstance(value, str) or not value or value == default:
        return False
    return not (value in _API_KEY_PLACEHOLDERS and _api_key_field(name))


def _secret_values(label: str, value: Any, default: Any = None, *, name: str = "") -> Iterator[KnownSecret]:
    if _collectable(value, default, name):
        yield KnownSecret(label, value)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if _collectable(item, name=name):
                yield KnownSecret(f"{label}[{i}]", item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if _collectable(item, name=str(key)):
                yield KnownSecret(f"{label}.{key}", item)


def _walk_value(value: Any, label: str) -> Iterator[KnownSecret]:
    if isinstance(value, BaseModel):
        yield from _walk_model(value, label)
    elif isinstance(value, dict):
        for key, item in value.items():
            sub = f"{label}.{key}"
            if isinstance(item, str):
                if _name_is_secret(str(key)):
                    yield from _secret_values(sub, item, name=str(key))
            elif isinstance(item, dict) and _name_is_secret_dict(str(key)):
                yield from _secret_values(sub, item)
            else:
                yield from _walk_value(item, sub)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _walk_value(item, f"{label}[{i}]")


def _walk_model(model: BaseModel, label: str) -> Iterator[KnownSecret]:
    for name, info in type(model).model_fields.items():
        value = getattr(model, name, None)
        if value is None:
            continue
        sub = f"{label}.{name}"
        if _field_is_secret(name, info):
            yield from _secret_values(sub, value, getattr(info, "default", None), name=name)
        elif isinstance(value, dict) and _name_is_secret_dict(name):
            yield from _secret_values(sub, value)
        else:
            yield from _walk_value(value, sub)
    # Extra sections (e.g. providers Raven has no spec for) are plain dicts.
    yield from _walk_value(model.model_extra or {}, label)


def _walk_raw(value: Any, label: str) -> Iterator[KnownSecret]:
    """Name-heuristic walk over a raw config dict.

    Runs on the file as written (before the loader strips extension blocks
    like memory/skillForge/plugins), so credentials those blocks carry are
    collected even though the validated model never sees them. Key spellings
    are the file's own (camelCase); ``_name_is_secret`` normalizes.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            sub = f"{label}.{key}"
            if isinstance(item, str):
                if _name_is_secret(str(key)) and _collectable(item, name=str(key)):
                    yield KnownSecret(sub, item)
            elif isinstance(item, dict) and _name_is_secret_dict(str(key)):
                yield from _secret_values(sub, item)
            elif isinstance(item, list) and _name_is_secret(str(key)):
                yield from _secret_values(sub, item, name=str(key))
            else:
                yield from _walk_raw(item, sub)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _walk_raw(item, f"{label}[{i}]")


def config_secrets(config: BaseModel) -> list[KnownSecret]:
    """Secret-typed values of a loaded raven config, labeled by dotted path."""
    return list(_walk_model(config, "config"))


def env_secrets(environ: Mapping[str, str] | None = None) -> list[KnownSecret]:
    """Credential-shaped environment variables, labeled ``env.<NAME>``.

    Variables named in ``_ENV_EXACT`` are known credentials and carry no
    length floor; the floor applies only to suffix-inferred names, where a
    short value is more likely shell junk than a secret.
    """
    env = os.environ if environ is None else environ
    out: list[KnownSecret] = []
    for name, value in env.items():
        if not isinstance(value, str) or not value:
            continue
        exact = name in _ENV_EXACT
        if not exact and not name.endswith(_ENV_SUFFIXES):
            continue
        if not exact and len(value) < _ENV_MIN_SECRET_LEN:
            continue
        if value in _API_KEY_PLACEHOLDERS and _api_key_field(name):
            continue
        out.append(KnownSecret(f"env.{name}", value))
    return out


def collect_known_secrets(
    config_path: Path | None = None, environ: Mapping[str, str] | None = None
) -> tuple[list[KnownSecret], bool]:
    """All known secret values on this machine, and whether config reading was complete.

    Config secrets come from the caller-named config file (if any) **and** the
    default config — a trajectory traced under ``--config alternate.json``
    must not leak the alternate file's keys just because the report ran on the
    default. Each file is read twice: through the validated model (canonical
    labels, explicit ``secret`` markers) and as raw JSON (extension blocks the
    loader strips, files the schema rejects). The flag is False when any
    existing config file could not be fully read — the pattern and residual
    layers still run, but the caller must surface the degradation: layer 1 is
    the only layer that knows this machine's actual values.
    """
    from raven.config.loader import ConfigReadError, get_config_path, load_config, read_raw_or_raise

    paths: list[Path] = []
    for candidate in (config_path, get_config_path()):
        if candidate is None:
            continue
        path = Path(candidate).expanduser()
        if not any(path.resolve() == known.resolve() for known in paths):
            paths.append(path)

    secrets: list[KnownSecret] = []
    seen: set[str] = set()
    complete = True

    def _add(found: Iterable[KnownSecret]) -> None:
        for secret in found:
            if secret.value not in seen:
                seen.add(secret.value)
                secrets.append(secret)

    for path in paths:
        try:
            _add(_walk_model(load_config(path), "config"))
        except Exception:
            complete = False
        if path.exists():
            try:
                _add(_walk_raw(read_raw_or_raise(path), "config"))
            except ConfigReadError:
                complete = False
    _add(env_secrets(environ))
    return secrets, complete


def _variants(value: str) -> set[str]:
    """The spellings a value can take inside bundle text.

    Bundle files are JSON (spans/session) holding stringified JSON payloads,
    so a value can appear raw, JSON-escaped, escaped with non-ASCII characters
    as ``\\uXXXX``, or double-escaped (JSON-in-JSON).
    """
    escaped = json.dumps(value, ensure_ascii=False)[1:-1]
    return {
        value,
        escaped,
        json.dumps(value, ensure_ascii=True)[1:-1],
        json.dumps(escaped, ensure_ascii=False)[1:-1],
    }


def _apply_exact(text: str, secrets: list[KnownSecret], counts: dict[str, int]) -> str:
    # One pass over the text with all variants as an alternation, longest
    # first (re picks the first alternative at a position, so longest-first
    # gives longest-match). Sequential str.replace re-scanned its own output:
    # a short value (a 1-char junk env key, say "k") then shredded the "k"
    # inside placeholders inserted for earlier, longer secrets.
    variants: dict[str, tuple[str, bool]] = {}
    for secret in secrets:
        for variant in _variants(secret.value):
            if variant and variant not in variants:
                variants[variant] = (secret.label, len(secret.value) < _MIN_UNBOUNDED_SECRET_LEN)
    if not variants:
        return text
    alternatives = []
    for variant in sorted(variants, key=len, reverse=True):
        escaped = re.escape(variant)
        if variants[variant][1]:
            escaped = rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
        alternatives.append(escaped)
    alternation = re.compile("|".join(alternatives))

    def _sub(match: re.Match[str]) -> str:
        label = variants[match.group(0)][0]
        counts[label] = counts.get(label, 0) + 1
        return f"[REDACTED:{label}]"

    return alternation.sub(_sub, text)


def _apply_patterns(text: str, counts: dict[str, int]) -> str:
    for name, pattern in PATTERNS:
        text, hits = pattern.subn(f"[REDACTED:pattern.{name}]", text)
        if hits:
            counts[name] = counts.get(name, 0) + hits
    return text


def _entropy(token: str) -> float:
    freq = Counter(token)
    n = len(token)
    return -sum(c / n * math.log2(c / n) for c in freq.values())


def _mask(token: str) -> str:
    return f"{token[:4]}***{token[-4:]}"


def _sample(line: str, start: int, end: int, window: int = 30) -> str:
    token = line[start:end]
    lo, hi = max(0, start - window), min(len(line), end + window)
    return (line[lo:start] + _mask(token) + line[end:hi]).strip()


def scan_residuals(root: Path) -> list[ResidualFinding]:
    """Layer 3: flag suspicious tokens left in an already-redacted tree.

    Reports long tokens whose Shannon entropy clears a charset-aware bar
    (:func:`_flag_threshold`) — pure hex and letter-only tokens included,
    since real credentials take both shapes. Skipped as benign: tracing ids
    (by prefix), provider tool-call ids (``call_…``), UUIDs, date-stamped
    names, and digit-only tokens (ids and quantities; a 10-symbol alphabet
    cannot clear any meaningful entropy bar). Deduped by token value.
    """
    findings: dict[str, ResidualFinding] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(root))
        for line_no, line in enumerate(text.splitlines(), 1):
            for match in _CANDIDATE.finditer(line):
                token = match.group(0)
                if "REDACTED" in token:
                    continue
                if _TRACING_ID.search(token) or _CALL_ID.match(token) or _UUID.match(token) or _DATE.search(token):
                    continue
                if _PURE_DIGITS.match(token):
                    continue
                if _CHECKSUM_CONTEXT.search(line[: match.start()]):
                    continue
                if _entropy(token) < _flag_threshold(token):
                    continue
                occurrence = {
                    "file": rel,
                    "line_no": line_no,
                    "line": line,
                    "start": match.start(),
                    "end": match.end(),
                }
                known = findings.get(token)
                if known:
                    known.count += 1
                    known.occurrences.append(occurrence)
                    continue
                category = "jwt-like" if token.startswith("eyJ") else "high-entropy"
                findings[token] = ResidualFinding(
                    category=category,
                    sample=_sample(line, match.start(), match.end()),
                    file=rel,
                    token=token,
                    occurrences=[occurrence],
                )
    return list(findings.values())


def redact_bundle(
    bundle_dir: Path,
    dest_dir: Path,
    *,
    secrets: list[KnownSecret] | None = None,
    config_path: Path | None = None,
) -> RedactionReport:
    """Write a redacted copy of ``bundle_dir`` at ``dest_dir``; never touch the original.

    ``secrets`` overrides layer 1's known values (tests, callers with their
    own collection); by default they are collected from the ``config_path``
    and default config files plus the environment (see
    :func:`collect_known_secrets`). ``dest_dir`` must not already exist — a fresh tree is the
    guarantee that nothing stale ships. Returns the :class:`RedactionReport`,
    whose metadata is also written to ``redaction.json`` inside the copy.
    """
    bundle_dir = bundle_dir.resolve()
    if not bundle_dir.is_dir():
        raise ValueError(f"bundle directory not found: {bundle_dir}")
    dest_dir = dest_dir.resolve()
    if dest_dir.exists():
        raise ValueError(f"destination already exists: {dest_dir}")
    if bundle_dir in (dest_dir, *dest_dir.parents):
        raise ValueError("destination must be outside the source bundle")

    config_loaded = True
    if secrets is None:
        secrets, config_loaded = collect_known_secrets(config_path)

    report = RedactionReport(bundle_dir=bundle_dir, redacted_dir=dest_dir, config_loaded=config_loaded)
    dest_dir.mkdir(parents=True)
    try:
        for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(bundle_dir)
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                report.skipped_binaries.append(str(rel))
                continue
            text = _apply_exact(text, secrets, report.exact)
            text = _apply_patterns(text, report.patterns)
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

        report.findings = scan_residuals(dest_dir)
        (dest_dir / REDACTION_METADATA_FILE).write_text(
            json.dumps(report.metadata(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except BaseException:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return report


__all__ = [
    "KnownSecret",
    "PATTERNS",
    "REDACTION_METADATA_FILE",
    "RedactionReport",
    "ResidualFinding",
    "collect_known_secrets",
    "config_secrets",
    "env_secrets",
    "redact_bundle",
    "scan_residuals",
]
