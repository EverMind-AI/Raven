"""Shared :mod:`questionary` ``Style`` for all interactive CLI prompts.

The palette and light/dark detection live in :mod:`raven.cli._theme`; this
module just resolves the active scheme once and exposes the built style. Import
stays lazy at call sites (a missing :mod:`questionary` install shouldn't break
the rest of the CLI), and because every caller imports this lazily at runtime,
detecting the scheme here does not run at process startup.

The matching prompt glyphs live in :mod:`raven.cli._theme` instead: they carry
no questionary dependency, so a caller can lay out a prompt's chrome without
paying for this module's import.
"""

from __future__ import annotations

from raven.cli._theme import build_questionary_style, detect_scheme

RAVEN_STYLE = build_questionary_style(detect_scheme())

__all__ = ["RAVEN_STYLE"]
