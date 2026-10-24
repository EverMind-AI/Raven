"""Fetching the files that cannot live in git, without fetching anything.

Three hosts and about 110 MB, so every case here supplies the download. What is
asserted is the part that is ours: where the files land, what counts as already
present, that a partial download cannot be mistaken for a whole one, and that
`--optional` really does let an install finish when a host is unreachable --
which is the contract install.sh leans on.
"""

from __future__ import annotations

import http.server
import threading
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

    def head(self, url: str, *, timeout: object = None):
        self.urls.append(url)
        return SimpleNamespace(headers={"content-length": str(sum(len(c) for c in self.chunks))})

    def stream(self, _method: str, url: str, *, timeout: object = None):
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


class _RoutedClient:
    """Stands in for httpx with one answer per host: the bytes it serves, or
    the exception asking it raises."""

    def __init__(self, answers: dict[str, bytes | Exception], **kwargs: object) -> None:
        self.answers = answers
        self.kwargs = kwargs
        self.urls: list[str] = []
        self.timeouts: list[tuple[str, object]] = []

    def __call__(self, **kwargs: object) -> "_RoutedClient":
        self.kwargs = kwargs
        return self

    def __enter__(self) -> "_RoutedClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def _answer(self, url: str) -> bytes:
        self.urls.append(url)
        answer = next((value for host, value in self.answers.items() if url.startswith(host)), None)
        assert answer is not None, f"nothing here serves {url}"
        if isinstance(answer, Exception):
            raise answer
        return answer

    def head(self, url: str, *, timeout: object = None):
        self.timeouts.append((url, timeout))
        return SimpleNamespace(headers={"content-length": str(len(self._answer(url)))})

    def stream(self, _method: str, url: str, *, timeout: object = None):
        self.timeouts.append((url, timeout))
        return _FakeResponse([self._answer(url)])


