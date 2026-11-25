"""Which ACP adapter pins npm has moved past.

Raven fetches three ACP shims with npx and pins them to exact versions,
because the pin decides what the agent offers: ``codex-acp`` builds its
model menu from the codex build it bundles, and ``claude-agent-acp``
carries the Claude Code CLI its SDK pin names. Nothing watched npm, so a
pin only moved when a person noticed. This is the check a scheduled
workflow runs: read the pins Raven ships, ask npm for each package's
``latest`` tag, and report the gap.

The check does not bump anything. A bump needs the handshake, model-menu
and cross-version ``session/load`` measurements
``raven.agent.subagent.presets`` describes, which need a signed-in agent
and have no place in CI.

Exit codes: ``0`` every pin is current, ``1`` at least one is behind,
``2`` npm could not be read -- so a network failure never reads as "up to
date".
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Sequence

from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS

_REGISTRY = "https://registry.npmjs.org/{package}/latest"
_TIMEOUT_SECONDS = 20.0

#: The shims Raven fetches, by preset key. The preset table carries the
#: command; npm's package name is the fact beside it that the table does
#: not spell out.
PACKAGES: dict[str, str] = {
    "codex": "@agentclientprotocol/codex-acp",
    "claude_code": "@agentclientprotocol/claude-agent-acp",
    "pi": "pi-acp",
}

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass(frozen=True)
class Pin:
    """One adapter Raven pins, and the version it pins."""

    preset: str
    package: str
    version: str


@dataclass(frozen=True)
class Reading:
    """A pin beside what npm's ``latest`` tag says today."""

    pin: Pin
    latest: str

    @property
    def behind(self) -> bool:
        return version_key(self.latest) > version_key(self.pin.version)


def shipped_pins() -> tuple[Pin, ...]:
    """The three pins Raven ships, read from the preset tables."""
    pins = []
    for preset, package in PACKAGES.items():
        entry = THIRD_PARTY_SUBAGENT_PRESETS.get(preset)
        command = entry.get("command") if isinstance(entry, dict) else None
        if not isinstance(command, str):
            raise LookupError(f"no {preset!r} preset carries a command")
        version = pinned_version(package, command)
        if version is None:
            raise LookupError(f"the {preset!r} preset does not name {package!r} with a version: {command!r}")
        pins.append(Pin(preset, package, version))
    return tuple(pins)


def pinned_version(package: str, command: str) -> str | None:
    """The version ``command`` names for ``package``, or None when it names none."""
    prefix = f"{package}@"
    for token in shlex.split(command):
        if token.startswith(prefix):
            return token[len(prefix) :] or None
    return None


def latest_version(package: str, *, timeout: float = _TIMEOUT_SECONDS) -> str:
    """The version npm's ``latest`` tag names for ``package``."""
    url = _REGISTRY.format(package=urllib.parse.quote(package, safe=""))
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.load(response)
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version:
        raise ValueError(f"npm's answer for {package!r} names no version")
    return version


def version_key(version: str) -> tuple[int, int, int]:
    """A comparable key for an npm version, ignoring a pre-release suffix."""
    match = _VERSION_RE.match(version)
    if match is None:
        raise ValueError(f"not an npm version: {version!r}")
    major, minor, patch = (int(part or 0) for part in match.groups())
    return major, minor, patch


def markdown(readings: Sequence[Reading]) -> str:
    """The report as an issue body."""
    behind = [reading for reading in readings if reading.behind]
    lines = [
        (
            "The scheduled check found ACP adapter releases newer than the pins Raven ships."
            if behind
            else "Every ACP adapter pin Raven ships is the newest npm release."
        ),
        "",
        "| preset | package | pinned | latest |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(f"| {r.pin.preset} | `{r.pin.package}` | {r.pin.version} | {r.latest} |" for r in readings)
    if behind:
        lines.extend(
            [
                "",
                "A bump also needs the handshake, model-menu and cross-version `session/load`",
                "measurements described in `raven/agent/subagent/presets.py`.",
            ]
        )
    return "\n".join(lines)


def plain(readings: Sequence[Reading]) -> str:
    """The report as aligned lines, for a terminal."""
    width = max(len(reading.pin.package) for reading in readings)
    return "\n".join(
        f"{r.pin.preset:<12} {r.pin.package:<{width}}  {r.pin.version} -> {r.latest}  "
        f"{'BEHIND' if r.behind else 'current'}"
        for r in readings
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--markdown", action="store_true", help="print the report as a Markdown table for an issue body"
    )
    args = parser.parse_args(argv)

    try:
        pins = shipped_pins()
    except (LookupError, ValueError) as exc:
        print(f"cannot read the pins Raven ships: {exc}", file=sys.stderr)
        return 2

    readings = []
    for pin in pins:
        try:
            readings.append(Reading(pin, latest_version(pin.package)))
        except Exception as exc:  # noqa: BLE001 - any read failure is one report
            print(f"cannot read npm for {pin.package}: {exc}", file=sys.stderr)
            return 2

    print(markdown(readings) if args.markdown else plain(readings))
    return 1 if any(reading.behind for reading in readings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
