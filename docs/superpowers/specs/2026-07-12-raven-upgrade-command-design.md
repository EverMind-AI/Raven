# Raven Upgrade Command Design

## Status

Approved for implementation on 2026-07-12. Tracks GitHub issue #111.

## Context

Raven's public installers resolve the wheel attached to the latest published
GitHub Release and install it as a global uv tool. Raven does not currently
expose a user-facing update command, so existing users must rerun the original
installer to receive a new release. The TUI also retains dormant Hermes-era
fields that refer to commit counts and a nonexistent `raven update` command.

## Goals

- Add `raven upgrade --check` for a read-only release check.
- Add `raven upgrade` for upgrading a supported uv-tool installation.
- Use the latest published stable GitHub Release as the only update source.
- Preserve Raven state under `~/.raven`.
- Refuse to overwrite editable or unsupported installations.
- Keep upgrade execution out of the running TUI process.
- Document the upgrade workflow for all supported operating systems.

## Non-goals

- Background or silent automatic updates.
- Updating from an unpublished commit, tag, draft Release, or prerelease.
- Updating a developer's source checkout automatically.
- Enabling PyPI distribution.
- Adding a synchronous network check to TUI startup.

## Considered approaches

### Native Python updater (selected)

The CLI queries GitHub's latest-release endpoint, validates the release wheel,
compares versions, and invokes uv with an argument list. This path is testable,
does not execute downloaded shell code, and can protect nonstandard installs.

### Installer wrapper

The CLI could rerun `install.sh` or `install.ps1`. This would reuse the current
installer but would execute remote scripts, repeat first-install runtime checks,
depend on platform shells, and be harder to test reliably.

### Check-only assistant

The CLI could report an available version and print the existing installer
command. This is the smallest change, but it leaves the manual reinstall step
that the feature is intended to remove.

## Architecture

### CLI module

Add `raven/cli/upgrade_commands.py` with the same `register(app)` boundary used
by the other top-level command modules. Keep orchestration in the command
callback and isolate network, metadata, version, and subprocess behavior behind
small functions that unit tests can replace.

The module will expose a small immutable release record containing the stable
version and wheel URL. Version comparison will accept Raven's documented
`MAJOR.MINOR.PATCH` format and will not treat commit distance as a release.

### Release resolution

Use `GET https://api.github.com/repos/EverMind-AI/Raven/releases/latest` through
the existing `httpx` runtime dependency. Require:

- a stable `vMAJOR.MINOR.PATCH` tag;
- a non-draft, non-prerelease response;
- exactly one Raven `.whl` asset for that version;
- an HTTPS download URL under the Raven GitHub Release path.

Malformed responses, timeouts, rate limits, missing assets, and non-success
responses must produce actionable errors and a nonzero exit code.

### Installation-mode protection

Read the installed distribution's PEP 610 `direct_url.json` metadata. An
editable installation may run `raven upgrade --check`, but `raven upgrade`
must stop and explain that the source checkout should be pulled and rebuilt.

Before mutation, require the uv tool receipt in the active environment. A
non-editable installation that is not managed by uv must receive the official
installer guidance instead of being overwritten.

### Upgrade execution

When a newer release exists, locate `uv` on `PATH` and mirror the supported
installer flow:

1. Run `uv tool install --force "raven[channels] @ <wheel-url>"`.
2. If optional channel dependencies fail, retry the base wheel.
3. If both attempts fail, return the final uv failure status with recovery
   guidance. Raven state under `~/.raven` remains untouched.

Subprocesses receive argument arrays and inherited terminal output. The command
does not modify `~/.raven`, so configuration, sessions, memory, and runtime state
remain intact. On success, the user is told to restart any running Raven process.

### TUI boundary

Add `upgrade` to the TUI RPC dispatch blacklist because replacing Raven from an
active TUI process is unsafe. Correct dormant fallback/demo text from
`raven update` to `raven upgrade`, but do not add a startup network request or a
new RPC version contract in this PR.

A future TUI update notice should use release-oriented fields such as
`update_available` and `latest_version`, populated asynchronously or from a
cache, rather than the current `update_behind` commit count.

## User-visible behavior

- Current version equals latest: report that Raven is up to date and exit zero.
- Current version is newer than latest: report a development/newer build and
  exit zero without downgrading.
- New stable release with `--check`: report `current -> latest`, print
  `Run raven upgrade`, and exit zero without a subprocess.
- New stable release without `--check`: run the uv installation flow.
- Editable or unsupported install with `--check`: perform the release comparison
  without changing the environment.
- Editable or unsupported install without `--check`: explain the correct update
  path and exit nonzero without attempting uv installation.

## Tests

Add `tests/test_cli_upgrade_commands.py` and update the pinned CLI smoke surface.
All network and subprocess behavior will be mocked. Cover:

- command help and root registration;
- up-to-date, newer-local, and update-available comparisons;
- `--check` never invoking uv;
- exact uv argument arrays and the channel-to-base fallback;
- missing uv and both installation attempts failing;
- editable and unsupported installation refusal;
- HTTP, malformed metadata, invalid URL, and missing-wheel failures;
- TUI dispatch blacklist and corrected fallback command text.

Run the focused Python suite, CLI/TUI RPC tests, TUI type/lint/tests, repository
lint, commit-message lint, PR-title lint, and the large-file check before the PR.

## Documentation

Update both README files so existing users understand that:

- `raven upgrade --check` checks the latest stable Release;
- `raven upgrade` replaces the installed Raven tool without resetting state;
- the command is manual, not a background auto-update service;
- source installations follow the developer update workflow.
