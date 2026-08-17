# AGENTS.md

Raven-X AI-collaboration spec. **Read this file before making any code change in this repo.**

`CLAUDE.md` is a symlink to this file. **Edit `AGENTS.md`.** Writing `CLAUDE.md` as a regular
file replaces the symlink and silently forks the two documents.

Scope: Codex / Claude Code / Claude API / any AI-assisted work. When a rule here conflicts
with an ad-hoc instruction in conversation, **this file wins** — unless the user *explicitly*
says "ignore rule X in AGENTS.md".

Hard constraints only (violations get reverted / rejected). Soft suggestions and style
preferences belong in conversation, not here.

| # | Section | Gist |
|---|---|---|
| 0 | [Measurement safety](#0-measurement-safety) | Never edit `raven/**` while a batch runs; distribution changes need a version bump |
| 1 | [Code comments](#1-code-comments) | Don't comment unless necessary; comments in English |
| 2 | [Branches](#2-branches) | `<type>/<snake_desc>`; confirm base before cutting |
| 3 | [Commits](#3-commits) | Conventional Commits, all-ASCII, never commit unprompted |
| 4 | [Dependencies](#4-dependencies-uv-only) | `uv` only — never `pip` / hand-edit lockfile |
| 5 | [Tests](#5-tests) | `uv run pytest`; strict file naming |
| 6 | [Domain terms](#6-domain-terms) | Consult `CONTEXT.md` before naming; use canonical terms |
| 7 | [Repository assets](#7-repository-assets) | No report assets, web artifacts, or large files |

---

## 0. Measurement safety

This repo is the instrument of a running measurement programme. These four rules cost more to
violate than everything else in this file combined.

### §0.1 Never edit `raven/**` while an evaluation batch is running

Every question in a batch launches a fresh `raven agent -m` subprocess that re-imports
`raven/**` from disk. An edit mid-batch applies to the not-yet-started questions only — one
arm ends up running two different builds, while the provenance record (`arm_env.json`, written
once at launch) says otherwise. Check first:

```bash
pgrep -f "raven agent -m" | wc -l    # must be 0
```

Same rule for `pyproject.toml`: touching it invites a `uv sync`, which replaces the venv those
subprocesses are importing from.

### §0.2 A distribution change requires a `drFlow.version` bump

Anything that changes what the model reads, or which turns are re-sampled, changes the
generated distribution: prompt segments, the budget line, verify criteria, tool-layer
rewrites, acceptance checks on a committed answer. Bump `drFlow.version` and add the
superseded label to `DRFlowConfig._SUPERSEDED_VERSIONS`, so an old label on a new build is
rejected instead of silently mislabelling a batch.

### §0.3 A deletion that changes `reserved_system` is a Flow change, not a prune

`AgentLoop._make_token_budget` estimates the system-prompt token count from
`context.build_system_prompt(...)` and subtracts it from `available_history`. Removing a prompt
segment therefore moves the context trim point **on both arms**, including the flow-off
anchor. Such removals ship as their own labelled version with a fresh anchor pair, never
folded into an unrelated pruning commit.

### §0.4 Do not move the anchor

The flow-off arm (`drFlow.enabled=false`) is the same-batch anchor for every A-minus-B. Fixes
that would flatter us are gated on `DRFlowConfig` so they no-op when the flow is off. If a
change must affect the anchor, it is a separate round with its own re-measured reference frame.

---

## 1. Code comments

### §1.1 Top rule: don't add comments unless necessary

- Match the style of surrounding lines. If neighboring code has no comments, **don't** add one
  to your new line.
- Comment **only** when: the logic is non-obvious; there is a hidden constraint (call-order
  sensitivity, a caller must do X first); or you need to explain **why**, not **what** (the
  name already says what).
- **Don't** add comments that describe what the code does (`# Increment counter` next to
  `counter += 1`), mark edits (`# <- new`), reference a PR / Issue / locally-visible-only path,
  or describe transient task context (`# For the X bug` — stale once the task is done).

### §1.2 When a comment is required, write it in English

Repo source comments **must not be in another language** — keep comment language consistent
across the repo. This constrains `raven/**` and `tests/**`. The evaluation harness in
`raven_train/pipeline/` is a separate tree with its own Chinese-comment convention; do not
"unify" them.

---

## 2. Branches

### §2.1 Format

`<type>/<short_desc>` — `short_desc` in **snake_case**, English, 3-5 words describing the
change.

| type | Use |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `refactor` | Refactor (not a feature, not a bug fix) |
| `perf` | Performance |
| `chore` | Misc (deps bump, doc structure) |
| `docs` | Docs only |
| `test` | Tests only |

| ✅ Good | ❌ Bad |
|---|---|
| `refactor/prune_non_dr_subsystems` | `tmp` |
| `fix/salvage_closing_tag_bar` | `bugfix` |

### §2.2 Confirm the base before cutting

Ask which base to cut from — don't pick one silently. If unspecified, default to `main`.
Confirm the base, cut the branch, **then** start editing; never write on a branch and carve it
out afterwards.

---

## 3. Commits

This is a **private, local-only repository** with a single squashed history, no CI, no pull
requests, and no shared branch. The upstream remote is fetch-only with its push URL disabled.
The rules below are what survives that.

### §3.1 Message format

```
<type>(<scope>): <subject>

<body — optional>
```

**type** — the §2.1 set, plus `build`, `ci`, `revert`.

**scope** — a top-level subpackage of `raven/`; see the Repo layout table in `README.md`, which
is normative. Spanning multiple scopes → omit the scope, or use `(*)`.

**subject** — lowercase start; ≤ 72 chars; no trailing period; English.

### §3.1.1 The whole message is ASCII

| Part | Rule |
|---|---|
| subject | English, lowercase start, ≤ 72 chars, no period |
| body | **All English**; when citing a non-English discussion, **translate** it, don't paste |
| punctuation | **ASCII-only** — no full-width punctuation, no em-dash, no curly quotes, no ellipsis |

Self-check with `git log -1`; any non-ASCII character → rewrite before committing. Do not
rewrite an already-made message with `git rebase -i` without explicit authorization.

### §3.2 When to commit

- **Don't commit unprompted** — only when the user explicitly says "commit" / "提交" / "save".
- "Commit per phase" written in a plan is **not** pre-authorization.
- After finishing a phase, **report and stop**; wait for acceptance plus an explicit commit
  instruction.
- **Don't** `git commit --amend`. If a pre-commit hook fails, create a **new** commit.
- **Don't push unprompted.** When you do: `--force-with-lease` on your own branch after a
  rebase; **never** `git push --force`; never force-push `main`.
- Append `Co-authored-by: Claude (<actual-model-id>) <noreply@anthropic.com>` when Claude wrote
  code — the real model id, not a placeholder. No emoji banners, no `Refs:` to local-only
  paths.

### §3.3 Do not use git as a verification tool

The entire build exists in the working tree of a shared volume. `git stash` / `checkout` /
`reset` risk it to settle a diagnostic question. Verify with file-level `grep` and by running
the target test file directly. Notably: a test that fails in a full-suite run and passes in
isolation is suite-order pollution, not a regression — check that before suspecting a change.

---

## 4. Dependencies (uv only)

### §4.1 `uv` is the only Python package manager

| Action | Command |
|---|---|
| Add runtime dep | `uv add <package>` |
| Add dev dep | `uv add --dev <package>` |
| Remove dep | `uv remove <package>` |
| Sync env from lockfile | `uv sync` |
| Upgrade one package | `uv lock --upgrade-package <package>` |
| Upgrade all | `uv lock --upgrade` |
| Run a command in the project env | `uv run <command>` |

### §4.2 Forbidden

- ❌ `pip install` / `pip uninstall`;
- ❌ hand-editing `[project.dependencies]` / `[project.optional-dependencies]` /
  `[dependency-groups]` in `pyproject.toml`;
- ❌ hand-editing `uv.lock`;
- ❌ `pip freeze > requirements.txt`;
- ❌ `python -m pip install ...` to bypass uv.

### §4.3 Exception

If the user *explicitly* says "let me try pip" / "manually add this line to pyproject", follow
the user. This rule constrains the assistant's **default** behavior, not the user's direct
instructions.

### §4.4 After any directory move or package rename

`uv sync --reinstall`, not bare `uv sync`. Bare sync only rewrites entry scripts for the
packages it actually reinstalls; the rest keep a dead absolute-path shebang and fail with
`cannot execute: required file not found` — or worse, `uv run pytest` silently falls back to a
different interpreter on `PATH` and reports a missing-module error that looks like a dependency
problem. Verify with `uv run python -m pytest` and by checking `.venv/bin/raven`'s shebang.

---

## 5. Tests

### §5.1 Unit tests

Under `tests/test_*.py`. CLI unit tests use one shape:
`tests/test_cli_<module>_commands.py` — one file per module, aligned with
`raven/cli/<module>_commands.py`. **Don't** split by phase / feature / ticket (no `phase4` /
`eve151` suffixes). Aspect suffixes are allowed: `test_cli_<helper>.py` for a private helper,
`test_cli_<aspect>.py` for cross-module behavior.

### §5.2 Integration tests

Under `tests/integration/test_*.py`, named `test_<scope>_<kind>.py` where `<kind>` is:

| kind | Meaning |
|---|---|
| `e2e` | End-to-end happy path |
| `smoke` | Multi-module interplay, just "it runs" |
| `real_<resource>` | Hits a real resource (`real_llm` / `real_vm` / ...) |

`<scope>` must not carry a version or ticket number.

### §5.3 Hard rules

- When changing or adding a CLI command, update the matching `test_cli_<module>_commands.py` —
  **don't create a new file**.
- When you spot a legacy file violating §5.1 / §5.2, **report it to the user first** — don't
  rename it unprompted.
- Always run tests via `uv run pytest ...`, never bare `pytest` (per §4).
- `tests/test_agent_flow_dr.py` is the flow/anchor contract. It must stay green through every
  refactor and pruning step, untouched.

---

## 6. Domain terms

- Naming a domain concept tracked in `CONTEXT.md`? Use the canonical term, not a synonym.
- Coining a new domain term: define it in `CONTEXT.md` in the same change, with a definition
  verifiable against the code (not guessed) — add an `_Avoid_` list only if a confusable
  synonym exists.

---

## 7. Repository assets

- Do not commit report assets or standalone web artifacts, regardless of size. This includes
  images, GIFs, SVGs, videos, audio files, PDFs, HTML files, web manifests, and WASM bundles.
- Do not add or modify files over 1 MiB unless the maintainer explicitly approves it before the
  commit.
- Run `make check-large-files` when touching docs, demos, assets, or generated outputs.

---

## Maintenance

This file holds **hard constraints only** (rules whose violation gets reverted / rejected).
Soft suggestions, design preferences, and style leanings go in conversation — not here.

Before adding a section, confirm with the user first — the shorter AGENTS.md stays, the more
useful it is.