class TestDictionaryMirror:
    """raw.githubusercontent.com is the one host the dictionary lives on, and
    from mainland China it times out more often than it answers. jsDelivr
    serves the same file byte for byte, so a publisher that cannot be reached
    at all is read through it instead -- and only then."""

    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, answers: dict[str, bytes | Exception]) -> _RoutedClient:
        import httpx

        client = _RoutedClient(answers)
        monkeypatch.setattr(httpx, "Client", client)
        monkeypatch.delenv("RAVEN_NO_MIRROR", raising=False)
        return client

    @staticmethod
    def _item() -> "resources.Resource":
        return resources.Resource("huqie.txt", resources.CORE_RES, "the word dictionary")

    def test_an_unreachable_publisher_is_read_through_its_mirror(
        self, res: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import httpx

        client = self._install(
            monkeypatch,
            {resources.DICTIONARY_REPO: httpx.ConnectTimeout("timed out"), resources.DICTIONARY_MIRROR: b"words"},
        )
        item = self._item()

        assert resources.fetch_dictionary((item,), force=False) == 1
        assert item.path.read_bytes() == b"words"
        assert client.urls[-1] == f"{resources.DICTIONARY_MIRROR}/huqie.txt"
        said = capsys.readouterr().out
        assert "cdn.jsdelivr.net" in said and "RAVEN_NO_MIRROR" in said, "the switch is announced, with its way out"

    def test_the_mirror_is_the_same_repository_through_jsdelivr(self) -> None:
        assert resources.DICTIONARY_REPO == "https://raw.githubusercontent.com/infiniflow/resource/main/rag"
        assert resources.DICTIONARY_MIRROR == "https://cdn.jsdelivr.net/gh/infiniflow/resource@main/rag"

    def test_a_publisher_that_answers_is_not_swapped_for_the_mirror(
        self, res: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An HTTP error is the publisher's answer. The mirror would only
        repeat it, or worse, serve something the publisher has withdrawn."""
        import httpx

        request = httpx.Request("GET", f"{resources.DICTIONARY_REPO}/huqie.txt")
        refused = httpx.HTTPStatusError("404 Not Found", request=request, response=httpx.Response(404, request=request))
        client = self._install(monkeypatch, {resources.DICTIONARY_REPO: refused, resources.DICTIONARY_MIRROR: b"words"})

        with pytest.raises(httpx.HTTPStatusError):
            resources.fetch_dictionary((self._item(),), force=False)

        assert not any(url.startswith(resources.DICTIONARY_MIRROR) for url in client.urls)

    def test_the_opt_out_keeps_the_publisher_only(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        client = self._install(
            monkeypatch,
            {resources.DICTIONARY_REPO: httpx.ConnectError("refused"), resources.DICTIONARY_MIRROR: b"words"},
        )
        monkeypatch.setenv("RAVEN_NO_MIRROR", "1")

        with pytest.raises(httpx.ConnectError):
            resources.fetch_dictionary((self._item(),), force=False)

        assert not any(url.startswith(resources.DICTIONARY_MIRROR) for url in client.urls)

    def test_a_host_with_a_mirror_behind_it_is_given_up_on_promptly(
        self, res: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dropped SYN is not refused, it is waited on: Linux retries a
        connect for about two minutes, which is how long the mirror would
        otherwise sit unused."""
        client = self._install(monkeypatch, {resources.DICTIONARY_REPO: b"words"})

        resources.fetch_dictionary((self._item(),), force=False)

        assert client.timeouts and all(timeout.connect <= 15 for _url, timeout in client.timeouts)

    def test_a_host_with_nothing_behind_it_waits_as_long_as_it_always_did(
        self, res: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shorter wait only buys time for a mirror. The mirror itself, and
        every download with the fallback turned off, keep the old one."""
        import httpx

        client = self._install(
            monkeypatch,
            {resources.DICTIONARY_REPO: httpx.ConnectTimeout("timed out"), resources.DICTIONARY_MIRROR: b"words"},
        )
        resources.fetch_dictionary((self._item(),), force=False)
        on_the_mirror = [timeout for url, timeout in client.timeouts if url.startswith(resources.DICTIONARY_MIRROR)]
        assert on_the_mirror and all(timeout.connect == 120 and timeout.read == 120 for timeout in on_the_mirror)

        client = self._install(monkeypatch, {resources.DICTIONARY_REPO: b"words"})
        monkeypatch.setenv("RAVEN_NO_MIRROR", "1")
        resources.fetch_dictionary((self._item(),), force=True)
        assert client.timeouts and all(t.connect == 120 and t.read == 120 for _url, t in client.timeouts)

    def test_the_files_after_a_switch_go_straight_to_the_mirror(
        self, res: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each would otherwise wait out the same timeout on the publisher before
        making the same switch."""
        import httpx

        client = self._install(
            monkeypatch,
            {resources.DICTIONARY_REPO: httpx.ConnectTimeout("timed out"), resources.DICTIONARY_MIRROR: b"words"},
        )
        items = (self._item(), resources.Resource("synonym.json", resources.CORE_RES, "the synonym table"))

        assert resources.fetch_dictionary(items, force=False) == 2
        assert [url for url in client.urls if url.startswith(resources.DICTIONARY_REPO)] == [
            f"{resources.DICTIONARY_REPO}/huqie.txt"
        ]
        assert capsys.readouterr().out.count("could not be reached") == 1


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


class TestDeepdocMirror:
    """huggingface.co does not answer from mainland China: its name resolves to
    an address that drops every packet. hf-mirror.com serves the same
    repository, so a Hub that cannot be reached at all is read through it --
    unless HF_ENDPOINT already names a Hub, which is the operator's choice."""

    @staticmethod
    def _install(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        hub_down: bool,
        refusal: Exception | None = None,
    ) -> list[tuple[str, str | None]]:
        import httpx
        import huggingface_hub
        from huggingface_hub.errors import LocalEntryNotFoundError

        cached = tmp_path / "hf-cache"
        cached.mkdir(exist_ok=True)
        asked: list[tuple[str, str | None]] = []

        def unreachable() -> Exception:
            # How hf_hub_download reports a Hub it could not connect to: its
            # own error, raised from the transport's.
            try:
                raise httpx.ConnectTimeout("timed out")
            except httpx.ConnectTimeout as cause:
                try:
                    raise LocalEntryNotFoundError("cannot find the requested files") from cause
                except LocalEntryNotFoundError as error:
                    return error

        def fake_download(
            *, repo_id: str, filename: str, endpoint: str | None = None, token: bool | str | None = None
        ) -> str:
            asked.append((filename, endpoint))
            if endpoint is None and refusal is not None:
                raise refusal
            if endpoint is None and hub_down:
                raise unreachable()
            blob = cached / filename
            blob.write_bytes(b"m" * 4)
            return str(blob)

        def fake_info(_repo: str, files_metadata: bool = False):
            if hub_down:
                raise httpx.ConnectTimeout("timed out")
            return SimpleNamespace(siblings=[SimpleNamespace(rfilename="det.onnx", size=4)])

        class FakeApi:
            def __init__(self, endpoint: str | None = None, token: bool | str | None = None) -> None:
                self.endpoint = endpoint

            def model_info(self, _repo: str, files_metadata: bool = False):
                asked.append(("model_info", self.endpoint))
                return SimpleNamespace(siblings=[SimpleNamespace(rfilename="det.onnx", size=4)])

        monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
        monkeypatch.setattr(huggingface_hub, "model_info", fake_info)
        monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        monkeypatch.delenv("RAVEN_NO_MIRROR", raising=False)
        return asked

    @staticmethod
    def _items() -> "tuple[resources.Resource, ...]":
        return (
            resources.Resource("det.onnx", resources.KNOWLEDGE_RES, "text detection"),
            resources.Resource("rec.onnx", resources.KNOWLEDGE_RES, "text recognition"),
        )

    def test_an_unreachable_hub_is_read_through_its_mirror(
        self, res: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        asked = self._install(monkeypatch, res, hub_down=True)
        items = self._items()

        assert resources.fetch_deepdoc(items, force=False) == 2
        said = capsys.readouterr().out
        assert "hf-mirror.com" in said and "RAVEN_NO_MIRROR" in said, "the switch is announced, with its way out"
        assert all(item.path.is_file() for item in items)
        downloads = [endpoint for name, endpoint in asked if name.endswith(".onnx")]
        assert downloads == [resources.DEEPDOC_MIRROR] * 2, "the index answered for the Hub, so no file waits on it"
        assert ("model_info", resources.DEEPDOC_MIRROR) in asked, "the sizes come from the mirror too"

    def test_a_hub_that_drops_a_download_hands_the_rest_to_the_mirror(
        self, res: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The index can answer and a download still not connect: the file
        that failed is asked of the mirror, and so is every one after it."""
        import huggingface_hub

        asked = self._install(monkeypatch, res, hub_down=True)
        monkeypatch.setattr(
            huggingface_hub,
            "model_info",
            lambda *_a, **_kw: SimpleNamespace(siblings=[SimpleNamespace(rfilename="det.onnx", size=4)]),
        )

        assert resources.fetch_deepdoc(self._items(), force=False) == 2
        assert asked == [
            ("det.onnx", None),
            ("det.onnx", resources.DEEPDOC_MIRROR),
            ("rec.onnx", resources.DEEPDOC_MIRROR),
        ]

    def test_the_mirror_is_hf_mirror(self) -> None:
        assert resources.DEEPDOC_MIRROR == "https://hf-mirror.com"

    def test_a_hub_the_operator_named_is_never_swapped(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from huggingface_hub.errors import LocalEntryNotFoundError

        asked = self._install(monkeypatch, res, hub_down=True)
        monkeypatch.setenv("HF_ENDPOINT", "https://hub.example.internal")

        with pytest.raises(LocalEntryNotFoundError):
            resources.fetch_deepdoc(self._items(), force=False)

        assert all(endpoint is None for _name, endpoint in asked)

    def test_the_opt_out_keeps_the_hub_only(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from huggingface_hub.errors import LocalEntryNotFoundError

        asked = self._install(monkeypatch, res, hub_down=True)
        monkeypatch.setenv("RAVEN_NO_MIRROR", "1")

        with pytest.raises(LocalEntryNotFoundError):
            resources.fetch_deepdoc(self._items(), force=False)

        assert all(endpoint is None for _name, endpoint in asked)

    def test_a_hub_that_refuses_is_not_swapped_for_the_mirror(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refusal is the Hub's answer about the repository, not about the
        network, and the mirror only relays the Hub."""
        refusal = RuntimeError("401 Unauthorized")
        asked = self._install(monkeypatch, res, hub_down=False, refusal=refusal)

        with pytest.raises(RuntimeError, match="401"):
            resources.fetch_deepdoc(self._items(), force=False)

        assert all(endpoint is None for _name, endpoint in asked)

    def test_the_mirror_is_never_handed_the_users_token(
        self, res: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """huggingface_hub sends a logged-in user's token to whatever endpoint
        it is given. The token is for huggingface.co, and these models are
        public, so the mirror is asked without it. The real client, against a
        stand-in mirror that records what arrives."""
        import huggingface_hub
        from huggingface_hub import constants

        seen: list[str | None] = []

        class _Mirror(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the name http.server dispatches on
                seen.append(self.headers.get("authorization"))
                self.send_response(404)
                self.end_headers()

            do_HEAD = do_GET  # noqa: N815 - the name http.server dispatches on

            def log_message(self, *_args: object) -> None:
                return None

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Mirror)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            real_api, real_download = huggingface_hub.HfApi, huggingface_hub.hf_hub_download
            # huggingface.co cannot be reached; everything after that is the real client.
            self._install(monkeypatch, res, hub_down=True)
            monkeypatch.setattr(huggingface_hub, "HfApi", real_api)
            monkeypatch.setattr(huggingface_hub, "hf_hub_download", real_download)
            monkeypatch.setattr(resources, "DEEPDOC_MIRROR", f"http://127.0.0.1:{server.server_address[1]}")
            monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)
            monkeypatch.setenv("HF_TOKEN", "hf_a_token_for_huggingface_co")
            monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
            monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
            monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

            with pytest.raises(Exception):  # noqa: B017 - the stand-in serves nothing; what it was sent is the point
                resources.fetch_deepdoc(self._items()[:1], force=False)
        finally:
            server.shutdown()
            server.server_close()

        assert seen, "the mirror was asked"
        assert seen == [None] * len(seen), "no request to the mirror carried the token"


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


class TestFetchNltk:
    """The corpora the latin half of the tokenizer reads, into the package
    rather than a user's home -- so an install is self-contained and two
    checkouts do not fight over one cache."""

    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, *, answer: bool = True) -> list[str]:
        asked: list[str] = []

        def download(corpus: str, download_dir: str = "", quiet: bool = False, halt_on_error: bool = True) -> bool:
            asked.append(corpus)
            return answer

        import nltk

        monkeypatch.setattr(nltk, "download", download)
        return asked

    def test_every_corpus_is_fetched(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        asked = self._install(monkeypatch)

        written = resources.fetch_nltk(force=False)

        assert written == len(resources.NLTK_CORPORA)
        assert asked == list(resources.NLTK_CORPORA)

    def test_the_order_puts_the_dependency_first(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """nltk gates wordnet behind omw-1.4, so a run that fetched wordnet
        alone still raises the first time the lemmatizer is asked."""
        asked = self._install(monkeypatch)

        resources.fetch_nltk(force=False)

        assert asked.index("omw-1.4") < asked.index("wordnet")

    def test_a_corpus_unpacked_into_a_directory_counts_as_present(
        self, res: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = self._install(monkeypatch)
        (resources.NLTK_DATA / "corpora" / "wordnet").mkdir(parents=True)

        resources.fetch_nltk(force=False)

        assert "wordnet" not in asked

    def test_a_corpus_left_as_its_zip_counts_as_present_too(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """nltk unpacks some and leaves others zipped; checking only for the
        directory re-downloaded wordnet on every run."""
        asked = self._install(monkeypatch)
        (resources.NLTK_DATA / "corpora").mkdir(parents=True)
        (resources.NLTK_DATA / "corpora" / "wordnet.zip").write_bytes(b"x")

        resources.fetch_nltk(force=False)

        assert "wordnet" not in asked

    def test_the_sentence_splitter_is_looked_for_under_tokenizers(
        self, res: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = self._install(monkeypatch)
        (resources.NLTK_DATA / "tokenizers" / "punkt_tab").mkdir(parents=True)

        resources.fetch_nltk(force=False)

        assert "punkt_tab" not in asked

    def test_force_fetches_what_is_already_here(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        asked = self._install(monkeypatch)
        (resources.NLTK_DATA / "corpora" / "wordnet").mkdir(parents=True)

        resources.fetch_nltk(force=True)

        assert "wordnet" in asked

    def test_a_refused_download_is_raised(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`halt_on_error=False` keeps nltk from aborting the batch itself, so
        the return value is what this has to report on."""
        self._install(monkeypatch, answer=False)

        with pytest.raises(RuntimeError):
            resources.fetch_nltk(force=False)

    def test_the_proxy_guard_is_opted_out_of(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """nltk's SSRF guard cannot tell an intentional corporate proxy from a
        redirect it was tricked into following, and this is an explicit fetch
        of named corpora run by an operator installing raven."""
        import os

        monkeypatch.delenv("NLTK_ALLOW_PROXIED_URLOPEN", raising=False)
        self._install(monkeypatch)

        resources.fetch_nltk(force=False)

        assert os.environ["NLTK_ALLOW_PROXIED_URLOPEN"] == "1"

    def test_a_host_that_already_decided_keeps_its_answer(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import os

        monkeypatch.setenv("NLTK_ALLOW_PROXIED_URLOPEN", "0")
        self._install(monkeypatch)

        resources.fetch_nltk(force=False)

        assert os.environ["NLTK_ALLOW_PROXIED_URLOPEN"] == "0"


class TestWarmDictionary:
    """Building the trie cache at install time rather than inside a reader's
    first query, which is six seconds."""

    def test_a_cache_already_built_is_left_alone(
        self, res: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from raven.core import tokenizer as tok

        dictionary = tmp_path / "huqie.txt"
        dictionary.write_text("", encoding="utf-8")
        dictionary.with_suffix(".txt.trie").write_bytes(b"cached")
        monkeypatch.setattr(tok, "dictionary_path", lambda **_kw: dictionary)
        built: list[int] = []
        monkeypatch.setattr(tok, "Tokenizer", lambda *a, **k: built.append(1))

        resources.warm_dictionary()

        assert built == []

    def test_a_missing_cache_is_built(self, res: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from raven.core import tokenizer as tok

        dictionary = tmp_path / "huqie.txt"
        dictionary.write_text("", encoding="utf-8")
        monkeypatch.setattr(tok, "dictionary_path", lambda **_kw: dictionary)
        built: list[int] = []
        monkeypatch.setattr(tok, "Tokenizer", lambda *a, **k: built.append(1))

        resources.warm_dictionary()

        assert built == [1]

    def test_a_failure_to_build_does_not_stop_the_install(
        self, res: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """It costs six seconds later, not correctness."""
        from raven.core import tokenizer as tok

        dictionary = tmp_path / "huqie.txt"
        dictionary.write_text("", encoding="utf-8")
        monkeypatch.setattr(tok, "dictionary_path", lambda **_kw: dictionary)
        monkeypatch.setattr(tok, "Tokenizer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no datrie here")))

        resources.warm_dictionary()


class TestWithoutTheSegmenter:
    """The segmenter is an extra, because datrie compiles from source on every
    platform PyPI publishes for -- so a core install has none of it, and both
    steps that reach it have to be steps to skip rather than errors."""

    def test_the_corpora_step_skips(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real = builtins.__import__

        def refuse(name: str, *args: object, **kw: object):
            if name == "nltk":
                raise ImportError("No module named 'nltk'")
            return real(name, *args, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", refuse)

        assert resources.fetch_nltk(force=False) == 0

    def test_the_index_warm_up_skips(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real = builtins.__import__

        def refuse(name: str, *args: object, **kw: object):
            if name.startswith("raven.core"):
                raise ImportError("No module named 'datrie'")
            return real(name, *args, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", refuse)

        resources.warm_dictionary()

    def test_the_dictionary_is_still_fetched(self, res: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """It is a word list, not code: an install with no segmenter can still
        hold it, and the one that adds the extra later finds it already here."""
        import httpx

        client = _FakeClient([b"words"])
        monkeypatch.setattr(httpx, "Client", lambda **_kw: client)
        item = resources.Resource("huqie.txt", resources.CORE_RES, "the word dictionary")

        assert resources.fetch_dictionary((item,), force=False) == 1
