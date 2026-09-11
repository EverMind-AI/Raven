"""``glob``: the fork's spelling of trunk's pathname-pattern tool.

Same engine, one name and one description: the fork measured that models
trained on the common convention reach for ``glob`` and read its output as a
listing, not as a command to run. The product config withholds trunk's
``find`` so the face carries one spelling.

Brace alternation (``*.{ts,tsx}``) is part of that convention and trunk's
engine takes one pattern, so it is expanded here and the listings are merged.
The engine returns paths together with status text. The merge preserves
errors, carries any child truncation into the final notice, deduplicates the
paths and sorts them by recency before applying the caller's total limit.
"""

from __future__ import annotations

import re
from typing import Any

from raven.agent.tools import file_search as trunk

#: How many patterns one brace expression may expand to. Past it the call is
#: refused rather than silently searching a subset -- a listing that quietly
#: omits half the alternatives reads exactly like a directory that lacks them.
BRACE_CAP = 20

#: The engine's own lines, which are not paths. Matched, not guessed: each is
#: a literal the engine writes (``file_search.FindTool``).
_NOTICE_RE = re.compile(
    r"^\(showing first \d+(?: of \d+ results| results across \d+ patterns)(?:; PARTIAL.*)?\)$"
    r"|^No files found matching pattern(?:\.|: .*)$"
    r"|^Error: |^Error running find: ",
)
_CHILD_TRUNCATION_RE = re.compile(r"^\(showing first \d+ of \d+ results\)$")


def is_notice(line: str) -> bool:
    """Whether this output line is the engine talking, not a path."""
    return bool(_NOTICE_RE.match(line.strip()))


def expand_braces(pattern: str) -> list[str]:
    """Every ``{a,b}`` group expanded, innermost-first, in listed order.

    Returns the pattern unchanged when it carries no group, and raises
    ``ValueError`` when the expansion would exceed :data:`BRACE_CAP` or the
    braces do not pair -- the caller turns either into a refusal the model can
    act on.
    """
    if "{" not in pattern:
        return [pattern]
    start = pattern.find("{")
    depth = 0
    end = -1
    for index in range(start, len(pattern)):
        if pattern[index] == "{":
            depth += 1
        elif pattern[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end < 0:
        raise ValueError(f"unbalanced braces in pattern {pattern!r}")
    alternatives = _split_top_level(pattern[start + 1 : end])
    out: list[str] = []
    for alt in alternatives:
        for rest in expand_braces(pattern[:start] + alt + pattern[end + 1 :]):
            if rest not in out:
                out.append(rest)
            if len(out) > BRACE_CAP:
                raise ValueError(
                    f"pattern {pattern!r} expands to more than {BRACE_CAP} patterns; "
                    "search the alternatives in separate calls"
                )
    return out


def _split_top_level(body: str) -> list[str]:
    """Split on commas that are not inside a nested group."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


class GlobTool(trunk.FindTool):
    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Find files by glob pattern (e.g. '*.py', 'src/**/*.ts' or '*.{ts,tsx}'). Prefer this over "
            "running find/ls through exec. Returns paths relative to the search root, "
            "most-recently-modified first. Noise directories (.git, node_modules, etc.) "
            "are skipped. Use it to locate a file before reading or editing it instead of "
            "guessing at the path. Brace alternatives share one limit after deduplication and sorting. "
            "A truncation notice means the result is partial; raise limit or narrow the pattern."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        base = super().parameters
        props = dict(base["properties"])
        if "pattern" in props:
            props["pattern"] = {
                **props["pattern"],
                "description": "Glob pattern, e.g. '*.py', 'src/**/*.ts' or '*.{ts,tsx}'",
            }
        if "path" in props:
            props["path"] = {**props["path"], "description": "Directory to search in (default: workspace root)"}
        props["limit"] = {
            **props["limit"],
            "description": "Maximum unique entries across all patterns (default 1000). "
            "Raise this limit or narrow the pattern when the result is marked partial.",
        }
        return {**base, "properties": props}

    async def execute(self, pattern: str = "", limit: int | None = None, **kwargs: Any):  # type: ignore[override]
        if not pattern:
            return "Error: missing required parameter 'pattern'."
        try:
            alternatives = expand_braces(pattern)
        except ValueError as exc:
            return f"Error: {exc}"
        if len(alternatives) == 1:
            return await super().execute(pattern=alternatives[0], limit=limit, **kwargs)
        try:
            base = self._resolve(kwargs.get("path", "."))
        except PermissionError as exc:
            return f"Error: {exc}"
        ceiling = limit if limit is not None else self._DEFAULT_LIMIT
        paths: list[str] = []
        seen: set[str] = set()
        cut = False
        for one in alternatives:
            result = await super().execute(pattern=one, limit=limit, **kwargs)
            text = result.model_text if hasattr(result, "model_text") else str(result)
            if text.startswith(("Error: ", "Error running find: ")):
                return result
            if text == "No files found matching pattern.":
                continue
            lines = text.splitlines()
            # FindTool puts truncation after a blank line. A filename that
            # merely starts with a notice's words is still a path.
            if len(lines) >= 2 and not lines[-2] and _CHILD_TRUNCATION_RE.fullmatch(lines[-1]):
                cut = True
                lines = lines[:-2]
            for line in lines:
                if line and line not in seen:
                    seen.add(line)
                    paths.append(line)
        if not paths:
            return f"No files found matching pattern: {pattern}"
        paths.sort(key=lambda path: self._mtime(base / path), reverse=True)
        cut = cut or len(paths) > ceiling
        shown = paths[:ceiling]
        listing = "\n".join(shown)
        if cut:
            listing += (
                f"\n(showing first {len(shown)} results across {len(alternatives)} patterns; "
                "PARTIAL result, raise limit or narrow the pattern)"
            )
        return listing


__all__ = ["BRACE_CAP", "GlobTool", "expand_braces", "is_notice"]
