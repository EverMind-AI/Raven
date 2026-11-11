"""Load an acceptance suite: the held-out cases an agent folder is judged on.

Layout::

    <suite>/suite.json        {"name", "version", "target": <agent name> | null, "timeout_s"}
    <suite>/cases/<id>.json   {"id", "task", "files", "criteria": [{"id", "statement"}],
                               "reference", "timeout_s"}

A file value is text, or ``{"base64": ...}`` for bytes. Anything malformed is a
:class:`ValidatorInputError`, raised before a single file is written anywhere.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from evolver.validator.report import ValidatorInputError

DEFAULT_TIMEOUT_S = 900


@dataclass(frozen=True)
class Criterion:
    id: str
    statement: str


@dataclass(frozen=True)
class Case:
    id: str
    task: str
    files: dict[str, bytes]
    criteria: tuple[Criterion, ...]
    reference: str
    timeout_s: int


@dataclass(frozen=True)
class Suite:
    name: str
    version: str
    target: str | None
    cases: tuple[Case, ...]
    digest: str


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidatorInputError(f"suite file missing: {path}") from exc
    except (OSError, ValueError) as exc:
        raise ValidatorInputError(f"suite file unreadable: {path}: {exc}") from exc


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidatorInputError(f"{what} must be a non-empty string")
    return value


def _timeout(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidatorInputError(f"{what} must be a positive integer")
    return value


def _relative(path: str, what: str) -> str:
    pure = PurePosixPath(path)
    if not path or "\\" in path or pure.is_absolute() or any(part in ("", ".", "..") for part in path.split("/")):
        raise ValidatorInputError(f"{what}: file path must be relative without '..': {path!r}")
    return path


def _files(raw: Any, what: str) -> dict[str, bytes]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidatorInputError(f"{what}: files must be an object")
    files: dict[str, bytes] = {}
    for path, value in raw.items():
        rel = _relative(path, what)
        if isinstance(value, str):
            files[rel] = value.encode("utf-8")
        elif isinstance(value, dict) and isinstance(value.get("base64"), str):
            try:
                files[rel] = base64.b64decode(value["base64"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValidatorInputError(f"{what}: {path!r} is not valid base64") from exc
        else:
            raise ValidatorInputError(f"{what}: {path!r} must be text or {{'base64': ...}}")
    return files


def _criteria(raw: Any, what: str) -> tuple[Criterion, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValidatorInputError(f"{what}: criteria must be a non-empty list")
    seen: set[str] = set()
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValidatorInputError(f"{what}: each criterion must be an object")
        cid = _text(item.get("id"), f"{what}: criterion id")
        if cid in seen:
            raise ValidatorInputError(f"{what}: duplicate criterion id {cid!r}")
        seen.add(cid)
        out.append(Criterion(cid, _text(item.get("statement"), f"{what}: criterion {cid} statement")))
    return tuple(out)


def load_suite(path: Path, *, agent_name: str) -> Suite:
    root = Path(path)
    head_path = root / "suite.json"
    head = _read_json(head_path)
    if not isinstance(head, dict):
        raise ValidatorInputError(f"{head_path} must hold an object")
    target = head.get("target")
    if target is not None and target != agent_name:
        raise ValidatorInputError(f"suite targets {target!r}, not {agent_name!r}")
    default_timeout = _timeout(head.get("timeout_s", DEFAULT_TIMEOUT_S), "suite timeout_s")

    digest = hashlib.sha256(head_path.read_bytes())
    case_paths = sorted((root / "cases").glob("*.json"))
    if not case_paths:
        raise ValidatorInputError(f"suite has no cases under {root / 'cases'}")
    cases: list[Case] = []
    seen: set[str] = set()
    for case_path in case_paths:
        raw = _read_json(case_path)
        if not isinstance(raw, dict):
            raise ValidatorInputError(f"{case_path} must hold an object")
        what = f"case {case_path.name}"
        cid = _text(raw.get("id"), f"{what}: id")
        if cid in seen:
            raise ValidatorInputError(f"duplicate case id {cid!r}")
        seen.add(cid)
        cases.append(
            Case(
                id=cid,
                task=_text(raw.get("task"), f"{what}: task"),
                files=_files(raw.get("files"), what),
                criteria=_criteria(raw.get("criteria"), what),
                reference=str(raw.get("reference") or ""),
                timeout_s=_timeout(raw.get("timeout_s", default_timeout), f"{what}: timeout_s"),
            )
        )
        digest.update(case_path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(case_path.read_bytes())
    return Suite(
        name=_text(head.get("name"), "suite name"),
        version=str(head.get("version") or ""),
        target=target,
        cases=tuple(cases),
        digest=digest.hexdigest(),
    )
