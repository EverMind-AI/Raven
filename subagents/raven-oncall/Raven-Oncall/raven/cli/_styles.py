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
        # Leading "?" glyph + the question text.
        ("qmark", "fg:#fbe23f bold"),
        ("question", "bold"),
        # The committed answer echoed after a prompt resolves.
        ("answer", "fg:#fbe23f bold"),
        # The "❯" pointer and the row it sits on (hover state).
        ("pointer", "fg:#fbe23f bold"),
        # Active row: same text color as the other rows, only bold — the yellow
        # "❯" pointer is the sole selection cue, so every option reads in one
        # consistent color. noreverse: prompt_toolkit's base style reverse-
        # highlights the active row, which would otherwise paint a solid block.
        ("highlighted", "fg:#FFF5EA bold noreverse"),
        # A previously selected value (e.g. checkbox); noreverse for the same
        # reason — show selection via the gold color, not a background block.
        ("selected", "fg:#c8a900 noreverse"),
        # Faint rule between option groups.
        ("separator", "fg:#444444"),
        # The "(Use arrow keys)" style hint after the question.
        ("instruction", "fg:#6c6c6c italic"),
        # Non-selectable rows.
        ("disabled", "fg:#585858 italic"),
        # Inline validation error toolbar.
        ("validation-toolbar", "fg:#ff5f5f bold"),
        # Free-text input the user is typing.
        ("text", "fg:#FFF5EA"),
    ]
)

__all__ = ["RAVEN_STYLE"]
