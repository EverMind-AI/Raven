"""Export one cultivation run's records as a self-contained, reviewable cultivation record.

A run directory holds what `experimental.iteration.run` and the travel-agency simulation wrote: the iteration record
with its analysis and curation records, each Harness generation's `observations.jsonl`, the kept deliverables, the
employee's agent home, `settings.json` and, once the suite finished the run, `suite-summary.json`. `build_record`
reads them without any model call and returns one JSON-serialisable dict (schema 1). `export_record` writes that dict
(`record.json`), its ledger (`ledger.json`), a Markdown transcript, copies of the deliverables and the page images
that already exist beside them, the scenario inputs, and `manifest.json` with every file's SHA-256, size and time.
Every string the record carries passes `redact`; no `config.json`, credential folder or trace log is ever copied.

Linking rules. They are deterministic, and every link says how it was made:

- A requirement the owner raised as the Analyst links `owner` to the criteria it named for it in the analysis record.
  Otherwise it links `explicit` to the criteria whose ids its behavior, observation, acceptance or evidence names.
  Otherwise it links `inferred` to the criteria that failed, in the same round, in a drill session it cites; when
  several did, those whose verdict shares a quoted passage with the requirement are kept, if any. Otherwise `none`.
  A session is cited by one of its turn ids (or an 8+ hex prefix), by `[name]`, a quoted or backticked name, its name
  beside "conversation", "drill", "session", "dialog", "chat", "trial" or "card", or its case-sensitive name next to
  non-ASCII text.
- After a round, a curation's change attaches to the criteria its reason names (by id or a shared SOP section
  marker) among those linked to that round's requirements, and to all of those when it names none of them. For the
  onboarding curation, or when the round linked no criterion, a change attaches only to criteria its reason names by
  id or shares a section marker with. Markers are `S4.1`, `G2`, `B3` and the like; ranges such as `S4.1 to S4.7` or
  `G1-G9` expand, and a bare section such as `S4` covers its subsections.
- A target's facet is the role the target catalogue gives it (so `planning.skills` is capability); `prompt.*` counts
  as memory and anything else as other.
- Mechanism evidence is the planning, participant, component, strategy (action, capability, memory), inference,
  hosting and loop-control rows of each drill turn, grouped by kind, target and decision, plus tool calls refused in
  that turn, each decision counted once (see `experimental.analyst.activity`), with the distinct reasons given and
  how often. Review verdicts `resample` and `end`, refused tools and refused planning steps are interventions.
- Mechanism rows carry the harness scope they ran in: `root`, or the child harness whose process records a
  `child.execution` row carries.
- In the ledger, a criterion's evidence for a round counts the mechanism rows of the scope and target that an
  installed curation sedimented for it before that round, from the drills its verdicts name (every drill when none
  is named).
- A criterion is `never_failed` when judged and never failed, `not_exercised` when never judged pass or fail,
  `still_failing` when its last judged round failed or was mixed, and otherwise `held_since_round_N`, where round N
  starts its final run of passing rounds.

Artifact diffs compare a curation's candidate with the merged artifact the last installed curation of the same scope
left; the hired baseline counts as empty, and a curation that was not installed does not advance it. A curation that
failed shows the last candidate it submitted for a check. Curation records that no round names are joined by their
feedback and say so. Turns in the observations that no recorded round holds (a trial cut off mid-round) form a last
round marked `partial`. Paths are relative to the run directory; `export_record` rewrites them into `assets/`.

`run.reproduce` is the command line the run stored in its settings, with the configuration, homes and folders left to
the reader; a run from before the command line was stored gets one rebuilt from its settings (see `_command`).

Keys beyond the agreed schema (`run.steps`, `run.warnings`, `run.suite`, `run.reproduce`, `unlinked_deliverables`,
`node_requirements`, `value` (the verdict of `experimental.simulation.value`), each change's `attached` and `paths`,
each mechanism row's `scope`, `count` and `intervention`, and the like) are additive. Cost comes from
`suite-summary.json` when the suite wrote one, else from the audit spans under `<state_root>/<run name>/traces/logs`
or the copy in the run's own `traces/`, else it is null.

The transcript's labels are English; a scenario may override any of them in a `record.md` beside its
`scenario.json`, one `- key: label` line per label.
"""

import argparse
import difflib
import hashlib
import json
import re
import shlex
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from ..analyst.activity import INTERVENTIONS
from ..analyst.activity import classify as _classify
from ..analyst.activity import cut as _cut
from ..analyst.activity import dicts as _dicts
from ..analyst.activity import scope_of as _scope
from ..analyst.activity import scoped as _scoped
from ..curator.raven_adapter.targets import catalogue
from ..iteration import records
from .agency import REFERENCES
from .attribution import RESULT as ATTRIBUTION
from .channel import seen
from .employee import REPLICAS
from .isolation import RESULT as ISOLATION
from .scenario import BUNDLED
from .value import judge

SCHEMA = 1
DIFF_LINES = 400
NODE_SUMMARY_LIMIT = 600
BASELINE_ROWS = 300
DEFAULT_SCENARIO = "travel_agency"
SUMMARY = "suite-summary.json"
SETTINGS = "settings.json"
REASONS = 12
COMMAND = ("uv", "run", "python", "-m", "experimental.simulation")
PLACEHOLDERS = {
    "--config": "<config.json with your own keys>",
    "--home": "<a home matching inputs.baseline>",
    "--state-dir": "<a new empty directory>",
    "--workdir": "<a new empty directory>",
}
# Each model role falls back to this one's model when the run named none of its own (see `__main__.settings`).
MODEL_FALLBACKS = {"curator": "employee", "analyst": "curator", "simulation": "curator", "traveller": "simulation"}
HANDED_OVER = "\n\n---\n\n"
REDACTED = "<redacted>"
SECRETS = (
    re.compile(r"sk-[A-Za-z0-9-]{20,}"),
    re.compile(r"jina_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{40}(?![0-9A-Za-z])"),
    re.compile(r"\bBearer\s+\S+"),
)
PUBLIC = frozenset({"raven_commit"})
NEVER_COPIED = frozenset({"config.json", "config.migrations.json", "credentials", ".lock"})
TEXT_SUFFIXES = frozenset({".md", ".txt", ".json", ".csv", ".html", ".htm", ".yaml", ".yml", ".jsonl"})
FACETS = ("memory", "planning", "capability", "action")
MARKER = re.compile(
    r"(?<![A-Za-z0-9])([SGB])(\d{1,2})(?:\.(\d{1,2}))?"
    r"(?:\s*(?:to|-|\u2013|\u2014|~)\s*([SGB])?(\d{1,2})(?:\.(\d{1,2}))?)?(?![A-Za-z0-9])"
)
QUOTE = re.compile(r"\"([^\"]{12,})\"|\u201c([^\u201d]{12,})\u201d|\u300c([^\u300d]{8,})\u300d")
HEX = re.compile(r"[0-9a-f]{8,32}")
TURN_FIELD = re.compile(r'"turn_id":\s*(?:null|"([^"]*)")')
CHAT_ID = re.compile(r"Chat ID:\s*([^\s:]+):[0-9A-Za-z]{6,}")
SESSION_WORDS = "conversation|drill|session|dialog|dialogue|chat|trial|card"
LABELS = {
    "title": "Cultivation record",
    "inputs": "Inputs",
    "profile": "Agency profile (the employee's task)",
    "opening": "Onboarding message",
    "materials": "Materials",
    "cards": "Drill cards",
    "baseline": "Employee baseline home",
    "settings": "Run settings",
    "onboarding": "Onboarding curation",
    "round": "Round {number}",
    "drills": "Drills",
    "pages": "Deliverable pages",
    "evaluation": "Owner review",
    "analysis": "Analyst requirements",
    "curation": "Curator reply and harness changes",
    "mechanism": "Mechanism at work",
    "ledger": "Knowledge-sedimentation ledger",
    "cost": "Cost and reproduction",
    "value": "Value verdict",
    "customer": "Customer",
    "employee": "Employee",
    "follow_up": "Follow-up turn (background work reported back)",
}


def redact(value):
    """`value` with anything shaped like an API key, a bearer token or a bare 40-hex token replaced, at any depth."""
    if isinstance(value, str):
        for pattern in SECRETS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {
            redact(key) if isinstance(key, str) else key: item if key in PUBLIC else redact(item)
            for key, item in value.items()
        }
    return value


def facet(target: str) -> str:
    """The role the target catalogue gives a target (so `planning.skills` is capability); `prompt.*` is memory."""
    roles = {item.name: item.roles for item in catalogue()}.get(str(target))
    if roles:
        return roles[0]
    head = str(target).split(".", 1)[0]
    if head == "prompt":
        return "memory"
    return head if head in FACETS else "other"


def markers(text) -> set[str]:
    """SOP section markers cited in `text`, with ranges expanded."""
    found = set()
    for letter, major, minor, letter2, major2, minor2 in MARKER.findall(str(text or "")):
        found.add(f"{letter}{major}" + (f".{minor}" if minor else ""))
        if not major2:
            continue
        end = f"{letter2 or letter}{major2}" + (f".{minor2}" if minor2 else "")
        if (letter2 or letter) != letter:
            found.add(end)
        elif minor and minor2 and major == major2:
            found.update(f"{letter}{major}.{n}" for n in range(int(minor), min(int(minor2), int(minor) + 50) + 1))
        elif not minor and not minor2:
            found.update(f"{letter}{n}" for n in range(int(major), min(int(major2), int(major) + 50) + 1))
        else:
            found.add(end)
    return found


def _marker_match(left: set[str], right: set[str]) -> bool:
    return any(a == b or b.startswith(a + ".") or a.startswith(b + ".") for a in left for b in right)


def _names(text: str, name: str) -> bool:
    return bool(name) and re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", text) is not None


