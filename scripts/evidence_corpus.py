#!/usr/bin/env python3
"""The signed-records corpus: real agent runs, attested, verifiable offline.

The Phase-0 artefact for the standards proposal is *"a signed-records corpus of
at least 100 real agent runs"*. This is the machinery for it, and a first
corpus built with it from the run records on this machine.

What a corpus entry is. One real agent run -- the ACP frame log of one
session -- summarised as an in-toto statement, wrapped in a DSSE envelope and
signed with an Ed25519 key. The statement's subject is the run's own digest,
so a verifier can recompute it from the run record and check that the
signature is over *this* run and not over a description of it.

What it is not. It is not an attestation that the runs were good. It says
what ran, which agent ran it, which tools it called, and how it ended. A
corpus that claimed more would be worth less than no corpus, and the house
rule is the same one the evidence bundle spec states: a part that establishes
nothing says so.

Why a local key rather than a transparency log. Sigstore keyless signing is
not available here: it needs an OIDC identity and a Fulcio/Rekor round trip,
and the plan already records that as a blocker. A local Ed25519 key gives
**verifiability without an account** -- anyone with the public key can check
every signature offline -- and it is not anchored in a transparency log, which
is a stated gap rather than a hidden one. A corpus entry records that gap
in its own predicate, so a reader is never left to guess.

Usage::

    python3 scripts/evidence_corpus.py build --runs <dir> --out <dir> [--key <file>]
    python3 scripts/evidence_corpus.py verify --corpus <dir> [--runs <dir>]

``build`` reads every ``*.jsonl`` run record under ``--runs``, writes one
signed envelope per run plus a manifest and the public key. ``verify`` checks
every signature, recomputes every subject digest from the run record when
the record is present, and reports each check as **passed**, **failed** or
**not checked** -- never as a silent pass.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

#: The DSSE payload type an in-toto statement travels in.
PAYLOAD_TYPE = "application/vnd.in-toto+json"

#: The in-toto statement type. This one is ours, and it is deliberately a
#: reversed-domain identifier: it says who wrote the predicate, and it makes
#: no claim to be anybody else's standard.
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"

#: The predicate type: what this statement is about. A vendor's own type,
#: which is what an extension would be before anyone sponsors it.
PREDICATE_TYPE = "https://evermind.ai/attestation/agent-run/v1"

#: The schema version of the predicate, so a later reader knows which shape
#: it is looking at.
PREDICATE_VERSION = 1


def canonical(obj: Any) -> bytes:
    """One byte-string per object, so a digest is a function of the value."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pae(payload_type: str, payload: bytes) -> bytes:
    """The pre-authentication encoding DSSE signs.

    Length-prefixed, so a payload cannot be shifted across the boundary
    between the type and the body.
    """
    type_bytes = payload_type.encode("utf-8")
    return b"DSSEv1 " + str(len(type_bytes)).encode() + b" " + type_bytes + b" " + str(len(payload)).encode() + b" " + payload


# --------------------------------------------------------------------------- #
# Reading a run record
# --------------------------------------------------------------------------- #


def read_frames(path: Path) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            frames.append(row)
    return frames


