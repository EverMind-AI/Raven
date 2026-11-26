"""Tests for raven.channels.media - traversal-safe, collision-safe writes."""

import nturl2path
import os
from pathlib import PureWindowsPath

import raven.channels.media as media


def _simulate_windows(monkeypatch):
    """Substitute the Windows converter beside the platform flag.

    ``url2pathname`` is what makes the conversion platform-aware, so a test
    that patches only the flag exercises the POSIX converter on a Windows
    branch. Windows' own converter is the same pure string conversion.
    """
    monkeypatch.setattr(media, "_IS_WINDOWS", True)
    monkeypatch.setattr(media, "url2pathname", nturl2path.url2pathname)


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
        # POSIX: a localhost authority names this machine, so the path is local.
        assert str(p) == "/C$/data/x.csv"


def test_file_uri_to_path_localhost_is_local_on_posix(tmp_path):
    """``file://localhost/p`` reads the same file as ``file:///p`` on POSIX
    (urllib does), so the resolver must agree with it."""
    if os.name == "nt":
        return
    f = tmp_path / "local.csv"
    f.write_text("x")
    uri = f"file://localhost{f}"
    assert media.file_uri_to_path(uri) == f


def test_file_uri_to_path_localhost_keeps_authority_for_windows(monkeypatch):
    """A localhost authority in front of a share survives as the UNC host."""
    _simulate_windows(monkeypatch)
    p = media.file_uri_to_path("file://localhost/C%24/data/x.csv")
    assert p is not None
    assert str(p) == r"\\localhost\C$\data\x.csv"


def test_file_uri_to_path_windows_unc_normalizes(monkeypatch):
    """The Windows branch builds a UNC string; PureWindowsPath checks the
    spelling a Windows host will normalize it to, even when the suite runs on
    POSIX."""
    _simulate_windows(monkeypatch)
    p = media.file_uri_to_path("file://localhost/C%24/data/x.csv")
    assert p is not None
    assert str(PureWindowsPath(str(p))) == r"\\localhost\C$\data\x.csv"


def test_file_uri_to_path_localhost_is_case_insensitive_on_posix(tmp_path):
    """``LOCALHOST`` names this machine, as the lowercase spelling does."""
    if os.name == "nt":
        return
    f = tmp_path / "mixed.csv"
    f.write_text("x")
    assert media.file_uri_to_path(f"file://LOCALHOST{f}") == f


def test_a_mixed_case_localhost_drive_url_keeps_the_drive_on_windows(monkeypatch):
    r"""``LOCALHOST`` names this machine, so a drive path stays a drive.

    A case-sensitive comparison refused this URI; returning the authority's
    UNC form instead would read ``\\LOCALHOST\C:\work\a.md``, a share named
    ``C:`` that no Windows host can open.
    """
    _simulate_windows(monkeypatch)
    p = media.file_uri_to_path("file://LOCALHOST/C:/work/a.md")
    assert p is not None
    assert str(p) == r"C:\work\a.md"


def test_a_mixed_case_localhost_share_url_keeps_the_authority_on_windows(monkeypatch):
    _simulate_windows(monkeypatch)
    p = media.file_uri_to_path("file://LOCALHOST/share/x")
    assert p is not None
    assert str(p) == r"\\LOCALHOST\share\x"


def test_file_uri_to_path_refuses_a_mixed_case_remote_host():
    assert media.file_uri_to_path("file://OtherBox/share/x") is None


def test_a_windows_path_the_converter_cannot_read_names_no_file(monkeypatch):
    """``nturl2path`` raises ``OSError`` on a doubled drive colon."""
    _simulate_windows(monkeypatch)
    assert media.file_uri_to_path("file:///C::/work/a.md") is None


def test_file_uri_to_path_refuses_remote_host():
    assert media.file_uri_to_path("file://server/share/x.csv") is None


def test_file_uri_to_path_refuses_remote_host_even_when_path_exists(tmp_path):
    """The host is what is refused: a remote URI whose path happens to exist
    locally must still resolve to None, never to that local file."""
    real = tmp_path / "x.csv"
    real.write_text("x")
    assert media.file_uri_to_path(f"file://server{real}") is None


def test_file_uri_to_path_refuses_non_file_uri():
    assert media.file_uri_to_path("http://example.com/x.csv") is None


def test_file_uri_to_path_refuses_unparsable_uri():
    """urlparse raises on a bracketed host that is not a valid IPv6 literal."""
    assert media.file_uri_to_path("file://[bad/x.csv") is None


def test_file_uri_to_path_refuses_empty_path():
    assert media.file_uri_to_path("file://") is None
    # A localhost authority with no path names no file on any platform.
    assert media.file_uri_to_path("file://localhost") is None
