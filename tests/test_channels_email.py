"""Tests for the email adapter package.

parsing.py — pure formatting/extraction/assembly helpers.
mailbox.py — IMAP search/fetch + SMTP send (imaplib/smtplib mocked).
channel.py — poll orchestration, UID dedup, reply send.

Real IMAP/SMTP round-trips are live flows left to integration/manual testing.
"""

from datetime import date
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from raven.channels.adapters.email import parsing
from raven.channels.adapters.email.channel import EmailChannel
from raven.channels.adapters.email.mailbox import EmailMailbox


def _cfg(**overrides):
    cfg = SimpleNamespace(
        imap_host="", imap_port=993, imap_use_ssl=True, imap_mailbox="INBOX",
        imap_username="", imap_password="",
        smtp_host="", smtp_port=587, smtp_use_ssl=False, smtp_use_tls=True,
        smtp_username="", smtp_password="",
        subject_prefix="Re: ", max_body_chars=4000, mark_seen=True,
        allow_from=["*"],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _channel(**overrides):
    return EmailChannel(_cfg(**overrides))


# ── parsing: formatting / extraction ──────────────────────────────────


def test_format_imap_date():
    assert parsing.format_imap_date(date(2026, 6, 4)) == "04-Jun-2026"
    assert parsing.format_imap_date(date(2026, 1, 15)) == "15-Jan-2026"
    assert parsing.format_imap_date(date(2026, 12, 31)) == "31-Dec-2026"


def test_extract_message_bytes():
    fetched = [(b"1 (UID 42 BODY[] {3}", b"raw"), b")"]
    assert parsing.extract_message_bytes(fetched) == b"raw"
    assert parsing.extract_message_bytes([b")"]) is None
    assert parsing.extract_message_bytes([(b"head",)]) is None


def test_extract_uid():
    fetched = [(b"1 (UID 42 BODY[] {3}", b"raw"), b")"]
    assert parsing.extract_uid(fetched) == "42"
    assert parsing.extract_uid([(b"1 (FLAGS ())", b"raw")]) == ""
    assert parsing.extract_uid([b")"]) == ""


def test_decode_header_value():
    assert parsing.decode_header_value("") == ""
    assert parsing.decode_header_value("Plain Subject") == "Plain Subject"
    # RFC 2047 encoded-word (UTF-8 base64 of "héllo")
    assert parsing.decode_header_value("=?utf-8?b?aMOpbGxv?=") == "héllo"


def test_html_to_text():
    assert parsing.html_to_text("a<br>b") == "a\nb"
    assert parsing.html_to_text("<p>x</p><p>y</p>") == "x\ny\n"
    assert parsing.html_to_text("<b>bold</b> &amp; <i>it</i>") == "bold & it"


def test_extract_text_body_plain():
    msg = EmailMessage()
    msg.set_content("hello world")
    assert parsing.extract_text_body(msg) == "hello world"


def test_extract_text_body_html_only():
    msg = EmailMessage()
    msg.set_content("<p>hi there</p>", subtype="html")
    assert parsing.extract_text_body(msg) == "hi there"


def test_extract_text_body_multipart_prefers_plain():
    msg = EmailMessage()
    msg.set_content("plain version")
    msg.add_alternative("<p>html version</p>", subtype="html")
    assert parsing.extract_text_body(msg) == "plain version"


def test_extract_text_body_multipart_skips_attachment():
    msg = EmailMessage()
    msg.set_content("body text")
    msg.add_attachment(b"\x00\x01", maintype="application", subtype="octet-stream", filename="blob.bin")
    assert parsing.extract_text_body(msg) == "body text"


def test_reply_subject():
    assert parsing.reply_subject("Hello") == "Re: Hello"
    assert parsing.reply_subject("Re: Hello") == "Re: Hello"      # idempotent
    assert parsing.reply_subject("RE: shouty") == "RE: shouty"    # case-insensitive
    assert parsing.reply_subject("") == "Re: Raven reply"
    assert parsing.reply_subject("x", "回复: ") == "回复: x"


# ── parsing: parse_message ─────────────────────────────────────────────


def test_parse_message_full():
    raw = (b"From: Alice <a@x.com>\r\nSubject: Hi there\r\n"
           b"Date: Mon, 01 Jun 2026 10:00:00 +0000\r\nMessage-ID: <mid-1>\r\n\r\nhello body")
    item = parsing.parse_message(raw, 4000, uid="42")
    assert item["sender"] == "a@x.com"
    assert item["subject"] == "Hi there"
    assert item["message_id"] == "<mid-1>"
    assert "hello body" in item["content"]
    assert item["metadata"] == {
        "message_id": "<mid-1>", "subject": "Hi there",
        "date": "Mon, 01 Jun 2026 10:00:00 +0000", "sender_email": "a@x.com", "uid": "42",
    }


def test_parse_message_no_sender_dropped():
    assert parsing.parse_message(b"Subject: x\r\n\r\nbody", 4000) is None


def test_parse_message_truncates_body():
    raw = b"From: a@x.com\r\nSubject: s\r\n\r\n" + b"Z" * 100
    item = parsing.parse_message(raw, 10)
    body = item["content"].split("\n\n", 1)[1]
    assert body == "Z" * 10


def test_parse_message_empty_body_placeholder():
    item = parsing.parse_message(b"From: a@x.com\r\nSubject: s\r\n\r\n", 4000)
    assert "(empty email body)" in item["content"]


# ── mailbox: IMAP search/fetch + SMTP send (mocked) ────────────────────


class _FakeIMAP:
    search_result = b"1 2"

    def __init__(self, host, port):
        self.stored = []
        self.fetched_ids = []
        self.logged_out = False

    def login(self, user, pw):
        pass

    def select(self, mailbox):
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        return ("OK", [self.search_result])

    def fetch(self, imap_id, spec):
        self.fetched_ids.append(imap_id)
        n = imap_id.decode()
        header = f"{n} (UID 1{n} BODY[] {{4}}".encode()
        return ("OK", [(header, f"raw{n}".encode()), b")"])

    def store(self, imap_id, flag, value):
        self.stored.append((imap_id, value))

    def logout(self):
        self.logged_out = True


def test_mailbox_search_fetch_marks_seen(monkeypatch):
    created = {}
    def fake_ssl(host, port):
        created["client"] = _FakeIMAP(host, port)
        return created["client"]
    monkeypatch.setattr("raven.channels.adapters.email.mailbox.imaplib.IMAP4_SSL", fake_ssl)

    mb = EmailMailbox(_cfg(imap_use_ssl=True))
    out = mb.search_fetch(("UNSEEN",), mark_seen=True, limit=0)
    assert out == [("11", b"raw1"), ("12", b"raw2")]
    assert len(created["client"].stored) == 2          # both marked seen
    assert created["client"].logged_out is True


def test_mailbox_search_fetch_limit_keeps_newest(monkeypatch):
    class _Many(_FakeIMAP):
        search_result = b"1 2 3 4 5"
    client = _Many("h", 1)
    monkeypatch.setattr("raven.channels.adapters.email.mailbox.imaplib.IMAP4_SSL", lambda h, p: client)
    out = EmailMailbox(_cfg()).search_fetch(("SINCE", "x"), mark_seen=False, limit=2)
    assert client.fetched_ids == [b"4", b"5"]   # newest 2 only
    assert [uid for uid, _ in out] == ["14", "15"]


def test_mailbox_search_fetch_no_mark(monkeypatch):
    client = _FakeIMAP("h", 1)
    monkeypatch.setattr("raven.channels.adapters.email.mailbox.imaplib.IMAP4_SSL", lambda h, p: client)
    mb = EmailMailbox(_cfg())
    mb.search_fetch(("UNSEEN",), mark_seen=False, limit=0)
    assert client.stored == []


def test_mailbox_search_fetch_select_fails(monkeypatch):
    class _NoSelect(_FakeIMAP):
        def select(self, mailbox):
            return ("NO", [b""])
    monkeypatch.setattr("raven.channels.adapters.email.mailbox.imaplib.IMAP4_SSL", lambda h, p: _NoSelect(h, p))
    assert EmailMailbox(_cfg()).search_fetch(("UNSEEN",), mark_seen=True, limit=0) == []


def test_mailbox_smtp_send_ssl(monkeypatch):
    sent = {}
    class _FakeSmtpSsl:
        def __init__(self, host, port, timeout=0): sent["ssl"] = True
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg): sent["msg"] = msg
    monkeypatch.setattr("raven.channels.adapters.email.mailbox.smtplib.SMTP_SSL", _FakeSmtpSsl)

    msg = EmailMessage()
    EmailMailbox(_cfg(smtp_use_ssl=True, smtp_username="u", smtp_password="p")).smtp_send(msg)
    assert sent["ssl"] and sent["login"] == ("u", "p") and sent["msg"] is msg


