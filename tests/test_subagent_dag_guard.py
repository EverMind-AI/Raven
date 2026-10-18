"""The guard language: grammar, three-valued evaluation, and the never-true checks."""

import pytest

from raven.agent.subagent.dag_guard import (
    KLEENE_AND,
    KLEENE_NOT,
    KLEENE_OR,
    GuardError,
    evaluate,
    negated_atoms,
    parse_guard,
    positive_atoms_under_and,
)


def test_parse_reads_a_bare_atom():
    guard = parse_guard("triage.bug")
    assert [a.node for a in guard.atoms] == ["triage"]
    assert [a.name for a in guard.atoms] == ["bug"]


def test_parse_reads_done_as_an_atom():
    guard = parse_guard("risk.done")
    assert guard.atoms[0].name == "done"


def test_parse_handles_parentheses_and_precedence():
    """`a or b and c` binds as `a or (b and c)`; the parentheses below force the other shape."""
    flat = parse_guard("t.a or t.b and t.c")
    grouped = parse_guard("(t.a or t.b) and t.c")
    assert evaluate(flat, lambda atom: atom.name == "a") is True
    assert evaluate(grouped, lambda atom: atom.name == "a") is False


def test_parse_tolerates_free_whitespace():
    assert parse_guard("   not ( t.bug  or\tt.feature )  ").atoms[0].node == "t"


def test_parse_refuses_a_syntax_error_with_a_position():
    with pytest.raises(GuardError) as exc:
        parse_guard("t.a and")
    assert "position" in str(exc.value)


def test_parse_refuses_an_empty_expression():
    with pytest.raises(GuardError):
        parse_guard("   ")


def test_parse_refuses_a_bare_name_with_no_dot():
    with pytest.raises(GuardError):
        parse_guard("triage")


def test_parse_refuses_an_unknown_keyword():
    with pytest.raises(GuardError):
        parse_guard("t.a xor t.b")


def test_parse_refuses_more_than_64_atoms():
    with pytest.raises(GuardError):
        parse_guard(" or ".join(f"t.r{i}" for i in range(65)))


def test_parse_accepts_64_atoms():
    assert len(parse_guard(" or ".join(f"t.r{i}" for i in range(64))).atoms) == 64


def test_parse_refuses_nesting_deeper_than_32_levels():
    with pytest.raises(GuardError):
        parse_guard("(" * 33 + "t.a" + ")" * 33)
    with pytest.raises(GuardError):
        parse_guard("not " * 33 + "t.a")


def test_parse_accepts_32_levels():
    assert parse_guard("(" * 32 + "t.a" + ")" * 32).atoms[0].name == "a"


@pytest.mark.parametrize(
    ("left", "right", "want"),
    [
        (True, True, True),
        (True, False, False),
        (True, None, None),
        (False, True, False),
        (False, False, False),
        (False, None, False),
        (None, True, None),
        (None, False, False),
        (None, None, None),
    ],
)
def test_kleene_and_matches_the_three_valued_and_table(left, right, want):
    assert KLEENE_AND(left, right) is want


@pytest.mark.parametrize(
    ("left", "right", "want"),
    [
        (True, True, True),
        (True, False, True),
        (True, None, True),
        (False, True, True),
        (False, False, False),
        (False, None, None),
        (None, True, True),
        (None, False, None),
        (None, None, None),
    ],
)
def test_kleene_or_matches_the_three_valued_or_table(left, right, want):
    assert KLEENE_OR(left, right) is want


@pytest.mark.parametrize(("value", "want"), [(True, False), (False, True), (None, None)])
def test_kleene_not_leaves_unknown_unknown(value, want):
    assert KLEENE_NOT(value) is want


def test_evaluate_reads_every_atom_through_the_callback():
    seen: list[str] = []

    def value_of(atom):
        seen.append(f"{atom.node}.{atom.name}")
        return True

    assert evaluate(parse_guard("a.x and b.y"), value_of) is True
    assert seen == ["a.x", "b.y"]


def test_evaluate_short_circuits_on_false_and():
    seen: list[str] = []

    def value_of(atom):
        seen.append(atom.node)
        return False

    assert evaluate(parse_guard("a.x and b.y"), value_of) is False
    assert seen == ["a"]


def test_evaluate_short_circuits_on_true_or():
    seen: list[str] = []

    def value_of(atom):
        seen.append(atom.node)
        return True

    assert evaluate(parse_guard("a.x or b.y"), value_of) is True
    assert seen == ["a"]


def test_a_not_over_an_unknown_stays_unknown():
    """The whole reason for `not`: `not risk.high` must not hold when risk never ran."""
    assert evaluate(parse_guard("not risk.high"), lambda atom: None) is None


def _evaluate_recording(source: str, values: dict[str, bool | None]) -> tuple[bool | None, list[str]]:
    seen: list[str] = []

    def value_of(atom):
        key = f"{atom.node}.{atom.name}"
        seen.append(key)
        return values[key]

    return evaluate(parse_guard(source), value_of), seen


def test_unknown_or_true_is_true_and_both_atoms_are_read():
    value, seen = _evaluate_recording("a.x or b.y", {"a.x": None, "b.y": True})
    assert value is True
    assert seen == ["a.x", "b.y"]


def test_unknown_or_false_is_unknown():
    value, seen = _evaluate_recording("a.x or b.y", {"a.x": None, "b.y": False})
    assert value is None
    assert seen == ["a.x", "b.y"]


