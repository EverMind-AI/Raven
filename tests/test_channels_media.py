"""Tests for raven.channels.media - traversal-safe, collision-safe writes."""

import os

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


def test_file_uri_to_path_resolves_local_uri(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("x")
    p = media.file_uri_to_path(f.as_uri())
    assert p is not None
    assert p == f


def test_file_uri_to_path_decodes_percent_encoding_once(tmp_path):
    f = tmp_path / "pct%20name.csv"
    f.write_text("x")
    assert media.file_uri_to_path(f.as_uri()) == f


def test_file_uri_to_path_keeps_localhost_share():
    p = media.file_uri_to_path("file://localhost/C%24/data/x.csv")
    if os.name == "nt":
        assert str(p) == r"\\localhost\C$\data\x.csv"
    else:
        assert str(p) == "//localhost/C$/data/x.csv"


def test_file_uri_to_path_refuses_remote_host():
    assert media.file_uri_to_path("file://server/share/x.csv") is None


def test_file_uri_to_path_refuses_non_file_uri():
    assert media.file_uri_to_path("http://example.com/x.csv") is None
