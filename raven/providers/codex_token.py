"""One storage object for the Codex OAuth credential.

``oauth_cli_kit`` defaults to its own directory and its own filename
(``codex.json``, named for the Codex CLI). Every login, refresh and status check
in raven has to pass the same storage instead, or they disagree about where the
credential is -- which is exactly how a signed-in Codex reported itself as
unauthenticated forever.
"""

from raven.config.paths import get_oauth_dir

#: Named for raven's provider slug, so the directory reads as one credential per
#: provider rather than one per upstream client's habits.
CREDENTIAL_FILENAME = "openai_codex.json"


def codex_storage():
    """Return the kit token storage pointed at raven's own OAuth directory.

    ``import_codex_cli`` is declined on purpose. The kit would otherwise copy the
    Codex CLI's ``~/.codex/auth.json`` in on first read, which buys one saved
    login and costs the three properties this directory exists for: the picker's
    status would come from a file the request path does not use, refreshing a
    shared credential can invalidate the copy the other client still holds, and a
    disconnect could not remove what it did not write.
    """
    from oauth_cli_kit.storage import FileTokenStorage

    return FileTokenStorage(
        token_filename=CREDENTIAL_FILENAME,
        data_dir=get_oauth_dir(),
        import_codex_cli=False,
    )
