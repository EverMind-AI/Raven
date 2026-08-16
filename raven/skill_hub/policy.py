"""Install-time safety policy shared by both Hub install paths.

The two call sites — the ``SkillsSegmentBuilder`` post-gate hydrate
(auto-inject) and the ``use_skill`` tool — route every Hub skill through
one :class:`SkillPolicy` decision before any ``SkillHubClient.install``:

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

Stdlib-only on purpose: this module ships with the client when the
package is extracted for reuse outside Raven.
"""

from __future__ import annotations

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
# any home dotdir reference in a skill body. Only ``~/.raven`` is ours;
# everything else is another product's (or the user's private) data.
_EXTERNAL_HOME_RE = re.compile(
    r"(?:~|\$HOME|/Users/[A-Za-z0-9_][\w.-]*|/home/[A-Za-z0-9_][\w.-]*)/\.(?P<name>[A-Za-z0-9][\w-]*)",
)
_ALLOWED_HOME_DOTDIRS = frozenset({"raven"})


def lint_external_paths(text: str | None) -> list[str]:
    """Foreign home-dotdir references found in a skill body, deduped and
    sorted (e.g. ``["~/.openclaw"]``). Empty list = clean."""
    if not text:
        return []
    found = {
        f"~/.{m.group('name')}"
        for m in _EXTERNAL_HOME_RE.finditer(text)
        if m.group("name").casefold() not in _ALLOWED_HOME_DOTDIRS
    }
    return sorted(found)


@dataclass(frozen=True)
class SkillPolicy:
    """The single install-policy decision both Hub install paths consult."""

    min_safety: float = 0.7
    blocklist: frozenset[str] = field(default_factory=frozenset)
    auto_install: str = "auto"

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

    def install_skip_reason(self, name: str) -> str | None:
        """Consent reason to skip a Hub bundle download, or ``None`` to
        proceed (``skillForge.autoInstall``).

        ``off`` always skips; ``prompt`` asks on an interactive stdin and
        behaves like ``off`` when no TTY is attached or the prompt is
        declined; any other value (``auto`` included) proceeds. Distinct
        from :meth:`refusal_for_detail`: this is operator consent for the
        download itself, not a safety verdict on the skill.
        """
        if self.auto_install == "off":
            return f"skill {name!r}: skillForge.autoInstall is 'off'"
        if self.auto_install == "prompt":
            try:
                interactive = sys.stdin is not None and sys.stdin.isatty()
            except (AttributeError, ValueError):
                interactive = False
            if not interactive:
                return (
                    f"skill {name!r}: skillForge.autoInstall is 'prompt' "
                    "but no interactive terminal is attached"
                )
            answer = input(f"Install skill {name!r} from the Skill Hub? [y/N] ")
            if answer.strip().casefold() in ("y", "yes"):
                return None
            return f"skill {name!r}: install declined at the autoInstall prompt"
        return None

    def refusal_for_detail(
        self,
        meta: dict[str, Any],
        *extra_identifiers: str | None,
    ) -> str | None:
        """Refusal reason for a hub skill's detail metadata, or ``None`` to
        allow. Checks blocklist, then the safety bar, then the body lint."""
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
        flagged = lint_external_paths(meta.get("skill_md"))
        if flagged:
            return f"skill {slug!r} references external home directories: {', '.join(flagged)}"
        return None


__all__ = [
    "SkillPolicy",
    "is_blocked",
    "lint_external_paths",
    "normalize_blocklist",
    "refuses_low_safety",
]
