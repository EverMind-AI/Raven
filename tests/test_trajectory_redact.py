"""Tests for bundle redaction and report packing (`raven.trajectory.redact` / `.report`).

Every secret in this file is a fabricated value; no test reads the user's
real config or asserts on real machine state.
"""

from __future__ import annotations

import json
import tarfile

import pytest

from raven.trajectory import redact as tredact
from raven.trajectory import report as treport

FAKE_KEY = "fk-unit-9Q7zXp2LmV4wRb8KsD3f"
FAKE_LABEL = "config.providers.anthropic.api_key"


def _secrets():
    return [tredact.KnownSecret(FAKE_LABEL, FAKE_KEY)]


def _make_bundle(tmp_path, *, spans_text=None, artifact_text=None):
    bundle = tmp_path / "att-1"
    (bundle / "artifacts").mkdir(parents=True)
    (bundle / "manifest.json").write_text(json.dumps({"attempt_id": "att-1"}), encoding="utf-8")
    (bundle / "spans.jsonl").write_text(
        spans_text if spans_text is not None else json.dumps({"traceId": "trace-1", "content": FAKE_KEY}) + "\n",
        encoding="utf-8",
    )
    (bundle / "artifacts" / "tool.output.json").write_text(
        artifact_text if artifact_text is not None else json.dumps({"stdout": f"key={FAKE_KEY}"}),
        encoding="utf-8",
    )
    return bundle


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _read_all(root):
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ── layer 1: known values ─────────────────────────────────────────────


def test_known_value_replaced_everywhere(tmp_path):
    bundle = _make_bundle(tmp_path)

    report = tredact.redact_bundle(bundle, tmp_path / "red", secrets=_secrets())

    for rel, text in _read_all(report.redacted_dir).items():
        if rel != tredact.REDACTION_METADATA_FILE:
            assert FAKE_KEY not in text, rel
    spans = (report.redacted_dir / "spans.jsonl").read_text(encoding="utf-8")
    assert f"[REDACTED:{FAKE_LABEL}]" in spans
    artifact = (report.redacted_dir / "artifacts" / "tool.output.json").read_text(encoding="utf-8")
    assert f"[REDACTED:{FAKE_LABEL}]" in artifact
    assert report.exact[FAKE_LABEL] == 2


def test_original_bundle_untouched(tmp_path):
    bundle = _make_bundle(tmp_path)
    before = _snapshot(bundle)

    tredact.redact_bundle(bundle, tmp_path / "red", secrets=_secrets())

    assert _snapshot(bundle) == before


def test_json_escaped_secret_cleared(tmp_path):
    secret = 'to"ken\\值-abc123456'
    once = json.dumps(secret, ensure_ascii=False)[1:-1]
    twice = json.dumps(once, ensure_ascii=False)[1:-1]
    ascii_form = json.dumps(secret, ensure_ascii=True)[1:-1]
    spans = f'{{"content": "a {once} b"}}\n{twice}\n{ascii_form}\n'
    bundle = _make_bundle(tmp_path, spans_text=spans, artifact_text="clean")

    report = tredact.redact_bundle(
        bundle, tmp_path / "red", secrets=[tredact.KnownSecret("config.channels.telegram.token", secret)]
    )

    text = (report.redacted_dir / "spans.jsonl").read_text(encoding="utf-8")
    for form in (secret, once, twice, ascii_form):
        assert form not in text
    assert report.exact["config.channels.telegram.token"] >= 3


def test_env_length_floor_only_for_suffix_inferred():
    got = tredact.env_secrets(
        {
            "SMALL_API_KEY": "abc",
            "REAL_API_KEY": "abcdef123456",
            "OPENAI_API_KEY": "abc",
            "ANTHROPIC_API_KEY": "EMPTY",
        }
    )
    # Explicitly listed credential names have no floor; the dummy "EMPTY"
    # stays exempt on api-key-named variables.
    assert {s.label for s in got} == {"env.REAL_API_KEY", "env.OPENAI_API_KEY"}


# ── layer 2: pattern fallback ─────────────────────────────────────────


