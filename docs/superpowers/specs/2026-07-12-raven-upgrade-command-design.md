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
compares versions, and replaces itself with a standard-library-only helper
running on the tool environment's external base Python. This path is testable,
does not execute downloaded shell code, and releases the active Raven
executable before uv replaces the installation on Windows.

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

Read the installed distribution's PEP 610 `direct_url.json` metadata. When the
file is present, require a nonempty URL and exactly one valid archive,
directory, or VCS origin record. An editable installation may run
`raven upgrade --check`, but `raven upgrade` must stop and explain that the
source checkout should be pulled and rebuilt.

Before mutation, require the uv tool receipt in the active environment. Derive
the active `UV_TOOL_DIR` from `sys.prefix` and `UV_TOOL_BIN_DIR` from the Raven
entrypoint's absolute `install-path`; malformed or ambiguous targets fail
closed. A non-editable installation that is not managed by uv must receive the
official installer guidance instead of being overwritten.

### Upgrade execution

When a newer release exists, locate `uv` on `PATH`, require both uv and
`sys._base_executable` to be outside the active Raven tool environment, and
replace the current process with that external Python via `os.execve`. The
inline helper is passed through `python -I -c`, uses only the standard library,
inherits the console, and receives explicit `UV_TOOL_DIR` and
`UV_TOOL_BIN_DIR` values. There is no temporary helper file to clean up.

Only after Raven has been replaced does the helper mirror the supported
installer flow:

1. Run `uv tool install --force "raven[channels] @ <wheel-url>"`.
2. If optional channel dependencies fail, retry the base wheel.
3. If the base fallback succeeds, warn that channel adapters may be
   unavailable.
4. If both attempts fail, return the final uv failure status with recovery
   guidance. Raven state under `~/.raven` remains untouched.

All process calls receive trusted argument arrays without a shell. The command
does not modify `~/.raven`, so configuration, sessions, memory, and runtime
state remain intact. The helper prints the final success or failure after uv
finishes and preserves the final uv exit status.

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

Add `tests/test_cli_upgrade_commands.py`, a bounded real-uv integration test,
and update the pinned CLI smoke surface. Cover:

- command help and root registration;
- up-to-date, newer-local, and update-available comparisons;
- `--check` never invoking uv;
- exact helper argument arrays, custom uv tool/bin targets, and process handoff;
- the channel-to-base fallback warning and final uv exit status;
- missing uv and both installation attempts failing;
- editable, malformed, and unsupported installation refusal;
- HTTP, malformed metadata, invalid URL, and missing-wheel failures;
- TUI dispatch blacklist and corrected fallback command text.

The integration test installs a temporary old uv tool in custom directories,
runs its own upgrade handoff, and verifies that the same entrypoint reports the
new version. A dedicated Windows CI job runs this test to protect the executable
locking scenario that motivated the external helper.

Run the focused Python suite, CLI/TUI RPC tests, TUI type/lint/tests, repository
lint, commit-message lint, PR-title lint, and the large-file check before the PR.

## Documentation

Update both README files so existing users understand that:

- `raven upgrade --check` checks the latest stable Release;
- `raven upgrade` replaces the installed Raven tool without resetting state;
- the command is manual, not a background auto-update service;
- source installations follow the developer update workflow.