def _cites(text: str, session: str, turns) -> bool:
    """Whether a requirement's text cites a drill session (see the module docstring)."""
    tokens = HEX.findall(text)
    if any(turn and any(turn.startswith(token) for token in tokens) for turn in turns):
        return True
    name = re.escape(session)
    free = r"(?![A-Za-z0-9_-])"
    patterns = (
        rf"\[{name}\]",
        rf"`{name}`",
        rf"[\"']{name}[\"']",
        rf"(?<![A-Za-z0-9_-]){name}(?:'s)?\s+(?i:{SESSION_WORDS})",
        rf"(?i:{SESSION_WORDS})\s+[`'\"]?{name}{free}",
        rf"(?<![A-Za-z0-9_-]){name}\s?(?=[^\x00-\x7f])",
        rf"(?<=[^\x00-\x7f])\s?{name}{free}",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _quotes(text: str) -> list[str]:
    return [" ".join(next(part for part in match if part).split()) for match in QUOTE.findall(text or "")]


def _shares_quote(text: str, other: str) -> bool:
    flat_text, flat_other = " ".join((text or "").split()), " ".join((other or "").split())
    return any(quote[:30] in flat_other for quote in _quotes(text)) or any(
        quote[:30] in flat_text for quote in _quotes(other)
    )


def _json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _jsonl(path: Path):
    try:
        with Path(path).open(errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _iso(epoch) -> str | None:
    try:
        return datetime.fromtimestamp(float(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _stem(name: str) -> str:
    return name[:-5] if name.endswith(".json") else name


def _unique(values) -> list:
    return list(dict.fromkeys(values))


def _inside(path: Path, root: Path) -> Path | None:
    """`path` resolved, when it is an existing file inside `root`; nothing outside the run is ever read."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    return resolved if resolved.is_file() and resolved.is_relative_to(root.resolve()) else None


def _local(path, root: Path) -> Path | None:
    """A file a record names by an absolute path from where the run was written, found inside this run's folder."""
    if not path:
        return None
    raw = Path(str(path))
    candidates = [raw if raw.is_absolute() else root / raw]
    parts = raw.parts
    if "home" in parts:
        start = len(parts) - 1 - parts[::-1].index("home")
        if start >= 2 and parts[start - 2] == REPLICAS:
            start -= 2
        candidates.append(root.joinpath(*parts[start:]))
    for candidate in candidates:
        found = _inside(candidate, root)
        if found:
            return found
    return None


def _relative(path: Path, root: Path) -> str:
    return Path(path).resolve().relative_to(root.resolve()).as_posix()


# Reading the run


def _record_path(run_dir: Path) -> Path:
    if run_dir.is_file():
        return run_dir
    found = records.runs(run_dir)
    if not found:
        raise FileNotFoundError(f"no iteration record under {run_dir}")
    return found[-1]


def _named(root: Path, folder: str, names) -> list[dict]:
    joined = []
    for name in names if isinstance(names, list | tuple) else []:
        if isinstance(name, dict):
            joined.append(name)
            continue
        data = _json(root / folder / str(name))
        joined.append({"file": str(name), **data} if isinstance(data, dict) else {"missing": str(name)})
    return joined


def _joined(path: Path) -> dict:
    """The run record with its analysis and curation records read in, tolerating records that are cut short."""
    try:
        return records.load(path)
    except (KeyError, TypeError, AttributeError, IndexError, ValueError):
        run = json.loads(path.read_text())
        root = path.parents[1]
        run["initial_curation"] = _named(root, "curation", run.get("initial_curation"))
        for item in _dicts(run.get("rounds")):
            item["analysis"] = _named(root, "analysis", item.get("analysis"))
            item["curation"] = _named(root, "curation", item.get("curation"))
        return run


def _scenario(scenario_dir, settings: dict) -> dict:
    """The scenario's files as plain data; a missing or older scenario leaves its parts empty."""
    root = Path(scenario_dir) if scenario_dir else BUNDLED / str(settings.get("scenario") or DEFAULT_SCENARIO)
    if not root.is_dir():
        root = BUNDLED / DEFAULT_SCENARIO
    spec = _json(root / "scenario.json")
    spec = spec if isinstance(spec, dict) else {}
    without = set(settings.get("without") or ())

    def read(name):
        try:
            return (root / name).read_text().strip()
        except OSError:
            return ""

    return {
        "root": root,
        "name": root.name,
        "profile": read("profile.md"),
        "onboarding": read("onboarding.md"),
        "criteria": [row for row in _dicts(spec.get("criteria")) if row.get("id")],
        "initial": [str(name) for name in spec.get("initial") or [] if name not in without],
        "materials": {
            path.parent.name: path.parent
            for path in sorted(root.glob("materials/*/SKILL.md"))
            if path.parent.name not in without
        },
        "cards": sorted(root.glob("personas/*.md")),
    }


def _labels(scenario: dict) -> dict:
    labels = dict(LABELS)
    try:
        text = (scenario["root"] / "record.md").read_text()
    except OSError:
        return labels
    for line in text.splitlines():
        match = re.match(r"\s*[-*]\s*([a-z_]+)\s*:\s*(.+?)\s*$", line)
        if match and match.group(1) in labels:
            labels[match.group(1)] = match.group(2)
    return labels


def _document(path: Path) -> str:
    try:
        text = Path(path).read_text()
    except OSError:
        return ""
    if text.startswith("---"):
        text = text.split("---", 2)[-1]
    return text.strip()


# Curations


def _last_check_failed(trace) -> bool:
    for event in reversed(_dicts(trace)):
        if event.get("event") not in ("validation", "preflight"):
            continue
        result = event.get("result")
        errors = event.get("errors") if "errors" in event else (result or {}).get("errors")
        if isinstance(errors, str):
            return errors.strip() not in ("", "[]")
        return bool(errors)
    return False


def _outcome(data: dict) -> tuple[str, str | None]:
    if data.get("missing"):
        return "error", f"curation record {data['missing']} is missing"
    if data.get("paused"):
        return "paused", data.get("error")
    error = data.get("error")
    if data.get("generated") and not error:
        return "installed", None
    if str(error or "").startswith("repair budget exhausted") or _last_check_failed(data.get("trace")):
        return "rejected", error
    return "error", error


def _plan(data: dict) -> tuple[dict, str | None]:
    """The Curator's reply: the installed candidate's plan, else the last plan or selection it submitted."""
    plan = ((data.get("generated") or {}).get("candidate") or {}).get("plan")
    if isinstance(plan, dict):
        return plan, "candidate"
    for event in reversed(_dicts(data.get("trace"))):
        output = event.get("output")
        if event.get("event") not in ("submit_plan", "submit_selection") or not isinstance(output, dict):
            continue
        plan = output.get("plan") if isinstance(output.get("plan"), dict) else output
        if event["event"] == "submit_selection":
            targets = [target for target in plan.get("targets") or [] if isinstance(target, str)]
            plan = {**plan, "changes": [{"target": target} for target in targets]}
        return plan, "trace"
    return {}, None


def _candidate(data: dict) -> dict | None:
    artifact = ((data.get("generated") or {}).get("candidate") or {}).get("artifact")
    if isinstance(artifact, dict):
        return artifact
    for event in reversed(_dicts(data.get("trace"))):
        arguments = event.get("arguments")
        if (
            event.get("event") == "preflight"
            and isinstance(arguments, dict)
            and ({"values", "files"} & arguments.keys())
        ):
            return arguments
    return None


def _merge(base, change):
    if isinstance(base, dict) and isinstance(change, dict):
        result = dict(base)
        for key, value in change.items():
            result[key] = _merge(result[key], value) if key in result else value
        return result
    return change


def _flatten(values: dict, files: dict) -> dict:
    """Each authored text an artifact holds, keyed by (target, path); a non-mapping payload is one `(value)` text."""

    def text(value):
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)

    out = {}
    for target, value in values.items():
        if isinstance(value, dict) and value:
            out.update({(target, str(key)): text(item) for key, item in value.items()})
        else:
            out[(target, "(value)")] = text(value)
    out.update({("files", str(path)): text(content) for path, content in files.items()})
    return out


def _diff(key, old, new) -> dict:
    target, path = key
    lines = list(
        difflib.unified_diff(
            (old or "").splitlines(), (new or "").splitlines(), f"a/{target}/{path}", f"b/{target}/{path}", lineterm=""
        )
    )
    body = [line for line in lines[2:] if not line.startswith("@@")]
    text = "\n".join(lines[:DIFF_LINES])
    if len(lines) > DIFF_LINES:
        text += f"\n... {len(lines) - DIFF_LINES} more diff lines"
    return {
        "target": target,
        "path": path,
        "change": "added" if old is None else "removed" if new is None else "modified",
        "lines_added": sum(line.startswith("+") for line in body),
        "lines_removed": sum(line.startswith("-") for line in body),
        "diff": text,
    }


def _references(payload) -> set[str]:
    text = json.dumps(payload, ensure_ascii=False)
    return set(re.findall(r"([A-Za-z_][\w.]*):[A-Za-z_]\w*", text))


def _module(path: str) -> str:
    stem = path[:-3] if path.endswith(".py") else path
    return stem.removesuffix("/__init__").replace("/", ".")


def _artifact_diff(state: tuple[dict, dict], artifact: dict) -> tuple[list[dict], tuple[dict, dict], dict]:
    """The file diffs a candidate makes, the merged artifact after it, and the changed paths of each target."""
    values, files = state
    proposed = artifact.get("values") if isinstance(artifact.get("values"), dict) else {}
    added_files = artifact.get("files") if isinstance(artifact.get("files"), dict) else {}
    removed = {target for target in artifact.get("remove") or [] if isinstance(target, str)}
    merged = _merge({key: value for key, value in values.items() if key not in removed}, proposed)
    after_files = {**files, **added_files}
    before, after = _flatten(values, files), _flatten(merged, after_files)
    keys = [key for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]
    entries = [_diff(key, before.get(key), after.get(key)) for key in keys]
    paths = {}
    for entry in entries:
        if entry["target"] != "files":
            paths.setdefault(entry["target"], []).append(entry["path"])
            continue
        module = _module(entry["path"])
        for target, payload in {**values, **merged}.items():
            if any(ref == module or ref.startswith(module + ".") for ref in _references(payload)):
                paths.setdefault(target, []).append(entry["path"])
    return entries, (merged, after_files), paths


def _feedback_round(data: dict, run: dict) -> int | None:
    feedback = data.get("feedback")
    if feedback is None or (isinstance(feedback, dict) and set(feedback) <= {"signals"}):
        return 0
    reason = feedback.get("reason") if isinstance(feedback, dict) else None
    for number, item in enumerate(_dicts(run.get("rounds")), start=1):
        if reason and isinstance(item.get("feedback"), dict) and item["feedback"].get("reason") == reason:
            return number
    return None


def _curation_sources(run: dict, root: Path) -> list[tuple[int | None, dict]]:
    """Every curation record with the round it followed (0 for onboarding), children expanded, in install order.

    A composed curation lists each child harness it changed under `child_changes` (harness name to that child's
    generation); each becomes its own curation of that harness's scope, installed together with its parent.
    """
    entries = [(0, data) for data in _dicts(run.get("initial_curation"))]
    for number, item in enumerate(_dicts(run.get("rounds")), start=1):
        entries += [(number, data) for data in _dicts(item.get("curation"))]
    named = {data.get("file") or data.get("missing") for _, data in entries}
    folder = root / "curation"
    for path in sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime) if folder.is_dir() else []:
        if path.name == "pending.json" or path.name in named:
            continue
        data = _json(path)
        if isinstance(data, dict):
            entries.append((_feedback_round(data, run), {"file": path.name, **data, "joined": "by feedback"}))
    expanded = []
    for number, data in entries:
        expanded.append((number, data))
        name = _stem(str(data.get("file") or data.get("missing") or "curation"))
        for index, child in enumerate(_dicts(data.get("children")), start=1):
            expanded.append((number, {"file": child.get("file") or f"{name}.{index}", **child}))
        changes = data.get("child_changes") if isinstance(data.get("child_changes"), dict) else {}
        for harness, generated in changes.items():
            if isinstance(generated, dict):
                child = {"scope": harness, "generated": generated, "trace": generated.get("trace")}
                expanded.append((number, {"file": f"{name}.{harness}", **child}))
    order = {id(data): index for index, (_, data) in enumerate(expanded)}
    return sorted(
        expanded,
        key=lambda pair: (
            pair[0] is None,
            pair[0] or 0,
            _scope(pair[1].get("scope")) != "root",
            _scope(pair[1].get("scope")),
            order[id(pair[1])],
        ),
    )


def _curations(run: dict, root: Path) -> list[dict]:
    states: dict[str, tuple[dict, dict]] = {}
    out = []
    for number, data in _curation_sources(run, root):
        scope = _scope(data.get("scope"))
        outcome, error = _outcome(data)
        plan, source = _plan(data)
        artifact = _candidate(data)
        entries, paths = [], {}
        if artifact is not None:
            entries, merged, paths = _artifact_diff(states.get(scope, ({}, {})), artifact)
            if outcome == "installed":
                states[scope] = merged
        validation = (data.get("generated") or {}).get("validation") or {}
        bound = [
            target
            for row in _dicts(validation.get("observations"))
            if row.get("kind") == "runtime.bound"
            for target in row.get("targets") or []
        ]
        out.append(
            {
                "id": _stem(str(data.get("file") or data.get("missing") or "")),
                "round": number,
                "scope": scope,
                "understanding": plan.get("understanding", ""),
                "design": plan.get("design", ""),
                "changes": [
                    {
                        "target": str(change.get("target", "")),
                        "facet": facet(str(change.get("target", ""))),
                        "reason": change.get("reason", ""),
                        "expected": change.get("expected", ""),
                        "verification": change.get("verification", ""),
                        "paths": paths.get(str(change.get("target", "")), []),
                    }
                    for change in _dicts(plan.get("changes"))
                ],
                "artifact_diff": entries,
                "outcome": outcome,
                "error": error,
                "plan_source": source,
                "validation": {"errors": validation.get("errors") or [], "bound": bound},
                "active_artifact": data.get("active_artifact_id"),
                "joined": data.get("joined", "by record"),
            }
        )
    return out


# Rounds


def _generations(root: Path) -> list[Path]:
    """The observation files of every Harness generation, the employee's and its replicas', oldest first."""
    found = [*root.glob("*/observations.jsonl"), *root.glob(f"{REPLICAS}/*/*/observations.jsonl")]
    return sorted(found, key=lambda path: path.stat().st_mtime)


def _kept(root: Path, turn_id: str) -> Path:
    """The folder holding what one turn delivered, on the employee or on the replica that played it."""
    folder = root / "deliverables" / turn_id
    if folder.is_dir():
        return folder
    return next((path for path in root.glob(f"{REPLICAS}/*/deliverables/{turn_id}") if path.is_dir()), folder)


def _delivered(path, root: Path) -> dict:
    raw = Path(str(path))
    file = _local(raw, root) or _local(_kept(root, raw.parent.name) / raw.name, root)
    if file is None:
        return {"name": raw.name, "path": None, "sha256": None, "pages": []}
    thumbs = file.with_name(file.name + ".thumbs")
    pages = sorted(thumbs.glob("*.png")) if thumbs.is_dir() else []
    return {
        "name": file.name,
        "path": _relative(file, root),
        "sha256": _sha256(file),
        "pages": [_relative(page, root) for page in pages if _inside(page, root)],
    }


def _node_summary(path, root: Path) -> str:
    file = _local(path, root)
    if file is None:
        return ""
    try:
        with file.open(errors="replace") as handle:
            text = " ".join(handle.read(4_000_000).split())
    except OSError:
        return ""
    half = NODE_SUMMARY_LIMIT // 2
    return text if len(text) <= NODE_SUMMARY_LIMIT else f"{text[:half]} ... {text[-half:]}"


def _playbooks(rows, root: Path) -> list[dict]:
    """Each playbook graph a turn ran, with every node's sub-agent, final status, duration and output summary."""
    runs: dict[str, dict] = {}
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        run_id = payload.get("run_id")
        if row.get("kind") != "dag.progress" or not run_id:
            continue
        nodes = runs.setdefault(run_id, {})

        def node(node_id, nodes=nodes):
            return nodes.setdefault(
                node_id, {"id": node_id, "subagent": None, "status": "pending", "seconds": None, "summary": ""}
            )

        if row.get("name") == "dag_run_started":
            for item in _dicts(payload.get("nodes")):
                node(item.get("id"))["subagent"] = item.get("subagent")
        elif row.get("name") == "dag_node_updated" and payload.get("node"):
            node(payload["node"])["status"] = payload.get("status") or node(payload["node"])["status"]
        elif row.get("name") == "dag_run_completed":
            for item in _dicts((payload.get("manifest") or {}).get("files")):
                entry = node(item.get("node"))
                entry["subagent"] = item.get("subagent") or entry["subagent"]
                entry["status"] = item.get("status") or entry["status"]
                if isinstance(item.get("started_at"), int | float) and isinstance(item.get("ended_at"), int | float):
                    entry["seconds"] = round((item["ended_at"] - item["started_at"]) / 1000, 1)
                entry["summary"] = _node_summary(item.get("output_file"), root)
    return [{"run_id": run_id, "nodes": list(nodes.values())} for run_id, nodes in runs.items()]


def _mechanism(rows, session: str, turn: int) -> list[dict]:
    groups: dict[tuple, dict] = {}
    tools: dict = {}
    for scope, row in _scoped(rows):
        found = _classify(row, tools)
        if found is None:
            continue
        kind, target, decision, summary = found
        group = groups.setdefault(
            (scope, kind, target, decision),
            {
                "scope": scope,
                "kind": kind,
                "target": target,
                "decision": decision,
                "session": session,
                "turn": turn,
                "count": 0,
                "intervention": decision in INTERVENTIONS,
                "summary": "",
                "reasons": {},
            },
        )
        group["count"] += 1
        group["summary"] = summary or group["summary"]
        reasons = group["reasons"]
        key = summary if summary in reasons or len(reasons) < REASONS else ""
        reasons[key] = reasons.get(key, 0) + 1
    return list(groups.values())


def _card(session: str, cards: set[str]) -> str:
    if session in cards:
        return session
    base = re.sub(r"-\d+$", "", session)
    return base if base in cards else session


def _exchange(index: int, customer: str, rows: list[dict], delivered: list[dict], root: Path, **extra) -> dict:
    return {
        "turn": index,
        "customer": customer,
        "assistant": seen(rows, [file["name"] for file in delivered if file.get("name")]),
        "delivered": delivered,
        "playbook_runs": _playbooks(rows, root),
        "errors": [
            _cut(row.get("error") or row.get("kind")) for row in rows if str(row.get("kind", "")).endswith(".error")
        ],
        **extra,
    }


def _drills(item: dict, root: Path, cards: set[str]) -> tuple[list[dict], list[dict], list[str]]:
    """A round's drills, their mechanism evidence and the artifact ids the drills ran on."""
    drills, evidence, artifacts = [], [], []
    for session, exchanges in (item.get("sessions") or {}).items() if isinstance(item.get("sessions"), dict) else ():
        drill = {"session": session, "card": _card(session, cards), "exchanges": []}
        for index, exchange in enumerate(_dicts(exchanges), start=1):
            execution = exchange.get("execution") if isinstance(exchange.get("execution"), dict) else {}
            rows = _dicts(execution.get("records"))
            if execution.get("artifact_id"):
                artifacts.append(execution["artifact_id"])
            delivered = [_delivered(path, root) for path in execution.get("deliverables") or [] if path]
            drill["exchanges"].append(
                _exchange(index, str(exchange.get("user", "")), rows, delivered, root, turn_id=execution.get("turn_id"))
            )
            evidence += _mechanism(rows, session, index)
        drills.append(drill)
    return drills, evidence, artifacts


def _evaluation(item: dict, severity: dict) -> dict:
    """The round's remark as the Curator heard it, and the owner's verdicts: carried on its signal, or, when an Analyst
    read the owner's words, kept apart on the scorecard in the analysis record."""
    signals = _dicts(item.get("signals"))
    scored = signals
    if not any(_dicts(signal.get("items")) for signal in signals):
        scored = [data["scorecard"] for data in _dicts(item.get("analysis")) if isinstance(data.get("scorecard"), dict)]
    metrics = {}
    for signal in [*signals, *scored]:
        metrics.update(signal.get("metrics") or {})
    return {
        "remark": "\n\n".join(str(signal.get("text")) for signal in signals if signal.get("text")),
        "items": [
            {
                "criterion": str(entry.get("id", "")),
                "severity": severity.get(str(entry.get("id", "")), "unknown"),
                "result": entry.get("result"),
                "session": entry.get("session"),
                "actual": entry.get("actual", ""),
                "note": entry.get("note", ""),
                "source": signal.get("source"),
            }
            for signal in scored
            for entry in _dicts(signal.get("items"))
        ],
        "metrics": metrics,
        "satisfied": [signal.get("satisfied") for signal in signals],
    }


def _requirement_text(requirement: dict) -> str:
    evidence = requirement.get("evidence") or []
    parts = [requirement.get("behavior"), requirement.get("observed"), requirement.get("acceptance")]
    return "\n".join(str(part) for part in [*parts, *(evidence if isinstance(evidence, list) else [evidence])] if part)


def link(requirement: dict, criteria: list[str], items: list[dict], turns: dict[str, list[str]]) -> tuple[list, str]:
    """The criteria a requirement concerns and how that was established (see the module docstring)."""
    text = _requirement_text(requirement)
    named = [criterion for criterion in criteria if _names(text, criterion)]
    if named:
        return named, "explicit"
    cited = {session for session, ids in turns.items() if _cites(text, session, ids)}
    failing = [item for item in items if item.get("result") == "fail" and item.get("session") in cited]
    ids = _unique(item["criterion"] for item in failing)
    if len(ids) > 1:
        quoted = [
            criterion
            for criterion in ids
            if any(
                _shares_quote(text, f"{item.get('actual', '')} {item.get('note', '')}")
                for item in failing
                if item["criterion"] == criterion
            )
        ]
        ids = quoted or ids
    return (ids, "inferred") if ids else ([], "none")


def _analysis(item: dict, criteria: list[str], items: list[dict], turns: dict) -> dict:
    feedback = item.get("feedback") if isinstance(item.get("feedback"), dict) else {}
    error = next((str(data["error"]) for data in _dicts(item.get("analysis")) if data.get("error")), None)
    requirements = []
    stated = next(
        (data["requirement_criteria"] for data in _dicts(item.get("analysis")) if "requirement_criteria" in data), None
    )
    for index, requirement in enumerate(_dicts(feedback.get("requirements"))):
        if isinstance(stated, list) and index < len(stated) and stated[index]:
            linked, how = list(stated[index]), "owner"
        else:
            linked, how = link(requirement, criteria, items, turns)
        evidence = requirement.get("evidence") or []
        requirements.append(
            {
                "behavior": requirement.get("behavior", ""),
                "observed": requirement.get("observed", ""),
                "evidence": list(evidence) if isinstance(evidence, list | tuple) else [str(evidence)],
                "acceptance": requirement.get("acceptance", ""),
                "expectation": requirement.get("expectation"),
                "strength": requirement.get("strength"),
                "recurrence": requirement.get("recurrence"),
                "criteria": linked,
                "link": how,
            }
        )
    waiting = sorted(
        {
            criterion
            for data in _dicts(item.get("analysis"))
            for shortfall in _dicts(data.get("shortfalls"))
            if shortfall.get("cause") == "material_missing"
            for criterion in shortfall.get("criteria") or []
        }
        | {
            str(criterion)
            for data in _dicts(item.get("analysis"))
            if isinstance(data.get("waiting_on_material"), dict)
            for criterion in data["waiting_on_material"]
        }
    )
    return {
        "decision": feedback.get("decision"),
        "reason": feedback.get("reason", ""),
        "requirements": requirements,
        "task_updates": list(feedback.get("task_updates") or []),
        "filtered": list(feedback.get("filtered") or []),
        "waiting_on_material": waiting,
        "error": error,
    }


def _turn_ids(item: dict) -> dict[str, list[str]]:
    turns = {}
    for session, exchanges in (item.get("sessions") or {}).items() if isinstance(item.get("sessions"), dict) else ():
        ids = []
        for exchange in _dicts(exchanges):
            execution = exchange.get("execution") if isinstance(exchange.get("execution"), dict) else {}
            ids += [execution.get("turn_id")] + [row.get("turn_id") for row in _dicts(execution.get("records"))]
        turns[session] = _unique(turn for turn in ids if turn)
    return turns


def _rounds(run: dict, root: Path, scenario: dict, criteria: list[str], severity: dict) -> list[dict]:
    """Recorded rounds; each drill carries the card values it drew and the figures and deck facts computed for it, the
    k-th judging of a drill in `references.jsonl` belonging to round k."""
    cards = {path.stem for path in scenario["cards"]}
    checked: dict[str, list[dict]] = {}
    for row in _jsonl(root / REFERENCES):
        checked.setdefault(str(row.get("drill")), []).append(row)
    out = []
    for number, item in enumerate(_dicts(run.get("rounds")), start=1):
        drills, evidence, artifacts = _drills(item, root, cards)
        for drill in drills:
            rows = checked.get(drill["session"], [])
            row = rows[number - 1] if len(rows) >= number else {}
            drill["drawn"] = row.get("trip")
            drill["card_text"] = row.get("card")
            drill["references"] = row.get("references") if isinstance(row.get("references"), dict) else {}
        evaluation = _evaluation(item, severity)
        following = [_stem(str(data.get("file") or "")) for data in _dicts(item.get("curation")) if data.get("file")]
        out.append(
            {
                "number": number,
                "revision": Counter(artifacts).most_common(1)[0][0] if artifacts else None,
                "partial": False,
                "drills": drills,
                "evaluation": evaluation,
                "analysis": _analysis(item, criteria, evaluation["items"], _turn_ids(item)),
                "mechanism_evidence": evidence,
                "curation": following[0] if following else None,
            }
        )
    return out


def _opening(rows: list[dict]) -> tuple[str | None, str, bool]:
    """(drill session, customer message, whether the turn only reports background work) of an unrecorded turn."""
    session = None
    for row in rows:
        conversation = str(row.get("conversation") or "")
        if row.get("kind") == "dag.progress" and conversation.count(":") >= 2:
            session = conversation.split(":")[1]
    for row in rows:
        if row.get("kind") != "provider.request":
            continue
        messages = _dicts((row.get("parameters") or {}).get("messages"))
        users = [message for message in messages if message.get("role") == "user"]
        content = users[-1].get("content") if users else ""
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) for part in _dicts(content))
        match = CHAT_ID.search(str(content))
        if not match:
            continue
        lines = str(content).splitlines()
        ends = [index for index, line in enumerate(lines) if line.startswith("[END UNTRUSTED")]
        start = ends[-1] + 1 if ends else next((i for i, line in enumerate(lines) if not line.strip()), -1) + 1
        return match.group(1), "\n".join(lines[start:]).strip(), "[BEGIN UNTRUSTED subagent" in str(content)
    return session, "", False


