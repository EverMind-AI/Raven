# Raven demos

Each subdirectory is one self-contained showcase: an inputs-+-runner-+-output
bundle that should be reproducible from a fresh checkout.

| Demo | What it shows |
|------|---------------|
| [skill_retrieval/](skill_retrieval/) | An agent reads a SKILL.md, follows it, and generates an image via Nano Banana on OpenRouter. |

## Layout convention

```
demos/<feature_name>/
  README.md          ← what the demo shows + how to run it + expected output
  run_*.sh           ← one-line driver (env vars, config injection, the actual run)
  skills/            ← any local skill files the demo's workspace needs
  example_output.*   ← committed sample output for "this is what success looks like"
```

## Running a demo

Each demo's README explains its own prerequisites. In general they need:

- ``uv`` and the project's venv (``uv sync`` from the repo root)
- An ``OPENROUTER_API_KEY`` (or whatever provider the demo calls)
- A working network — most demos hit OpenRouter directly

## Notes

- ``raven agent`` writes workspace artifacts (``AGENTS.md``, ``memory/``,
  ``sessions/``, ...) into the demo dir on each run. They are not committed
  but ``git status`` will show them after every run; clean up with
  ``git clean -fd demos/<name>/`` before committing.
- Demos are **not** part of the CI test suite — they hit live APIs and
  cost real money to run.
