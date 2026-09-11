"""Safety policy shared by every Hub skill path, at two strengths.

The install strength (:meth:`SkillPolicy.refusal_for_detail`) gates the
two paths that put a body to work — the ``SkillsSegmentBuilder``
post-gate hydrate (auto-inject) and the ``use_skill`` tool - before any
``SkillHubClient.install``:

- the ``score_safety`` bar. Catalog search payloads omit the score, so
  the authoritative check runs on the *detail* metadata (already fetched
  by both paths); a missing score passes to keep score-less deployments
  working.
- the operator blocklist (``skillForge.blocklist``), matched
  case-insensitively against any known identifier of the skill
  (name / slug / native id).
- an external-home-directory lint over the skill body: a hub skill whose
  instructions point at another product's dotdir (``~/.openclaw`` and
  friends) is refused rather than rewritten.
- the ``skillForge.autoInstall`` consent gate over the bundle download
  itself (``auto`` / ``prompt`` / ``off``), consulted right before an
  install would start.

The read strength (:meth:`SkillPolicy.refusal_for_read`) drops the body
lint and keeps the rest. ``read_skill`` installs nothing and hands the
body back wrapped as untrusted data, and the scent menu advertises hub
skills on the identity checks alone — so linting the read too would
recommend skills by id that can never be read.

Stdlib-only on purpose: this module ships with the client when the
package is extracted for reuse outside Raven.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable


def normalize_blocklist(names: Iterable[str] | None) -> frozenset[str]:
    """Casefold + trim a user-supplied blocklist into a match set."""
    if not names:
        return frozenset()
    return frozenset(n.strip().casefold() for n in names if n and n.strip())


def is_blocked(blocklist: frozenset[str], *identifiers: str | None) -> bool:
    """True when any identifier of the skill is on the blocklist."""
    if not blocklist:
        return False
    return any(i is not None and i.casefold() in blocklist for i in identifiers)


def refuses_low_safety(score: object, min_safety: float) -> bool:
    """True when a present, parseable ``score_safety`` is below the bar.

    A missing or malformed score passes: catalog/detail payloads without
    scores are a supported deployment shape, and the guard must not turn
    them into a hard outage.
    """
    if score is None:
        return False
    try:
        return float(score) < min_safety  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


# ``~/.foo`` / ``$HOME/.foo`` / ``/Users/x/.foo`` / ``/home/x/.foo`` —
# any home dotdir reference in a skill body, with up to two segments
# under it. Only ``~/.raven`` is ours; everything else is another
# product's (or the user's private) data.
_EXTERNAL_HOME_RE = re.compile(
    r"(?:~|\$HOME|/Users/[A-Za-z0-9_][\w.-]*|/home/[A-Za-z0-9_][\w.-]*)"
    r"/\.(?P<root>[A-Za-z0-9][\w-]*)"
    r"(?:/(?P<child>[A-Za-z0-9][\w.-]*))?"
    r"(?:/(?P<grandchild>[A-Za-z0-9][\w.-]*))?",
)
_ALLOWED_HOME_DOTDIRS = frozenset({"raven"})

# Dotdirs that host other products rather than being one: ``~/.config`` is
# where everyone's settings live, so the segment naming whose data it is sits
# one further down. Reporting the root alone refuses a skill for mentioning
# XDG at all, which is what made a clean skill unreadable.
_GENERIC_HOME_ROOTS = frozenset({"config", "cache", "local", "share", "state"})


def lint_external_paths(text: str | None) -> list[str]:
    """Foreign home-dotdir references found in a skill body, deduped and
    sorted (e.g. ``["~/.openclaw"]``). Empty list = clean.

    Under a generic root the reported path runs down to the segment that
    names the owning product (``~/.config/openclaw``), because the root by
    itself belongs to nobody.
    """
    if not text:
        return []
    found: set[str] = set()
    for m in _EXTERNAL_HOME_RE.finditer(text):
        segments = [g for g in m.group("root", "child", "grandchild") if g]
        owner = 0
        while owner < len(segments) and segments[owner].casefold() in _GENERIC_HOME_ROOTS:
            owner += 1
        if owner == len(segments) or segments[owner].casefold() in _ALLOWED_HOME_DOTDIRS:
            continue
        found.add("~/." + "/".join(segments[: owner + 1]))
    return sorted(found)


@dataclass(frozen=True)
class SkillPolicy:
    """The single install-policy decision both Hub install paths consult."""

    min_safety: float = 0.7
    blocklist: frozenset[str] = field(default_factory=frozenset)
    auto_install: str = "auto"
    _prompt_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    @classmethod
    def create(
        cls,
        *,
        min_safety: float = 0.7,
        blocklist: Iterable[str] | None = None,
        auto_install: str = "auto",
    ) -> "SkillPolicy":
        return cls(
            min_safety=min_safety,
            blocklist=normalize_blocklist(blocklist),
            auto_install=auto_install,
        )

    async def install_skip_reason(self, name: str) -> str | None:
        """Consent reason to skip a Hub bundle download, or ``None`` to
        proceed (``skillForge.autoInstall``).

        ``off`` always skips; ``prompt`` asks on an interactive stdin and
        behaves like ``off`` when no TTY is attached or the prompt is
        declined; any other value (``auto`` included) proceeds. Distinct
        from :meth:`refusal_for_detail`: this is operator consent for the
        download itself, not a safety verdict on the skill.

        Async on purpose: both install paths run on the agent's shared
        event loop, so the blocking stdin read happens on a worker thread
        (``asyncio.to_thread``); the per-policy lock keeps concurrently
        gathered hydrates from interleaving two prompts on one stdin.
        """
        if self.auto_install == "off":
            return f"skill {name!r}: skillForge.autoInstall is 'off'"
        if self.auto_install == "prompt":
            try:
                interactive = sys.stdin is not None and sys.stdin.isatty()
            except (AttributeError, ValueError):
                interactive = False
            if not interactive:
                return f"skill {name!r}: skillForge.autoInstall is 'prompt' but no interactive terminal is attached"
            async with self._prompt_lock:
                answer = await asyncio.to_thread(input, f"Install skill {name!r} from the Skill Hub? [y/N] ")
            if answer.strip().casefold() in ("y", "yes"):
                return None
            return f"skill {name!r}: install declined at the autoInstall prompt"
        return None

    def _refusal_for_identity(
        self,
        meta: dict[str, Any],
        extra_identifiers: tuple[str | None, ...],
    ) -> str | None:
        """Blocklist then safety bar — the checks that hold at every
        strength, because they are about the skill rather than its body."""
        slug = str(next((i for i in (meta.get("slug"), meta.get("name"), *extra_identifiers) if i), "?"))
        if is_blocked(
            self.blocklist,
            meta.get("slug"),
            meta.get("name"),
            meta.get("skill_id"),
            *extra_identifiers,
        ):
            return f"skill {slug!r} is on the operator blocklist (skillForge.blocklist)"
        score = meta.get("score_safety")
        if refuses_low_safety(score, self.min_safety):
            return f"skill {slug!r}: score_safety {score} is below the configured minimum {self.min_safety}"
        return None

    def refusal_for_read(
        self,
        meta: dict[str, Any],
        *extra_identifiers: str | None,
    ) -> str | None:
        """Refusal reason for *reading* a hub skill's body, or ``None``.

        Blocklist and safety bar only. The body lint is deliberately absent:
        a read installs nothing and returns the text wrapped as untrusted
        data, and the scent menu advertises hub skills on these same two
        checks — so refusing a read here would put an id in front of the
        model that no call can ever resolve. Use
        :meth:`refusal_for_detail` wherever the body is about to be
        installed or injected as instructions.
        """
        return self._refusal_for_identity(meta, extra_identifiers)

    def refusal_for_detail(
        self,
        meta: dict[str, Any],
        *extra_identifiers: str | None,
    ) -> str | None:
        """Refusal reason for a hub skill's detail metadata, or ``None`` to
        allow. Checks blocklist, then the safety bar, then the body lint."""
        refusal = self._refusal_for_identity(meta, extra_identifiers)
        if refusal is not None:
            return refusal
        flagged = lint_external_paths(meta.get("skill_md"))
        if flagged:
            slug = str(next((i for i in (meta.get("slug"), meta.get("name"), *extra_identifiers) if i), "?"))
            return f"skill {slug!r} references external home directories: {', '.join(flagged)}"
        return None


__all__ = [
    "SkillPolicy",
    "is_blocked",
    "lint_external_paths",
    "normalize_blocklist",
    "refuses_low_safety",
]