def test_mailbox_smtp_send_starttls(monkeypatch):
    calls = {"starttls": False}
    class _FakeSmtp:
        def __init__(self, host, port, timeout=0): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): calls["starttls"] = True
        def login(self, u, p): pass
        def send_message(self, msg): calls["sent"] = True
    monkeypatch.setattr("raven.channels.adapters.email.mailbox.smtplib.SMTP", _FakeSmtp)

    EmailMailbox(_cfg(smtp_use_ssl=False, smtp_use_tls=True)).smtp_send(EmailMessage())
    assert calls["starttls"] and calls["sent"]


# ── channel: orchestration ─────────────────────────────────────────────


def test_validate_config_missing():
    assert _channel()._validate_config() is False


def test_validate_config_complete():
    ch = _channel(
        imap_host="imap.x", imap_username="u", imap_password="p",
        smtp_host="smtp.x", smtp_username="u", smtp_password="p",
    )
    assert ch._validate_config() is True


def test_remember_uid_dedup():
    ch = _channel()
    ch._remember_uid("a")
    ch._remember_uid("a")
    ch._remember_uid("b")
    assert ch._seen_uids == {"a", "b"}
    assert list(ch._seen_queue) == ["a", "b"]


def test_remember_uid_fifo_eviction(monkeypatch):
    monkeypatch.setattr("raven.channels.adapters.email.channel._MAX_SEEN_UIDS", 3)
    ch = _channel()
    for u in ("1", "2", "3", "4"):
        ch._remember_uid(u)
    assert ch._seen_uids == {"2", "3", "4"}
    assert len(ch._seen_queue) == 3


