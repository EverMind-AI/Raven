"""Unit tests for ``raven.config.update_channels`` — the channel write path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.config.update_channels import (
    channel_field_specs,
    disable_channel,
    enable_channel,
    get_channel_config,
    reset_channel,
    set_channel_fields,
)


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    """Provide a sandboxed config path; never touch the real ~/.raven."""
    return tmp_path / "config.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# enable_channel
# ---------------------------------------------------------------------------


def test_enable_simple_channel_with_token(cfg_path: Path) -> None:
    enable_channel("telegram", {"token": "123:AAAA"}, config_path=cfg_path)

    raw = _read(cfg_path)
    assert raw["channels"]["telegram"]["enabled"] is True
    assert raw["channels"]["telegram"]["token"] == "123:AAAA"


def test_enable_complex_channel_feishu(cfg_path: Path) -> None:
    enable_channel(
        "feishu",
        {"app_id": "cli_xxx", "app_secret": "sec_yyy", "encrypt_key": "ekey"},
        config_path=cfg_path,
    )

    section = _read(cfg_path)["channels"]["feishu"]
    assert section["enabled"] is True
    assert section["appId"] == "cli_xxx"
    assert section["appSecret"] == "sec_yyy"
    assert section["encryptKey"] == "ekey"


def test_enable_unknown_channel_raises(cfg_path: Path) -> None:
    with pytest.raises(KeyError, match="Unknown channel 'foobar'"):
        enable_channel("foobar", {}, config_path=cfg_path)


def test_enable_nested_dotted_path_slack_dm(cfg_path: Path) -> None:
    enable_channel(
        "slack",
        {"bot_token": "xoxb-x", "app_token": "xapp-y", "dm.policy": "allowlist"},
        config_path=cfg_path,
    )

    section = _read(cfg_path)["channels"]["slack"]
    assert section["enabled"] is True
    assert section["botToken"] == "xoxb-x"
    assert section["appToken"] == "xapp-y"
    assert section["dm"]["policy"] == "allowlist"


# ---------------------------------------------------------------------------
# set_channel_fields
# ---------------------------------------------------------------------------


def test_set_unknown_field_raises_with_helpful_message(cfg_path: Path) -> None:
    with pytest.raises(KeyError) as exc_info:
        set_channel_fields("telegram", {"tokn": "xxx"}, config_path=cfg_path)
    msg = str(exc_info.value)
    assert "tokn" in msg
    assert "token" in msg
    assert "Available fields" in msg


def test_set_invalid_value_raises_validation_error(cfg_path: Path) -> None:
    with pytest.raises(ValidationError):
        set_channel_fields("telegram", {"group_policy": "definitely_not_a_valid_literal"}, config_path=cfg_path)


def test_set_returns_previous_values(cfg_path: Path) -> None:
    enable_channel("telegram", {"token": "first"}, config_path=cfg_path)
    prev = set_channel_fields("telegram", {"token": "second"}, config_path=cfg_path)
    assert prev == {"token": "first"}
    assert _read(cfg_path)["channels"]["telegram"]["token"] == "second"


def test_set_coerces_bool_from_string(cfg_path: Path) -> None:
    set_channel_fields("telegram", {"reply_to_message": "true"}, config_path=cfg_path)
    section = _read(cfg_path)["channels"]["telegram"]
    assert section["replyToMessage"] is True


def test_set_coerces_csv_to_list(cfg_path: Path) -> None:
    set_channel_fields("telegram", {"allow_from": "alice,bob,carol"}, config_path=cfg_path)
    section = _read(cfg_path)["channels"]["telegram"]
    assert section["allowFrom"] == ["alice", "bob", "carol"]


def test_set_coerces_json_list(cfg_path: Path) -> None:
    set_channel_fields("slack", {"dm.allow_from": '["U1","U2"]'}, config_path=cfg_path)
    section = _read(cfg_path)["channels"]["slack"]
    assert section["dm"]["allowFrom"] == ["U1", "U2"]


# ---------------------------------------------------------------------------
# get_channel_config
# ---------------------------------------------------------------------------


def test_get_redacts_secret_fields(cfg_path: Path) -> None:
    enable_channel("telegram", {"token": "real_bot_token"}, config_path=cfg_path)
    cfg = get_channel_config("telegram", config_path=cfg_path)
    assert cfg["token"] == "****set****"
    assert "real_bot_token" not in str(cfg)


def test_get_shows_empty_for_unset_secret(cfg_path: Path) -> None:
    enable_channel("feishu", {"app_id": "X"}, config_path=cfg_path)
    cfg = get_channel_config("feishu", config_path=cfg_path)
    assert cfg["app_secret"] == "(empty)"
    assert cfg["app_id"] == "X"  # non-secret, unredacted


def test_get_show_secrets_disabled(cfg_path: Path) -> None:
    enable_channel("telegram", {"token": "real_value"}, config_path=cfg_path)
    cfg = get_channel_config("telegram", redact_secrets=False, config_path=cfg_path)
    assert cfg["token"] == "real_value"


# ---------------------------------------------------------------------------
# disable_channel / reset_channel
# ---------------------------------------------------------------------------


def test_disable_preserves_other_fields(cfg_path: Path) -> None:
    enable_channel(
        "telegram",
        {"token": "keep_me", "proxy": "http://127.0.0.1:7890"},
        config_path=cfg_path,
    )
    disable_channel("telegram", config_path=cfg_path)

    section = _read(cfg_path)["channels"]["telegram"]
    assert section["enabled"] is False
    assert section["token"] == "keep_me"
    assert section["proxy"] == "http://127.0.0.1:7890"


def test_reset_keeps_key_clears_values(cfg_path: Path) -> None:
    enable_channel("telegram", {"token": "abc"}, config_path=cfg_path)
    assert _read(cfg_path)["channels"]["telegram"]["token"] == "abc"

    reset_channel("telegram", config_path=cfg_path)
    section = _read(cfg_path)["channels"]["telegram"]
    assert section["enabled"] is False
    assert section["token"] == ""


# ---------------------------------------------------------------------------
# Atomicity / integrity
# ---------------------------------------------------------------------------


def test_atomic_write_no_corruption_on_validation_error(cfg_path: Path) -> None:
    enable_channel("telegram", {"token": "original"}, config_path=cfg_path)
    before = _read(cfg_path)

    with pytest.raises(ValidationError):
        set_channel_fields("telegram", {"group_policy": "garbage_literal"}, config_path=cfg_path)

    assert _read(cfg_path) == before  # nothing got partially written


def test_camelcase_round_trip(cfg_path: Path) -> None:
    enable_channel("feishu", {"app_id": "X", "app_secret": "Y"}, config_path=cfg_path)

    raw = _read(cfg_path)["channels"]["feishu"]
    assert "appId" in raw and "appSecret" in raw
    assert "app_id" not in raw and "app_secret" not in raw

    cfg = get_channel_config("feishu", redact_secrets=False, config_path=cfg_path)
    assert "app_id" in cfg and "app_secret" in cfg
    assert "appId" not in cfg


# ---------------------------------------------------------------------------
# channel_field_specs reflection
# ---------------------------------------------------------------------------


def test_field_specs_flattens_nested_slack_dm() -> None:
    specs = channel_field_specs("slack")
    assert "dm.enabled" in specs
    assert "dm.policy" in specs
    assert "dm.allow_from" in specs
    assert "dm" not in specs  # parent model node should not appear as a leaf


def test_field_specs_marks_known_secrets() -> None:
    assert channel_field_specs("telegram")["token"]["is_secret"] is True
    assert channel_field_specs("feishu")["app_secret"]["is_secret"] is True
    assert channel_field_specs("feishu")["encrypt_key"]["is_secret"] is True
    assert channel_field_specs("feishu")["app_id"]["is_secret"] is False
    assert channel_field_specs("slack")["bot_token"]["is_secret"] is True
    assert channel_field_specs("slack")["dm.policy"]["is_secret"] is False


def test_field_specs_marks_required() -> None:
    fs = channel_field_specs("feishu")
    assert fs["app_id"]["required"] is True
    assert fs["app_secret"]["required"] is True
    # Optional credential fields must NOT be marked required.
    assert fs["encrypt_key"]["required"] is False
    assert fs["verification_token"]["required"] is False
    # Non-credential fields (defaults / lists / literals) are optional too.
    assert fs["allow_from"]["required"] is False
    assert channel_field_specs("telegram")["token"]["required"] is True
    # Channels with no hard-required field carry no required marker.
    assert all(not s["required"] for s in channel_field_specs("whatsapp").values())


def test_field_specs_unknown_channel_raises() -> None:
    with pytest.raises(KeyError, match="Unknown channel 'foobar'"):
        channel_field_specs("foobar")


# ---------------------------------------------------------------------------
# Coverage across every channel registered in ChannelsConfig
# ---------------------------------------------------------------------------


def _all_channel_names() -> list[str]:
    from pydantic import BaseModel

    from raven.config.schema import ChannelsConfig

    out: list[str] = []
    for fname, finfo in ChannelsConfig.model_fields.items():
        ann = finfo.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out.append(fname)
    return out


ALL_CHANNELS = _all_channel_names()


@pytest.mark.parametrize("name", ALL_CHANNELS)
def test_every_channel_has_specs_with_enabled(name: str) -> None:
    """Every channel must reflect cleanly and expose an ``enabled`` field."""
    specs = channel_field_specs(name)
    assert "enabled" in specs
    assert specs["enabled"]["type"] == "bool"
    assert specs["enabled"]["default"] is False


@pytest.mark.parametrize("name", ALL_CHANNELS)
def test_every_channel_enable_disable_reset_round_trip(name: str, cfg_path: Path) -> None:
    """Closed-loop enable -> disable -> reset works for every channel."""
    enable_channel(name, config_path=cfg_path)
    assert _read(cfg_path)["channels"][name]["enabled"] is True

    disable_channel(name, config_path=cfg_path)
    assert _read(cfg_path)["channels"][name]["enabled"] is False

    reset_channel(name, config_path=cfg_path)
    section = _read(cfg_path)["channels"][name]
    assert section["enabled"] is False


@pytest.mark.parametrize("name", ALL_CHANNELS)
def test_every_channel_get_returns_all_spec_keys(name: str, cfg_path: Path) -> None:
    """``get_channel_config`` must surface every key advertised by ``channel_field_specs``."""
    enable_channel(name, config_path=cfg_path)
    cfg = get_channel_config(name, config_path=cfg_path)
    assert set(cfg.keys()) == set(channel_field_specs(name).keys())


def test_mochat_groups_dict_field_round_trip(cfg_path: Path) -> None:
    """``mochat.groups`` is the only ``dict[str, BaseModel]`` field in the schema.

    Verify it round-trips via JSON-encoded input and stays untouched by
    unrelated patches.
    """
    enable_channel(
        "mochat",
        {"groups": '{"chat42": {"requireMention": true}}', "claw_token": "tk"},
        config_path=cfg_path,
    )
    section = _read(cfg_path)["channels"]["mochat"]
    assert section["groups"] == {"chat42": {"requireMention": True}}
    assert section["clawToken"] == "tk"

    set_channel_fields("mochat", {"agent_user_id": "ua"}, config_path=cfg_path)
    section = _read(cfg_path)["channels"]["mochat"]
    assert section["groups"] == {"chat42": {"requireMention": True}}  # preserved


def test_writing_channel_a_preserves_channel_b(cfg_path: Path) -> None:
    """Patching channel A must leave channel B's existing config byte-for-byte intact."""
    enable_channel("slack", {"bot_token": "xoxb", "app_token": "xapp"}, config_path=cfg_path)
    slack_before = _read(cfg_path)["channels"]["slack"]

    set_channel_fields("telegram", {"token": "tg_value"}, config_path=cfg_path)
    slack_after = _read(cfg_path)["channels"]["slack"]
    assert slack_after == slack_before

    disable_channel("feishu", config_path=cfg_path)
    slack_final = _read(cfg_path)["channels"]["slack"]
    assert slack_final == slack_before

    reset_channel("discord", config_path=cfg_path)
    slack_post_reset = _read(cfg_path)["channels"]["slack"]
    assert slack_post_reset == slack_before


