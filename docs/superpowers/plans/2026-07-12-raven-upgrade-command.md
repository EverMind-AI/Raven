# Raven Upgrade Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe `raven upgrade` command that checks and installs the latest published stable Raven Release without requiring users to rerun the public installer.

**Architecture:** A focused top-level CLI module resolves and validates GitHub Release metadata, compares strict Raven semantic versions, and protects editable or malformed installations. For mutation, Raven replaces itself with an isolated, standard-library-only helper running on the external base Python; the helper restores the active uv tool/bin directories and performs the installer-compatible fallback only after the Raven executable is no longer active. TUI dispatch remains unable to run self-upgrade, while dormant update copy points to the real command.

**Tech Stack:** Python 3.12, Typer, Rich, httpx, importlib.metadata, uv, pytest, React/Ink, TypeScript, Vitest.

## Global Constraints

- Only `GET https://api.github.com/repos/EverMind-AI/Raven/releases/latest` defines an available stable update.
- Never advertise unpublished commits, tags without Releases, draft Releases, or prereleases.
- Do not add background or silent automatic updates.
- Do not overwrite editable source checkouts or unsupported package-manager installations.
- Do not modify Raven state under `~/.raven`.
- Use uv for all Python dependency and command execution; never use pip.
- Run Python tests with `uv run pytest`.
- Keep repository comments necessary and English-only.
- Do not add report assets, standalone web artifacts, or files over 1 MiB.
- Use Conventional Commits with ASCII-only English messages.

## File map

- Create `raven/cli/upgrade_commands.py`: release lookup, validation, install-mode guards, uv execution, and Typer registration.
- Create `tests/test_cli_upgrade_commands.py`: focused unit and command tests with no real network or tool mutation.
- Create `tests/integration/test_cli_upgrade_real_uv.py`: bounded uv self-replacement test using temporary custom directories.
- Modify `.github/workflows/ci.yml`: run the real self-replacement test on Windows.
- Modify `raven/cli/commands.py`: register the new top-level command.
- Modify `tests/test_cli_smoke.py`: pin `upgrade` in the root command surface.
- Modify `raven/tui_rpc/methods/cli_dispatch.py`: reject upgrades from an active TUI RPC process.
- Modify `tests/test_tui_rpc_cli_dispatch.py`: pin the expanded dispatch blacklist.
- Modify `ui-tui/src/components/branding.tsx`: replace the nonexistent fallback command.
- Modify `ui-tui/src/demo/gallery.tsx`: keep demo data aligned with production copy.
- Modify `ui-tui/src/__tests__/branding.test.tsx`: render and verify the fallback upgrade hint.
- Modify `README.md` and `README.zh-CN.md`: document checks, upgrades, state preservation, and source-install behavior.

---

### Task 1: Release discovery and version comparison

**Files:**
- Create: `raven/cli/upgrade_commands.py`
- Create: `tests/test_cli_upgrade_commands.py`

**Interfaces:**
- Consumes: `httpx.Client`, `importlib.metadata.version("raven")`.
- Produces: `ReleaseInfo(version: str, wheel_url: str)`, `_version_key(value: str) -> tuple[int, int, int]`, `_parse_release_payload(payload: object) -> ReleaseInfo`, `_fetch_latest_release() -> ReleaseInfo`, `_current_version() -> str`.

- [ ] **Step 1: Write failing tests for strict Raven versions and release metadata**

Add tests with exact stable and invalid examples:

```python
import pytest

from raven.cli import upgrade_commands


def test_version_key_accepts_documented_stable_versions() -> None:
    assert upgrade_commands._version_key("0.1.3") == (0, 1, 3)
    assert upgrade_commands._version_key("v2.10.4") == (2, 10, 4)


@pytest.mark.parametrize("value", ["0.1", "0.1.3-rc1", "latest", "01.2.3"])
def test_version_key_rejects_nonstable_versions(value: str) -> None:
    with pytest.raises(upgrade_commands.UpgradeError):
        upgrade_commands._version_key(value)


def test_parse_release_payload_selects_exact_release_wheel() -> None:
    release = upgrade_commands._parse_release_payload(
        {
            "tag_name": "v0.1.4",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": "raven-0.1.4-py3-none-any.whl",
                    "browser_download_url": "https://github.com/EverMind-AI/Raven/releases/download/v0.1.4/raven-0.1.4-py3-none-any.whl",
                }
            ],
        }
    )
    assert release.version == "0.1.4"
```

