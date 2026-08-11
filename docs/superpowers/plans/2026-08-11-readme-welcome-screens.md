# README Welcome Screens Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optimized light and dark Raven terminal welcome screens below the benchmark table in both READMEs.

**Architecture:** Produce two 1600-pixel-wide JPEG derivatives outside the repository, upload them through GitHub User Content, and reference the resulting public URLs from a two-column Markdown table. Keep the English and Chinese documents structurally aligned and leave git free of image binaries.

**Tech Stack:** macOS `sips`, GitHub User Content, GitHub-flavored Markdown, git, GitHub pull request 280

## Global Constraints

- Resize each source screenshot to 1600 pixels wide while preserving aspect ratio.
- Re-encode each screenshot as JPEG at quality 82.
- Keep each delivered image below 500 KB.
- Do not add image binaries to git.
- Use the same two public GitHub User Content URLs in both READMEs.
- Place the showcase after the benchmark caveat and before Quick Start.

---

### Task 1: Publish and embed the terminal welcome screens

**Files:**
- Modify: `README.md:30-32`
- Modify: `README.zh-CN.md:30-32`
- Create outside git: `/private/tmp/raven-welcome-light.jpg`
- Create outside git: `/private/tmp/raven-welcome-dark.jpg`

**Interfaces:**
- Consumes: the two user-provided JPEG screenshots and the existing benchmark sections
- Produces: two public attachment URLs and matching English and Chinese Markdown tables

- [ ] **Step 1: Create optimized derivatives**

Use `sips --resampleWidth 1600 --setProperty format jpeg --setProperty formatOptions 82` to create `/private/tmp/raven-welcome-light.jpg` and `/private/tmp/raven-welcome-dark.jpg` without modifying the source files.

- [ ] **Step 2: Verify visual quality and size**

Run `file` and `ls -l` on both derivatives. Confirm each is a 1600-pixel-wide JPEG below 512000 bytes, then inspect both images for readable terminal text and intact colors.

- [ ] **Step 3: Upload through GitHub User Content**

Attach both derivatives to pull request 280 through GitHub's comment composer. Capture the resulting `https://github.com/user-attachments/assets/...` URLs and confirm each URL downloads to the expected byte size.

- [ ] **Step 4: Add the English showcase**

Insert this structure after the benchmark caveat in `README.md`, using the captured URLs:

```markdown
Install Raven, run `raven` in your terminal, and this is the welcome screen you will see:

| Light theme | Dark theme |
| --- | --- |
| ![Raven terminal welcome screen in light theme](https://github.com/user-attachments/assets/d415573d-98ab-4265-872b-67c33b42dcee) | ![Raven terminal welcome screen in dark theme](https://github.com/user-attachments/assets/0ffa1ba4-c03f-4d3f-bfff-d9eda87122dd) |
```

- [ ] **Step 5: Add the Chinese showcase**

Insert the translated structure at the same location in `README.zh-CN.md`:

```markdown
安装 Raven 后，在终端运行 `raven`，你会看到下面的欢迎界面：

| 浅色主题 | 深色主题 |
| --- | --- |
| ![Raven 终端浅色主题欢迎界面](https://github.com/user-attachments/assets/d415573d-98ab-4265-872b-67c33b42dcee) | ![Raven 终端深色主题欢迎界面](https://github.com/user-attachments/assets/0ffa1ba4-c03f-4d3f-bfff-d9eda87122dd) |
```

- [ ] **Step 6: Verify the repository diff**

Run `git diff --check`, confirm both READMEs contain the same two attachment URLs, and run `make check-large-files`. Expected: all commands exit 0 and no image binary appears in `git status`.

- [ ] **Step 7: Commit and synchronize**

Commit the two README changes and this plan with `docs: add terminal welcome screen showcase`. Fetch `origin/main`, run `git merge-tree --write-tree HEAD origin/main`, rerun the checks, and push `docs/readme_github_banner` to pull request 280.

- [ ] **Step 8: Preview and verify pull request 280**

Open the exact remote commit on GitHub, confirm the two-column table renders between Benchmarks and Quick Start in both READMEs, update the pull request description, and wait for all checks to pass.
