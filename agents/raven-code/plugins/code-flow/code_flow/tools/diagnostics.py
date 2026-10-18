"""Bounded Python diagnostics attached to the edit that made them observable."""

from __future__ import annotations

import ast
import importlib.util
import io
import re
import sys
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from code_flow.tools.diagnostic_workers import WORKERS

MAX_SOURCE_BYTES = 256 * 1024
# The syntax check runs past the name checks' bound up to this one. Python's
# syntax tree of 2 MiB of source took about 0.3 s and 190 MiB to build, and of
# 16 MiB 7 s and 1.4 GiB, on a worker nothing can interrupt once it starts.
MAX_SYNTAX_BYTES = 2 * 1024 * 1024
MAX_FINDINGS = 8
CHECK_WAIT_SECONDS = 1.0
UNKNOWN = object()
# A marker counts the way Ruff reads one: an optional lowercase ``ruff:`` or
# ``flake8:`` prefix for the file-level form, ``noqa`` in any case, then either
# a colon and codes or, with no colon, whitespace, another comment or the
# comment's end. Anything else (a URL fragment, a quoted mention, a colon
# without codes) is text. Unlike Ruff, which takes the first directive in a
# comment, every directive in it counts.
_NOQA = re.compile(
    r"#\s*(?:(ruff|flake8)\s*:\s*)?(?i:noqa)"
    r"(?:\s*:\s*([A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*)|(?!\s*:)(?=[\s#]|$))"
)


@dataclass(frozen=True)
class Finding:
    code: str
    line: int
    column: int
    message: str
    source: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.code, self.message, self.source.strip()


def read_python(path: Path) -> str | None | object:
    """Read bounded source; distinguish an absent file from an unavailable baseline."""
    try:
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            return UNKNOWN
        with path.open("rb") as handle:
            raw = handle.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            return UNKNOWN
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        return raw.decode(encoding)
    except (OSError, UnicodeError, SyntaxError):
        return UNKNOWN


def read_source(path: Path) -> bytes:
    """A changed file's bytes for a check, at most one past the syntax bound.

    A failed read raises: a baseline that cannot be read is unknown, but the
    content being checked has no verdict without its bytes.
    """
    if not path.is_file():
        raise FileNotFoundError(f"no regular file at {path}")
    with path.open("rb") as handle:
        return handle.read(MAX_SYNTAX_BYTES + 1)


def _refused(raw: bytes, exc: SyntaxError | UnicodeDecodeError) -> Finding:
    """Source Python refuses to decode, placed at the first byte it cannot.

    Python's parser places a declared encoding's failure at line 0, and 3.12's
    passes undecodable bytes inside a comment, so the place is found here: the
    first byte that does not decode, or else the line of the first two that
    ``tokenize.detect_encoding`` rejects.
    """
    if isinstance(exc, UnicodeDecodeError):
        # The codec's own buffer: a BOM's codec decodes only what follows the mark.
        raw, at, encoding, message = exc.object, exc.start, exc.encoding, str(exc)
    else:
        first = raw.split(b"\n", 1)[0]
        try:
            tokenize.detect_encoding(io.BytesIO(first).readline)
        except SyntaxError:
            at = 0
        else:
            at = len(first) + 1
        encoding, message = "utf-8", exc.msg
    start = raw.rfind(b"\n", 0, at) + 1
    end = raw.find(b"\n", at)
    line = raw[start : end if end >= 0 else len(raw)].decode(encoding, "replace")
    return Finding("syntax", raw.count(b"\n", 0, at) + 1, at - start + 1, message, line)


async def read_before(path: Path) -> str | None | object:
    try:
        return await WORKERS.run(lambda: read_python(path), timeout=CHECK_WAIT_SECONDS)
    except Exception:  # noqa: BLE001 - an unavailable baseline must not block a write
        return UNKNOWN


def _ignores(text: str) -> dict[int, set[str] | None]:
    ignored: dict[int, set[str] | None] = {}
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT:
            continue
        # Ruff honors a file-level directive only on a line of its own and
        # ignores one that trails code, which suppresses nothing.
        own_line = not token.line[: token.start[1]].strip()
        for match in _NOQA.finditer(token.string):
            if match[1] and not own_line:
                continue
            row = 0 if match[1] else token.start[0]
            codes = set(re.findall(r"[A-Z]+\d+", match[2].upper())) if match[2] else None
            if row not in ignored:
                ignored[row] = codes
            elif ignored[row] is not None:
                ignored[row] = None if codes is None else ignored[row] | codes
    return ignored