def test_pattern_fallback(tmp_path):
    pem = "-----BEGIN " + "RSA PRIVATE KEY-----\\nMIIEfakefakefake\\n-----END RSA PRIVATE KEY-----"
    lines = [
        "sk-proj-fake1234567890abcdef",
        "AKIAFAKEFAKEFAKEFAKE",
        "ghp_" + "a1" * 18,
        "xoxb-1234-fakefakefake",
        "Authorization: Bearer fake.token_value-1234567890",
        pem,
    ]
    bundle = _make_bundle(tmp_path, spans_text="\n".join(lines), artifact_text="clean")

    report = tredact.redact_bundle(bundle, tmp_path / "red", secrets=[])

    text = (report.redacted_dir / "spans.jsonl").read_text(encoding="utf-8")
    assert "sk-proj" not in text and "AKIAFAKE" not in text and "ghp_" not in text
    assert "xoxb-" not in text and "Bearer fake" not in text and "MIIEfake" not in text
    for name in (
        "openai-style-key",
        "aws-access-key-id",
        "github-token",
        "slack-token",
        "bearer-token",
        "private-key-block",
    ):
        assert report.patterns[name] == 1, name
        assert f"[REDACTED:pattern.{name}]" in text


# ── layer 3: residual scan ────────────────────────────────────────────


def test_residual_scan_flags_high_entropy_not_ids(tmp_path):
    leftover = "qT7zXp2LmV9wRb4KsD8fGh3J"
    artifact_ref = "artifacts/152100968396-trace-1a01fc2e2b9-1f3bc84c-cli-demo-tool.output-1650fa884e.json"
    spans = (
        json.dumps(
            {
                "traceId": "trace-198f3a2b1c4-a1b2c3d4",
                "content": f"token {leftover} here",
                "tool.output.artifact_path": artifact_ref,
            }
        )
        + "\n"
    )
    bundle = _make_bundle(tmp_path, spans_text=spans, artifact_text="clean")

    report = tredact.redact_bundle(bundle, tmp_path / "red", secrets=[])

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.category == "high-entropy"
    assert finding.file == "spans.jsonl"
    assert leftover not in finding.sample
    assert leftover[:4] in finding.sample and "***" in finding.sample


def test_residual_scan_flags_hex_and_alpha_tokens(tmp_path):
    """Pure-hex and letter-only random tokens are valid credential shapes and
    must be flagged; identifiers and digit-only ids must not drown the preview."""
    hex_token = "a3f9c2b7d8e64a1b9c0d2e5f7a8b3c4d5e6f7a8b9c0d1e2f"
    alpha_token = "qwZxKvPtRmYbNcLdHgFsJaTe"
    sha1 = "5d4e9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e2d"
    lines = [
        hex_token,
        alpha_token,
        "rewritten_artifact_paths",
        "1234567890123456789012",
        f'{{"tool.output.artifact_sha1": "{sha1}"}}',
    ]
    bundle = _make_bundle(tmp_path, spans_text="\n".join(lines), artifact_text="clean")

    report = tredact.redact_bundle(bundle, tmp_path / "red", secrets=[])

    samples = [f.sample for f in report.findings]
    assert len(samples) == 2
    assert any(hex_token[:4] in s for s in samples)
    assert any(alpha_token[:4] in s for s in samples)
    assert not any("rewr" in s for s in samples)
    assert not any("1234" in s for s in samples)
    # The envelope's own artifact checksum is known noise, not a residue.
    assert not any(sha1[:4] in s for s in samples)


def test_residual_scan_reports_never_rewrites(tmp_path):
    leftover = "qT7zXp2LmV9wRb4KsD8fGh3J"
    bundle = _make_bundle(tmp_path, spans_text=leftover, artifact_text="clean")

    report = tredact.redact_bundle(bundle, tmp_path / "red", secrets=[])

    assert (report.redacted_dir / "spans.jsonl").read_text(encoding="utf-8") == leftover


def test_residual_finding_carries_token_and_occurrences(tmp_path):
    leftover = "qT7zXp2LmV9wRb4KsD8fGh3J"
    spans = f"first {leftover} here\nplain line\nagain {leftover}"
    bundle = _make_bundle(tmp_path, spans_text=spans, artifact_text=f"also {leftover}")

    report = tredact.redact_bundle(bundle, tmp_path / "red", secrets=[])

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.token == leftover
    assert finding.count == 3
    assert [(o["file"], o["line_no"]) for o in finding.occurrences] == [
        ("artifacts/tool.output.json", 1),
        ("spans.jsonl", 1),
        ("spans.jsonl", 3),
    ]
    occurrence = finding.occurrences[1]
    assert occurrence["line"] == f"first {leftover} here"
    assert occurrence["line"][occurrence["start"] : occurrence["end"]] == leftover


def test_metadata_serializes_no_token_or_occurrences(tmp_path):
    leftover = "qT7zXp2LmV9wRb4KsD8fGh3J"
    bundle = _make_bundle(tmp_path, spans_text=f"prefix {leftover} suffix", artifact_text="clean")

    report = tredact.redact_bundle(bundle, tmp_path / "red", secrets=[])

    meta = json.loads((report.redacted_dir / tredact.REDACTION_METADATA_FILE).read_text(encoding="utf-8"))
    (entry,) = meta["residual_findings"]
    assert set(entry) == {"category", "sample", "file", "count"}
    assert leftover not in json.dumps(meta)


