# Audit-artifact deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Fully executed. This document is the historical planning record,
not an open work order: do not re-execute the checkboxes below. The shipped code is
ahead of the plan text in places a later review corrected; see the status
notes on the `CompactResult` interface in Task 2 and Task 3.

**Goal:** Store each distinct audit-artifact payload once on disk, and give the
operator a command that folds the files already written into the same shape,
without moving the path contract any consumer reads. Sized against a store of
603,629 files in 8.1 GB measured 2026-08-22; that store was pruned during
implementation and now holds 280,033 files in 5.6 GB. See the spec's section 4
for the restated figures.

**Architecture:** `TraceStore.persist_artifact` publishes the payload as a blob
under `audit-artifacts/_blobs/<sha1[:2]>/<sha1>.<ext>` and hard-links the
per-kind/day path at it, so a repeated payload costs a `stat` and a `link`
instead of a write. A separate `raven/tracing/compact.py` applies the same
folding to already-written files in place and unlinks blobs nothing references
any more; `raven tracing compact` is its only caller.

**Tech Stack:** Python 3.12+, stdlib only in `raven/tracing/` (that package is
deliberately dependency-free), typer + rich for the CLI, pytest, uv for every
command.

**Spec:** `docs/specs/2026-08-22-audit-artifacts-dedup-design.md`

## Global Constraints

- Every command runs through `uv`: `uv run pytest ...`, never bare `pytest`
  (AGENTS.md 4, 5.4).
- **Do not commit unprompted** (AGENTS.md 3.4). Each task's Commit step gives
  the message to use *when the maintainer authorises it*; a plan is not
  authorisation.
- The branch base is confirmed with the maintainer before any branch is cut
  (AGENTS.md 2.2). The spec names `main` as the base.
- Commit messages: Conventional Commits, all-English, ASCII-only, header <= 100
  chars, `Co-authored-by: Claude (<actual-session-model-id>) <noreply@anthropic.com>`
  (AGENTS.md 3.1, 3.1.1, 3.3).
- `raven/tracing/` stays stdlib-only. No new dependency, no `uv add`.
- Every new file carries an English module docstring. Inline comments only where
  the logic is non-obvious or a constraint is hidden; if neighbouring lines carry
  no comments, add none (AGENTS.md 1).
- The path contract does not move: `artifact_path` keeps pointing at
  `<kind>/<YYYY-MM-DD>/<HHMMSS_us>-<traceId>-<sessionId>-<label>-<sha1[:10]>.<ext>`,
  and that file stays a complete, readable, plaintext payload. The seven
  existing `artifact_path` assertions in `tests/test_tracing_api.py` and
  `tests/test_subagent_acp.py` must stay green **unmodified** - that is the
  regression evidence.
- Nothing on this path may break the host: `persist_artifact` already returns an
  error dict rather than raising, and must continue to.
- Blobs are named by the full 40-character sha1, never the 10-character
  filename suffix.
- The freshness window is 60 seconds, shared by the compaction walk and the
  orphan sweep.

---

### Task 1: Write path stores one copy per distinct payload

**Files:**
- Modify: `raven/tracing/store.py:1-13` (module docstring layout block)
- Modify: `raven/tracing/store.py:126-165` (`persist_artifact`)
- Test: `tests/test_tracing_api.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `TraceStore.blobs_dir` (a `Path`, `<state_dir>/logs/audit-artifacts/_blobs`)
  and the on-disk blob naming `_blobs/<sha1[:2]>/<sha1>.<ext>`. Task 2 reads
  both. `persist_artifact`'s returned dict is unchanged:
  `{"kind": str, "path": str | None, "sha1": str | None, "bytes": int | None, "preview": str, "error"?: str}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tracing_api.py`. The file already has the `trace_dir`
fixture (line 19) that points `RAVEN_TRACING_DIR` at `tmp_path` and resets
`_spans._store`; reuse it. Add `from pathlib import Path` and
`from raven.tracing import store as _store_mod` to the existing imports.

```python
def test_identical_payloads_share_one_inode(trace_dir):
    store = _spans._get_store()
    payload = {"messages": ["a" * 5000]}

    first = store.persist_artifact("llm.input", {"traceId": "t1"}, payload)
    second = store.persist_artifact("llm.input", {"traceId": "t2"}, payload)

    p1, p2 = Path(first["path"]), Path(second["path"])
    assert p1 != p2
    assert p1.stat().st_ino == p2.stat().st_ino
    assert json.loads(p1.read_text(encoding="utf-8")) == payload
    assert json.loads(p2.read_text(encoding="utf-8")) == payload
    assert first["sha1"] == second["sha1"]
    assert first["bytes"] == second["bytes"]


