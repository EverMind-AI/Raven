"""IMAP/SMTP transport for the email adapter.

Connects, searches, fetches raw message bytes (and marks them seen), and sends
replies over SMTP. No parsing — that's :mod:`.parsing`. Live network flows,
exercised by integration/manual testing.
"""

from __future__ import annotations

import imaplib
import smtplib
import ssl
from email.message import EmailMessage

from raven.channels.adapters.email import parsing


class EmailMailbox:
    """Thin wrapper over imaplib/smtplib driven by an EmailConfig."""

    def __init__(self, config):
        self.config = config

    def search_fetch(self, search_criteria: tuple[str, ...], *, mark_seen: bool, limit: int) -> list[tuple[str, bytes]]:
        """Search the mailbox and return ``(uid, raw_bytes)`` per match, newest
        ``limit`` only when ``limit > 0``. Marks fetched messages seen when
        ``mark_seen`` is set. Dedup/parsing are the caller's concern."""
        results: list[tuple[str, bytes]] = []
        mailbox = self.config.imap_mailbox or "INBOX"
        client = (
            imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port)
            if self.config.imap_use_ssl
            else imaplib.IMAP4(self.config.imap_host, self.config.imap_port)
        )
        try:
            client.login(self.config.imap_username, self.config.imap_password)
            if client.select(mailbox)[0] != "OK":
                return results
            status, data = client.search(None, *search_criteria)
            if status != "OK" or not data:
                return results

            ids = data[0].split()
            if limit > 0 and len(ids) > limit:
                ids = ids[-limit:]
            for imap_id in ids:
                status, fetched = client.fetch(imap_id, "(BODY.PEEK[] UID)")
                if status != "OK" or not fetched:
                    continue
                raw = parsing.extract_message_bytes(fetched)
                if raw is None:
                    continue
                results.append((parsing.extract_uid(fetched), raw))
                if mark_seen:
                    client.store(imap_id, "+FLAGS", "\\Seen")
        finally:
            try:
                client.logout()
            except Exception:
                pass
        return results

    def smtp_send(self, msg: EmailMessage) -> None:
        if self.config.smtp_use_ssl:
            with smtplib.SMTP_SSL(self.config.smtp_host, self.config.smtp_port, timeout=30) as smtp:
                smtp.login(self.config.smtp_username, self.config.smtp_password)
                smtp.send_message(msg)
            return
        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=30) as smtp:
            if self.config.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.send_message(msg)
