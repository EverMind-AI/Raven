"""Pluggable context synthesizer registry.

Adding a new synthesizer:
  1. Create a new module in this package (e.g. `nlp.py` or `llm.py`).
  2. Implement the `ContextSynthesizer` Protocol from `.base`.
  3. Import the class here and register it in SYNTHESIZERS.

Select via `get_synthesizer(name, **kwargs)` or the adapter CLI's
`--synthesizer NAME` flag.
"""

from __future__ import annotations

from .base import ContextSynthesizer, SynthesizedContext
from .keyword import KeywordSynthesizer

SYNTHESIZERS: dict[str, type] = {
    KeywordSynthesizer.name: KeywordSynthesizer,
}


def get_synthesizer(name: str = "keyword", **kwargs) -> ContextSynthesizer:
    if name not in SYNTHESIZERS:
        available = ", ".join(sorted(SYNTHESIZERS))
        raise KeyError(f"Unknown synthesizer '{name}'. Registered: {available}")
    return SYNTHESIZERS[name](**kwargs)


__all__ = [
    "ContextSynthesizer",
    "KeywordSynthesizer",
    "SYNTHESIZERS",
    "SynthesizedContext",
    "get_synthesizer",
]
