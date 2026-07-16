"""In-package AppWorld self-evolution: the domain brain (W1-W7 diagnosis,
trajectory rendering, bash-editor design) + wiring into the generic evolver
orchestrator. Replaces the external ``appworld-run/evo_scripts/appworld_bash.py``
loop reimplementation — here the orchestrator/gate/baseline come from
``raven.evolver.orchestrator`` and only the AppWorld-specific steps live here.
"""