def test_unknown_and_false_is_false_and_both_atoms_are_read():
    value, seen = _evaluate_recording("a.x and b.y", {"a.x": None, "b.y": False})
    assert value is False
    assert seen == ["a.x", "b.y"]


def test_unknown_and_true_is_unknown():
    value, seen = _evaluate_recording("a.x and b.y", {"a.x": None, "b.y": True})
    assert value is None
    assert seen == ["a.x", "b.y"]


def test_positive_atoms_under_and_sees_an_and_chain():
    guard = parse_guard("t.bug and r.high and t.feature")
    chains = [tuple(f"{a.node}.{a.name}" for a in chain) for chain in positive_atoms_under_and(guard)]
    assert chains == [("t.bug", "r.high", "t.feature")]


def test_positive_atoms_under_and_stops_at_an_or():
    guard = parse_guard("(t.bug and r.high) or (t.feature and r.low)")
    chains = [tuple(f"{a.node}.{a.name}" for a in chain) for chain in positive_atoms_under_and(guard)]
    assert chains == [("t.bug", "r.high"), ("t.feature", "r.low")]


def test_positive_atoms_under_and_skips_negated_atoms():
    guard = parse_guard("t.bug and not t.feature")
    chains = [tuple(a.name for a in chain) for chain in positive_atoms_under_and(guard)]
    assert chains == [("bug",)]


def _chains_of(source: str) -> set[tuple[str, ...]]:
    return {tuple(f"{a.node}.{a.name}" for a in chain) for chain in positive_atoms_under_and(parse_guard(source))}


def test_positive_atoms_under_and_yields_a_bare_atom_as_a_one_tuple():
    assert _chains_of("t.a") == {("t.a",)}


def test_positive_atoms_under_and_does_not_splice_an_or_arm_into_its_sibling():
    assert _chains_of("(t.bug or t.feature) and r.high") == {("r.high",), ("t.bug",), ("t.feature",)}


def test_positive_atoms_under_and_keeps_two_routes_of_one_node_apart():
    """Spliced, the arm r.a would share a chain with r.b: two routes of one node, which the single-select
    check reads as impossible, though the guard holds with r on b."""
    assert _chains_of("(r.a or r.b) and r.b") == {("r.a",), ("r.b",)}


def test_positive_atoms_under_and_searches_an_and_chain_inside_an_or():
    assert _chains_of("x.y and (a.p and a.q or b.r)") == {("x.y",), ("a.p", "a.q"), ("b.r",)}


def test_negated_atoms_finds_only_direct_negations():
    guard = parse_guard("not r.done and (a.x or not b.y) and not (c.z and d.w)")
    assert [f"{a.node}.{a.name}" for a in negated_atoms(guard)] == ["r.done", "b.y"]


def test_a_node_id_spelled_like_a_keyword_is_an_atom():
    """`and`, `or` and `not` are legal node ids, so a word followed by a dot is a node."""
    guard = parse_guard("and.x")
    assert [(a.node, a.name) for a in guard.atoms] == [("and", "x")]


def test_a_keyword_spelled_node_id_joins_a_real_guard():
    guard = parse_guard("not.a and r.b")
    assert [(a.node, a.name) for a in guard.atoms] == [("not", "a"), ("r", "b")]
    assert evaluate(guard, lambda atom: atom.node == "not") is False
    assert evaluate(guard, lambda atom: True) is True


def test_a_keyword_standing_alone_is_still_an_operator():
    """The lookahead only changes what follows a dot; the words keep their meaning."""
    guard = parse_guard("not t.a and not (t.b or t.c)")
    assert [f"{a.node}.{a.name}" for a in guard.atoms] == ["t.a", "t.b", "t.c"]
    assert [f"{a.node}.{a.name}" for a in negated_atoms(guard)] == ["t.a"]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("t.", "expected a route name after 't.' at position 2"),
        ("t.a $ t.b", "unexpected character '$' at position 4"),
        ("t.a t.b", "unexpected 't.b' at position 4"),
        ("(t.a", "expression ends early at position 4"),
        ("t.a)", "unexpected ')' at position 3"),
        ("(t.a t.b)", "expected ')' at position 5"),
        (")", "unexpected ')' at position 0"),
        ("triage", "expected '.' after 'triage' at position 0"),
    ],
)
def test_every_syntax_error_arm_names_its_position(source, message):
    """The tokenizer's stray-dot text is unchanged by the keyword lookahead above."""
    with pytest.raises(GuardError) as exc:
        parse_guard(source)
    assert message in str(exc.value)


def test_an_atom_carries_its_extent():
    """`end` is where the atom stops in the source, which is what a rename splices by."""
    guard = parse_guard("  not ( r.a or s.done ) ")
    assert [(a.pos, a.end) for a in guard.atoms] == [(8, 11), (15, 21)]
    assert [guard.source[a.pos : a.end] for a in guard.atoms] == ["r.a", "s.done"]


def test_an_and_chain_flattens_its_right_side_too():
    """`a.x and (a.y and b.z)` is one chain of three: read as two, the last two
    routes of the same single-select node would never be compared."""
    assert _chains_of("a.x and (a.y and b.z)") == {("a.x", "a.y", "b.z")}


def test_nothing_under_a_negated_and_chain_is_yielded():
    """A negated chain cannot make the guard impossible, so it is not searched."""
    assert _chains_of("not (r.a and r.b)") == set()
    assert _chains_of("not (r.a and r.b) or x.y") == {("x.y",)}
