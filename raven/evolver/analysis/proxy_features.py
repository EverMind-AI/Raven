"""Per-trial cheap metadata extraction for downstream stratification.

Reads a legacy-runner trial dir and emits a :class:`ProxyFeatures` dataclass per
trial. The features are cheap to compute (parse one session.jsonl + one
result.json, no replay, no LLM, no container) and stable enough to feed
into the cold-start bandit's K-means sub-strata on the ``stable_fail`` 0/3
bucket — where ~70% of tasks land under v7 and where the bandit needs to
slice the population for cohort selection.

Feature set (all per-trial, all O(session.jsonl)):

- ``turn_count``               — assistant turns with at least one tool call
- ``final_exit_status``        — categorical (:class:`ExitStatus`)
- ``has_tool_calls_ever``      — bool: did the agent ever invoke a tool
- ``assistant_text_length_avg``— mean length (chars) of assistant.content across all assistant messages
- ``docker_error_count``       — count of docker-error patterns across tool responses + exception traceback

The docker-error count is a noise indicator (container-side issues that
surface as docker daemon errors during exec). It is intentionally loose
since per-trial occurrences should be rare; spikes indicate infra issues
worth filtering out before bandit stratification.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "ExitStatus",
    "ProxyFeatures",
    "extract_features",
    "extract_trial_dir",
]


class ExitStatus(str, Enum):
    """Outcome of a trial as observed at the harness level.

    ``PASSED`` / ``FAILED_VERIFIER`` are the normal exit modes; the rest
    encode different failure modes the bandit may want to filter out
    (e.g. wall-cap-bound tasks shouldn't be in the same K-means cluster
    as agent-decision-bound tasks).
    """

    PASSED = "passed"
    FAILED_VERIFIER = "failed_verifier"
    AGENT_TIMEOUT = "agent_timeout"
    VERIFIER_TIMEOUT = "verifier_timeout"
    REWARD_FILE_NOT_FOUND = "reward_file_not_found"
    RUNTIME_ERROR = "runtime_error"
    NO_SESSION = "no_session"
    OTHER = "other"


@dataclass(frozen=True)
class ProxyFeatures:
    trial_id: str
    task_id: str
    turn_count: int
    final_exit_status: ExitStatus
    has_tool_calls_ever: bool
    assistant_text_length_avg: float
    docker_error_count: int


# Docker / container daemon errors most commonly seen inside exec tool output
# or in the runner's exception traceback when something blows up at the infra layer.
_DOCKER_ERROR_PATTERNS = re.compile(
    r"(?:"
    r"docker:\s+error\b"
    r"|cannot\s+connect\s+to\s+the\s+docker\s+daemon"
    r"|error\s+response\s+from\s+daemon"
    r"|no\s+such\s+container"
    r"|container\s+not\s+running"
    r"|docker\.errors\."
    r")",
    re.IGNORECASE,
)


_EXCEPTION_TO_STATUS = {
    "AgentTimeoutError": ExitStatus.AGENT_TIMEOUT,
    "VerifierTimeoutError": ExitStatus.VERIFIER_TIMEOUT,
    "RewardFileNotFoundError": ExitStatus.REWARD_FILE_NOT_FOUND,
    "RuntimeError": ExitStatus.RUNTIME_ERROR,
}


def _classify_exit(result_json: dict, reward_passed: bool | None) -> ExitStatus:
    """Map result.json's exception_info / reward.txt into an ExitStatus."""
    if reward_passed is True:
        return ExitStatus.PASSED
    exc = (result_json.get("exception_info") or {}).get("exception_type")
    if exc in _EXCEPTION_TO_STATUS:
        return _EXCEPTION_TO_STATUS[exc]
    if reward_passed is False:
        return ExitStatus.FAILED_VERIFIER
    # no reward and no recognised exception → bucket as OTHER
    if exc:
        return ExitStatus.OTHER
    return ExitStatus.NO_SESSION


def _read_reward(trial_dir: Path) -> bool | None:
    rt = trial_dir / "verifier" / "reward.txt"
    if not rt.exists():
        return None
    try:
        return float(rt.read_text().strip()) >= 1.0
    except (ValueError, OSError):
        return None