def _unrecorded(root: Path, recorded: set[str]) -> list[tuple[str, list[dict], str | None]]:
    """Turns no recorded round holds, in the order they ran, each with the package prefix its runtime was bound to."""
    turns: dict[str, tuple[list[dict], str | None]] = {}
    skip = {*recorded, ""}
    for path in _generations(root):
        bound = None
        with path.open(errors="replace") as handle:
            for line in handle:
                field = TURN_FIELD.search(line, 0, 400)
                if field and (field.group(1) or "") in skip and '"runtime.bound"' not in line[:200]:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("kind") == "runtime.bound" and row.get("package"):
                    bound = Path(str(row["package"])).name.removeprefix("_curator_")
                turn = row.get("turn_id")
                if turn and turn not in recorded:
                    turns.setdefault(turn, ([], bound))[0].append(row)
    return [(turn, rows, bound) for turn, (rows, bound) in turns.items()]


def _partial_round(number: int, turns, root: Path, scenario: dict) -> dict | None:
    """A trial cut off before its round was recorded, rebuilt from the observations of its drill turns."""
    cards = {path.stem for path in scenario["cards"]}
    drills: dict[str, dict] = {}
    evidence, packages = [], []
    for turn_id, rows, bound in turns:
        session, customer, follow_up = _opening(rows)
        if session is None:
            continue
        packages += [bound] if bound else []
        drill = drills.setdefault(session, {"session": session, "card": _card(session, cards), "exchanges": []})
        folder = _kept(root, turn_id)
        kept = sorted(path for path in folder.iterdir() if path.is_file()) if folder.is_dir() else []
        index = len(drill["exchanges"]) + 1
        drill["exchanges"].append(
            _exchange(
                index,
                customer,
                rows,
                [_delivered(path, root) for path in kept],
                root,
                turn_id=turn_id,
                follow_up=follow_up,
            )
        )
        evidence += _mechanism(rows, session, index)
    if not drills:
        return None
    return {
        "number": number,
        "revision": Counter(packages).most_common(1)[0][0] if packages else None,
        "partial": True,
        "drills": list(drills.values()),
        "evaluation": {"remark": "", "items": [], "metrics": {}, "satisfied": []},
        "analysis": {
            "decision": None,
            "reason": "",
            "requirements": [],
            "task_updates": [],
            "filtered": [],
            "waiting_on_material": [],
            "error": None,
        },
        "mechanism_evidence": evidence,
        "curation": None,
    }