def test_residual_scan_exempts_provider_call_ids(tmp_path):
    call_id = "call_9Q7zXp2LmV4wRb8KsD3fT6yH"
    hyphenated = "call-9Q7zXp2LmV4wRb8KsD3fT6yH"
    prefixed = "Xcall_9Q7zXp2LmV4wRb8KsD3fT6yH"
    bundle = _make_bundle(tmp_path, spans_text=f"{call_id}\n{hyphenated}\n{prefixed}", artifact_text="clean")

    report = tredact.redact_bundle(bundle, tmp_path / "red", secrets=[])

    tokens = {f.token for f in report.findings}
    assert call_id not in tokens
    assert hyphenated in tokens
    assert prefixed in tokens


# ── binary policy ─────────────────────────────────────────────────────


def test_binary_file_excluded_and_recorded(tmp_path):
    bundle = _make_bundle(tmp_path)
    (bundle / "artifacts" / "blob.bin").write_bytes(b"\xff\xfe\x00" + FAKE_KEY.encode())

    report = tredact.redact_bundle(bundle, tmp_path / "red", secrets=_secrets())

    assert not (report.redacted_dir / "artifacts" / "blob.bin").exists()
    assert report.skipped_binaries == ["artifacts/blob.bin"]
    meta = json.loads((report.redacted_dir / tredact.REDACTION_METADATA_FILE).read_text(encoding="utf-8"))
    assert meta["skipped_binaries"] == ["artifacts/blob.bin"]
    assert "excluded" in meta["binary_policy"]


# ── placeholders + metadata ───────────────────────────────────────────


def test_placeholder_stable_across_runs(tmp_path):
    bundle = _make_bundle(tmp_path)

    first = tredact.redact_bundle(bundle, tmp_path / "red1", secrets=_secrets())
    second = tredact.redact_bundle(bundle, tmp_path / "red2", secrets=_secrets())

    a = (first.redacted_dir / "spans.jsonl").read_text(encoding="utf-8")
    b = (second.redacted_dir / "spans.jsonl").read_text(encoding="utf-8")
    assert a == b and f"[REDACTED:{FAKE_LABEL}]" in a


def test_metadata_records_layer_counts(tmp_path):
    bundle = _make_bundle(tmp_path, spans_text=f"{FAKE_KEY} sk-proj-fake1234567890abcdef", artifact_text="clean")

    report = tredact.redact_bundle(bundle, tmp_path / "red", secrets=_secrets())

    meta = json.loads((report.redacted_dir / tredact.REDACTION_METADATA_FILE).read_text(encoding="utf-8"))
    assert meta["exact_replacements"] == {FAKE_LABEL: 1}
    assert meta["pattern_replacements"] == {"openai-style-key": 1}
    assert meta["config_secrets_loaded"] is True
    assert meta["raven_version"]


def test_dest_constraints(tmp_path):
    bundle = _make_bundle(tmp_path)
    (tmp_path / "exists").mkdir()
    with pytest.raises(ValueError):
        tredact.redact_bundle(bundle, tmp_path / "exists", secrets=[])
    with pytest.raises(ValueError):
        tredact.redact_bundle(bundle, bundle / "inner", secrets=[])
    with pytest.raises(ValueError):
        tredact.redact_bundle(tmp_path / "no-such-bundle", tmp_path / "red", secrets=[])


# ── secret collection ─────────────────────────────────────────────────


