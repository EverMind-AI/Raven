# -*- coding: utf-8 -*-
"""The OpenAI-compatible HTTP sub-agent tool."""

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, AsyncGenerator, List

from .._logging import logger
from ..message import TextBlock, ToolResultState
from ..permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from ..tool import BackendBase, LocalBackend, ToolBase, ToolChunk, ToolMiddlewareBase
from ._instance_progress import emit_instance_status

_MAX_OUTPUT_CHARS = 128000

# (url, headers, json_body, timeout) -> (status_code, parsed_json)
HttpPost = Callable[[str, dict, dict, float], Awaitable[tuple[int, dict]]]


async def _default_post(
    url: str,
    headers: dict,
    json_body: dict,
    timeout: float,
) -> tuple[int, dict]:
    """POST ``json_body`` to ``url``; return ``(status, parsed_json)``.

    Args:
        url (`str`):
            The endpoint URL.
        headers (`dict`):
            Request headers.
        json_body (`dict`):
            The JSON request body.
        timeout (`float`):
            Request timeout in seconds.

    Returns:
        `tuple[int, dict]`:
            The HTTP status code and parsed JSON body. A synthetic
            ``{"error": {"message": ...}}`` envelope is returned when the
            response body is not JSON.
    """
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        resp = await client.post(url, headers=headers, json=json_body)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - non-JSON body
            data = {"error": {"message": resp.text}}
        return resp.status_code, data


