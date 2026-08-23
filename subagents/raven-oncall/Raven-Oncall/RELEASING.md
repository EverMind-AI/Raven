# Releasing Raven

How a new Raven version is cut and published.

## Versioning

- Semantic versioning `MAJOR.MINOR.PATCH`. The source of truth is `version` in
  `pyproject.toml`.
- Tags: `vX.Y.Z` for a stable release, `vX.Y.Z-rcN` for a pre-release. The tag
  must match the `pyproject.toml` version -- CI enforces this (`release.yml`).
- For a pre-release, keep `pyproject.toml` at the base version (e.g. `0.1.3`
  while tagging `v0.1.3-rc1`); CI compares only the base. Do NOT set the
  version to `0.1.3-rc1` -- that is not a version hatch will build.

## Release title

`Raven X.Y.Z (YYYY-MM-DD)` -- for example `Raven 0.1.3 (2026-07-08)`. The CI
draft fills this in automatically (date is the build date; adjust when
publishing if needed).

## Release notes

Notes are hand-written and curated. The CI draft prefills the boilerplate
(Install, Release Status, Notes); a human writes the one-line summary and the
Highlights before publishing. Structure:

```
<one-line summary>

## Highlights
- <user-facing change>

## Install
  curl -fsSL https://raven.evermind.ai/install.sh | bash
  then: raven onboard

## Release Status
- Version: `X.Y.Z`
- Tag: `vX.Y.Z`
- Stability: <public preview patch | public preview minor | ...>   # fill by hand per release type
- Assets: wheel and source distribution attached to this release

## Notes
- pre-1.0 evolution caveat
- PyPI not enabled; install via the GitHub Release wheel
```

`Stability` is not boilerplate -- set it by release type (patch / minor / rc).

## Flow

1. Bump `version` in `pyproject.toml`; open a PR; merge to `main`.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. CI (`release.yml`) builds the wheel + sdist and creates a **draft** GitHub
   Release with both attached, titled and prefilled from the template.
4. Fill the summary + Highlights in the draft, then click **Publish**.
   Publishing makes it `/releases/latest`, which `install.sh` serves.

## Pre-releases

- `vX.Y.Z-rcN` tags build a draft marked **pre-release**. A pre-release is never
  `/releases/latest`, so `curl | sh` users are unaffected. Use an rc tag to
  verify the release pipeline before cutting the stable tag; delete the rc
  release and tag afterward.

## Notes

- `main` is squash-merge + PR-only. The release itself is not automated past the
  draft: publishing is a deliberate human step.
- PyPI publishing is not wired up; the supported install path is the GitHub
  Release wheel asset resolved by `install.sh`.
