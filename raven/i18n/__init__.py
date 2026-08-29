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

from raven.i18n import zh

_CATALOGS: dict[str, dict[str, str]] = {"zh": zh.MESSAGES}
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


__all__ = ["current_language", "set_language", "t", "t_in"]
