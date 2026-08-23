"""Client-side observation ledger: append-only, write-only, read by nobody at run time.

Off unless ``RAVEN_WEB_LEDGER`` names a file, so ordinary use and every
already-measured arm are unchanged. It records, it never decides.

Why client-side rather than a service log: on a fixed corpus the retrieval service is
ours and its log is the most stable dependency there is - it records what was really
returned, so nothing can be recomputed into a different answer later. Live web has no
such service. It also has a blind spot the corpus log cannot close: a repeat_notice
cache hit is *defined* as a request that never reaches the service, so a server log
cannot see it, and on one batch 38.1% of a DR arm's search calls were replays. Logging
here sees both, which makes this ledger strictly stronger - but only if the bit is
written at the moment it happens. Reconstructing it afterwards from a rendered
transcript is the thing this is meant to replace.

One file per question-arm, named by the caller with the run token: concurrent questions
are separate processes, so a shared file would interleave, and the token in the filename
is what joins a line back to its arm and question.

Every record carries ``op``. Readers must partition on it and must count the ops they do
not recognise rather than dropping them - a reader that silently ignores an unknown op
reports a smaller denominator than it measured.

The env var keeps its original ``WEB`` name on purpose. Renaming it would mean editing
the launcher that exports it, and the launcher is the one file a running batch cannot
have edited underneath it; a name that is merely historically narrow costs less than a
coordinated rename across a frozen file.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from typing import Any

from loguru import logger

LEDGER_ENV = "RAVEN_WEB_LEDGER"

_product_path: ContextVar[str | None] = ContextVar("raven_product_ledger", default=None)
"""Per-turn fallback, set by :func:`open_product_ledger`. The env var always wins.

Separate from the env var rather than assigned into it because a batch launcher exports
``RAVEN_WEB_LEDGER`` per question-arm and the appendix must never be able to redirect
that: writing into the environment would let a product-side default silently take over a
measured arm's ledger if the two were ever both configured. Precedence is stated once,
in :func:`ledger_path`, so it cannot differ between the writer and the reader.

