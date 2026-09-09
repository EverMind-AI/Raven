"""Trajectory regression cases — replay a cassette, assert the divergence direction.

A Trajectory Regression Case turns a fixed harness bug into a permanent CI
guard: one directory holding a Trajectory Cassette (``cassette/``) and an
expectation file (``expect.yaml``). Running the case replays the cassette
through the live harness and asserts *where* the replay departs from the
recording and *what the live side does* there. After a bug fix the harness
necessarily diverges from a recording made by the buggy code, so the natural
assertion shape is "the first divergence is at the expected call and the live
value is the fixed behavior" — not "zero divergence" (though a case may assert
that too, to guard faithful reproduction).

Expectation file (YAML)::

    mode: strict              # optional: strict (default) | warn
    divergence:               # omit (or null) to expect zero divergence
      kind: llm               # llm | tool — the replay's FIRST divergence
      index: 0                # 0-based call index within its kind
      field: messages[1]      # optional: the diverging field name
    checks:                   # optional assertions on the live side
      - call: llm
        index: 0              # 0-based llm call index
        message: -1           # message index in that call's live request
                              # (optional, default -1: the newest message)
        op: contains          # contains | not_contains | equals
        value: "fixed text"
      - call: tool
        index: 0              # 0-based tool call index
        op: params_equal      # params_equal | name_equals
        value: {path: a.txt}

Checks read the live requests the replay captured
(:attr:`raven.trajectory.replay.ReplayReport.llm_requests` /
``tool_requests``), so they can assert actual values at and before the
divergence point even when strict mode halted there. Failures are returned as
human-readable strings, one per unmet expectation.

Case metadata (YAML, ``case.yaml`` next to ``expect.yaml``)::

    issue: https://github.com/org/repo/issues/123   # the bug this case guards
    owner: someone                                  # who answers for the case
    why: the contract the assertions protect        # why it must (not) diverge
    re_record: when the baseline may be re-recorded
    risk: low                                       # optional: low|medium|high
    created_from: att-20260101-abcdef               # optional: source bundle id
    reviewed_residuals:                             # optional: findings a human
      - sha256: <64-hex digest of the full token>   # inspected and vouches benign
        note: identifier-shaped prose, not a credential

The four leading fields are required and must be non-blank — the scaffold
writes them empty on purpose, so a case cannot pass validation until a human
fills in the real bug link, owner, and rationale.

:func:`validate_case` is the static commit gate for one case directory (never
replays): both schemas, cassette completeness down to the replay contract
(valid JSON is not yet a usable payload), residual-scan coverage and
cleanliness, and the size budget. :func:`discover_case_dirs` deliberately
lists *every* subdirectory of the cases root, so a directory missing its
``expect.yaml`` is reported broken instead of silently skipped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from raven.trajectory.cassette import replayability_problems
from raven.trajectory.redact import scan_residuals
from raven.trajectory.replay import REPLAY_MODES, ReplayReport, load_recording, run_replay

EXPECTATION_FILE = "expect.yaml"
CASE_FILE = "case.yaml"
CASSETTE_DIR = "cassette"

MAX_FILE_BYTES = 256 * 1024
MAX_CASE_BYTES = 1024 * 1024

_TOP_KEYS = {"mode", "divergence", "checks"}
_CASE_KEYS = {"issue", "owner", "why", "re_record", "risk", "created_from", "reviewed_residuals"}
_REVIEWED_KEYS = {"sha256", "note"}
_CASE_REQUIRED = ("issue", "owner", "why", "re_record")
_RISK_LEVELS = ("low", "medium", "high")
_DIVERGENCE_KEYS = {"kind", "index", "field"}
_CHECK_KEYS = {"call", "index", "op", "value", "message"}
_KINDS = ("llm", "tool")
_LLM_OPS = ("contains", "not_contains", "equals")
_TOOL_OPS = ("params_equal", "name_equals")


@dataclass(frozen=True)
class DivergenceExpectation:
    """Where the replay's first divergence must land."""

    kind: str
    index: int
    field: str | None = None

    def render(self) -> str:
        suffix = f" on field {self.field!r}" if self.field else ""
        return f"{self.kind} call #{self.index + 1}{suffix}"


@dataclass(frozen=True)
class Check:
    """One assertion against a captured live request."""

    call: str
    index: int
    op: str
    value: Any
    message: int = -1