# Inputs, run facts and cost


def _skills(item: dict) -> set[str]:
    names = set()
    for data in _dicts(item.get("analysis")):
        for skill in (data.get("materials") or {}).get("skills") or []:
            if isinstance(skill, str) and "/" in skill and not skill.startswith("skill.builtin"):
                names.add(skill.split("/", 1)[1])
    return names


def _material_of(path) -> str:
    """The material an uploaded file belongs to: its folder, `uploads/<material>/<file>`."""
    return Path(str(path)).parent.name


def _materials(scenario: dict, run: dict, settings: dict, root: Path) -> list[dict]:
    """Each scenario material with its hash and when the owner gave it (see `given`)."""
    rounds = _dicts(run.get("rounds"))
    opening = {_material_of(path) for signal in _dicts(run.get("opening")) for path in signal.get("attachments") or []}
    if settings.get("disclose") == "all":
        opening |= set(scenario["materials"])
    elif settings.get("disclose") == "staged":
        opening |= set(scenario["initial"])
    if not opening and rounds:
        opening = _skills(rounds[0])
    handed: dict[str, int] = {}
    for number, item in enumerate(rounds, start=1):
        remark = "\n".join(str(signal.get("text") or "") for signal in _dicts(item.get("signals")))
        later = _skills(rounds[number]) if number < len(rounds) else set()
        for name, folder in scenario["materials"].items():
            head = _document(folder / "SKILL.md")[:160]
            if name not in opening and name not in handed and ((head and head in remark) or name in later):
                handed[name] = number
    home = root / "home"
    out = []
    for name, folder in scenario["materials"].items():
        present = (home / "skills" / name).is_dir() or (home / "uploads" / name).is_dir()
        given = "opening" if name in opening else "handed_over" if name in handed or present else "withheld"
        out.append(
            {
                "name": name,
                "sha256": _sha256(folder / "SKILL.md"),
                "given": given,
                "round": handed.get(name),
                "files": {
                    path.name: _sha256(path)
                    for path in sorted(folder.iterdir())
                    if path.is_file() and path.name != "SKILL.md"
                },
            }
        )
    return out


