"""A constraints export must drop every workspace member, not only the root.

``uv tool install -c`` rejects unnamed requirements, and ``uv export
--no-emit-project`` keeps workspace members in the output as unnamed
editables (``-e ./plugins-dist/...``), so every installer that exports the
lock into a constraints file broke the moment ``plugins-dist/*`` joined the
workspace. The trunk's export sites therefore use ``--no-emit-workspace``,
which drops the members along with the root.

The vendored forks under ``subagents/`` still say ``--no-emit-project``,
and that is correct there today: none of them declares
``[tool.uv.workspace]``, so the root project is the only editable their
export could emit. This contract is the tripwire for the day that changes:
any export site whose owning pyproject declares a workspace must use
``--no-emit-workspace``. It lives here rather than in
``check_vendored_invariants.py`` because the forks do not carry a fix to
keep -- their flag is right until a workspace appears -- and because the
sites span shell, PowerShell, YAML, and Python, which that script's
AST-shaped invariants cannot read.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

SCANNED_SUFFIXES = {".sh", ".ps1", ".yml", ".yaml", ".py"}

# The trunk's own export sites. Named so a rename fails the scan instead of
# silently leaving the renamed site unchecked: a contract that stops seeing
# its subject keeps passing while describing nothing.
TRUNK_SITES = (
    "install.sh",
    "install.ps1",
    ".github/workflows/release.yml",
    "scripts/publish_beta.py",
)


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO / name for name in out.split("\0") if name]


def _export_flag_lines() -> list[tuple[Path, int, str]]:
    """Every tracked line carrying a member-emission flag, with its location."""
    hits: list[tuple[Path, int, str]] = []
    for path in _tracked_files():
        if path.suffix not in SCANNED_SUFFIXES or path.resolve() == SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if "--no-emit-project" in line or "--no-emit-workspace" in line:
                hits.append((path, lineno, line))
    return hits


def _declares_workspace(project_root: Path) -> bool:
    data = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    return "workspace" in data.get("tool", {}).get("uv", {})


def _owning_project(path: Path) -> Path | None:
    """Nearest ancestor with a pyproject.toml, within the repo."""
    for parent in path.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
        if parent == REPO:
            return None
    return None


def test_workspace_projects_export_with_no_emit_workspace() -> None:
    offenders = []
    for path, lineno, line in _export_flag_lines():
        if "--no-emit-project" not in line:
            continue
        owner = _owning_project(path)
        if owner is not None and _declares_workspace(owner):
            offenders.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not offenders, (
        "These constraints exports use --no-emit-project inside a project that "
        "declares [tool.uv.workspace], so workspace members land in the output "
        "as unnamed editables and `uv tool install -c` rejects the file. "
        f"Use --no-emit-workspace: {offenders}"
    )


def test_trunk_export_sites_are_seen_and_fixed() -> None:
    hits = {path.relative_to(REPO).as_posix(): line for path, _, line in _export_flag_lines()}
    missing = [site for site in TRUNK_SITES if site not in hits]
    assert not missing, (
        f"Export sites this contract expects to watch are gone: {missing}. "
        "If the export moved, point TRUNK_SITES at its new home."
    )
    assert _declares_workspace(REPO), (
        "The trunk no longer declares [tool.uv.workspace]; this contract's "
        "premise moved, rewrite it to describe where the guarantee lives now."
    )
    wrong = [site for site in TRUNK_SITES if "--no-emit-workspace" not in hits[site]]
    assert not wrong, f"Trunk export sites regressed to --no-emit-project: {wrong}"


# The vendored trees that pin installs to an exported constraints file today.
# raven-research is absent on purpose: its installer (install.py) never
# exports constraints, so there is nothing there for the tripwire to watch.
# A retired fork comes off this list in the commit that removes its tree.
FORK_TREES = (
    "subagents/raven-code/Raven-main",
    "subagents/raven-design/Raven-Design",
    "subagents/raven-oncall/Raven-Oncall",
    "subagents/raven-ppt/Raven-PPT",
)
FORK_SITE_FILES = ("install.sh", "install.ps1", ".github/workflows/release.yml")


def test_scan_sees_every_fork_export_site() -> None:
    seen = {path.relative_to(REPO).as_posix() for path, _, _ in _export_flag_lines()}
    unseen = [f"{tree}/{name}" for tree in FORK_TREES for name in FORK_SITE_FILES if f"{tree}/{name}" not in seen]
    assert not unseen, (
        f"Fork export sites the scan never saw: {unseen}. The site moved or "
        "lost its member-emission flag; the workspace tripwire is blind there "
        "until the scan sees it again."
    )
