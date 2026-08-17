"""What the campaign was set up with, checked against what it is now.

Measured 2026-08-12, three times in one day, and it is one shape:

  * an arm read its own meta.json, decided ``staged_case`` disagreed with the task
    text, and rewrote ``remote_dir`` / ``staged_case`` / ``command`` with
    ``edit_file``. The job then ran out of a different directory against a shared
    baseline case, and a budget guard watching the declared path watched nothing.
  * another arm decided the case's initial water column was mis-specified and
    rewrote setFieldsDict. The next job "reached endTime" in 51 seconds with a
    third of the water -- a different problem, reported as usable.
  * a third cut the case's endTime in half because it had mis-read the budget.

None of the three was concealment: each edit was recorded, each carried a reason,
and each reason was coherent on its own premises. What they share is that **when
finishing got hard, what changed was the definition of the task rather than the
approach to it** -- and in every case the arm believed it was fixing an error, not
redefining anything.

So this does not try to decide which edits are legitimate. Deciding that requires
knowing the domain, and a check that knows the domain is a check that must be
rewritten for every new one. It records instead:

  * **meta.json is the apparatus' own declaration** -- targets, budget, which case,
    which host. An agent editing it is out of role no matter what the domain is,
    and the damage is not only to scoring: the run itself goes somewhere else.
    A changed meta REFUSES the next submit, and points at ops_ask_owner.
  * **the case is the agent's to edit** -- fixing a wrong viscosity is the job.
    Case edits are recorded and shown, never refused. Whether a particular edit
    amounted to changing the question is a judgement for that task's
    pre-registration, not for this layer.

The baseline is captured on the first submit, because that is the last moment the
apparatus is known to be as the operator left it.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.ops.apparatus import (
    baseline_of,
    compare,
    load_baseline,
    save_baseline,
)


def _meta(tmp_path: Path, **over) -> Path:
    cdir = tmp_path / "c"
    cdir.mkdir(exist_ok=True)
    meta = {
        "backend": "process",
        "host": "h",
        "command": "run {config} {job_dir}",
        "staged_case": "/remote/case",
        "budget": {"unit": "core-minute", "total": 150},
    }
    meta.update(over)
    (cdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return cdir


class _Runner:
    """Stands in for the backend's remote shell. Serves a fixed file tree."""

    def __init__(self, tree: dict[str, str] | None):
        self.tree = tree
        self.calls: list[str] = []

    def __call__(self, cmd: str):
        self.calls.append(cmd)
        if self.tree is None:
            return 1, ""
        return 0, "".join(f"{sha}  {path}\n" for path, sha in sorted(self.tree.items()))


def test_a_baseline_names_the_meta_and_every_case_file(tmp_path):
    cdir = _meta(tmp_path)
    runner = _Runner({"/remote/case/system/controlDict": "aaa", "/remote/case/constant/transportProperties": "bbb"})
    b = baseline_of(cdir, runner)

    assert b["meta_sha"], "the declaration itself must be fingerprinted"
    assert b["case"] == {"/remote/case/system/controlDict": "aaa", "/remote/case/constant/transportProperties": "bbb"}


def test_an_unchanged_apparatus_compares_clean(tmp_path):
    cdir = _meta(tmp_path)
    runner = _Runner({"/remote/case/system/controlDict": "aaa"})
    save_baseline(cdir, baseline_of(cdir, runner))
    d = compare(load_baseline(cdir), baseline_of(cdir, runner))
    assert d.meta_changed is False
    assert d.case_changed == [] and d.case_added == [] and d.case_removed == []
    assert d.is_clean


def test_an_edited_meta_is_reported_as_changed(tmp_path):
    cdir = _meta(tmp_path)
    runner = _Runner({"/remote/case/system/controlDict": "aaa"})
    save_baseline(cdir, baseline_of(cdir, runner))

    m = json.loads((cdir / "meta.json").read_text())
    m["staged_case"] = "/remote/other"  # what the B2 arm did
    (cdir / "meta.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    d = compare(load_baseline(cdir), baseline_of(cdir, _Runner({"/remote/other/x": "ccc"})))
    assert d.meta_changed is True
    assert not d.is_clean


def test_an_edited_case_file_is_reported_but_kept_separate_from_meta(tmp_path):
    """Editing the case is the job. It is recorded, not refused -- and it must not
    be conflated with editing the declaration."""
    cdir = _meta(tmp_path)
    save_baseline(
        cdir,
        baseline_of(
            cdir, _Runner({"/remote/case/system/setFieldsDict": "aaa", "/remote/case/system/controlDict": "bbb"})
        ),
    )

    d = compare(
        load_baseline(cdir),
        baseline_of(
            cdir,
            _Runner(
                {
                    "/remote/case/system/setFieldsDict": "REWRITTEN",  # what the leg A arm did
                    "/remote/case/system/controlDict": "bbb",
                }
            ),
        ),
    )

    assert d.meta_changed is False
    assert d.case_changed == ["/remote/case/system/setFieldsDict"]
    assert not d.is_clean


def test_added_and_removed_case_files_are_named(tmp_path):
    cdir = _meta(tmp_path)
    save_baseline(cdir, baseline_of(cdir, _Runner({"/remote/case/a": "1", "/remote/case/b": "2"})))
    d = compare(load_baseline(cdir), baseline_of(cdir, _Runner({"/remote/case/a": "1", "/remote/case/c": "3"})))
    assert d.case_removed == ["/remote/case/b"]
    assert d.case_added == ["/remote/case/c"]


def test_a_campaign_with_no_staged_case_still_fingerprints_its_meta(tmp_path):
    """Most domains have no staged case -- the ML line submits configs instead.
    The declaration still has to be watched."""
    cdir = _meta(tmp_path, staged_case=None)
    b = baseline_of(cdir, _Runner(None))
    assert b["meta_sha"]
    assert b["case"] == {}
    assert compare(b, baseline_of(cdir, _Runner(None))).is_clean


def test_an_unreachable_host_does_not_read_as_a_wiped_case(tmp_path):
    """A runner that cannot answer must not make every file look deleted -- that
    would refuse a submit for a network blip."""
    cdir = _meta(tmp_path)
    save_baseline(cdir, baseline_of(cdir, _Runner({"/remote/case/a": "1"})))
    b_now = baseline_of(cdir, _Runner(None))
    assert b_now["case_readable"] is False
    d = compare(load_baseline(cdir), b_now)
    assert d.case_removed == [], "unreadable is not removed"
    assert d.case_unreadable is True


def test_no_baseline_yet_compares_as_clean(tmp_path):
    """The first submit is where the baseline is taken; there is nothing to
    compare against before it, and that is not a drift."""
    cdir = _meta(tmp_path)
    assert load_baseline(cdir) is None
    d = compare(None, baseline_of(cdir, _Runner({"/remote/case/a": "1"})))
    assert d.is_clean and d.meta_changed is False
