"""Shared fixtures for everos memory integration tests.

These tests drive a **real** everos runtime (real LLM + embedding +
sqlite/lancedb), gated behind the ``real_llm`` marker; they skip when the
runtime isn't configured. Configure via ``~/.everos/config.toml``, a
repo-root ``.env``, or ``EVEROS_*`` env vars, then::

    uv run pytest tests/integration -m real_llm

Harness design (mirrors everos's own ``tests/e2e/conftest.py``):

- **Full lifespan init.** everos's schema (sqlite tables, lancedb
  indexes) and its OME extraction engine are created by the FastAPI app
  lifespan, not on first service call. The :func:`everos_env` fixture
  drives ``create_app().router.lifespan_context`` so embedded service
  calls (``memorize`` / ``search``) work in-process.
- **Per-test isolation.** ``EVEROS_MEMORY__ROOT`` points at a per-test
  ``tmp_path`` and everos's module-level singletons (writers, pipelines,
  OME engine, llm client, strategy writers) are nulled so each test
  rebuilds against its own store. The fixture is therefore function
  scoped (real init per test — slow + billable, but correct).
- **Async extraction.** ``memorize`` enqueues work the OME engine runs in
  the background; tests must ``await pipeline_drain()`` before searching
  or the just-written memories won't be visible yet.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

_DATA_DIR = Path(__file__).parent / "data"
_CORPUS_PATH = _DATA_DIR / "everos_skill_corpus.json"

# Load a gitignored repo-root ``.env`` so secrets placed there reach both
# everos (via pydantic-settings) and the resolved-settings gate below.
# Real env vars still win — ``load_dotenv`` does not override.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:  # python-dotenv absent — rely on real env vars only
    pass


# everos module-level singletons that capture MemoryRoot / engine at first
# use and survive across tests; null them so each test rebuilds against
# its own ``EVEROS_MEMORY__ROOT``. Mirrors everos tests/e2e/conftest.py.
_MEMORIZE_SINGLETONS: tuple[str, ...] = (
    "_episode_writer",
    "_prompt_loader",
    "_user_pipeline",
    "_agent_pipeline",
    "_ome_engine",
)
_STRATEGY_SINGLETONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("everos.memory.strategies.extract_atomic_facts", ("_writer",)),
    ("everos.memory.strategies.extract_foresight", ("_writer",)),
    ("everos.memory.strategies.extract_user_profile", ("_writer", "_reader")),
    ("everos.memory.strategies.extract_agent_case", ("_writer",)),
    ("everos.memory.strategies.extract_agent_skill", ("_writer",)),
)


def _reset_everos_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = importlib.import_module("everos.service.memorize")
    for attr in _MEMORIZE_SINGLETONS:
        monkeypatch.setattr(svc, attr, None, raising=False)
    client_mod = importlib.import_module("everos.component.llm.client")
    monkeypatch.setattr(client_mod, "_llm_client", None, raising=False)
    for mod_name, attrs in _STRATEGY_SINGLETONS:
        mod = importlib.import_module(mod_name)
        for attr in attrs:
            monkeypatch.setattr(mod, attr, None, raising=False)


def _missing_runtime_config(settings: Any) -> list[str]:
    """Which everos LLM/embedding fields are unset across *all* config
    sources (``~/.everos/config.toml`` + ``.env`` + ``EVEROS_*`` env).

    Checking everos's resolved ``Settings`` rather than raw env vars
    means a user who configures models/keys in ``config.toml`` (not env)
    still un-skips the suite. Empty list == runtime ready.
    """
    missing: list[str] = []
    if settings.llm.api_key is None:
        missing.append("llm.api_key")
    if not settings.embedding.model:
        missing.append("embedding.model")
    if settings.embedding.api_key is None:
        missing.append("embedding.api_key")
    return missing


@pytest_asyncio.fixture
async def everos_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Isolated everos store + full lifespan, in the test's event loop.

    Skips when everos isn't installed or its LLM / embedding runtime
    isn't configured (in toml, .env, or env).
    """
    pytest.importorskip("everos")
    # Snapshot the real root BEFORE redirecting, so we can copy config
    # files that carry LLM / embedding credentials into the isolated dir.
    import shutil

    from everos.config.settings import load_settings, resolve_root

    load_settings.cache_clear()
    real_root = resolve_root()

    # Redirect data root to an isolated tmp dir so each test gets a
    # fresh sqlite + lancedb.
    monkeypatch.setenv("EVEROS_ROOT", str(tmp_path))
    monkeypatch.setenv("EVEROS_MEMORY__ROOT", str(tmp_path))

    for name in ("everos.toml", "ome.toml"):
        src = real_root / name
        if src.is_file():
            shutil.copy2(src, tmp_path / name)

    # mode=agent runs both user-memory and agent-memory pipelines.
    monkeypatch.setenv("EVEROS_MEMORIZE__MODE", "agent")
    # Tighten the boundary so the backend's accumulate-only store() path
    # (no is_final flush) still crosses a boundary by volume in L3.
    monkeypatch.setenv("EVEROS_BOUNDARY_DETECTION__HARD_MSG_LIMIT", "6")
    load_settings.cache_clear()

    missing = _missing_runtime_config(load_settings())
    if missing:
        load_settings.cache_clear()
        pytest.skip(
            "everos runtime not configured (missing: "
            + ", ".join(missing)
            + "); set them in ~/.everos/config.toml, a repo-root .env, "
            "or EVEROS_* env vars",
        )

    _reset_everos_singletons(monkeypatch)

    # Force a fresh lifespan entry by clearing the refcounted singleton
    # in Raven's backend module. Without this, a previous test's
    # _embedded_lifespan_cm survives and _acquire skips re-entry — but
    # the everos singletons above were already nulled, so the old
    # lifespan's OME engine is gone.
    import raven.plugin.memory.everos.backend as _be_mod

    monkeypatch.setattr(_be_mod, "_embedded_lifespan_cm", None)
    monkeypatch.setattr(_be_mod, "_embedded_lifespan_refs", 0)

    # Bring up the in-process everos runtime via the production path:
    # EverosBackend.start() drives the (refcounted, process-shared) everos
    # lifespan that creates schema + the OME engine. L2 tests then call
    # everos.service directly against this runtime; L3 backends start()
    # again and just share the same lifespan (refcount > 1).
    from raven.plugin import PluginContext, ServiceLocator
    from raven.plugin.memory.everos.backend import EverosBackend

    be = EverosBackend(
        PluginContext(
            config={"mode": "embedded"},
            services=ServiceLocator(workspace=tmp_path),
        )
    )
    await be.start()
    try:
        yield SimpleNamespace(root=tmp_path, backend=be)
    finally:
        await be.stop()
        load_settings.cache_clear()


