"""Shared interpretation of terminal LLM responses used by Playbook calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.contracts.llm_provider import ErrorClassification


@dataclass(frozen=True)
class ProviderFailure:
    """A provider error after its retry and optional fallback ladder ended."""

    classification: "ErrorClassification | None"
    category: str
    detail: str

    @property
    def message(self) -> str:
        return f"provider_call_failed [{self.category}]: {self.detail}"


def provider_failure(response: Any) -> ProviderFailure | None:
    """Return the structured failure carried by an error response, if any."""
    if getattr(response, "finish_reason", None) != "error":
        return None
    classification = getattr(response, "error_classification", None)
    category = getattr(classification, "category", "unclassified")
    detail = str(getattr(response, "content", None) or "").strip() or "provider returned an error"
    return ProviderFailure(classification=classification, category=category, detail=detail)


class ProviderResponseError(RuntimeError):
    """A structured-output call ended at the Provider failure boundary."""

    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class RequiredToolError(RuntimeError):
    """A successful response violated a required tool-call contract."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def required_tool_arguments(response: Any, required_name: str) -> dict[str, Any]:
    """Return one required tool's object arguments or raise a typed boundary error."""
    if failure := provider_failure(response):
        raise ProviderResponseError(failure)
    calls = getattr(response, "tool_calls", None) or []
    if not calls:
        if getattr(response, "truncated", False):
            raise RequiredToolError(
                "required_tool_output_truncated",
                f"response reached max_tokens={getattr(response, 'max_tokens', None)} before calling {required_name}",
            )
        raise RequiredToolError(
            "required_tool_missing",
            f"response finished with {getattr(response, 'finish_reason', 'unknown')!r} without calling {required_name}",
        )
    if len(calls) != 1:
        raise RequiredToolError(
            "required_tool_multiple_calls",
            f"expected one {required_name} call, got {len(calls)}",
        )
    call = calls[0]
    name = getattr(call, "name", None)
    if name != required_name:
        raise RequiredToolError("required_tool_wrong_name", f"expected {required_name}, got {name!r}")
    args = getattr(call, "arguments", None)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError as exc:
            if getattr(response, "truncated", False):
                code = "required_tool_arguments_truncated"
                detail = f"arguments were cut off at max_tokens={getattr(response, 'max_tokens', None)}"
            else:
                code = "required_tool_arguments_invalid"
                detail = f"arguments are not complete JSON: {exc.msg}"
            raise RequiredToolError(code, f"{required_name} {detail}") from exc
    if not isinstance(args, dict):
        raise RequiredToolError("required_tool_arguments_invalid", f"{required_name} arguments must be an object")
    return args
