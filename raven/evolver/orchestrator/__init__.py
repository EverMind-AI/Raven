"""Self-evolution orchestrator — a deterministic harness over the seven-step funnel.

The canonical evolver package (``raven.evolver.{scheduler,analysis,judge,tree}``)
ships every deterministic operator the self-evolution SOP needs — anchor
selection, stability bucketing, the tree-aware bandit, the failure-map
builder, the node ledger, git ops, and the LLM judge. What it never had is a
faithful *driver* of the SOP's seven-step funnel.

This package supplies that missing layer. It is a small finite-state machine
that owns the control flow the SOP used to delegate to a long, high-compliance
Claude session:

- the round loop and per-candidate fork,
- the "never stop early" discipline (a code counter, not a prompt),
- the termination conditions (10 rounds with no vanilla-beating candidate,
  or a hard cap of 20 rounds),
- and state persistence so no model has to hold cross-round context.

The driver model (diagnose / design / verdict) only ever makes small,
schema-validated calls through the existing judge backends
(``raven.evolver.judge.llm_client``), which already route self-hosted Qwen,
Claude, and OpenRouter models. That is what lets a weaker model drive the loop:
the harness carries the control-flow burden the model cannot.
"""

from __future__ import annotations
