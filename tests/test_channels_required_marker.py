"""Guard: channel ``required`` markers stay aligned with adapter ``start()`` guards.

Each channel's required set below is the audited truth — the credentials whose
absence makes the adapter bail on startup (see the referenced guard). The schema
markers (``Field(json_schema_extra={"required": True})``) must match this table
exactly. When an adapter changes what it enforces, update the adapter, the schema
marker, and this table in the same change.
"""

from __future__ import annotations

import pytest

from raven.config.update_channels import channel_field_specs

# channel -> required fields, each backed by the adapter guard that proves it.
EXPECTED_REQUIRED: dict[str, set[str]] = {
    "feishu": {"app_id", "app_secret"},  # feishu/channel.py: if not app_id or not app_secret: return
    "telegram": {"token"},  # telegram/channel.py: if not self.config.token
    "discord": {"token"},  # discord/channel.py: if not self.config.token
    "dingtalk": {"client_id", "client_secret"},  # dingtalk/channel.py: if not client_id or not client_secret
    "qq": {"app_id", "secret"},  # qq/channel.py: if not app_id or not secret
    "slack": {"bot_token", "app_token"},  # slack/channel.py: if not bot_token or not app_token
    "wecom": {"bot_id", "secret"},  # wecom/channel.py: if not bot_id or not secret
    "mochat": {"claw_token"},  # mochat/channel.py: if not self.config.claw_token
    "email": {  # email/channel.py _validate_config(): all six must be set
        "imap_host",
        "imap_username",
        "imap_password",
        "smtp_host",
        "smtp_username",
        "smtp_password",
    },
    # Judgment calls (no hard start() guard) — see the plan for rationale.
    "matrix": {"access_token", "user_id"},  # fed into AsyncClient; effectively required to connect
    "whatsapp": set(),  # local bridge; bridge_token auto-generated when empty
    "weixin": set(),  # credentials arrive via `raven channels login weixin`
}


@pytest.mark.parametrize("channel, expected", sorted(EXPECTED_REQUIRED.items()))
def test_required_markers_match_audit(channel: str, expected: set[str]) -> None:
    specs = channel_field_specs(channel)
    marked = {path for path, spec in specs.items() if spec.get("required")}
    assert marked == expected
