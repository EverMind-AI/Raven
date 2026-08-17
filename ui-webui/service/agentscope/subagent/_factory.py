# -*- coding: utf-8 -*-
"""The sub-agent factory class."""

from typing import Type, get_args, get_type_hints

from ._base import (
    CliSubAgentConfig,
    OpenAISubAgentConfig,
    SubAgentConfigBase,
)


class SubAgentFactory:
    """Registry and deserializer for :class:`SubAgentConfigBase` types.

    Mirrors :class:`~agentscope.credential.CredentialFactory`: built-in
    types are pre-registered; call :meth:`register` to add custom types
    before starting the app.
    """

    _classes: list[Type[SubAgentConfigBase]] = [
        CliSubAgentConfig,
        OpenAISubAgentConfig,
    ]

    @classmethod
    def register(cls, subagent_cls: Type[SubAgentConfigBase]) -> None:
        """Register a custom :class:`SubAgentConfigBase` subclass.

        Args:
            subagent_cls (`Type[SubAgentConfigBase]`):
                The subclass to register. Must define a ``type`` field
                with a unique ``Literal`` default.
        """
        if subagent_cls not in cls._classes:
            cls._classes.append(subagent_cls)

    @classmethod
    def get_subagent_class(
        cls,
        provider: str,
    ) -> Type[SubAgentConfigBase] | None:
        """Return the config class for the given ``type``, or ``None``.

        Args:
            provider (`str`):
                The ``type`` discriminator value (e.g. ``"cli_subagent"``).

        Returns:
            `Type[SubAgentConfigBase] | None`:
                The matching subclass, or ``None`` if not found.
        """
        for candidate in cls._classes:
            hints = get_type_hints(candidate)
            type_hint = hints.get("type")
            if type_hint is None:
                continue
            args = get_args(type_hint)
            if args and args[0] == provider:
                return candidate
        return None

    @classmethod
    def from_dict(cls, data: dict) -> SubAgentConfigBase:
        """Deserialize a config dict (from storage) to a typed instance.

        Args:
            data (`dict`):
                Raw dict containing a ``"type"`` key.

        Returns:
            `SubAgentConfigBase`:
                A typed subclass instance.
        """
        target = cls.get_subagent_class(data.get("type"))
        if target is None:
            raise ValueError(
                f"Unknown sub-agent type: {data.get('type')!r}",
            )
        return target.model_validate(data)

    @classmethod
    def list_schemas(cls) -> list[dict]:
        """Return JSON schemas for all registered sub-agent types.

        Returns:
            `list[dict]`:
                One ``model_json_schema()`` per registered type.
        """
        return [candidate.model_json_schema() for candidate in cls._classes]