def _inputs(scenario: dict, run: dict, settings: dict, root: Path, played: set[str]) -> dict:
    opening = "\n\n".join(str(signal.get("text")) for signal in _dicts(run.get("opening")) if signal.get("text"))
    return {
        "scenario": scenario["name"],
        "profile": str(run.get("task") or scenario["profile"]),
        "onboarding": opening or scenario["onboarding"],
        "materials": _materials(scenario, run, settings, root),
        "cards": [
            {"name": path.stem, "sha256": _sha256(path), "played": path.stem in played} for path in scenario["cards"]
        ],
        "baseline": settings.get("baseline") if isinstance(settings.get("baseline"), dict) else {},
    }


def _revisions(root: Path, curations: list[dict], rounds: list[dict]) -> list[str]:
    """Artifact ids in install order: the hired baseline (as its package prefix when only that is known) first."""
    bound = []
    for path in sorted(root.glob("*/observations.jsonl"), key=lambda path: path.stat().st_mtime):
        for row in _jsonl(path):
            if row.get("kind") == "runtime.bound" and row.get("package"):
                bound.append(Path(str(row["package"])).name.removeprefix("_curator_"))
                break
    installed = [curation["active_artifact"] for curation in curations if curation.get("active_artifact")]
    used = [item["revision"] for item in rounds if item.get("revision")]
    ordered: list[str] = []
    for artifact in [*bound[:1], *installed, *used]:
        same = next(
            (i for i, known in enumerate(ordered) if known.startswith(artifact) or artifact.startswith(known)), None
        )
        if same is None:
            ordered.append(artifact)
        elif len(artifact) > len(ordered[same]):
            ordered[same] = artifact
    return ordered


def _cost(root: Path, state_root) -> dict | None:
    """The suite's own spend when it summarised the run, else the model calls the audit spans of the run's Raven home
    and of every child harness recorded (see `experimental.simulation.suite.spend`)."""
    summary = _json(root / SUMMARY)
    spend = summary.get("spend") if isinstance(summary, dict) else None
    if isinstance(spend, dict):
        parts = {key: float(value) for key, value in spend.items() if key != "total" and isinstance(value, int | float)}
        total = spend.get("total")
        return {
            "total": float(total) if isinstance(total, int | float) else sum(parts.values()),
            "by_part": parts,
            "source": SUMMARY,
        }
    from .suite import spend

    parts = spend(Path(state_root) / root.name if state_root else root / "traces" / "absent", root)
    total = parts.pop("total")
    return {"total": total, "by_part": parts, "source": "audit spans (children included)"} if total else None


def _times(root: Path, path: Path, settings: dict) -> tuple[str | None, str | None]:
    started = settings.get("started")
    if started is None:
        files = [path, *root.glob("curation/*.json"), *root.glob("analysis/*.json"), *_generations(root)]
        started = min((file.stat().st_mtime for file in files if file.is_file()), default=None)
    return _iso(started), _iso(path.stat().st_mtime)


def _steps(run: dict, curations: list[dict], rounds: list[dict], root: Path) -> list[dict]:
    """How far the loop got: onboarding, then each round's trial, review, analysis and curation, then the ending."""
    steps = [
        {"step": "onboarding", "round": 0, "state": curation["outcome"], "ref": curation["id"]}
        for curation in curations
        if curation["round"] == 0
    ]
    for item in rounds:
        number = item["number"]
        steps.append({"step": "trial", "round": number, "state": "unfinished" if item["partial"] else "done"})
        if item["partial"]:
            continue
        steps.append(
            {"step": "evaluation", "round": number, "state": "done" if item["evaluation"]["items"] else "missing"}
        )
        analysis = item["analysis"]
        steps.append(
            {
                "step": "analysis",
                "round": number,
                "state": analysis["decision"] or ("error" if analysis["error"] else "missing"),
            }
        )
        steps += [
            {"step": "curation", "round": number, "state": curation["outcome"], "ref": curation["id"]}
            for curation in curations
            if curation["round"] == number
        ]
    progress = _json(root / "progress" / "curation.json")
    if isinstance(progress, dict) and not progress.get("finished") and run.get("status") == "running":
        steps.append({"step": "curation", "round": len(rounds), "state": "running", "stage": progress.get("stage")})
    steps.append({"step": "end", "state": run.get("status"), "note": run.get("error") or run.get("stop")})
    return steps


def placed(argv, values: dict[str, str]) -> list[str]:
    """`argv` with the value of every option `values` names replaced by its placeholder, as `--opt v` or `--opt=v`."""
    out, pending = [], None
    for token in map(str, argv):
        name, equals, _ = token.partition("=")
        if pending is not None:
            out.append(pending)
            pending = None
        elif equals and name in values:
            out.append(f"{name}={values[name]}")
        else:
            out.append(token)
            pending = values.get(token)
    return out


def _command(settings: dict) -> str | None:
    """The simulation command that reproduces this run's setup; configuration, homes and folders are the reader's own.

    A run that stored its command line (`argv`) gets it back, with its seed and analysis added when it left them to
    the draw or the default: a later default must not change what the command reproduces. An older run's command is
    rebuilt from its settings as far as they go: a run with no `analysis` setting predates it and ran the owner's own
    analysis, a partition is given as `--partition`, and a model is named only where it differs from the one its role
    falls back to.
    """
    if not settings:
        return None
    if isinstance(settings.get("argv"), list):
        argv = placed(settings["argv"], PLACEHOLDERS)
        for option, key in (("--seed", "seed"), ("--analysis", "analysis")):
            given = any(token == option or token.startswith(f"{option}=") for token in argv)
            if not given and settings.get(key) is not None:
                argv += [option, str(settings[key])]
        return shlex.join([*COMMAND, *argv])
    argv = [
        *COMMAND,
        *(item for option in ("--config", "--home", "--state-dir") for item in (option, PLACEHOLDERS[option])),
    ]
    if settings.get("scenario"):
        argv += ["--scenario", str(settings["scenario"])]
    chain, disclose = settings.get("chain"), settings.get("disclose")
    if chain == "partition" or (not chain and isinstance(disclose, list)):
        argv += ["--partition", json.dumps(disclose, ensure_ascii=False)]
    elif chain:
        argv += ["--chain", str(chain)]
    else:
        argv += [
            flag for key in ("deliver", "disclose") if settings.get(key) for flag in (f"--{key}", str(settings[key]))
        ]
    argv += ["--analysis", str(settings.get("analysis") or "owner")]
    for key in ("rounds", "turns", "repeats"):
        if settings.get(key) is not None:
            argv += [f"--{key}", str(settings[key])]
    budget = settings.get("curator_budget") if isinstance(settings.get("curator_budget"), dict) else {}
    argv += [
        item
        for key in ("calls", "queries")
        if budget.get(key) is not None
        for item in (f"--curator-{key}", str(budget[key]))
    ]
    models = settings.get("models") if isinstance(settings.get("models"), dict) else {}
    for role, fallback in MODEL_FALLBACKS.items():
        if models.get(role) and models[role] != models.get(fallback):
            argv += [f"--{role}-model", str(models[role])]
    if models.get("subagents"):
        argv += ["--subagent-model", str(models["subagents"])]
    efforts = settings.get("efforts") if isinstance(settings.get("efforts"), dict) else {}
    argv += [
        item
        for role in ("curator", "traveller")
        if efforts.get(role)
        for item in (f"--{role}-effort", str(efforts[role]))
    ]
    if settings.get("cards"):
        argv += ["--cards", *map(str, settings["cards"])]
    if (settings.get("concurrent_drills") or 1) > 1:
        argv += ["--concurrent-drills", str(settings["concurrent_drills"])]
    if settings.get("seed") is not None:
        argv += ["--seed", str(settings["seed"])]
    if settings.get("without"):
        argv += ["--without", *map(str, settings["without"])]
    return shlex.join(argv)


# Ledger


