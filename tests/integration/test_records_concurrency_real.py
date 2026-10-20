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
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
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

        db_path, name, field, source = sys.argv[1:5]
        with RecordStore(Path(db_path)) as store:
            store.put(
                Record(
                    type=RecordType.COMPANY,
                    dedupe_key="acme",
                    fields={"name": name},
                    provenance=[
                        Provenance(
                            field=field,
                            source_url="https://example.test/" + source,
                            source=source,
                            method="fetch",
                        )
                    ],
                )
            )
        """
    )

    writers = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(db_path), "Acme Inc", "employees", "opencorporates"],
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
        assert stored.fields["name"] == "Acme Inc"
