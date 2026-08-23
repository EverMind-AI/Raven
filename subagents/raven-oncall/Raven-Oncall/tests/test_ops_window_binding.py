"""Two windows, two experiments, no ``--config``.

The way anyone would actually use this: open a couple of TUI windows and have
each watch a different experiment. Asked for on 2026-08-13. Two things stopped
it, and both come from the same omission -- a window's identity exists but is
not carried anywhere.

**Which experiment is this window's.** With no name given, the campaign was
guessed by counting directories under the ops home: exactly one meant that one,
anything else meant "say which one". Two live campaigns is the normal state of
the two-window case, so both windows got the error. The window's session key is
already minted per window (``tui:<id>`` from ``session.create``; ``tui:default``
is legacy in ``system.hello``) and is already handed to the spawn and
deep_research tools at the same call site -- ops just never received it. So the
binding is written on the first submit and read on every unnamed call after, and
the directory count stays as the fallback for a window that never submitted.

**Whose alarm this is.** A wake job recorded only ``channel`` and ``to``, both
``tui``/``default`` for every window, and a service claims by channel -- so
either window could claim either campaign's wake, and the turn would run in a
window holding the other experiment's conversation. It would look like it
worked.

Handing the wake to another window is fine when the owner is gone: the campaign
lives on disk and a wake turn is a cold start that reads it, so a fresh window
serves it with everything. What is not fine is waiting half an hour for that --
see ``test_cron_service_dead_claim.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops import OpsSubmitTool, _resolve_campaign_dir
from raven.ops.window import bind_window, campaign_for_window


class _Job:
    def __init__(self, i): self.id = str(i)


class _FakeCron:
    def __init__(self): self.jobs = []
    def add_job(self, **kw): self.jobs.append(kw); return _Job(len(self.jobs))
    def list_jobs(self, **kw):
        from types import SimpleNamespace
        return [SimpleNamespace(id=str(i), name=j["name"],
                                payload=SimpleNamespace(campaign=j.get("campaign")))
                for i, j in enumerate(self.jobs)]
    def remove_job(self, job_id): del self.jobs[int(job_id)]; return True


def _home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "ops"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr("raven.agent.tools.ops._ops_home", lambda: home)
    return home


def _campaign(home: Path, name: str, *, concluded: bool = False) -> Path:
    d = home / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({
        "backend": "process", "host": "h", "port": 22, "key": "~/.ssh/id_rsa",
        "command": "run {config} {job_dir}",
        "budget": {"unit": "gpu-minute", "total": 60, "overlap": "additive"},
    }), encoding="utf-8")
    if concluded:
        (d / "concluded.json").write_text(
            json.dumps({"concluded_at": "2026-08-13T17:25:26", "outcome": "done"}),
            encoding="utf-8")
    return d


def _install(monkeypatch):
    from types import SimpleNamespace

    class _Backend:
        _run = staticmethod(lambda cmd: (0, ""))
        async def spent_minutes(self): return 0.0
        async def remaining_minutes(self): return 60.0
        def unmeasured_spend(self): return {}
        async def submit(self, spec):
            return SimpleNamespace(backend="process", job_id=f"ops-{spec.idem_key}")
        async def poll(self, handle):
            from raven.ops import JobStatus
            return JobStatus.RUNNING

    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Backend())
    monkeypatch.setattr("raven.ops.prepare_from_meta", lambda *a, **k: None, raising=False)


# ------------------------------------------------------------------ the store

def test_a_window_remembers_what_it_is_working_on(tmp_path):
    bind_window(tmp_path, "tui:a1b2c3", "cfd-dambreak")
    assert campaign_for_window(tmp_path, "tui:a1b2c3") == "cfd-dambreak"


def test_two_windows_are_kept_apart(tmp_path):
    bind_window(tmp_path, "tui:a1b2c3", "cfd-dambreak")
    bind_window(tmp_path, "tui:d4e5f6", "embedding-tune")
    assert campaign_for_window(tmp_path, "tui:a1b2c3") == "cfd-dambreak"
    assert campaign_for_window(tmp_path, "tui:d4e5f6") == "embedding-tune"


def test_an_unknown_window_has_no_answer(tmp_path):
    assert campaign_for_window(tmp_path, "tui:nobody") is None


def test_rebinding_moves_the_window_to_its_new_work(tmp_path):
    """One window may finish one experiment and start another."""
    bind_window(tmp_path, "tui:a1b2c3", "cfd-dambreak")
    bind_window(tmp_path, "tui:a1b2c3", "embedding-tune")
    assert campaign_for_window(tmp_path, "tui:a1b2c3") == "embedding-tune"


def test_a_corrupt_store_does_not_take_the_tools_down(tmp_path):
    (tmp_path / ".window-campaigns.json").write_text("{ not json", encoding="utf-8")
    assert campaign_for_window(tmp_path, "tui:a1b2c3") is None
    bind_window(tmp_path, "tui:a1b2c3", "c")          # and it recovers by rewriting
    assert campaign_for_window(tmp_path, "tui:a1b2c3") == "c"


def test_an_empty_session_key_is_not_a_window(tmp_path):
    """Callers with no session (a cron turn, a test) must not all share one row."""
    bind_window(tmp_path, "", "c")
    assert campaign_for_window(tmp_path, "") is None


# ------------------------------------------------------------------ resolving

def test_the_binding_beats_counting_directories(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    a = _campaign(home, "cfd-dambreak")
    _campaign(home, "embedding-tune")
    bind_window(home, "tui:a1b2c3", "cfd-dambreak")
    assert _resolve_campaign_dir("", None, session_key="tui:a1b2c3") == a


def test_each_window_gets_its_own(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    a = _campaign(home, "cfd-dambreak")
    b = _campaign(home, "embedding-tune")
    bind_window(home, "tui:a1b2c3", "cfd-dambreak")
    bind_window(home, "tui:d4e5f6", "embedding-tune")
    assert _resolve_campaign_dir("", None, session_key="tui:a1b2c3") == a
    assert _resolve_campaign_dir("", None, session_key="tui:d4e5f6") == b


def test_a_window_that_never_submitted_still_falls_back(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    only = _campaign(home, "cfd-dambreak")
    assert _resolve_campaign_dir("", None, session_key="tui:fresh") == only


def test_a_binding_to_something_gone_falls_back_rather_than_pointing_nowhere(
    monkeypatch, tmp_path
):
    home = _home(monkeypatch, tmp_path)
    only = _campaign(home, "embedding-tune")
    bind_window(home, "tui:a1b2c3", "deleted-campaign")
    assert _resolve_campaign_dir("", None, session_key="tui:a1b2c3") == only


def test_an_explicit_name_still_wins_over_the_binding(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "cfd-dambreak")
    other = _campaign(home, "embedding-tune")
    bind_window(home, "tui:a1b2c3", "cfd-dambreak")
    assert _resolve_campaign_dir("embedding-tune", None,
                                 session_key="tui:a1b2c3") == other


# ------------------------------------------------------------------ writing it

@pytest.mark.asyncio
async def test_submitting_binds_the_window(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "cfd-dambreak")
    _install(monkeypatch)

    sub = OpsSubmitTool(cron_service=_FakeCron())
    sub.set_context("tui", "default", "tui:a1b2c3")
    out = await sub.execute(host="h", configs=[{"a": 1}], objective="o",
                            eta_seconds=60, round=0, campaign="cfd-dambreak")
    assert "Submitted" in out
    assert campaign_for_window(home, "tui:a1b2c3") == "cfd-dambreak"


@pytest.mark.asyncio
async def test_a_wake_records_the_window_that_asked_for_it(monkeypatch, tmp_path):
    """Otherwise the other window can claim it and run the turn on its own work."""
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "cfd-dambreak")
    _install(monkeypatch)

    cron = _FakeCron()
    sub = OpsSubmitTool(cron_service=cron)
    sub.set_context("tui", "default", "tui:a1b2c3")
    await sub.execute(host="h", configs=[{"a": 1}], objective="o",
                      eta_seconds=60, round=0, campaign="cfd-dambreak")
    assert cron.jobs and cron.jobs[0].get("owner") == "tui:a1b2c3"


@pytest.mark.asyncio
async def test_no_session_key_still_works(monkeypatch, tmp_path):
    """A single window, a cron turn, the CLI: none of them are broken by this."""
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "cfd-dambreak")
    _install(monkeypatch)

    sub = OpsSubmitTool(cron_service=_FakeCron())
    sub.set_context("cli", "direct")
    out = await sub.execute(host="h", configs=[{"a": 1}], objective="o",
                            eta_seconds=60, round=0, campaign="cfd-dambreak")
    assert "Submitted" in out
    assert not (home / ".window-campaigns.json").exists(), (
        "no window, no row -- otherwise every session-less caller shares one"
    )


# ------------------------------------------------- 4: naming is the claim

def test_naming_a_campaign_is_how_a_window_claims_it(monkeypatch, tmp_path):
    """A window's first call is a read, not a submit. Binding only on submit left
    that first call with nothing to resolve, so the two-window case broke on the
    very first thing either window did.

    Naming a campaign is the window saying what it is working on -- not a side
    effect of submitting to it, which is why it belongs in the resolver that every
    tool goes through rather than in one tool.
    """
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "cfd-dambreak")
    _campaign(home, "embedding-tune")

    _resolve_campaign_dir("cfd-dambreak", None, session_key="tui:a1b2c3")
    assert campaign_for_window(home, "tui:a1b2c3") == "cfd-dambreak"
    # and from here on it needs no name
    assert _resolve_campaign_dir("", None, session_key="tui:a1b2c3").name == "cfd-dambreak"


def test_reading_a_finished_campaign_does_not_move_the_window_onto_it(monkeypatch, tmp_path):
    """Checking a finished campaign's report is ordinary. The window is still
    working on whatever it was working on."""
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "task-a", concluded=True)
    _campaign(home, "task-b")
    bind_window(home, "tui:a1b2c3", "task-b")
    _resolve_campaign_dir("task-a", None, session_key="tui:a1b2c3")
    assert campaign_for_window(home, "tui:a1b2c3") == "task-b"


# ------------------------------------------------- 5: the task is an identity too

def test_a_new_window_with_the_same_task_rejoins_it(monkeypatch, tmp_path):
    """The point of the state being on disk. A session key dies with its window;
    the task statement the operator hands over does not."""
    from raven.ops.window import campaign_for_task, remember_task

    home = _home(monkeypatch, tmp_path)
    d = _campaign(home, "cfd-dambreak")
    _campaign(home, "embedding-tune")
    remember_task(d, "sha-of-the-legA-statement")

    assert campaign_for_task(home, "sha-of-the-legA-statement") == "cfd-dambreak"
    assert _resolve_campaign_dir(
        "", None, session_key="tui:brand-new", task="sha-of-the-legA-statement"
    ) == d


def test_a_task_still_watched_by_a_live_window_is_not_taken_over(monkeypatch, tmp_path):
    """Running the same statement twice on purpose is a second experiment, not a
    takeover. Same liveness rule as a wake's owner: yield only to a window that
    is still there."""
    import os

    from raven.ops.window import campaign_for_task, remember_task

    home = _home(monkeypatch, tmp_path)
    d = _campaign(home, "cfd-dambreak")
    remember_task(d, "sha-1")
    bind_window(home, "tui:owner", "cfd-dambreak", pid=os.getppid())

    assert campaign_for_task(home, "sha-1", exclude_live=True) is None


def test_a_task_whose_window_is_gone_is_free_to_rejoin(monkeypatch, tmp_path):
    import os

    from raven.ops.window import campaign_for_task, remember_task

    home = _home(monkeypatch, tmp_path)
    d = _campaign(home, "cfd-dambreak")
    remember_task(d, "sha-1")
    dead = os.fork()
    if dead == 0:
        os._exit(0)
    os.waitpid(dead, 0)
    bind_window(home, "tui:gone", "cfd-dambreak", pid=dead)

    assert campaign_for_task(home, "sha-1", exclude_live=True) == "cfd-dambreak"


def test_the_session_binding_is_consulted_before_the_task(monkeypatch, tmp_path):
    from raven.ops.window import remember_task

    home = _home(monkeypatch, tmp_path)
    a = _campaign(home, "cfd-dambreak")
    b = _campaign(home, "embedding-tune")
    remember_task(a, "sha-1")
    bind_window(home, "tui:a1b2c3", "embedding-tune")
    assert _resolve_campaign_dir("", None, session_key="tui:a1b2c3", task="sha-1") == b


# ------------------------------------------------- 6: the fallback has to be usable

def test_the_ambiguity_message_carries_what_a_task_statement_names(monkeypatch, tmp_path):
    """Third tier, and the only one a person or an agent has to act on. Listing
    names alone cannot be matched against a task statement; the case path, the
    host and the budget can."""
    home = _home(monkeypatch, tmp_path)
    for name, case in (("dambreak-legA", "/home/cfd/work/case_legA"),
                       ("dambreak-legB2", "/home/cfd/work/case_legB2")):
        d = _campaign(home, name)
        meta = json.loads((d / "meta.json").read_text())
        meta["staged_case"] = case
        meta["objective"] = {"metric": "completion", "direction": "max"}
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        _resolve_campaign_dir("", None, session_key="tui:brand-new")
    msg = str(exc.value)
    assert "/home/cfd/work/case_legA" in msg and "/home/cfd/work/case_legB2" in msg
    assert "campaign=" in msg, "and it must say how to claim one"
    assert "60 gpu-minute" in msg or "gpu-minute" in msg


@pytest.mark.asyncio
async def test_submitting_records_which_statement_it_was_for(monkeypatch, tmp_path):
    from raven.ops.window import TASK_FILE, campaign_for_task

    home = _home(monkeypatch, tmp_path)
    d = _campaign(home, "cfd-dambreak")
    _install(monkeypatch)

    sub = OpsSubmitTool(cron_service=_FakeCron())
    sub.set_context("tui", "default", "tui:a1b2c3", "sha-legA")
    await sub.execute(host="h", configs=[{"a": 1}], objective="o",
                      eta_seconds=60, round=0, campaign="cfd-dambreak")

    assert (d / TASK_FILE).read_text().strip() == "sha-legA"
    assert campaign_for_task(home, "sha-legA") == "cfd-dambreak"


def test_the_fingerprint_ignores_rewrapping_but_not_the_task(tmp_path):
    from raven.ops.window import task_fingerprint

    a = task_fingerprint("跑个 OpenFOAM 溃坝仿真 算例在 /home/cfd/work/case_legA")
    a_rewrapped = task_fingerprint("跑个 OpenFOAM 溃坝仿真\n算例在   /home/cfd/work/case_legA\n")
    b = task_fingerprint("跑个 OpenFOAM 溃坝仿真 算例在 /home/cfd/work/case_legB2")
    assert a == a_rewrapped, "a re-paste that rewraps is the same task"
    assert a != b, "and the one word that differs between the two legs must not collide"
    assert task_fingerprint("") == "" and task_fingerprint("   ") == ""


def test_the_statement_is_this_turn_s_message():
    """Superseded the rule this file first pinned.

    It originally read the session's *first* user message, to keep a wake turn's
    "[Ops campaign 'X' round 2 due]" from becoming the identity. That protection
    was already provided by writing the fingerprint once, at the campaign's
    origin -- and reading the first message broke the ordinary case instead: one
    window doing task A, then chat, then task B stamped B's campaign with A's
    fingerprint, and a window that opened with "hi" stamped its campaign "hi".
    """
    from raven.agent.loop.main import AgentLoop

    session = type("S", (), {"messages": [
        {"role": "user", "content": "跑个 OpenFOAM 溃坝仿真,算例 /home/cfd/work/case_legA"},
        {"role": "assistant", "content": "好"},
    ]})()
    assert AgentLoop._task_statement(session, "第二个任务:微调 embedding") == "第二个任务:微调 embedding"
    empty = type("S", (), {"messages": []})()
    assert AgentLoop._task_statement(empty, "the first message") == "the first message"


def test_a_name_that_is_not_its_own_slug_still_resolves(monkeypatch, tmp_path):
    """Written because macOS hid it. The store held the campaign as the operator
    wrote it, ``dambreak-legA``, and the lookup re-slugged to ``dambreak-lega``:
    a different directory on any case-sensitive filesystem, which is every host
    these campaigns actually run on. On the developer's Mac the two matched and
    the binding appeared to work.
    """
    home = _home(monkeypatch, tmp_path)
    d = _campaign(home, "dambreak-legA")
    bind_window(home, "tui:a1b2c3", "dambreak-legA")
    got = _resolve_campaign_dir("", None, session_key="tui:a1b2c3")
    assert got.name == d.name, f"resolved {got.name!r}, campaign is {d.name!r}"
    assert got == d


# ------------------------------------------------- 实测暴露的两处(2026-08-13 夜)

def test_the_task_is_recorded_once_and_not_rewritten(tmp_path):
    """A campaign's statement is established when it is created.

    Measured on the two-window run: ``remember_task`` ran on every submit, and a
    wake turn's own first user message is "[Ops campaign 'X' round 1 due] ...", so
    the fingerprint of the statement was overwritten by the fingerprint of a wake
    message. A window opened afterwards with the original statement could then no
    longer recognise the campaign -- which is the one thing the fingerprint exists
    for.
    """
    from raven.ops.window import TASK_FILE, remember_task

    remember_task(tmp_path, "the-statement")
    remember_task(tmp_path, "a-wake-message")
    assert (tmp_path / TASK_FILE).read_text().strip() == "the-statement"


def test_a_wake_turn_is_not_a_window(tmp_path):
    """A wake turn already knows its campaign -- the wake message names it -- so it
    has no binding to write. Writing one adds a row per wake, which grows without
    bound over a long watch, and none of those rows is a window."""
    bind_window(tmp_path, "cron:6b3bd95f", "cfd-dambreak", pid=1)
    assert campaign_for_window(tmp_path, "cron:6b3bd95f") is None
    assert not (tmp_path / ".window-campaigns.json").exists()


# ------------------------------------- 一个窗口连续做多个任务(中间还闲聊)

def _loop_statement(prior_messages, now):
    from raven.agent.loop.main import AgentLoop

    return AgentLoop._task_statement(type("S", (), {"messages": prior_messages})(), now)


TASK_A = "跑个 OpenFOAM 溃坝仿真,算例在 /home/cfd/work/case_legA"
TASK_B = "帮我微调一版 embedding 模型,看 NFCorpus 的 nDCG@10"


def test_a_second_task_in_the_same_window_is_its_own_task():
    """One window, task A, some chat, then task B -- which is how anyone uses a
    chat window. Taking the statement from the session's *first* user message
    froze it at task A forever, so task B's campaign was stamped with task A's
    fingerprint and two campaigns claimed one task. The turn's own message is the
    statement; a wake message cannot overwrite one because the fingerprint is
    written once, at the campaign's origin.
    """
    from raven.ops.window import task_fingerprint

    history = [{"role": "user", "content": TASK_A}, {"role": "assistant", "content": "ok"},
               {"role": "user", "content": "刚才那个结果再解释一下"},
               {"role": "assistant", "content": "..."}]
    assert task_fingerprint(_loop_statement(history, TASK_B)) == task_fingerprint(TASK_B)
    assert task_fingerprint(_loop_statement([], TASK_A)) == task_fingerprint(TASK_A)


def test_chat_before_the_task_does_not_become_the_task():
    """A window that opened with "hi" must not stamp its campaign with "hi"."""
    from raven.ops.window import task_fingerprint

    history = [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在"}]
    assert task_fingerprint(_loop_statement(history, TASK_A)) == task_fingerprint(TASK_A)


def test_a_wake_message_cannot_replace_a_recorded_statement(tmp_path):
    """The turn's message is used, so a wake turn offers the wake text -- and the
    campaign keeps the statement it was created with."""
    from raven.ops.window import TASK_FILE, remember_task, task_fingerprint

    remember_task(tmp_path, task_fingerprint(TASK_A))
    remember_task(tmp_path, task_fingerprint("[Ops campaign 'x' round 2 due] Jobs ready."))
    assert (tmp_path / TASK_FILE).read_text().strip() == task_fingerprint(TASK_A)


def test_the_window_follows_whichever_task_it_is_on_now(monkeypatch, tmp_path):
    """Starting a second task moves the window onto it; the first keeps running
    and its wakes still name it, so it is not orphaned."""
    home = _home(monkeypatch, tmp_path)
    a = _campaign(home, "cfd-a")
    b = _campaign(home, "ml-b")
    _resolve_campaign_dir("cfd-a", None, session_key="tui:one")
    assert _resolve_campaign_dir("", None, session_key="tui:one") == a
    _resolve_campaign_dir("ml-b", None, session_key="tui:one")
    assert _resolve_campaign_dir("", None, session_key="tui:one") == b
    assert _resolve_campaign_dir("cfd-a", None, session_key="tui:one") == a


def test_a_window_whose_experiment_finished_is_not_held_to_it(monkeypatch, tmp_path):
    """One window, one experiment after another -- the ordinary way a chat window
    gets used.

    The binding still named the finished experiment, and the binding is consulted
    first, so the next statement's fingerprint never got a chance. The agent's
    first call returned the finished campaign, whose status leads with "This
    campaign is over ... There is nothing left to do here" -- which reads as an
    answer to "start the second experiment".

    The machinery for this was already there; only the order was wrong. Skipping a
    finished binding lets the fingerprint be asked, and it correctly finds nothing,
    which is how the caller learns this is new work. The same "exclude concluded"
    rule was already applied when counting directories; this is the level it was
    missing from.
    """
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "exp-one", concluded=True)
    bind_window(home, "tui:win1", "exp-one", pid=1)

    with pytest.raises(ValueError) as exc:
        _resolve_campaign_dir("", None, session_key="tui:win1")
    assert "new" in str(exc.value), "the answer is that this is new work"


def test_a_finished_experiment_is_still_readable_by_name(monkeypatch, tmp_path):
    """Its report is checked by naming it, and that must keep working."""
    home = _home(monkeypatch, tmp_path)
    d = _campaign(home, "exp-one", concluded=True)
    bind_window(home, "tui:win1", "exp-one", pid=1)
    assert _resolve_campaign_dir("exp-one", None, session_key="tui:win1") == d


def test_the_next_experiment_takes_the_window_over(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "exp-one", concluded=True)
    two = _campaign(home, "exp-two")
    bind_window(home, "tui:win1", "exp-one", pid=1)
    assert _resolve_campaign_dir("", None, session_key="tui:win1") == two, (
        "one live campaign left, so no name is needed at all"
    )


def test_the_last_live_campaign_is_not_handed_to_another_window(monkeypatch, tmp_path):
    """"Only one campaign is live, so it must be the one you mean" is true for a
    single window and false the moment there are two.

    Found by walking the real pattern: window A finishes its first experiment and
    is given a second statement while window B is still running its own. A's
    binding is skipped (finished), A's new statement matches nothing, and the
    count-the-directories fallback then had exactly one live campaign to offer --
    B's. A would have driven B's experiment.
    """
    import os

    home = _home(monkeypatch, tmp_path)
    _campaign(home, "exp-one", concluded=True)
    _campaign(home, "exp-two")
    bind_window(home, "tui:A", "exp-one", pid=os.getpid())
    bind_window(home, "tui:B", "exp-two", pid=os.getppid())   # a live window

    with pytest.raises(ValueError):
        _resolve_campaign_dir("", None, session_key="tui:A")
    # B itself still resolves it, through its own binding
    assert _resolve_campaign_dir("", None, session_key="tui:B").name == "exp-two"


def test_a_campaign_whose_window_is_gone_is_still_offered(monkeypatch, tmp_path):
    """The takeover case must not be blocked by the same rule."""
    import os

    home = _home(monkeypatch, tmp_path)
    only = _campaign(home, "exp-two")
    dead = os.fork()
    if dead == 0:
        os._exit(0)
    os.waitpid(dead, 0)
    bind_window(home, "tui:gone", "exp-two", pid=dead)
    assert _resolve_campaign_dir("", None, session_key="tui:fresh") == only