class OpenAISubAgentTool(ToolBase):
    """Delegate a task to a sub-agent behind an OpenAI-compatible API.

    Instead of running a CLI (see :class:`CliSubAgentTool`), this tool
    POSTs an OpenAI Chat Completions request to a configured endpoint and
    returns the assistant's reply. When stateful, the per-instance
    ``messages`` history is persisted under the session workdir and
    replayed on resume (the endpoint itself is stateless). The final
    reply is returned; ``search_results`` (citations) and ``usage`` are
    attached as tool-result metadata.
    """

    is_read_only: bool = False
    is_concurrency_safe: bool = False
    offload_hint_label: str = "subagent_response"
    """Hint label shown when a backgrounded run delivers its result."""
    offload_noun: str = "Sub-Agent"
    """Human-readable noun used in offload notifications."""

    def __init__(
        self,
        name: str,
        description: str,
        model: str,
        base_url: str,
        api_key: str,
        organization: str | None = None,
        stateful: bool = True,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int = 1200,
        backend: BackendBase | None = None,
        cwd: str | None = None,
        registry: Any = None,
        post: HttpPost | None = None,
        middlewares: List[ToolMiddlewareBase] | None = None,
        progress_publisher: (
            Callable[[str, dict], Awaitable[None]] | None
        ) = None,
    ) -> None:
        """Initialize the OpenAI-API sub-agent tool.

        Args:
            name (`str`):
                The tool name presented to the agent.
            description (`str`):
                The tool description presented to the agent.
            model (`str`):
                The chat completions model id to request.
            base_url (`str`):
                The OpenAI-compatible endpoint base URL (e.g. ending
                in ``/v1``).
            api_key (`str`):
                The plaintext API key (resolved per turn from the
                credential store).
            organization (`str | None`, optional):
                Optional organization id sent as ``OpenAI-Organization``.
            stateful (`bool`, defaults to `True`):
                When true (and a ``registry`` is set), instances persist
                and replay their message history on resume.
            system_prompt (`str | None`, optional):
                Optional system message prepended to the conversation.
            temperature (`float | None`, optional):
                Optional sampling temperature.
            max_tokens (`int | None`, optional):
                Optional maximum completion tokens.
            timeout (`int`, defaults to `1200`):
                Request timeout in seconds.
            backend (`BackendBase | None`, optional):
                Backend used for prompt/output/history file I/O. Defaults
                to :class:`LocalBackend`.
            cwd (`str | None`, optional):
                Directory for the prompt/history files.
            registry (`Any`, optional):
                A :class:`SessionInstanceRegistry`-like object. Required
                for the tool to be stateful.
            post (`HttpPost | None`, optional):
                Injectable async HTTP POST (for tests). Defaults to a
                real ``httpx`` call.
            middlewares (`List[ToolMiddlewareBase] | None`, optional):
                Tool middlewares wrapping execution.
            progress_publisher
                (`Callable[[str, dict], Awaitable[None]] | None`,
                optional):
                Async callback publishing a live + durable instance
                status event; ``None`` disables emission.
        """
        super().__init__(middlewares=middlewares)
        self.name = name
        self.description = description
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._organization = organization
        self._stateful = stateful
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._backend = backend or LocalBackend()
        self._cwd = cwd
        self._registry = registry
        self._post = post or _default_post
        self._progress_publisher = progress_publisher
        self._transport = "openai"
        self.input_schema = self._build_input_schema()

    @property
    def is_stateful(self) -> bool:
        """Whether this tool manages resumable instances."""
        return self._stateful and self._registry is not None

    async def _emit(
        self,
        status: str,
        handle: str | None,
        agent_id: str | None,
        action: str | None,
    ) -> None:
        """Emit an instance status transition (no-op when stateless)."""
        await emit_instance_status(
            self._progress_publisher,
            handle=handle,
            agent_id=agent_id,
            prototype=self.name,
            transport=self._transport,
            status=status,
            action=action,
        )

    def _build_input_schema(self) -> dict:
        """Build the tool input schema.

        Returns:
            `dict`:
                A ``{prompt}``-only schema when stateless; a
                ``{prompt, instance}`` schema when stateful.
        """
        properties: dict = {
            "prompt": {
                "type": "string",
                "description": "The task to delegate to the sub-agent.",
            },
        }
        required = ["prompt"]
        if self.is_stateful:
            properties["instance"] = {
                "type": "string",
                "description": (
                    "A stable handle for this sub-agent instance. Choose a "
                    "short, semantic name that reflects the instance's "
                    "role (e.g. `researcher`, `db-migrator`), not a random "
                    "string. Reuse the same handle to continue the same "
                    "conversation (context is preserved); use a new handle "
                    "to start a fresh instance."
                ),
            }
            required.append("instance")
        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    async def _write_temp_prompt(self, prompt: str) -> str:
        """Write ``prompt`` to a temp file under ``cwd`` and return its path.

        Args:
            prompt (`str`):
                The prompt text to persist.

        Returns:
            `str`:
                The written prompt file path (read back to fill the
                user message).
        """
        base = self._cwd or "."
        path = self._backend.join_path(
            base,
            f".ravenx_prompt_{uuid.uuid4().hex}.md",
        )
        await self._backend.write_file(path, prompt.encode("utf-8"))
        return path

    def _history_path(self, agent_id: str) -> str:
        """Return the per-instance history file path.

        Args:
            agent_id (`str`):
                The provisioned conversation id.

        Returns:
            `str`:
                The JSON history file path under ``cwd``.
        """
        base = self._cwd or "."
        return self._backend.join_path(
            base,
            f".ravenx_openai_{agent_id}.json",
        )

    def _seed_history(self) -> list[dict]:
        """Return a fresh message list seeded with the system prompt.

        Returns:
            `list[dict]`:
                ``[{"role": "system", ...}]`` when a system prompt is
                configured, else ``[]``.
        """
        if self._system_prompt:
            return [{"role": "system", "content": self._system_prompt}]
        return []

    async def call(  # type: ignore[override]
        self,
        prompt: str,
        instance: str | None = None,
        prompt_file: str | None = None,
        output_file: str | None = None,
    ) -> AsyncGenerator[ToolChunk, None]:
        """Run the sub-agent over HTTP and yield its result.

        Args:
            prompt (`str`):
                The task to delegate.
            instance (`str | None`, optional):
                The instance handle (stateful only). An unknown/absent
                handle creates; a known handle resumes.
            prompt_file (`str | None`, optional):
                Path to a file holding the prompt. When ``None``,
                ``prompt`` is written to a temp file; the file's contents
                become the user message.
            output_file (`str | None`, optional):
                When set and the call succeeds, the full reply is written
                here (untruncated).

        Yields:
            `ToolChunk`:
                A single terminal chunk with the sub-agent's reply.
        """
        handle: str | None = None
        action: str | None = None
        agent_id: str | None = None
        created = False

        if self.is_stateful:
            handle = instance or uuid.uuid4().hex
            try:
                existing = await self._registry.lookup(handle)
            except Exception as exc:  # noqa: BLE001
                await self._emit("failed", handle, None, None)
                yield ToolChunk(
                    content=[
                        TextBlock(
                            text=(
                                f"Sub-Agent instance lookup failed: {exc}. "
                                "Not starting a new session (would orphan an "
                                "existing one); retry."
                            ),
                        ),
                    ],
                    state=ToolResultState.ERROR,
                    is_last=True,
                )
                return
            if existing is not None:
                agent_id = existing
                action = "resume"
            else:
                created = True
                action = "create"
                agent_id = str(uuid.uuid4())
            await self._emit("running", handle, agent_id, action)
        else:
            agent_id = str(uuid.uuid4())

        # Materialize + read the prompt (single source of truth + audit).
        try:
            if prompt_file is None:
                prompt_file = await self._write_temp_prompt(prompt)
            prompt_text = (await self._backend.read_file(prompt_file)).decode(
                "utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            await self._emit("failed", handle, agent_id, action)
            yield ToolChunk(
                content=[
                    TextBlock(text=f"Sub-Agent prompt file error: {exc}"),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        # Build the message history.
        if self.is_stateful and action == "resume":
            assert agent_id is not None
            try:
                raw = await self._backend.read_file(
                    self._history_path(agent_id),
                )
                history = json.loads(raw.decode("utf-8"))
                if not isinstance(history, list):
                    raise ValueError("history is not a list")
            except Exception as exc:  # noqa: BLE001 - missing/corrupt
                logger.warning(
                    "Sub-Agent instance %s: could not read history "
                    "(%s); starting a fresh context.",
                    agent_id,
                    type(exc).__name__,
                )
                history = self._seed_history()
        else:
            history = self._seed_history()
        history.append({"role": "user", "content": prompt_text})

        # Build the request.
        url = self._base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._organization:
            headers["OpenAI-Organization"] = self._organization
        body: dict = {
            "model": self._model,
            # Snapshot the list: `history` is mutated in place below
            # (the assistant reply is appended for persistence) after
            # this request is sent, and callers of `post` (including
            # the offloader/tests) may retain a reference to `body`.
            "messages": list(history),
            "stream": False,
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens

        try:
            status, data = await self._post(
                url,
                headers,
                body,
                float(self._timeout),
            )
        except Exception as exc:  # noqa: BLE001
            await self._emit("failed", handle, agent_id, action)
            yield ToolChunk(
                content=[TextBlock(text=f"Sub-Agent HTTP error: {exc}")],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        if status < 200 or status >= 300:
            err = data.get("error") if isinstance(data, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            message = (
                err.get("message") if isinstance(err, dict) else str(data)
            )
            await self._emit("failed", handle, agent_id, action)
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"Sub-Agent HTTP error ({status}/{code}): "
                            f"{message}"
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        # Parse the reply.
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            await self._emit("failed", handle, agent_id, action)
            yield ToolChunk(
                content=[
                    TextBlock(text=f"Sub-Agent malformed response: {exc}"),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        if finish_reason in ("error", "cancelled") or not content:
            await self._emit("failed", handle, agent_id, action)
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "Sub-Agent did not complete "
                            f"(finish_reason={finish_reason})."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        # Success. Persist history + commit a successful create.
        if self.is_stateful:
            assert agent_id is not None
            history.append({"role": "assistant", "content": content})
            try:
                await self._backend.write_file(
                    self._history_path(agent_id),
                    json.dumps(history).encode("utf-8"),
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.warning(
                    "Sub-Agent instance %s: failed to persist history "
                    "(%s); the next resume will lose this turn.",
                    agent_id,
                    type(exc).__name__,
                )
            if created:
                assert handle is not None
                try:
                    await self._registry.commit(
                        handle,
                        agent_id,
                        self.name,
                    )
                except Exception:  # noqa: BLE001 - best-effort persistence
                    pass

        # Persist the full reply, then truncate the in-context return.
        if output_file is not None:
            await self._backend.write_file(
                output_file,
                content.encode("utf-8"),
            )
        output = content
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"

        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        metadata: dict = {
            "model": self._model,
            "citations": data.get("search_results") or [],
            "usage": {
                "reasoning_tokens": details.get("reasoning_tokens"),
                "num_search_queries": usage.get("num_search_queries"),
                "total_tokens": usage.get("total_tokens"),
            },
        }
        if self.is_stateful:
            metadata["instance"] = handle
            metadata["agent_id"] = agent_id
            metadata["action"] = action

        await self._emit("completed", handle, agent_id, action)
        yield ToolChunk(
            content=[TextBlock(text=output)],
            state=ToolResultState.RUNNING,
            is_last=True,
            metadata=metadata,
        )

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """Auto-run: always allow sub-agent calls.

        Args:
            tool_input (`dict[str, Any]`):
                The tool input for this invocation.
            context (`PermissionContext`):
                The permission context.

        Returns:
            `PermissionDecision`:
                An ALLOW decision.
        """
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Sub-Agent calls run automatically.",
        )
