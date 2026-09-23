"""Finding libcairo where Homebrew puts it, and importing cairosvg against it."""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

from raven.utils import cairo


@pytest.fixture()
def brew_prefix(monkeypatch, tmp_path: Path) -> Path:
    """A darwin host whose default lookup finds nothing and whose brew lib dir has cairo."""
    lib = tmp_path / "homebrew" / "lib"
    lib.mkdir(parents=True)
    (lib / "libcairo.2.dylib").write_bytes(b"")
    monkeypatch.setattr(cairo.sys, "platform", "darwin")
    monkeypatch.setattr(cairo, "find_library", lambda name: None)
    monkeypatch.setattr(cairo, "_DARWIN_LIBRARY_DIRS", (str(tmp_path / "absent"), str(lib)))
    monkeypatch.delenv(cairo._FALLBACK_VAR, raising=False)
    return lib


def test_a_homebrew_libcairo_the_default_lookup_misses_is_found(brew_prefix: Path) -> None:
    assert cairo.libcairo_path() == str(brew_prefix / "libcairo.2.dylib")


def test_what_the_default_lookup_finds_wins(brew_prefix: Path, monkeypatch) -> None:
    monkeypatch.setattr(cairo, "find_library", lambda name: "/usr/lib/libcairo.so.2" if name == "cairo" else None)

    assert cairo.libcairo_path() == "/usr/lib/libcairo.so.2"


def test_no_libcairo_anywhere_is_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cairo.sys, "platform", "darwin")
    monkeypatch.setattr(cairo, "find_library", lambda name: None)
    monkeypatch.setattr(cairo, "_DARWIN_LIBRARY_DIRS", (str(tmp_path),))

    assert cairo.libcairo_path() is None


@pytest.mark.parametrize(
    ("platform", "hint"),
    [("darwin", "brew install cairo"), ("linux", "apt install libcairo2"), ("win32", "GTK runtime")],
)
def test_the_install_hint_names_this_platforms_package(monkeypatch, platform: str, hint: str) -> None:
    monkeypatch.setattr(cairo.sys, "platform", platform)

    assert hint in cairo.install_hint()


def test_the_lookup_variable_is_held_only_inside_the_block(brew_prefix: Path) -> None:
    """Set for the import and gone after it, so a subprocess spawned later does not
    inherit a library path nobody asked for."""
    with cairo.libcairo_reachable():
        inside = os.environ.get(cairo._FALLBACK_VAR)

    assert inside == str(brew_prefix)
    assert cairo._FALLBACK_VAR not in os.environ


def test_a_lookup_variable_the_user_set_is_kept_and_restored(brew_prefix: Path, monkeypatch) -> None:
    monkeypatch.setenv(cairo._FALLBACK_VAR, "/their/lib")

    with pytest.raises(OSError), cairo.libcairo_reachable():
        inside = os.environ.get(cairo._FALLBACK_VAR)
        raise OSError("the import failed")

    assert inside == os.pathsep.join((str(brew_prefix), "/their/lib"))
    assert os.environ[cairo._FALLBACK_VAR] == "/their/lib"


def test_a_host_whose_default_lookup_works_is_left_alone(brew_prefix: Path, monkeypatch) -> None:
    monkeypatch.setattr(cairo, "find_library", lambda name: "/usr/lib/libcairo.so.2")

    with cairo.libcairo_reachable():
        assert cairo._FALLBACK_VAR not in os.environ


@pytest.mark.skipif(sys.platform != "darwin", reason="the dyld fallback path is macOS-only")
def test_cairosvg_imports_with_no_dyld_variable_where_brew_installed_cairo() -> None:
    """The reported failure, end to end, in a fresh interpreter: no DYLD variable set,
    and a libcairo only under a Homebrew prefix."""
    if find_spec("cairosvg") is None:
        pytest.skip("cairosvg is the ppt engine's dependency and is not installed")
    if cairo._darwin_library_dir() is None:
        pytest.skip("no Homebrew or MacPorts libcairo on this host")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DYLD_")}
    probe = "from raven.utils.cairo import libcairo_reachable\nwith libcairo_reachable():\n    import cairosvg\nprint('loaded')"

    result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True)

    assert result.stdout.strip() == "loaded", result.stdout + result.stderr
