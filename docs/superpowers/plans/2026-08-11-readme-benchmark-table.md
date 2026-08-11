# README Benchmark Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the README benchmark image with a source-linked Markdown table in the same location.

**Architecture:** Keep the existing `## Benchmarks` section and hero banner. Replace only the benchmark image, summary bullets, and separate source list with one native Markdown table plus the existing evaluation caveat.

**Tech Stack:** GitHub-flavored Markdown, git, GitHub pull request 280

## Global Constraints

- Keep the Raven hero banner unchanged.
- Do not add an image or generated asset to git.
- Preserve every published benchmark value.
- Link each benchmark name to its official source.
- Update the existing draft pull request 280.

---

### Task 1: Replace the benchmark image with a table

**Files:**
- Modify: `README.md:21-34`
- Create: `docs/superpowers/plans/2026-08-11-readme-benchmark-table.md`

**Interfaces:**
- Consumes: the existing benchmark values and source URLs in `README.md`
- Produces: a native Markdown table rendered by GitHub without a benchmark image request

- [ ] **Step 1: Replace the image and bullets**

Use this exact table under `## Benchmarks`:

```markdown
| Benchmark | Raven Result | Comparison |
| --- | --- | --- |
| [Efficiency](https://raven.evermind.ai/) | `56.7%` at 27B; `58.1%` at 397B | Hermes `46.8%` / `47.9%`; `+9.9pp` at 27B |
| [Self-evolution](https://evermind-ai.github.io/EvoAgentBench/) | Ranked `#1` on EvoAgentBench | `+6.2pp` over the next result across four methods |
| [Proactivity](https://x.com/evermind) | `0.60` F1 on ProAgentBench | `2.4x` Hermes/OpenClaw at `0.253` |
```

Keep this sentence immediately below the table:

```markdown
Results describe the published test configurations; model, task set, and evaluation protocol all affect outcomes.
```

- [ ] **Step 2: Verify the Markdown diff**

Run:

```bash
git diff --check
rg -n 'user-attachments/assets|^\\| \\[.*\\]\\(https://' README.md
make check-large-files
```

Expected: the only `user-attachments` URL left in `README.md` is the Raven hero banner, all three benchmark rows contain source links, and both repository checks exit 0.

- [ ] **Step 3: Commit the README and plan**

```bash
git add README.md docs/superpowers/plans/2026-08-11-readme-benchmark-table.md
git commit -m "docs: replace benchmark image with table"
```

- [ ] **Step 4: Synchronize and push**

```bash
git fetch origin main
git merge-tree --write-tree HEAD origin/main
make check-large-files
git push origin docs/readme_github_banner
```

Expected: the branch is based on the latest `origin/main`, the checks pass, and pull request 280 contains the table commit.

- [ ] **Step 5: Preview pull request 280**

Open the branch README on GitHub, confirm the table occupies the former benchmark image position, and confirm all pull request checks pass.