def _attach(curations: list[dict], rounds: dict[int, dict], criteria: list[str], rules: dict) -> None:
    """Attach each curation change to criteria in place (see the module docstring), recording how."""
    for curation in curations:
        item = rounds.get(curation["round"]) if curation["round"] else None
        pool = _unique(
            criterion
            for requirement in (item["analysis"]["requirements"] if item else [])
            for criterion in requirement["criteria"]
        )
        for change in curation["changes"]:
            cited = markers(change["reason"])
            named = [criterion for criterion in criteria if _names(str(change["reason"]), criterion)]
            marked = [
                criterion for criterion in criteria if cited and _marker_match(cited, markers(rules.get(criterion, "")))
            ]
            if pool:
                chosen = [criterion for criterion in pool if criterion in named or criterion in marked]
                attached = [(c, "named" if c in named else "marker") for c in chosen] or [(c, "round") for c in pool]
            else:
                attached = [(c, "named") for c in named] + [(c, "marker") for c in marked if c not in named]
            change["attached"] = [{"criterion": criterion, "link": how} for criterion, how in attached]


def _verdict(passes: int, fails: int) -> str:
    if passes and fails:
        return "mixed"
    return "pass" if passes else "fail" if fails else "unknown"


def _status(timeline: list[dict]) -> str:
    judged = [(moment["round"], moment["result"]) for moment in timeline if moment["result"] != "unknown"]
    if not judged:
        return "not_exercised"
    if all(result == "pass" for _, result in judged):
        return "never_failed"
    if judged[-1][1] != "pass":
        return "still_failing"
    start = judged[-1][0]
    for number, result in reversed(judged):
        if result != "pass":
            break
        start = number
    return f"held_since_round_{start}"


def _moment(criterion: str, number: int, item: dict | None, curations: list[dict]) -> dict:
    items = [entry for entry in (item["evaluation"]["items"] if item else []) if entry["criterion"] == criterion]
    passes = sum(entry["result"] == "pass" for entry in items)
    fails = sum(entry["result"] == "fail" for entry in items)
    sedimented = {
        (curation["scope"], change["target"])
        for curation in curations
        if curation["outcome"] == "installed" and curation["round"] is not None and curation["round"] < number
        for change in curation["changes"]
        if any(entry["criterion"] == criterion for entry in change.get("attached", []))
    }
    sessions = {entry["session"] for entry in items if entry.get("session")}
    evidence = Counter()
    for row in item["mechanism_evidence"] if item else []:
        if (row.get("scope", "root"), row["target"]) in sedimented and (not sessions or row["session"] in sessions):
            evidence[f"{row['kind']}/{row['decision']}"] += row["count"]
    return {
        "round": number,
        "result": _verdict(passes, fails),
        "fails": fails,
        "passes": passes,
        "requirements": [
            index
            for index, requirement in enumerate(item["analysis"]["requirements"] if item else [])
            if criterion in requirement["criteria"]
        ],
        "changes": [
            {
                "curation": curation["id"],
                "scope": curation["scope"],
                "target": change["target"],
                "facet": change["facet"],
                "paths": change["paths"],
                "outcome": curation["outcome"],
                "link": entry["link"],
            }
            for curation in curations
            if curation["round"] == number
            for change in curation["changes"]
            for entry in change.get("attached", [])
            if entry["criterion"] == criterion
        ],
        "evidence": dict(sorted(evidence.items())),
    }


def ledger(criteria: list[dict], rounds: list[dict], curations: list[dict]) -> list[dict]:
    """Per criterion: its verdicts, requirements, attached changes and mechanism evidence round by round."""
    out = []
    for meta in criteria:
        criterion = meta["id"]
        timeline = [_moment(criterion, 0, None, curations)] if any(c["round"] == 0 for c in curations) else []
        timeline += [_moment(criterion, item["number"], item, curations) for item in rounds]
        rule = str(meta.get("check") or "")
        out.append(
            {
                "criterion": criterion,
                "severity": meta.get("severity", "unknown"),
                "rule": rule,
                "timeline": timeline,
                "sedimented_in": [
                    {
                        "round": curation["round"],
                        "scope": curation["scope"],
                        "facet": change["facet"],
                        "target": change["target"],
                        "paths": change["paths"],
                        "link": entry["link"],
                    }
                    for curation in curations
                    if curation["outcome"] == "installed"
                    for change in curation["changes"]
                    for entry in change.get("attached", [])
                    if entry["criterion"] == criterion
                ],
                "status": _status(timeline),
            }
        )
    return out


def _criteria(scenario: dict, run: dict) -> list[dict]:
    """The scenario's criteria in its order, then any other criterion the record judged, with its check text.

    When the record judged a criterion the scenario does not define, the run followed another version of the
    scenario, and only the judged criteria are listed.
    """
    judged = {}
    for item in _dicts(run.get("rounds")):
        for signal in _dicts(item.get("signals")):
            for entry in _dicts(signal.get("items")):
                key = str(entry.get("id", ""))
                if key and key not in judged:
                    judged[key] = {"id": key, "check": entry.get("expected", ""), "severity": "unknown"}
    found = {
        row["id"]: {"id": row["id"], "check": row.get("check", ""), "severity": row.get("severity", "standard")}
        for row in scenario["criteria"]
    }
    if not judged.keys() <= found.keys():
        return [found.get(key, row) for key, row in judged.items()]
    return list(found.values()) + [row for key, row in judged.items() if key not in found]


# Building and exporting


def _build(run_dir, scenario_dir=None, state_root=None) -> tuple[dict, dict]:
    run_dir = Path(run_dir)
    path = _record_path(run_dir)
    root = path.parents[1]
    settings = _json(root / SETTINGS)
    settings = settings if isinstance(settings, dict) else {}
    scenario = _scenario(scenario_dir, settings)
    run = _joined(path)
    criteria = _criteria(scenario, run)
    ids = [row["id"] for row in criteria]
    severity = {row["id"]: row["severity"] for row in criteria}
    rounds = _rounds(run, root, scenario, ids, severity)
    recorded = {turn for item in _dicts(run.get("rounds")) for turns in _turn_ids(item).values() for turn in turns}
    partial = _partial_round(len(rounds) + 1, _unrecorded(root, recorded), root, scenario)
    if partial:
        rounds.append(partial)
    curations = _curations(run, root)
    for item in rounds:
        if item["curation"] is None:
            item["curation"] = next(
                (c["id"] for c in curations if c["round"] == item["number"] and c["scope"] == "root"), None
            )
    revisions = _revisions(root, curations, rounds)
    for item in rounds:
        if item["partial"] and item["revision"]:
            item["revision"] = next((full for full in revisions if full.startswith(item["revision"])), item["revision"])
    _attach(curations, {item["number"]: item for item in rounds}, ids, {row["id"]: row["check"] for row in criteria})
    played = {drill["card"] for item in rounds for drill in item["drills"]}
    started, updated = _times(root, path, settings)
    unknown = sorted({row["id"] for row in criteria} - {row["id"] for row in scenario["criteria"]})
    warnings = []
    if unknown:
        warnings.append(
            f"scenario {scenario['name']} does not define {len(unknown)} judged criteria ({', '.join(unknown[:5])}"
            f"{', ...' if len(unknown) > 5 else ''}); the ledger lists the record's own criteria"
        )
    warnings += [
        f"curation {c['id']} was joined to its round by its feedback" for c in curations if c["joined"] != "by record"
    ]
    linked = {
        file["path"]
        for item in rounds
        for drill in item["drills"]
        for exchange in drill["exchanges"]
        for file in exchange["delivered"]
        if file.get("path")
    }
    kept = sorted(
        path
        for path in [*root.glob("deliverables/*/*"), *root.glob(f"{REPLICAS}/*/deliverables/*/*")]
        if path.is_file()
    )
    suite = _json(root / SUMMARY)
    inputs = _inputs(scenario, run, settings, root, played)
    record = {
        "schema": SCHEMA,
        "run": {
            "name": root.name,
            "record_id": path.stem,
            "task_id": run.get("task_id"),
            "status": run.get("status"),
            "error": run.get("error"),
            "stop": run.get("stop"),
            "started": started,
            "updated": updated,
            "chain": settings.get("chain"),
            "settings": {
                **({"curator": run["curator"]} if run.get("curator") else {}),
                **{key: value for key, value in settings.items() if key != "baseline"},
            },
            "revisions": revisions,
            "steps": _steps(run, curations, rounds, root),
            "reproduce": _command(settings),
            "suite": {key: value for key, value in suite.items() if key != "spend"}
            if isinstance(suite, dict)
            else None,
            "warnings": warnings,
        },
        "inputs": inputs,
        "curations": curations,
        "rounds": rounds,
        "ledger": ledger(criteria, rounds, curations),
        "unlinked_deliverables": [
            _delivered(file, root)
            for file in kept
            if _relative(file, root) not in linked and file.name not in NEVER_COPIED
        ],
        "node_requirements": _node_requirements(root),
        "cost": _cost(root, state_root),
        "isolation": _json(root / ISOLATION),
        "attribution": _json(root / ATTRIBUTION),
    }
    record["value"] = judge(record)
    return redact(record), {"root": root, "scenario": scenario, "labels": _labels(scenario)}


def _node_requirements(root: Path) -> list[dict]:
    out = []
    for file in sorted((root / "home" / "playbooks").glob("*/nodes/*/requirements.json")):
        data = _json(file)
        out.append(
            {
                "playbook": file.parents[2].name,
                "node": file.parent.name,
                "path": _relative(file, root),
                "sha256": _sha256(file),
                "requirements": data if data is not None else "unreadable",
            }
        )
    return out


def build_record(run_dir: Path, scenario_dir: Path | None = None, *, state_root: Path | None = None) -> dict:
    """The run's cultivation record as JSON-serialisable data (schema 1); see the module docstring for its rules."""
    return _build(run_dir, scenario_dir, state_root)[0]


