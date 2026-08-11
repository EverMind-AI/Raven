# README Benchmark Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Raven's repetitive README narrative with a concise benchmark-led story and detailed, evidence-backed Deep Research and Tracing sections.

**Architecture:** Build one deterministic benchmark proof board outside the repository, upload it through GitHub User Content, and reference it from a rewritten `README.md`. Keep implementation and generated assets out of git; only Markdown source and planning documents enter the pull request.

**Tech Stack:** Markdown, HTML/CSS/SVG rendered through local Chrome, WebP or PNG, GitHub User Content, git, gh CLI

## Global Constraints

- Do not commit images, SVGs, HTML, manifests, or other generated assets.
- Keep the benchmark image below 500 KB while preserving crisp chart text.
- Use only claims supported by Raven code, an official benchmark, or approved EverMind copy.
- Keep repository prose and commit metadata in English.
- Run `make check-large-files` because README and generated-output references change.
- Update the existing draft pull request instead of creating a second pull request.

---

### Task 1: Build the benchmark proof board

**Files:**
- Create outside git: `/private/tmp/raven-benchmark-board.html`
- Create outside git: `/private/tmp/raven-benchmark-board.png`
- Create outside git: `/private/tmp/raven-benchmark-board.webp`

**Interfaces:**
- Consumes: the three user-supplied JPEG drafts and the current Raven banner palette
- Produces: one 1920 x 960 benchmark board suitable for a full-width GitHub README image

- [ ] **Step 1: Lock the visual system**

Use this palette and type hierarchy in the temporary HTML:

```text
canvas:       #160f08
panel:        #24170c
panel-soft:   #2e1d0f
gold:         #f2b51d
gold-muted:   #a97d17
text:         #f7f0df
text-muted:   #b8aa92
grid:         #4b3825
headline:     ui-monospace, SFMono-Regular, Menlo, monospace
body:         Inter, ui-sans-serif, system-ui, sans-serif
```

- [ ] **Step 2: Build one three-card board**

Use the following exact card copy and approved figures:

```text
01  EFFICIENCY
Better Results, Fewer Tokens
56.7% vs 46.8% at 27B
58.1% vs 47.9% at 397B

02  SELF-EVOLUTION
Learns Best Among Peers
#1 across four methods
+6.2pp lift on EvoAgentBench

03  PROACTIVITY
Acts Earlier, Scores Higher
0.60 F1 vs 0.253
2.4x on ProAgentBench
```

Each card contains a compact chart, a large result, a short benchmark label, and no paragraph longer than two lines. Use `OpenClaw` consistently and include `Hermes` only where the supplied comparison names it.

- [ ] **Step 3: Render and compress**

Render at 1920 x 960, then compare lossless WebP and optimized PNG. Select the smallest version whose labels remain crisp at a 920px GitHub display width.

Run:

```bash
file /private/tmp/raven-benchmark-board.webp
stat -f "%z bytes" /private/tmp/raven-benchmark-board.webp
shasum -a 256 /private/tmp/raven-benchmark-board.webp
```

Expected: 1920 x 960 image, fewer than 500000 bytes, and a recorded SHA-256 digest.

### Task 2: Rewrite the README narrative

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the information architecture in `docs/superpowers/specs/2026-08-11-readme-benchmark-redesign.md`
- Produces: a concise README ready to receive the generated GitHub User Content URL

- [ ] **Step 1: Replace the opening narrative**

Keep the existing Raven banner and community links. Follow them with one positioning paragraph and a `## Benchmarks` section before installation. The section must include the proof-board image, a one-sentence interpretation, and direct links to the Raven benchmark page and EvoAgentBench methodology.

- [ ] **Step 2: Collapse onboarding into Quick Start**

Keep the POSIX and native Windows install commands, then show this exact first-run sequence:

```bash
raven onboard
raven
raven doctor
```

Move upgrade details to one short paragraph and two commands.

- [ ] **Step 3: Write the Deep Research section**

Explain opt-in setup, per-query deep-versus-regular choice, MiroThinker execution, report persistence, background delivery, quota awareness, and these commands:

```bash
raven deep-research enable
raven deep-research get
```

- [ ] **Step 4: Write the Tracing section**

Explain local span capture, the turn-to-memory hierarchy, usage/cost/latency/error inspection, artifact panels, no-throw behavior, local storage, and this command:

```bash
raven tracing
```

- [ ] **Step 5: Consolidate the remaining sections**

Retain one compact section for core systems, one support line for providers and gateways, the architecture diagram, a compact command table, and links for docs, ecosystem, contributing, status, and license. Remove duplicate feature narratives, full gateway/status/ecosystem tables, repeated back-to-top badges, and the long Agent Templates policy copy.

### Task 3: Publish the image without adding it to git

**Files:**
- Modify: `README.md`
- Upload outside git: `/private/tmp/raven-benchmark-board.webp`

**Interfaces:**
- Consumes: the compressed proof board from Task 1
- Produces: a public `https://github.com/user-attachments/assets/...` URL referenced by `README.md`

- [ ] **Step 1: Upload through pull request 280**

Upload the WebP in the GitHub pull request composer, copy the generated GitHub User Content URL, and ensure the pull request references the attachment so GitHub publishes it.

- [ ] **Step 2: Replace the temporary README URL**

Set the alt text to `Raven benchmark results across efficiency, self-evolution, and proactivity` and use the exact asset URL returned by the GitHub composer.

- [ ] **Step 3: Verify the public attachment**

Download it without browser authentication and compare the uploaded bytes:

Download the exact generated asset URL to `/private/tmp/raven-benchmark-board-public.webp`, then run:

```bash
stat -f "%z bytes" /private/tmp/raven-benchmark-board-public.webp
shasum -a 256 /private/tmp/raven-benchmark-board.webp /private/tmp/raven-benchmark-board-public.webp
```

Expected: the download succeeds, remains below 500000 bytes, and both SHA-256 digests match.

### Task 4: Verify, commit, and update the draft pull request

**Files:**
- Modify: `README.md`
- Create: `docs/superpowers/specs/2026-08-11-readme-benchmark-redesign.md`
- Create: `docs/superpowers/plans/2026-08-11-readme-benchmark-redesign.md`

**Interfaces:**
- Consumes: the finished README and public benchmark asset URL
- Produces: an updated, reviewable draft pull request 280

- [ ] **Step 1: Run documentation checks**

```bash
git diff --check
make check-large-files
```

Expected: both commands exit 0.

- [ ] **Step 2: Review the exact diff**

```bash
git status --short
git diff -- README.md docs/superpowers/specs/2026-08-11-readme-benchmark-redesign.md docs/superpowers/plans/2026-08-11-readme-benchmark-redesign.md
```

Expected: no generated image or web artifact appears in git status.

- [ ] **Step 3: Commit the approved scope**

```bash
git add README.md docs/superpowers/specs/2026-08-11-readme-benchmark-redesign.md docs/superpowers/plans/2026-08-11-readme-benchmark-redesign.md
git commit -m "docs: redesign README around benchmark proof"
```

- [ ] **Step 4: Synchronize and push**

```bash
git fetch origin main
git merge-tree --write-tree HEAD origin/main
make check-large-files
git push --force-with-lease origin docs/readme_github_banner
```

Expected: the branch is based on the latest `origin/main`, checks pass after synchronization, and draft pull request 280 updates.

- [ ] **Step 5: Update and verify the pull request**

Update the PR title to `docs: redesign README around benchmark proof`, rewrite the description using `.github/pull_request_template.md`, verify the entire description is ASCII, and preview the rendered README on GitHub. Keep the PR in draft state for maintainer review.
