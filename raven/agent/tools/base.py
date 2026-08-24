"""Base class for agent tools."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from raven.utils.helpers import ContentPart


@dataclass
class ToolResult:
    """A tool's output split into the model-facing text and an optional
    human-facing display string.

    ``execute`` may return a bare ``str`` (model text only — the UI falls back
    to a generic preview of it) or this, when the tool wants a cleaner
    transcript rendering than what it feeds the model. ``display_text`` must be
    built from the tool's own execution data, not by re-parsing ``model_text``.

    ``retryable=False`` suppresses the registry's generic change-approach hint.
    ``abort_action=True`` tells the agent loop not to execute sibling calls or
    ask the model for another approach.

    ``blocks`` carries multimodal content parts (OpenAI-shaped ``text`` /
    ``image_url`` dicts) for tools whose result is not expressible as text — a
    read of a PNG, say. It is strictly *additive*: ``model_text`` must stand on
    its own, because only providers that can carry an image in a tool result
    ever look at ``blocks`` (see ``supports_image_tool_result``). Everything
    else — the sentinel, subagents, the curator, session export, a provider
    talking Chat Completions — keeps using the text and must still make sense.
    So a tool setting ``blocks`` puts the metadata *and* the file path in
    ``model_text``, never "see the image above".

    ``metadata`` and ``diff`` are the two client-only fields: neither is ever
    shown to the model, and both ride out on the ``tool.complete`` lifecycle
    event for an outlet to render. ``metadata`` is a structured payload the tool
    chose to publish (a file manifest, say) that an outlet which does not
    understand a key simply ignores; ``diff`` is a unified diff of what the call
    changed on disk. A tool that leaves them ``None`` -- most of them -- costs
    the wire two nulls.
    """

    model_text: str
    display_text: str | None = None
    retryable: bool = True
    abort_action: bool = False
    blocks: list[ContentPart] | None = None
    metadata: dict[str, Any] | None = None
    diff: str | None = None


class ToolOutput(str):
    """What :meth:`ToolRegistry.execute` hands back: the model-facing text with
    the optional display string and multimodal blocks attached.

    A ``str`` subclass on purpose. Every caller of the registry boundary --
    the sentinel action executor, the subagent manager, the context curator,
    tracing -- puts the return value straight into a message, a preview or an
    artifact, so the boundary has to return something that *is* a str; handing
    them a :class:`ToolResult` would format its repr into model context and
    user-facing replies. The agent loop reads ``display_text`` off it to render
    the transcript row, ``blocks`` to build a multimodal tool result, the
    control flags to enforce terminal tool decisions, and ``metadata``/``diff``
    to fill the client-facing fields of the ``tool.complete`` event.
    """

    display_text: str | None
    retryable: bool
    abort_action: bool
    blocks: list[ContentPart] | None
    metadata: dict[str, Any] | None
    diff: str | None

    def __new__(
        cls,
        model_text: str,
        display_text: str | None = None,
        *,
        retryable: bool = True,
        abort_action: bool = False,
        blocks: list[ContentPart] | None = None,
        metadata: dict[str, Any] | None = None,
        diff: str | None = None,
    ) -> "ToolOutput":
        out = super().__new__(cls, model_text)
        out.display_text = display_text
        out.retryable = retryable
        out.abort_action = abort_action
        out.blocks = blocks
        out.metadata = metadata
        out.diff = diff
        return out


class Tool(ABC):
    """
    Abstract base class for agent tools.

    Tools are capabilities that the agent can use to interact with
    the environment, such as reading files, executing commands, etc.
    """

    # Hard ceiling (seconds) the registry enforces via asyncio.wait_for, so a
    # tool that lacks its own timeout can't wedge the whole agent loop. None ->
    # the registry default. Tools with a longer legitimate runtime (exec,
    # video generation, spawn) raise this; see ToolRegistry.execute.
    timeout_seconds: float | None = None

    # Tools that intentionally block waiting on a human (ask_user,
    # request_permissions, future human-approval gates) set this True so the
    # registry does NOT wrap them in a timeout — they manage their own
    # auto-resolution instead of being killed mid-wait.
    blocking_interaction: bool = False

    _TYPE_MAP = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""
        pass

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for tool parameters."""
        pass

    @abstractmethod
    async def execute(self, **kwargs: Any) -> "str | ToolResult":
        """
        Execute the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters.

        Returns:
            The model-facing result as a ``str``, or a :class:`ToolResult` when
            the tool wants a distinct human-facing display string.
        """
        pass

    def display_call(self, args: dict[str, Any]) -> str | None:
        """Human-facing one-line summary of a call to this tool.

        ``None`` (the default) lets the UI derive a generic summary from the
        arguments. Override only when a tool wants a cleaner label than the
        generic one (e.g. ask_user showing just its question, not the raw
        arguments blob).
        """
        return None

    @property
    def truncation_hint(self) -> str | None:
        """What to do differently when a call to this tool arrives cut off.

        The generic truncation message can only say "send less", which leaves a
        model to guess at what smaller looks like -- and dropping the largest
        field is one of the guesses. What it needs is the next action, and only
        the tool knows what that is: write_file can be appended to, a shell
        command can be split into several runs, and some tools have no smaller
        form at all.

        ``None`` (the default) means the generic message stands on its own.

        Only for a cut the upstream confirmed. Where the cause is merely likely
        see ``incomplete_hint``: advice written for a turn that ran out of room
        misleads a model that simply wrote bad JSON, and sends it looking for a
        size problem it does not have.
        """
        return None

    @property
    def incomplete_hint(self) -> str | None:
        """What to do differently when a call arrives unparseable and last.

        Same situation as ``truncation_hint`` under one of its two readings, and
        deliberately a separate string rather than the same one reused: this one
        is consumed under an ``If it was the output limit:`` heading and has to
        read as the consequent of a condition, where the other states a fact.

        The near-duplication is the cost of not asserting a cause we cannot
        establish. A tool answering one of the two and not the other leaves the
        ambiguous refusal with no way forward, which is the case that exists
        because the upstream under-reports -- guarded by a test.
        """
        return None

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply safe schema-driven casts before validation."""
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            return params

        return self._cast_object(params, schema)

    def _cast_object(self, obj: Any, schema: dict[str, Any]) -> dict[str, Any]:
        """Cast an object (dict) according to schema."""
        if not isinstance(obj, dict):
            return obj

        props = schema.get("properties", {})
        result = {}

        for key, value in obj.items():
            if key in props:
                result[key] = self._cast_value(value, props[key])
            else:
                result[key] = value

        return result

    def _cast_value(self, val: Any, schema: dict[str, Any]) -> Any:
        """Cast a single value according to schema."""
        target_type = schema.get("type")

        if target_type == "boolean" and isinstance(val, bool):
            return val
        if target_type == "integer" and isinstance(val, int) and not isinstance(val, bool):
            return val
        if target_type in self._TYPE_MAP and target_type not in ("boolean", "integer", "array", "object"):
            expected = self._TYPE_MAP[target_type]
            if isinstance(val, expected):
                return val

        if target_type == "integer" and isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                return val

        if target_type == "number" and isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                return val

        if target_type == "string":
            return val if val is None else str(val)

        if target_type == "boolean" and isinstance(val, str):
            val_lower = val.lower()
            if val_lower in ("true", "1", "yes"):
                return True
            if val_lower in ("false", "0", "no"):
                return False
            return val

        if target_type == "array" and isinstance(val, list):
            item_schema = schema.get("items")
            return [self._cast_value(item, item_schema) for item in val] if item_schema else val

        if target_type == "object" and isinstance(val, dict):
            return self._cast_object(val, schema)

        return val

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate tool parameters against JSON schema. Returns error list (empty if valid)."""
        if not isinstance(params, dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return self._validate(params, {**schema, "type": "object"}, "")

    def _validate(self, val: Any, schema: dict[str, Any], path: str) -> list[str]:
        t, label = schema.get("type"), path or "parameter"
        if t == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
            return [f"{label} should be integer"]
        if t == "number" and (not isinstance(val, self._TYPE_MAP[t]) or isinstance(val, bool)):
            return [f"{label} should be number"]
        if t in self._TYPE_MAP and t not in ("integer", "number") and not isinstance(val, self._TYPE_MAP[t]):
            return [f"{label} should be {t}"]

        errors = []
        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{label} must be one of {schema['enum']}")
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(f"{label} must be >= {schema['minimum']}")
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(f"{label} must be <= {schema['maximum']}")
        if t == "string":
            if "minLength" in schema and len(val) < schema["minLength"]:
                errors.append(f"{label} must be at least {schema['minLength']} chars")
            if "maxLength" in schema and len(val) > schema["maxLength"]:
                errors.append(f"{label} must be at most {schema['maxLength']} chars")
        if t == "object":
            props = schema.get("properties", {})
            for k in schema.get("required", []):
                if k not in val:
                    errors.append(f"missing required {path + '.' + k if path else k}")
            for k, v in val.items():
                if k in props:
                    errors.extend(self._validate(v, props[k], path + "." + k if path else k))
        if t == "array" and "items" in schema:
            for i, item in enumerate(val):
                errors.extend(self._validate(item, schema["items"], f"{path}[{i}]" if path else f"[{i}]"))
        return errors

    def to_schema(self) -> dict[str, Any]:
        """Convert tool to OpenAI function schema format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
