# -*- coding: utf-8 -*-
"""Sub-Agent config base classes."""

import re
import shlex
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .._utils._common import _generate_id


class SubAgentConfigBase(BaseModel):
    """The base class for all sub-agent configs.

    A sub-agent config is user-authored data describing an external
    agent the unified agent may delegate to. Subclasses set a unique
    ``type`` discriminator so :class:`SubAgentFactory` can round-trip
    them through storage.
    """

    id: str = Field(
        default_factory=_generate_id,
        description="The sub-agent id.",
    )


class CliSubAgentConfig(SubAgentConfigBase):
    """A sub-agent invoked by running a CLI command.

    The ``command`` is a template whose ``{prompt}`` placeholder is
    replaced with the task the unified agent wants to delegate, e.g.
    ``claude -p {prompt} --dangerously-skip-permissions``. The tool
    reads the prompt from a file and substitutes its contents for
    ``{prompt}``.
    """

    model_config = ConfigDict(title="CLI Sub-Agent")

    type: Literal["cli_subagent"] = "cli_subagent"
    """The sub-agent type discriminator."""

    name: str = Field(
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Tool name shown to the agent. Letters, digits, '_' and '-'."
        ),
    )
    """The tool name presented to the agent."""

    description: str = Field(
        description=(
            "Agent-readable description used to decide when to delegate "
            "to this sub-agent."
        ),
    )
    """The tool description presented to the agent."""

    command: str = Field(
        description=(
            "Shell command template. Must contain '{prompt}', replaced "
            "with the delegated task (read from a file by the tool)."
        ),
    )
    """The command template containing ``{prompt}``."""

    resume_command: str | None = Field(
        default=None,
        description=(
            "Optional command template used to RESUME an existing "
            "instance. When set, the prototype is stateful: the first "
            "call to an instance handle runs `command` (create) and "
            "later calls run this template (resume). Both `command` "
            "and `resume_command` must then contain '{agent_id}' and "
            "'{prompt}'. When empty, the prototype is stateless."
        ),
    )
    """The optional resume command template containing ``{agent_id}``
    and ``{prompt}``; presence makes the prototype stateful."""

    id_source: Literal["provisioned", "derived"] = Field(
        default="provisioned",
        description=(
            "How the CLI session id is provisioned. 'provisioned': "
            "RavenX mints a uuid and injects it into `command` via "
            "'{agent_id}' (e.g. Claude Code's --session-id). 'derived': "
            "the CLI mints the id itself; `command` carries no "
            "'{agent_id}' and RavenX extracts it from the create run's "
            "stdout using `session_id_pattern`."
        ),
    )
    """The id-provisioning strategy."""

    session_id_pattern: str | None = Field(
        default=None,
        description=(
            "Regex with exactly one capture group, matched against the "
            "create run's stdout to extract the CLI-minted session id. "
            "Required when `id_source` is 'derived'."
        ),
    )
    """The derived-id extraction regex."""

    output_pattern: str | None = Field(
        default=None,
        description=(
            "Optional regex with exactly one capture group. When set, "
            "group 1 of the first match against stdout is returned as "
            "the sub-agent reply (strips CLI transcript wrappers)."
        ),
    )
    """The optional reply-extraction regex."""

    transcript_format: Literal["text", "codex_jsonl"] = Field(
        default="text",
        description=(
            "How the sub-agent's stdout transcript is parsed. 'text' "
            "(default): use `session_id_pattern` / `output_pattern` "
            "regexes. 'codex_jsonl': parse a Codex `exec --json` JSONL "
            "stream structurally for the session id and reply; the regex "
            "fields must then be unset."
        ),
    )
    """The transcript parsing strategy."""

    cwd: str | None = Field(
        default=None,
        description="Optional working directory for the sub-agent.",
    )
    """The working directory."""

    env: dict[str, str] | None = Field(
        default=None,
        description="Optional extra environment variables.",
    )
    """Extra environment variables."""

    timeout: int = Field(
        default=600,
        gt=0,
        description="Maximum seconds to wait for the sub-agent.",
    )
    """The execution timeout in seconds."""

    @field_validator("command")
    @classmethod
    def _require_prompt_placeholder(cls, value: str) -> str:
        """Ensure the command carries a prompt-delivery placeholder.

        A command is valid when it contains ``{prompt}``. The prompt's
        contents are substituted for this placeholder (read from a file
        by the tool).

        Args:
            value (`str`):
                The command template to validate.

        Returns:
            `str`:
                The validated command template.
        """
        if "{prompt}" not in value:
            raise ValueError("command template must contain '{prompt}'")
        return value

    @field_validator("command")
    @classmethod
    def _require_literal_executable(cls, value: str) -> str:
        """Ensure the command's first token is a literal executable.

        This guards against a delegated prompt shaped like
        ``NAME=VALUE`` being swallowed as an environment override by
        the ``env`` prefix (used when ``env`` is set) instead of being
        passed as the executable or argument.

        Args:
            value (`str`):
                The command template to validate.

        Returns:
            `str`:
                The validated command template.
        """
        try:
            tokens = shlex.split(value)
        except ValueError as error:
            raise ValueError(
                f"command template is not valid shell syntax: {error}",
            ) from error
        if not tokens:
            raise ValueError("command must contain an executable")
        if "{prompt}" in tokens[0]:
            raise ValueError(
                "command's first token must be a literal executable, "
                "not a prompt placeholder",
            )
        return value

    @model_validator(mode="after")
    def _validate_patterns(self) -> "CliSubAgentConfig":
        """Ensure each regex pattern compiles with exactly one group.

        Returns:
            `CliSubAgentConfig`:
                The validated config instance.
        """
        for label, pattern in (
            ("session_id_pattern", self.session_id_pattern),
            ("output_pattern", self.output_pattern),
        ):
            if pattern is None:
                continue
            try:
                compiled = re.compile(pattern)
            except re.error as error:
                raise ValueError(
                    f"{label} is not a valid regex: {error}",
                ) from error
            if compiled.groups != 1:
                raise ValueError(
                    f"{label} must have exactly one capture group, "
                    f"found {compiled.groups}",
                )
        return self

    @model_validator(mode="after")
    def _validate_transcript_format(self) -> "CliSubAgentConfig":
        """codex_jsonl parses structurally; regex fields must be unset.

        Returns:
            `CliSubAgentConfig`:
                The validated config instance.
        """
        if self.transcript_format == "codex_jsonl":
            if self.session_id_pattern is not None:
                raise ValueError(
                    "session_id_pattern must be unset when "
                    "transcript_format is 'codex_jsonl' (the id is parsed "
                    "from the JSONL stream)",
                )
            if self.output_pattern is not None:
                raise ValueError(
                    "output_pattern must be unset when transcript_format "
                    "is 'codex_jsonl' (the reply is parsed from the JSONL "
                    "stream)",
                )
        return self

    @model_validator(mode="after")
    def _validate_stateful(self) -> "CliSubAgentConfig":
        """Validate id_source and the create/resume template pair.

        Stateless prototypes must be 'provisioned' and carry no
        `session_id_pattern`. Stateful 'provisioned' prototypes require
        `{agent_id}` in both templates (unchanged). Stateful 'derived'
        prototypes forbid `{agent_id}` in `command`, require it in
        `resume_command`, and (only when `transcript_format` is 'text')
        require a `session_id_pattern`.

        Returns:
            `CliSubAgentConfig`:
                The validated config instance.
        """
        if not self.resume_command:
            if self.id_source == "derived":
                raise ValueError(
                    "id_source 'derived' requires a resume_command "
                    "(derived instances are stateful)",
                )
            if self.session_id_pattern is not None:
                raise ValueError(
                    "session_id_pattern is only valid for a stateful "
                    "'derived' prototype",
                )
            return self

        for label, template in (
            ("command", self.command),
            ("resume_command", self.resume_command),
        ):
            if "{prompt}" not in template:
                raise ValueError(
                    f"stateful {label} must contain '{{prompt}}'",
                )
            try:
                tokens = shlex.split(template)
            except ValueError as error:
                raise ValueError(
                    f"{label} is not valid shell syntax: {error}",
                ) from error
            if not tokens or "{prompt}" in tokens[0]:
                raise ValueError(
                    f"{label}'s first token must be a literal executable",
                )

        if "{agent_id}" not in self.resume_command:
            raise ValueError(
                "stateful resume_command must contain the '{agent_id}' "
                "placeholder",
            )

        if self.id_source == "provisioned":
            if "{agent_id}" not in self.command:
                raise ValueError(
                    "provisioned stateful command must contain the "
                    "'{agent_id}' placeholder",
                )
        else:  # derived
            if "{agent_id}" in self.command:
                raise ValueError(
                    "derived command must not contain '{agent_id}' (the "
                    "CLI mints the session id itself)",
                )
            if (
                self.transcript_format == "text"
                and not self.session_id_pattern
            ):
                raise ValueError(
                    "derived prototype with 'text' transcript_format "
                    "requires a session_id_pattern",
                )
        return self