Also cover draft, prerelease, malformed payload, wrong wheel filename, duplicate exact wheels, HTTP URL, wrong host, and wrong repository path.

- [ ] **Step 2: Run the focused tests and confirm the red state**

Run:

```bash
uv run pytest tests/test_cli_upgrade_commands.py -q
```

Expected: collection or import failure because `raven.cli.upgrade_commands` does not exist.

- [ ] **Step 3: Implement the immutable release record and strict parsing**

Create the module with these boundaries:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import metadata
from urllib.parse import urlparse

import httpx

LATEST_RELEASE_API = "https://api.github.com/repos/EverMind-AI/Raven/releases/latest"
_VERSION_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class UpgradeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    wheel_url: str


def _version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise UpgradeError(f"Unsupported Raven version: {value}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _current_version() -> str:
    return metadata.version("raven")
```

Implement `_parse_release_payload` so it validates booleans, the exact `raven-X.Y.Z-py3-none-any.whl` asset, HTTPS, host `github.com`, and path `/EverMind-AI/Raven/releases/download/vX.Y.Z/<wheel>` before returning `ReleaseInfo`.

- [ ] **Step 4: Implement the HTTP boundary with deterministic errors**

Use an injectable client and the public API headers:

```python
def _fetch_latest_release(client: httpx.Client | None = None) -> ReleaseInfo:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"raven/{_current_version()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if client is not None:
        response = client.get(LATEST_RELEASE_API, headers=headers)
        response.raise_for_status()
        return _parse_release_payload(response.json())
    with httpx.Client(timeout=10.0, follow_redirects=True) as owned_client:
        response = owned_client.get(LATEST_RELEASE_API, headers=headers)
        response.raise_for_status()
        return _parse_release_payload(response.json())
```

Extend tests with `httpx.MockTransport` for success, timeout, non-2xx, and invalid JSON. The command layer will translate `httpx.HTTPError`, `ValueError`, and `UpgradeError` into user-facing failures.

- [ ] **Step 5: Run focused tests and commit the release-discovery unit**

Run:

```bash
uv run pytest tests/test_cli_upgrade_commands.py -q
uv run ruff check raven/cli/upgrade_commands.py tests/test_cli_upgrade_commands.py
uv run ruff format --check raven/cli/upgrade_commands.py tests/test_cli_upgrade_commands.py
```

Expected: all Task 1 tests pass and lint exits zero.

Commit:

```bash
git add raven/cli/upgrade_commands.py tests/test_cli_upgrade_commands.py
git commit -m "feat(cli): resolve raven release upgrades"
```

### Task 2: Installation guards and upgrade command

**Files:**
- Modify: `raven/cli/upgrade_commands.py`
- Modify: `tests/test_cli_upgrade_commands.py`
- Modify: `raven/cli/commands.py:104-118`
- Modify: `tests/test_cli_smoke.py:44-73,154-181`

**Interfaces:**
- Consumes: `ReleaseInfo`, `_version_key`, `_fetch_latest_release`, `_current_version` from Task 1.
- Produces: `_is_editable_install() -> bool`, `_uv_tool_target() -> ToolInstallTarget | None`, `_handoff_upgrade(release, current_version, target) -> NoReturn`, an inline standard-library helper, and `register(app: typer.Typer) -> None`.

- [ ] **Step 1: Write failing command and install-mode tests**

Add CLI tests that monkeypatch all external boundaries:

```python
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from raven.cli import upgrade_commands
from raven.cli.commands import app

WHEEL_URL = "https://github.com/EverMind-AI/Raven/releases/download/v0.1.4/raven-0.1.4-py3-none-any.whl"
runner = CliRunner()


def test_upgrade_check_reports_available_without_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_commands, "_current_version", lambda: "0.1.3")
    monkeypatch.setattr(
        upgrade_commands,
        "_fetch_latest_release",
        lambda: upgrade_commands.ReleaseInfo("0.1.4", WHEEL_URL),
    )
    handoff = Mock()
    monkeypatch.setattr(upgrade_commands, "_handoff_upgrade", handoff)

    result = runner.invoke(app, ["upgrade", "--check"])

    assert result.exit_code == 0
    assert "0.1.3 -> 0.1.4" in result.stdout
    assert "raven upgrade" in result.stdout
    handoff.assert_not_called()