def run_record(path: Path) -> dict[str, Any]:
    """One agent run, read out of its frame log.

    The record is a reading of the frames, not the frames themselves: what
    ran, who ran it, which tools it called and how it ended. The frames
    themselves are what the digest covers.
    """
    frames = read_frames(path)
    agent: dict[str, Any] = {}
    resolution = None
    protocol_version: int | None = None
    session_id: str | None = None
    stop_reason: str | None = None
    tool_calls: list[str] = []
    timestamps: list[str] = []

    # A response frame carries an ``id`` and a ``result`` and no ``method``,
    # so the method it answers has to come from the request with the same
    # id. Matching on the result's shape instead would guess, and a guess
    # that reads the wrong field produces a record that is confidently
    # wrong.
    methods_by_id: dict[Any, str] = {}
    for row in frames:
        frame = row.get("frame")
        if isinstance(frame, dict) and isinstance(frame.get("method"), str) and frame.get("id") is not None:
            methods_by_id[frame["id"]] = frame["method"]

    for row in frames:
        stamp = row.get("timestamp")
        if isinstance(stamp, str):
            timestamps.append(stamp)
        frame = row.get("frame")
        if not isinstance(frame, dict):
            continue
        method = frame.get("method")
        result = frame.get("result") if isinstance(frame.get("result"), dict) else None
        if result is not None and frame.get("id") is not None:
            method = methods_by_id.get(frame["id"], method)
            if method is None:
                # The request frame is not in this log -- the log may start
                # mid-session, or the request may have been written
                # elsewhere. The result's own shape still says which call it
                # answers, and using it is better than dropping the field:
                # a missing frame read as an absent value is the bug this
                # whole corpus exists to avoid. How it was resolved is
                # recorded, so a reader can see which it was.
                if isinstance(result.get("sessionId"), str):
                    method = "session/new"
                    resolution = "result-shape"
                elif isinstance(result.get("stopReason"), str):
                    method = "session/prompt"
                    resolution = "result-shape"
            elif resolution is None:
                resolution = "request-id"
        if method == "initialize" and result:
            info = result.get("agentInfo")
            if isinstance(info, dict):
                agent = {
                    "name": str(info.get("name") or ""),
                    "title": str(info.get("title") or ""),
                    "version": str(info.get("version") or ""),
                }
            if isinstance(result.get("protocolVersion"), int):
                protocol_version = result["protocolVersion"]
        if method == "session/new" and result and isinstance(result.get("sessionId"), str):
            session_id = result["sessionId"]
        if method == "session/prompt" and result and isinstance(result.get("stopReason"), str):
            stop_reason = result["stopReason"]
        if method == "session/update":
            params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
            update = params.get("update") if isinstance(params.get("update"), dict) else {}
            if update.get("sessionUpdate") == "tool_call":
                title = str(update.get("title") or update.get("kind") or "").strip()
                if title:
                    tool_calls.append(title)

    # A run that called no tool is a different kind of thing from a run that
    # did, and a corpus that does not say which is a corpus that lets a
    # reader assume more than it should. A handshake establishes that the
    # transport works; a tool-using run establishes that the agent worked.
    kind = "tool-using" if tool_calls else "transport-only"

    return {
        "kind": kind,
        "method_resolution": resolution,
        "source": path.name,
        "session_id": session_id or path.stem,
        "agent": agent,
        "protocol_version": protocol_version,
        "started_at": min(timestamps) if timestamps else None,
        "ended_at": max(timestamps) if timestamps else None,
        "frames": len(frames),
        "tool_calls": sorted(set(tool_calls)),
        "tool_call_count": len(tool_calls),
        "stop_reason": stop_reason,
        "frames_digest": {"sha256": sha256_hex(canonical(frames))},
    }


# --------------------------------------------------------------------------- #
# The statement, the envelope, the signature
# --------------------------------------------------------------------------- #


def statement_for(record: dict[str, Any]) -> dict[str, Any]:
    """The in-toto statement for one run.

    The subject is the run's digest. A verifier recomputes it from the run
    record and finds the same value, which is what ties the signature to
    this run rather than to a description of it.
    """
    tool_chain = {
        "tools": record["tool_calls"],
        "digest": {"sha256": sha256_hex(canonical(record["tool_calls"]))},
    }
    return {
        "_type": STATEMENT_TYPE,
        "subject": [
            {
                "name": f"agent-run:{record['session_id']}",
                "digest": record["frames_digest"],
            }
        ],
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "version": PREDICATE_VERSION,
            "run": {
                "kind": record["kind"],
                "method_resolution": record["method_resolution"],
                "source": record["source"],
                "session_id": record["session_id"],
                "agent": record["agent"],
                "protocol_version": record["protocol_version"],
                "started_at": record["started_at"],
                "ended_at": record["ended_at"],
                "frames": record["frames"],
                "tool_call_count": record["tool_call_count"],
                "stop_reason": record["stop_reason"],
            },
            "tool_chain": tool_chain,
            "what_this_establishes": (
                "That this run happened, that these frames are the ones that produced it, "
                "and that the tool chain above is the one the run called. It establishes "
                "nothing about the quality of the run, the correctness of its output, or "
                "any regulatory position."
                + (
                    " This run called no tool, so what it establishes is that the transport "
                    "worked, not that the agent did any work."
                    if record["kind"] == "transport-only"
                    else ""
                )
            ),
            "known_gaps": [
                "The signature is a local Ed25519 key, verifiable offline but not anchored "
                "in a transparency log. Keyless signing is unavailable to this build.",
            ],
        },
    }


def sign(statement: dict[str, Any], private_key: Ed25519PrivateKey, key_id: str) -> dict[str, Any]:
    """The statement in a DSSE envelope, signed over its pre-authentication encoding."""
    payload = canonical(statement)
    signature = private_key.sign(pae(PAYLOAD_TYPE, payload))
    return {
        "payloadType": PAYLOAD_TYPE,
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [
            {
                "keyid": key_id,
                "sig": base64.b64encode(signature).decode("ascii"),
            }
        ],
    }


def envelope_statement(envelope: dict[str, Any]) -> dict[str, Any]:
    return json.loads(base64.b64decode(envelope["payload"]))


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def load_or_make_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        raw = path.read_bytes()
        return Ed25519PrivateKey.from_private_bytes(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes_raw())
    path.chmod(0o600)
    return key


