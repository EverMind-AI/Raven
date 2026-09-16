"""Dependabot's commit prefixes stay inside the scope enum that lints them.

Two files have to agree and neither imports the other: `.github/dependabot.yml`
decides what a dependency-bump commit is called, and `commitlint.config.cjs`
decides which names are legal. They drifted apart once already. A
`chore(deps-dev)` bump merged on 2026-06-30 (#24); the scope enum became
enforced on 2026-09-05 carrying a literal `deps` and no sibling; from then on
every dependabot dev-dependency pull request failed a gate for using a scope
the repo had already accepted, and nothing caught it until a person read a CI
log months later.

The other half is coverage. dependabot.yml only configures the trees listed in
it, while security updates reach every manifest through the dependency graph --
so a tree missing from this file still gets pull requests, just with none of
the labels, grouping or commit-message settings. Two npm trees had been in that
state since they were created.

Both directions are pinned here: every scope the config will emit is legal,
scopes it will never emit are still rejected (an enum that accepted everything
would satisfy the first assertion), and every lockfile in the tree is
configured.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DEPENDABOT = REPO / ".github" / "dependabot.yml"

# `include: "scope"` is the documented switch that appends the dependency type
# to the prefix, and these two values are the whole of what it can produce.
DEPENDABOT_SCOPES = ("deps", "deps-dev")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="the scope enum is a node module and only node can evaluate it",
)


def _updates() -> list[dict]:
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    updates = config["updates"]
    assert updates, "dependabot.yml lost its updates list"
    return updates


def _commitlint_rule(name: str) -> list[str]:
    script = f"process.stdout.write(JSON.stringify(require('./commitlint.config.cjs').rules['{name}'][2]))"
    dumped = subprocess.run(
        ["node", "-e", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    values = json.loads(dumped)
    # Most of the enum is computed from the tree at load time, so an extraction
    # that quietly returned the wrong thing would satisfy every membership
    # assertion below. Anchor it on an entry that cannot move.
    anchor = {"scope-enum": "cli", "type-enum": "feat"}[name]
    assert anchor in values, f"{name} did not come through node: {values[:5]}"
    return values


def _directory(lockfile: str) -> str:
    parent = Path(lockfile).parent
    return "/" if parent == Path(".") else "/" + parent.as_posix()


def test_every_ecosystem_pins_the_prefix_instead_of_letting_dependabot_guess() -> None:
    types = _commitlint_rule("type-enum")

    for entry in _updates():
        where = f"{entry['package-ecosystem']} {entry['directory']}"
        commit_message = entry.get("commit-message")

        assert commit_message is not None, f"{where} lets dependabot infer its own prefix from history"
        assert commit_message.get("include") == "scope", f"{where} does not ask for the dependency-type scope"
        assert commit_message.get("prefix") in types, f"{where} pins a type the commit lint rejects"


def test_the_ecosystems_agree_on_one_prefix() -> None:
    # Pinning each ecosystem separately still leaves the log split if they are
    # pinned to different types, which is the state this replaced: the merged
    # bumps say chore(deps), the open ones say build(deps).
    # Whether an entry is pinned at all is the test above; this one only asks
    # whether the pinned ones agree, so a missing block fails in one place.
    prefixes = {block["prefix"] for entry in _updates() if (block := entry.get("commit-message"))}

    assert len(prefixes) == 1, f"dependency bumps land under more than one type: {sorted(prefixes)}"


def test_every_scope_dependabot_emits_is_legal() -> None:
    scopes = _commitlint_rule("scope-enum")

    missing = [scope for scope in DEPENDABOT_SCOPES if scope not in scopes]
    assert not missing, f"dependabot will emit scopes the enum rejects: {missing}"


def test_the_enum_still_rejects_scopes_nobody_declared() -> None:
    scopes = _commitlint_rule("scope-enum")

    for bogus in ("deps-devv", "dependencies", "nonsense-scope"):
        assert bogus not in scopes, f"the scope enum accepts {bogus!r}, so it is not gating anything"


def test_the_workflows_tree_has_an_ecosystem_too() -> None:
    workflows = subprocess.run(
        ["git", "ls-files", ".github/workflows"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert workflows, "git ls-files found no workflows at all"

    configured = {(entry["package-ecosystem"], entry["directory"]) for entry in _updates()}

    assert ("github-actions", "/") in configured, "the workflows get action bumps that nothing here configures"


def test_every_dependency_manifest_in_the_tree_is_configured() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "*package-lock.json", "uv.lock"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tracked, "git ls-files found no lockfiles at all"

    on_disk = {
        "npm": {_directory(path) for path in tracked if path.endswith("package-lock.json")},
        "uv": {_directory(path) for path in tracked if path.endswith("uv.lock")},
    }
    configured: dict[str, set[str]] = {"npm": set(), "uv": set()}
    for entry in _updates():
        ecosystem = entry["package-ecosystem"]
        if ecosystem in configured:
            configured[ecosystem].add(entry["directory"])

    for ecosystem, directories in on_disk.items():
        assert configured[ecosystem] == directories, (
            f"{ecosystem} lockfile with no dependabot entry: {sorted(directories - configured[ecosystem])}; "
            f"dependabot entry with no {ecosystem} lockfile: {sorted(configured[ecosystem] - directories)}"
        )
