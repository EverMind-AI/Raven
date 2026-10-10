"""Where the weights are, and which runtime is allowed to run them.

Small and worth pinning. `providers` is the one that carries a decision: an
accelerator is not a free speedup for these graphs, because which provider
runs a given node -- and what it answers -- moves with a version bump, and a
parser whose output changes with the machine it indexed on is a knowledge base
whose chunks depend on where they were made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.knowledge.parser.deepdoc import _onnx


@pytest.fixture(autouse=True)
def _no_cached_sessions():
    """The session cache is module state keyed by path. A fake built here must
    not outlive its case, and a real one loaded elsewhere must not be the
    answer to a case that asked for a fake."""
    _onnx._reset_for_tests()
    yield
    _onnx._reset_for_tests()


@pytest.fixture
def weights(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(_onnx, "RES_DIR", tmp_path)
    return tmp_path


class TestModelPath:
    def test_a_model_is_named_beside_the_others(self, weights: Path) -> None:
        assert _onnx.model_path("layout") == weights / "layout.onnx"

    def test_an_absent_model_is_still_named(self, weights: Path) -> None:
        """Asked whether or not it is there: the caller decides what an absent
        one means."""
        assert not _onnx.model_path("layout").exists()

    def test_requiring_an_absent_model_names_the_command_that_fixes_it(self, weights: Path) -> None:
        """The alternative is onnxruntime's own message, which names a path and
        not the install step that was skipped."""
        with pytest.raises(_onnx.ModelsMissingError, match="raven resources"):
            _onnx.model_path("layout", required=True)

    def test_requiring_a_model_that_is_there_answers_its_path(self, weights: Path) -> None:
        (weights / "layout.onnx").write_bytes(b"weights")

        assert _onnx.model_path("layout", required=True).is_file()


class TestAvailable:
    def test_every_named_model_has_to_be_there(self, weights: Path) -> None:
        (weights / "det.onnx").write_bytes(b"x")

        assert _onnx.available("det")
        assert not _onnx.available("det", "rec")

    def test_asking_for_nothing_asks_for_all_four(self, weights: Path) -> None:
        for name in ("det", "rec", "layout", "tsr"):
            assert not _onnx.available()
            (weights / f"{name}.onnx").write_bytes(b"x")

        assert _onnx.available()


class TestProviders:
    def test_the_cpu_runs_them_unless_an_operator_says_otherwise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RAVEN_ONNX_PROVIDERS", raising=False)

        assert _onnx.providers() == ["CPUExecutionProvider"]

    def test_a_requested_provider_is_tried_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import onnxruntime as ort

        monkeypatch.setattr(ort, "get_available_providers", lambda: ["AcmeProvider", "CPUExecutionProvider"])
        monkeypatch.setenv("RAVEN_ONNX_PROVIDERS", "AcmeProvider")

        assert _onnx.providers() == ["AcmeProvider", "CPUExecutionProvider"]

    def test_the_cpu_is_always_the_last_resort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """So a bad name costs a fallback rather than a failure."""
        import onnxruntime as ort

        monkeypatch.setattr(ort, "get_available_providers", lambda: ["CPUExecutionProvider"])
        monkeypatch.setenv("RAVEN_ONNX_PROVIDERS", "NotInstalledProvider")

        assert _onnx.providers() == ["CPUExecutionProvider"]

    def test_a_provider_the_runtime_does_not_carry_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import onnxruntime as ort

        monkeypatch.setattr(ort, "get_available_providers", lambda: ["AcmeProvider", "CPUExecutionProvider"])
        monkeypatch.setenv("RAVEN_ONNX_PROVIDERS", "GhostProvider,AcmeProvider")

        assert _onnx.providers() == ["AcmeProvider", "CPUExecutionProvider"]

    def test_the_names_are_read_with_their_spacing_forgiven(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import onnxruntime as ort

        monkeypatch.setattr(ort, "get_available_providers", lambda: ["AcmeProvider", "CPUExecutionProvider"])
        monkeypatch.setenv("RAVEN_ONNX_PROVIDERS", "  AcmeProvider , ")

        assert _onnx.providers() == ["AcmeProvider", "CPUExecutionProvider"]

    def test_asking_for_the_cpu_does_not_name_it_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import onnxruntime as ort

        monkeypatch.setattr(ort, "get_available_providers", lambda: ["CPUExecutionProvider"])
        monkeypatch.setenv("RAVEN_ONNX_PROVIDERS", "CPUExecutionProvider")

        assert _onnx.providers() == ["CPUExecutionProvider"]


class TestSession:
    def test_a_missing_model_is_refused_before_the_runtime_is_asked(self, weights: Path) -> None:
        _onnx._reset_for_tests()

        with pytest.raises(_onnx.ModelsMissingError):
            _onnx.session("layout")

    def test_a_session_is_built_once_and_reused(self, weights: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A gateway that rebuilt the graph per page would pay the load for
        every page of every document."""
        import onnxruntime as ort

        (weights / "layout.onnx").write_bytes(b"weights")
        built: list[str] = []

        class _Session:
            def __init__(self, path, options, providers) -> None:
                built.append(path)

            def get_providers(self):
                return ["CPUExecutionProvider"]

        monkeypatch.setattr(ort, "InferenceSession", _Session)
        _onnx._reset_for_tests()

        first = _onnx.session("layout")
        second = _onnx.session("layout")

        assert first is second
        assert len(built) == 1

    def test_the_memory_arena_is_turned_off(self, weights: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """It keeps every block it ever allocated; a gateway holding a page's
        worth of scratch for the rest of its life is a resident-set graph
        nobody can explain."""
        import onnxruntime as ort

        (weights / "layout.onnx").write_bytes(b"weights")
        seen: list[object] = []

        class _Session:
            def __init__(self, path, options, providers) -> None:
                seen.append(options)

            def get_providers(self):
                return ["CPUExecutionProvider"]

        monkeypatch.setattr(ort, "InferenceSession", _Session)
        _onnx._reset_for_tests()

        _onnx.session("layout")

        assert seen[0].enable_cpu_mem_arena is False
