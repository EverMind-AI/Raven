# -*- coding: utf-8 -*-
"""The CLI sub-agent tool."""

import re
import shlex
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, AsyncGenerator, List

from ..message import TextBlock, ToolResultState
from ..permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from ..tool import BackendBase, LocalBackend, ToolBase, ToolChunk, ToolMiddlewareBase
from ._instance_progress import emit_instance_status
from ._transcript import parse_codex_jsonl

_MAX_OUTPUT_CHARS = 128000


class CliSubAgentTool(ToolBase):
    """A tool that delegates a task to an external CLI sub-agent.

    Runs a fixed command template (with a ``{prompt}`` placeholder) via
    the execution backend and returns the sub-agent's stdout. Modeled on
    the built-in :class:`~agentscope.tool.Bash` tool, but the argv is
    fixed by config and the delegated prompt is passed as a single argv
    token (no shell), so the prompt cannot inject shell metacharacters.
    """

    is_read_only: bool = False
    is_concurrency_safe: bool = False
    offload_hint_label: str = "subagent_response"
    """Hint label shown when a backgrounded sub-agent run delivers its
    result (rendered as "Sub-Agent Response" in the web UI)."""
    offload_noun: str = "Sub-Agent"
    """Human-readable noun used in offload notifications, e.g.
    "Sub-Agent 'X' is running in background"."""

    def __init__(
        self,
        name: str,
        description: str,
        command: str,
        resume_command: str | None = None,
        id_source: str = "provisioned",
        session_id_pattern: str | None = None,
        output_pattern: str | None = None,
        transcript_format: str = "text",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        backend: BackendBase | None = None,
        registry: Any = None,
        middlewares: List[ToolMiddlewareBase] | None = None,
        progress_publisher: (
            Callable[[str, dict], Awaitable[None]] | None
        ) = None,
    ) -> None:
        """Initialize the CLI sub-agent tool.

        Args:
            name (`str`):
                The tool name presented to the agent.
            description (`str`):
                The tool description presented to the agent.
            command (`str`):
                The create/first-call command template containing
                ``{prompt}`` (and ``{agent_id}`` when stateful).
            resume_command (`str | None`, optional):
                The resume command template. When set (with a
                ``registry``), the tool is stateful.
            id_source (`str`, defaults to `"provisioned"`):
                Id strategy: ``"provisioned"`` (RavenX mints the id and
                injects it via ``{agent_id}``) or ``"derived"`` (the CLI
                mints it and it is parsed from stdout).
            session_id_pattern (`str | None`, optional):
                Regex (one capture group) extracting the CLI-minted id
                from a derived create run's stdout.
            output_pattern (`str | None`, optional):
                Regex (one capture group) extracting the real reply from
                stdout; when unset, the raw output is returned.
            transcript_format (`str`, defaults to `"text"`):
                Transcript parsing strategy: ``"text"`` (regex via
                ``session_id_pattern`` / ``output_pattern``) or
                ``"codex_jsonl"`` (parse a Codex ``exec --json`` stream).
            cwd (`str | None`, optional):
                Working directory for the sub-agent process.
            env (`dict[str, str] | None`, optional):
                Extra environment variables applied via an ``env`` prefix.
            timeout (`int`, defaults to `600`):
                Maximum seconds to wait for the sub-agent.
            backend (`BackendBase | None`, optional):
                The execution backend. Defaults to :class:`LocalBackend`.
            registry (`Any`, optional):
                A :class:`SessionInstanceRegistry`-like object resolving
                instance handles to CLI session ids. Required for the
                tool to be stateful.
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
        self._command = command
        self._resume_command = resume_command
        self._id_source = id_source
        self._session_id_re = (
            re.compile(session_id_pattern) if session_id_pattern else None
        )
        self._output_re = (
            re.compile(output_pattern) if output_pattern else None
        )
        self._transcript_format = transcript_format
        self._cwd = cwd
        self._env = env
        self._timeout = timeout
        self._backend = backend or LocalBackend()
        self._registry = registry
        self._progress_publisher = progress_publisher
        self._transport = (
            "codex" if transcript_format == "codex_jsonl" else "cli"
        )
        self.input_schema = self._build_input_schema()

    @property
    def is_stateful(self) -> bool:
        """Whether this tool manages resumable instances."""
        return bool(self._resume_command) and self._registry is not None

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

    def _build_argv(
        self,
        prompt: str,
        template: str,
        agent_id: str | None = None,
    ) -> list[str]:
        """Build the argv for a delegated ``prompt``.

        Substitutes ``{prompt}`` (and ``{agent_id}`` when provided)
        inside each token, then prepends an ``env KEY=VAL`` prefix when
        env vars are configured.

        Args:
            prompt (`str`):
                The prompt text substituted for ``{prompt}``.
            template (`str`):
                The command template to expand.
            agent_id (`str | None`, optional):
                The CLI session id substituted for ``{agent_id}``.

        Returns:
            `list[str]`:
                The argv to run without a shell.
        """
        argv = []
        for tok in shlex.split(template):
            tok = tok.replace("{prompt}", prompt)
            if agent_id is not None:
                tok = tok.replace("{agent_id}", agent_id)
            argv.append(tok)
        if self._env:
            argv = [
                "env",
                *(f"{key}={value}" for key, value in self._env.items()),
                *argv,
            ]
        return argv

    async def _write_temp_prompt(self, prompt: str) -> str:
        """Write ``prompt`` to a temp file under ``cwd`` and return its path.

        Args:
            prompt (`str`):
                The prompt text to persist.

        Returns:
            `str`:
                The path of the written prompt file (read back to fill
                ``{prompt}``).
        """
        base = self._cwd or "."
        path = self._backend.join_path(
            base,
            f".ravenx_prompt_{uuid.uuid4().hex}.md",
        )
        await self._backend.write_file(path, prompt.encode("utf-8"))
        return path

    async def call(  # type: ignore[override]
        self,
        prompt: str,
        instance: str | None = None,
        prompt_file: str | None = None,
        output_file: str | None = None,
    ) -> AsyncGenerator[ToolChunk, None]:
        """Run the sub-agent and yield its result.

        Args:
            prompt (`str`):
                The task to delegate to the sub-agent.
            instance (`str | None`, optional):
                The instance handle (stateful prototypes only). An
                unknown/absent handle creates; a known handle resumes.
            prompt_file (`str | None`, optional):
                Path to a file holding the prompt. When ``None`` (a
                direct call), ``prompt`` is written to a generated temp
                file. The file's contents are read and substituted for
                ``{prompt}``.
            output_file (`str | None`, optional):
                When set and the sub-agent exits successfully, the final
                (post-extraction, untruncated) output is written here.

        Yields:
            `ToolChunk`:
                A single terminal chunk with the sub-agent's output.
        """
        handle: str | None = None
        action: str | None = None
        agent_id: str | None = None
        created = False
        template = self._command
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
                assert self._resume_command is not None
                template = self._resume_command
                action = "resume"
            else:
                created = True
                action = "create"
                agent_id = (
                    None if self._id_source == "derived" else str(uuid.uuid4())
                )
            await self._emit("running", handle, agent_id, action)
        else:
            agent_id = str(uuid.uuid4())

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
                    TextBlock(
                        text=f"Sub-Agent prompt file error: {exc}",
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        argv = self._build_argv(prompt_text, template, agent_id)

        try:
            result = await self._backend.exec_shell(
                argv,
                cwd=self._cwd,
                timeout=float(self._timeout),
            )
        except Exception as exc:  # noqa: BLE001
            await self._emit("failed", handle, agent_id, action)
            yield ToolChunk(
                content=[TextBlock(text=f"Sub-Agent failed: {exc}")],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        stdout = result.stdout.decode("utf-8", errors="replace").replace(
            "\r\n",
            "\n",
        )
        stderr = result.stderr.decode("utf-8", errors="replace").replace(
            "\r\n",
            "\n",
        )

        if result.exit_code == -1 and result.stderr == b"timed out":
            await self._emit("failed", handle, agent_id, action)
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"Sub-Agent timed out after " f"{self._timeout}s."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        if not result.ok():
            output = stdout
            if stderr:
                output = f"{output}\n{stderr}" if output else stderr
            if len(output) > _MAX_OUTPUT_CHARS:
                output = (
                    output[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"
                )
            await self._emit("failed", handle, agent_id, action)
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"Sub-Agent exited with code "
                            f"{result.exit_code}.\n{output}"
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        # Success. Parse a structured JSONL transcript once, if configured.
        jsonl_id: str | None = None
        jsonl_reply: str | None = None
        if self._transcript_format == "codex_jsonl":
            jsonl_id, jsonl_reply = parse_codex_jsonl(stdout)

        # 1) Determine the CLI-minted id for a derived create.
        if created and self._id_source == "derived":
            if self._transcript_format == "codex_jsonl":
                if jsonl_id is not None:
                    agent_id = jsonl_id
            elif self._session_id_re is not None:
                match = self._session_id_re.search(stdout)
                if match is not None:
                    agent_id = match.group(1)

        # 2) Deferred persistence: commit only a successful create.
        if created and self.is_stateful and agent_id is not None:
            assert handle is not None
            try:
                await self._registry.commit(handle, agent_id, self.name)
            except Exception:  # noqa: BLE001 - best-effort persistence
                pass

        # 3) Extract the reply, or fall back to the raw combined output.
        combined = stdout
        if stderr:
            combined = f"{combined}\n{stderr}" if combined else stderr
        if self._transcript_format == "codex_jsonl":
            output = (
                jsonl_reply.strip() if jsonl_reply is not None else combined
            )
        elif self._output_re is not None:
            match = self._output_re.search(stdout)
            output = match.group(1).strip() if match is not None else combined
        else:
            output = combined

        # 4) Append a parse-miss warning last, so extraction cannot drop it.
        if created and self._id_source == "derived" and agent_id is None:
            output = (
                f"{output}\n\n[RavenX] Warning: could not extract a session "
                "id from the create output; this instance is not resumable. "
                "Use a new instance handle to recreate."
            )

        # 5) Persist the full output, then truncate the in-context return.
        if output_file is not None:
            await self._backend.write_file(
                output_file,
                output.encode("utf-8"),
            )
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"

        if self.is_stateful:
            metadata = {
                "instance": handle,
                "agent_id": agent_id,
                "action": action,
            }
        else:
            metadata = {}

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
