"""Which playbooks this turn shows the model, when the library outgrows the prompt.

The library used to be listed whole in ``load_playbook``'s description, which is
right for the handful of playbooks an install starts with and wrong by the time
there are a hundred: each one costs a description plus a parameter table, and all
of it is spent on every turn whether or not anything is relevant.

The narrowing is the same shape as the skill side's
(:class:`~raven.memory_engine.skill_forge.SkillForgeRouter`): over-fetch, then cut
to ``top_k``, with the sizes in config. What is deliberately *not* copied is the
weighted cross-source RRF -- a playbook library has one source, so there is
nothing to fuse -- and the router instance itself: mixing playbooks into the skill
router would put them in competition for the same K slots and change which skills
a turn gets.

**Two costs, handled differently.** The `name` enum stays the *whole* library
(~5 tokens a name), because a recall miss must not become "the model cannot reach
it": the user can still name a playbook outright, and the tool's own error can
list every one. The expensive part -- description, parameter table, which fields
are blank -- is rendered for the top-K only, so it stays flat as the library grows.
"""

from __future__ import annotations

from dataclasses import dataclass

from raven.playbook.matcher import TriggerIndex
from raven.playbook.triggers import normalize
from raven.playbook.types import PlaybookSpec


@dataclass(frozen=True)
class RouterSizes:
    """How wide to look and how much to keep."""

    top_k: int = 5
    over_fetch_factor: int = 2

    @property
    def over_fetch(self) -> int:
        return max(self.top_k, self.top_k * max(1, self.over_fetch_factor))


def _score(spec: PlaybookSpec, text: str, keyword_hits: int) -> int:
    """How strongly one playbook answers this message.

    Trigger keywords are the author's own account of when their playbook applies,
    so a keyword hit is the strongest signal available without a model call; the
    count matters because a message matching three of a playbook's words is a
    better fit than one matching a single generic word. The description is scanned
    too, at a lower weight, so a playbook whose vocabulary is thin is still
    reachable by what it says it does.

    The keyword half is counted by :class:`TriggerIndex`, which normalized the
    whole library's vocabulary once at load. Re-normalizing it here would repeat
    that for every keyword of every playbook on every turn -- two thousand calls a
    turn on a hundred-playbook library, to reach the answer the index already has.

    Deliberately not an embedding lookup: this runs on every turn, ahead of the
    model, and the same reasoning that kept the vocabulary scan free applies here.
    Retrieval failure is no longer a lost feature either -- it only costs a line in
    a tool description, and the enum still holds every name.
    """
    if keyword_hits:
        return keyword_hits * 10
    words = [w for w in normalize(spec.description).split() if len(w) > 3]
    return sum(1 for w in words if w in text)


def select_playbooks(
    specs: dict[str, PlaybookSpec],
    message: str,
    *,
    index: TriggerIndex | None = None,
    sizes: RouterSizes | None = None,
) -> list[str]:
    """The playbook names to describe in full this turn, best first.

    A library at or under ``top_k`` is returned whole -- narrowing it would spend
    a scan to drop nothing. Above that, the ranking is by score and then by name,
    so the list is deterministic: a model that saw one ordering last turn is not
    handed a reshuffled one for the same message.

    Never returns an empty list for a non-empty library. A message matching
    nothing still gets ``top_k`` names, because "here are some of the playbooks
    you have" is more useful than silence, and the alternative -- rendering
    nothing until a keyword hits -- is how a playbook whose vocabulary misses
    becomes invisible.
    """
    sizes = sizes or RouterSizes()
    names = sorted(specs)
    if len(names) <= sizes.top_k:
        return names
    text = normalize(message or "")
    hits = (index or TriggerIndex({pid: spec.triggers for pid, spec in specs.items()})).hit_counts(message or "")
    ranked = sorted(names, key=lambda n: (-_score(specs[n], text, hits.get(n, 0)), n))
    return ranked[: sizes.over_fetch][: sizes.top_k]


__all__ = ["RouterSizes", "select_playbooks"]
