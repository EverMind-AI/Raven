"""Tests for the case read/edit tools.

These tools shipped with no tests at all. The gap surfaced when a real round
showed why the trial requirement mattered: a campaign has no trials until
something is submitted, so an agent that wanted to look at the case it was about
to run had no sanctioned way to do it, went out over raw ssh, timed out on the
default port, and submitted the job anyway.

So the reads here are: a trial's case still works; the campaign's staged case
works with no trial; and neither path lets a caller out of the case directory.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.tools.ops_case_dict import OpsEditCaseDictTool, OpsReadCaseDictTool

STAGED = "/remote/staged_case"


class _FakeBackend:
    """Records the shell commands a tool would run, and answers them."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.edited = False
        self.files = {
            f"{STAGED}/constant/transportProperties": "water { nu 0.001; }",
            "/remote/jobs/run1/case/system/controlDict": "endTime 1;",
        }

    def _job_dir(self, key: str) -> str:
        return f"/remote/jobs/{key}"

    def _run(self, cmd: str):
        self.commands.append(cmd)
        for path, body in self.files.items():
            if path in cmd and cmd.startswith("cat "):
                return 0, body
        if cmd.startswith("sha256sum"):
            # Different after a set: the tool compares the hash before and after to
            # tell a real edit from one the solver silently ignored, so a fake that
            # always returns the same digest exercises only the ignored branch.
            return 0, ("cafe1234  x\n" if self.edited else "deadbeef  x\n")
        if "-value" in cmd:
            return 0, "0.001\n"
        if "-set" in cmd:
            self.edited = True
            return 0, ""
        return 1, "no such thing"