def test_fetch_new_messages_dedups_and_parses():
    ch = _channel()
    raw = b"From: a@x.com\r\nSubject: s\r\n\r\nbody"
    ch._mailbox.search_fetch = MagicMock(return_value=[("11", raw), ("11", raw), ("12", raw)])
    items = ch._fetch_new_messages()
    # "11" appears twice -> deduped to one; "12" once
    assert len(items) == 2
    assert sorted(i["metadata"]["uid"] for i in items) == ["11", "12"]
    assert ch._seen_uids == {"11", "12"}


def test_fetch_messages_between_dates_empty_range():
    ch = _channel()
    ch._mailbox.search_fetch = MagicMock()
    assert ch.fetch_messages_between_dates(date(2026, 6, 5), date(2026, 6, 4)) == []
    ch._mailbox.search_fetch.assert_not_called()


def test_fetch_messages_between_dates_delegates():
    ch = _channel()
    ch._mailbox.search_fetch = MagicMock(return_value=[("9", b"From: a@x.com\r\nSubject: s\r\n\r\nbody")])
    items = ch.fetch_messages_between_dates(date(2026, 6, 1), date(2026, 6, 5), limit=10)
    assert len(items) == 1 and items[0]["metadata"]["uid"] == "9"
    call = ch._mailbox.search_fetch.call_args
    assert call.args[0] == ("SINCE", "01-Jun-2026", "BEFORE", "05-Jun-2026")
    assert call.kwargs == {"mark_seen": False, "limit": 10}


def test_send_builds_reply(monkeypatch):
    ch = _channel(consent_granted=True, smtp_host="smtp.x", auto_reply_enabled=True,
                  from_address="bot@x.com")
    ch._last_subject["to@x.com"] = "Question"
    ch._last_message_id["to@x.com"] = "<orig>"
    captured = {}
    ch._mailbox.smtp_send = MagicMock(side_effect=lambda m: captured.update(msg=m))
    monkeypatch.setattr(
        "raven.channels.adapters.email.channel.asyncio.to_thread",
        AsyncMock(side_effect=lambda fn, *a: fn(*a)),
    )

    import asyncio
    asyncio.run(ch.send("to@x.com", "answer"))
    m = captured["msg"]
    assert m["To"] == "to@x.com"
    assert m["Subject"] == "Re: Question"
    assert m["In-Reply-To"] == "<orig>"
    assert m["From"] == "bot@x.com"
    assert m.get_content().strip() == "answer"


def test_send_skips_without_consent():
    ch = _channel(consent_granted=False, smtp_host="smtp.x")
    ch._mailbox.smtp_send = MagicMock()
    import asyncio
    asyncio.run(ch.send("to@x.com", "x"))
    ch._mailbox.smtp_send.assert_not_called()


def _run_send(ch, chat_id, content, media=None):
    import asyncio
    with patch("raven.channels.adapters.email.channel.asyncio.to_thread",
               AsyncMock(side_effect=lambda fn, *a: fn(*a))):
        asyncio.run(ch.send(chat_id, content, media))


def test_send_carries_no_threading_for_proactive():
    """A proactive send (recipient never mailed us) carries nothing extra: no
    In-Reply-To/References threading, default subject, plain content."""
    ch = _channel(consent_granted=True, smtp_host="smtp.x", auto_reply_enabled=True,
                  from_address="b@x.com")
    captured = {}
    ch._mailbox.smtp_send = MagicMock(side_effect=lambda m: captured.update(msg=m))
    _run_send(ch, "new@x.com", "hello")
    m = captured["msg"]
    assert m["Subject"] == "Re: Raven reply"
    assert m["In-Reply-To"] is None and m["References"] is None