def _copy(source: Path, target: Path) -> bool:
    """Copy one file, redacting text files; True when redaction changed the copy."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() in TEXT_SUFFIXES:
        text = source.read_text(errors="replace")
        clean = redact(text)
        target.write_text(clean)
        shutil.copystat(source, target)
        return clean != text
    shutil.copy2(source, target)
    return False


def _assets(record: dict, root: Path, out: Path) -> set[str]:
    """Copy every delivered file and page image into `assets/`, rewriting the record's paths; returns redacted copies."""
    redacted, done = set(), {}

    def place(relative):
        if not relative:
            return relative
        if relative not in done:
            source = _inside(root / relative, root)
            if source is None or source.name in NEVER_COPIED or NEVER_COPIED & set(Path(relative).parts):
                done[relative] = None
            else:
                if _copy(source, out / "assets" / relative):
                    redacted.add(f"assets/{relative}")
                done[relative] = f"assets/{relative}"
        return done[relative]

    files = [
        file
        for item in record["rounds"]
        for drill in item["drills"]
        for exchange in drill["exchanges"]
        for file in exchange["delivered"]
    ]
    for file in [*files, *record["unlinked_deliverables"]]:
        file["path"] = place(file["path"])
        file["pages"] = [page for page in (place(page) for page in file["pages"]) if page]
    return redacted


def _scenario_assets(record: dict, scenario: dict, out: Path) -> set[str]:
    """Copy the scenario inputs into `assets/scenario/` and point each material at its copy."""
    source = scenario["root"]
    redacted = set()
    names = ("profile.md", "onboarding.md", "handover.md", "scenario.json", "record.md", "reference.md")
    wanted = [source / name for name in names]
    wanted += sorted((source / "personas").rglob("*"))
    wanted += sorted(path for folder in scenario["materials"].values() for path in Path(folder).rglob("*"))
    for path in wanted:
        if path.is_file() and path.name not in NEVER_COPIED and "__pycache__" not in path.parts:
            relative = path.relative_to(source).as_posix()
            if _copy(path, out / "assets" / "scenario" / relative):
                redacted.add(f"assets/scenario/{relative}")
    for material in record["inputs"]["materials"]:
        material["copy"] = f"assets/scenario/materials/{material['name']}/SKILL.md"
    return redacted


def _prepare(out: Path) -> None:
    if out.is_dir() and any(out.iterdir()):
        if not (out / "manifest.json").is_file():
            raise ValueError(f"{out} is not empty and holds no earlier record export")
        shutil.rmtree(out / "assets", ignore_errors=True)
        for name in ("record.json", "ledger.json", "transcript.md", "manifest.json"):
            (out / name).unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)


