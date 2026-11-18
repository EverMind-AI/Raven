"""The native bridge between the desktop window and the web page, checked once.

The two sides are written in different languages and each typechecks on its
own, so a rename on either side is invisible until a reader hits it: a lost
`ready` leaves the native splash up forever, a lost `reauth` sends a restarted
gateway's stale session back to a terminal banner. This test reads both trees
and fails when the message set, the handler name or the user-agent token drift
apart.

It is static on purpose -- the macOS window needs a GUI, but the contract is
just literals, so it can be held on the same Ubuntu shards that run the rest of
the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SWIFT = REPO / "desktop" / "macos" / "RavenDesktop.swift"
PAGE_SIDES = (
    REPO / "ui-web" / "src" / "app" / "connection.ts",
    REPO / "ui-web" / "src" / "state" / "look.ts",
    REPO / "ui-web" / "src" / "state" / "shellWindow.ts",
)

RECEIVE = re.compile(r"func receive\(_ message: WKScriptMessage\) \{(.*?)\n    \}", re.S)
SWIFT_CASE = re.compile(r'case "([a-z]+)":')
SWIFT_HANDLER = re.compile(r'userContentController\.add\(ScriptBridge\(app: self\), name: "([\w-]+)"\)')
SWIFT_UA = re.compile(r'applicationNameForUserAgent = "([^"]+)"')

TS_POSTED = re.compile(r"postMessage\(\{\s*type:\s*'([a-z]+)'")
TS_UNION = re.compile(r"function tell\(type:\s*([^)]*)\)")
TS_QUOTED = re.compile(r"'([a-z]+)'")
TS_HANDLER = re.compile(r"messageHandlers\??\.([\w]+)\??")
TS_UA = re.compile(r"/(\w+)/\.test\(navigator\.userAgent\)")


def _swift() -> str:
    assert SWIFT.exists(), f"the native window source is gone from {SWIFT.relative_to(REPO)}"
    return SWIFT.read_text(encoding="utf-8")


def _page_side() -> str:
    missing = [str(p.relative_to(REPO)) for p in PAGE_SIDES if not p.exists()]
    assert not missing, f"the page's side of the bridge moved: {missing}"
    return "\n".join(p.read_text(encoding="utf-8") for p in PAGE_SIDES)


def _swift_messages() -> set[str]:
    body = RECEIVE.search(_swift())
    assert body, "the native window no longer has a receive(_:) switch to read"
    return set(SWIFT_CASE.findall(body.group(1)))


def _page_messages() -> set[str]:
    text = _page_side()
    found = set(TS_POSTED.findall(text))
    for union in TS_UNION.findall(text):
        found |= set(TS_QUOTED.findall(union))
    return found


def test_both_sides_send_the_same_messages() -> None:
    native = _swift_messages()
    page = _page_messages()
    assert native, "the native switch handles no message"
    assert page, "the page posts no message"
    assert native == page, (
        "the native window and the page disagree about the bridge: "
        f"handled natively but never posted: {sorted(native - page)}; "
        f"posted but never handled: {sorted(page - native)}"
    )


def test_the_handler_name_matches() -> None:
    handler = SWIFT_HANDLER.search(_swift())
    assert handler, "the native window no longer registers its script message handler"
    names = set(TS_HANDLER.findall(_page_side()))
    assert names == {handler.group(1)}, (
        f"the native window answers on '{handler.group(1)}' but the page posts to {sorted(names)}"
    )


def test_the_user_agent_token_matches() -> None:
    token = SWIFT_UA.search(_swift())
    assert token, "the native window no longer sets applicationNameForUserAgent"
    expected = token.group(1).split("/")[0]
    seen = set(TS_UA.findall(_page_side()))
    assert seen == {expected}, f"the window calls itself '{expected}' but the page looks for {sorted(seen)}"
