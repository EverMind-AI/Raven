# -*- coding: utf-8 -*-
"""Grammar for the template placeholders used in DAG node prompts."""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from raven.agent.subagent.prompt_errors import DagValidationError

_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Placeholder:
    """A single parsed ``{{ ... }}`` reference in a prompt template.

    Attributes:
        kind (`str`):
            One of ``"input"``, ``"input_path"``, ``"output"``,
            ``"output_path"``, ``"ref"``, ``"ref_path"``.
        name (`str`):
            The input key, node id, or file path referenced.
        raw (`str`):
            The exact ``{{ ... }}`` substring, used for substitution.
    """

    kind: str
    name: str
    raw: str

    def content_form(self) -> str:
        """The same reference rewritten to inject contents instead of a path."""
        if self.kind == "output_path":
            return f"{{{{ {self.name}.output }}}}"
        if self.kind == "input_path":
            return f"{{{{ inputs.{self.name} }}}}"
        return f"{{{{ ref:{self.name} }}}}"


def parse_placeholders(template: str) -> list[Placeholder]:
    """Parse all ``{{ ... }}`` references from a prompt template.

    Args:
        template (`str`):
            The prompt template to scan.

    Returns:
        `list[Placeholder]`:
            The placeholders in order of appearance. A ``{{ ... }}`` whose
            body matches no known shape is not included -- it is ordinary
            text, not a placeholder.

    Raises:
        `DagValidationError`:
            When a ``ref:`` or ``ref_path:`` body names an empty path.
    """
    return [ph for _, _, ph in iter_placeholders(template)]


def iter_placeholders(
    template: str,
) -> Iterator[tuple[int, int, Placeholder]]:
    """Yield ``(start, end, placeholder)`` per match, in order.

    The spans let a renderer rebuild the string in a single left-to-right
    pass so a resolved value that itself contains ``{{ ... }}`` text is
    never re-scanned or re-substituted. A ``{{ ... }}`` whose body matches no
    known placeholder shape is skipped entirely, so its raw text is carried
    through untouched by the spans on either side of it.

    Args:
        template (`str`):
            The prompt template to scan.

    Yields:
        `tuple[int, int, Placeholder]`:
            The match start, end, and parsed placeholder.

    Raises:
        `DagValidationError`:
            When a ``ref:`` or ``ref_path:`` body names an empty path.
    """
    for match in _PLACEHOLDER_RE.finditer(template):
        placeholder = _parse_body(match.group(1).strip(), match.group(0))
        if placeholder is None:
            continue
        yield match.start(), match.end(), placeholder


def _parse_body(body: str, raw: str) -> Placeholder | None:
    """Parse the inside of one ``{{ ... }}``, or ``None`` when it is not one.

    A body that matches no known shape is not a mistyped placeholder, it is
    ordinary text: template syntax from another system (Jinja, Vue, Handlebars)
    reaches a sub-agent through these prompts, and no escape form exists to get
    it past a hard rejection. A body that DOES match a known shape still parses
    and still fails downstream on a bad key, path, or node id, so a typo inside
    a placeholder is caught as before.

    Args:
        body (`str`):
            The trimmed text between the braces.
        raw (`str`):
            The full ``{{ ... }}`` substring.

    Returns:
        `Placeholder | None`:
            The parsed placeholder, or ``None`` when ``body`` matches no known
            shape.

    Raises:
        `DagValidationError`:
            When body starts with ``ref:`` or ``ref_path:`` but names an empty
            path.
    """
    for prefix, kind in (("ref:", "ref"), ("ref_path:", "ref_path")):
        if body.startswith(prefix):
            path = body[len(prefix) :].strip()
            if not path:
                raise DagValidationError(f"empty path in '{raw}'")
            return Placeholder(kind=kind, name=path, raw=raw)

    parts = body.split(".")
    if len(parts) == 2 and parts[0] == "inputs" and _NAME_RE.match(parts[1]):
        return Placeholder(kind="input", name=parts[1], raw=raw)
    if len(parts) == 3 and parts[0] == "inputs" and _NAME_RE.match(parts[1]) and parts[2] == "path":
        return Placeholder(kind="input_path", name=parts[1], raw=raw)
    if len(parts) == 2 and _NAME_RE.match(parts[0]) and parts[1] == "output":
        return Placeholder(kind="output", name=parts[0], raw=raw)
    if len(parts) == 2 and _NAME_RE.match(parts[0]) and parts[1] == "output_path":
        return Placeholder(kind="output_path", name=parts[0], raw=raw)
    return None