@dataclass(frozen=True)
class RegressionExpectation:
    """The parsed ``expect.yaml`` of one regression case."""

    mode: str = "strict"
    divergence: DivergenceExpectation | None = None
    checks: tuple[Check, ...] = ()


def _require(condition: bool, where: str, problem: str) -> None:
    if not condition:
        raise ValueError(f"{where}: {problem}")


def _require_str_keys(where: str, data: dict) -> None:
    # Guards every unknown-key sorted() over external YAML: a mixed int/str
    # key set would raise TypeError instead of reporting a problem.
    _require(all(isinstance(key, str) for key in data), where, "mapping keys must be strings")


def _parse_divergence(where: str, data: Any) -> DivergenceExpectation:
    _require(isinstance(data, dict), where, f"divergence must be a mapping, got {type(data).__name__}")
    _require_str_keys(where, data)
    unknown = set(data) - _DIVERGENCE_KEYS
    _require(not unknown, where, f"unknown divergence key(s) {sorted(unknown)}; allowed: {sorted(_DIVERGENCE_KEYS)}")
    kind = data.get("kind")
    _require(kind in _KINDS, where, f"divergence.kind must be one of {_KINDS}, got {kind!r}")
    index = data.get("index")
    _require(
        isinstance(index, int) and not isinstance(index, bool) and index >= 0,
        where,
        f"divergence.index must be a 0-based integer, got {index!r}",
    )
    field = data.get("field")
    _require(field is None or isinstance(field, str), where, f"divergence.field must be a string, got {field!r}")
    return DivergenceExpectation(kind=kind, index=index, field=field)


def _parse_check(where: str, pos: int, data: Any) -> Check:
    where = f"{where}: checks[{pos}]"
    _require(isinstance(data, dict), where, f"must be a mapping, got {type(data).__name__}")
    _require_str_keys(where, data)
    unknown = set(data) - _CHECK_KEYS
    _require(not unknown, where, f"unknown key(s) {sorted(unknown)}; allowed: {sorted(_CHECK_KEYS)}")
    call = data.get("call")
    _require(call in _KINDS, where, f"call must be one of {_KINDS}, got {call!r}")
    index = data.get("index")
    _require(
        isinstance(index, int) and not isinstance(index, bool) and index >= 0,
        where,
        f"index must be a 0-based integer, got {index!r}",
    )
    op = data.get("op")
    _require("value" in data, where, "value is required")
    if call == "llm":
        _require(op in _LLM_OPS, where, f"llm op must be one of {_LLM_OPS}, got {op!r}")
        message = data.get("message", -1)
        _require(
            isinstance(message, int) and not isinstance(message, bool),
            where,
            f"message must be an integer index, got {message!r}",
        )
        if op in ("contains", "not_contains"):
            _require(isinstance(data["value"], str), where, f"{op} value must be a string")
        return Check(call=call, index=index, op=op, value=data["value"], message=message)
    _require(op in _TOOL_OPS, where, f"tool op must be one of {_TOOL_OPS}, got {op!r}")
    _require("message" not in data, where, "message applies to llm checks only")
    return Check(call=call, index=index, op=op, value=data["value"])


