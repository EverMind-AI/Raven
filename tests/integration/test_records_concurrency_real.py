"""Two processes, one file, one record.

The store's promise is that a second run meets the first run's record rather
than making a second one. That promise is about processes, not about calls
inside one interpreter, so it is tested with processes: two interpreters
writing the same company at the same time, and the file left holding one
record with the evidence of both.

The lock is the repository's own portable advisory lock, so the test runs on
Windows as well as POSIX -- the point of the lock is that a concurrent write
is serialized rather than lost, and a lock that only works on one platform
would lose the write on the other.

Opening the file is part of the same promise. The first process to open a
fresh path sets the journal mode and creates the schema, and the processes
behind it have to wait for that rather than find the file half-made, so
that is tested with processes too.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from raven.records import Provenance, Record, RecordStore, RecordType

pytestmark = pytest.mark.integration


def _write_one(db_path: Path, name: str, field: str, source: str) -> None:
    """One process, one write."""
    with RecordStore(db_path) as store:
        store.put(
            Record(
                type=RecordType.COMPANY,
                dedupe_key="acme",
                fields={"name": name},
                provenance=[
                    Provenance(
                        field=field, source_url=f"https://example.test/{source}", source=source, method="fetch"
                    )
                ],
            )
        )


def test_two_processes_writing_the_same_thing_leave_one_record(tmp_path: Path) -> None:
    db_path = tmp_path / "records.db"
    with RecordStore(db_path) as store:
        store.put(
            Record(
                type=RecordType.COMPANY,
                dedupe_key="acme",
                fields={"name": "Acme"},
                provenance=[
                    Provenance(
                        field="name", source_url="https://example.test/seed", source="sec_edgar", method="fetch"
                    )
                ],
            )
        )

    program = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from raven.records import Provenance, Record, RecordStore, RecordType

        db_path, name, source = sys.argv[1:4]
        with RecordStore(Path(db_path)) as store:
            store.put(
                Record(
                    type=RecordType.COMPANY,
                    dedupe_key="acme",
                    fields={"name": name, "employees": 12},
                    provenance=[
                        Provenance(
                            field="name",
                            source_url="https://example.test/" + source,
                            source=source,
                            method="fetch",
                        ),
                        Provenance(
                            field="employees",
                            source_url="https://example.test/" + source,
                            source=source,
                            method="fetch",
                        ),
                    ],
                )
            )
        """
    )

    writers = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(db_path), "Acme Inc", "opencorporates"],
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        for _ in range(4)
    ]
    for writer in writers:
        assert writer.wait(timeout=120) == 0

    with RecordStore(db_path) as store:
        assert store.count(RecordType.COMPANY) == 1
        stored = store.get(RecordType.COMPANY, "acme")
        assert stored is not None
        assert {entry.field for entry in stored.provenance} == {"name", "employees"}
        assert len(stored.sources_for("name")) == 2
        assert stored.fields["name"] == "Acme Inc"


# ---------------------------------------------------------------------------
# Opening one fresh file, from several processes at once
# ---------------------------------------------------------------------------

_OPENING_CHILD = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from raven.records import Provenance, Record, RecordStore, RecordType

    db_path, ready_path, go_path = sys.argv[1], sys.argv[2], sys.argv[3]
    Path(ready_path).write_text("ready", encoding="utf-8")
    while not Path(go_path).exists():
        time.sleep(0.0002)
    with RecordStore(Path(db_path)) as store:
        store.put(
            Record(
                type=RecordType.COMPANY,
                dedupe_key="acme",
                fields={"name": "Acme"},
                provenance=[
                    Provenance(
                        field="name",
                        source_url="https://example.test/acme",
                        source="sec_edgar",
                        method="fetch",
                    )
                ],
            )
        )
    """
)

_LOCKED_OPENING_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from raven.records import RecordStore

    db_path, ready_path = sys.argv[1], sys.argv[2]
    Path(ready_path).write_text("ready", encoding="utf-8")
    with RecordStore(Path(db_path)) as store:
        print("OPENED", flush=True)
    """
)


def _wait_for(paths: list[Path], timeout: float = 120.0) -> None:
    """Wait until every one of the paths is there."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(path.exists() for path in paths):
            return
        time.sleep(0.005)
    raise AssertionError("the children did not all get ready in time")


def test_eight_processes_opening_one_fresh_file_all_succeed(tmp_path: Path) -> None:
    """The ordinary first open is a race too, and it must not be a failure.

    The race is run more than once, because a race that does not collide
    proves nothing, and one that does is the failure this is here to stop.
    """
    for round_index in range(5):
        round_dir = tmp_path / f"round-{round_index}"
        round_dir.mkdir()
        db_path = round_dir / "records.db"
        ready_dir = round_dir / "ready"
        ready_dir.mkdir()
        go_path = round_dir / "go"

        children = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _OPENING_CHILD,
                    str(db_path),
                    str(ready_dir / f"ready-{index}"),
                    str(go_path),
                ],
                cwd=str(Path(__file__).resolve().parent.parent.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(8)
        ]
        _wait_for([ready_dir / f"ready-{index}" for index in range(8)])
        go_path.write_text("go", encoding="utf-8")

        for child in children:
            _, stderr = child.communicate(timeout=120)
            assert child.returncode == 0, stderr
            assert "database is locked" not in stderr

        with RecordStore(db_path) as store:
            assert store.count(RecordType.COMPANY) == 1
            stored = store.get(RecordType.COMPANY, "acme")
            assert stored is not None
            assert stored.fields["name"] == "Acme"


def test_opening_a_database_waits_for_the_store_lock(tmp_path: Path) -> None:
    """The opening is a write, so it takes its turn like every other write."""
    db_path = tmp_path / "records.db"
    ready_path = tmp_path / "ready"

    with RecordStore(db_path) as store:
        with store.locked():
            child = subprocess.Popen(
                [sys.executable, "-c", _LOCKED_OPENING_CHILD, str(db_path), str(ready_path)],
                cwd=str(Path(__file__).resolve().parent.parent.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _wait_for([ready_path])
            time.sleep(1.0)
            assert child.poll() is None, "the child opened the database while the store's lock was held"

        stdout, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, stderr
        assert stdout.strip() == "OPENED"
