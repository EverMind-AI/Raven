"""A scripted person at the other end of an escalation.

Asking a human is the only action in an on-call loop whose cost is not compute.
It spends trust, it cannot be retried for free, and -- unlike every tool call --
the answer does not come back in the same turn. Scoring it needs three things a
grading form cannot provide:

  - the reply arrives after a scripted delay in campaign time, so "asked at 3am,
    answered six hours later" is expressible and waiting for a person becomes
    the longest durable wait the loop has to survive;
  - the reply can be read late, so the gap between arrival and reading is the
    metric -- the scripted latency is fixed and says nothing about the loop;
  - the person can stay silent, and silence must not read as consent.

The world is in-process by nature, like ``ScriptedJobBackend``: an eval that
spans a process restart needs the pending question persisted instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raven.ops.simclock import SimClock

_SILENT = None


@dataclass(frozen=True)
class HumanReply:
    """What the scripted person will say, and how long they take to say it.

    Either field left as None means silence: no reply ever arrives.
    """

    after_ms: int | None = None
    answer: str | None = None

    @property
    def arrives(self) -> bool:
        return self.after_ms is not None and self.answer is not None


@dataclass
class Escalation:
    """One question put to a person, and what became of it.

    ``message`` is the text of the first ask, which is what a report-quality
    scorer reads; follow-ups are kept apart because nagging is charged, not
    graded.
    """

    topic: str
    message: str
    asked_at_ms: int
    reply: HumanReply
    follow_ups: list[str] = field(default_factory=list)
    read_at_ms: int | None = None

    @property
    def asks(self) -> int:
        return 1 + len(self.follow_ups)

    @property
    def replied_at_ms(self) -> int | None:
        if not self.reply.arrives:
            return None
        return self.asked_at_ms + int(self.reply.after_ms or 0)


class ScriptedHuman:
    """The escalation target for an on-call eval.

    A follow-up on a topic that has not been answered yet does not shorten the
    reply: the delay is measured from the first ask. Nagging a person who is
    asleep only spends trust, and the script has to reflect that or a loop that
    polls the human every minute would score the same as one that waits.
    """

    def __init__(
        self,
        clock: SimClock,
        *,
        replies: dict[str, HumanReply] | None = None,
        default: HumanReply | None = None,
    ) -> None:
        self._clock = clock
        self._replies = replies or {}
        self._default = default or HumanReply()
        self._asked: dict[str, Escalation] = {}

    def ask(self, topic: str, message: str) -> None:
        existing = self._asked.get(topic)
        if existing is not None:
            existing.follow_ups.append(message)
            return
        self._asked[topic] = Escalation(
            topic=topic,
            message=message,
            asked_at_ms=self._clock.now_ms(),
            reply=self._replies.get(topic, self._default),
        )

    def poll(self, topic: str) -> str | None:
        """The person's answer, or None if they have not answered yet.

        Reading is what stamps ``read_at_ms``: a poll that finds nothing is not
        a read, so the unread gap measures the loop's attention rather than the
        number of times it looked.
        """
        esc = self._asked.get(topic)
        if esc is None:
            return _SILENT
        replied_at = esc.replied_at_ms
        if replied_at is None or self._clock.now_ms() < replied_at:
            return _SILENT
        if esc.read_at_ms is None:
            esc.read_at_ms = self._clock.now_ms()
        return esc.reply.answer

    # ---- measurement surface (evals read these; the loop never does) ----

    def escalations(self) -> list[Escalation]:
        """One entry per topic raised, in the order it was first raised."""
        return list(self._asked.values())

    def ask_count(self, topic: str | None = None) -> int:
        """Asks on one topic, or across the campaign -- follow-ups included."""
        if topic is None:
            return sum(esc.asks for esc in self._asked.values())
        esc = self._asked.get(topic)
        return esc.asks if esc else 0

    def duplicate_asks(self) -> int:
        """Follow-ups raised on a topic the person had not yet answered.

        Charged separately from a fresh question: it is the same interruption
        twice, and it buys nothing the first ask had not already bought.
        """
        return sum(len(esc.follow_ups) for esc in self._asked.values())

    def reply_latency_ms(self, topic: str) -> int | None:
        """How long the person took. Scripted, so it grades the world, not the
        loop -- it is here to be subtracted from ``wait_ms``."""
        esc = self._asked.get(topic)
        if esc is None or esc.replied_at_ms is None:
            return None
        return esc.replied_at_ms - esc.asked_at_ms

    def wait_ms(self, topic: str) -> int | None:
        """Campaign time from asking to reading the answer."""
        esc = self._asked.get(topic)
        if esc is None or esc.read_at_ms is None:
            return None
        return esc.read_at_ms - esc.asked_at_ms

    def unread_gap_ms(self, topic: str) -> int | None:
        """How long a delivered answer sat unread.

        This is the durable-wait metric. A loop that sleeps four hours after
        escalating leaves a ten-minute reply idle for the rest, and neither the
        reply latency nor the total wait separates that from a person who simply
        took four hours.
        """
        esc = self._asked.get(topic)
        if esc is None or esc.read_at_ms is None or esc.replied_at_ms is None:
            return None
        return max(0, esc.read_at_ms - esc.replied_at_ms)

    def unanswered(self) -> list[str]:
        """Topics with no answer available at this instant -- silence and still
        pending alike, because the loop cannot tell them apart either."""
        now = self._clock.now_ms()
        return [
            esc.topic
            for esc in self._asked.values()
            if esc.replied_at_ms is None or now < esc.replied_at_ms
        ]

    def interruption_cost(self) -> dict[str, int]:
        """Campaign aggregate. ``asks`` is the trust bill; ``unread_gap_ms`` is
        the machine time lost waiting on an answer already given."""
        unanswered = set(self.unanswered())
        gaps = [self.unread_gap_ms(topic) for topic in self._asked]
        return {
            "asks": self.ask_count(),
            "topics": len(self._asked),
            "duplicate_asks": self.duplicate_asks(),
            "answered": len(self._asked) - len(unanswered),
            "unanswered": len(unanswered),
            "unread_gap_ms": sum(gap for gap in gaps if gap is not None),
        }