def test_mochat_nested_mention_dotted_path(cfg_path: Path) -> None:
    """``mochat.mention.require_in_groups`` is the second nested dotted path
    in the schema (besides ``slack.dm.*``). Verify it round-trips."""
    enable_channel(
        "mochat",
        {"mention.require_in_groups": "true", "claw_token": "tk"},
        config_path=cfg_path,
    )
    section = _read(cfg_path)["channels"]["mochat"]
    assert section["mention"]["requireInGroups"] is True
    assert section["clawToken"] == "tk"

    set_channel_fields("mochat", {"mention.require_in_groups": "false"}, config_path=cfg_path)
    section = _read(cfg_path)["channels"]["mochat"]
    assert section["mention"]["requireInGroups"] is False


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_bool_coerce_string_forms(cfg_path: Path, raw: str, expected: bool) -> None:
    set_channel_fields("telegram", {"reply_to_message": raw}, config_path=cfg_path)
    assert _read(cfg_path)["channels"]["telegram"]["replyToMessage"] is expected


def test_literal_choices_appear_in_description() -> None:
    """``Literal[...]`` fields without a Field(description=) get a synthetic
    description with the choice list, so CLI consumers can surface valid values.
    """
    specs = channel_field_specs("telegram")
    assert specs["group_policy"]["type"] == "Literal"
    desc = specs["group_policy"]["description"]
    assert "open" in desc
    assert "mention" in desc


