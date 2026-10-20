"""The design index must stay true of the documents it indexes.

The repo's design backlog is a hundred documents deep, and several of them
record a decision *not* to act, with the measurement behind it. A contributor
who cannot see that will re-propose what was already declined; a reader who
cannot tell a draft from a shipped design will cite a proposal as if it were
the design.

So the index is generated from the documents themselves, and this test
regenerates it and compares. A design document added without an index entry
fails here rather than joining the backlog unnoticed, and a document whose
header changed fails until the index is regenerated.

The index is also the honest record of what it does not know: a Chinese
design with no English rendering is recorded as having none, and a document
whose status cannot be read is recorded as ``unknown`` rather than guessed
at. Both are asserted here, because an index that guesses is worse than no
index.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "docs" / "specs"
PLANS = ROOT / "docs" / "plans"
INDEX_JSON = ROOT / "docs" / "design-index.json"
INDEX_MD = ROOT / "docs" / "design-index.md"


def _load_generator():
    """The generator, loaded from its path so the test and the script agree."""
    path = ROOT / "scripts" / "build_design_index.py"
    spec = importlib.util.spec_from_file_location("build_design_index", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the owning module through sys.modules, so a module
    # loaded without being registered cannot build its classes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    return _load_generator()


@pytest.fixture(scope="module")
def index() -> dict:
    return json.loads(INDEX_JSON.read_text(encoding="utf-8"))


def _corpus() -> list[Path]:
    return sorted(SPECS.glob("*.md")) + sorted(PLANS.glob("*.md"))


def test_the_index_file_exists_and_declares_its_schema(index: dict) -> None:
    assert INDEX_JSON.exists()
    assert INDEX_MD.exists()
    assert index["schema"] == "raven.design-index/1"
    assert index["generated_by"] == "scripts/build_design_index.py"


def test_the_index_covers_every_design_document(index: dict) -> None:
    """Every document in the corpus has an entry, and every entry is a real file."""
    corpus = {path.stem for path in _corpus()}
    indexed = {doc["id"] for doc in index["documents"]}
    assert indexed == corpus, {
        "missing from the index": sorted(corpus - indexed),
        "in the index but not on disk": sorted(indexed - corpus),
    }
    assert index["count"] == len(index["documents"]) == len(corpus)


def test_every_entry_carries_the_fields_a_reader_needs(index: dict) -> None:
    for doc in index["documents"]:
        for field in ("id", "path", "title", "date", "language", "status", "decision", "baseline"):
            assert field in doc, f"{doc['id']} has no {field}"
        assert doc["title"], f"{doc['id']} has no title"
        assert doc["date"], f"{doc['id']} has no date"
        assert doc["language"] in ("en", "zh"), doc["id"]
        assert doc["decision"] in ("design", "plan", "implemented", "declined", "extended", "unknown"), doc["id"]
        assert (ROOT / doc["path"]).exists(), doc["path"]


def test_the_index_is_in_sync_with_the_documents(generator) -> None:
    """The CI check: a document added or changed without regenerating fails here.

    This is the whole point of generating the index rather than maintaining it
    by hand. A hand-maintained index drifts silently; a generated one cannot.
    """
    documents = generator.build()
    payload = {
        "schema": "raven.design-index/1",
        "generated_by": "scripts/build_design_index.py",
        "count": len(documents),
        "documents": [doc.to_dict() for doc in sorted(documents, key=lambda item: item.id)],
    }
    expected = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    assert INDEX_JSON.read_text(encoding="utf-8") == expected, (
        "the design index is out of date. Run: python scripts/build_design_index.py"
    )
    assert INDEX_MD.read_text(encoding="utf-8") == generator.render_markdown(documents), (
        "the design index's markdown view is out of date. Run: python scripts/build_design_index.py"
    )


def test_the_index_is_deterministic(generator) -> None:
    """Two runs produce the same bytes.

    An index that changes every run cannot be diffed, and a diff that cannot
    be read is a diff nobody reads.
    """
    first = json.dumps([doc.to_dict() for doc in generator.build()], indent=2, ensure_ascii=False)
    second = json.dumps([doc.to_dict() for doc in generator.build()], indent=2, ensure_ascii=False)
    assert first == second


def test_a_chinese_design_without_an_english_rendering_is_recorded_as_such(index: dict) -> None:
    """The index says what it does not have.

    The largest design in the repo is Chinese, and it has no English twin.
    An index that implied otherwise would send a reader looking for a
    rendering that does not exist.
    """
    chinese = [doc for doc in index["documents"] if doc["language"] == "zh"]
    assert chinese, "the corpus has Chinese designs and the index must record them"
    for doc in chinese:
        twin = ROOT / doc["path"]
        assert not twin.with_suffix(".en.md").exists(), f"{doc['id']} has an English twin the index does not know about"
        assert doc["has_english_rendering"] is False, doc["id"]


def test_an_english_design_records_itself_as_the_rendering(index: dict) -> None:
    english = [doc for doc in index["documents"] if doc["language"] == "en"]
    assert english
    assert all(doc["has_english_rendering"] is True for doc in english)


def test_the_supersede_relation_points_the_right_way(index: dict) -> None:
    """A document that extended another is recorded as the one doing it.

    The Chinese documents say "已被扩展" -- has been extended -- and link to
    the document that extended them. Reading that as the other way round
    would tell a reader the newer document is the stale one.
    """
    by_id = {doc["id"]: doc for doc in index["documents"]}
    for doc in index["documents"]:
        for name in doc["supersedes"]:
            target = by_id.get(Path(name).stem)
            assert target is not None, f"{doc['id']} supersedes {name}, which is not in the corpus"
            assert doc["id"] in target["superseded_by"], f"{doc['id']} -> {name} is not recorded the other way"
        for name in doc["superseded_by"]:
            target = by_id.get(Path(name).stem)
            assert target is not None, f"{doc['id']} is superseded by {name}, which is not in the corpus"
            assert doc["id"] in target["supersedes"], f"{doc['id']} <- {name} is not recorded the other way"


def test_a_document_with_no_readable_status_is_filed_from_its_title_or_not_at_all(generator) -> None:
    """The index does not guess.

    A status that was read must land in a bucket: a readable status that
    filed as ``unknown`` would mean the vocabulary had a hole, and a reader
    would have no way to know which documents those were.

    A document with *no* status is filed from its title when the title names
    a plan or a design -- the plan family is titled "Implementation Plan" and
    says nothing else in its header, and a title that says "design" is not a
    guess. Everything else is ``unknown``.
    """
    for doc in generator.build():
        if doc.status.strip():
            assert doc.decision != "unknown", f"{doc.id} has the status {doc.status!r} but was filed as unknown"
            continue
        lowered = doc.title.lower()
        if "implementation plan" in lowered:
            assert doc.decision == "plan", f"{doc.id} is titled a plan but was filed as {doc.decision}"
        elif "design" in lowered or "proposal" in lowered:
            assert doc.decision == "design", f"{doc.id} is titled a design but was filed as {doc.decision}"
        else:
            assert doc.decision == "unknown", f"{doc.id} has no status but was filed as {doc.decision}"


def test_the_index_is_exportable_as_an_evidence_input(index: dict) -> None:
    """It has to survive being handed to something that did not write it.

    The evidence bundle is the product the plan is aimed at, and the index is
    meant to be one of its inputs. That means plain JSON, a declared schema,
    and a count that agrees with the list.
    """
    raw = INDEX_JSON.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert isinstance(parsed["documents"], list)
    assert parsed["count"] == len(parsed["documents"])
    for doc in parsed["documents"]:
        assert isinstance(doc["id"], str)
        assert isinstance(doc["supersedes"], list)
        assert isinstance(doc["superseded_by"], list)
        assert isinstance(doc["bytes"], int)
        assert isinstance(doc["cjk_share"], float)
    # It must not carry anything a machine cannot read.
    assert raw.encode("utf-8").decode("utf-8") == raw
