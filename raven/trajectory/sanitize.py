"""Export sanitization for bug report packages — path removal and tree digests.

A Bug Report Package must not leak absolute local paths (POSIX, Windows drive,
or UNC), yet ``collect_bundle`` keeps the original absolute path of a missing
artifact in ``manifest.json`` and in un-rewritten ``*.artifact_path`` span
attributes, and free text (model output, tool output, user-typed problem
fields, residual-scan samples) can carry arbitrary paths. This module provides
the path parser shared by sanitization and the pre-export assertions, the
text- and tree-level sanitizers built on it, and the tree digest that freezes
an export snapshot's identity:

- :func:`find_absolute_paths` — locate absolute-path tokens in text. The rule
  is charset-independent: a ``/`` at the start of a line, after whitespace, or
  after a boundary character opens a token; a token before-quoted extends to
  the closing quote (spaces allowed), a bare token ends at whitespace or a
  terminator. URL paths are not local paths and are skipped. Over-matching is
  the accepted trade-off — a replaced ``/usr/bin/env`` costs little, a leaked
  path is a contract violation.
- :func:`sanitize_text` / :func:`sanitize_export_tree` — replace known local
  roots and every parsed token with ``[REDACTED:path]``; the tree variant also
  rewrites the structured ``missing_artifacts`` and ``*.artifact_path``
  fields, keeping the basename for diagnostics.
- :func:`scan_absolute_paths` — the assertion pass: re-run the same parser
  over a sanitized tree; any hit means the tree must not ship.
- :func:`tree_digest` — order-stable content digest over a directory tree,
  refusing symlinks and special files (an injected link can never "pass").
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Iterable

PATH_PLACEHOLDER = "[REDACTED:path]"

# Characters that may legitimately precede an absolute path in text or JSON.
# Closers (")", "]", "}") are deliberately absent: "[REDACTED:path]/name" must
# not re-open a token after the placeholder.
_BOUNDARY = set("\"'`=:([{,<>")
_QUOTES = ('"', "'", "`")
# Where a bare (unquoted) token ends. Quotes end a bare token too — a path
# glued to a closing quote was not part of the quoted string.
_BARE_END = set("\"'`)]},;:<>|")

# A URL right before the token start: the "path" belongs to the URL, not to
# this machine. Two shapes — already inside the authority/path, or exactly at
# the "//" after the scheme (":" is a boundary character, so the parser stops
# there first). "file" is exempt from the exemption: a file:// path IS a local
# path. Bounded lookbehind window — URLs in bundle text are line-local.
_URL_BEFORE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://[^\s]*$")
_URL_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):$")
_URL_WINDOW = 512


def _in_url(text: str, i: int) -> bool:
    window = max(0, i - _URL_WINDOW)
    match = _URL_BEFORE.search(text, window, i)
    if match is None and text.startswith("//", i):
        match = _URL_SCHEME.search(text, window, i)
    return match is not None and match.group(1).lower() != "file"


_DRIVE = re.compile(r"[A-Za-z]:[\\/]")


def _token_end(text: str, start: int, prev: str) -> int:
    """End of the path token opening at ``start`` (which holds its first char).

    A token preceded by a quote runs to the matching close quote on the same
    line (spaces and any Unicode inside); without one, or with an unterminated
    quote, it runs to the next whitespace or terminator character.
    """
    n = len(text)
    if prev in _QUOTES:
        newline = text.find("\n", start)
        line_end = n if newline == -1 else newline
        close = text.find(prev, start, line_end)
        if close != -1:
            return close
    j = start
    while j < n and not text[j].isspace() and text[j] not in _BARE_END:
        j += 1
    return j


def find_absolute_paths(text: str) -> list[tuple[int, int]]:
    """Spans of absolute-path tokens in ``text`` (POSIX, Windows drive, UNC).

    The single parser behind sanitization and the export assertions — both
    must agree on what a path is, or a "clean" scan would not imply a clean
    package.
    """
    spans: list[tuple[int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        prev = text[i - 1] if i > 0 else ""
        at_boundary = i == 0 or prev.isspace() or prev in _BOUNDARY
        if not at_boundary:
            i += 1
            continue
        if ch == "/":
            if _in_url(text, i):
                i = _token_end(text, i, "")
                continue
            end = _token_end(text, i, prev)
            if text[i:end].strip("/"):
                spans.append((i, end))
            i = max(end, i + 1)
        elif ch == "\\" and text.startswith("\\\\", i):
            end = _token_end(text, i, prev)
            if len(text[i:end].strip("\\")) > 0:
                spans.append((i, end))
            i = max(end, i + 1)
        elif _DRIVE.match(text, i):
            # Scan past the "X:" prefix — ":" is a bare-token terminator, but
            # here it is part of the drive spelling, not a boundary.
            end = _token_end(text, i + 2, prev)
            spans.append((i, end))
            i = max(end, i + 1)
        else:
            i += 1
    return spans


def _root_patterns(roots: Iterable[str]) -> list[re.Pattern[str]]:
    patterns = []
    for root in sorted({r.rstrip("/\\") for r in roots if r and r.strip("/\\")}, key=len, reverse=True):
        # The root plus any trailing segments, however the path continues.
        patterns.append(re.compile(re.escape(root) + r"[^\s\"'`)\]},;:<>|]*"))
    return patterns


def sanitize_text(value: str, roots: Iterable[str] = ()) -> str:
    """``value`` with known local roots and every absolute-path token replaced.

    Known roots are replaced wherever they appear (no boundary requirement — a
    root glued to other text still identifies this machine); everything else
    goes through :func:`find_absolute_paths`.
    """
    for pattern in _root_patterns(roots):
        value = pattern.sub(PATH_PLACEHOLDER, value)
    spans = find_absolute_paths(value)
    if not spans:
        return value
    out: list[str] = []
    last = 0
    for start, end in spans:
        out.append(value[last:start])
        out.append(PATH_PLACEHOLDER)
        last = end
    out.append(value[last:])
    return "".join(out)


def _is_abs_path_value(value: str) -> bool:
    return value.startswith("/") or value.startswith("\\\\") or bool(_DRIVE.match(value))


def _basename(value: str) -> str:
    if value.startswith("/"):
        return value.rstrip("/").rsplit("/", 1)[-1]
    return PureWindowsPath(value).name or value


def _redacted_ref(value: str) -> str:
    """``[REDACTED:path]/<basename>`` — the diagnostic identity survives."""
    name = _basename(value)
    return f"{PATH_PLACEHOLDER}/{name}" if name else PATH_PLACEHOLDER


def _walk_files(root: Path) -> list[Path]:
    """Regular files under ``root``; symlinks and special files are refused —
    sanitizing through a link could rewrite files outside the tree."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in sorted(dirnames):
            if (base / name).is_symlink():
                raise ValueError(f"unsupported entry in tree: {(base / name).relative_to(root)}")
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"unsupported entry in tree: {path.relative_to(root)}")
            files.append(path)
    return files


