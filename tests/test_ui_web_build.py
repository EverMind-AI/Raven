"""What ui-web/build.py leaves in dist/: the page, and beside it the gzip copy.

`raven serve` answers `/` with aiohttp's FileResponse, which sends
``index.html.gz`` to a client that accepts gzip when the file lies beside
the page. So the copy has to be the page, byte for byte once inflated, and
must never outlive the page it was made from.
"""

from __future__ import annotations

import gzip
import importlib.util
import time
from pathlib import Path
from types import ModuleType

import pytest

BUILD = Path(__file__).resolve().parent.parent / "ui-web" / "build.py"


@pytest.fixture(scope="module")
def build() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ui_web_build", BUILD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_gzip_copy_inflates_to_the_page(build: ModuleType, tmp_path: Path) -> None:
    build._write_page(tmp_path, "<html>\u00e9\n</html>")
    page = (tmp_path / "index.html").read_bytes()
    assert page == "<html>\u00e9\n</html>".encode()
    assert gzip.decompress((tmp_path / "index.html.gz").read_bytes()) == page


def test_the_same_page_packs_to_the_same_bytes(
    build: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An hour apart, so a timestamp in the gzip header would show."""
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    build._write_page(first, "<html>same</html>")
    later = time.time() + 3600
    monkeypatch.setattr(time, "time", lambda: later)
    build._write_page(second, "<html>same</html>")
    assert (first / "index.html.gz").read_bytes() == (second / "index.html.gz").read_bytes()


def test_a_failed_write_leaves_no_copy_of_the_page_before(
    build: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index.html.gz").write_bytes(gzip.compress(b"<html>old</html>"))

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "write_text", refuse)
    with pytest.raises(OSError):
        build._write_page(tmp_path, "<html>new</html>")
    assert not (tmp_path / "index.html.gz").exists()


def test_a_copy_held_open_for_a_moment_is_still_replaced(
    build: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows refuses to remove a file another handle holds, and the gateway
    holds the copy for as long as it is sending it."""
    copy = tmp_path / "index.html.gz"
    copy.write_bytes(gzip.compress(b"<html>old</html>"))
    unlink = Path.unlink
    refusals = iter([True, True])

    def held(self: Path, missing_ok: bool = False) -> None:
        if self == copy and next(refusals, False):
            raise PermissionError(13, "The process cannot access the file")
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", held)
    monkeypatch.setattr(build.time, "sleep", lambda _s: None)
    build._write_page(tmp_path, "<html>new</html>")
    assert gzip.decompress(copy.read_bytes()) == b"<html>new</html>"


def test_a_copy_that_stays_held_fails_the_build_before_the_page_changes(
    build: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "index.html.gz"
    copy.write_bytes(gzip.compress(b"<html>old</html>"))
    (tmp_path / "index.html").write_text("<html>old</html>", encoding="utf-8")

    def held(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(Path, "unlink", held)
    monkeypatch.setattr(build.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        build._write_page(tmp_path, "<html>new</html>")
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == "<html>old</html>"
    assert gzip.decompress(copy.read_bytes()) == b"<html>old</html>"
