# README Welcome Screens Design

## Goal

Show the first Raven terminal screen directly below the benchmark table so readers can see the installed product before starting the setup instructions.

## Layout

Add a short introductory sentence followed by a two-column GitHub-flavored Markdown table:

| Light theme | Dark theme |
| --- | --- |
| Light welcome screen | Dark welcome screen |

Use equivalent translated copy in `README.zh-CN.md`. Keep the section between the benchmark caveat and Quick Start in both files.

## Image Delivery

- Resize each source screenshot to 1600 pixels wide while preserving aspect ratio.
- Re-encode as JPEG at quality 82 and keep each file below 500 KB.
- Upload both files through GitHub User Content.
- Reference only the public attachment URLs from the READMEs.
- Do not add image files to git.

## Accessibility

Use descriptive alt text that identifies Raven's terminal welcome screen and the displayed theme. Keep visible column labels so the comparison remains understandable if images load slowly.

## Verification

- Confirm both public attachment URLs load successfully.
- Confirm both files are below 500 KB.
- Confirm the English and Chinese READMEs use the same two URLs and matching placement.
- Preview the rendered table on GitHub.
- Run `git diff --check` and `make check-large-files`.

## Rollback

Remove the introductory sentence and image table from both READMEs. The unreferenced GitHub User Content attachments do not affect repository size.