def export_record(
    run_dir: Path, out_dir: Path, scenario_dir: Path | None = None, state_root: Path | None = None
) -> Path:
    """Write the record bundle (record, ledger, transcript, assets, manifest) to `out_dir`; returns `out_dir`."""
    out = Path(out_dir)
    record, context = _build(run_dir, scenario_dir, state_root)
    _prepare(out)
    redacted = _assets(record, context["root"], out) | _scenario_assets(record, context["scenario"], out)
    (out / "record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    (out / "ledger.json").write_text(json.dumps(record["ledger"], ensure_ascii=False, indent=2))
    (out / "transcript.md").write_text(redact(transcript(record, context["labels"])))
    files = []
    for path in sorted(file for file in out.rglob("*") if file.is_file() and file.name != "manifest.json"):
        relative = path.relative_to(out).as_posix()
        files.append(
            {
                "path": relative,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
                "modified": _iso(path.stat().st_mtime),
                **({"redacted": True} if relative in redacted else {}),
            }
        )
    manifest = {
        "schema": SCHEMA,
        "run": record["run"]["name"],
        "record_id": record["run"]["record_id"],
        "generated": _iso(datetime.now(UTC).timestamp()),
        "files": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return out


# Transcript


def _fenced(text: str, language: str = "") -> list[str]:
    """A code block whose fence is longer than any backtick run inside it."""
    runs = [len(run) for run in re.findall(r"`+", text or "")]
    fence = "`" * max(3, max(runs, default=0) + 1)
    return [fence + language, text or "", fence, ""]


def _quote(text) -> str:
    lines = str(text or "").strip().splitlines()
    return "\n".join(f"> {line}" if line.strip() else ">" for line in lines) if lines else "> (empty)"


def _cell(value, limit: int | None = None) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) if value is not None else ""
    if limit and len(text) > limit:
        text = text[: limit - 3] + "..."
    return text.replace("|", "\\|").replace("\n", "<br>")


def _table(header: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return lines + ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows] + [""]


def _label(revisions: list[str], artifact) -> str:
    if not artifact:
        return "unknown"
    index = next((i for i, known in enumerate(revisions) if known == artifact or known.startswith(artifact)), None)
    return f"v{index} (`{str(artifact)[:12]}`)" if index is not None else f"`{str(artifact)[:12]}`"


def _inputs_md(record: dict, labels: dict) -> list[str]:
    inputs, run = record["inputs"], record["run"]
    lines = [f"## {labels['inputs']}", "", f"### {labels['profile']}", "", _quote(inputs["profile"]), ""]
    lines += [f"### {labels['opening']}", "", _quote(inputs["onboarding"]), ""]
    lines += [f"### {labels['materials']}", ""]
    lines += _table(
        ["Material", "Given", "Round", "SHA-256", "Copy"],
        [
            [m["name"], m["given"], m.get("round") or "", m["sha256"] or "", m.get("copy", "")]
            for m in inputs["materials"]
        ],
    )
    lines += [f"### {labels['cards']}", ""]
    lines += _table(
        ["Card", "Played", "SHA-256"],
        [[c["name"], "yes" if c["played"] else "no", c["sha256"]] for c in inputs["cards"]],
    )
    baseline = inputs["baseline"]
    lines += [f"### {labels['baseline']}", "", f"{len(baseline)} files fingerprinted at the start of the run.", ""]
    if baseline:
        rows = [[path, digest] for path, digest in list(baseline.items())[:BASELINE_ROWS]]
        lines += _table(["Path", "SHA-256"], rows)
        if len(baseline) > BASELINE_ROWS:
            lines += [f"... and {len(baseline) - BASELINE_ROWS} more in record.json (`inputs.baseline`).", ""]
    lines += [f"### {labels['settings']}", ""]
    return lines + _fenced(json.dumps(run["settings"], ensure_ascii=False, indent=2), "json")


def _curation_md(curation: dict) -> list[str]:
    title = f"#### Curation `{curation['id']}` (scope {curation['scope']}): {curation['outcome']}"
    lines = [title, ""]
    if curation["joined"] != "by record":
        lines += [f"Joined to this round {curation['joined']}.", ""]
    if curation["error"]:
        lines += ["Error:", "", _quote(curation["error"]), ""]
    if curation["understanding"]:
        lines += [f"Understanding ({curation['plan_source']}):", "", _quote(curation["understanding"]), ""]
    if curation["design"]:
        lines += ["Design:", "", _quote(curation["design"]), ""]
    if curation["changes"]:
        lines += _table(
            ["Target", "Facet", "Reason", "Expected", "Criteria"],
            [
                [
                    c["target"],
                    c["facet"],
                    c["reason"],
                    c["expected"],
                    ", ".join(f"{a['criterion']} ({a['link']})" for a in c.get("attached", [])),
                ]
                for c in curation["changes"]
            ],
        )
    validation = curation["validation"]
    if validation["errors"] or validation["bound"]:
        lines += [
            f"Validation: errors {validation['errors'] or 'none'}; bound targets {', '.join(validation['bound']) or 'none'}.",
            "",
        ]
    for entry in curation["artifact_diff"]:
        lines += [
            f"`{entry['target']}` `{entry['path']}`: {entry['change']}, +{entry['lines_added']} -{entry['lines_removed']}",
            "",
        ]
        lines += _fenced(entry["diff"], "diff")
    return lines


def _drills_md(item: dict, labels: dict) -> list[str]:
    lines = [f"### {labels['drills']}", ""]
    for drill in item["drills"]:
        lines += [f"#### {drill['session']} (card {drill['card']})", ""]
        if drill.get("drawn"):
            lines += ["Card values this round: " + ", ".join(f"{k} {v}" for k, v in drill["drawn"].items()), ""]
        if drill.get("card_text"):
            lines += ["Card as the simulated customer read it:", "", _quote(drill["card_text"]), ""]
        if drill.get("references"):
            lines += ["Figures and deck facts computed for the owner:", ""]
            lines += _fenced(json.dumps(drill["references"], ensure_ascii=False, indent=2), "json")
        for exchange in drill["exchanges"]:
            who = labels["follow_up"] if exchange.get("follow_up") else labels["customer"]
            lines += [f"**{exchange['turn']}. {who}**", "", _quote(exchange["customer"]), ""]
            lines += [f"**{labels['employee']}**", "", _quote(exchange["assistant"]), ""]
            for file in exchange["delivered"]:
                lines += [
                    f"Delivered: `{file['name']}` ({file['path'] or 'not kept'}, sha256 {file['sha256'] or 'n/a'})",
                    "",
                ]
            for run in exchange["playbook_runs"]:
                lines += [f"Playbook run `{run['run_id']}`:", ""]
                lines += _table(
                    ["Node", "Sub-agent", "Status", "Seconds", "Output summary"],
                    [
                        [n["id"], n["subagent"], n["status"], n["seconds"], _cell(n["summary"], 400)]
                        for n in run["nodes"]
                    ],
                )
            if exchange["errors"]:
                lines += ["Execution errors: " + "; ".join(exchange["errors"]), ""]
    return lines


def _pages_md(item: dict, labels: dict) -> list[str]:
    lines = [f"### {labels['pages']}", ""]
    shown = False
    for drill in item["drills"]:
        for exchange in drill["exchanges"]:
            for file in exchange["delivered"]:
                if not file["pages"]:
                    continue
                shown = True
                lines += [f"#### {drill['session']}, turn {exchange['turn']}: {file['name']}", ""]
                lines += [
                    f"![{file['name']} page {index}]({page})" for index, page in enumerate(file["pages"], start=1)
                ]
                lines += [""]
    return lines + ([] if shown else ["No page images were kept this round.", ""])


def _review_md(item: dict, labels: dict) -> list[str]:
    evaluation = item["evaluation"]
    lines = [f"### {labels['evaluation']}", ""]
    if not evaluation["items"] and not evaluation["remark"]:
        return lines + ["No review was recorded.", ""]
    remark, _, handed = evaluation["remark"].partition(HANDED_OVER)
    lines += [_quote(remark), ""]
    if handed:
        lines += [f"({handed.count(HANDED_OVER) + 1} handed-over document(s) follow the remark in record.json.)", ""]
    lines += _table(
        ["Criterion", "Severity", "Result", "Session", "Actual", "Note"],
        [
            [e["criterion"], e["severity"], e["result"], e["session"] or "", e["actual"], e["note"]]
            for e in evaluation["items"]
        ],
    )
    if evaluation["metrics"]:
        lines += ["Metrics: " + ", ".join(f"{key} {value}" for key, value in evaluation["metrics"].items()), ""]
    return lines


def _analysis_md(item: dict, labels: dict) -> list[str]:
    analysis = item["analysis"]
    lines = [f"### {labels['analysis']}", ""]
    if analysis["error"]:
        lines += [f"The analysis failed: {analysis['error']}", ""]
    if analysis["decision"]:
        lines += [f"Decision: **{analysis['decision']}**", "", _quote(analysis["reason"]), ""]
    for index, requirement in enumerate(analysis["requirements"]):
        criteria = ", ".join(requirement["criteria"]) or "none"
        lines += [f"{index}. **{requirement['behavior']}**", ""]
        lines += [f"   - Observed: {requirement['observed']}"]
        lines += [f"   - Evidence: {evidence}" for evidence in requirement["evidence"]]
        lines += [f"   - Acceptance: {requirement['acceptance']}"]
        lines += [
            f"   - Strength {requirement['strength']}, recurrence {requirement['recurrence']}; criteria {criteria} ({requirement['link']})",
            "",
        ]
    if analysis["task_updates"]:
        lines += ["Task updates:", ""] + [f"- {update}" for update in analysis["task_updates"]] + [""]
    return lines


def _mechanism_md(item: dict, labels: dict) -> list[str]:
    lines = [f"### {labels['mechanism']}", ""]
    if not item["mechanism_evidence"]:
        return lines + ["No planning, participant, component or refusal rows were recorded.", ""]
    grouped: dict[tuple, dict] = {}
    for row in item["mechanism_evidence"]:
        group = grouped.setdefault(
            (row["session"], row.get("scope", "root"), row["kind"], row["target"], row["decision"]),
            {**row, "turns": [], "count": 0},
        )
        group["turns"].append(row["turn"])
        group["count"] += row["count"]
        group["summary"] = row["summary"] or group["summary"]
    lines += ["Per drill; the record keeps each turn separately.", ""]
    return lines + _table(
        ["Session", "Turns", "Kind", "Target", "Decision", "Count", "Last summary"],
        [
            [
                row["session"],
                ", ".join(map(str, _unique(row["turns"]))),
                row["kind"],
                row["target"] if row.get("scope", "root") == "root" else f"{row['scope']}: {row['target']}",
                f"**{row['decision']}**" if row["intervention"] else row["decision"],
                row["count"],
                _cell(row["summary"], 300),
            ]
            for row in grouped.values()
        ],
    )


def _ledger_md(record: dict, labels: dict) -> list[str]:
    lines = [f"## {labels['ledger']}", ""]
    lines += _table(
        ["Criterion", "Severity", "Status", "Sedimented in"],
        [
            [
                entry["criterion"],
                entry["severity"],
                entry["status"],
                "; ".join(
                    f"round {s['round']} {s['scope']} {s['target']} ({s['facet']})" for s in entry["sedimented_in"]
                ),
            ]
            for entry in record["ledger"]
        ],
    )
    for entry in record["ledger"]:
        lines += [f"### {entry['criterion']} ({entry['severity']}): {entry['status']}", "", _quote(entry["rule"]), ""]
        lines += _table(
            ["Round", "Result", "Pass", "Fail", "Requirements", "Changes", "Evidence"],
            [
                [
                    moment["round"],
                    moment["result"],
                    moment["passes"],
                    moment["fails"],
                    ", ".join(map(str, moment["requirements"])),
                    "; ".join(
                        f"{c['target']} {', '.join(c['paths'])} ({c['outcome']}, {c['link']})"
                        for c in moment["changes"]
                    ),
                    ", ".join(f"{key} x{count}" for key, count in moment["evidence"].items()),
                ]
                for moment in entry["timeline"]
            ],
        )
    return lines


def _cost_md(record: dict, labels: dict) -> list[str]:
    run, cost = record["run"], record["cost"]
    lines = [f"## {labels['cost']}", ""]
    if cost:
        lines += [f"Total ${cost['total']:.4f} (from {cost['source']}).", ""]
        lines += _table(["Part", "USD"], [[part, f"{value:.4f}"] for part, value in cost["by_part"].items()])
    else:
        lines += ["No spend record was found for this run.", ""]
    if run["reproduce"]:
        lines += ["Reproduce the setup:", ""] + _fenced(run["reproduce"], "bash")
    lines += [
        f"Record `iteration/{run['record_id']}.json`; revisions in install order: {', '.join(f'`{r[:12]}`' for r in run['revisions']) or 'none'}.",
        "",
    ]
    lines += [
        "Re-export: `uv run python -m experimental.simulation.record <run dir> --out <dir>`. Every file here is listed with its SHA-256 in `manifest.json`.",
        "",
    ]
    return lines


def _value_md(record: dict, labels: dict) -> list[str]:
    value = record.get("value")
    if not value:
        return []
    lines = [f"## {labels['value']}", "", f"**{value['verdict']}** (score {value['score']}).", ""]
    lines += _table(
        ["Condition", "Met", "Detail"],
        [[name, "yes" if check["ok"] else "no", check["detail"]] for name, check in value["checks"].items()],
    )
    if value.get("cultivated"):
        lines += ["Cultivated rules:", ""]
        lines += _table(
            ["Rule", "Severity", "Failed in", "Answered after", "Held in", "Surfaces"],
            [
                [
                    item["criterion"],
                    item["severity"],
                    ", ".join(map(str, item["failed_in"])),
                    item["answered_after"],
                    ", ".join(map(str, item["held_in"])),
                    ", ".join(item["surfaces"]),
                ]
                for item in value["cultivated"]
            ],
        )
    for title, rows in (("Cases", value["cases"]), ("Held cases", value["held_cases"])):
        if rows:
            lines += [f"{title}:", ""]
            lines += _table(
                [
                    "Rule",
                    "Severity",
                    "Scope",
                    "Targets",
                    "Sedimented after",
                    "Failed in",
                    "Held in",
                    "Rows",
                    "Interventions",
                ],
                [
                    [
                        case["criterion"],
                        case["severity"],
                        case["sedimented"]["scope"],
                        ", ".join(case["targets"]),
                        case["sedimented"]["round"],
                        ", ".join(map(str, case["failed_in"])) or "-",
                        ", ".join(map(str, case["held_in"])),
                        case["mechanism_rows"],
                        case["interventions"],
                    ]
                    for case in rows
                ],
            )
    return lines


def transcript(record: dict, labels: dict | None = None) -> str:
    """The record as Markdown, readable without the page: inputs, onboarding, each round, the ledger and the cost."""
    labels = {**LABELS, **(labels or {})}
    run = record["run"]
    lines = [f"# {labels['title']}: {run['name']}", ""]
    lines += [
        f"- Status: {run['status']}; chain: {run['chain'] or 'not recorded'}; started {run['started']}; updated {run['updated']}"
    ]
    if run["error"] or run["stop"]:
        lines += [f"- Ended: {_cell(run['error'] or run['stop'], 600)}"]
    lines += [
        "- Steps: "
        + " -> ".join(
            f"{step['step']}{' ' + str(step['round']) if step.get('round') else ''} {step['state']}"
            for step in run["steps"]
        )
    ]
    lines += [f"- Warning: {warning}" for warning in run["warnings"]] + [""]
    lines += _inputs_md(record, labels)
    lines += [f"## {labels['onboarding']}", ""]
    onboarding = [curation for curation in record["curations"] if curation["round"] == 0]
    for curation in onboarding:
        lines += _curation_md(curation)
    if not onboarding:
        lines += ["No onboarding curation was recorded.", ""]
    for item in record["rounds"]:
        title = labels["round"].replace("{number}", str(item["number"]))
        suffix = " (unfinished trial)" if item["partial"] else f" on {_label(run['revisions'], item['revision'])}"
        lines += [f"## {title}{suffix}", ""]
        lines += (
            _drills_md(item, labels) + _pages_md(item, labels) + _review_md(item, labels) + _analysis_md(item, labels)
        )
        lines += [f"### {labels['curation']}", ""]
        following = [curation for curation in record["curations"] if curation["round"] == item["number"]]
        for curation in following:
            lines += _curation_md(curation)
        if not following:
            lines += ["No curation followed this round.", ""]
        lines += _mechanism_md(item, labels)
    unplaced = [curation for curation in record["curations"] if curation["round"] is None]
    if unplaced:
        lines += ["## Curations not joined to a round", ""]
        for curation in unplaced:
            lines += _curation_md(curation)
    lines += _ledger_md(record, labels) + _value_md(record, labels) + _cost_md(record, labels)
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None) -> Path:
    parser = argparse.ArgumentParser(description="Export one cultivation run as a self-contained cultivation record.")
    parser.add_argument("run_dir", type=Path, help="The run's record directory (or one iteration record file)")
    parser.add_argument("--out", type=Path, required=True, help="A new or earlier-export directory for the bundle")
    parser.add_argument("--state-root", type=Path, help="Where each run's Raven home lives, for spend from audit spans")
    parser.add_argument("--scenario", type=Path, help="Scenario directory; defaults to the one the settings name")
    args = parser.parse_args(argv)
    out = export_record(args.run_dir, args.out, args.scenario, args.state_root)
    print(out)
    return out


if __name__ == "__main__":
    main()
