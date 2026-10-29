"""The Task Board in Chinese: a Chinese query finds its matter, and the zh pack's update instruction renders."""

from __future__ import annotations

import pytest

import raven.i18n as i18n
from raven.agent.task_board.board import Board, Dispatch, ProgressStep, TaskGroup
from raven.agent.task_board.recall import search
from raven.agent.task_board.runner import build_instruction


@pytest.fixture
def zh():
    before = i18n.current_language()
    i18n.set_language("zh")
    yield
    i18n.set_language(before)


def _group(gid: str, title: str, text: str) -> TaskGroup:
    return TaskGroup(gid, title, "active", 1, 1, [ProgressStep(f"s_{gid}", "T1", None, text, [])])


def test_a_chinese_query_finds_its_matter():
    board = Board(
        [
            _group("g_1", "修复登录白屏", "定位到 bug：恢复会话时 token 为空"),
            _group("g_2", "写发布说明", "初稿写好，等你挑亮点"),
        ],
        [],
    )

    out = search(board, "登录 白屏", status_of=lambda _link: "unknown", lineage={})

    assert out.splitlines()[0].startswith("g_1 ")


def test_the_zh_update_instruction_renders_the_board_and_handles(zh):
    board = Board([_group("g_1", "修复登录白屏", "定位到 bug")], [])

    text = build_instruction(board, [Dispatch("spawn:locate_login_bug", "定位白屏原因")])

    assert "当前看板" in text
    assert "g_1 [active] 修复登录白屏" in text
    assert "spawn:locate_login_bug -- 定位白屏原因" in text
    assert "{board}" not in text and "{dispatches}" not in text
    assert text.index("[BEGIN BOARD RECORD]") < text.index("修复登录白屏") < text.index("[END BOARD RECORD]")
    assert text.index("[BEGIN DISPATCHES]") < text.index("定位白屏原因") < text.index("[END DISPATCHES]")
