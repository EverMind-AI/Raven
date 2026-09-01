"""What a campaign may DO, declared before it does any of it.

A campaign could start a trial and nothing else, which is enough while the work is
a parameter sweep. It is not enough for one that watches: when the condition holds,
an order has to be placed, an application sent, an alert raised, and none of those
is a trial.

Two things make it a declared table rather than a line composed at the moment of
acting. It is a permission list -- read back to the owner before anything runs, so
a loop cannot put 'sell' where it said 'buy' on some later wake with nobody
watching. And repeating is not always harmless: marking a thread read twice leaves
it read, buying three shares twice leaves six, and only the declaration knows
which of those this is.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import actions as a  # noqa: E402


def _rows(*rows):
    return [{"name": n, "command": c, "repeat": r} for n, c, r in rows]


def test_an_action_must_say_what_repeating_it_does() -> None:
    """Nothing here can work it out from the command, and a wake turn remembers
    nothing, so an unguarded repeat is a real second order."""
    problem = a.table_problem([{"name": "buy", "command": "broker buy VOLT {qty}"}])

    assert problem.startswith("REFUSED")
    assert "harmful" in problem and "safe" in problem


def test_an_action_needs_a_command() -> None:
    assert a.table_problem(_rows(("buy", "", "harmful"))).startswith("REFUSED")


def test_run_and_none_are_not_available_as_names() -> None:
    """'run' means starting a trial and 'none' means the look needed no action."""
    assert a.table_problem(_rows(("run", "x", "safe"))).startswith("REFUSED")
    assert a.table_problem(_rows(("none", "x", "safe"))).startswith("REFUSED")


def test_an_action_cannot_be_redefined() -> None:
    """The table is what the owner was shown. Rewriting an entry would make that
    reading worthless."""
    existing = [a.Action("buy", "broker buy VOLT {qty} market", a.HARMFUL)]

    problem = a.table_problem(_rows(("buy", "broker sell VOLT {qty} market", "harmful")), existing)

    assert problem.startswith("REFUSED") and "already declared" in problem


def test_redeclaring_the_same_action_is_fine() -> None:
    existing = [a.Action("buy", "broker buy VOLT {qty}", a.HARMFUL)]

    assert a.table_problem(_rows(("buy", "broker buy VOLT {qty}", "harmful")), existing) is None


def test_a_missing_value_is_named_rather_than_left_empty() -> None:
    """An order line that lost its quantity is not a smaller order."""
    action = a.Action("buy", "broker buy VOLT {qty} {kind}", a.HARMFUL)

    command, missing = a.fill(action, {"qty": 3})

    assert missing == ["kind"]


def test_the_values_go_into_the_command() -> None:
    action = a.Action("buy", "broker buy VOLT {qty} market", a.HARMFUL)

    command, missing = a.fill(action, {"qty": 3})

    assert (command, missing) == ("broker buy VOLT 3 market", [])


def test_a_harmful_action_keys_on_what_it_was_asked_to_do() -> None:
    """Which is the entire cold-start guarantee: the second attempt at the same
    thing collides with the record of the first."""
    buy = a.Action("buy", "broker buy VOLT {qty}", a.HARMFUL)

    first = a.idem_key(buy, {"qty": 3}, already=0)
    again = a.idem_key(buy, {"qty": 3}, already=1)

    assert first == again


def test_a_safe_action_gets_its_own_line_each_time() -> None:
    """Doing it again is legitimate, and each occurrence is worth recording."""
    like = a.Action("like", "api like {track}", a.SAFE)

    assert a.idem_key(like, {"track": 41}, already=0) != a.idem_key(like, {"track": 41}, already=1)


def test_different_values_are_a_different_call() -> None:
    buy = a.Action("buy", "broker buy VOLT {qty}", a.HARMFUL)

    assert a.idem_key(buy, {"qty": 3}) != a.idem_key(buy, {"qty": 4})


def test_a_row_with_an_unknown_repeat_rule_reads_as_harmful() -> None:
    """The safe direction for a table whose whole job is to stop a second order."""
    declared = a.declared({"actions": [{"name": "buy", "command": "x", "repeat": "maybe"}]})

    assert declared[0].repeat_is_harmful is True