def build(runs_dir: Path, out_dir: Path, key_path: Path) -> int:
    records = [run_record(path) for path in sorted(runs_dir.rglob("*.jsonl"))]
    if not records:
        print(f"no run records under {runs_dir}", file=sys.stderr)
        return 2

    private_key = load_or_make_key(key_path)
    public_key = private_key.public_key()
    key_id = sha256_hex(public_key.public_bytes(Encoding.Raw, PublicFormat.Raw))[:16]
    out_dir.mkdir(parents=True, exist_ok=True)
    entries_dir = out_dir / "entries"
    entries_dir.mkdir(exist_ok=True)

    entries = []
    kinds: dict[str, int] = {}
    for record in records:
        statement = statement_for(record)
        envelope = sign(statement, private_key, key_id)
        name = f"{record['session_id']}.envelope.json"
        (entries_dir / name).write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        kinds[record["kind"]] = kinds.get(record["kind"], 0) + 1
        entries.append(
            {
                "entry": f"entries/{name}",
                "session_id": record["session_id"],
                "source": record["source"],
                "subject": statement["subject"][0],
                "predicate_type": PREDICATE_TYPE,
            }
        )

    tool_using = kinds.get("tool-using", 0)
    transport_only = kinds.get("transport-only", 0)
    mix = f"{tool_using} tool-using, {transport_only} transport-only"

    manifest = {
        "schema": "raven_evidence_corpus",
        "version": 1,
        "predicate_type": PREDICATE_TYPE,
        "statement_type": STATEMENT_TYPE,
        "payload_type": PAYLOAD_TYPE,
        "key_id": key_id,
        "public_key": base64.b64encode(public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii"),
        "key_algorithm": "ed25519",
        "built_at": None,
        "run_kinds": kinds,
        "entries": entries,
        "known_gaps": [
            "The signing key is local and the signatures are not anchored in a transparency log.",
            f"The corpus holds the runs that exist on the machine that built it ({mix}). It is not a "
            "sample of anything, and it does not claim to be representative.",
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"built {len(entries)} entries into {out_dir}")
    return 0


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #


def verify(corpus_dir: Path, runs_dir: Path | None) -> int:
    manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(manifest["public_key"]))

    passed = failed = not_checked = 0
    problems: list[str] = []

    for entry in manifest["entries"]:
        envelope = json.loads((corpus_dir / entry["entry"]).read_text(encoding="utf-8"))
        payload = base64.b64decode(envelope["payload"])
        signature = base64.b64decode(envelope["signatures"][0]["sig"])
        try:
            public_key.verify(signature, pae(envelope["payloadType"], payload))
            passed += 1
        except Exception as exc:  # noqa: BLE001 - a bad signature is a finding, not a crash
            failed += 1
            problems.append(f"{entry['entry']}: signature does not verify ({exc})")
            continue

        statement = json.loads(payload)
        if statement.get("predicateType") != manifest["predicate_type"]:
            failed += 1
            problems.append(f"{entry['entry']}: wrong predicate type")
            continue

        # The subject digest, recomputed from the run record when it is here.
        source = entry.get("source")
        if runs_dir is None or not source:
            not_checked += 1
            continue
        record_path = next(iter(sorted(runs_dir.rglob(source))), None)
        if record_path is None:
            not_checked += 1
            continue
        recomputed = run_record(record_path)["frames_digest"]["sha256"]
        claimed = statement["subject"][0]["digest"]["sha256"]
        if recomputed != claimed:
            failed += 1
            problems.append(f"{entry['entry']}: the run record now digests to {recomputed}, not {claimed}")
        else:
            passed += 1

    print(f"corpus: {corpus_dir}")
    print(f"  entries: {len(manifest['entries'])}")
    print(f"  checks passed: {passed}")
    print(f"  checks failed: {failed}")
    print(f"  checks not run: {not_checked}")
    for problem in problems:
        print(f"  ! {problem}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    builder = sub.add_parser("build", help="build a corpus from run records")
    builder.add_argument("--runs", required=True, type=Path)
    builder.add_argument("--out", required=True, type=Path)
    builder.add_argument("--key", required=True, type=Path, help="the Ed25519 private key; generated if absent")

    checker = sub.add_parser("verify", help="verify a corpus offline")
    checker.add_argument("--corpus", required=True, type=Path)
    checker.add_argument("--runs", type=Path, help="the run records, to recompute the subject digests")

    args = parser.parse_args()
    if args.command == "build":
        return build(args.runs, args.out, args.key)
    return verify(args.corpus, args.runs)


if __name__ == "__main__":
    raise SystemExit(main())
