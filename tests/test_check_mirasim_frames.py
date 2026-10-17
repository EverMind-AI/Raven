"""The Mirasim frame check: a host bundle read for the literals the adapter depends on."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import check_mirasim_frames as check

ADAPTER = Path(__file__).resolve().parents[1] / "raven" / "acp" / "mirasim"


def bundle(*, drop: str = "", split: bool = False) -> str:
    """A stand-in bundle that names everything the adapter uses, minified the way Mirasim's is."""
    cases = "".join(f"case'{name}':break;" for name in check.SENT if name != drop)
    types = "".join(f"send({{'type':'{name}'}});" for name in check.RECEIVED if name != drop)
    fields = "".join(f"x['{name}'];" for name in check.FIELDS if name != drop)
    text = "/[^']/.test(s);" + cases + types + fields
    if split:
        text = text.replace("'appendText'", "'appendTe'+'xt'").replace("case'listClis'", "case'list'+'Clis'")
    return text


def test_a_bundle_with_everything_misses_nothing() -> None:
    assert check.missing(bundle()) == {}
    assert check.missing(bundle(split=True)) == {}


@pytest.mark.parametrize(
    ("name", "kind"),
    [("steer", "sent frames (no case label)"), ("clis", "read frames (no type literal)"), ("fromSeq", "fields")],
)
def test_what_is_missing_is_named_by_kind(name: str, kind: str) -> None:
    assert check.missing(bundle(drop=name)) == {kind: [name]}


def test_a_quote_inside_a_regular_expression_hides_no_field() -> None:
    assert check.quoted("a=/[^']/;b={'fromSeq':1}", "fromSeq")
    assert not check.quoted("'fromSeqs'", "fromSeq")


def test_changes_list_what_appeared_and_what_vanished() -> None:
    old = "case'prompt':case'gone':send({'type':'accepted'});"
    new = "case'prompt':case'fresh':send({'type':'accepted'});send({'type':'newFrame'});"
    assert check.changes(old, new) == {
        "case labels added": ["fresh"],
        "case labels removed": ["gone"],
        "type literals added": ["newFrame"],
        "type literals removed": [],
    }


def test_the_entry_reports_and_exits_by_what_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    good, bad = tmp_path / "good.cjs", tmp_path / "bad.cjs"
    good.write_text(bundle())
    bad.write_text(bundle(drop="interact"))
    assert check.main([str(good)]) == 0
    assert "all 13 sent frames" in capsys.readouterr().out
    assert check.main([str(bad), "--against", str(good)]) == 1
    out = capsys.readouterr().out
    assert "case labels removed (1): interact" in out and "MISSING sent frames (no case label): interact" in out
    assert check.main([str(tmp_path / "absent.cjs")]) == 2
    monkeypatch.setattr(check, "DEFAULT_BUNDLE", good)
    assert check.main([]) == 0


NOT_FRAMES = {
    "text",
    "image",
    "resource",
    "resource_link",
    "content",
    "agent_message_chunk",
    "select",
    "array",
    "object",
    "string",
}
"""``"type"`` values the adapter spells that are not host frames: ACP content blocks and updates, the
type of a question a turn asks, and the JSON Schema types of the form that asks it."""


def _source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(ADAPTER.glob("*.py")))


def test_every_frame_the_adapter_sends_is_checked() -> None:
    sent = set(re.findall(r'\{\s*"type":\s*"(\w+)"', _source())) - NOT_FRAMES
    assert {"getHistory", "prompt", "steer"} <= sent
    assert sent and sent <= set(check.SENT), sent - set(check.SENT)


def test_every_frame_the_adapter_reads_is_checked() -> None:
    source = _source()
    read: set[str] = set()
    for single, group in re.findall(r'(?:get\("type"\)|\bkind)\s*(?:==|in)\s*(?:"(\w+)"|\(([^)]*)\))', source):
        read |= {single} if single else set(re.findall(r'"(\w+)"', group))
    read -= NOT_FRAMES
    assert read and read <= set(check.RECEIVED) | set(check.TOLERATED), read - set(check.RECEIVED)