def test_config_secret_collection():
    from raven.config.schema import Config

    config = Config.model_validate(
        {
            "providers": {
                "anthropic": {"apiKey": "fk-anthropic-123456"},
                "gemini": {"apiKeyList": ["fk-gemini-a-123456", "fk-gemini-b-123456"]},
                "openai": {
                    "extraHeaders": {"APP-Code": "fk-header-123456"},
                    "endpoints": [{"label": "e1", "extraHeaders": {"APP-Code": "fk-endpoint-header-1"}}],
                },
                "hostedVllm": {"apiKey": "EMPTY"},
            },
            "channels": {
                "telegram": {"token": "fk-telegram-123456"},
                "email": {"imapPassword": "abc12", "smtpPassword": "EMPTY"},
            },
            "routing": {"models": [{"model": "m", "apiBase": "http://x", "apiKey": "EMPTY"}]},
            "tools": {
                "mcpServers": {
                    "srv": {
                        "env": {"MY_API_KEY": "fk-mcp-123456", "PATH": "/usr/bin:/bin"},
                        "headers": {"X-Custom-Auth": "fk-mcp-header-1"},
                    }
                }
            },
        }
    )

    got = {s.label: s.value for s in tredact.config_secrets(config)}

    assert got["config.providers.anthropic.api_key"] == "fk-anthropic-123456"
    assert got["config.providers.gemini.api_key_list[0]"] == "fk-gemini-a-123456"
    assert got["config.providers.gemini.api_key_list[1]"] == "fk-gemini-b-123456"
    assert got["config.providers.openai.extra_headers.APP-Code"] == "fk-header-123456"
    assert got["config.channels.telegram.token"] == "fk-telegram-123456"
    assert got["config.tools.mcp_servers.srv.env.MY_API_KEY"] == "fk-mcp-123456"
    # Arbitrary-named header values are credentials wherever the dict sits.
    assert got["config.providers.openai.endpoints[0].extra_headers.APP-Code"] == "fk-endpoint-header-1"
    assert got["config.tools.mcp_servers.srv.headers.X-Custom-Auth"] == "fk-mcp-header-1"
    # Schema-confirmed secrets have no length floor.
    assert got["config.channels.email.imap_password"] == "abc12"
    # "EMPTY" is exempt only under api-key fields; as a password it is real.
    assert got["config.channels.email.smtp_password"] == "EMPTY"
    assert "config.routing.models[0].api_key" not in got
    assert "config.providers.hosted_vllm.api_key" not in got
    assert "/usr/bin:/bin" not in got.values()


def test_single_char_secret_only_replaces_standalone_tokens(tmp_path):
    bundle = _make_bundle(
        tmp_path,
        spans_text=f'{{"session.key": "cli:a", "marker": "ok", "api_key": "k", "content": "{FAKE_KEY}"}}',
    )

    report = tredact.redact_bundle(
        bundle,
        tmp_path / "red",
        secrets=[tredact.KnownSecret(FAKE_LABEL, FAKE_KEY), tredact.KnownSecret("env.DEEPSEEK_API_KEY", "k")],
    )

    text = (report.redacted_dir / "spans.jsonl").read_text(encoding="utf-8")
    assert f"[REDACTED:{FAKE_LABEL}]" in text
    assert '"session.key": "cli:a"' in text
    assert '"marker": "ok"' in text
    assert '"api_key": "[REDACTED:env.DEEPSEEK_API_KEY]"' in text
    assert report.exact["env.DEEPSEEK_API_KEY"] == 1


def test_short_config_secret_replaced(tmp_path):
    bundle = _make_bundle(tmp_path, spans_text='{"content": "the password is abc12"}', artifact_text="clean")

    report = tredact.redact_bundle(
        bundle, tmp_path / "red", secrets=[tredact.KnownSecret("config.channels.email.imap_password", "abc12")]
    )

    text = (report.redacted_dir / "spans.jsonl").read_text(encoding="utf-8")
    assert "abc12" not in text
    assert "[REDACTED:config.channels.email.imap_password]" in text


def test_collect_covers_extension_blocks_and_both_configs(tmp_path, monkeypatch):
    """Raw-walk coverage: extension blocks the loader strips, plus the union of
    the caller-named config and the default config."""
    default_cfg = tmp_path / "default.json"
    default_cfg.write_text(
        json.dumps(
            {
                "providers": {"anthropic": {"apiKey": "fk-default-key-123456"}},
                "memory": {"embeddingApiKey": "fk-embedding-key-123456"},
            }
        ),
        encoding="utf-8",
    )
    alt_cfg = tmp_path / "alt.json"
    alt_cfg.write_text(json.dumps({"providers": {"openai": {"apiKey": "fk-alternate-key-123456"}}}), encoding="utf-8")
    monkeypatch.setattr("raven.config.loader._current_config_path", default_cfg)

    secrets, complete = tredact.collect_known_secrets(config_path=alt_cfg, environ={})

    values = {s.value for s in secrets}
    assert complete is True
    assert {"fk-default-key-123456", "fk-embedding-key-123456", "fk-alternate-key-123456"} <= values


