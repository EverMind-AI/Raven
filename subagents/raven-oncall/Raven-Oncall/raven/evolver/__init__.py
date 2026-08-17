"""Harness self-evolution subsystem.

A budget-bounded loop that improves an agent harness against a benchmark:
diagnose failing trajectories, design candidate patches as real git commits,
screen them cheaply, confirm survivors at K=3, and promote only what beats
the baseline through three verification gates — with a sealed test set for
an honest generalisation number. Methodology:
``docs/specs/self-evolution-loop-sop.md``; user-facing entry point and
quickstart: ``raven/evolver/README.md`` (``python -m raven.evolver``).

Package layout:

- ``launch``       — the unified entry: run spec, bench plugin contract,
                     run/check/status/finalize state machine
- ``orchestrator`` — the SOP round loop: diagnose, design, screen, confirm,
                     gates, sealed test (see ``orchestrator/DESIGN.md``)
- ``tree``         — git-backed harness version tree (commit-per-candidate)
- ``activation``   — beacon ledger: which candidate code actually fired
- ``applier``      — path whitelist + beacon guards on candidate edits
- ``judge``        — LLM judge with L1/L2/L3 + (WHERE, WHY) output
- ``scheduler``    — task/anchor selection and bandit utilities
- ``analysis``     — trial-ledger readers and stability bucketing
- ``compressor``   — trajectory compression for diagnosis prompts

Provenance notes for readers of module docstrings: citations of the form
``spec §NN`` refer to the upstream project's internal design document (not
shipped; the shipped methodology spec is
``docs/specs/self-evolution-loop-sop.md``) — the numbers are kept as
provenance for the constants and enums they justify. ``TB2`` names the
upstream terminal-agent benchmark line this system was first developed
against; the example benchmark shipped here is AppWorld. ``GSME`` (Gated
Semantic MAP-Elites) is the quality-diversity elite archive
(``orchestrator/archive.py``).
"""
