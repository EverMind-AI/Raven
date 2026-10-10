"""Fetching the files that cannot live in git, without fetching anything.

Three hosts and about 110 MB, so every case here supplies the download. What is
asserted is the part that is ours: where the files land, what counts as already
present, that a partial download cannot be mistaken for a whole one, and that
`--optional` really does let an install finish when a host is unreachable --
which is the contract install.sh leans on.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import resources


@pytest.fixture
def res(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both resource trees at a temporary directory.

    The real ones live inside the installed package; a test that wrote there
    would leave 110 MB behind, or worse, a truncated file the next run trusts.
    """
    core, knowledge = tmp_path / "core", tmp_path / "knowledge"
    monkeypatch.setattr(resources, "CORE_RES", core)
    monkeypatch.setattr(resources, "KNOWLEDGE_RES", knowledge)
    monkeypatch.setattr(resources, "NLTK_DATA", core / "nltk_data")
    return tmp_path


class TestWhereTheFilesGo:
    def test_the_destinations_are_derived_from_the_package(self) -> None:
        """Not from the repository root, which a wheel install does not have.
        This is what makes one call correct for both installs."""
        assert resources.CORE_RES == resources.PACKAGE / "core" / "res"
        assert resources.KNOWLEDGE_RES == resources.PACKAGE / "knowledge" / "res"

    def test_a_resource_knows_its_own_path(self) -> None:
        item = resources.Resource("huqie.txt", Path("/tmp/x"), "the word dictionary")

        assert item.path == Path("/tmp/x/huqie.txt")

    def test_a_resource_can_be_saved_under_another_name(self) -> None:
        """The publisher's name and the reader's are not always the same."""
        item = resources.Resource("published.bin", Path("/tmp/x"), "a model", saved_as="expected.onnx")

        assert item.path == Path("/tmp/x/expected.onnx")


class TestPresence:
    """What counts as already here, so a re-run is cheap and a truncated file
    is not trusted."""

    def test_a_file_of_the_published_size_is_present(self, tmp_path: Path) -> None:
        path = tmp_path / "f.bin"
        path.write_bytes(b"x" * 10)

        assert resources._present(resources.Resource("f.bin", tmp_path, "a file"), 10)

    def test_a_file_that_is_not_there_is_not_present(self, tmp_path: Path) -> None:
        assert not resources._present(resources.Resource("f.bin", tmp_path, "a file"), 10)

    def test_a_short_file_is_refetched(self, tmp_path: Path) -> None:
        """The failure this guards is a connection dropped mid-file, which
        leaves a file of the right name and the wrong length."""
        path = tmp_path / "f.bin"
        path.write_bytes(b"x" * 3)

        assert not resources._present(resources.Resource("f.bin", tmp_path, "a file"), 10)

    def test_a_file_is_accepted_when_the_published_size_is_unknown(self, tmp_path: Path) -> None:
        """The index is a convenience. Without it, a file that exists is taken
        on trust rather than downloaded on every run."""
        path = tmp_path / "f.bin"
        path.write_bytes(b"x")

        assert resources._present(resources.Resource("f.bin", tmp_path, "a file"), None)


class TestHumanSizes:
    @pytest.mark.parametrize(
        ("size", "shown"),
        [(0, "0B"), (512, "512B"), (1024, "1.0KB"), (1536, "1.5KB"), (1024 * 1024, "1.0MB")],
    )
    def test_sizes_read_as_a_person_would_write_them(self, size: int, shown: str) -> None:
        assert resources._human(size) == shown


class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.headers = {"content-length": str(sum(len(c) for c in chunks))}

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, _size: int):
        yield from self._chunks

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeClient:
    """Stands in for httpx, recording what was asked for."""

    def __init__(self, chunks: list[bytes], *, fail: bool = False) -> None:
        self.chunks = chunks
        self.fail = fail
        self.urls: list[str] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def head(self, url: str):
        self.urls.append(url)
        return SimpleNamespace(headers={"content-length": str(sum(len(c) for c in self.chunks))})

    def stream(self, _method: str, url: str):
        self.urls.append(url)
        if self.fail:
            raise RuntimeError("the host is unreachable")
        return _FakeResponse(self.chunks)