A ``ContextVar`` and not a module global. Bench runs one question per process, so a
global would have been correct there and wrong everywhere else: a channel process serves
several sessions at once, each turn in its own asyncio task, and a global would let the
turn that started second redirect the first one's writes into its own file - producing an
appendix that credits this answer with another conversation's pages. The task that opens
the ledger and the tool coroutines it awaits share one context, so the value reaches
exactly the writes that belong to the turn, and no lock is involved.
"""


def ledger_path() -> str | None:
    """Where this process is writing, or None when the ledger is off.

    The module docstring's "read by nobody at run time" is about *generation*: an
    instrument that can steer what it measures is not an instrument. dr@2.8 adds
    one reader, ``agent/process_appendix.py``, which runs after the turn's last
    generation and attaches its output to the value returned to the caller rather
    than to the persisted message - so no model ever sees it, in this turn or as
    history in the next. That is the only exemption, and it is narrow on purpose:
    anything that reads this before or during generation is a different change.

    This is the ONLY resolver. ``ledger_append`` used to read the environment
    directly, which made "where do I write?" and "where do I read?" two separate
    pieces of code that happened to agree - and the first fix for the product-side
    defect below (dr@2.9) is exactly the edit that would have made them disagree: a
    fallback added here alone yields an appendix that reads a file nothing ever
    wrote, i.e. the same silent empty trail, one layer deeper.
    """
    return os.environ.get(LEDGER_ENV) or _product_path.get()


def open_product_ledger(token: str) -> str | None:
    """Point this turn's ledger at a fresh product-side file. Returns it, or None.

    Why this exists: ``final_shape.process_appendix`` defaults on, because the default
    *is* the product - but the trail it renders is computed from this ledger, and the
    only writer of ``RAVEN_WEB_LEDGER`` in the tree is the batch launcher. So the one
    configuration that asks for an appendix was the one configuration that could not
    get one, and it failed the quiet way: ``build_appendix`` returns
    ``("", {"emitted": False, "reason": "ledger_not_configured"})``, the answer is
    returned intact, and nothing logs. The nine bench profiles all pin the knob off, so
    no arm ever exercised the path that needed this.

    Per *turn*, not per process. The appendix reads the whole file, and an AgentLoop is
    long-lived on the product side (one instance serves a whole session), so a
    per-process file would render turn five's trail with turns one through four's
    searches in it - a trail that overstates what this answer rests on. Bench gets this
    right by construction: one file per question-arm, one question per process.

    Failure is degradation, never an exception: an unwritable home directory leaves the
    ledger off and the appendix reports ``ledger_not_configured`` as before. An
    appendix is a nicety; the answer is not.
    """
    if os.environ.get(LEDGER_ENV):
        # A caller that named a file owns it, including its lifetime. Returning None
        # also keeps the seam from deleting a measured arm's ledger on the way out.
        return None
    try:
        from raven.config.paths import get_product_ledger_dir

        path = str(get_product_ledger_dir() / f"{token}.jsonl")
    except Exception as e:  # noqa: BLE001 - an appendix must not be able to kill a turn
        logger.warning("product ledger unavailable, research trail disabled: {}", e)
        path = None
    _product_path.set(path)
    return path


def close_product_ledger() -> None:
    """Drop this turn's product-side file after the appendix has read it.

    Only ever touches a path this module allocated - a caller-supplied
    ``RAVEN_WEB_LEDGER`` is measurement data and deleting it would destroy a batch's
    strongest retrieval instrument. The file is consumed by the appendix at the end of
    the same turn it was opened, so keeping it would accumulate one file per turn
    forever with no reader. A process killed mid-turn leaves one behind; it is a few
    kilobytes and the next turn does not read it.
    """
    path = _product_path.get()
    _product_path.set(None)
    if not path:
        return
    # Drop the cached sequence with the file. The product side is long-lived and
    # allocates a fresh path per turn, but a token collision would otherwise have this
    # process keep counting up from a file that no longer exists.
    _session_seq.pop(path, None)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("product ledger cleanup failed ({}): {}", path, e)


_session_seq: dict[str, int] = {}
"""path -> which run of that file this process is. Resolved once per path, lazily."""


def _resolve_session_seq(path: str) -> int:
    """1 for a fresh file, ``last + 1`` when this file already holds earlier runs.

    Why the ledger needs this at all: the file is named by **workspace**, and a
    re-run of the same question appends to the same file, while ``traj_raw.jsonl``
    keeps exactly one row per question. So a single-question ledger can hold two
    complete sessions and the trajectory only shows the second. Measured on
    ``extbench_20260811/run_dr29``, ``browsecomp-781``: two ``verify_gate
    event=installed`` records 398 seconds apart, 217 lines in the first session and
    **67 in the second that never reached the trajectory**. Any offline replay that
    reads the whole file over-counts that question by 31%.

    Until now the only way to split the sessions was to infer them from
    ``verify_gate installed`` markers - i.e. to recover a fact about the run from a
    behavioural artefact that happens to correlate with it. That is the shape this
    project keeps paying for ("which model served this hop must be a first-class
    field, not reconstructed afterwards"), so it becomes a field.

    Read from the tail, not by scanning: the value is monotonic within a file, so the
    last line carries the maximum, and these files reach tens of thousands of lines.
    Lines with no ``session_seq`` are treated as session 0, which makes an old file's
    first re-run session 1 - so "absent" and "1" stay distinguishable rather than
    collapsing into each other.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65_536))
            tail = fh.read().decode("utf-8", errors="replace")
        for line in reversed(tail.split("\n")):
            if not line.strip():
                continue
            try:
                return int(json.loads(line).get("session_seq") or 0) + 1
            except (ValueError, TypeError, AttributeError):
                # A truncated first line in the tail window, or a pre-field record.
                continue
        return 1
    except FileNotFoundError:
        return 1
    except Exception as e:  # noqa: BLE001 - an instrument must not kill the run
        logger.warning("ledger session_seq unresolved ({}): {}", path, e)
        return 1


def ledger_append(record: dict[str, Any]) -> None:
    """Append one line to the client-side ledger, if one is configured.

    A write failure is logged and swallowed: an instrument that can kill the run it
    measures is worse than no instrument. It must be loud, though - a silently empty
    ledger reads as "this arm issued no searches", which is a different claim than
    "the ledger could not be written".

    ``session_seq`` is stamped HERE rather than at the call sites. There are several
    writers across two modules and every one of them would have to remember; a
    comment telling callers to include it is exactly what the next caller bypasses.
    One chokepoint defines a line, so one chokepoint stamps its provenance.
    """
    if not (path := ledger_path()):
        return
    if path not in _session_seq:
        _session_seq[path] = _resolve_session_seq(path)
    # ``setdefault``, so a caller that has a better answer keeps it and this never
    # silently overwrites a field it did not own.
    record.setdefault("session_seq", _session_seq[path])
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 - deliberate; see below
        # Deliberately broader than OSError. ``json.dumps`` runs inside this block and
        # raises TypeError on any value a caller forgot to make serialisable, which
        # would propagate out of an INSTRUMENT and kill the run it is measuring - the
        # exact outcome the docstring above rules out. Narrowing this to the errors
        # anticipated today would make the guarantee conditional on nobody ever adding
        # a field, which is not a guarantee. BaseException is still allowed through, so
        # a cancellation or KeyboardInterrupt is never swallowed here.
        logger.error("ledger append failed ({}): {}", path, e)