class OpenAISubAgentConfig(SubAgentConfigBase):
    """A sub-agent invoked over an OpenAI-compatible Chat Completions API.

    Unlike :class:`CliSubAgentConfig` (which runs a subprocess), this
    delegates by POSTing an OpenAI Chat Completions request to a hosted
    endpoint. The endpoint + API key are supplied by a stored
    :class:`~agentscope.credential.OpenAICompatibleCredential` referenced
    by ``credential_id`` (nothing sensitive is stored on the config).
    """

    model_config = ConfigDict(title="OpenAI-API Sub-Agent")

    type: Literal["openai_subagent"] = "openai_subagent"
    """The sub-agent type discriminator."""

    name: str = Field(
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Tool name shown to the agent. Letters, digits, '_' and '-'."
        ),
    )
    """The tool name presented to the agent."""

    description: str = Field(
        description=(
            "Agent-readable description used to decide when to delegate "
            "to this sub-agent."
        ),
    )
    """The tool description presented to the agent."""

    credential_id: str = Field(
        description=(
            "Id of a stored OpenAI-compatible credential supplying the "
            "endpoint base URL and API key."
        ),
    )
    """Reference to an ``OpenAICompatibleCredential``."""

    model: str = Field(
        description=(
            "The model id to request, e.g. 'mirothinker-1-7-deepresearch'."
        ),
    )
    """The chat completions model id."""

    stateful: bool = Field(
        default=True,
        description=(
            "When true, the per-instance message history is persisted and "
            "replayed on resume (the endpoint itself is stateless). When "
            "false, every call is an independent one-shot request."
        ),
    )
    """Whether instances carry replayed conversation history."""

    system_prompt: str | None = Field(
        default=None,
        description="Optional system message prepended to the conversation.",
    )
    """The optional system prompt."""

    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="Optional sampling temperature in [0.0, 2.0].",
    )
    """The optional sampling temperature."""

    max_tokens: int | None = Field(
        default=None,
        gt=0,
        description="Optional maximum completion tokens.",
    )
    """The optional completion-token cap."""

    timeout: int = Field(
        default=1200,
        gt=0,
        description="Maximum seconds to wait for the API response.",
    )
    """The request timeout in seconds."""

    @field_validator("credential_id", "model")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        """Reject blank references and strip surrounding whitespace.

        Args:
            value (`str`):
                The ``credential_id`` or ``model`` value.

        Returns:
            `str`:
                The stripped, non-empty value.
        """
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()
