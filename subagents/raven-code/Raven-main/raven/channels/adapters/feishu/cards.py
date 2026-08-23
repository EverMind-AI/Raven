"""Outbound rendering of agent markdown into Feishu message payloads.

Feishu accepts three flavours — plain ``text``, rich-text ``post``, and an
interactive ``card``. ``detect_format`` picks the smallest one that can
carry the content; the builders below emit the matching JSON. This is the
render half of the formatting layer and is Feishu-schema-specific, so it
stays with the adapter (the shared half is markdown *parsing*, not this).
"""

from __future__ import annotations

import json
import re

_TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
    re.MULTILINE,
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_COMPLEX_RE = re.compile(r"```|^\|.+\|.*\n\s*\|[-:\s|]+\||^#{1,6}\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"\*\*.+?\*\*|__.+?__|(?<!\*)\*(?!\*).+?(?<!\*)\*(?!\*)|~~.+?~~", re.DOTALL)
_BULLET_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
_ORDERED_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)

_TEXT_MAX_LEN = 200
_POST_MAX_LEN = 2000


def detect_format(content: str) -> str:
    """Return ``"text"`` / ``"post"`` / ``"interactive"`` for *content*.

    Card wins for anything Feishu's simpler flavours can't render (code,
    tables, headings, emphasis, lists) or anything long; links alone go to
    post; short unformatted text stays text.
    """
    text = content.strip()
    if _COMPLEX_RE.search(text):
        return "interactive"
    if len(text) > _POST_MAX_LEN:
        return "interactive"
    if _EMPHASIS_RE.search(text):
        return "interactive"
    if _BULLET_RE.search(text) or _ORDERED_RE.search(text):
        return "interactive"
    if _LINK_RE.search(text):
        return "post"
    return "text" if len(text) <= _TEXT_MAX_LEN else "post"


def text_payload(content: str) -> str:
    return json.dumps({"text": content.strip()}, ensure_ascii=False)


def post_payload(content: str) -> str:
    """Render markdown to a Feishu post: each line is a paragraph, links
    become ``a`` tags and everything else ``text`` tags."""
    paragraphs: list[list[dict]] = []
    for line in content.strip().split("\n"):
        elements: list[dict] = []
        cursor = 0
        for m in _LINK_RE.finditer(line):
            if before := line[cursor : m.start()]:
                elements.append({"tag": "text", "text": before})
            elements.append({"tag": "a", "text": m.group(1), "href": m.group(2)})
            cursor = m.end()
        if rest := line[cursor:]:
            elements.append({"tag": "text", "text": rest})
        paragraphs.append(elements or [{"tag": "text", "text": ""}])
    return json.dumps({"zh_cn": {"content": paragraphs}}, ensure_ascii=False)


def card_payloads(content: str) -> list[str]:
    """Render markdown into one or more interactive-card payloads.

    Feishu rejects more than one table per card (API 11310), so the
    element stream is split into table-bounded groups, one card each.
    """
    elements = _build_elements(content)
    payloads = []
    for group in _split_by_table_limit(elements):
        card = {"config": {"wide_screen_mode": True}, "elements": group}
        payloads.append(json.dumps(card, ensure_ascii=False))
    return payloads


def _build_elements(content: str) -> list[dict]:
    elements: list[dict] = []
    cursor = 0
    for m in _TABLE_RE.finditer(content):
        if before := content[cursor : m.start()]:
            if before.strip():
                elements += _split_headings(before)
        elements.append(parse_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)})
        cursor = m.end()
    if (rest := content[cursor:]) and rest.strip():
        elements += _split_headings(rest)
    return elements or [{"tag": "markdown", "content": content}]


def parse_table(table_text: str) -> dict | None:
    """Parse a markdown pipe-table into a Feishu ``table`` element."""
    lines = [ln.strip() for ln in table_text.strip().split("\n") if ln.strip()]
    if len(lines) < 3:
        return None

    def cells(ln: str) -> list[str]:
        return [c.strip() for c in ln.strip("|").split("|")]

    headers = cells(lines[0])
    rows = [cells(ln) for ln in lines[2:]]
    columns = [{"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"} for i, h in enumerate(headers)]
    return {
        "tag": "table",
        "page_size": len(rows) + 1,
        "columns": columns,
        "rows": [{f"c{i}": (r[i] if i < len(r) else "") for i in range(len(headers))} for r in rows],
    }


def _split_headings(content: str) -> list[dict]:
    """Turn headings into bold ``div`` elements, leaving other text as
    markdown blocks. Code fences are shielded from heading detection."""
    blocks: list[str] = []
    shielded = content
    for m in _CODE_BLOCK_RE.finditer(content):
        blocks.append(m.group(1))
        shielded = shielded.replace(m.group(1), f"\x00{len(blocks) - 1}\x00", 1)

    elements: list[dict] = []
    cursor = 0
    for m in _HEADING_RE.finditer(shielded):
        if before := shielded[cursor : m.start()].strip():
            elements.append({"tag": "markdown", "content": before})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{m.group(2).strip()}**"}})
        cursor = m.end()
    if rest := shielded[cursor:].strip():
        elements.append({"tag": "markdown", "content": rest})

    for i, block in enumerate(blocks):
        for el in elements:
            if el.get("tag") == "markdown":
                el["content"] = el["content"].replace(f"\x00{i}\x00", block)
    return elements or [{"tag": "markdown", "content": content}]


def _split_by_table_limit(elements: list[dict], max_tables: int = 1) -> list[list[dict]]:
    if not elements:
        return [[]]
    groups: list[list[dict]] = []
    current: list[dict] = []
    tables = 0
    for el in elements:
        if el.get("tag") == "table":
            if tables >= max_tables and current:
                groups.append(current)
                current, tables = [], 0
            current.append(el)
            tables += 1
        else:
            current.append(el)
    if current:
        groups.append(current)
    return groups or [[]]
