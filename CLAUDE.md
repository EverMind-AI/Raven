# CLAUDE.md

Raven AI-collaboration spec. **Read this file before making any code change in this repo.**

Scope: Claude Code / Claude API / any AI-assisted work. When a rule here conflicts with an ad-hoc instruction in conversation, **this file wins** — unless the user *explicitly* says "ignore rule X in CLAUDE.md".

Hard constraints only (violations get reverted / rejected). Soft suggestions and style preferences belong in personal notes or conversation, not here. See the [Maintenance](#maintenance) note before adding sections — shorter is better.

| # | Section | Gist |
|---|---|---|
| 1 | [Code comments](#1-code-comments) | Don't comment unless necessary; comments in English |
| 2 | [Branch naming](#2-branch-naming) | `<type>/<snake_desc>`; confirm base before cutting |
| 3 | [Commits](#3-commits-conventional-commits) | Conventional Commits, all-English, `Co-authored-by` trailer |
| 4 | [Dependencies](#4-dependencies-uv-only) | `uv` only — never `pip` / hand-edit lockfile |
| 5 | [Tests](#5-tests) | `uv run pytest`; strict file-naming |
| 6 | [Domain terms](#6-domain-terms) | Consult `CONTEXT-MAP.md` before naming; use canonical terms |

---

## 1. Code comments

### §1.1 Top rule: don't add comments unless necessary

- Match the style of surrounding lines. If neighboring code has no comments, **don't** add one to your new line.
- Comment **only** when:
  - the logic is non-obvious;
  - there's a hidden constraint (e.g. call-order sensitivity, a caller must do X first);
  - you need to explain **why**, not **what** (the name already says what).
- **Don't** add comments that:
  - describe what the code does (`# Increment counter` next to `counter += 1`);
  - mark edits (`# ← new` / `# changed this line`);
  - reference a PR / Issue / locally-visible-only doc path (`# Refs: ...` — invisible to others);
  - describe transient task context (`# For the X bug` — stale once the task is done).

### §1.2 When a comment is required, write it in English

- Repo source comments **must not be in another language** — keep comment language consistent across the repo.

### §1.3 Examples

❌ Non-English review comment copied straight into source:

```python
self.logger = logger.bind(channel=self.name)   # ← new
```

❌ Neighbors have no comments, yet the new line adds a meaningless one:

```python
def __init__(self, config: Any, bus: MessageBus):
    self.config = config
    self.bus = bus
    self._running = False
    self.logger = logger.bind(channel=self.name)   # ← drop this comment
```

✅ Clean, no comment, consistent:

```python
def __init__(self, config: Any, bus: MessageBus):
    self.config = config
    self.bus = bus
    self._running = False
    self.logger = logger.bind(channel=self.name)
```

✅ Rare case that genuinely needs a *why*, in English:

```python
# Bind channel name into logger context so every log entry auto-tags channel.
self.logger = logger.bind(channel=self.name)
```

---

## 2. Branch naming

### §2.1 Format

`<type>/<short-desc>`

| type | Use |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `refactor` | Refactor (not a feature, not a bug fix) |
| `perf` | Performance |
| `chore` | Misc (deps bump, doc structure, etc.) |
| `docs` | Docs only |
| `test` | Tests only |

`short-desc`: **snake_case**, English, 3–5 words describing the change.

| ✅ Good | ❌ Bad |
|---|---|
| `feat/whatsapp_lid_mapping` | `feat/优化` |
| `fix/cron_dst_transition` | `bugfix` |
| `refactor/cli_cron_sentinel` | `huangjie-test` |
| `chore/upgrade_uv` | `tmp` |

### §2.2 Confirm the base before cutting

- Before cutting any branch (`fix` / `feat` / `refactor` / anything), **ask the user which base to cut from** — don't pick one silently.
- If unspecified, default to **`main`** (the integration branch).
- Flow: `git fetch origin main`, then cut from the latest tip.
- Combined with the branch-first rule: **confirm base + cut the branch, then start editing** — never write on a working branch and carve the branch out afterwards.

---

## 3. Commits (Conventional Commits)

### §3.1 Message format

```
<type>(<scope>): <subject>

<body — optional>

<footer — optional>
```

**type** — same set as §2.1, plus 3 commit-only types:

| type | Meaning |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Docs only |
| `refactor` | Refactor |
| `perf` | Performance |
| `test` | Tests only |
| `build` | Build system / external deps |
| `ci` | CI config |
| `chore` | Other misc |
| `revert` | Revert a prior commit |

**scope** — a top-level subpackage of `raven/`. See the `Repo layout` section of `README.md` for the canonical list. Spanning multiple scopes → omit the scope, or use `(*)`.

**subject** — lowercase start; ≤ 72 chars; no trailing period; English.

**footer** (optional):
- `BREAKING CHANGE: <desc>` — triggers a MAJOR bump once public;
- `Closes #123` — auto-closes the issue on merge.

### §3.1.1 Top rule: the whole message is English (subject + body + footer)

No other languages anywhere in the message — not just the subject; body and footer too.

| Part | Rule |
|---|---|
| subject | English, lowercase start, ≤ 72 chars, no period |
| body | **All English**; when citing a non-English plan / discussion, **translate** it, don't paste |
| punctuation | No full-width punctuation (`：`,`，`,`。`,`「」`,`""` …), no `§`-numbering, no non-English path names; the latin part of a §N.M anchor is fine |
| trailer | `Co-authored-by: ...` is ASCII by format |

**Why:** Conventional-Commits tooling (commitlint / semantic-release / changelog generators) parses ASCII grammar and mis-lints on non-English text; cross-language reviewers and a public commit history both need English.

**Process:**
1. **Before writing:** translate the points in your head to English first — don't write a non-English body then translate (that leaves full-width residue).
2. **After writing:** self-check with `git log -1`; any non-English char → rewrite.
3. **Already committed but violating:** rewrite the message with `git rebase -i` **only after explicit user authorization**; don't rewrite history unprompted (see §3.4).

### §3.2 ✅ Good / ❌ Bad

✅ Good:

```
feat(cli): rename cron show/remove to get/delete
fix(channels): default allow_from to ['*'] instead of deny-all
refactor(cli): replace --cron-expr with --cron and --every-seconds with --every
```

❌ Bad:
- `更新代码` (non-English + no type/scope);
- `update` (no type/scope);
- `feat: Cron 命令重命名为 get 和 delete.` (uppercase + period + non-English + no scope).

### §3.3 Trailer rules (commit message + PR description)

**✅ Required:**

- `Co-authored-by: Claude (<model-id>) <noreply@anthropic.com>` — when Claude helped write the code, append it at the end of the commit body (blank line above), or at the end of the PR description.
  - `<model-id>` = the **actual current-session model ID** (e.g. `claude-opus-4-8` / `claude-sonnet-4-6` / `claude-haiku-4-5`), not a placeholder. The model version keeps per-model contribution distinguishable.
  - Format follows the aider convention; GitHub renders `Co-authored-by` as a co-author on the commit / PR.
- Multiple co-authors → one per line, standard git trailer format (`Name <email>`).
- The repo uses **rebase merge** (not squash): every commit's body/trailer enters `main` history as-is, so each commit must stand on its own — don't rely on the PR description.

**❌ Don't add:**
- `Refs: ...` pointing at locally-visible-only / git-ignored paths (invisible to others);
- `🤖 Generated with Claude Code` and similar emoji banners — `Co-authored-by` already conveys co-authorship (and is the structured, machine-readable attribution); a marketing badge adds no attribution value;
- internal commit-hash references / temporary branch names — docs/PRs describe the present state only.

### §3.4 Hard rule: when to commit

- **Don't commit unprompted** — only when the user explicitly says "commit" / "提交" / "save".
- "Commit per phase" written in a plan is **not** pre-authorization — a plan is a reference; committing still needs the user's word.
- After finishing a phase, **report and stop**; wait for acceptance + an explicit commit instruction.
- **Don't** `git commit --amend` a prior commit (unless the user explicitly asks to amend).
- If a pre-commit hook fails, create a **new** commit to fix it — don't amend.

### §3.5 Sync before push + force-push boundary

**Rule:** before pushing a feature branch, base it on the **latest `main`**.

| Step | Command | Note |
|---|---|---|
| 1. Sync remote | `git fetch origin <target>` | Doesn't touch the working tree |
| 2. Dry-run conflicts | `git merge-tree --write-tree HEAD origin/<target>` | exit 0 = clean; non-zero prints conflicts |
| 3. Rebase (if remote ahead) | `git rebase origin/<target>` | Re-applies your branch onto the target tip |
| 4. Re-run tests | `uv run pytest <relevant tests> -x` | Confirm the rebase didn't break anything |
| 5. Push | `git push -u origin <branch>` (first) or `git push --force-with-lease` (after rebase) | — |

**Why:** CI runs "your commits on top of the latest remote" (catches runtime conflicts before merge); the PR diff stays clean; and with rebase-merge each commit lands on `main` verbatim, so a local rebase keeps history linear.

**Force-push boundary:**
- ✅ `--force-with-lease` (checks the remote wasn't changed by others) on **your own feature branch** after a rebase;
- ❌ `git push --force` (blind, can clobber others' pushes);
- ❌ never force-push to long-lived / protected branches (`main`).

### §3.6 Hard rule: Claude push

- Always `git fetch` first to check ahead/behind;
- if the remote target has commits not on your branch, **rebase before pushing** (the §3.5 flow);
- **re-run tests after the rebase**;
- **don't push unprompted** — like §3.4, only when the user says "push";
- use `--force-with-lease`, never `--force`.

### §3.7 After a successful push, offer to open the PR

After pushing a new feature branch, **proactively ask** whether to open the PR with `gh pr create` — don't leave the user to do it in the web UI.

**Title:** same Conventional-Commits grammar as commits (`<type>(<scope>): <subject>`), subject reflecting the PR's overall goal, not any single commit. **Title length may relax to ≤ 90 chars** (the 72 limit is for `git log --oneline` wrapping; web-UI titles don't wrap) — but shorter is better.

**Description must be all English** (same as §3.1.1): no other languages / full-width punctuation / `§` numbering anywhere (subject + body + tables + checklist).

**Description structure: use the repo PR template** at `.github/pull_request_template.md` if present (`gh pr create` picks it up automatically); otherwise fill the structure below into `--body` by hand (all English):

```markdown
## Change description

> Description here

## Type of change
- [ ] Bug fix
- [ ] New feature
- [ ] Document
- [ ] Others

## Related issues (if there is)

> Fix [#1]()

## Checklists

### Development

- [ ] Lint rules pass locally
- [ ] Application changes have been tested thoroughly
- [ ] Automated tests covering modified code pass

### Security

- [ ] Security impact of change has been considered
- [ ] Code follows security best practices and guidelines

### Code review

- [ ] Pull request has a descriptive title and context useful to a reviewer. Screenshots or screencasts are attached as necessary
```

Filling rules:
- `Change description` — the PR's overall goal + key decisions (summarize the phase evolution for multi-commit PRs);
- `Type of change` — check what applies;
- check only the boxes you actually satisfied — leave the rest blank and explain in the description; never blanket-check;
- anything the template doesn't cover but the reviewer needs (breaking change / cherry-pick option / mixed topics) → append to `Change description`.

**Trailer** (with §3.3):
- rebase-merge → each commit already carries the `Co-authored-by` trailer into `main` → **don't repeat it in the PR description**;
- if the repo ever switches to squash-merge (single commit) → the PR description **must** end with `Co-authored-by: Claude (<model-id>) <noreply@anthropic.com>`, else the squash commit loses the trailer.

**Description must NOT contain** (same as §3.3):
- `🤖 Generated with [Claude Code](https://...)` marketing banners;
- `Refs: ...` to ignored/local-only paths;
- internal branch names / commit-hash references (no reviewer context).

**Preview-verification (required):**
1. After drafting, **grep for full-width / non-English chars first**:
   ```bash
   grep -cP "[\x{4E00}-\x{9FFF}]|[\x{3000}-\x{303F}]|[\x{FF00}-\x{FFEF}]" /tmp/pr_description.md
   # must be 0
   ```
2. show the full text for preview;
3. only after the user edits/confirms, run `gh pr create --title "..." --body "$(cat /tmp/pr_description.md)"`;
4. report the PR URL.

**Not allowed:**
- pushing and walking away, leaving PR creation to the user;
- running `gh pr create` without letting the user preview the description (they must get a chance to edit);
- delivering a description without grepping for non-English residue.

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
- ❌ hand-editing `[project.dependencies]` / `[project.optional-dependencies]` / `[dependency-groups]` in `pyproject.toml`;
- ❌ hand-editing `uv.lock`;
- ❌ `pip freeze > requirements.txt`;
- ❌ `python -m pip install ...` to bypass uv.

### §4.3 Exception

If the user *explicitly* says "let me try pip" / "manually add this line to pyproject", follow the user. This rule constrains Claude's **default** behavior, not the user's direct instructions.

---

## 5. Tests

### §5.1 Unit tests

Under `tests/test_*.py`. CLI unit tests use one shape:

```
tests/test_cli_<module>_commands.py
```

- one file per module (aligns with `raven/cli/<module>_commands.py`);
- **don't** split by phase / feature / ticket (no `phase4` / `eve151` suffixes);
- aspect suffixes are allowed:
  - testing a CLI private helper: `test_cli_<helper>.py` (e.g. `test_cli_helpers.py` / `test_cli_stacks.py`);
  - cross-module behavior: `test_cli_<aspect>.py` (e.g. `test_cli_config_precedence.py` / `test_cli_smoke.py`).

### §5.2 Integration tests

Under `tests/integration/test_*.py`. Run against real environments (real LLM / channel / fcntl / subprocess / VM, etc.).

Naming: `test_<scope>_<kind>.py`, where `<kind>` ∈:

| kind | Meaning |
|---|---|
| `e2e` | End-to-end happy path, single/multi module |
| `smoke` | Multi-module interplay, just "it runs" |
| `real_<resource>` | Hits a real resource (`real_vm` / `real_llm` / `real_channel`, …) |

- **`<scope>` must not carry a version / ticket number** (no `v002` / `eve151`) — use a feature/scenario description.

### §5.3 Examples

| ❌ Wrong | ✅ Right | Reason |
|---|---|---|
| `test_cli_cron.py` | `test_cli_cron_commands.py` | missing `_commands` suffix |
| `test_sentinel_cli.py` | `test_cli_sentinel_commands.py` | order reversed |
| `test_cli_sentinel_phase4.py` | merge into `test_cli_sentinel_commands.py` | no phase suffix |
| `tests/integration/test_v002_smoke.py` | `test_<feature>_smoke.py` | no version number |
| `tests/integration/test_eve151_smoke.py` | `test_<feature>_smoke.py` | no ticket number |

### §5.4 Hard rules for Claude

- when changing/adding a CLI command, update the matching `test_cli_<module>_commands.py` — **don't create a new file**;
- when you spot a legacy file violating §5.1 / §5.2, **report it to the user first** — don't rename it unprompted (renames touch git history and may collide with follow-up PRs);
- always run tests via `uv run pytest ...`, never bare `pytest` (per §4).

---

## 6. Domain terms

- Naming a domain concept tracked in `CONTEXT-MAP.md` (the entry point — it routes to `CONTEXT.md` for Runtime terms and `ui-tui/CONTEXT.md` for TUI)? Use the canonical term, not a synonym.
- Coining a new domain term: define it in the matching `CONTEXT.md` in the same change, with a definition verifiable against the code (not guessed) — add an `_Avoid_` list only if a confusable synonym exists.

---

## Maintenance

This file holds **hard constraints only** (rules whose violation gets reverted / rejected). Soft suggestions, design preferences, and style leanings go in personal notes or conversation — not here.

Before adding a section, confirm with the user in conversation first — the shorter CLAUDE.md stays, the more useful it is.
