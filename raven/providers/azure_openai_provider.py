"""Azure OpenAI provider implementation with API version 2024-10-21."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from urllib.parse import urljoin

import httpx
import json_repair

from raven.providers.base import (
    LLMProvider,
    LLMResponse,
    ProviderHTTPError,
    RunMeta,
    ToolCallRequest,
    format_llm_error,
)

_AZURE_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name"})


class AzureOpenAIProvider(LLMProvider):
    """
    Azure OpenAI provider with API version 2024-10-21 compliance.

    Features:
    - Hardcoded API version 2024-10-21
    - Uses model field as Azure deployment name in URL path
    - Uses api-key header instead of Authorization Bearer
    - Uses max_completion_tokens instead of max_tokens
    - Direct HTTP calls, bypasses LiteLLM
    """

    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        default_model: str = "gpt-5.6-sol",
        deployment: str = "",
        api_version: str = "2024-10-21",
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        # Empty means "the model id names the deployment", which is how every
        # config written before the field existed says it.
        self.deployment = deployment
        self.api_version = api_version

        # Validate required parameters
        if not api_key:
            raise ValueError("Azure OpenAI api_key is required")
        if not api_base:
            raise ValueError("Azure OpenAI api_base is required")

        # Ensure api_base ends with /
        if not api_base.endswith("/"):
            api_base += "/"
        self.api_base = api_base

    def wire_model_id(self, model: str) -> str:
        """See ``LLMProvider.wire_model_id``.

        A configured ``deployment`` decides; otherwise the model id names it, as
        it did before the field existed. Falling back rather than requiring the
        field keeps working configs working -- and it is why the id may still
        not carry a prefix in that case: whatever is here goes into the URL path.
        """
        from raven.providers.registry import find_by_name
        from raven.providers.wire import wire_model

        return self.deployment or wire_model(model, spec=find_by_name("azure_openai"))

    def _build_chat_url(self, deployment_name: str) -> str:
        """Build the Azure OpenAI chat completions URL."""
        # Azure OpenAI URL format:
        # https://{resource}.openai.azure.com/openai/deployments/{deployment}/chat/completions?api-version={version}
        deployment_name = self.wire_model_id(deployment_name)

        base_url = self.api_base
        if not base_url.endswith("/"):
            base_url += "/"

        url = urljoin(base_url, f"openai/deployments/{deployment_name}/chat/completions")
        return f"{url}?api-version={self.api_version}"

    def _build_headers(self) -> dict[str, str]:
        """Build headers for Azure OpenAI API with api-key header."""
        return {
            "Content-Type": "application/json",
            "api-key": self.api_key,  # Azure OpenAI uses api-key header, not Authorization
            "x-session-affinity": uuid.uuid4().hex,  # For cache locality
        }

    @staticmethod
    def _supports_temperature(
        deployment_name: str,
        reasoning_effort: str | None = None,
    ) -> bool:
        """Return True when temperature is likely supported for this deployment."""
        if reasoning_effort:
            return False
        name = deployment_name.lower()
        return not any(token in name for token in ("gpt-5", "o1", "o3", "o4"))

    def _prepare_request_payload(
        self,
        deployment_name: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request payload with Azure OpenAI 2024-10-21 compliance."""
        payload: dict[str, Any] = {
            "messages": self._sanitize_request_messages(
                self._sanitize_empty_content(messages),
                _AZURE_MSG_KEYS,
            ),
        }
        # Azure API 2024-10-21 uses max_completion_tokens, and treats it as
        # optional. Only a caller's own pin ever names one; absent that, the
        # deployment's own limit applies.
        if max_tokens is not None:
            payload["max_completion_tokens"] = max(1, max_tokens)

        if self._supports_temperature(deployment_name, reasoning_effort):
            payload["temperature"] = temperature

        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"

        return payload

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request to Azure OpenAI.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (used as deployment name).
            max_tokens: Maximum tokens in response (mapped to max_completion_tokens).
            temperature: Sampling temperature.
            reasoning_effort: Optional reasoning effort parameter.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        deployment_name = model or self.default_model
        url = self._build_chat_url(deployment_name)
        headers = self._build_headers()
        payload = self._prepare_request_payload(
            deployment_name,
            messages,
            tools,
            max_tokens,
            temperature,
            reasoning_effort,
            tool_choice=tool_choice,
        )

        try:
            async with httpx.AsyncClient(timeout=self.generation.timeout, verify=True) as client:
                response = await asyncio.wait_for(
                    client.post(url, headers=headers, json=payload), self.generation.timeout
                )
                if response.status_code != 200:
                    exc = ProviderHTTPError(
                        response.status_code, f"Azure OpenAI API Error {response.status_code}: {response.text}"
                    )
                    classification = self.classify_error(exc)
                    return LLMResponse(
                        content=format_llm_error(exc, classification, provider="azure_openai"),
                        finish_reason="error",
                        error_classification=classification,
                    )

                response_data = response.json()
                return self._parse_response(response_data)

        except Exception as e:
            classification = self.classify_error(e)
            return LLMResponse(
                content=format_llm_error(e, classification, provider="azure_openai"),
                finish_reason="error",
                error_classification=classification,
            )

    def _parse_response(self, response: dict[str, Any]) -> LLMResponse:
        """Parse Azure OpenAI response into our standard format."""
        try:
            choice = response["choices"][0]
            message = choice["message"]

            tool_calls = []
            if message.get("tool_calls"):
                for tc in message["tool_calls"]:
                    # Parse arguments from JSON string if needed. Whether the
                    # repair was needed travels with the call: a cut mid-blob
                    # is closed here silently, and that repair is the one local
                    # signal that the call never finished arriving.
                    args = tc["function"]["arguments"]
                    repaired = False
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = json_repair.loads(args)
                            repaired = True

                    tool_calls.append(
                        ToolCallRequest(
                            id=tc["id"],
                            name=tc["function"]["name"],
                            arguments=args,
                            run_meta=RunMeta(arguments_repaired=True) if repaired else None,
                        )
                    )

            usage = {}
            if response.get("usage"):
                usage_data = response["usage"]
                usage = {
                    "prompt_tokens": usage_data.get("prompt_tokens", 0),
                    "completion_tokens": usage_data.get("completion_tokens", 0),
                    "total_tokens": usage_data.get("total_tokens", 0),
                }

            reasoning_content = message.get("reasoning_content") or None

            return LLMResponse(
                content=message.get("content"),
                tool_calls=tool_calls,
                finish_reason=choice.get("finish_reason", "stop"),
                usage=usage,
                reasoning_content=reasoning_content,
            )

        except (KeyError, IndexError) as e:
            err = ValueError(f"unexpected Azure OpenAI response shape: {e!r}")
            classification = self.classify_error(err)
            return LLMResponse(
                content=format_llm_error(err, classification, provider="azure_openai"),
                finish_reason="error",
                error_classification=classification,
            )

    def get_default_model(self) -> str:
        """Get the default model (also used as default deployment name)."""
        return self.default_model
