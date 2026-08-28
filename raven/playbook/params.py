"""Parameter values and parameter references, for one playbook.

Two halves of one contract live here: what a declared parameter resolves to at
run time, and where that value may be written into the file's own text. Both
used to sit in the executor with the reference pattern duplicated in the
validator, so a spelling one substituted and the other did not check was one
edit away.

Two spellings mean the same thing: ``${params.X}`` and ``{{ params.X }}``. The
second exists because ``{{ ... }}`` is the file's other placeholder family (a
node's output), so an author reaching for it inside an MCP server definition is
not making a different request. Node-output placeholders are untouched by this
module: they are the runner's, and only ``params.`` is claimed here.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.playbook.types import PlaybookSpec

_PARAM_REF_RE = re.compile(r"\$\{params\.([A-Za-z0-9_]+)\}|\{\{\s*params\.([A-Za-z0-9_]+)\s*\}\}")


def param_refs(text: str) -> list[str]:
    """Every parameter named by a reference in ``text``, in order of appearance."""
    return [dollar or braces for dollar, braces in _PARAM_REF_RE.findall(text or "")]


def has_param_ref(text: str) -> bool:
    """Whether ``text`` references any parameter at all."""
    return _PARAM_REF_RE.search(text or "") is not None


def fill_param_refs(text: str, values: Mapping[str, str]) -> str:
    """Substitute every parameter reference; an unknown name is left as written.

    Left rather than emptied: an undeclared reference is a validation error, so a
    name that arrives here unknown is one the caller did not supply, and text
    still reading ``${params.topic}`` says so where an empty string would read as
    a finished sentence.
    """
    return _PARAM_REF_RE.sub(lambda m: values.get(m.group(1) or m.group(2), m.group(0)), text or "")


SECRET_PLACEHOLDER = "[secret {name} withheld]"  # noqa: S105 - a template for a redaction, not a credential
"""What a secret reference becomes anywhere a value would leave the host.

Not the value, and not the reference either: leaving ``${params.TOKEN}`` would
read to a sub-agent as a substitution that failed and invite it to ask for the
literal, while an empty string reads as a finished sentence with a blank in it.
This says what happened."""


def fill_param_refs_without_secrets(
    text: str, values: Mapping[str, str], secrets: Collection[str]
) -> tuple[str, list[str]]:
    """Substitute every reference except a secret's, and name the ones withheld.

    A secret exists so a playbook can name a credential the run supplies without
    carrying it, and the only place its value may land is an ``mcpServers``
    ``env`` or ``headers`` entry, which the host fills and keeps. A prompt is
    neither: it travels to a sub-agent and is written into the run's transcript.

    Withheld here rather than refused at the file, because ``mcpServers`` is an
    optional section on top of a playbook that otherwise runs, and a reference
    written in the wrong place must cost that reference -- not the playbook. The
    author is told through the returned names; the value never leaves.
    """
    withheld: list[str] = []

    def one(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        if name in secrets:
            withheld.append(name)
            return SECRET_PLACEHOLDER.format(name=name)
        return values.get(name, match.group(0))

    return _PARAM_REF_RE.sub(one, text or ""), withheld


def render_value(value: Any) -> str:
    """One supplied parameter value as the text a reference substitutes to."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def resolve_params(spec: "PlaybookSpec", params: Mapping[str, Any]) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Resolve every declared param to a string value, or collect what is missing.

    Returns the missing params as ``(name, description)``: the description is the
    follow-up wording per the field definition, and the name is what the retry
    hint needs.
    """
    values: dict[str, str] = {}
    missing: list[tuple[str, str]] = []
    for name, p in spec.params.items():
        if name in params and params[name] is not None:
            values[name] = render_value(params[name])
        elif p.default is not None:
            values[name] = render_value(p.default)
        elif p.required:
            missing.append((name, p.description))
        else:
            values[name] = ""
    return values, missing


def secret_param_names(spec: "PlaybookSpec") -> frozenset[str]:
    """The declared params whose value must not travel with the playbook."""
    return frozenset(name for name, p in spec.params.items() if p.type == "secret")


__all__ = [
    "fill_param_refs",
    "has_param_ref",
    "param_refs",
    "render_value",
    "resolve_params",
    "SECRET_PLACEHOLDER",
    "fill_param_refs_without_secrets",
    "secret_param_names",
]