def test_one_blob_backs_every_reference(trace_dir):
    store = _spans._get_store()
    payload = {"messages": ["b" * 100]}

    store.persist_artifact("llm.input", {"traceId": "t1"}, payload)
    art = store.persist_artifact("tool.input", {"traceId": "t2"}, payload)

    blobs = sorted(store.blobs_dir.rglob("*.json"))
    assert len(blobs) == 1
    assert blobs[0].name == f"{art['sha1']}.json"
    assert blobs[0].parent.name == art["sha1"][:2]
    # blob + the two span paths
    assert Path(art["path"]).stat().st_nlink == 3


def test_distinct_payloads_do_not_share_a_blob(trace_dir):
    store = _spans._get_store()

    a = store.persist_artifact("llm.input", {"traceId": "t1"}, {"m": "one"})
    b = store.persist_artifact("llm.input", {"traceId": "t2"}, {"m": "two"})

    assert a["sha1"] != b["sha1"]
    assert Path(a["path"]).stat().st_ino != Path(b["path"]).stat().st_ino
    assert len(sorted(store.blobs_dir.rglob("*.json"))) == 2


def test_write_path_falls_back_when_hard_links_are_unavailable(trace_dir, monkeypatch):
    store = _spans._get_store()

    def _no_links(*_args, **_kwargs):
        raise OSError("hard links unsupported")

    monkeypatch.setattr(_store_mod.os, "link", _no_links)
    payload = {"m": "fallback"}

    art = store.persist_artifact("tool.input", {"traceId": "t1"}, payload)

    path = Path(art["path"])
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert path.stat().st_nlink == 1
    assert art.get("error") is None
    assert not list(store.blobs_dir.rglob("*.tmp"))


def test_string_payloads_keep_the_txt_extension(trace_dir):
    store = _spans._get_store()

    art = store.persist_artifact("subagent.external.transcript", {"traceId": "t1"}, "raw text")

    path = Path(art["path"])
    assert path.suffix == ".txt"
    assert path.read_text(encoding="utf-8") == "raw text"
    assert (store.blobs_dir / art["sha1"][:2] / f"{art['sha1']}.txt").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tracing_api.py -k "inode or blob or fallback or txt" -v`
Expected: FAIL - `AttributeError: 'TraceStore' object has no attribute 'blobs_dir'`,
and `test_identical_payloads_share_one_inode` fails on differing `st_ino`.

- [ ] **Step 3: Add the blob directory to `TraceStore.__init__`**

In `raven/tracing/store.py`, next to the module-level constants add:

```python
BLOBS_DIR_NAME = "_blobs"
```

and in `__init__` (after line 74, `self.artifacts_dir = ...`):

```python
        self.blobs_dir = self.artifacts_dir / BLOBS_DIR_NAME
```

`_blobs` sits inside `audit-artifacts/` on purpose: the viewer's
`isSafeArtifactPath` (`raven/tracing/viewer/server.js:826`) refuses any resolved
artifact path outside `ARTIFACTS_DIR`. Nothing enumerates the kind directories,
so the extra directory is invisible to it.

- [ ] **Step 4: Add the materialize helper**

Add to `TraceStore`, in the `# -- artifacts ---` section above `persist_artifact`:

```python
    def _blob_path(self, sha1: str, extension: str) -> Path:
        return self.blobs_dir / sha1[:2] / f"{sha1}.{extension}"

    def _materialize(self, file_path: Path, text: str, sha1: str, extension: str) -> None:
        """Link ``file_path`` at the single stored copy of ``text``.

        Hard link, not copy: the payload occupies its blocks once however many
        spans reference it, and the link count the filesystem keeps is the
        reference count :mod:`raven.tracing.compact` later reads. Publishing the
        blob with ``os.link`` rather than ``os.replace`` means a concurrent
        writer never swaps the inode out from under a link already handed out.
        """
        blob = self._blob_path(sha1, extension)
        try:
            if not blob.exists():
                self._ensure(blob.parent)
                tmp = blob.parent / f"{sha1}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                try:
                    tmp.write_text(text, encoding="utf-8")
                    os.link(tmp, blob)
                except FileExistsError:
                    pass
                finally:
                    tmp.unlink(missing_ok=True)
            os.link(blob, file_path)
        except FileExistsError:
            pass
        except OSError:
            file_path.write_text(text, encoding="utf-8")
```

- [ ] **Step 5: Call it from `persist_artifact`**

Replace line 155 of `raven/tracing/store.py`:

```python
            file_path.write_text(text, encoding="utf-8")
```

with:

```python
            self._materialize(file_path, text, sha1, extension)
```

Nothing else in `persist_artifact` changes. The surrounding
`except OSError` still turns a genuinely failed write into the error dict.

- [ ] **Step 6: Make the module docstring true**

`raven/tracing/store.py:10` currently claims the directory is "SHA1-deduped",
which it has never been. Replace the layout block's artifact line:

```python
    <state_dir>/logs/audit-artifacts/_blobs/<sha1[:2]>/<sha1>.<ext>   # one copy per payload
    <state_dir>/logs/audit-artifacts/<kind>/<date>/...      # hard links to those blobs
```

- [ ] **Step 7: Run the new tests**

Run: `uv run pytest tests/test_tracing_api.py -k "inode or blob or fallback or txt" -v`
Expected: PASS (5 tests).

- [ ] **Step 8: Run the full regression, unmodified**

Run: `uv run pytest tests/test_tracing_api.py tests/test_subagent_acp.py -q`
Expected: PASS. The pre-existing `artifact_path` assertions are the contract
check - if any of them needed editing, the path contract moved and the change is
wrong.

