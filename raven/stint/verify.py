"""Running a project's own checks, and reporting what they said.

The one signal in a round that no model produced. A check is a real command --
a build, a boot, a test suite -- run over the tree as the role left it, so what
the check saw and what the next role reads are the same code. Its verdict is
not an opinion and cannot be argued with, which is exactly why a round is
worth more with one than without.

A check that needs a display and finds none is *skipped*, not failed: the
honest answer to a frame-rate question on a headless machine is "not measured",
not a number nobody took.

Which checks a project has is the caller's business -- H* sniffs the tree for
them, a playbook running ``mode: stint`` reads them off its own ``verify``
section. What is here is how one is run and how a set of results reads.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_TIMEOUT_SEC = 1800.0
_TAIL_CHARS = 4000
_SEED_ENV = "STINT_SEED"


@dataclass(frozen=True)
class CheckSummary:
    """One check, as a role prompt and an evidence bundle carry it."""

    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CheckSummary:
        return cls(
            name=str(data.get("name") or ""),
            status=str(data.get("status") or ""),
            detail=str(data.get("detail") or ""),
        )


@dataclass(frozen=True)
class Display:
    """Where a run of the game may draw, and whether it may at all."""

    name: str = ""
    provider: str = "none"
    started: bool = False

    @property
    def available(self) -> bool:
        return bool(self.name) or self.provider == "native"

    def env(self) -> dict[str, str]:
        return {"DISPLAY": self.name} if self.name else {}

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "provider": self.provider, "started": self.started, "available": self.available}


@dataclass(frozen=True)
class CheckSpec:
    """A command the runtime runs over the project tree."""

    name: str
    command: str
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    needs_display: bool = False
    seedable: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "command": self.command,
            "timeout_sec": self.timeout_sec,
            "needs_display": self.needs_display,
            "seedable": self.seedable,
        }


@dataclass(frozen=True)
class CheckResult:
    name: str
    command: str
    returncode: int | None
    duration_sec: float
    stdout_tail: str = ""
    stderr_tail: str = ""
    log_path: str = ""
    timed_out: bool = False
    skipped_reason: str = ""

    @property
    def status(self) -> str:
        if self.skipped_reason:
            return "skipped"
        if self.timed_out:
            return "timeout"
        return "ok" if self.returncode == 0 else "failed"

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def summary(self) -> CheckSummary:
        if self.skipped_reason:
            detail = self.skipped_reason
        elif self.timed_out:
            detail = f"no result after {self.duration_sec:.0f}s"
        else:
            detail = f"exit {self.returncode} in {self.duration_sec:.0f}s"
        return CheckSummary(name=self.name, status=self.status, detail=detail)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "command": self.command,
            "returncode": self.returncode,
            "duration_sec": round(self.duration_sec, 3),
            "status": self.status,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "log_path": self.log_path,
            "timed_out": self.timed_out,
            "skipped_reason": self.skipped_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "CheckResult":
        """Back from what :meth:`to_dict` wrote, for a plan reading its own record.

        ``status`` is dropped rather than accepted: it is derived, and a stored
        one that disagreed with the numbers beside it would be believed.
        """
        fields = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
        return cls(**fields)  # type: ignore[arg-type]


def parse_check_spec(text: str, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> CheckSpec:
    """``name=command`` from the command line; a trailing ``!display`` needs one."""
    name, separator, command = text.partition("=")
    if not separator or not name.strip() or not command.strip():
        raise ValueError(f"a check is name=command, not {text!r}")
    name = name.strip()
    needs_display = False
    if name.endswith("!display"):
        name, needs_display = name[: -len("!display")].rstrip(), True
    return CheckSpec(name=name, command=command.strip(), timeout_sec=timeout_sec, needs_display=needs_display)


def resolve_display(requested: str | None, *, start_xvfb: bool = False) -> Display:
    """The display a run may use: the one named, one this runtime starts, or none.

    macOS has no Xvfb and no headless GPU path, so a stint there records that
    it had no display rather than pretending a frame was rendered. Every gate
    whose evidence needs one is then ``blocked``, which is the truth.
    """
    if requested:
        return Display(name=requested, provider="given")
    inherited = os.environ.get("DISPLAY", "").strip()
    if inherited:
        return Display(name=inherited, provider="inherited")
    if platform.system() == "Darwin":
        # Asking for an Xvfb here is a habit from the Linux runners; this machine
        # draws natively and has no Xvfb, so the request is simply not needed.
        return Display(provider="native")
    if start_xvfb and shutil.which("Xvfb"):
        return Display(provider="xvfb")
    return Display(provider="none")


def start_display(
    display: Display, *, screen: str = "1920x1080x24", number: int = 99
) -> tuple[Display, subprocess.Popen | None]:
    """An Xvfb for ``display`` when it asked for one; otherwise it unchanged."""
    if display.provider != "xvfb" or display.name:
        return display, None
    name = f":{number}"
    process = subprocess.Popen(
        ["Xvfb", name, "-screen", "0", screen],  # noqa: S607 -- Xvfb is a system tool; PATH lookup intended
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(1.0)
    return Display(name=name, provider="xvfb", started=True), process


def _tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if len(text) <= _TAIL_CHARS else text[-_TAIL_CHARS:]


#: How long a signalled check is given before the next signal. Short: it has
#: already had its whole timeout.
_GROUP_GRACE_SEC = 5.0


def _group_of(process: "subprocess.Popen[bytes]") -> int | None:
    """The process group this check was given, while there still is one."""
    try:
        return os.getpgid(process.pid)
    except (AttributeError, OSError):  # not POSIX, or already gone
        return None


def _end_group(process: "subprocess.Popen[bytes]", group: int | None) -> None:
    """End everything the timed-out check started, not just its shell.

    A check is a shell line, so ``uv run pytest`` is the shell's child and the
    tests are its grandchildren. ``subprocess.run`` ends a timeout by killing
    the direct child, which is the shell: the suite goes on running, goes on
    spending the machine, and goes on writing into the log file whose tail is
    read just below as the result. ``start_new_session=True`` was already here
    and made the group addressable; nothing had ever addressed it.

    Term before kill, so a suite can flush what it was writing; kill after,
    because a run that ignored the first signal is over either way.

    **Only on the timeout path.** Here the leader has not been reaped, so its
    pid -- which is the group id -- cannot have been reused by anything else.
    Doing this after a clean exit would mean signalling a group id whose leader
    was already waited on, and a recycled one belongs to somebody else. A check
    that exits cleanly having left a daemon behind therefore leaks it, which is
    the lesser of the two.
    """
    if group is None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_GROUP_GRACE_SEC)


def run_check(
    spec: CheckSpec,
    *,
    cwd: Path,
    log_dir: Path,
    display: Display | None = None,
    env: Mapping[str, str] | None = None,
    seed: str | None = None,
) -> CheckResult:
    """``spec`` in the project tree, with its output kept beside the run."""
    if spec.needs_display and display is not None and not display.available:
        return CheckResult(
            name=spec.name,
            command=spec.command,
            returncode=None,
            duration_sec=0.0,
            skipped_reason="no display on this machine",
        )
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / f"{spec.name}.out"
    err_path = log_dir / f"{spec.name}.err"
    environment = dict(os.environ)
    environment.update(env or {})
    if display is not None:
        environment.update(display.env())
    if seed is not None:
        environment[_SEED_ENV] = seed
    started = time.monotonic()
    timed_out = False
    with out_path.open("w", encoding="utf-8") as out, err_path.open("w", encoding="utf-8") as err:
        process = subprocess.Popen(  # noqa: S602 - a check is a shell line by contract; see CheckSpec.command
            spec.command,
            shell=True,
            cwd=str(cwd),
            stdout=out,
            stderr=err,
            env=environment,
            start_new_session=True,
        )
        # Read while it is alive: after the process is reaped its group can no
        # longer be looked up, and the group is the thing that has to be ended.
        group = _group_of(process)
        try:
            returncode: int | None = process.wait(timeout=spec.timeout_sec)
        except subprocess.TimeoutExpired:
            returncode, timed_out = None, True
            _end_group(process, group)
    return CheckResult(
        name=spec.name,
        command=spec.command,
        returncode=returncode,
        duration_sec=time.monotonic() - started,
        stdout_tail=_tail(out_path),
        stderr_tail=_tail(err_path),
        log_path=str(out_path),
        timed_out=timed_out,
    )


def run_checks(
    specs: Sequence[CheckSpec],
    *,
    cwd: Path,
    log_dir: Path,
    display: Display | None = None,
    env: Mapping[str, str] | None = None,
    seed: str | None = None,
) -> list[CheckResult]:
    return [run_check(spec, cwd=cwd, log_dir=log_dir, display=display, env=env, seed=seed) for spec in specs]


def render_checks(results: Sequence[CheckResult]) -> str:
    if not results:
        return "(no runtime checks were configured for this tree)"
    lines = ["| check | status | detail |", "| --- | --- | --- |"]
    for result in results:
        summary = result.summary()
        lines.append(f"| {summary.name} | {summary.status} | {summary.detail} |")
    return "\n".join(lines)


def build_status(results: Sequence[CheckResult]) -> str:
    """One word for the whole check pass, for the run record."""
    if not results:
        return "none"
    if all(result.status == "skipped" for result in results):
        return "skipped"
    if any(result.status in {"failed", "timeout"} for result in results):
        return "failing"
    return "passing"


def save_results(results: Sequence[CheckResult], path: Path, *, display: Display | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "status": build_status(results),
        "checks": [result.to_dict() for result in results],
    }
    if display is not None:
        payload["display"] = display.to_dict()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


CHECKS_FILE = "checks.json"
"""Where a project records what each declared check actually runs here.