class PythonDiagnostics:
    """Report actionable name errors; optional import notices never rewrite code."""

    def __init__(self, *, lint: bool = True, unused_imports: bool = False) -> None:
        self.lint = lint
        self.unused_imports = unused_imports

    def findings(self, path: Path, content: str | bytes, *, syntax_only: bool = False) -> list[Finding]:
        """Syntax findings, then the name findings unless ``syntax_only``.

        Bytes are decoded as Python reads a source file, by its coding
        declaration or else as UTF-8; bytes that do not decode are the syntax
        error Python reports for them.
        """
        if isinstance(content, str):
            text = content
        else:
            try:
                text = importlib.util.decode_source(content)
            except (SyntaxError, UnicodeDecodeError) as exc:
                return [_refused(content, exc)]
        lines = text.splitlines()
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            row = exc.lineno or 1
            source = lines[row - 1] if 0 < row <= len(lines) else ""
            return [Finding("syntax", row, exc.offset or 1, exc.msg, source)]
        except ValueError:
            return []
        if syntax_only or not self.lint:
            return []
        from pyflakes.checker import Checker

        codes = {"UndefinedName": "F821", "UndefinedExport": "F822", "UndefinedLocal": "F823"}
        if self.unused_imports:
            codes["UnusedImport"] = "F401"
        ignored = _ignores(text)
        findings = []
        for message in Checker(tree, filename=str(path)).messages:
            code = codes.get(type(message).__name__)
            if code is None:
                continue
            row = message.lineno
            if any(key in ignored and (ignored[key] is None or code in ignored[key]) for key in (0, row)):
                continue
            source = lines[row - 1] if 0 < row <= len(lines) else ""
            findings.append(Finding(code, row, message.col + 1, message.message % message.message_args, source))
        return sorted(findings, key=lambda item: (item.line, item.column, item.code))

    def note(self, path: Path, content: str | bytes, *, before: str | None | object = UNKNOWN) -> str:
        try:
            if path.suffix != ".py":
                return ""
            # Bytes as stored; text longer than the bound in characters is longer in bytes too.
            size = (
                len(content.encode("utf-8"))
                if isinstance(content, str) and len(content) <= MAX_SYNTAX_BYTES
                else len(content)
            )
            if size > MAX_SYNTAX_BYTES:
                return (
                    f"\nPython diagnostics skipped for {path}: the source is over "
                    f"{MAX_SYNTAX_BYTES // 1024 // 1024} MiB. Run targeted project checks."
                )
            syntax_only = size > MAX_SOURCE_BYTES
            findings = self.findings(path, content, syntax_only=syntax_only)
            if before is None:
                baseline = Counter()
            elif isinstance(before, str) and len(before.encode("utf-8")) <= MAX_SOURCE_BYTES:
                # Source nested too deeply for the parser or checker raises
                # rather than parsing to nothing: unavailable for the content
                # written, an unknown baseline for the content replaced.
                try:
                    earlier = self.findings(path, before)
                except Exception:  # noqa: BLE001 - an unanalyzable baseline is unknown, not clean
                    earlier = None
                baseline = (
                    None
                    if earlier is None or any(item.code == "syntax" for item in earlier)
                    else Counter(item.key for item in earlier)
                )
            else:
                baseline = None
        except Exception as exc:  # noqa: BLE001 - diagnostics must not change an already completed write's verdict
            return f"\nPython diagnostics unavailable ({type(exc).__name__}); the file was kept. Run targeted project checks."
        if baseline is not None:
            fresh = []
            for finding in findings:
                if baseline[finding.key]:
                    baseline[finding.key] -= 1
                else:
                    fresh.append(finding)
            findings = fresh
        if not findings:
            if syntax_only and self.lint:
                return (
                    f"\nPython name checks skipped for {path}: the source is over "
                    f"{MAX_SOURCE_BYTES // 1024} KiB, so only its syntax was checked."
                )
            return ""
        scope = (
            "new findings compared with the content before this call"
            if baseline is not None
            else "current findings; the baseline is unknown or could not be parsed, so these may predate this call"
        )
        rows = [f"\nPython diagnostics for {path} ({scope}; the file was kept):"]
        for finding in findings[:MAX_FINDINGS]:
            label = "Python syntax error" if finding.code == "syntax" else finding.code
            severity = "NOTICE" if finding.code == "F401" else "WARNING"
            rows.append(f"{severity}: line {finding.line}, column {finding.column}: {label}: {finding.message[:240]}")
            if finding.source:
                rows.append(f"  {finding.source.strip()[:160]}")
        if len(findings) > MAX_FINDINGS:
            rows.append(f"{len(findings) - MAX_FINDINGS} more findings omitted; inspect this file directly.")
        if any(item.code == "syntax" for item in findings):
            rows.append(
                f"Parsed with Python {sys.version_info.major}.{sys.version_info.minor}; "
                "check the project's Python version before changing newer syntax. "
                "Fix confirmed syntax/indentation errors before relying on execution results."
            )
        if any(item.code in {"F821", "F822", "F823"} for item in findings):
            rows.append(
                "Check the named symbol's spelling, scope, import, or assignment order. "
                "Confirm the intended binding; do not add dummy values just to silence this warning."
            )
        if any(item.code == "F401" for item in findings):
            rows.append(
                "An unused import can be a public re-export or have side effects; inspect its role before removing it."
            )
        return "\n".join(rows)

    async def check(self, path: Path, *, text: str | None = None, before: str | None | object = UNKNOWN) -> str:
        if path.suffix != ".py":
            return ""

        def inspect() -> str:
            return self.note(path, read_source(path) if text is None else text, before=before)

        try:
            return await WORKERS.run(inspect, timeout=CHECK_WAIT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - keep the completed operation's result on checker failure
            return f"\nPython diagnostics unavailable ({type(exc).__name__}); the file was kept. Run targeted project checks."


__all__ = [
    "MAX_SOURCE_BYTES",
    "MAX_SYNTAX_BYTES",
    "UNKNOWN",
    "PythonDiagnostics",
    "read_before",
    "read_python",
    "read_source",
]