def test_send_media_only_still_sends():
    """send(chat_id, "", media=[path]) sends the mail (email ignores media
    payloads — carry-nothing default)."""
    ch = _channel(consent_granted=True, smtp_host="smtp.x", auto_reply_enabled=True,
                  from_address="b@x.com")
    captured = {}
    ch._mailbox.smtp_send = MagicMock(side_effect=lambda m: captured.update(msg=m))
    _run_send(ch, "to@x.com", "", media=["/tmp/img.png"])
    ch._mailbox.smtp_send.assert_called_once()
    assert captured["msg"]["To"] == "to@x.com"


def test_send_skips_auto_reply_when_disabled():
    ch = _channel(consent_granted=True, smtp_host="smtp.x", auto_reply_enabled=False)
    ch._last_subject["to@x.com"] = "Q"          # makes this a reply
    ch._mailbox.smtp_send = MagicMock()
    _run_send(ch, "to@x.com", "hi")
    ch._mailbox.smtp_send.assert_not_called()


def test_send_skips_missing_recipient():
    ch = _channel(consent_granted=True, smtp_host="smtp.x")
    ch._mailbox.smtp_send = MagicMock()
    _run_send(ch, "   ", "hi")
    ch._mailbox.smtp_send.assert_not_called()


def test_send_skips_without_smtp_host():
    ch = _channel(consent_granted=True, smtp_host="")
    ch._mailbox.smtp_send = MagicMock()
    _run_send(ch, "to@x.com", "hi")
    ch._mailbox.smtp_send.assert_not_called()


def test_send_reraises_on_smtp_failure():
    """SMTP failures propagate (logged then re-raised) so the manager's
    send-retry can back off — email does not swallow like some channels."""
    import pytest
    ch = _channel(consent_granted=True, smtp_host="smtp.x", auto_reply_enabled=True,
                  from_address="b@x.com")
    ch._mailbox.smtp_send = MagicMock(side_effect=RuntimeError("smtp down"))
    with pytest.raises(RuntimeError):
        _run_send(ch, "to@x.com", "hi")


# ── inbound gate (reject before recording reply state) ────────────────


def test_process_item_denied_sender_does_not_poison_reply_state():
    """A denied sender must not update _last_subject/_last_message_id (used by
    send() for Re:/In-Reply-To) nor reach the bus."""
    ch = _channel(allow_from=["friend@x.com"])
    ch.intake.publish = AsyncMock()
    import asyncio
    asyncio.run(ch._process_item({
        "sender": "stranger@x.com", "subject": "spam", "message_id": "<spam-1>",
        "content": "buy now", "metadata": {},
    }))
    assert ch._last_subject == {} and ch._last_message_id == {}
    ch.intake.publish.assert_not_awaited()


def test_process_item_allowed_sender_records_and_publishes():
    ch = _channel(allow_from=["friend@x.com"])
    ch.intake.publish = AsyncMock()
    import asyncio
    asyncio.run(ch._process_item({
        "sender": "friend@x.com", "subject": "Hi", "message_id": "<m1>",
        "content": "hello", "metadata": {"uid": "1"},
    }))
    assert ch._last_subject == {"friend@x.com": "Hi"}
    ch.intake.publish.assert_awaited_once()


# ── stop contract ──────────────────────────────────────────────────────


def test_stop_wakes_the_poll_immediately():
    """stop() must not wait out the poll interval (stop contract #5): the
    poll sleep is an Event wait that stop() sets."""
    ch = _channel(
        consent_granted=True, poll_interval_seconds=3600,
        imap_host="h", imap_username="u", imap_password="p",
        smtp_host="h", smtp_username="u", smtp_password="p",
    )
    ch._fetch_new_messages = lambda: []
    import asyncio

    async def scenario():
        task = asyncio.create_task(ch.start())
        await asyncio.sleep(0.05)          # let it enter the poll wait
        await ch.stop()
        await asyncio.wait_for(task, timeout=2)   # returns now, not in 3600s

    asyncio.run(scenario())


# ── contract conformance ───────────────────────────────────────────────


def test_email_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations
    ch = _channel()
    assert isinstance(ch, Channel)              # name/capabilities/start/stop/send
    assert capability_violations(ch) == []      # no login/streaming declared or implemented


def test_email_spec_import_is_cheap():
    """Importing email.spec must NOT import the channel implementation — the
    EmailChannel/mailbox import is deferred into SPEC.factory."""
    import subprocess
    import sys
    code = (
        "import sys, raven.channels.adapters.email.spec as s;"
        "assert 'raven.channels.adapters.email.channel' not in sys.modules, "
        "'spec import pulled in the channel implementation';"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'Email'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