Beside the backlog, under the directory named for the mode rather than for the
recipe that usually writes it: a backlog is the `stint` recipe's content, and
this is the mode's -- any `mode: stint` playbook that declares a check by
description needs somewhere to keep the answer, whether or not it also asked for
the recipe's layout.

Keyed ``<playbook>-<check>``. Not by check name alone: two playbooks may both
want a gate called `build` and mean different things by it, and the one that
arrived second would silently inherit the first one's command.
"""


def checks_path(project: Path) -> Path:
    from raven.stint.backlog import STINT_DIR

    return Path(project).expanduser() / STINT_DIR / CHECKS_FILE


def _ledger(project: Path) -> dict[str, dict[str, object]]:
    path = checks_path(project)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_ledger(project: Path, rows: dict[str, dict[str, object]]) -> Path:
    path = checks_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def remember_check(project: Path, playbook: str, name: str, command: str, *, found: str = "person") -> Path:
    """Write down what this project runs for one of a playbook's declared checks."""
    rows = _ledger(project)
    rows[f"{playbook}-{name}"] = {
        "run": command,
        "from": found,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return _write_ledger(project, rows)


def resolve_checks(project: Path, playbook: str, entries: Sequence[Any]) -> tuple[list[CheckSpec], list[str]]:
    """What each declared check runs here, and which ones nobody has answered.

    Resolved once per project and written down, not worked out per run: a command
    that is re-derived every time can change between two rounds of one stint
    without anybody having decided that it should, and a resolution that happens
    on every start is a resolution that can never stop to ask.

    Order: the playbook's own ``run`` if it has one -- a file that names its
    command is not asking a question -- then this project's ledger, then what the
    tree plainly affords. Anything still unanswered comes back as a name, for a
    caller to refuse over: a declared check that silently does not run is worse
    than no check, because the round reports a gate nobody measured.
    """
    # Imported here: `bootstrap` reads this module for CheckSpec, so the pair
    # cannot see each other at import time.
    from raven.stint.bootstrap import detect_checks

    rows = _ledger(project)
    afforded = {spec.name: spec for spec in detect_checks(project)}
    specs: list[CheckSpec] = []
    missing: list[str] = []
    for entry in entries:
        if literal := str(getattr(entry, "run", "") or "").strip():
            specs.append(_as_spec(entry, literal))
            continue
        recorded = rows.get(f"{playbook}-{entry.name}")
        if recorded and str(recorded.get("run") or "").strip():
            specs.append(_as_spec(entry, str(recorded["run"])))
            continue
        if (found := afforded.get(entry.name)) is not None:
            remember_check(project, playbook, entry.name, found.command, found="detected")
            specs.append(_as_spec(entry, found.command))
            continue
        missing.append(entry.name)
    return specs, missing


def _as_spec(entry: Any, command: str) -> CheckSpec:
    return CheckSpec(
        name=entry.name,
        command=command,
        timeout_sec=getattr(entry, "timeout_sec", DEFAULT_TIMEOUT_SEC),
        needs_display=bool(getattr(entry, "needs_display", False)),
    )