def _read_result_json(trial_dir: Path) -> dict:
    rj = trial_dir / "result.json"
    if not rj.exists():
        return {}
    try:
        return json.loads(rj.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _session_path(trial_dir: Path) -> Path | None:
    """Locate the agent's session.jsonl file under a trial dir."""
    sessions_dir = trial_dir / "agent" / "workspace" / "sessions"
    if not sessions_dir.is_dir():
        return None
    # the legacy runner writes a single tb2-task.jsonl by convention; fall back to
    # any .jsonl if name differs.
    preferred = sessions_dir / "tb2-task.jsonl"
    if preferred.exists():
        return preferred
    candidates = sorted(sessions_dir.glob("*.jsonl"))
    return candidates[0] if candidates else None


def _scan_session(session_path: Path) -> tuple[int, bool, float, int]:
    """Walk session.jsonl once and emit
    ``(turn_count, has_tool_calls_ever, assistant_text_length_avg, docker_error_count)``.

    Iteration is a single pass over the file; each line is parsed once
    and three counters are updated. Returns 0/False/0.0/0 for an empty
    or unreadable session.
    """
    turn_count = 0
    has_tool_calls_ever = False
    assistant_lengths: list[int] = []
    docker_errors = 0

    try:
        with session_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = r.get("role")
                if role == "assistant":
                    content = r.get("content") or ""
                    assistant_lengths.append(len(content))
                    if r.get("tool_calls"):
                        turn_count += 1
                        has_tool_calls_ever = True
                elif role == "tool":
                    content = r.get("content") or ""
                    docker_errors += len(_DOCKER_ERROR_PATTERNS.findall(content))
    except OSError:
        pass

    avg_len = (sum(assistant_lengths) / len(assistant_lengths)) if assistant_lengths else 0.0
    return turn_count, has_tool_calls_ever, avg_len, docker_errors


def _trial_task_id(trial_name: str) -> str:
    return trial_name.rsplit("__", 1)[0] if "__" in trial_name else trial_name


def extract_features(trial_dir: str | Path) -> ProxyFeatures:
    """Extract :class:`ProxyFeatures` from a single trial dir.

    Raises :class:`FileNotFoundError` when ``trial_dir`` does not exist
    or is not a directory.
    """
    p = Path(trial_dir)
    if not p.is_dir():
        raise FileNotFoundError(p)

    result = _read_result_json(p)
    reward_passed = _read_reward(p)

    session = _session_path(p)
    if session is None:
        turn_count = 0
        has_tool_calls_ever = False
        avg_len = 0.0
        docker_errors_session = 0
    else:
        turn_count, has_tool_calls_ever, avg_len, docker_errors_session = _scan_session(session)

    # also scan exception traceback for docker errors
    exc_tb = (result.get("exception_info") or {}).get("exception_traceback") or ""
    docker_errors = docker_errors_session + len(_DOCKER_ERROR_PATTERNS.findall(exc_tb))

    final = _classify_exit(result, reward_passed)

    return ProxyFeatures(
        trial_id=p.name,
        task_id=_trial_task_id(p.name),
        turn_count=turn_count,
        final_exit_status=final,
        has_tool_calls_ever=has_tool_calls_ever,
        assistant_text_length_avg=avg_len,
        docker_error_count=docker_errors,
    )


def _find_attempt_root(trial_dir: Path) -> Path:
    """Same logic as stability_bucket: discriminate by ``verifier/`` subdir."""
    if not trial_dir.is_dir():
        raise NotADirectoryError(trial_dir)
    has_trial_children = any(p.is_dir() and "__" in p.name and (p / "verifier").is_dir() for p in trial_dir.iterdir())
    if has_trial_children:
        return trial_dir
    nested = [p for p in trial_dir.iterdir() if p.is_dir()]
    if len(nested) == 1:
        return nested[0]
    return trial_dir


def extract_trial_dir(trial_dir: str | Path) -> dict[str, ProxyFeatures]:
    """Extract :class:`ProxyFeatures` for every trial under ``trial_dir``.

    Accepts either the legacy jobs_dir or the dated subdir; returns
    ``{trial_id: ProxyFeatures}``.
    """
    root = _find_attempt_root(Path(trial_dir))
    out: dict[str, ProxyFeatures] = {}
    for d in sorted(root.iterdir()):
        if not d.is_dir() or "__" not in d.name:
            continue
        if not (d / "result.json").exists():
            continue
        if not (d / "verifier").is_dir():
            continue
        out[d.name] = extract_features(d)
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    from collections import Counter

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trial-dir", required=True, help="legacy jobs_dir or dated subdir")
    ap.add_argument("--json", default=None, help="optional JSON dump path")
    args = ap.parse_args(argv)

    feats = extract_trial_dir(args.trial_dir)
    by_status = Counter(f.final_exit_status.value for f in feats.values())

    print(f"trial_dir: {args.trial_dir}")
    print(f"trials observed: {len(feats)}")
    print("\nExit status breakdown:")
    for status, n in sorted(by_status.items(), key=lambda x: -x[1]):
        print(f"  {status:24s} {n}")

    print("\nTurn count quantiles (incl. wall-cap-zero):")
    turns = sorted(f.turn_count for f in feats.values())
    if turns:
        for q, p in (("min", 0.0), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("max", 1.0)):
            idx = min(len(turns) - 1, int(p * len(turns)))
            print(f"  {q}: {turns[idx]}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(
                {
                    tid: {
                        "task_id": x.task_id,
                        "turn_count": x.turn_count,
                        "final_exit_status": x.final_exit_status.value,
                        "has_tool_calls_ever": x.has_tool_calls_ever,
                        "assistant_text_length_avg": x.assistant_text_length_avg,
                        "docker_error_count": x.docker_error_count,
                    }
                    for tid, x in sorted(feats.items())
                },
                f,
                indent=2,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
