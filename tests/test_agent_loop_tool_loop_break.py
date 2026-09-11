"""Tool-failure loop break: nudge the model off a tool it keeps failing on.

When the same tool fails deterministically N times running (transient errors
excluded), the loop appends a change-approach nudge to the tool result — once
per fresh streak, bounded per turn — so a weak model stops repeating a dead call.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.loop.failure_streak import failure_class, is_hard_tool_failure
from raven.agent.loop.no_progress import (
    NoProgressAction,
    NoProgressGuard,
    no_progress_key,
    no_progress_stop,
)
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# --------------------------------------------------------------------------- #
# unit: is_hard_tool_failure                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "result,expected",
    [
        ("Error: Tool 'x' is not available. It may have been unloaded, or the name may be wrong.", True),
        ("Error: file does not exist", True),
        ("No matches found.", False),  # empty search = success, not a failure
        ("No files found", False),  # find empty result
        ("route not found in cache, using local fallback", False),  # success mentioning the phrase
        ("Exit code: 1\nboom", True),
        ("Exit code: 0\nok", False),  # exit 0 = success
        ("ok, wrote 3 files", False),
        ("Error: 429 rate limit, retry later", False),  # transient → not hard
        ("request timed out", False),  # transient → not hard
    ],
)
def test_is_hard_tool_failure(result, expected):
    assert is_hard_tool_failure(result) is expected


@pytest.mark.parametrize(
    "result,expected",
    [
        # The registry's own wording for a name that did not resolve. It says
        # "is not available" rather than "not found" because it cannot tell a
        # hallucinated name from a tool unloaded mid-turn -- but it is the same
        # failure, and the streak must not file it under the catch-all.
        ("Error: Tool 'x' is not available. It may have been unloaded, or the name may be wrong.", "not_found"),
        ("Error: tool 'x' is not available. It may have been unloaded, or the name may be wrong.", "not_found"),
        ("Error: Invalid parameters for tool 'x': missing 'path'", "schema"),
        ("Error: Tool 'x' timed out after 300s.", "timeout"),
        ("Error: something else entirely", "other"),
    ],
)
def test_failure_class(result, expected):
    assert failure_class(result) == expected


# --------------------------------------------------------------------------- #
# loop level: repeated same-tool failure -> bounded nudges                     #
# --------------------------------------------------------------------------- #


class _AlwaysFailsSameToolProvider(LLMProvider):
    """Keeps calling one (nonexistent) tool that hard-fails every time."""

    def __init__(self):
        super().__init__(api_key="test")
        self.loop_marker_counts: list[int] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.loop_marker_counts.append(sum(1 for m in messages if "[loop]" in str(m.get("content", ""))))
        if tools is None:  # max-iter synthesis call
            return LLMResponse(content="done", finish_reason="stop")
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{len(self.loop_marker_counts)}", name="no_such_tool", arguments={})],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_repeated_tool_failure_nudges_bounded(workspace):
    provider = _AlwaysFailsSameToolProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=6),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )

    # A nudge fired (>=1 [loop] marker seen) but never exceeded the per-turn cap.
    assert max(provider.loop_marker_counts) == AgentLoop._LOOP_BREAK_MAX


class _NudgeTextProvider(_AlwaysFailsSameToolProvider):
    """Keeps the nudge text itself, not just a count of markers."""

    def __init__(self):
        super().__init__()
        self.tool_results: list[str] = []

    async def chat(self, messages, **kwargs):
        self.tool_results += [
            str(m.get("content", ""))
            for m in messages
            if m.get("role") == "tool" and "[loop]" in str(m.get("content", ""))
        ]
        return await super().chat(messages, **kwargs)


def _find_skill_stub():
    from raven.contracts.tool import Tool

    class _FindSkill(Tool):
        @property
        def name(self) -> str:
            return "find_skill"

        @property
        def description(self) -> str:
            return "stub"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs) -> str:
            return "ran"

    return _FindSkill()


async def _nudges_with_find_skill_switched(workspace, off: bool) -> list[str]:
    """Drive a real failing streak and hand back the nudges it produced.

    Switched off through the config file rather than through the registry's
    source directly, so the path under test is the one the settings page uses.
    """
    import json
    from pathlib import Path

    from raven.config.loader import get_config_path

    cfg = get_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"tools": {"disabledTools": ["find_skill"] if off else []}}), encoding="utf-8")

    provider = _NudgeTextProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=6),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    agent.tools.register(_find_skill_stub())
    assert isinstance(cfg, Path)

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )
    return provider.tool_results


@pytest.mark.asyncio
async def test_the_nudge_does_not_name_a_switched_off_find_skill(workspace):
    """The nudge is model-visible text, so a registration check advertised a tool
    the operator had switched off -- and `execute` then refuses it as absent.

    Registration was a fair proxy for availability until the off switch stopped
    unregistering; a withheld tool stays in the registry, which is what makes the
    switch reversible.
    """
    nudges = await _nudges_with_find_skill_switched(workspace, off=True)

    assert nudges, "no nudge fired, so the assertion below would pass vacuously"
    assert not any("find_skill" in n for n in nudges)


@pytest.mark.asyncio
async def test_and_does_name_it_when_it_is_on(workspace):
    """The pairing case, so the assertion above cannot pass by never mentioning
    the tool at all."""
    nudges = await _nudges_with_find_skill_switched(workspace, off=False)

    assert nudges
    assert any("find_skill" in n for n in nudges)


class _AlwaysTruncatedWriteProvider(LLMProvider):
    """Every turn is cut off inside the same `write_file` call."""

    def __init__(self):
        super().__init__(api_key="test")
        self.nudges: list[str] = []

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.nudges.extend(str(m.get("content", "")) for m in messages if "[loop]" in str(m.get("content", "")))
        if tools is None:  # max-iter synthesis call
            return LLMResponse(content="done", finish_reason="stop")
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{len(self.nudges)}", name="write_file", arguments={"path": "snake.py"})],
            finish_reason="length",
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_truncation_streak_is_nudged_toward_a_smaller_payload(workspace):
    """The class reaches the nudge from the loop, not only from a direct call.

    The streak key is already `(tool, class)`; only `[0]` was being read, so a
    per-class text is inert until the call site passes the rest of what it has.
    """
    provider = _AlwaysTruncatedWriteProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=6),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="write a long file",
        ),
        session_key="s1",
    )

    assert provider.nudges, "two truncations running should have fired the loop break"
    assert all("different tool" not in n for n in provider.nudges)
    assert all("smaller" in n for n in provider.nudges)


# --------------------------------------------------------------------------- #
# no progress: a call that keeps succeeding and keeps answering the same       #
# --------------------------------------------------------------------------- #


def test_no_progress_key_separates_two_spellings_of_one_read():
    """Why the count is per key rather than per turn: the run that motivated
    this alternated a read with the same read behind a checksum, so the two
    spellings key apart and each has to reach the threshold on its own. That
    costs twice the calls and still fires -- a consecutive streak would have
    stayed at one forever.
    """
    plain = no_progress_key("exec", {"command": "cat t.json"}, "keys: [1]")
    checksummed = no_progress_key("exec", {"command": "md5sum t.json && cat t.json"}, "keys: [1]")
    assert plain != checksummed
    assert plain == no_progress_key("exec", {"command": "cat t.json"}, "keys: [1]")
    assert plain != no_progress_key("exec", {"command": "cat t.json"}, "keys: [2]"), "a changed answer is progress"


class _IdenticalSucceedingCallProvider(LLMProvider):
    """Calls one tool with one set of arguments, which succeeds the same way every time."""

    def __init__(self):
        super().__init__(api_key="test")
        self.loop_marker_counts: list[int] = []
        self.nudges: list[str] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.loop_marker_counts.append(sum(1 for m in messages if "[loop]" in str(m.get("content", ""))))
        self.nudges += [str(m.get("content", "")) for m in messages if "[loop]" in str(m.get("content", ""))]
        if tools is None:  # max-iter synthesis call
            return LLMResponse(content="done", finish_reason="stop")
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(id=f"c{len(self.loop_marker_counts)}", name="list_dir", arguments={"path": "."})
            ],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_an_unchanging_successful_call_is_nudged_and_bounded(workspace):
    """The case the failure streak structurally cannot see: every call returns
    exit 0, and a success resets that counter to zero.
    """
    provider = _IdenticalSucceedingCallProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=AgentLoop._NO_PROGRESS_THRESHOLD + 4),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )

    assert max(provider.loop_marker_counts) >= 1, "an unchanging successful call was never nudged"
    assert max(provider.loop_marker_counts) <= AgentLoop._NO_PROGRESS_MAX
    assert any("same result for the same arguments" in n for n in provider.nudges)


class _ImageReadProvider(LLMProvider):
    """Reads one image over and over, repainting it first when asked to.

    The repaint is the point: the file keeps its size, so `read_file` answers with
    the same metadata line every time and different pixels every time.
    """

    def __init__(self, target, repaint: bool):
        super().__init__(api_key="test")
        self._target = target
        self._repaint = repaint
        self.loop_marker_counts: list[int] = []

    async def chat(self, messages, tools=None, **kwargs):
        from PIL import Image

        self.loop_marker_counts.append(sum(1 for m in messages if "[loop]" in str(m.get("content", ""))))
        n = len(self.loop_marker_counts)
        shade = (n * 23 % 256, 40, 90) if self._repaint else (10, 40, 90)
        Image.new("RGB", (320, 240), shade).save(self._target)
        if tools is None:  # max-iter synthesis call
            return LLMResponse(content="done", finish_reason="stop")
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{n}", name="read_file", arguments={"path": "slide.png"})],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


async def _read_image_turn(workspace, *, repaint: bool, monkeypatch) -> _ImageReadProvider:
    pytest.importorskip("PIL")
    monkeypatch.setattr(AgentLoop, "_supports_vision", lambda self, m=None: True)
    provider = _ImageReadProvider(workspace / "slide.png", repaint)
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=AgentLoop._NO_PROGRESS_THRESHOLD + 4),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )
    return provider


@pytest.mark.asyncio
async def test_changing_pixels_behind_one_metadata_line_are_progress(workspace, monkeypatch):
    """`model_text` is not the whole result, so it cannot be the whole key.

    An image read answers with path, dimensions and a token estimate -- stable
    across a repaint at the same size -- while the pixels ride in the routed
    blocks. Keyed on the text alone, a render-edit-inspect pass reads "the same
    result" eight times and is told to stop looking, which is the workflow this
    guard was written to help. The pair is what makes this discriminating: the
    same loop over an unchanging image must still fire.
    """
    repainted = await _read_image_turn(workspace, repaint=True, monkeypatch=monkeypatch)
    assert max(repainted.loop_marker_counts) == 0, "a repainted image counted as no progress"

    unchanged = await _read_image_turn(workspace, repaint=False, monkeypatch=monkeypatch)
    assert max(unchanged.loop_marker_counts) >= 1, "an unchanging image was never nudged"


# --------------------------------------------------------------------------- #
# no progress: the ladder past the nudge                                      #
# --------------------------------------------------------------------------- #


def _guard() -> NoProgressGuard:
    return NoProgressGuard(
        nudge_at=AgentLoop._NO_PROGRESS_THRESHOLD,
        refuse_at=AgentLoop._NO_PROGRESS_REFUSE,
        max_nudges=AgentLoop._NO_PROGRESS_MAX,
        max_refusals=AgentLoop._NO_PROGRESS_REFUSALS_MAX,
    )


def _walk(guard: NoProgressGuard, tool: str, args: dict, result: str, rounds: int) -> list[str]:
    """One call made `rounds` times, as the loop makes it: check, run, record."""
    steps = []
    for _ in range(rounds):
        action, _text = guard.check(tool, args)
        if action is not NoProgressAction.RUN:
            steps.append(action.name.lower())
            if action is NoProgressAction.END_TURN:
                break
            continue
        guard.record(tool, args, result)
        steps.append("run+nudge" if guard.take_nudge() else "run")
    return steps


def test_the_ladder_nudges_then_refuses_then_ends_the_turn():
    """Why the nudge alone was not enough: it can only fire once for one answer,
    and the model it has to reach may be a fixed point. The measured turn sent
    288 byte-identical calls, 280 of them behind the only guard there was.
    """
    steps = _walk(_guard(), "exec", {"command": "cat t.json"}, "Exit code: 0\nsame", 40)

    assert steps == ["run"] * 7 + ["run+nudge"] + ["run"] * 4 + ["refuse"] * 3 + ["end_turn"]
    assert steps.index("end_turn") == 15, "the turn has to end on the 16th identical call"


def test_a_refusal_says_what_happened_and_what_to_do_instead():
    """A bare refusal produces a different loop, so the way out has to be in the
    error text -- that is the seat the model has been measured to read."""
    guard = _guard()
    for _ in range(AgentLoop._NO_PROGRESS_REFUSE):
        guard.check("exec", {"command": "cat t.json"})
        guard.record("exec", {"command": "cat t.json"}, "Exit code: 0\nsame")
    action, text = guard.check("exec", {"command": "cat t.json"})

    assert action is NoProgressAction.REFUSE
    assert text.startswith("Error"), "the registry reads the leading word as the verdict"
    assert "was not run" in text
    assert "take the next real step" in text
    assert "wait for it in one longer wait" in text, (
        "for a poll, waiting explicitly *is* the call being refused, so the way out has to be "
        "a wait the model can actually make instead"
    )
    assert "answered something new" in text
    assert "then this turn ends" in text


def test_a_poll_whose_answer_changes_is_left_alone():
    """The shape the result has always been half the key for: identical
    arguments, a moving answer. Forty rounds of it must not reach any step."""
    guard = _guard()
    for i in range(40):
        action, _ = guard.check("read_file", {"path": "job.log"})
        assert action is NoProgressAction.RUN, f"a moving answer was refused on round {i}"
        guard.record("read_file", {"path": "job.log"}, f"lines read: {i}")
        assert guard.take_nudge() is None


def test_a_wait_loop_keeps_the_identical_sleep_it_waits_with():
    """The case that made the enforcing step ask a harder question than the
    nudge does. A model waiting on a background job sleeps with one identical
    call and checks with another whose answer moves, so the sleep's own answer
    never changes -- counted the way the nudge counts, it would be refused after
    twelve rounds and the wait would die. The nudge still fires, deliberately:
    injecting a line of advice into a workflow that is in fact progressing costs
    a line of advice, and refusing its calls costs the workflow.
    """
    guard = _guard()
    nudged = False
    for i in range(40):
        action, _ = guard.check("exec", {"command": "sleep 5"})
        assert action is NoProgressAction.RUN, f"the sleep a wait loop waits with was refused on round {i}"
        guard.record("exec", {"command": "sleep 5"}, "Exit code: 0")
        nudged = nudged or bool(guard.take_nudge())
        guard.record("exec", {"command": "cat progress"}, f"{i}% done")
        guard.take_nudge()

    assert nudged, "the advisory step is meant to stay lenient, so it should still have fired"


def test_intervening_progress_lets_a_frozen_pair_run_again():
    """The freeze is evidence, not a verdict, and evidence goes stale.

    Twelve identical answers establish what the call returns. Then real work
    happens -- three writes, each a first sighting -- and what the call returns
    is no longer what was established. Reading the freeze off a table instead of
    re-asking left the model with three refusals and the end of the turn, and an
    error telling it to change the call so that it asks a different question,
    which for an exec means editing a command string in order to run the same
    command.
    """
    guard = _guard()
    call = ("exec", {"command": "cat build.log"})
    for _ in range(AgentLoop._NO_PROGRESS_REFUSE):
        assert guard.check(*call)[0] is NoProgressAction.RUN
        guard.record(*call, "Exit code: 0\nno such target")

    assert guard.check(*call)[0] is NoProgressAction.REFUSE, "nothing else has answered anything yet"

    for i in range(3):
        guard.record("write_file", {"path": f"src/{i}.py"}, f"wrote src/{i}.py")

    action, text = guard.check(*call)
    assert action is NoProgressAction.RUN, "real work happened, so the established answer is stale"
    assert text == ""
    guard.record(*call, "Exit code: 0\nbuild ok")
    assert guard.check(*call)[0] is NoProgressAction.RUN, "and the answer it gave was a new one"


def test_a_rescued_pair_that_goes_back_to_repeating_still_ends_the_turn():
    """The bound a rescue must not cost. One new answer per ``refuse_at``
    repeats would otherwise rescue a pair forever -- 12 wasted calls in 13, and
    no ladder at all -- so an establishment after the first spends from the same
    budget the refusals spend.
    """
    guard = _guard()
    call = ("exec", {"command": "cat t.json"})
    ran = novel = 0
    ended = None
    for attempt in range(600):
        action, _ = guard.check(*call)
        if action is NoProgressAction.END_TURN:
            ended = attempt + 1
            break
        if action is NoProgressAction.REFUSE:
            guard.record("exec", {"command": f"echo {novel}"}, str(novel))
            novel += 1
            continue
        guard.record(*call, "Exit code: 0\nsame")
        guard.take_nudge()
        ran += 1

    assert ended is not None, "a pair rescued by manufactured novelty has to stay bounded"
    assert ran <= AgentLoop._NO_PROGRESS_REFUSE * (AgentLoop._NO_PROGRESS_REFUSALS_MAX + 1)
    assert (ran, novel, ended) == (24, 2, 27)


def test_the_stop_says_a_rescued_call_was_run_again_rather_than_only_refused():
    """A model told it was refused three times when it was in fact run again
    twice reads the wrong lesson out of the stop."""
    guard = _guard()
    call = ("exec", {"command": "cat t.json"})
    text = ""
    for attempt in range(600):
        action, text = guard.check(*call)
        if action is NoProgressAction.END_TURN:
            break
        if action is NoProgressAction.REFUSE:
            guard.record("exec", {"command": f"echo {attempt}"}, str(attempt))
            continue
        guard.record(*call, "Exit code: 0\nsame")

    assert "refused 2 times" in text
    assert "run again 1 time after other work answered something new" in text
    assert "run again 1 times" not in text, "one rescue is one time, not one times"
    # A guard on nothing at all: at max_refusals 0 the turn ends on the freeze
    # itself, and neither half of the budget was drawn on.
    assert no_progress_stop("exec", 0, 0).endswith(
        "The same call was already established, so it is stopped here rather than repeated to the iteration limit."
    )


def test_a_wait_loop_whose_poll_answers_identically_is_stopped_and_says_so():
    """The shape the wait-loop guarantee does not cover, and had no test.

    ``_novelty`` counts first sightings of a (call, answer) key, so a poll
    answering ``status: RUNNING`` byte-identically contributes none after the
    first: neither key resets, both freeze, and the turn ends on a wait that was
    working. Pinned rather than fixed, because there is nothing to count -- see
    the test below for why -- and pinned with the numbers so the cost of the
    honest answer stays visible: twelve polls, three chances, then the turn ends
    with a wrap-up naming what it was waiting for.
    """
    guard = _guard()
    sleep = ("exec", {"command": "sleep 5"})
    poll = ("exec", {"command": "cat status"})
    polls = 0
    ended = None
    for round_no in range(1, 200):
        for pair, answer in ((sleep, "Exit code: 0"), (poll, "status: RUNNING")):
            action, _ = guard.check(*pair)
            if action is NoProgressAction.END_TURN:
                ended = round_no
                break
            if action is NoProgressAction.RUN:
                guard.record(*pair, answer)
                guard.take_nudge()
                polls += pair is poll
        if ended:
            break

    assert (polls, ended) == (12, 16)


def test_a_stable_wait_loop_and_a_stuck_read_pair_are_one_transcript():
    """Why the shape above is pinned instead of fixed.

    A fixed-interval wait with a stable poll and the two spellings of one stuck
    read hand this class the same thing: two pairs, each answering its own
    constant, alternating, nothing else. Any rule that keeps the first running
    keeps the second running too, and the second is the run this guard was
    written for -- an hour spent reading one file 127 times. The distinguishing
    fact, that one poll's answer depends on something outside the turn that will
    change it, is not in the transcript at all.
    """

    def verdicts(first, second):
        guard = _guard()
        out = []
        for _ in range(20):
            for tool, arguments, answer in (first, second):
                action, _text = guard.check(tool, arguments)
                out.append(action.name)
                if action is NoProgressAction.END_TURN:
                    return out
                if action is NoProgressAction.RUN:
                    guard.record(tool, arguments, answer)
                    guard.take_nudge()
        return out

    wait_loop = verdicts(
        ("exec", {"command": "sleep 5"}, "Exit code: 0"),
        ("exec", {"command": "cat status"}, "status: RUNNING"),
    )
    stuck_read = verdicts(
        ("exec", {"command": "cat t.json"}, "Exit code: 0\nsame"),
        ("exec", {"command": "md5sum t.json && cat t.json"}, "Exit code: 0\nsame md5"),
    )

    assert wait_loop == stuck_read, "no content rule can separate these, so neither can this guard"
    assert wait_loop[-1] == "END_TURN"


def test_a_longer_wait_lets_the_poll_it_waits_with_run_again():
    """What the wait loop gets instead of a rule that cannot exist.

    The refusal tells the model to wait for it in one longer wait. A wait of a
    different length is a first sighting, which is exactly the evidence the
    poll's freeze rests on being absent -- so the poll runs again, three times
    over, and a backing-off wait outlives its own refusal.
    """
    guard = _guard()
    poll = ("exec", {"command": "cat status"})
    seconds = 5
    polls = 0
    ended = None
    for round_no in range(1, 200):
        action, _ = guard.check("exec", {"command": f"sleep {seconds}"})
        if action is NoProgressAction.END_TURN:
            ended = round_no
            break
        if action is NoProgressAction.RUN:
            guard.record("exec", {"command": f"sleep {seconds}"}, "Exit code: 0")
            guard.take_nudge()
        else:
            seconds *= 2
            continue
        action, _ = guard.check(*poll)
        if action is NoProgressAction.END_TURN:
            ended = round_no
            break
        if action is NoProgressAction.RUN:
            guard.record(*poll, "status: RUNNING")
            guard.take_nudge()
            polls += 1
        else:
            seconds *= 2

    assert polls == 36, "three chances at twelve polls each, against twelve when the wait never changes"
    assert ended == 40


def test_the_measured_identical_call_loop_is_stopped_where_it_always_was():
    """The 207-call turn this ladder exists for, run past the fix.

    Nothing else answers anything new in it, so there is no evidence for the
    freeze to go stale against and the pair never runs again: twelve calls,
    three refusals, the turn ends on the sixteenth attempt -- the same numbers
    as before the freeze became re-checkable.
    """
    guard = _guard()
    call = ("exec", {"command": "python train.py --dry-run"})
    ran = refused = 0
    ended = None
    for attempt in range(207):
        action, _ = guard.check(*call)
        if action is NoProgressAction.END_TURN:
            ended = attempt + 1
            break
        if action is NoProgressAction.REFUSE:
            refused += 1
            continue
        guard.record(*call, "Exit code: 0\nok")
        guard.take_nudge()
        ran += 1

    assert (ran, refused, ended) == (12, 3, 16)


def test_two_spellings_of_one_stuck_read_each_reach_the_ladder():
    """The alternating shape the per-turn count was written for still lands.
    Each spelling keys apart and pays its own way there, and the first rounds
    reset while the other spelling is still new, so it costs more calls -- but
    it is bounded, which is the whole point.
    """
    guard = _guard()
    ended = 0
    for _ in range(80):
        for command in ("cat t.json", "md5sum t.json && cat t.json"):
            action, _ = guard.check("exec", {"command": command})
            if action is NoProgressAction.END_TURN:
                ended += 1
                break
            if action is NoProgressAction.REFUSE:
                continue
            guard.record("exec", {"command": command}, "Exit code: 0\nsame")
            guard.take_nudge()
        if ended:
            break

    assert ended == 1


class _IdenticalCallPastTheLadderProvider(LLMProvider):
    """Makes one identical succeeding call for as long as it is allowed to."""

    def __init__(self):
        super().__init__(api_key="test")
        self.turns = 0
        self.synthesized = False
        self.last_messages: list[dict] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.last_messages = list(messages)
        if tools is None:  # the early-exit wrap-up
            self.synthesized = True
            return LLMResponse(content="here is what I got", finish_reason="stop")
        self.turns += 1
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{self.turns}", name="list_dir", arguments={"path": "."})],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_frozen_call_stops_the_turn_without_reaching_the_iteration_cap(workspace):
    """End to end: the shape that reached iteration 317 of 600 having built
    nothing now ends its own turn, the refusals never run the tool, and the user
    gets the wrap-up rather than a canned apology.
    """
    cap = AgentLoop._NO_PROGRESS_REFUSE + AgentLoop._NO_PROGRESS_REFUSALS_MAX + 8
    provider = _IdenticalCallPastTheLadderProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=cap),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="go",
        ),
        session_key="s1",
    )

    results = [str(m.get("content", "")) for m in provider.last_messages if m.get("role") == "tool"]
    refused = [r for r in results if "was not run" in r]
    assert len(refused) == AgentLoop._NO_PROGRESS_REFUSALS_MAX + 1, "three refusals then the one that ends the turn"
    assert any("this turn is ending" in r for r in refused)
    ran = [r for r in results if "was not run" not in r]
    assert len(ran) == AgentLoop._NO_PROGRESS_REFUSE, "a refused call must not run the tool"
    assert provider.turns < cap, "the turn should have ended on the ladder, not on the iteration cap"
    assert provider.synthesized, "an early exit still owes the user an answer"


class _PollUntilWrittenProvider(LLMProvider):
    """Polls one path with one set of arguments while a job writes to it.

    Identical arguments every round and a different answer every round, which is
    the case the result has always been half the key for -- run through the loop
    rather than the guard, so the wiring is what the assertion is about.
    """

    def __init__(self, target, rounds: int):
        super().__init__(api_key="test")
        self._target = target
        self._rounds = rounds
        self.polls = 0
        self.refusals: list[str] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.refusals += [str(m.get("content", "")) for m in messages if "was not run" in str(m.get("content", ""))]
        if tools is None:
            return LLMResponse(content="done", finish_reason="stop")
        if self.polls >= self._rounds:
            return LLMResponse(content="the job finished", finish_reason="stop")
        self.polls += 1
        self._target.write_text("\n".join(f"line {i}" for i in range(self.polls)))
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{self.polls}", name="read_file", arguments={"path": "job.log"})],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_poll_runs_past_the_ladder_through_the_whole_loop(workspace):
    """More rounds than the ladder has steps, and not one of them is refused."""
    rounds = AgentLoop._NO_PROGRESS_REFUSE + AgentLoop._NO_PROGRESS_REFUSALS_MAX + 5
    (workspace / "job.log").write_text("")
    provider = _PollUntilWrittenProvider(workspace / "job.log", rounds)
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=rounds + 5),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="wait for the job",
        ),
        session_key="s1",
    )

    assert provider.polls == rounds, "the poll was cut short"
    assert provider.refusals == []