- [ ] **Step 9: Commit** (only on the maintainer's word - AGENTS.md 3.4)

```bash
git add raven/tracing/store.py tests/test_tracing_api.py
git commit -m "perf(tracing): store one copy per distinct audit artifact

Every artifact was written in full, so identical payloads accumulated
byte-identical copies: turn.output alone held 42641 files with one distinct
content. Publish each payload as a blob under audit-artifacts/_blobs and hard
link the per-kind path at it, so a repeat costs a stat and a link instead of a
write, and the filesystem link count becomes the reference count.

Paths, returned metadata and span attributes are unchanged; a filesystem
without hard links falls back to the previous direct write.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

### Task 2: Compaction over already-written artifacts

**Files:**
- Create: `raven/tracing/compact.py`
- Test: `tests/test_tracing_compact.py`

**Interfaces:**
- Consumes: `BLOBS_DIR_NAME` from `raven/tracing/store.py` and the
  `_blobs/<sha1[:2]>/<sha1>.<ext>` naming Task 1 established. `compact` takes the
  artifacts directory as an argument rather than a `TraceStore`.
- Produces:
  - `compact(artifacts_dir: Path, *, dry_run: bool = False) -> CompactResult`
  - `CompactResult` dataclass with fields `scanned: int`, `folded: int`,
    `skipped_fresh: int`, `blobs_removed: int`, `bytes_reclaimed: int`,
    `errors: list[str]`.
  - Status note: the shipped dataclass also carries `errors_dropped: int`
    (added by the final review's fix wave), and `errors` is capped rather
    than growing without bound.
  - `FRESH_SECONDS: int = 60`.
  Task 3 calls `compact` and formats `CompactResult`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tracing_compact.py`:

```python
"""Unit tests for ``raven.tracing.compact``.

Covers the two halves of a compaction run: folding already-written artifacts
onto one blob per distinct payload, and removing blobs nothing references any
more. Both must leave every artifact path in place and byte-identical.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from raven.tracing.compact import FRESH_SECONDS, CompactResult, compact


def _write(artifacts: Path, kind: str, day: str, name: str, text: str, *, age: int = 3600) -> Path:
    path = artifacts / kind / day / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def test_duplicate_files_are_folded_onto_one_inode(tmp_path):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    b = _write(tmp_path, "llm.input", "2026-08-02", "b.json", '{"m":"same"}')

    result = compact(tmp_path)

    assert result.scanned == 2
    assert result.folded == 1
    assert a.stat().st_ino == b.stat().st_ino
    assert a.read_text(encoding="utf-8") == '{"m":"same"}'
    assert b.read_text(encoding="utf-8") == '{"m":"same"}'


def test_distinct_files_are_left_alone(tmp_path):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"one"}')
    b = _write(tmp_path, "llm.input", "2026-08-01", "b.json", '{"m":"two"}')

    result = compact(tmp_path)

    assert result.folded == 0
    assert a.stat().st_ino != b.stat().st_ino


def test_compaction_is_idempotent(tmp_path):
    _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    _write(tmp_path, "llm.input", "2026-08-01", "b.json", '{"m":"same"}')

    compact(tmp_path)
    second = compact(tmp_path)

    assert second.folded == 0
    assert second.bytes_reclaimed == 0


def test_fresh_files_are_skipped(tmp_path):
    _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    b = _write(tmp_path, "llm.input", "2026-08-01", "b.json", '{"m":"same"}', age=FRESH_SECONDS // 2)
    before = b.stat().st_ino

    result = compact(tmp_path)

    assert result.skipped_fresh == 1
    assert b.stat().st_ino == before


def test_dry_run_writes_nothing(tmp_path):
    a = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    b = _write(tmp_path, "llm.input", "2026-08-01", "b.json", '{"m":"same"}')

    result = compact(tmp_path, dry_run=True)

    assert result.folded == 1
    assert a.stat().st_ino != b.stat().st_ino
    assert not (tmp_path / "_blobs").exists()


def test_unreferenced_blobs_are_removed(tmp_path):
    path = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"gone"}')
    compact(tmp_path)
    blobs = sorted((tmp_path / "_blobs").rglob("*.json"))
    assert len(blobs) == 1
    stamp = time.time() - 3600
    path.unlink()
    os.utime(blobs[0], (stamp, stamp))

    result = compact(tmp_path)

    assert result.blobs_removed == 1
    assert not blobs[0].exists()


def test_referenced_blobs_survive(tmp_path):
    _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"kept"}')
    compact(tmp_path)
    blob = sorted((tmp_path / "_blobs").rglob("*.json"))[0]
    stamp = time.time() - 3600
    os.utime(blob, (stamp, stamp))

    result = compact(tmp_path)

    assert result.blobs_removed == 0
    assert blob.exists()


def test_fresh_orphan_blobs_survive(tmp_path):
    path = _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"racing"}')
    compact(tmp_path)
    blob = sorted((tmp_path / "_blobs").rglob("*.json"))[0]
    path.unlink()
    os.utime(blob, None)

    result = compact(tmp_path)

    assert result.blobs_removed == 0
    assert blob.exists()


def test_blobs_directory_is_not_scanned_as_a_kind(tmp_path):
    _write(tmp_path, "llm.input", "2026-08-01", "a.json", '{"m":"same"}')
    _write(tmp_path, "llm.input", "2026-08-01", "b.json", '{"m":"same"}')

    compact(tmp_path)
    second = compact(tmp_path)

    assert second.scanned == 2


def test_missing_artifacts_dir_is_not_an_error(tmp_path):
    result = compact(tmp_path / "nope")

    assert result == CompactResult()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tracing_compact.py -v`
Expected: FAIL at collection - `ModuleNotFoundError: No module named 'raven.tracing.compact'`.

- [ ] **Step 3: Write the module**

Create `raven/tracing/compact.py`:

```python
"""Fold already-written audit artifacts onto one copy per distinct payload.

The writer (:meth:`raven.tracing.store.TraceStore.persist_artifact`) hard-links
every new artifact at a blob under ``audit-artifacts/_blobs``. Artifacts written
before that landed are standalone files; this module rewrites them in place as
links to the same blobs, and removes blobs nothing references any more.

Paths never change and no referenced artifact is removed, so a run is a pure
convergence: idempotent, and safe against a live raven. Two properties carry
that guarantee. Replacement goes through ``os.replace`` within one directory, so
a reader sees either the old file or the new link and never a partial one; and
anything modified inside :data:`FRESH_SECONDS` is left alone, which keeps the
walk off a file still being written and keeps the sweep off the gap in which the
writer has published a blob but not yet linked a span path to it.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from raven.tracing.store import BLOBS_DIR_NAME

FRESH_SECONDS = 60
_READ_CHUNK = 1 << 20


@dataclass
class CompactResult:
    """What one compaction run did. Byte counts are of blocks actually freed."""

    scanned: int = 0
    folded: int = 0
    skipped_fresh: int = 0
    blobs_removed: int = 0
    bytes_reclaimed: int = 0
    errors: list[str] = field(default_factory=list)


def _sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_fresh(stat_result: os.stat_result, now: float) -> bool:
    return now - stat_result.st_mtime < FRESH_SECONDS


def _artifact_files(artifacts_dir: Path):
    for kind_dir in sorted(artifacts_dir.iterdir()):
        if not kind_dir.is_dir() or kind_dir.name == BLOBS_DIR_NAME:
            continue
        for day_dir in sorted(kind_dir.iterdir()):
            if not day_dir.is_dir():
                continue
            for path in sorted(day_dir.iterdir()):
                if path.is_file():
                    yield path


def _fold_one(path: Path, blob: Path, stat_result: os.stat_result, result: CompactResult) -> None:
    if not blob.exists():
        blob.parent.mkdir(parents=True, exist_ok=True)
        os.link(path, blob)
        return
    tmp = path.parent / f".{path.name}.compact.tmp"
    os.link(blob, tmp)
    os.replace(tmp, path)
    result.folded += 1
    if stat_result.st_nlink == 1:
        result.bytes_reclaimed += stat_result.st_size


def _sweep_orphan_blobs(blobs_dir: Path, now: float, dry_run: bool, result: CompactResult) -> None:
    if not blobs_dir.is_dir():
        return
    for shard in sorted(blobs_dir.iterdir()):
        if not shard.is_dir():
            continue
        for blob in sorted(shard.iterdir()):
            try:
                stat_result = blob.stat()
            except OSError as exc:
                result.errors.append(f"{blob}: {exc}")
                continue
            if stat_result.st_nlink != 1 or _is_fresh(stat_result, now):
                continue
            if not dry_run:
                try:
                    blob.unlink()
                except OSError as exc:
                    result.errors.append(f"{blob}: {exc}")
                    continue
            result.blobs_removed += 1
            result.bytes_reclaimed += stat_result.st_size


def compact(artifacts_dir: Path, *, dry_run: bool = False) -> CompactResult:
    """Fold every artifact under ``artifacts_dir`` onto one blob per payload."""
    result = CompactResult()
    artifacts_dir = Path(artifacts_dir)
    if not artifacts_dir.is_dir():
        return result
    blobs_dir = artifacts_dir / BLOBS_DIR_NAME
    now = time.time()
    planned: set[str] = set()

    for path in _artifact_files(artifacts_dir):
        try:
            stat_result = path.stat()
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
            continue
        result.scanned += 1
        if _is_fresh(stat_result, now):
            result.skipped_fresh += 1
            continue
        try:
            sha1 = _sha1_of(path)
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
            continue
        blob = blobs_dir / sha1[:2] / f"{sha1}{path.suffix}"
        try:
            if blob.exists() and blob.stat().st_ino == stat_result.st_ino:
                planned.add(sha1)
                continue
            if dry_run:
                if blob.exists() or sha1 in planned:
                    result.folded += 1
                    if stat_result.st_nlink == 1:
                        result.bytes_reclaimed += stat_result.st_size
                planned.add(sha1)
                continue
            _fold_one(path, blob, stat_result, result)
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")

    _sweep_orphan_blobs(blobs_dir, now, dry_run, result)
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tracing_compact.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Verify the write path and compaction agree**

They must produce the same on-disk shape, or a compaction run after a live
raven would re-fold what the writer already folded. Run:

```bash
uv run python - <<'PY'
import os, tempfile
from pathlib import Path
from raven.tracing.store import TraceStore
from raven.tracing.compact import compact

with tempfile.TemporaryDirectory() as d:
    store = TraceStore(d)
    a = store.persist_artifact("llm.input", {"traceId": "t1"}, {"m": "x"})
    b = store.persist_artifact("llm.input", {"traceId": "t2"}, {"m": "x"})
    assert Path(a["path"]).stat().st_ino == Path(b["path"]).stat().st_ino
    for p in store.artifacts_dir.rglob("*.json"):
        stamp = 0
        os.utime(p, (stamp, stamp))
    result = compact(store.artifacts_dir)
    print(result)
    assert result.folded == 0, "writer and compactor disagree on layout"
    assert result.blobs_removed == 0
    print("OK: layouts agree")
PY
```

Expected: prints `OK: layouts agree`.

- [ ] **Step 6: Commit** (only on the maintainer's word - AGENTS.md 3.4)

```bash
git add raven/tracing/compact.py tests/test_tracing_compact.py
git commit -m "feat(tracing): fold already-written audit artifacts onto shared blobs

The write path now links new artifacts at a blob, but a month of files predates
it. Walk them, hash each one, and relink it at the blob for its payload through
an atomic replace, then unlink blobs no artifact references any more.

Paths and bytes are preserved and a run is idempotent, so it is safe against a
live raven. Anything touched in the last minute is left alone: that keeps the
walk off a file being written and the sweep off the window where a freshly
published blob has no reference yet.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

### Task 3: `raven tracing compact`

**Files:**
- Modify: `raven/cli/tracing_commands.py:1-22` (module docstring), and
  `raven/cli/tracing_commands.py:281-308` (`register` / the `tracing` command)
- Test: `tests/test_cli_tracing_commands.py`

**Interfaces:**
- Consumes: `compact(artifacts_dir, *, dry_run) -> CompactResult` and
  `CompactResult` from Task 2; `TraceStore.artifacts_dir` from Task 1.
  Status note: the shipped `CompactResult` also carries `errors_dropped: int`
  (added by the final review's fix wave), and `errors` is capped rather than
  growing without bound.
- Produces: the CLI surface `raven tracing compact [--dry-run]`. No other module
  imports it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli_tracing_commands.py`. The module already has
`runner = CliRunner()` (line 27), `from raven.cli import tracing_commands as tc`
and `from raven.cli.commands import app`. Add `import os`, `import time` and
`from pathlib import Path` if not already imported (`os` already is).

```python
def _aged_artifact(state_dir: Path, name: str, text: str) -> Path:
    path = state_dir / "logs" / "audit-artifacts" / "llm.input" / "2026-08-01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - 3600
    os.utime(path, (stamp, stamp))
    return path


def test_tracing_compact_folds_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))
    a = _aged_artifact(tmp_path, "a.json", '{"m":"same"}')
    b = _aged_artifact(tmp_path, "b.json", '{"m":"same"}')

    r = runner.invoke(app, ["tracing", "compact"])

    assert r.exit_code == 0
    assert "scanned 2" in r.output
    assert "folded 1" in r.output
    assert a.stat().st_ino == b.stat().st_ino


