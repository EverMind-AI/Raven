"""The playbook vocabulary index — a retrieval hint, no longer a trigger.

``TriggerIndex`` normalizes a library's keywords once and substring-matches a
message against them. That is all this module does now.

The decision belongs where the context is. The model sees the playbooks in
``load_playbook``'s description and chooses, exactly as it chooses between
``spawn`` and ``run_subagent_dag``. Keywords still earn a playbook its place in
that description when the library is too big to list whole
(:mod:`raven.playbook.router`) -- they make it *visible*, they no longer make it
*run*.
"""

from __future__ import annotations

from raven.playbook.triggers import normalize
from raven.playbook.types import Triggers


class TriggerIndex:
    """The whole library's vocabulary, substring-matched per message.

    Vocabulary entries are normalized at build time with the same
    :func:`normalize` the query goes through, so matching is a plain ``in``
    check. Library sizes here are tens of playbooks with tens of entries each; a
    scan is microseconds and needs no cleverness.
    """

    def __init__(self, library: dict[str, Triggers]) -> None:
        self._entries: list[tuple[str, str]] = [
            (normalize(entry), pid) for pid, trig in library.items() for entry in trig.keywords if entry.strip()
        ]

    def hit_counts(self, message: str) -> dict[str, int]:
        """How many of each playbook's keywords this message mentions.

        The count, not just the fact: a message matching three of a playbook's
        words is a better fit than one matching a single generic word, and that
        difference is the whole ranking signal
        (:mod:`raven.playbook.router`). ``match`` used to be the only reader and it
        threw the count away, because a funnel only needed "nominated or not".
        """
        text = normalize(message)
        if not text:
            return {}
        counts: dict[str, int] = {}
        for entry, pid in self._entries:
            if entry in text:
                counts[pid] = counts.get(pid, 0) + 1
        return counts

    def match(self, message: str) -> list[str]:
        """Playbook ids this message mentions, first-hit order, deduped."""
        text = normalize(message)
        if not text:
            return []
        hits: list[str] = []
        for entry, pid in self._entries:
            if pid not in hits and entry in text:
                hits.append(pid)
        return hits


__all__ = ["TriggerIndex"]
