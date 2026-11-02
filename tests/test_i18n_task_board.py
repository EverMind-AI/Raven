"""The Task Board in Chinese: a Chinese query finds its matter, and the zh pack's update instruction renders."""

from __future__ import annotations

import json

import pytest

import raven.i18n as i18n
from raven.agent.task_board.board import (
    Board,
    Dispatch,
    ProgressStep,
    TaskGroup,
    UpdateRejectedError,
    apply_update,
    word_count,
)
from raven.agent.task_board.recall import search
from raven.agent.task_board.runner import build_instruction


@pytest.fixture
def zh():
    before = i18n.current_language()
    i18n.set_language("zh")
    yield
    i18n.set_language(before)


def _group(gid: str, title: str, text: str) -> TaskGroup:
    return TaskGroup(gid, title, "", "idle", 1, 1, [ProgressStep(f"s_{gid}", "T1", None, text, text, [])])


def test_word_count_counts_each_chinese_character_and_each_english_word():
    assert word_count("web 前端输入框高度可拖拽") == 11
    assert word_count("修复登录后白屏，等用户选") == 12
    assert word_count("2026年10月") == 4


def test_a_chinese_title_and_summary_keep_to_twenty_and_fifty_characters():
    def update(new_title: str, new_summary: str) -> dict:
        step = {"title": "定位原因", "summary": "恢复会话时 token 为空", "status": "idle"}
        return {"steps": [{"new_title": new_title, "new_summary": new_summary, **step}]}

    twenty, fifty = "把输入框改成可以拖拽调高并且限制最高半屏", "进展" * 25
    board = apply_update(Board([], []), update(twenty, fifty), [], turn_at="T1", turn_id=None, now_ms=1)
    assert (board.groups[0].title, board.groups[0].summary) == (twenty, fifty)

    with pytest.raises(UpdateRejectedError, match=r"^new_title is 21 words long; the limit is 20: "):
        apply_update(Board([], []), update(twenty + "幕", fifty), [], turn_at="T1", turn_id=None, now_ms=1)
    with pytest.raises(UpdateRejectedError, match=r"^new_summary is 51 words long; the limit is 50: "):
        apply_update(Board([], []), update(twenty, fifty + "中"), [], turn_at="T1", turn_id=None, now_ms=1)


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
    assert "g_1 [idle] 修复登录白屏" in text
    assert "spawn:locate_login_bug -- 定位白屏原因" in text
    assert "{board}" not in text and "{dispatches}" not in text
    assert text.index("[BEGIN BOARD RECORD]") < text.index("修复登录白屏") < text.index("[END BOARD RECORD]")
    assert text.index("[BEGIN DISPATCHES]") < text.index("定位白屏原因") < text.index("[END DISPATCHES]")


def test_the_zh_instruction_puts_a_question_to_the_user_ahead_of_running_work(zh):
    text = build_instruction(Board([], []), [])

    status_rule = text[text.index("- `status`：") : text.index("- `questions`：")]
    assert (
        "回复就这件事问用户时写 `needs_you`，即使这件事上发起的工作还在运行；问的是别的事，不会让这件事变成 `needs_you`。"
        in status_rule
    )


def test_the_zh_instruction_has_a_needs_you_summary_open_with_the_ask(zh):
    text = build_instruction(Board([], []), [])

    summary_rule = text[text.index("- `summary`：") : text.index("- `status`：")]
    assert "状态是 `needs_you` 时，先写就这件事问了用户什么" in summary_rule


def test_the_zh_instructions_example_is_an_update_the_board_accepts(zh):
    text = build_instruction(Board([], []), [])
    example = json.loads(text[text.index("示例：") + len("示例：") :].strip())

    board = apply_update(
        Board([], []), example, [Dispatch("spawn:locate_login_bug", "定位")], turn_at="T1", turn_id=None, now_ms=1
    )

    assert board.groups[0].status == "needs_you"
    assert board.groups[0].steps[0].questions


def test_the_zh_instruction_keeps_each_question_with_its_own_matter(zh):
    text = build_instruction(Board([], []), [])

    questions_rule = text[text.index("- `questions`：") : text.index("- `tasks`：")]
    assert "列出回复里就这件事问用户的每个问题" in questions_rule
    assert "问别的事的问题归那件事：新问的写在那个任务的事件里，只是重复之前问过的就不必写。" in questions_rule
