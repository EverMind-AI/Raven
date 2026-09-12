# Refresh Raven Harness Branding Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Align Raven's public README positioning and banner copy with the Harness of Harnesses direction while removing the public DeepSeek Harness integration claim.

**Architecture:** Keep runtime provider support unchanged. Update only public documentation and the external banner reference, with a local generated banner preview kept outside the repository until it can be uploaded as a GitHub user attachment.

**Tech Stack:** Markdown, GitHub-hosted image URL, repository documentation checks.

### Task 1: Update the public positioning copy

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`

**Step 1:** Replace the public network phrase with `One Surface, All Agents` in English-facing copy and the corresponding Chinese wording where it is presented as translated prose.

**Step 2:** Keep `The Harness of Harnesses` as the canonical product name and remove public wording that presents Deep Research as the headline positioning.

**Step 3:** Update the banner alt text so it describes the current brand message.

### Task 2: Remove the public DeepSeek Harness integration claim

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`

**Step 1:** Remove the DeepSeek Harness / DSH row from the public ecosystem table.

**Step 2:** Preserve the DeepSeek model provider and all runtime/provider implementation paths.

**Step 3:** Verify no README claim or link still advertises the removed public integration.

### Task 3: Validate the documentation change

**Files:**
- No new tracked asset; local preview remains outside the repository.

**Step 1:** Run the README-focused pytest checks with `uv run pytest`.

**Step 2:** Run `make check-large-files` because README files were changed.

**Step 3:** Inspect the final diff and confirm the worktree contains only the intended documentation/context changes.