def test_tracing_compact_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))
    a = _aged_artifact(tmp_path, "a.json", '{"m":"same"}')
    b = _aged_artifact(tmp_path, "b.json", '{"m":"same"}')

    r = runner.invoke(app, ["tracing", "compact", "--dry-run"])

    assert r.exit_code == 0
    assert "folded 1" in r.output
    assert a.stat().st_ino != b.stat().st_ino
    assert not (tmp_path / "logs" / "audit-artifacts" / "_blobs").exists()


def test_tracing_compact_without_artifacts_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))

    r = runner.invoke(app, ["tracing", "compact"])

    assert r.exit_code == 0
    assert "no artifacts" in r.output.lower()


def test_tracing_rejects_an_unknown_action(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))

    r = runner.invoke(app, ["tracing", "wat"])

    assert r.exit_code == 2
    assert "compact" in r.output
    assert "stop" in r.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_tracing_commands.py -k compact -v`
Expected: FAIL - `raven tracing compact` is rejected as an unknown action with
exit code 2, so the fold assertions never run.

- [ ] **Step 3: Add the compaction runner**

In `raven/cli/tracing_commands.py`, above `def register(...)`:

```python
def _run_compact(*, dry_run: bool) -> None:
    from raven.tracing.compact import compact
    from raven.tracing.store import TraceStore

    artifacts_dir = TraceStore(tracing_config.state_dir()).artifacts_dir
    if not artifacts_dir.is_dir():
        console.print(f"[yellow]No artifacts at {artifacts_dir}.[/yellow]")
        return
    result = compact(artifacts_dir, dry_run=dry_run)
    if dry_run:
        console.print("[dim]dry run - nothing is written[/dim]")
    console.print(f"scanned {result.scanned}, folded {result.folded}, skipped {result.skipped_fresh} fresh")
    console.print(
        f"removed {result.blobs_removed} unreferenced blobs, "
        f"reclaimed {result.bytes_reclaimed / (1024 * 1024):.1f} MB"
    )
    for message in result.errors[:10]:
        console.print(f"[yellow]skipped:[/yellow] {message}")
    if len(result.errors) > 10:
        console.print(f"[yellow]... and {len(result.errors) - 10} more[/yellow]")
