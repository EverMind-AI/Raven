"""Loading the deepdoc graphs, and saying so when they are not installed.

One session per model per process. Building one costs the read of a file that
runs to 76 MB and onnxruntime's own graph preparation, and every page of every
document would otherwise pay it again.

Adapted from the loader in RAGFlow's ``deepdoc/vision/ocr.py`` (Apache-2.0; see
NOTICES.md). What is not carried over: its CUDA branch pip-installs torch at
import to ask whether a GPU exists, and its multi-device path holds one session
per device behind a semaphore. Raven indexes one document at a time inside the
gateway, so a second session would contend for the same cores; the provider
list onnxruntime already publishes answers the GPU question without installing
anything.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from loguru import logger

#: Where `scripts/fetch_resources.py` puts the weights. Beside the package that
#: reads them, like the tokenizer's dictionary, so an install is one tree.
RES_DIR = Path(__file__).resolve().parent.parent.parent / "res"

#: Threads per session. Two, not "as many as there are cores": an indexing run
#: shares this process with a gateway serving turns, and onnxruntime's default
#: is to take the machine.
_INTRA_OP_THREADS = int(os.environ.get("RAVEN_ONNX_INTRA_OP_THREADS", "2"))
_INTER_OP_THREADS = int(os.environ.get("RAVEN_ONNX_INTER_OP_THREADS", "2"))

_sessions: "dict[str, Any]" = {}
_lock = threading.Lock()


class ModelsMissingError(RuntimeError):
    """The deepdoc weights are not installed, so nothing here can run."""


def model_path(name: str, *, required: bool = False) -> Path:
    """Where a model lives, whether or not it has been fetched.

    ``required`` turns its absence into a raise carrying the command that fixes
    it. The alternative is onnxruntime's own message, which names a path and
    not the install step that was skipped.
    """
    path = RES_DIR / f"{name}.onnx"
    if required and not path.is_file():
        raise ModelsMissingError(
            f"the deepdoc model {name!r} is not installed at {path}; run `make fetch-resources` "
            "(or `uv run python scripts/fetch_resources.py`) to download it"
        )
    return path


def available(*names: str) -> bool:
    """Whether every named model is on disk. Asked before one is needed."""
    return all(model_path(name).is_file() for name in names or ("det", "rec", "layout", "tsr"))


def providers() -> "list[str]":
    """The execution providers to try, best first.

    CPU alone unless an operator asks for more. An accelerator is not a free
    speedup here: CoreML takes this layout graph as seven partitions rather
    than one, and both it and the CUDA provider implement a different subset of
    operators per release, so which one runs a given node -- and what it
    answers -- moves with a transitive version bump. A parser whose output
    changes with the machine it indexed on is a knowledge base whose chunks
    depend on where they were made.

    ``RAVEN_ONNX_PROVIDERS`` is the opt-in, naming providers in order. Anything
    the installed runtime does not carry is dropped, and CPU is appended
    whatever happens, so a bad name costs a fallback rather than a failure.
    """
    import onnxruntime as ort

    installed = set(ort.get_available_providers())
    asked = [name.strip() for name in os.environ.get("RAVEN_ONNX_PROVIDERS", "").split(",") if name.strip()]
    wanted = [name for name in asked if name in installed and name != "CPUExecutionProvider"]
    if asked and not wanted:
        logger.warning("deepdoc: none of the requested providers {} are installed; using the CPU", asked)
    return [*wanted, "CPUExecutionProvider"]


def session(name: str) -> Any:
    """The session for one model, built once and reused.

    Raises:
        `ModelsMissingError`: when the weights were never fetched.
    """
    import onnxruntime as ort

    path = model_path(name, required=True)
    key = str(path)
    with _lock:
        held = _sessions.get(key)
        if held is not None:
            return held

        options = ort.SessionOptions()
        # The arena keeps every block it ever allocated. A gateway holding a
        # page's worth of scratch for the rest of its life is the difference
        # between indexing a report and a resident-set graph nobody can explain.
        options.enable_cpu_mem_arena = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.intra_op_num_threads = _INTRA_OP_THREADS
        options.inter_op_num_threads = _INTER_OP_THREADS

        built = ort.InferenceSession(key, options, providers=providers())
        logger.debug("deepdoc: loaded {} on {}", name, built.get_providers()[0])
        _sessions[key] = built
        return built


def _reset_for_tests() -> None:
    """Drop the cached sessions, so a test can load against another path."""
    with _lock:
        _sessions.clear()


__all__ = ["RES_DIR", "ModelsMissingError", "available", "model_path", "providers", "session"]