```

Cover equal versions, a newer local version with no downgrade, editable refusal, unsupported install refusal, missing uv, successful channel install, channel failure followed by base success, both attempts failing, and network/release errors returning exit code 1.

For exact uv behavior, capture these calls:

```python
assert calls == [
    ("/usr/bin/uv", f"raven[channels] @ {WHEEL_URL}"),
    ("/usr/bin/uv", WHEEL_URL),
]
```

- [ ] **Step 2: Run tests and confirm command registration is red**

Run:

```bash
uv run pytest tests/test_cli_upgrade_commands.py tests/test_cli_smoke.py -q
```

Expected: failures because `upgrade` is not registered and install helpers do not exist.

- [ ] **Step 3: Implement strict PEP 610 and uv-receipt guards**

Distinguish an absent `direct_url.json` from a malformed present file. Require
a nonempty URL and exactly one valid `archive_info`, `dir_info`, or `vcs_info`
record. Treat `dir_info.editable` as optional with a false default, but require
it to be boolean when present; require the PEP 610 VCS fields.

Parse `sys.prefix/uv-receipt.toml` into an immutable `ToolInstallTarget`.
Require a Raven requirement and exactly one Raven entrypoint with an absolute
`install-path`. Derive `UV_TOOL_DIR` from `Path(sys.prefix).parent` and
`UV_TOOL_BIN_DIR` from the entrypoint parent. Missing receipts remain an
unsupported install; present malformed receipts fail closed.

- [ ] **Step 4: Implement post-exit uv execution with the installer-compatible fallback**

Resolve uv and `sys._base_executable` to files outside the active Raven tool
prefix. Override `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` in a copied environment,
flush inherited output, and call `os.execve` with:

```python
[base_python, "-I", "-c", helper_source, uv_path, wheel_url, current, latest]
```

The inline helper must use only the standard library and trusted argument
arrays. It runs the channels requirement first, falls back to the base wheel,
warns only when that fallback succeeds, prints the final success itself, catches
uv execution `OSError`, and returns the final uv status when both attempts fail.
Because the helper is inline, no temporary artifact needs cleanup.

- [ ] **Step 5: Register and orchestrate the top-level command**

Add `upgrade_commands` to the import and registration list in `raven/cli/commands.py`. The Typer callback must:

1. Fetch and validate the latest Release.
2. Compare current and latest version keys.
3. Exit zero for equal or newer-local versions.
4. Print `current -> latest` and exit zero for `--check`.
5. Refuse editable, malformed, and non-uv installs before invoking `_handoff_upgrade`.
6. Do not print success in the parent; the post-exit helper owns final status.
7. Catch `UpgradeError`, `httpx.HTTPError`, JSON errors, and metadata errors, print one actionable red error, and raise `typer.Exit(1)`.

Register `upgrade` in both `TOP_LEVEL_COMMANDS` and `REGISTERED_COMMAND_NAMES` in `tests/test_cli_smoke.py`.

Add `tests/integration/test_cli_upgrade_real_uv.py` to install an old temporary
uv tool in custom directories, invoke its own handoff, and verify the same
entrypoint reports the new version. Run this bounded test in a dedicated
Windows CI job to cover executable locking.

- [ ] **Step 6: Run focused tests and commit the working CLI**

Run:

```bash
uv run pytest tests/test_cli_upgrade_commands.py tests/test_cli_smoke.py -q
uv run ruff check raven/cli/upgrade_commands.py raven/cli/commands.py tests/test_cli_upgrade_commands.py tests/test_cli_smoke.py
uv run ruff format --check raven/cli/upgrade_commands.py raven/cli/commands.py tests/test_cli_upgrade_commands.py tests/test_cli_smoke.py
```

Expected: all focused tests and lint pass.

Commit:

```bash
git add raven/cli/upgrade_commands.py raven/cli/commands.py tests/test_cli_upgrade_commands.py tests/test_cli_smoke.py
git commit -m "feat(cli): add raven upgrade command"
```

### Task 3: TUI upgrade safety and accurate hint

**Files:**
- Modify: `raven/tui_rpc/methods/cli_dispatch.py:66-103`
- Modify: `tests/test_tui_rpc_cli_dispatch.py:232-257`
- Modify: `ui-tui/src/components/branding.tsx:415-435`
- Modify: `ui-tui/src/demo/gallery.tsx:102-104`
- Modify: `ui-tui/src/__tests__/branding.test.tsx`

**Interfaces:**
- Consumes: the registered `upgrade` command from Task 2.
- Produces: `("upgrade",)` in `_DISPATCH_BLACKLIST`; TUI fallback text `raven upgrade`.

- [ ] **Step 1: Write failing Python and TypeScript safety tests**

Extend the Python blacklist expectation and probe:

```python
expected_entries = {
    ("gateway",),
    ("provider", "login"),
    ("channels", "login", "weixin"),
    ("channels", "login", "whatsapp"),
    ("sandbox", "shell"),
    ("tui",),
    ("onboard",),
    ("upgrade",),
}
assert _is_dispatch_compatible(["upgrade"]) is False
assert _is_dispatch_compatible(["upgrade", "--check"]) is False
```

Render the real session panel in `branding.test.tsx`:

```tsx
it('recommends the real raven upgrade command', () => {
  const info: SessionInfo = {
    model: 'anthropic/claude-sonnet-4-6',
    skills: {},
    tools: {},
    update_behind: 1
  }
  const { lastFrame } = render(<SessionPanel info={info} maxCols={80} sid="test" t={DEFAULT_THEME} />)
  expect(lastFrame()).toContain('raven upgrade')
  expect(lastFrame()).not.toContain('raven update')
})
```

Import `SessionPanel` and `SessionInfo` explicitly.

- [ ] **Step 2: Run both tests and confirm the red state**

Run:

```bash
uv run pytest tests/test_tui_rpc_cli_dispatch.py -q
npm test --prefix ui-tui -- src/__tests__/branding.test.tsx
```

Expected: Python blacklist mismatch and TypeScript fallback text assertion failure.

- [ ] **Step 3: Add the TUI blacklist entry and correct both literals**

Add `("upgrade",)` with an English why-comment stating that replacing the active Raven process is terminal-only. Update blacklist count comments and assertions from seven to eight entries.

Change both dormant literals:

```tsx
{info.update_command || 'raven upgrade'}
```

```tsx
update_command: 'raven upgrade'
```

- [ ] **Step 4: Run TUI and RPC tests and commit**

Run:

```bash
uv run pytest tests/test_tui_rpc_cli_dispatch.py tests/test_tui_rpc_commands_catalog.py -q
npm test --prefix ui-tui -- src/__tests__/branding.test.tsx
npm run lint --prefix ui-tui
npm run type-check --prefix ui-tui
```

Expected: all commands exit zero.

Commit:

```bash
git add raven/tui_rpc/methods/cli_dispatch.py tests/test_tui_rpc_cli_dispatch.py ui-tui/src/components/branding.tsx ui-tui/src/demo/gallery.tsx ui-tui/src/__tests__/branding.test.tsx
git commit -m "fix(cli): keep upgrades outside active tui"
```

### Task 4: User documentation

**Files:**
- Modify: `README.md:52-90`
- Modify: `README.zh-CN.md:56-94`

**Interfaces:**
- Consumes: the final CLI behavior from Task 2.
- Produces: matching English and Chinese upgrade instructions.

- [ ] **Step 1: Add an existing-install upgrade section to both READMEs**

The English section must include these exact commands and boundaries:

```markdown
### Upgrade an existing installation

