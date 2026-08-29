"""Declarative descriptor for the Email channel. Importing this module does not
import the channel implementation (IMAP/SMTP wiring) — that is deferred into the
factory."""

from __future__ import annotations

from raven.channels.contract import Capabilities, ChannelSpec


def _make(config):
    from raven.channels.adapters.email.channel import EmailChannel

    return EmailChannel(config)


SPEC = ChannelSpec(
    display_name="Email",
    factory=_make,
    capabilities=Capabilities(),
    # Cargo declaration (config-with-cargo): the fields only this adapter
    # consumes. Socket fields (enabled / allow_from / workspace) stay with the
    # host. Defaults and secrecy travel with the cargo; the central model
    # mirrors them until it retires.
    config_schema={
        "consent_granted": {"type": "boolean", "default": False},
        "imap_host": {"type": "string", "default": "", "required": True},
        "imap_port": {"type": "integer", "default": 993},
        "imap_username": {"type": "string", "default": "", "required": True},
        "imap_password": {"type": "string", "default": "", "required": True, "secret": True},
        "imap_mailbox": {"type": "string", "default": "INBOX"},
        "imap_use_ssl": {"type": "boolean", "default": True},
        "smtp_host": {"type": "string", "default": "", "required": True},
        "smtp_port": {"type": "integer", "default": 587},
        "smtp_username": {"type": "string", "default": "", "required": True},
        "smtp_password": {"type": "string", "default": "", "required": True, "secret": True},
        "smtp_use_tls": {"type": "boolean", "default": True},
        "smtp_use_ssl": {"type": "boolean", "default": False},
        "from_address": {"type": "string", "default": ""},
        "auto_reply_enabled": {"type": "boolean", "default": True},
        "poll_interval_seconds": {"type": "integer", "default": 30},
        "mark_seen": {"type": "boolean", "default": True},
        "max_body_chars": {"type": "integer", "default": 12000},
        "subject_prefix": {"type": "string", "default": "Re: "},
    },
)