```

- [ ] **Step 4: Wire the action into the command**

In `raven/cli/tracing_commands.py`, change the `action` argument help (line 291)
to name both actions:

```python
        action: str = typer.Argument(
            None, help="Optional action: 'stop' shuts down the background viewer; 'compact' folds duplicate artifacts."
        ),
```

Add the option after `foreground` (line 293-295):

```python
        dry_run: bool = typer.Option(
            False, "--dry-run", help="With 'compact': report what would change without writing."
        ),
```

Add the branch above the `stop` branch (line 298) and extend the rejection
message:

```python
        if action == "compact":
            _run_compact(dry_run=dry_run)
            return
        if action == "stop":
            _stop_viewer()
            return
        if action is not None:
            console.print(f"[red]Unknown action '{action}'.[/red] Supported actions: stop, compact")
            raise typer.Exit(2)
```

- [ ] **Step 5: Document the action in the module docstring**

`raven/cli/tracing_commands.py:19-21` states that `stop` is the optional
positional action. Extend that sentence so the file still describes its own
surface:

```python
``stop`` and ``compact`` are optional positional actions; foreground mode, port
and ``--dry-run`` are options, not subcommands.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_tracing_commands.py -v`
Expected: PASS - the four new tests plus the pre-existing viewer-launch tests.

- [ ] **Step 7: Run the whole affected suite**

Run: `uv run pytest tests/test_tracing_api.py tests/test_tracing_compact.py tests/test_cli_tracing_commands.py tests/test_subagent_acp.py -q`
Expected: PASS.

- [ ] **Step 8: Check the real directory with `--dry-run` before anything else**

This is the first contact with the operator's real store (280,033 files, 5.6 GB
as of 2026-08-22). It hashes every file, so expect it to take minutes. Run:

```bash
uv run raven tracing compact --dry-run
```

Expected: a `dry run: scanned ..., folded ..., ...` line, no directory
modified, and `du -sh ~/.raven/traces/logs/audit-artifacts` unchanged. Report
the numbers to the maintainer and stop; running it for real is their call.

- [ ] **Step 9: Commit** (only on the maintainer's word - AGENTS.md 3.4)

```bash
git add raven/cli/tracing_commands.py tests/test_cli_tracing_commands.py
git commit -m "feat(cli): add raven tracing compact

A second positional action alongside stop, so tracing stays a leaf command and
the TUI catalog keeps listing it as a plain /tracing slash. It folds duplicate
artifacts already on disk and drops unreferenced blobs, reporting what it did;
--dry-run reports without writing.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Verification of the whole change

After Task 3, before proposing a merge:

```bash
uv run pytest tests/test_tracing_api.py tests/test_tracing_compact.py \
  tests/test_cli_tracing_commands.py tests/test_subagent_acp.py -q
uv run ruff check raven/tracing/ raven/cli/tracing_commands.py
make check-large-files
git diff --stat origin/main...HEAD
```

The diff must not touch `raven/tracing/viewer/` or anything under `subagents/`
(the four vendored `store.py` copies are out of scope), and must not modify any
existing `artifact_path` assertion.

Before opening an MR, run the pre-submit sweep from
`.claude/skills/mr-review-patterns/`.
