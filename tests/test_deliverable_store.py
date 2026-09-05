"""Tests for the persisted deliverable token store."""

from __future__ import annotations

import json

from raven.agent.tools.deliverables import DeliverableStore


def _make_file(tmp_path, name="report.pdf", body=b"hello"):
    fp = tmp_path / name
    fp.write_bytes(body)
    return fp


def test_register_returns_random_token_not_derived_from_path(tmp_path) -> None:
    """The token is the download capability, so it must not be derivable from
    the path (paths are guessable; a derived token would be too)."""
    store = DeliverableStore(tmp_path / "deliverables.json")
    a = _make_file(tmp_path, "a.txt")
    b = _make_file(tmp_path, "b.txt")

    rec_a = store.register(path=str(a), name="a.txt", media_type="text/plain", size=5, conversation="web:s1")
    rec_b = store.register(path=str(b), name="b.txt", media_type="text/plain", size=5, conversation="web:s1")

    assert rec_a.token != rec_b.token
    assert len(rec_a.token) >= 32
    assert "a.txt" not in rec_a.token


def test_register_same_path_same_conversation_reuses_token_and_refreshes_size(tmp_path) -> None:
    """Re-delivering a file must not mint a second token (the panel de-dups by
    path), but the size shown must track the file as it is now."""
    store = DeliverableStore(tmp_path / "deliverables.json")
    fp = _make_file(tmp_path, "a.txt", b"12345")

    first = store.register(path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s1")
    second = store.register(path=str(fp), name="a.txt", media_type="text/plain", size=99, conversation="web:s1")

    assert second.token == first.token
    assert second.size == 99
    assert store.get(first.token).size == 99


def test_register_same_path_different_conversation_mints_new_token(tmp_path) -> None:
    store = DeliverableStore(tmp_path / "deliverables.json")
    fp = _make_file(tmp_path, "a.txt")

    one = store.register(path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s1")
    two = store.register(path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s2")

    assert one.token != two.token


def test_token_resolves_after_reload(tmp_path) -> None:
    """The core promise of persisting the registry: a gateway restart must not
    break download buttons the (persisted) manifest still shows."""
    path = tmp_path / "deliverables.json"
    fp = _make_file(tmp_path, "a.txt")
    token = (
        DeliverableStore(path)
        .register(path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s1")
        .token
    )

    reloaded = DeliverableStore(path)

    rec = reloaded.get(token)
    assert rec is not None
    assert rec.path == str(fp)
    assert rec.name == "a.txt"


def test_startup_prunes_entries_whose_file_is_gone(tmp_path) -> None:
    path = tmp_path / "deliverables.json"
    fp = _make_file(tmp_path, "a.txt")
    store = DeliverableStore(path)
    token = store.register(path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s1").token
    fp.unlink()

    reloaded = DeliverableStore(path)

    assert reloaded.get(token) is None
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_unreadable_store_starts_empty_instead_of_raising(tmp_path) -> None:
    path = tmp_path / "deliverables.json"
    path.write_text("{ not json", encoding="utf-8")

    store = DeliverableStore(path)

    assert store.get("anything") is None


def test_drop_removes_and_persists(tmp_path) -> None:
    path = tmp_path / "deliverables.json"
    fp = _make_file(tmp_path, "a.txt")
    store = DeliverableStore(path)
    token = store.register(path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s1").token

    store.drop(token)

    assert store.get(token) is None
    assert DeliverableStore(path).get(token) is None


def test_the_store_remembers_what_the_agent_called_the_file(tmp_path):
    """Title and description are stored, not only sent.

    They ride the turn event to whichever client is connected at that moment.
    Everything that asks later -- a reconnect, a second client, a shelf rebuilt
    after a compaction archived the turn -- reads them from here or not at all.
    """
    store = DeliverableStore(tmp_path / "d.json")
    target = tmp_path / "brief.md"
    target.write_text("x")

    store.register(
        path=str(target),
        name="brief.md",
        media_type="text/markdown",
        size=1,
        conversation="tui:s1",
        title="The brief",
        description="what it is",
    )

    reopened = DeliverableStore(tmp_path / "d.json")
    rows = reopened.for_conversation("tui:s1")
    assert [(r.title, r.description) for r in rows] == [("The brief", "what it is")]


def test_for_conversation_is_that_conversation_oldest_first(tmp_path):
    store = DeliverableStore(tmp_path / "d.json")
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).write_text(name)
    store.register(path=str(tmp_path / "a.md"), name="a.md", media_type="text/markdown", size=1, conversation="tui:s1")
    store.register(path=str(tmp_path / "b.md"), name="b.md", media_type="text/markdown", size=1, conversation="tui:s2")
    store.register(path=str(tmp_path / "c.md"), name="c.md", media_type="text/markdown", size=1, conversation="tui:s1")

    rows = store.for_conversation("tui:s1")

    assert [r.name for r in rows] == ["a.md", "c.md"]
    assert [r.created_at for r in rows] == sorted(r.created_at for r in rows)
    assert store.for_conversation("") == []
    assert store.for_conversation("tui:nobody") == []


def test_a_registry_written_before_titles_existed_still_loads(tmp_path):
    """The two fields default, so an entry saved by an older build is read back
    rather than dropped as malformed."""
    path = tmp_path / "d.json"
    target = tmp_path / "old.md"
    target.write_text("x")
    path.write_text(
        json.dumps(
            {
                "tok1": {
                    "path": str(target),
                    "name": "old.md",
                    "media_type": "text/markdown",
                    "size": 1,
                    "conversation": "tui:s1",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            }
        )
    )

    rows = DeliverableStore(path).for_conversation("tui:s1")

    assert [(r.name, r.title, r.description) for r in rows] == [("old.md", "", "")]
