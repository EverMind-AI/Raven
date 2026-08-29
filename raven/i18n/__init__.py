"""User-facing text in the user's language.

The English source text is the message id (gettext style): ``t("Back")`` returns
``"Back"`` under the default language and the catalog's translation under
another. Format arguments ride as keywords, ``t("Skipped {label}.", label=...)``,
so a translation may reorder them. The language is process state set by the
host (the onboarding wizard from its first screen, other entrances from
``config.language``); it is never read from the config here, because the wizard
runs before a config exists.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from raven.i18n import zh

_CATALOGS: dict[str, dict[str, str]] = {"zh": zh.MESSAGES}
_PROMPTS = Path(__file__).resolve().parent.parent / "templates" / "prompts"
_language = "en"


def set_language(language: str) -> None:
    """Select the language every later :func:`t` renders in; unknown codes mean English."""
    global _language
    _language = language if language in _CATALOGS else "en"


def current_language() -> str:
    return _language


def t(text: str, /, **arguments: object) -> str:
    """Translate ``text`` into the current language and fill its ``{placeholders}``."""
    return t_in(_language, text, **arguments)


def t_in(language: str, text: str, /, **arguments: object) -> str:
    """Translate into a named language: for text whose language follows its content, not the UI."""
    catalog = _CATALOGS.get(language)
    message = catalog.get(text, text) if catalog is not None else text
    return message.format(**arguments) if arguments else message


def prompt(name: str) -> str:
    """A model-facing prompt template in the current language (``templates/prompts/<language>/<name>.md``)."""
    return prompt_in(_language, name)


@lru_cache(maxsize=None)
def prompt_in(language: str, name: str) -> str:
    """The named template in ``language``, falling back to English when that language has none."""
    for candidate in (language, "en"):
        path = _PROMPTS / candidate / f"{name}.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"no prompt template named {name!r}")


__all__ = ["current_language", "prompt", "prompt_in", "set_language", "t", "t_in"]
