# -*- coding: utf-8 -*-
"""Factory that turns stored sub-agent configs into toolkit tools."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from .._logging import logger
from ..credential import CredentialFactory, OpenAICompatibleCredential
from ..tool import BackendBase, ToolBase
from ._base import CliSubAgentConfig, OpenAISubAgentConfig
from ._dag import SubAgentDagTool
from ._dag._projection import fold_dag_run_entry
from ._factory import SubAgentFactory
from ._instance_registry import SessionInstanceRegistry
from ._openai_tool import OpenAISubAgentTool
from ._tool import CliSubAgentTool


async def _resolve_workspace(
    storage: Any,
    workspace_manager: Any,
    user_id: str,
    agent_id: str,
    session_id: str,
) -> tuple[BackendBase | None, str | None]:
    """Best-effort resolution of the session's workspace backend + workdir.

    Returns ``(None, None)`` on any failure so the tool falls back to a
    :class:`LocalBackend` and the sub-agent's own ``cwd`` (or the process
    cwd). Honors the session's ``work_dir`` override when present, so
    sub-agents run in the same directory as the session's ``Bash`` tool.

    Args:
        storage (`Any`): The app storage backend.
        workspace_manager (`Any`): The workspace manager.
        user_id (`str`): The owner user id.
        agent_id (`str`): The agent id.
        session_id (`str`): The session id.

    Returns:
        `tuple[BackendBase | None, str | None]`:
            The resolved backend and workspace workdir, or ``(None, None)``.
    """
    try:
        session = await storage.get_session(user_id, agent_id, session_id)
        workspace_id = session.config.workspace_id if session else None
        work_dir = session.config.work_dir if session else None
        workspace = await workspace_manager.get_workspace(
            user_id,
            agent_id,
            session_id,
            workspace_id,
            workdir=work_dir,
        )
        return workspace.get_backend(), workspace.workdir
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return None, None


def build_dag_progress_publisher(
    message_bus: Any,
    session_id: str,
) -> Callable[[str, dict], Awaitable[None]]:
    """Build a DAG progress publisher: durable upsert + live publish.

    Accumulates a self-contained per-run entry in-closure and writes it
    to the durable ``SessionProjection`` (kind ``"dag_run"``) on every
    event, then publishes the live ``CustomEvent``. ``dag_run_started``
    is augmented with ``created_at`` so the frontend can order runs.

    Args:
        message_bus (`MessageBus`):
            The app message bus (durable hash + live pub/sub).
        session_id (`str`):
            The session the events belong to.

    Returns:
        `Callable[[str, dict], Awaitable[None]]`:
            An async ``(name, value) -> None`` publisher.
    """
    runs: dict[str, dict] = {}

    async def _publish(name: str, value: dict) -> None:
        # Lazy import: subagent must not import app at module top.
        from ..app._service._session_projection import SessionProjection

        projection = SessionProjection(message_bus)
        run_id = value.get("run_id")
        published = value
        if run_id is not None:
            folded = fold_dag_run_entry(
                runs.get(run_id),
                name,
                value,
                datetime.now().isoformat(),
            )
            if folded is not None:
                runs[run_id] = folded
                await projection.upsert(
                    session_id,
                    "dag_run",
                    run_id,
                    folded,
                )
                if name == "dag_run_started":
                    published = {**value, "created_at": folded["created_at"]}
        await projection.publish(session_id, name, published)

    return _publish


def build_instance_progress_publisher(
    message_bus: Any,
    session_id: str,
) -> Callable[[str, dict], Awaitable[None]]:
    """Build a sub-agent-instance status publisher.

    Upserts the latest payload for the handle into the durable
    ``SessionProjection`` (kind ``"subagent_instance"``, entry_id =
    ``handle``) and publishes the live ``CustomEvent``.

    Args:
        message_bus (`MessageBus`):
            The app message bus.
        session_id (`str`):
            The session the events belong to.

    Returns:
        `Callable[[str, dict], Awaitable[None]]`:
            An async ``(name, value) -> None`` publisher.
    """

    async def _publish(name: str, value: dict) -> None:
        from ..app._service._session_projection import SessionProjection

        projection = SessionProjection(message_bus)
        handle = value.get("handle")
        if handle:
            await projection.upsert(
                session_id,
                "subagent_instance",
                handle,
                value,
            )
        await projection.publish(session_id, name, value)

    return _publish


def make_subagent_tool_factory(
    storage: Any,
    workspace_manager: Any,
    message_bus: Any = None,
) -> Callable[[str, str, str], Awaitable[list[ToolBase]]]:
    """Build an ``extra_agent_tools`` factory backed by stored configs.

    The returned async factory reads the user's sub-agent configs and
    returns one :class:`CliSubAgentTool` or :class:`OpenAISubAgentTool`
    per valid config, binding the session's workspace backend when
    resolvable.

    Args:
        storage (`StorageBase`):
            The app storage backend (used to read sub-agent configs).
        workspace_manager (`WorkspaceManagerBase`):
            The workspace manager (used to resolve the session backend).
        message_bus (`Any`, optional):
            The app-global message bus. When provided, wires live DAG
            progress events onto the session's SSE stream.

    Returns:
        `Callable[[str, str, str], Awaitable[list[ToolBase]]]`:
            An ``AgentToolFactory`` suitable for
            ``create_app(extra_agent_tools=...)``.
    """

    async def _factory(
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> list[ToolBase]:
        """Build sub-agent tools for one chat turn."""
        records = await storage.list_subagents(user_id)
        backend, session_workdir = await _resolve_workspace(
            storage,
            workspace_manager,
            user_id,
            agent_id,
            session_id,
        )
        tools: list[ToolBase] = []
        instance_publisher = (
            build_instance_progress_publisher(message_bus, session_id)
            if message_bus is not None
            else None
        )
        # Leader-only: always offer file delivery to the user. Imported
        # lazily to avoid an agentscope.subagent -> agentscope.app cycle.
        from ..app._tool import DeliverFiles

        tools.append(
            DeliverFiles(
                storage=storage,
                workspace_manager=workspace_manager,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            ),
        )
        for record in records:
            try:
                config = SubAgentFactory.from_dict(record.data)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "Skipping malformed sub-agent %s: %s",
                    record.id,
                    exc,
                )
                continue
            if isinstance(config, CliSubAgentConfig):
                tools.append(
                    CliSubAgentTool(
                        name=config.name,
                        description=config.description,
                        command=config.command,
                        resume_command=config.resume_command,
                        id_source=config.id_source,
                        session_id_pattern=config.session_id_pattern,
                        output_pattern=config.output_pattern,
                        transcript_format=config.transcript_format,
                        cwd=config.cwd or session_workdir,
                        env=config.env,
                        timeout=config.timeout,
                        backend=backend,
                        registry=SessionInstanceRegistry(
                            storage,
                            session_id,
                        ),
                        progress_publisher=instance_publisher,
                    ),
                )
            elif isinstance(config, OpenAISubAgentConfig):
                cred_rec = await storage.get_credential(
                    user_id,
                    config.credential_id,
                )
                if cred_rec is None:
                    logger.warning(
                        "Skipping openai sub-agent %s: credential %s "
                        "not found",
                        record.id,
                        config.credential_id,
                    )
                    continue
                try:
                    cred = CredentialFactory.from_dict(cred_rec.data)
                except (ValueError, TypeError) as exc:
                    logger.warning(
                        "Skipping openai sub-agent %s: invalid credential "
                        "%s (%s)",
                        record.id,
                        config.credential_id,
                        type(exc).__name__,
                    )
                    continue
                if not isinstance(cred, OpenAICompatibleCredential):
                    logger.warning(
                        "Skipping openai sub-agent %s: credential %s is "
                        "not OpenAI-compatible",
                        record.id,
                        config.credential_id,
                    )
                    continue
                tools.append(
                    OpenAISubAgentTool(
                        name=config.name,
                        description=config.description,
                        model=config.model,
                        base_url=cred.base_url,
                        api_key=cred.api_key.get_secret_value(),
                        organization=cred.organization,
                        stateful=config.stateful,
                        system_prompt=config.system_prompt,
                        temperature=config.temperature,
                        max_tokens=config.max_tokens,
                        timeout=config.timeout,
                        backend=backend,
                        cwd=session_workdir,
                        registry=SessionInstanceRegistry(
                            storage,
                            session_id,
                        ),
                        progress_publisher=instance_publisher,
                    ),
                )
            else:
                continue
        subagent_tools = {t.name: t for t in tools}
        if subagent_tools and backend is not None and session_workdir:
            dag_publisher = (
                build_dag_progress_publisher(message_bus, session_id)
                if message_bus is not None
                else None
            )
            tools.append(
                SubAgentDagTool(
                    subagents=subagent_tools,
                    backend=backend,
                    workdir=session_workdir,
                    progress_publisher=dag_publisher,
                ),
            )
        return tools

    return _factory
