"""Tests for raven.channels.media — traversal-safe, collision-safe writes."""

import raven.channels.media as media


def test_safe_name():
    assert media.safe_name("../../etc/passwd") == "passwd"
    assert media.safe_name("/abs/evil.sh") == "evil.sh"
    assert media.safe_name("ok.jpg") == "ok.jpg"
    assert media.safe_name("") == "file"
    assert media.safe_name(None) == "file"


def test_save_strips_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "get_media_dir", lambda _ch: tmp_path)
    p = media.save_media_bytes("feishu", b"data", "../../etc/passwd")
    assert p.parent == tmp_path  # did not escape the media dir
    assert p.name.endswith("_passwd")
    assert p.read_bytes() == b"data"


def test_save_no_collision_for_different_content(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "get_media_dir", lambda _ch: tmp_path)
    p1 = media.save_media_bytes("feishu", b"aaa", "report.pdf")
    p2 = media.save_media_bytes("feishu", b"bbb", "report.pdf")
    assert p1 != p2  # same name, different content -> distinct files
    assert p1.read_bytes() == b"aaa"
    assert p2.read_bytes() == b"bbb"


def test_save_idempotent_for_same_content(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "get_media_dir", lambda _ch: tmp_path)
    p1 = media.save_media_bytes("feishu", b"same", "a.bin")
    p2 = media.save_media_bytes("feishu", b"same", "a.bin")
    assert p1 == p2
