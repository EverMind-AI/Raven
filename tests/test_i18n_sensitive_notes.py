"""Every sensitive reason the approval card can show exists in both locales.

``change_view`` sends the reason's English sentence beside a
``gui.confirm.cfg.why.<path>`` key that the page translates (falling back to the
sentence), so a reason the catalogue never grew renders English to a zh reader --
and the note is the reader's main input for deciding a security-relevant change.
This walks every source of reasons and holds the catalogue to them.

The key must also be the path's declared spelling: a channel field accepts
camelCase and snake_case (``gatewayUrl`` / ``gateway_url``) while the catalogue
carries only the declared one, so a key sent as the call spelled it misses the
same way.
"""

import json
from pathlib import Path

from raven.config import self_surface as surface
from raven.config.update_channels import channel_field_specs, channel_names

_CATALOGUE = json.loads((Path(__file__).resolve().parents[1] / "i18n" / "messages.json").read_text(encoding="utf-8"))[
    "ui"
]


def _reasons() -> dict[str, str]:
    reasons = {
        setting.path: setting.sensitive
        for section in surface.SECTIONS
        for setting in section.settings
        if setting.sensitive
    }
    for name in channel_names():
        for field, spec in channel_field_specs(name).items():
            if spec.get("sensitive"):
                reasons[f"channels.{name}.{field}"] = spec["sensitive"]
    return reasons


def test_every_sensitive_reason_has_both_locales():
    reasons = _reasons()
    assert len(reasons) >= 85, "the catalog and the channel specs are the two sources; a drop here is a lost reason"
    for path, sentence in sorted(reasons.items()):
        entry = _CATALOGUE.get(f"gui.confirm.cfg.why.{path}")
        assert entry is not None, f"no catalogue entry for {path}"
        assert entry.get("en") == sentence, f"the entry for {path} no longer carries the source's sentence"
        assert entry.get("zh"), f"the entry for {path} has no zh"


def test_an_accepted_alias_names_the_declared_key():
    view = surface.change_view({"action": "set", "path": "channels.discord.gatewayUrl", "value": "x"}, {})
    assert view["sensitive_key"] == "channels.discord.gateway_url"
    assert f"gui.confirm.cfg.why.{view['sensitive_key']}" in _CATALOGUE


def test_an_alias_nested_in_an_object_set_names_the_declared_key():
    view = surface.change_view({"action": "set", "path": "channels.discord", "value": {"gatewayUrl": "x"}}, {})
    assert view["sensitive_key"] == "channels.discord.gateway_url"
