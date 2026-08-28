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
    # host. Defaults stay in the central model until the storage handover.
    config_schema={
        "consent_granted": {"type": "boolean"},
        "imap_host": {"type": "string", "required": True},
        "imap_port": {"type": "integer"},
        "imap_username": {"type": "string", "required": True},
        "imap_password": {"type": "string", "required": True},
        "imap_mailbox": {"type": "string"},
        "imap_use_ssl": {"type": "boolean"},
        "smtp_host": {"type": "string", "required": True},
        "smtp_port": {"type": "integer"},
        "smtp_username": {"type": "string", "required": True},
        "smtp_password": {"type": "string", "required": True},
        "smtp_use_tls": {"type": "boolean"},
        "smtp_use_ssl": {"type": "boolean"},
        "from_address": {"type": "string"},
        "auto_reply_enabled": {"type": "boolean"},
        "poll_interval_seconds": {"type": "integer"},
        "mark_seen": {"type": "boolean"},
        "max_body_chars": {"type": "integer"},
        "subject_prefix": {"type": "string"},
    },
)