def test_email_two_secrets_both_redacted(cfg_path: Path) -> None:
    """email has two distinct password fields; both must redact."""
    enable_channel(
        "email",
        {"imap_password": "p1", "smtp_password": "p2", "imap_host": "imap.x"},
        config_path=cfg_path,
    )
    cfg = get_channel_config("email", config_path=cfg_path)
    assert cfg["imap_password"] == "****set****"
    assert cfg["smtp_password"] == "****set****"
    assert cfg["imap_host"] == "imap.x"  # non-secret stays plain


def test_malformed_config_refuses_write_and_preserves_file(cfg_path: Path) -> None:
    # REGRESSION: a present-but-unparseable config must NOT be clobbered by a
    # write command (the bug that wiped a real config down to one section).
    from raven.config.loader import ConfigReadError

    original = '{\n  "channels": {"telegram": {"enabled": true}},\n  // comment => invalid JSON\n}\n'
    cfg_path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigReadError):
        enable_channel("telegram", {"token": "x"}, config_path=cfg_path)
    assert cfg_path.read_text(encoding="utf-8") == original  # untouched
    with pytest.raises(ConfigReadError):
        set_channel_fields("telegram", {"token": "y"}, config_path=cfg_path)
    assert cfg_path.read_text(encoding="utf-8") == original