def _campaign(tmp_path, with_staged: bool, trials=("run1",)):
    cdir = tmp_path / "camp"
    cdir.mkdir()
    meta = {"backend": "fake", "remote_dir": "/remote"}
    if with_staged:
        meta["staged_case"] = STAGED
    (cdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    records = {
        k: {
            "idem_key": k,
            "status": "pending",
            "campaign": "c",
            "handle": None,
            "result": None,
            "attempts": 0,
            "escalated": False,
        }
        for k in trials
    }
    (cdir / "ledger.json").write_text(json.dumps({"version": 1, "records": records}), encoding="utf-8")
    return cdir


@pytest.fixture
def patched(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: backend)
    return backend


@pytest.mark.asyncio
async def test_reads_the_staged_case_with_no_trial(tmp_path, patched):
    """The point of the change: before a first submit there is no trial, and the
    case still has to be readable."""
    cdir = _campaign(tmp_path, with_staged=True, trials=())

    out = await OpsReadCaseDictTool().execute(
        campaign="c", path="constant/transportProperties", ledger=str(cdir / "ledger.json")
    )

    assert "nu 0.001" in out
    assert f"cat {STAGED}/constant/transportProperties" in " ".join(patched.commands)


@pytest.mark.asyncio
async def test_a_trial_still_reads_that_trials_own_case(tmp_path, patched):
    cdir = _campaign(tmp_path, with_staged=True)

    out = await OpsReadCaseDictTool().execute(
        campaign="c", trial="run1", path="system/controlDict", ledger=str(cdir / "ledger.json")
    )

    assert "endTime 1" in out
    assert "/remote/jobs/run1/case/system/controlDict" in " ".join(patched.commands)


@pytest.mark.asyncio
async def test_no_trial_and_no_staged_case_says_so(tmp_path, patched):
    """Silence would be the wrong answer here: a campaign that declares no staged
    case cannot serve this call, and the caller needs to know which of the two
    things is missing."""
    cdir = _campaign(tmp_path, with_staged=False, trials=())

    out = await OpsReadCaseDictTool().execute(
        campaign="c", path="constant/transportProperties", ledger=str(cdir / "ledger.json")
    )

    assert "staged_case" in out


@pytest.mark.asyncio
async def test_unknown_trial_names_the_ones_that_exist(tmp_path, patched):
    cdir = _campaign(tmp_path, with_staged=True)

    out = await OpsReadCaseDictTool().execute(
        campaign="c", trial="nope", path="system/controlDict", ledger=str(cdir / "ledger.json")
    )

    assert "Unknown trial" in out and "run1" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../outside", "/etc/passwd/../../x"])
async def test_paths_may_not_escape_the_case(tmp_path, patched, bad):
    cdir = _campaign(tmp_path, with_staged=True, trials=())

    out = await OpsReadCaseDictTool().execute(campaign="c", path=bad, ledger=str(cdir / "ledger.json"))

    assert "Refusing path" in out


@pytest.mark.asyncio
async def test_editing_the_staged_case_records_which_case_it_was(tmp_path, patched):
    """The event log is the only place the three pre-registered readings come from,
    so an edit with no trial must still say what it touched."""
    cdir = _campaign(tmp_path, with_staged=True, trials=())

    out = await OpsEditCaseDictTool().execute(
        campaign="c",
        path="constant/transportProperties",
        entry="water/nu",
        value="1e-06",
        reason="test",
        ledger=str(cdir / "ledger.json"),
    )

    events = [json.loads(line) for line in (cdir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    edits = [e for e in events if e.get("kind") == "edit_case_dict"]
    assert len(edits) == 1
    assert edits[0]["case"] == "staged"
    assert edits[0]["trial"] is None
    assert "next trial submitted" in out


@pytest.mark.asyncio
async def test_editing_a_trial_still_tells_you_to_restart_it(tmp_path, patched):
    cdir = _campaign(tmp_path, with_staged=True)

    out = await OpsEditCaseDictTool().execute(
        campaign="c",
        trial="run1",
        path="system/controlDict",
        entry="endTime",
        value="2",
        ledger=str(cdir / "ledger.json"),
    )

    assert "Restart trial run1" in out


def test_descriptions_do_not_name_a_knob_to_look_at():
    """Which entries matter is what the round measures. The examples in these
    descriptions illustrate syntax only."""
    for tool in (OpsReadCaseDictTool(), OpsEditCaseDictTool()):
        text = tool.description.lower()
        for word in ("deltat", "residualcontrol", "nu ", "viscosity", "alpha", "courant"):
            assert word not in text, f"{tool.name} description names {word!r}"


# --- ops_case_changes: 差异本身,不是判断 -----------------------------------


class _DiffBackend(_FakeBackend):
    """Answers the diff command with a canned unified diff."""

    def __init__(self, body: str = "") -> None:
        super().__init__()
        self.body = body

    def _run(self, cmd: str):
        self.commands.append(cmd)
        if "foamDictionary" in cmd or cmd.startswith("diff ") or cmd.startswith("bash -c"):
            return (1 if self.body else 0), self.body
        return super()._run(cmd)


REF = "/remote/reference_case"
DIFF = (
    "--- /remote/reference_case/constant/transportProperties\n"
    "+++ /remote/staged_case/constant/transportProperties\n"
    "@@ -20 +20 @@\n"
    "-    nu              1e-06;\n"
    "+    nu              0.001;\n"
)


def _with_reference(tmp_path, body: str, monkeypatch):
    cdir = _campaign(tmp_path, with_staged=True, trials=())
    meta = json.loads((cdir / "meta.json").read_text(encoding="utf-8"))
    meta["reference_case"] = REF
    (cdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    backend = _DiffBackend(body)
    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: backend)
    return cdir, backend


@pytest.mark.asyncio
async def test_it_reports_the_one_entry_that_moved(tmp_path, monkeypatch):
    """Seven watch rounds read the settings one at a time and none noticed the
    single altered entry. The point of this tool is that the alteration is one
    line rather than one value among eighty."""
    from raven.agent.tools.ops_case_dict import OpsCaseChangesTool

    cdir, backend = _with_reference(tmp_path, DIFF, monkeypatch)

    out = await OpsCaseChangesTool().execute(campaign="c", ledger=str(cdir / "ledger.json"))

    assert "nu              1e-06" in out and "nu              0.001" in out
    assert REF in out
    cmd = next(c for c in backend.commands if "foamDictionary" in c)
    assert "find system constant" in cmd, "only dictionaries: time dirs and logs are outputs"
    assert cmd.count("foamDictionary") >= 2, "both sides must be normalised by the same printer"


@pytest.mark.asyncio
async def test_an_identical_case_says_so_rather_than_printing_nothing(tmp_path, monkeypatch):
    """Empty output would read the same as a broken comparison."""
    from raven.agent.tools.ops_case_dict import OpsCaseChangesTool

    cdir, _ = _with_reference(tmp_path, "", monkeypatch)

    out = await OpsCaseChangesTool().execute(campaign="c", ledger=str(cdir / "ledger.json"))

    assert "identical to the reference" in out


@pytest.mark.asyncio
async def test_no_reference_declared_says_which_thing_is_missing(tmp_path, patched):
    from raven.agent.tools.ops_case_dict import OpsCaseChangesTool

    cdir = _campaign(tmp_path, with_staged=True, trials=())

    out = await OpsCaseChangesTool().execute(campaign="c", ledger=str(cdir / "ledger.json"))

    assert "reference_case" in out


def test_the_description_reports_a_difference_not_a_verdict():
    """A deliberate change and a mistake look the same here; saying which is which
    is the agent's job, and a tool that pre-judged it would be answering the very
    question the round measures."""
    from raven.agent.tools.ops_case_dict import OpsCaseChangesTool

    text = OpsCaseChangesTool().description.lower()
    for word in ("wrong", "incorrect", "suspicious", "error", "should", "nu ", "viscosity"):
        assert word not in text
    assert "nothing about whether any of them is right" in text


# ---- round zero: no ledger yet ----


def _campaign_without_ledger(tmp_path):
    """A campaign as it exists before the first submit: meta on disk, no ledger."""
    cdir = tmp_path / "camp0"
    cdir.mkdir()
    (cdir / "meta.json").write_text(
        json.dumps({"backend": "fake", "remote_dir": "/remote", "staged_case": STAGED}),
        encoding="utf-8",
    )
    return cdir


@pytest.mark.asyncio
async def test_the_staged_case_reads_before_anything_has_been_submitted(tmp_path, patched):
    """The ledger records what has been submitted, so it does not exist in round
    zero -- and round zero is the round these tools are most needed for. The rule
    these campaigns carry first is "check the magnitudes before submitting", and
    the case it means is the staged one, which the ledger says nothing about.

    Measured 2026-08-11: three reads refused with "no campaign state", then
    fourteen ssh calls to read the case by hand and a sed to change it, leaving
    the change recorded nowhere. The same failure is written in _case_root's own
    docstring from the round that made the trial argument optional -- the argument
    was freed and the gate above it was not.
    """
    cdir = _campaign_without_ledger(tmp_path)
    assert not (cdir / "ledger.json").exists()

    out = await OpsReadCaseDictTool().execute(
        campaign="c", ledger=str(cdir / "ledger.json"), path="constant/transportProperties"
    )

    assert "need ledger.json" not in out and "No campaign meta" not in out
    assert STAGED in out or "nu" in out


@pytest.mark.asyncio
async def test_a_named_trial_still_says_it_is_unknown_rather_than_blaming_the_campaign(tmp_path, patched):
    """Naming a trial before anything ran is a different mistake from the campaign
    having no state, and it gets the sentence that names the trial."""
    cdir = _campaign_without_ledger(tmp_path)

    out = await OpsReadCaseDictTool().execute(
        campaign="c", ledger=str(cdir / "ledger.json"), path="constant/g", trial="t1"
    )

    assert "Unknown trial" in out


@pytest.mark.asyncio
async def test_a_campaign_with_no_meta_at_all_still_says_so(tmp_path, patched):
    cdir = tmp_path / "empty"
    cdir.mkdir()

    out = await OpsReadCaseDictTool().execute(campaign="c", ledger=str(cdir / "ledger.json"), path="constant/g")

    assert "need meta.json" in out
