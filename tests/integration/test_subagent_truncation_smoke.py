"""A long real answer crossing the process boundary, at the shipped cap.

Real child process, real agent table built from real config, real record
directory, the real default ``max_output_chars``. The only stub is the provider,
which a CLI backend never calls -- it shells out to an agent that picks its own
model.

The unit tests each drive one seam with a cap small enough to keep the fixture
readable (2000 chars, sometimes 6). None of them answers the question this one
does: whether the shipped default cap, against an answer of the size that
actually provoked the defect, still leaves the whole answer recoverable. The
numbers here are the defect's own shape -- roughly 92000 characters returned
through a 30000-character cap, with the decisive sentence last, where a bare
slice put it out of reach of the caller, the user, and the record all at once.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from raven.agent.subagent.manager import SubagentManager
from raven.config.schema import ThirdPartyCliSubagentConfig

_FINDINGS = 2300
_VERDICT = "FINAL VERDICT: the decisive fact is the last thing said"


class _UncalledProvider:
    """Stands in for the binding a spawn resolves; a CLI backend never asks it."""

    def get_default_model(self) -> str:
        return "stub"


def _reporter(home: Path) -> Path:
    script = home / "reporter.py"
    script.write_text(
        "import sys\n"
        f"for n in range({_FINDINGS}):\n"
        "    sys.stdout.write(f'finding-{n:04d}: a paragraph of the report\\n')\n"
        f"sys.stdout.write({_VERDICT!r} + '\\n')\n",
        encoding="utf-8",
    )
    return script


async def test_the_shipped_cap_returns_a_capped_reply_and_records_the_whole_answer(tmp_path: Path) -> None:
    cfg = ThirdPartyCliSubagentConfig(name="Reporter", command=f"{sys.executable} {_reporter(tmp_path)}")
    cap = cfg.max_output_chars

    mgr = SubagentManager(
        provider=_UncalledProvider(),
        workspace=tmp_path,
        session_dir=lambda key: tmp_path / "sessions" / key.replace(":", "/"),
    )
    mgr.apply_agents([cfg])
    announced: list = []
    mgr.set_submit(announced.append)

    await mgr.spawn(task="write the long report", agent="Reporter", session_key="cli:smoke")
    await asyncio.gather(*list(mgr._running_tasks.values()))

    assert len(announced) == 1, "one run, one announce"
    text = announced[0].text
    record = Path(text.rsplit("Record: ", 1)[1].splitlines()[0].strip())
    out = (record / "out.md").read_text(encoding="utf-8")
    meta = json.loads((record / "meta.json").read_text(encoding="utf-8"))

    # The answer was far larger than the cap, so this is the real case and not a
    # fixture that happened to fit.
    assert len(out) > 2 * cap
    # What the caller was handed respects the cap and says why it stops.
    assert "[raven] Output truncated" in text
    assert f"of {len(out)} characters" in text
    assert _VERDICT not in text, "the caller is handed the head of the answer, not its end"
    # What the record holds is the answer, ending where the sub-agent ended.
    assert out.splitlines()[-1] == _VERDICT
    assert out.count("finding-") == _FINDINGS
    # And the loss is stated in numbers a later reader can check against the file.
    assert meta["status"] == "completed"
    assert meta["output_truncated"] is True
    assert meta["output_chars_total"] == len(out)
    assert meta["output_chars_returned"] <= cap
    assert meta["output_chars_discarded"] == len(out) - meta["output_chars_returned"]
    assert meta["output_truncation_reason"] == "max_output_chars"


async def test_a_report_that_fits_the_shipped_cap_is_recorded_with_no_truncation_claim(tmp_path: Path) -> None:
    """The same path, under the cap: no notice, no counters, nothing to recover.

    Here so the assertions above are read as a report of a real loss rather than
    a disclaimer this path attaches to every long-ish answer.
    """
    script = tmp_path / "brief.py"
    script.write_text("print('the whole answer fits')\n", encoding="utf-8")
    cfg = ThirdPartyCliSubagentConfig(name="Brief", command=f"{sys.executable} {script}")

    mgr = SubagentManager(
        provider=_UncalledProvider(),
        workspace=tmp_path,
        session_dir=lambda key: tmp_path / "sessions" / key.replace(":", "/"),
    )
    mgr.apply_agents([cfg])
    announced: list = []
    mgr.set_submit(announced.append)

    await mgr.spawn(task="answer briefly", agent="Brief", session_key="cli:smoke")
    await asyncio.gather(*list(mgr._running_tasks.values()))

    text = announced[0].text
    record = Path(text.rsplit("Record: ", 1)[1].splitlines()[0].strip())
    meta = json.loads((record / "meta.json").read_text(encoding="utf-8"))

    assert "the whole answer fits" in text
    assert "[raven] Output truncated" not in text
    assert (record / "out.md").read_text(encoding="utf-8").strip() == "the whole answer fits"
    assert "output_truncated" not in meta
