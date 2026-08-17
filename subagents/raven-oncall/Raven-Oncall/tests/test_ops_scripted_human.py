"""A scripted human on the other end of an escalation.

Asking a person is the one action in an on-call loop whose cost is not compute:
it spends trust, and it cannot be retried for free. Nothing in the public
monitoring benchmarks prices it -- they either have no human at all, or the
human is a grading form that ends the episode the moment it is submitted.

This carrier makes three things measurable that a form cannot:

  - **the reply is not instant.** "asked at 3am, answered six hours later" is a
    delay in campaign time, so waiting for a person becomes the longest durable
    wait a loop has to survive.
  - **the reply can be read late.** A loop that sleeps four hours after asking
    leaves a ten-minute reply sitting unread -- so the gap between reply and
    read is the metric, not the reply latency the script already fixes.
  - **the person can stay silent.** No reply is a real outcome, and a loop must
    keep holding rather than treat silence as consent.
"""

from __future__ import annotations

from raven.ops.scripted_human import HumanReply, ScriptedHuman
from raven.ops.simclock import SimClock


def _clock() -> SimClock:
    return SimClock(speed_factor=0)


def test_asking_does_not_return_an_answer_immediately():
    clock = _clock()
    human = ScriptedHuman(clock, replies={"kill-run": HumanReply(after_ms=60_000, answer="kill it")})
    human.ask("kill-run", "loss has climbed for 20 steps; kill or hold?")

    assert human.poll("kill-run") is None, "the person has not replied yet"


def test_the_answer_appears_once_campaign_time_passes_the_scripted_delay():
    clock = _clock()
    human = ScriptedHuman(clock, replies={"kill-run": HumanReply(after_ms=60_000, answer="kill it")})
    human.ask("kill-run", "kill or hold?")

    clock.advance_ms(59_999)
    assert human.poll("kill-run") is None
    clock.advance_ms(1)
    assert human.poll("kill-run") == "kill it"


def test_a_six_hour_reply_is_expressible_in_compressed_time():
    clock = _clock()
    six_hours = 6 * 60 * 60 * 1000
    human = ScriptedHuman(clock, replies={"q": HumanReply(after_ms=six_hours, answer="hold")})
    human.ask("q", "asked at 3am")

    clock.advance_ms(six_hours)
    assert human.poll("q") == "hold"
    assert human.reply_latency_ms("q") == six_hours


def test_silence_is_a_real_outcome_and_never_resolves():
    clock = _clock()
    human = ScriptedHuman(clock, replies={"q": HumanReply(after_ms=None, answer=None)})
    human.ask("q", "anyone there?")

    clock.advance_ms(24 * 60 * 60 * 1000)
    assert human.poll("q") is None
    assert human.unanswered() == ["q"], "silence must not read as consent"


def test_an_unasked_topic_has_no_answer_to_poll():
    human = ScriptedHuman(_clock(), replies={"q": HumanReply(after_ms=0, answer="yes")})
    assert human.poll("q") is None, "a reply exists only in response to a question"


def test_a_reply_read_long_after_it_arrived_is_charged_as_an_unread_gap():
    clock = _clock()
    human = ScriptedHuman(clock, replies={"q": HumanReply(after_ms=10 * 60_000, answer="go")})
    human.ask("q", "kill or hold?")

    clock.advance_ms(4 * 60 * 60 * 1000)  # slept four hours; the reply came at ten minutes
    assert human.poll("q") == "go"
    assert human.unread_gap_ms("q") == 4 * 60 * 60 * 1000 - 10 * 60_000
    assert human.wait_ms("q") == 4 * 60 * 60 * 1000


def test_reading_a_reply_promptly_leaves_almost_no_unread_gap():
    clock = _clock()
    human = ScriptedHuman(clock, replies={"q": HumanReply(after_ms=60_000, answer="go")})
    human.ask("q", "kill or hold?")
    clock.advance_ms(61_000)
    human.poll("q")

    assert human.unread_gap_ms("q") == 1_000


def test_asking_the_same_topic_twice_is_counted_as_a_duplicate_interruption():
    clock = _clock()
    human = ScriptedHuman(clock, replies={"q": HumanReply(after_ms=60_000, answer="go")})
    human.ask("q", "kill or hold?")
    clock.advance_ms(5_000)
    human.ask("q", "still waiting -- kill or hold?")

    assert human.ask_count("q") == 2
    assert human.duplicate_asks() == 1, "nagging before a reply spends trust twice"


def test_asking_again_after_a_reply_is_a_new_question_not_a_duplicate():
    clock = _clock()
    human = ScriptedHuman(
        clock,
        replies={"q": HumanReply(after_ms=1_000, answer="go"), "q2": HumanReply(after_ms=1_000, answer="stop")},
    )
    human.ask("q", "first")
    clock.advance_ms(2_000)
    human.poll("q")
    human.ask("q2", "second")

    assert human.duplicate_asks() == 0
    assert human.ask_count() == 2


def test_escalations_carry_the_message_so_report_quality_can_be_graded():
    clock = _clock()
    human = ScriptedHuman(clock, replies={"q": HumanReply(after_ms=0, answer="go")})
    human.ask("q", "loss rose from 0.31 to 0.94 over 20 steps; killing saves 6h")

    (esc,) = human.escalations()
    assert esc.topic == "q"
    assert "0.31" in esc.message, "the same text the report-quality scorer reads"
    assert esc.asked_at_ms == 0


def test_a_topic_with_no_script_falls_back_to_the_default_reply():
    clock = _clock()
    human = ScriptedHuman(clock, default=HumanReply(after_ms=1_000, answer="use your judgement"))
    human.ask("anything", "?")
    clock.advance_ms(1_000)

    assert human.poll("anything") == "use your judgement"


def test_with_no_script_at_all_the_person_stays_silent():
    clock = _clock()
    human = ScriptedHuman(clock)
    human.ask("q", "?")
    clock.advance_ms(10 * 60 * 60 * 1000)

    assert human.poll("q") is None
    assert human.unanswered() == ["q"]


def test_interruption_cost_summarises_a_whole_campaign():
    clock = _clock()
    human = ScriptedHuman(
        clock,
        replies={
            "a": HumanReply(after_ms=60_000, answer="go"),
            "b": HumanReply(after_ms=None, answer=None),
        },
    )
    human.ask("a", "first")
    human.ask("a", "nag")
    human.ask("b", "second")
    clock.advance_ms(120_000)
    human.poll("a")

    cost = human.interruption_cost()
    assert cost["asks"] == 3
    assert cost["topics"] == 2
    assert cost["duplicate_asks"] == 1
    assert cost["answered"] == 1
    assert cost["unanswered"] == 1
    assert cost["unread_gap_ms"] == 60_000