def sanitize_export_tree(root: Path, roots: Iterable[str] = ()) -> None:
    """Sanitize a redacted bundle tree in place before it may be exported.

    Structured fields first — ``manifest.json``'s ``missing_artifacts`` and
    any absolute ``*.artifact_path`` value in ``spans.jsonl`` become
    ``[REDACTED:path]/<basename>`` — then every text file goes through
    :func:`sanitize_text`. Non-UTF-8 files are left as-is (redaction already
    excluded them from the copy; anything else is refused by the assertion
    pass, which reports them).
    """
    root = root.resolve()
    manifest_path = root / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            manifest = None
        if isinstance(manifest, dict) and isinstance(manifest.get("missing_artifacts"), list):
            manifest["missing_artifacts"] = [
                _redacted_ref(item) if isinstance(item, str) and _is_abs_path_value(item) else item
                for item in manifest["missing_artifacts"]
            ]
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    spans_path = root / "spans.jsonl"
    if spans_path.is_file():
        lines = []
        changed = False
        for line in spans_path.read_text(encoding="utf-8").splitlines():
            try:
                span = json.loads(line)
            except json.JSONDecodeError:
                lines.append(line)
                continue
            attrs = span.get("attributes") if isinstance(span, dict) else None
            if isinstance(attrs, dict):
                for key, value in attrs.items():
                    if key.endswith(".artifact_path") and isinstance(value, str) and _is_abs_path_value(value):
                        # null, not a placeholder path: replay's _artifact_path
                        # treats a non-string as "missing at pack time", while a
                        # relative placeholder outside artifacts/ would raise.
                        # The sanitized basename survives in missing_artifacts.
                        attrs[key] = None
                        changed = True
            lines.append(json.dumps(span, ensure_ascii=False))
        if changed:
            spans_path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    for path in _walk_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        sanitized = sanitize_text(text, roots)
        if sanitized != text:
            path.write_text(sanitized, encoding="utf-8")


def scan_absolute_paths(root: Path, roots: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Assertion pass: ``(relative file, sample)`` for every remaining hit.

    Runs the same parser sanitization used, so an empty result is the proof
    the tree carries no absolute paths. A non-UTF-8 file is itself a finding —
    it cannot be verified clean.
    """
    root = root.resolve()
    hits: list[tuple[str, str]] = []
    root_needles = sorted({r.rstrip("/\\") for r in roots if r and r.strip("/\\")})
    for path in _walk_files(root):
        rel = str(path.relative_to(root))
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            hits.append((rel, "non-UTF-8 content cannot be verified"))
            continue
        for start, end in find_absolute_paths(text):
            hits.append((rel, text[start:end][:120]))
        for needle in root_needles:
            if needle in text:
                hits.append((rel, needle))
    return hits


def tree_digest(root: Path) -> str:
    """Order-stable sha256 over every entry of a directory tree.

    One line per entry — ``<relpath>\\0d\\n`` for a directory,
    ``<relpath>\\0f\\0<sha256(content)>\\n`` for a file, relative paths in
    ascending order — hashed together. Symlinks and special files raise: a
    tree containing one has no trustworthy digest.
    """
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    entries: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in dirnames + filenames:
            entries.append((str((base / name).relative_to(root)), base / name))
    digest = hashlib.sha256()
    for rel, path in sorted(entries, key=lambda e: e[0]):
        if path.is_symlink():
            raise ValueError(f"unsupported entry in tree: {rel}")
        if path.is_dir():
            digest.update(f"{rel}\0d\n".encode("utf-8"))
        elif path.is_file():
            content = hashlib.sha256(path.read_bytes()).hexdigest()
            digest.update(f"{rel}\0f\0{content}\n".encode("utf-8"))
        else:
            raise ValueError(f"unsupported entry in tree: {rel}")
    return f"sha256:{digest.hexdigest()}"


__all__ = [
    "PATH_PLACEHOLDER",
    "find_absolute_paths",
    "sanitize_export_tree",
    "sanitize_text",
    "scan_absolute_paths",
    "tree_digest",
]