class TestFetchDictionary:
    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
        import httpx

        monkeypatch.setattr(httpx, "Client", lambda **_kw: client)

    def test_a_download_lands_at_the_readers_path(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, _FakeClient([b"abc", b"def"]))
        item = resources.Resource("huqie.txt", resources.CORE_RES, "the word dictionary")

        written = resources.fetch_dictionary((item,), force=False)

        assert written == 1
        assert item.path.read_bytes() == b"abcdef"

    def test_a_file_already_here_is_left_alone(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient([b"abcdef"])
        self._install(monkeypatch, client)
        item = resources.Resource("huqie.txt", resources.CORE_RES, "the word dictionary")
        item.into.mkdir(parents=True)
        item.path.write_bytes(b"abcdef")

        assert resources.fetch_dictionary((item,), force=False) == 0

    def test_force_downloads_over_a_file_that_is_already_here(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, _FakeClient([b"new"]))
        item = resources.Resource("huqie.txt", resources.CORE_RES, "the word dictionary")
        item.into.mkdir(parents=True)
        item.path.write_bytes(b"old")

        assert resources.fetch_dictionary((item,), force=True) == 1
        assert item.path.read_bytes() == b"new"

    def test_nothing_is_left_behind_when_the_download_fails(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A half-written file under the real name is one every later run
        treats as present, so the download goes to a staging name first."""
        self._install(monkeypatch, _FakeClient([], fail=True))
        item = resources.Resource("huqie.txt", resources.CORE_RES, "the word dictionary")

        with pytest.raises(RuntimeError):
            resources.fetch_dictionary((item,), force=False)

        assert not item.path.exists()

    def test_it_asks_the_publisher_for_the_file_by_name(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient([b"x"])
        self._install(monkeypatch, client)
        item = resources.Resource("huqie.txt", resources.CORE_RES, "the word dictionary")

        resources.fetch_dictionary((item,), force=False)

        assert any(url.endswith("/huqie.txt") for url in client.urls)


class TestFetchDeepdoc:
    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, sizes: dict[str, int] | None = None):
        cached = tmp_path / "hf-cache"
        cached.mkdir(exist_ok=True)
        calls: list[str] = []

        def fake_download(*, repo_id: str, filename: str) -> str:
            calls.append(filename)
            blob = cached / filename
            blob.write_bytes(b"m" * (sizes or {}).get(filename, 4))
            return str(blob)

        def fake_info(_repo: str, files_metadata: bool = False):
            siblings = [SimpleNamespace(rfilename=name, size=size) for name, size in (sizes or {}).items()]
            return SimpleNamespace(siblings=siblings)

        import huggingface_hub

        monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
        monkeypatch.setattr(huggingface_hub, "model_info", fake_info)
        return calls

    def test_a_model_is_copied_out_of_the_cache(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Copied rather than symlinked: an image build throws the cache layer
        away, and a link into a directory that is gone is worse than no file."""
        self._install(monkeypatch, res, sizes={"det.onnx": 4})
        item = resources.Resource("det.onnx", resources.KNOWLEDGE_RES, "text detection")

        written = resources.fetch_deepdoc((item,), force=False)

        assert written == 1
        assert item.path.is_file() and not item.path.is_symlink()

    def test_a_model_already_here_is_left_alone(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._install(monkeypatch, res, sizes={"det.onnx": 4})
        item = resources.Resource("det.onnx", resources.KNOWLEDGE_RES, "text detection")
        item.into.mkdir(parents=True)
        item.path.write_bytes(b"m" * 4)

        assert resources.fetch_deepdoc((item,), force=False) == 0
        assert calls == []

    def test_an_unreadable_index_does_not_stop_the_download(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The published sizes are a convenience; without them the files are
        fetched anyway rather than the whole step failing."""
        import huggingface_hub

        self._install(monkeypatch, res, sizes={"det.onnx": 4})
        monkeypatch.setattr(
            huggingface_hub,
            "model_info",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("no index")),
        )
        item = resources.Resource("det.onnx", resources.KNOWLEDGE_RES, "text detection")

        assert resources.fetch_deepdoc((item,), force=False) == 1


class TestMain:
    """The entry point install.sh calls, and the `--optional` contract."""

    @staticmethod
    def _plan(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        ran: list[str] = []

        def record(name: str):
            def step(_resources=(), *, force=False):
                ran.append(name)
                return 0

            return step

        monkeypatch.setattr(resources, "fetch_dictionary", record("dictionary"))
        monkeypatch.setattr(resources, "fetch_nltk", lambda *, force=False: ran.append("nltk") or 0)
        monkeypatch.setattr(resources, "warm_dictionary", lambda: ran.append("warm"))
        monkeypatch.setattr(resources, "fetch_deepdoc", record("deepdoc"))
        return ran

    def test_it_fetches_both_sets_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ran = self._plan(monkeypatch)

        assert resources.main([]) == 0
        assert "dictionary" in ran and "deepdoc" in ran

    def test_only_dictionary_leaves_the_models_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ran = self._plan(monkeypatch)

        assert resources.main(["--only", "dictionary"]) == 0
        assert "deepdoc" not in ran

    def test_only_deepdoc_leaves_the_dictionary_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ran = self._plan(monkeypatch)

        assert resources.main(["--only", "deepdoc"]) == 0
        assert "dictionary" not in ran and "nltk" not in ran

    def test_a_failure_is_fatal_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._plan(monkeypatch)
        monkeypatch.setattr(
            resources,
            "fetch_dictionary",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("unreachable")),
        )

        assert resources.main([]) == 1

    def test_optional_reports_the_failure_and_carries_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The contract install.sh leans on: a host that cannot be reached
        leaves a working raven rather than an install that stopped halfway."""
        ran = self._plan(monkeypatch)
        monkeypatch.setattr(
            resources,
            "fetch_dictionary",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("unreachable")),
        )

        assert resources.main(["--optional"]) == 0
        assert "deepdoc" in ran, "the step after the failure still ran"

    def test_optional_survives_a_missing_library(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The deepdoc half needs huggingface-hub, which is not a declared
        dependency -- it arrives under litellm. If that ever changes, the fetch
        reports itself missing rather than breaking the install."""
        self._plan(monkeypatch)
        monkeypatch.setattr(
            resources,
            "fetch_deepdoc",
            lambda *_a, **_kw: (_ for _ in ()).throw(ImportError("no huggingface_hub")),
        )

        assert resources.main(["--optional"]) == 0

    def test_the_extra_layout_models_are_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Three more at 76 MB each, which nothing selects between yet."""
        asked: list[tuple] = []
        self._plan(monkeypatch)
        monkeypatch.setattr(resources, "fetch_deepdoc", lambda res, *, force=False: asked.append(res) or 0)

        resources.main([])
        default = len(asked[0])
        asked.clear()
        resources.main(["--all-layouts"])

        assert len(asked[0]) > default
