"""The host's Task Board: each conversation's matters, and each turn's advance on them.

A Task Group is one matter a conversation is pushing; a Progress Step is one
turn's advance on one group, with the Tasks (spawn calls, DAG runs) that turn
dispatched for it. The host model keeps the board in one extra call after every
turn and reads it back through the ``task_board`` tool. See
``docs/specs/2026-10-29-task-board-design.md``.
"""