def test_placeholder_exemption_scoped_to_api_key_fields(tmp_path, monkeypatch):
    """A password literally set to "EMPTY" is a real credential; only api-key
    fields treat that value as the keyless-endpoint dummy."""
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "channels": {"email": {"imapPassword": "EMPTY"}},
                "providers": {"hostedVllm": {"apiKey": "EMPTY"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("raven.config.loader._current_config_path", cfg)

    secrets, complete = tredact.collect_known_secrets(environ={})

    assert complete is True
    labels = {s.label for s in secrets if s.value == "EMPTY"}
    assert labels == {"config.channels.email.imap_password"}


def test_collect_flags_unreadable_config(tmp_path, monkeypatch):
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr("raven.config.loader._current_config_path", broken)

    secrets, complete = tredact.collect_known_secrets(environ={"SOME_API_KEY": "fk-env-still-works-1"})

    assert complete is False
    assert {s.value for s in secrets} == {"fk-env-still-works-1"}


def test_env_secret_collection():
    environ = {
        "ANTHROPIC_API_KEY": "fk-env-anthropic-123",
        "CUSTOM_THING_TOKEN": "fk-env-custom-123",
        "HOME": "/Users/somebody",
        "AWS_SECRET_ACCESS_KEY": "fk-env-aws-123",
    }
    got = {s.label: s.value for s in tredact.env_secrets(environ)}
    assert got == {
        "env.ANTHROPIC_API_KEY": "fk-env-anthropic-123",
        "env.CUSTOM_THING_TOKEN": "fk-env-custom-123",
        "env.AWS_SECRET_ACCESS_KEY": "fk-env-aws-123",
    }


# ── report packing ────────────────────────────────────────────────────


def test_pack_report_roundtrip(tmp_path):
    bundle = _make_bundle(tmp_path)
    report = tredact.redact_bundle(bundle, tmp_path / "red" / "att-1", secrets=_secrets())

    tarball = treport.pack_report(report.redacted_dir, tmp_path / "out" / "att-1.tar.gz")

    with tarfile.open(tarball, "r:gz") as tar:
        names = tar.getnames()
    assert "att-1/manifest.json" in names
    assert f"att-1/{tredact.REDACTION_METADATA_FILE}" in names


def test_get_uploader_local_and_unknown(tmp_path):
    uploader = treport.get_uploader("local")
    assert uploader.name == "local"
    assert uploader.upload(tmp_path / "x.tar.gz", metadata={}) == str(tmp_path / "x.tar.gz")
    with pytest.raises(ValueError):
        treport.get_uploader("http")


# ── end to end ────────────────────────────────────────────────────────


def test_end_to_end_report_with_real_tracer(tmp_path, monkeypatch):
    """Trace with a fake key -> save -> report -> the tarball holds no trace of it."""
    from typer.testing import CliRunner

    from raven.cli.trajectory_commands import trajectory_app
    from raven.tracing import spans as _spans
    from raven.tracing import trace

    fake_env_key = "fk-e2e-3f9c2b7a8d6e4a1b9c0dZ"
    fake_config_key = "fk-cfg-7b1a9d4e2c8f6a3b5d0eZ"
    state = tmp_path / "traces"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"providers": {"anthropic": {"apiKey": fake_config_key}}}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    monkeypatch.setenv("FAKE_E2E_API_KEY", fake_env_key)
    # A leaked junk credential (provider tests write these into os.environ)
    # must not corrupt the placeholders of the real secrets.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr("raven.config.loader._current_config_path", config_path)
    monkeypatch.setattr("raven.trajectory.bundle._default_workspace", lambda: tmp_path / "ws")
    _spans._store = None
    try:
        # A single turn is addressed by its trace id (attempt id = trace id).
        with trace.span("session.turn", session_key="cli:e2e") as root:
            with trace.span("tool.call") as s:
                s.artifact("tool.output", {"stdout": f"auth={fake_env_key} cfg={fake_config_key}"})
        aid = root.trace_id

        out = tmp_path / "report.tar.gz"
        result = CliRunner().invoke(trajectory_app, ["report", aid, "--yes", "--out", str(out)])

        assert result.exit_code == 0, result.output
        assert out.exists()
        extracted = tmp_path / "unpacked"
        with tarfile.open(out, "r:gz") as tar:
            tar.extractall(extracted, filter="data")
        texts = _read_all(extracted)
        assert texts, "tarball was empty"
        for rel, text in texts.items():
            assert fake_env_key not in text, rel
            assert fake_config_key not in text, rel
        joined = "".join(texts.values())
        assert "[REDACTED:env.FAKE_E2E_API_KEY]" in joined
        assert "[REDACTED:config.providers.anthropic.api_key]" in joined
        meta = json.loads(texts[f"{aid}/{tredact.REDACTION_METADATA_FILE}"])
        assert meta["exact_replacements"]["env.FAKE_E2E_API_KEY"] >= 1
        assert meta["exact_replacements"]["config.providers.anthropic.api_key"] >= 1
    finally:
        _spans._store = None
