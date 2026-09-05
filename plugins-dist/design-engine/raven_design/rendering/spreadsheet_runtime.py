"""Compute spreadsheet layouts and viewport slices for rendering."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven_design.rendering.models import RenderConfig, RenderError
from raven_design.rendering.util import json_write

_DISPLAY_NUMBERS = range(90, 200)
_DISPLAY_WAIT_ATTEMPTS = 40
_DISPLAY_WAIT_SECONDS = 0.05
_LOG_TAIL_LENGTH = 2000
_PROCESS_STOP_SECONDS = 5
_UNO_CONNECT_TIMEOUT_SECONDS = 30
_UNO_IMPORT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class SpreadsheetWorkerResult:
    payload: dict[str, Any]
    worker_root: Path
    capture_viewports: bool
    started_at: float


def uno_python_path() -> str | None:
    candidates = dict.fromkeys(
        candidate
        for candidate in (
            sys.executable,
            shutil.which("python3"),
            "/usr/bin/python3",
        )
        if candidate
    )
    for candidate in candidates:
        if not Path(candidate).is_file():
            continue
        try:
            result = subprocess.run(
                [candidate, "-c", "import uno"],
                capture_output=True,
                text=True,
                timeout=_UNO_IMPORT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return candidate
    return None


def spreadsheet_viewport_available() -> bool:
    return bool(shutil.which("Xvfb") and shutil.which("import"))


def run_spreadsheet_worker(
    source: Path,
    bundle_root: Path,
    python_path: str,
    config: RenderConfig,
) -> SpreadsheetWorkerResult:
    if not config.libreoffice_path:
        raise RenderError(
            "renderer_unavailable",
            "LibreOffice is unavailable.",
        )
    worker_root = bundle_root / ".worker" / "spreadsheet"
    input_dir = worker_root / "input"
    profile_dir = worker_root / "profile"
    input_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    staged_source = input_dir / source.name
    shutil.copy2(source, staged_source)
    sheets_dir = bundle_root / "sheets"
    sheets_dir.mkdir()
    port = _free_loopback_port()
    xvfb_path = shutil.which("Xvfb")
    capture_path = shutil.which("import")
    capture_viewports = bool(xvfb_path and capture_path)
    display = _free_x_display() if capture_viewports else None
    environment = os.environ.copy()
    xvfb_process: subprocess.Popen[Any] | None = None
    if display is not None:
        xvfb_process = _start_xvfb(
            worker_root,
            display,
            config,
            xvfb_path,
        )
        if _wait_for_display(display, xvfb_process):
            environment["DISPLAY"] = display
        else:
            _stop_process(xvfb_process)
            xvfb_process = None
            display = None
            capture_viewports = False
    request_path = worker_root / "request.json"
    result_path = worker_root / "result.json"
    json_write(
        request_path,
        {
            "source": str(staged_source),
            "output_dir": str(sheets_dir),
            "port": port,
            "connect_timeout_seconds": min(
                _UNO_CONNECT_TIMEOUT_SECONDS,
                config.timeout_seconds,
            ),
            "capture_viewports": capture_viewports,
            "capture_path": capture_path,
            "display": display,
            "viewport_width": config.spreadsheet_viewport_width,
            "viewport_height": config.spreadsheet_viewport_height,
            "max_viewports": config.spreadsheet_max_viewports,
        },
    )
    command = [config.libreoffice_path]
    if not capture_viewports:
        command.append("--headless")
    command.extend(
        [
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            (f"--accept=socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"),
        ]
    )
    log_path = worker_root / "libreoffice.log"
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        office_process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
    worker_path = Path(__file__).with_name("uno_spreadsheet_worker.py")
    worker_result: subprocess.CompletedProcess[str] | None = None
    try:
        worker_result = subprocess.run(
            [python_path, str(worker_path), str(request_path), str(result_path)],
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(
            "render_timeout",
            "Per-sheet spreadsheet rendering exceeded its time limit.",
            retryable=True,
        ) from exc
    finally:
        _stop_process(office_process)
        _stop_process(xvfb_process)
    if worker_result is None or worker_result.returncode != 0 or not result_path.is_file():
        log_tail = (
            log_path.read_text(encoding="utf-8", errors="replace")[-_LOG_TAIL_LENGTH:] if log_path.is_file() else ""
        )
        raise RenderError(
            "conversion_failed",
            "The per-sheet spreadsheet renderer failed.",
            details={
                "exit_code": (worker_result.returncode if worker_result is not None else None),
                "stdout": (worker_result.stdout[-_LOG_TAIL_LENGTH:] if worker_result is not None else ""),
                "stderr": (worker_result.stderr[-_LOG_TAIL_LENGTH:] if worker_result is not None else ""),
                "libreoffice_log": log_tail,
            },
        )
    return SpreadsheetWorkerResult(
        payload=json.loads(result_path.read_text(encoding="utf-8")),
        worker_root=worker_root,
        capture_viewports=capture_viewports,
        started_at=started,
    )


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _free_x_display() -> str:
    socket_root = Path("/tmp/.X11-unix")
    for number in _DISPLAY_NUMBERS:
        if not Path(f"/tmp/.X{number}-lock").exists() and not (socket_root / f"X{number}").exists():
            return f":{number}"
    raise RenderError(
        "renderer_unavailable",
        "No free local X display is available for spreadsheet view capture.",
    )


def _start_xvfb(
    worker_root: Path,
    display: str,
    config: RenderConfig,
    executable: str,
) -> subprocess.Popen[Any]:
    log_path = worker_root / "xvfb.log"
    log = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            [
                executable,
                display,
                "-screen",
                "0",
                (
                    f"{config.spreadsheet_viewport_width}x"
                    f"{config.spreadsheet_viewport_height}x"
                    f"{config.spreadsheet_viewport_depth}"
                ),
                "-nolisten",
                "tcp",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        log.close()


def _wait_for_display(
    display: str,
    process: subprocess.Popen[Any],
) -> bool:
    socket_path = Path("/tmp/.X11-unix") / f"X{display[1:]}"
    for _ in range(_DISPLAY_WAIT_ATTEMPTS):
        if socket_path.exists() and process.poll() is None:
            return True
        time.sleep(_DISPLAY_WAIT_SECONDS)
    return False


def _stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_PROCESS_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_PROCESS_STOP_SECONDS)