Check for the latest published stable release:

    raven upgrade --check

Upgrade Raven without resetting configuration, sessions, or memory:

    raven upgrade

Raven upgrades are user-triggered, not automatic. Editable source installs are
not overwritten; update the checkout and rerun its development setup instead.
```

Add the equivalent Chinese section with the same commands and meaning. Also add `raven upgrade --check` and `raven upgrade` to each useful-command table.

- [ ] **Step 2: Verify documentation scope and commit**

Run:

```bash
rg -n "raven upgrade" README.md README.zh-CN.md
git diff --check
PYTHONPATH=. uv run --extra dev python scripts/check_large_files.py origin/main
```

Expected: both READMEs contain check and upgrade guidance; checks exit zero.

Commit:

```bash
git add README.md README.zh-CN.md
git commit -m "docs: explain raven upgrade workflow"
```

### Task 5: Full verification and pull request

**Files:**
- Verify all files changed by Tasks 1-4.
- Create: `/tmp/raven-upgrade-pr.md` outside the repository.

**Interfaces:**
- Consumes: all implementation commits.
- Produces: pushed branch and draft pull request closing issue #111.

- [ ] **Step 1: Run the complete relevant verification matrix**

Run:

```bash
uv run pytest tests/test_cli_upgrade_commands.py tests/test_cli_smoke.py tests/test_tui_rpc_cli_dispatch.py tests/test_tui_rpc_commands_catalog.py -q
uv run pytest -q
uv run ruff check raven/cli/upgrade_commands.py raven/cli/commands.py raven/tui_rpc/methods/cli_dispatch.py tests/test_cli_upgrade_commands.py tests/test_cli_smoke.py tests/test_tui_rpc_cli_dispatch.py
uv run ruff format --check raven/cli/upgrade_commands.py raven/cli/commands.py raven/tui_rpc/methods/cli_dispatch.py tests/test_cli_upgrade_commands.py tests/test_cli_smoke.py tests/test_tui_rpc_cli_dispatch.py
npm test --prefix ui-tui
npm run lint --prefix ui-tui
npm run lint:rpc --prefix ui-tui
npm run type-check --prefix ui-tui
make check-large-files
```

Expected: every command exits zero with no failures.

- [ ] **Step 2: Rebase onto the latest main and rerun focused tests**

Run:

```bash
git fetch origin main
git merge-tree --write-tree HEAD origin/main
git rebase origin/main
uv run pytest tests/test_cli_upgrade_commands.py tests/test_cli_smoke.py tests/test_tui_rpc_cli_dispatch.py tests/test_tui_rpc_commands_catalog.py -q
npm test --prefix ui-tui -- src/__tests__/branding.test.tsx
```

Expected: the merge-tree and rebase are clean, then focused tests pass.

- [ ] **Step 3: Run repository message and title lint**

Run:

```bash
make check-commits
PR_TITLE='feat(cli): add raven upgrade command' make check-pr-title
```

Expected: both lint gates pass.

- [ ] **Step 4: Draft and validate the PR description**

Write `/tmp/raven-upgrade-pr.md` with the repository template and these facts:

```markdown
## Change description

