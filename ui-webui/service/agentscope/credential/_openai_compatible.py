# -*- coding: utf-8 -*-
"""The OpenAI-compatible custom credential."""

from typing import TYPE_CHECKING, Literal, Type

from pydantic import ConfigDict, Field, SecretStr

from ._base import CredentialBase

if TYPE_CHECKING:
    from ..model import ChatModelBase, ModelCard


class OpenAICompatibleCredential(CredentialBase):
    """A credential for any OpenAI-compatible API endpoint.

    Unlike :class:`OpenAICredential`, ``base_url`` is required and the
    built-in model catalog is empty: the models this endpoint serves are
    supplied entirely through the per-credential ``models`` override.
    """

    model_config = ConfigDict(
        title="OpenAI-Compatible API",
    )

    type: Literal[
        "openai_compatible_credential"
    ] = "openai_compatible_credential"
    """The credential type."""

    api_key: SecretStr = Field(
        description="The API key for the OpenAI-compatible endpoint.",
        title="API Key",
    )
    """The API key."""

    base_url: str = Field(
        title="API Base URL",
        description=(
            "The base URL of the OpenAI-compatible endpoint, e.g. "
            "``http://host:port/v1``."
        ),
    )
    """The required base URL."""

    organization: str | None = Field(
        default=None,
        title="Organization",
        description="Optional organization ID.",
    )
    """The optional organization ID."""

    @classmethod
    def get_chat_model_class(cls) -> Type["ChatModelBase"]:
        """Return the OpenAIChatModel class."""
        from ..model import OpenAIChatModel

        return OpenAIChatModel

    @classmethod
    def list_models(cls) -> list["ModelCard"]:
        """Custom endpoints have no built-in catalog — models come from the
        per-credential override only.

        Returns:
            `list[ModelCard]`:
                Always an empty list.
        """
        return []