def _load_yaml_mapping(path: Path, what: str) -> dict[str, Any]:
    """``path`` parsed as a YAML mapping; a YAML syntax error raises the same
    file-naming ``ValueError`` malformed fields do, so callers collecting
    problems need exactly one exception type for expected parse failures."""
    where = str(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{where}: cannot be parsed as YAML: {exc}") from exc
    if data is None:
        data = {}
    _require(isinstance(data, dict), where, f"{what} must be a mapping, got {type(data).__name__}")
    _require_str_keys(where, data)
    return data


def load_expectation(path: Path) -> RegressionExpectation:
    """Parse and validate one ``expect.yaml``; raises ``ValueError`` on any
    unknown key, malformed field, or YAML syntax error, naming the file and
    the problem."""
    path = Path(path)
    where = str(path)
    data = _load_yaml_mapping(path, "expectation")
    unknown = set(data) - _TOP_KEYS
    _require(not unknown, where, f"unknown key(s) {sorted(unknown)}; allowed: {sorted(_TOP_KEYS)}")
    mode = data.get("mode", "strict")
    _require(mode in REPLAY_MODES, where, f"mode must be one of {REPLAY_MODES}, got {mode!r}")
    divergence = None if data.get("divergence") is None else _parse_divergence(where, data["divergence"])
    raw_checks = data.get("checks") or []
    _require(isinstance(raw_checks, list), where, f"checks must be a list, got {type(raw_checks).__name__}")
    checks = tuple(_parse_check(where, pos, c) for pos, c in enumerate(raw_checks))
    return RegressionExpectation(mode=mode, divergence=divergence, checks=checks)


def _excerpt(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _content_text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _run_check(report: ReplayReport, pos: int, check: Check) -> str | None:
    label = f"checks[{pos}] ({check.call} call #{check.index + 1}, {check.op})"
    if check.call == "llm":
        if check.index >= len(report.llm_requests):
            return f"{label}: the harness made only {len(report.llm_requests)} llm call(s)"
        messages = report.llm_requests[check.index]["messages"]
        try:
            msg = messages[check.message]
        except IndexError:
            return f"{label}: message index {check.message} is out of range ({len(messages)} message(s))"
        content = msg.get("content") if isinstance(msg, dict) else msg
        text = _content_text(content)
        if check.op == "contains" and check.value not in text:
            return f"{label}: message[{check.message}] does not contain {check.value!r}; content: {_excerpt(text)!r}"
        if check.op == "not_contains" and check.value in text:
            return (
                f"{label}: message[{check.message}] must not contain {check.value!r},"
                f" but does; content: {_excerpt(text)!r}"
            )
        if check.op == "equals" and content != check.value:
            return (
                f"{label}: message[{check.message}] differs;"
                f" expected {_excerpt(_content_text(check.value))!r}, got {_excerpt(text)!r}"
            )
        return None
    if check.index >= len(report.tool_requests):
        return f"{label}: the harness made only {len(report.tool_requests)} tool call(s)"
    request = report.tool_requests[check.index]
    if check.op == "name_equals" and request["name"] != check.value:
        return f"{label}: expected tool name {check.value!r}, got {request['name']!r}"
    if check.op == "params_equal" and request["params"] != check.value:
        return (
            f"{label}: params differ; expected {_excerpt(_content_text(check.value))!r},"
            f" got {_excerpt(_content_text(request['params']))!r}"
        )
    return None


def check_report(report: ReplayReport, expectation: RegressionExpectation) -> list[str]:
    """Evaluate an expectation against a replay report.

    Returns one human-readable failure string per unmet expectation; an empty
    list means the case passes. The ``divergence`` expectation is matched
    against the replay's *first* divergence.
    """
    failures: list[str] = []
    first = report.divergences[0] if report.divergences else None
    expected = expectation.divergence
    if expected is None:
        if report.divergences:
            listed = "; ".join(d.render() for d in report.divergences[:3])
            more = f" (+{len(report.divergences) - 3} more)" if len(report.divergences) > 3 else ""
            failures.append(
                f"expected no divergence, but the replay recorded {len(report.divergences)}: {listed}{more}"
            )
    elif first is None:
        failures.append(
            f"expected the first divergence at {expected.render()}, but the replay completed with no divergence"
        )
    elif (first.kind, first.index) != (expected.kind, expected.index):
        failures.append(f"expected the first divergence at {expected.render()}, got: {first.render()}")
    elif expected.field is not None and first.field != expected.field:
        failures.append(f"expected the divergence on field {expected.field!r}, got: {first.render()}")

    for pos, check in enumerate(expectation.checks):
        failure = _run_check(report, pos, check)
        if failure is not None:
            failures.append(failure)
    return failures


async def run_regression_case(case_dir: Path) -> tuple[ReplayReport, list[str]]:
    """Replay one case directory (``cassette/`` + ``expect.yaml``).

    Returns the replay report and the failure list from :func:`check_report`
    (empty = the case passes). Tool execution and tracing suppression follow
    :func:`raven.trajectory.replay.run_replay` — no real tool runs, no spans
    are emitted.
    """
    case_dir = Path(case_dir)
    expectation = load_expectation(case_dir / EXPECTATION_FILE)
    report = await run_replay(case_dir / CASSETTE_DIR, mode=expectation.mode)
    return report, check_report(report, expectation)


@dataclass(frozen=True)
class ReviewedResidual:
    """One residual-scan finding a human reviewed and vouched benign: the
    sha256 of the full token (never the token itself) plus the reason."""

    sha256: str
    note: str


@dataclass(frozen=True)
class CaseMetadata:
    """The parsed ``case.yaml`` of one regression case — the human contract:
    who answers for the case, what bug it guards, when re-recording the
    baseline is legitimate, and which residual findings were reviewed."""

    issue: str
    owner: str
    why: str
    re_record: str
    risk: str | None = None
    created_from: str | None = None
    reviewed_residuals: tuple[ReviewedResidual, ...] = ()


def _parse_reviewed_residual(where: str, pos: int, data: Any) -> ReviewedResidual:
    where = f"{where}: reviewed_residuals[{pos}]"
    _require(isinstance(data, dict), where, f"must be a mapping, got {type(data).__name__}")
    _require_str_keys(where, data)
    unknown = set(data) - _REVIEWED_KEYS
    _require(not unknown, where, f"unknown key(s) {sorted(unknown)}; allowed: {sorted(_REVIEWED_KEYS)}")
    digest = data.get("sha256")
    _require(
        isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest.lower()),
        where,
        f"sha256 must be the 64-hex digest of the full token, got {digest!r}",
    )
    note = data.get("note")
    _require(
        isinstance(note, str) and note.strip() != "",
        where,
        "note is required and must say why the token is benign",
    )
    return ReviewedResidual(sha256=digest.lower(), note=note)


def load_case_metadata(path: Path) -> CaseMetadata:
    """Parse and validate one ``case.yaml``; raises ``ValueError`` on any
    unknown key, malformed field, blank required field, or YAML syntax
    error, naming the file and the problem."""
    path = Path(path)
    where = str(path)
    data = _load_yaml_mapping(path, "case metadata")
    unknown = set(data) - _CASE_KEYS
    _require(not unknown, where, f"unknown key(s) {sorted(unknown)}; allowed: {sorted(_CASE_KEYS)}")
    for key in _CASE_REQUIRED:
        value = data.get(key)
        _require(
            isinstance(value, str) and value.strip() != "",
            where,
            f"{key} is required and must be a non-blank string, got {value!r}",
        )
    risk = data.get("risk")
    _require(risk is None or risk in _RISK_LEVELS, where, f"risk must be one of {_RISK_LEVELS}, got {risk!r}")
    created_from = data.get("created_from")
    _require(
        created_from is None or (isinstance(created_from, str) and created_from.strip() != ""),
        where,
        f"created_from must be a non-blank string, got {created_from!r}",
    )
    raw_reviewed = data.get("reviewed_residuals") or []
    _require(
        isinstance(raw_reviewed, list),
        where,
        f"reviewed_residuals must be a list, got {type(raw_reviewed).__name__}",
    )
    reviewed = tuple(_parse_reviewed_residual(where, pos, entry) for pos, entry in enumerate(raw_reviewed))
    return CaseMetadata(
        issue=data["issue"],
        owner=data["owner"],
        why=data["why"],
        re_record=data["re_record"],
        risk=risk,
        created_from=created_from,
        reviewed_residuals=reviewed,
    )


def discover_case_dirs(root: Path) -> list[Path]:
    """Every direct subdirectory of ``root``, sorted by name.

    Deliberately not filtered by ``expect.yaml`` presence: a case directory
    missing its expectation must reach :func:`validate_case` and be reported
    broken, not silently skipped. Plain files under the root (a README) are
    not cases."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"cases root {root} is not a directory")
    return sorted(path for path in root.iterdir() if path.is_dir())


def validate_case(case_dir: Path) -> list[str]:
    """Statically validate one case directory; one problem string each, empty
    list = the case is fit to commit.

    Checks both schemas, cassette completeness down to the replay contract
    (a referenced artifact must exist, parse, and carry a usable payload —
    valid JSON alone proves nothing), that every committed file is scannable
    (the residual scan silently skips unreadable/non-UTF-8 files, so zero
    findings on such a file would vouch for nothing), that every residual
    finding carries an explicit human review entry in ``case.yaml``
    (:func:`_residual_problems`), and the size budget. Never replays; the
    pytest suite does that.
    """
    case_dir = Path(case_dir)
    if not case_dir.is_dir():
        return [f"{case_dir} is not a directory"]
    problems: list[str] = []

    expect_path = case_dir / EXPECTATION_FILE
    if expect_path.is_file():
        try:
            load_expectation(expect_path)
        except ValueError as exc:
            problems.append(str(exc))
        except OSError as exc:
            problems.append(f"{expect_path} cannot be read: {exc}")
    else:
        problems.append(f"{EXPECTATION_FILE} is missing")

    metadata: CaseMetadata | None = None
    case_path = case_dir / CASE_FILE
    if case_path.is_file():
        try:
            metadata = load_case_metadata(case_path)
        except ValueError as exc:
            problems.append(str(exc))
        except OSError as exc:
            problems.append(f"{case_path} cannot be read: {exc}")
    else:
        problems.append(f"{CASE_FILE} is missing")

    cassette_dir = case_dir / CASSETTE_DIR
    if cassette_dir.is_dir():
        problems.extend(_cassette_problems(cassette_dir))
    else:
        problems.append(f"{CASSETTE_DIR}/ directory is missing")

    problems.extend(_scannability_problems(case_dir))
    problems.extend(_residual_problems(case_dir, metadata))
    problems.extend(_size_problems(case_dir))
    return problems


def _residual_problems(case_dir: Path, metadata: CaseMetadata | None) -> list[str]:
    """Residual findings without an explicit human review entry.

    The scanner reports false positives on legitimate prose (identifier-shaped
    tokens in tool descriptions, ``redaction.json``'s own stat keys), so
    absolute zero findings would fail every honestly-built case. The gate for
    each finding is the ``reviewed_residuals`` list in ``case.yaml``: a human
    inspects the token, vouches for it by full-token sha256 with a note, and
    that entry goes through git review like the rest of the case. Nothing is
    exempted automatically — a machine-produced report proves a scan ran, not
    that a person approved what it found — and the digest covers the whole
    token, so a different token sharing the visible prefix/suffix of a masked
    sample cannot ride along. A missing or unparseable ``case.yaml`` means an
    empty review list: every finding fails."""
    reviewed = {entry.sha256 for entry in metadata.reviewed_residuals} if metadata else set()
    problems: list[str] = []
    for finding in scan_residuals(case_dir):
        digest = hashlib.sha256(finding.token.encode("utf-8")).hexdigest()
        if digest in reviewed:
            continue
        if finding.token.lower() in reviewed:
            # The finding is a listed digest itself: the 64-hex review entries
            # in case.yaml are high-entropy tokens to the scanner. A digest
            # literal carries no secret — it is the review record.
            continue
        problems.append(
            f"residual scan flagged a {finding.category} token in {finding.file} with no"
            f" reviewed_residuals entry in {CASE_FILE}; if a human inspects it and finds it"
            f" benign, record sha256 {digest} with a note"
        )
    return problems


def _cassette_problems(cassette_dir: Path) -> list[str]:
    problems: list[str] = []
    manifest_path = cassette_dir / "manifest.json"
    spans_path = cassette_dir / "spans.jsonl"
    if not (cassette_dir / "redaction.json").is_file():
        problems.append("cassette/redaction.json is missing (the cassette never went through redaction)")
    if not (cassette_dir / "artifacts").is_dir():
        problems.append("cassette/artifacts/ is missing")

    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            problems.append(f"cassette/manifest.json cannot be parsed: {exc}")
        else:
            # A parsed non-object (null included) is its own failure, distinct
            # from a parse error — it must not skip the content checks.
            if not isinstance(manifest, dict):
                problems.append("cassette/manifest.json must hold a JSON object")
            else:
                if "format_version" not in manifest:
                    problems.append("cassette/manifest.json has no format_version")
                if not isinstance(manifest.get("minimized"), dict):
                    problems.append(
                        "cassette/manifest.json has no minimized block"
                        " (cassettes come from trajectory minimize, not from copying a raw bundle)"
                    )
    else:
        problems.append("cassette/manifest.json is missing")

    structural: list[str] = []
    if spans_path.is_file():
        structural, references = _span_problems(cassette_dir, spans_path)
        problems.extend(structural)
        problems.extend(references)
    else:
        problems.append("cassette/spans.jsonl is missing")

    # Semantic completeness on top of the static checks: load the recording
    # the way replay does and hold it to the minimize gate's contract. Gated
    # on structurally sound spans — load_recording assumes well-formed span
    # records (a non-mapping attributes value would crash it), and the
    # structural problems above already fail the case.
    if manifest_path.is_file() and spans_path.is_file() and not structural:
        try:
            recording = load_recording(cassette_dir)
        except (OSError, ValueError) as exc:
            problems.append(f"cassette cannot be parsed for replay: {exc}")
        else:
            problems.extend(f"cassette is not replayable: {p}" for p in replayability_problems(recording))
    return problems


def _span_problems(cassette_dir: Path, spans_path: Path) -> tuple[list[str], list[str]]:
    """Static span checks: (structural problems, artifact reference problems).

    Structural problems mean the span records themselves are malformed and
    the caller must not feed the file to ``load_recording``; reference
    problems leave the structure sound (replay tolerates a missing payload,
    the semantic pass reports it)."""
    structural: list[str] = []
    references: list[str] = []
    try:
        lines = spans_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cassette/spans.jsonl cannot be read: {exc}"], []
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            span = json.loads(line)
        except json.JSONDecodeError:
            structural.append(f"cassette/spans.jsonl line {line_no} is not valid JSON")
            continue
        if not isinstance(span, dict):
            structural.append(f"cassette/spans.jsonl line {line_no} is not a JSON object")
            continue
        span_id = span.get("spanId")
        if span_id is not None and not isinstance(span_id, str):
            structural.append(f"cassette/spans.jsonl line {line_no}: spanId must be a string")
            continue
        attrs = span.get("attributes")
        if attrs is None:
            continue
        if not isinstance(attrs, dict):
            structural.append(f"cassette/spans.jsonl line {line_no}: attributes must be a mapping")
            continue
        for key, ref in attrs.items():
            if isinstance(key, str) and key.endswith(".artifact_path"):
                ref_structural, ref_references = _artifact_ref_problems(cassette_dir, line_no, key, ref)
                structural.extend(ref_structural)
                references.extend(ref_references)
    return structural, references


def _artifact_ref_problems(cassette_dir: Path, line_no: int, key: str, ref: Any) -> tuple[list[str], list[str]]:
    """Checks for one artifact reference: (structural problems, reference problems).

    A missing, unreadable, or non-JSON payload is a reference problem —
    ``load_recording`` degrades those to ``None`` and the semantic pass
    reports the gap. A payload that parses to a non-object is structural:
    the loaders index into it and must not be fed the file."""
    label = f"cassette/spans.jsonl line {line_no}: {key} {ref!r}"
    if not isinstance(ref, str) or not ref:
        return [], [f"{label} is not a usable reference"]
    if PurePosixPath(ref).is_absolute() or ".." in PurePosixPath(ref).parts:
        return [], [f"{label} escapes the cassette"]
    resolved = (cassette_dir / ref).resolve()
    if cassette_dir.resolve() not in resolved.parents:
        return [], [f"{label} escapes the cassette"]
    if not resolved.is_file():
        return [], [f"{label} does not exist"]
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"{label} cannot be read: {exc}"]
    except json.JSONDecodeError:
        return [], [f"{label} is not valid JSON"]
    if not isinstance(payload, dict):
        return [f"{label} must hold a JSON object"], []
    return [], []


def _scannability_problems(case_dir: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(p for p in case_dir.rglob("*") if p.is_file()):
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            problems.append(
                f"{path.relative_to(case_dir)} is not readable as UTF-8 text —"
                " the residual scan silently skips such files and cannot vouch for them"
            )
    return problems


def _size_problems(case_dir: Path) -> list[str]:
    problems: list[str] = []
    total = 0
    for path in sorted(p for p in case_dir.rglob("*") if p.is_file()):
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES:
            problems.append(
                f"{path.relative_to(case_dir)} is {size} bytes, over the {MAX_FILE_BYTES}-byte per-file budget"
                " (sized to stay well under the repo's 1 MiB large-file gate)"
            )
    if total > MAX_CASE_BYTES:
        problems.append(
            f"the case totals {total} bytes, over the {MAX_CASE_BYTES}-byte budget (the repo's 1 MiB large-file gate)"
        )
    return problems