@pytest.fixture
def pipeline_drain() -> Any:
    """Return an async waiter: blocks until the OME engine is idle AND
    the md_change_state cascade queue is fully drained.

    everos extraction is async/background — ``memorize`` returns before
    the strategies (and their md -> lancedb propagation) finish. Call
    ``await pipeline_drain()`` after memorize/store and before search.
    """

    async def _wait(*, deadline_seconds: float = 180.0, interval: float = 0.5) -> None:
        from everos.infra.persistence.sqlite import md_change_state_repo
        from everos.service.memorize import _get_engine

        engine = _get_engine()
        async with asyncio.timeout(deadline_seconds):
            while True:
                if await engine.wait_idle(timeout=0.5):
                    summary = await md_change_state_repo.queue_summary()
                    if summary.pending == 0:
                        return
                await asyncio.sleep(interval)

    return _wait


@pytest.fixture
def ids() -> Any:
    """Fresh identifiers per test for data isolation.

    ``user_id`` / ``agent_id`` are the bare everos owner identities: what
    ``sender_id`` is stamped with on store, what search filters on, and
    what the host passes to ``backend.recall(user_id=...)`` /
    ``recall(agent_id=...)``.
    """
    tag = uuid.uuid4().hex[:8]
    user_id = f"u-{tag}"
    agent_id = f"a-{tag}"
    return SimpleNamespace(
        user_id=user_id,
        agent_id=agent_id,
        session=f"sess-{tag}",
    )


@pytest.fixture(scope="session")
def corpus() -> dict[str, Any]:
    """The seeded conversation corpus (user facts + skill demonstrations)."""
    return json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


def as_everos_payload(
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    user_id: str,
    agent_id: str,
    base_ts_ms: int | None = None,
) -> dict[str, Any]:
    """Convert AgentLoop-shape messages into the everos memorize payload.

    Stamps ``sender_id`` by role to match everos's owner model (the L2
    direct-service path mirrors what the backend does on L3):
    ``user`` -> ``user_id``; ``assistant`` / ``tool`` -> ``agent_id``.
    So a later search with ``user_id`` / ``agent_id`` finds these rows.
    Passes through ``tool_calls`` / ``tool_call_id`` when present (agent
    case/skill extraction needs real tool round-trips).
    """
    base = base_ts_ms if base_ts_ms is not None else int(time.time() * 1000)
    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        role = m["role"]
        entry: dict[str, Any] = {
            "sender_id": user_id if role == "user" else agent_id,
            "role": role,
            "timestamp": base + i * 1000,
            "content": m["content"],
        }
        if m.get("tool_calls") is not None:
            entry["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id") is not None:
            entry["tool_call_id"] = m["tool_call_id"]
        out.append(entry)
    return {"session_id": session_id, "messages": out}
