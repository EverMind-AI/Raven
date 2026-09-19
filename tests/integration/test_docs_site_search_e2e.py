"""The search index a reader is actually served, in both ways the site runs.

One tree is built once per language and the i18n plugin then merges the two
search indexes into one, so a reader searching in either language is offered
pages in the other. A hook splits that merged index back apart.

Which event the hook splits on decides whether the split ever happens. The
merge lands in a plugin's `on_post_build`, so a hook that runs before that
plugin sees a half-built picture, and one that waits for `on_shutdown` never
runs at all under `mkdocs serve` -- the preview server does not shut down. The
two tests here are the same assertion made against the two ways the site is
served, because passing one says nothing about the other.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs-site"
ZH = "zh/"
#: What the segmenter puts between two Chinese words: a zero-width space,
#: the only word boundary the text has.
BOUNDARY = "\u200b"
READY_TIMEOUT = 120.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _fetch(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, OSError):
        # the preview server is not listening yet; the caller polls
        return 0, b""


def _locations(payload: bytes) -> list[str]:
    return [entry["location"] for entry in json.loads(payload)["docs"]]


def _assert_split(english: list[str], chinese: list[str]) -> None:
    """Each index holds one language, addressed from its own root.

    The Chinese entries lose their `zh/` prefix because the Chinese pages are
    pointed at their own index, and the theme resolves every result href
    against that same root.
    """
    assert english, "the English index is empty"
    assert chinese, "the Chinese index is empty"
    strays = [location for location in english if location.startswith(ZH)]
    assert not strays, f"the English index carries {len(strays)} Chinese page(s): {strays[:3]}"
    prefixed = [location for location in chinese if location.startswith(ZH)]
    assert not prefixed, f"{len(prefixed)} Chinese entries are still addressed from the site root: {prefixed[:3]}"


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not shutil.which("uv"):
        pytest.skip("uv is required to build the documentation site")
    out = tmp_path_factory.mktemp("built") / "Raven"
    build = subprocess.run(
        ["uv", "run", "--group", "docs", "mkdocs", "build", "--strict", "-d", str(out)],
        cwd=DOCS,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.skip(f"mkdocs build unavailable: {build.stderr[-400:]}")
    return out


@pytest.fixture(scope="module")
def preview() -> Iterator[str]:
    if not shutil.which("uv"):
        pytest.skip("uv is required to run the documentation preview")
    port = _free_port()
    server = subprocess.Popen(  # noqa: S603
        ["uv", "run", "--group", "docs", "mkdocs", "serve", "--dev-addr", f"127.0.0.1:{port}"],
        cwd=DOCS,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}/Raven/"
    deadline = time.monotonic() + READY_TIMEOUT
    try:
        while time.monotonic() < deadline:
            if server.poll() is not None:
                pytest.skip(f"mkdocs serve exited: {(server.stdout.read() if server.stdout else '')[-400:]}")
            if _fetch(base)[0] == 200:
                break
            time.sleep(0.25)
        else:
            pytest.skip("mkdocs serve did not come up")
        yield base
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()


def test_a_built_site_gives_each_language_its_own_index(built: Path) -> None:
    english = built / "search" / "search_index.json"
    chinese = built / ZH / "search" / "search_index.json"
    assert chinese.exists(), "the Chinese pages have no index of their own to fetch"
    _assert_split(
        _locations(english.read_bytes()),
        _locations(chinese.read_bytes()),
    )


@pytest.mark.production_timing  # the wait is a real server starting, not a delay the suite can shorten
def test_the_preview_server_gives_each_language_its_own_index(preview: str) -> None:
    english_status, english = _fetch(preview + "search/search_index.json")
    assert english_status == 200, f"the English index is not served: HTTP {english_status}"
    chinese_status, chinese = _fetch(preview + ZH + "search/search_index.json")
    assert chinese_status == 200, (
        f"the preview serves no Chinese index (HTTP {chinese_status}), so Chinese pages read the English one"
    )
    _assert_split(_locations(english), _locations(chinese))


def _searchable(payload: bytes) -> tuple[int, str]:
    index = json.loads(payload)
    marked = sum(1 for e in index["docs"] if BOUNDARY in (e.get("title", "") + e.get("text", "")))
    return marked, index["config"]["separator"]


def test_the_chinese_pages_are_searchable_by_word(built: Path) -> None:
    """Chinese runs no spaces, so nothing in the text says where one word ends.
    Two things have to line up for a reader to find anything: the build has to
    segment the text and mark the boundaries, and the theme's separator has to
    treat that mark as a break. Either one alone leaves the whole run as a
    single token, which only matches a reader who types the entire run."""
    payload = (built / ZH / "search" / "search_index.json").read_bytes()
    marked, separator = _searchable(payload)
    total = len(json.loads(payload)["docs"])
    assert marked == total, (
        f"only {marked} of {total} Chinese entries carry word boundaries; the build is not segmenting the text"
    )
    assert re.search(separator, BOUNDARY), (
        f"the separator {separator!r} does not break on the word boundary, so every Chinese run stays one token"
    )
