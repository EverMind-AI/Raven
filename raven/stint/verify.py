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
from pathlib import Path
from typing import Mapping, Sequence

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
    """Where a check that renders may draw, and whether it may at all."""

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
    """The end of a log, read from the end: a chatty check writes gigabytes."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_CHARS * 4))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return text if len(text) <= _TAIL_CHARS else text[-_TAIL_CHARS:]


#: What a check's shell gets to see. The host's own executor hands a command the
#: same short list rather than the gateway's whole environment, and a check is a
#: command a playbook file wrote: its output tail lands in a role's prompt and
#: in the stint's record, so a check that echoed its environment would carry
#: every host secret into a model's context and into committed state.
def curated_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    from raven.sandbox.direct_executor import baseline_env

    env = baseline_env()
    env.update(extra or {})
    return env


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
        # No group to address (not POSIX, or the leader already gone): the
        # shell itself is still ended, rather than left to run out its timeout
        # under nobody's watch.
        with suppress(ProcessLookupError, OSError):
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_GROUP_GRACE_SEC)
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
    # Replaced, not overlaid: `curated_env` hands back a *subset* of the host's
    # environment, and updating a full copy of `os.environ` with a subset removes
    # nothing -- the check kept every secret the curation was there to drop.
    environment = dict(env) if env is not None else dict(os.environ)
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
