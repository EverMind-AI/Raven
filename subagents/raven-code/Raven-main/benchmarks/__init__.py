"""Repo-root benchmark harnesses (see README.md in this directory).

This __init__ exists so evolvable benches (e.g. ``benchmarks.appworld``) are
importable with the repo checkout root on ``sys.path`` — the evolver inserts
the subject repo root before loading a bench plugin, and worktree evals rely
on cwd-first ``python -m`` resolution. The directory still ships in neither
the wheel nor the sdist.
"""
