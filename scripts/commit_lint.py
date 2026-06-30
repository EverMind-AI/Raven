from __future__ import annotations

import re
from dataclasses import dataclass

ALLOWED_TYPES = {
    "feat",
    "fix",
    "docs",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
}

HEADER_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9_*.-]+)\))?: (?P<subject>.+)$")


@dataclass(frozen=True)
class CommitLintConfig:
    subject_limit: int = 72
    pr_title_subject_limit: int = 90


@dataclass(frozen=True)
class LintResult:
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def check_commit_message(message: str, config: CommitLintConfig | None = None) -> LintResult:
    return _check_message(message, subject_limit=(config or CommitLintConfig()).subject_limit)


def check_pr_title(title: str, config: CommitLintConfig | None = None) -> LintResult:
    return _check_message(
        title, subject_limit=(config or CommitLintConfig()).pr_title_subject_limit
    )


def _check_message(message: str, *, subject_limit: int) -> LintResult:
    errors: list[str] = []
    text = message.strip()
    header = text.splitlines()[0] if text else ""

    if header.startswith(("fixup!", "squash!")):
        errors.append("fixup/squash commits are not allowed")
        return LintResult(errors)

    if header.startswith("Merge "):
        errors.append("merge commits are not allowed in PR ranges")
        return LintResult(errors)

    if not _is_ascii(text):
        errors.append("must be ASCII-only English")

    match = HEADER_RE.match(header)
    if not match:
        errors.append("must match <type>(<scope>): <subject>")
        return LintResult(errors)

    commit_type = match.group("type")
    subject = match.group("subject")

    if commit_type not in ALLOWED_TYPES:
        errors.append(f"type must be one of: {', '.join(sorted(ALLOWED_TYPES))}")

    if len(subject) > subject_limit:
        errors.append(f"subject must be {subject_limit} characters or fewer")

    first_alpha = next((ch for ch in subject if ch.isalpha()), "")
    if first_alpha and not first_alpha.islower():
        errors.append("subject must start lowercase")

    if subject.endswith((".", "!", "?")):
        errors.append("subject must not end with punctuation")

    return LintResult(errors)


def _is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True
