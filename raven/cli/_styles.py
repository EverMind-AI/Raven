"""Shared :mod:`questionary` ``Style`` for all interactive CLI prompts.

Centralizing the palette here lets future commands (sessions picker,
provider login, etc.) stay visually consistent without re-declaring colors
inline. Import is deferred to module load — callers that only need other
helpers should still import lazily so a missing :mod:`questionary` install
doesn't break the rest of the CLI.
"""

from __future__ import annotations

from questionary import Style

RAVEN_STYLE = Style(
    [
        ("qmark", "fg:cyan bold"),
        ("question", "bold"),
        ("answer", "fg:cyan"),
        ("pointer", "fg:cyan bold"),
        ("highlighted", "fg:cyan bold"),
        ("selected", "fg:#5fafff"),
        ("separator", "fg:#3a3a3a"),
        ("instruction", "fg:#808080"),
        ("disabled", "fg:#5f5f5f italic"),
        ("validation-toolbar", "fg:#d75f5f bold"),
    ]
)

__all__ = ["RAVEN_STYLE"]