> Add `raven upgrade --check` and `raven upgrade` using the latest published stable GitHub Release wheel. Protect editable and unsupported installs, preserve `~/.raven` state, keep self-upgrade outside the active TUI process, and document the user workflow.

Closes #111

## Type of change
- [ ] Bug fix
- [x] New feature
- [x] Document
- [ ] Others

## Related issues (if there is)

> Closes #111

## Checklists

### Development

- [x] Lint rules pass locally
- [x] Application changes have been tested thoroughly
- [x] Automated tests covering modified code pass

### Security

- [x] Security impact of change has been considered
- [x] Code follows security best practices and guidelines

### Code review

- [x] Pull request has a descriptive title and context useful to a reviewer. Screenshots or screencasts are attached as necessary
```

Validate:

```bash
if LC_ALL=C rg -n '[^\x00-\x7F]' /tmp/raven-upgrade-pr.md; then exit 1; fi
cat /tmp/raven-upgrade-pr.md
```

Expected: no non-ASCII matches and the full body matches verified work.

- [ ] **Step 5: Push and open the draft PR**

Run:

```bash
git push -u origin feat/raven_upgrade_command
gh pr create --repo EverMind-AI/Raven --base main --head feat/raven_upgrade_command --draft --title 'feat(cli): add raven upgrade command' --body-file /tmp/raven-upgrade-pr.md
```

Expected: GitHub prints the new draft PR URL.
