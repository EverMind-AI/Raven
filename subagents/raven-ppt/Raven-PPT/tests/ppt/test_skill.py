"""The skill's factual claims, checked against the code that has to back them.

A skill document is prose and prose drifts. The predecessor's carried eight rules
each restated four to six times, and by the end the copy that mattered was the one
nobody read; this one carries the craft and points at the code for the facts. What
that buys is only real if the facts stay true, so every checkable claim in it is
asserted here: the tools it names, the themes and faces it lists, the icon count,
the type floors, the environment variables, the helper functions, the script path,
and the split between what the build refuses and what it only reports.

The port itself was line-checked this way and five claims in the old document had
gone stale: `Calibri` as a safe face (it is not among the measured six), a
`good`/`crop`/`avoid` usability label that was deliberately removed, `append=true`
where the tool takes `mode="append"`, "the gates refuse two things" when seven kinds
now refuse, and a twelve-page render cap that batching replaced.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pptx")

CHECKER = Path(__file__).parent / "_skill_claims.py"


def test_every_checkable_claim_in_the_skill_still_holds() -> None:
    done = subprocess.run([sys.executable, str(CHECKER)], capture_output=True, text=True, check=False)
    report = done.stdout.strip() + done.stderr.strip()
    assert done.returncode == 0, report
    assert "0 failed" in report, report
    counted = int(report.split(" claims checked")[0].splitlines()[-1])
    assert counted >= 60, f"the checker stopped checking things: {report}"


def test_the_route_ships_the_skill_it_declares() -> None:
    """The field had no reader for the whole of the port, so all three routes named
    a skill that did not exist and nothing said so."""
    from raven.memory_engine.skill_local.registry import _DEFAULT_BUILTIN_SKILLS_DIR
    from raven.ppt.profiles import registry

    profile = registry.get(registry.DEFAULT)
    assert profile.skill
    assert (Path(_DEFAULT_BUILTIN_SKILLS_DIR) / profile.skill / "SKILL.md").is_file()


def test_the_skill_is_the_one_always_on_skill() -> None:
    """It reaches the prompt through the catalogue's own `always` flag rather than
    anything in raven/ppt/, and it must not be crowded out by another."""
    from raven.memory_engine.skill_forge.catalog import LocalSkillCatalog
    from raven.ppt.profiles import registry

    catalog = LocalSkillCatalog(Path("/tmp/raven-skill-check"), start_watcher=False)

    assert [m.name for m in catalog.get_always_skills()] == [registry.get(registry.DEFAULT).skill]
