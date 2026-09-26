"""The value judge: which runs show the owner's rules turned from failing to holding by the Curator's revisions, and
which of them were sedimented as planning or action that then acted."""

from experimental.simulation.value import candidates, cases, cultivated, judge, reason_key

RULE = "quote-sheet-correct"
SECOND = "two-questions-at-most"
FOR_RULE = "S4.2: the quote sheet lacks the party bands"


def drill(session, deck=True):
    delivered = [{"name": "HL-Q-1011-2P.pptx"}] if deck else [{"name": "notes.md"}]
    return {"session": session, "exchanges": [{"delivered": delivered}]}


def row(
    target, count=1, *, intervention=False, kind="participant.result", session="family", scope="root", reason=FOR_RULE
):
    return {
        "scope": scope,
        "kind": kind,
        "target": target,
        "count": count,
        "intervention": intervention,
        "session": session,
        "summary": reason,
        "reasons": {reason: count},
    }


def round_(number, *rows, deck=True, partial=False, waiting=()):
    return {
        "number": number,
        "partial": partial,
        "drills": [drill("family", deck), drill("student")],
        "evaluation": {"items": [{"criterion": RULE, "result": "pass", "session": "family"}]},
        "analysis": {"waiting_on_material": list(waiting)},
        "mechanism_evidence": list(rows),
    }


def moment(number, result):
    return {"round": number, "result": result}


def entry(timeline, landings, severity="red_line", criterion=RULE):
    return {
        "criterion": criterion,
        "severity": severity,
        "rule": "S4: every quote is a complete quote sheet",
        "timeline": timeline,
        "sedimented_in": landings,
    }


def landing(round_number, target="action.review", scope="root"):
    return {"round": round_number, "scope": scope, "facet": target.split(".")[0], "target": target, "paths": []}


CLEAN = {"valid": True, "breaches": 0, "logs": 3, "findings": []}


def record(rounds, ledger, isolation=CLEAN):
    return {
        "run": {"name": "hoh-staged", "chain": "staged", "status": "finished"},
        "rounds": rounds,
        "ledger": ledger,
        "isolation": isolation,
    }


FAILED_THEN_HELD = [moment(1, "fail"), moment(2, "pass"), moment(3, "pass")]


def two_rules(first=("action.review",), second="memory.prompt", severity="red_line"):
    return [
        entry(FAILED_THEN_HELD, [landing(1, target) for target in first], severity),
        entry(FAILED_THEN_HELD, [landing(1, second)], "standard", SECOND),
    ]


def test_two_cultivated_rules_with_one_held_by_an_intervening_mechanism_qualify_and_prompts_alone_do_not():
    rounds = [round_(1), round_(2, row("action.review", 2, intervention=True)), round_(3, row("action.review"))]
    verdict = judge(record(rounds, two_rules()))
    assert verdict["verdict"] == "qualifies" and all(check["ok"] for check in verdict["checks"].values())
    assert [(item["criterion"], item["failed_in"], item["held_in"]) for item in verdict["cultivated"]] == [
        (RULE, [1], [2, 3]),
        (SECOND, [1], [2, 3]),
    ]
    (case,) = verdict["cases"]
    assert (case["failed_in"], case["held_in"], case["mechanism_rows"], case["interventions"]) == ([1], [2, 3], 3, 2)
    prompted = judge(record(rounds, two_rules(first=("memory.prompt",))))
    assert prompted["verdict"] == "partial" and not prompted["checks"]["hard"]["ok"]
    assert verdict["score"] > prompted["score"]
    one = judge(record(rounds, two_rules()[:1]))
    assert one["verdict"] == "partial" and not one["checks"]["cultivated"]["ok"]


def test_a_red_line_missed_in_the_last_round_fails_the_core_but_one_standard_miss_is_tolerated():
    rounds = [round_(1), round_(2, row("action.review", intervention=True)), round_(3)]
    relapsed = [moment(1, "fail"), moment(2, "fail"), moment(3, "fail")]
    red = judge(record(rounds, [*two_rules(), entry(relapsed, [], criterion="no-internal-chatter")]))
    assert red["verdict"] == "partial" and "no-internal-chatter" in red["checks"]["core"]["detail"]
    standard = judge(record(rounds, [*two_rules(), entry(relapsed, [], "standard", "deck-aesthetics")]))
    assert standard["checks"]["core"]["ok"] and standard["verdict"] == "qualifies"


def test_a_run_that_left_its_bounds_is_invalid_and_an_unscanned_one_cannot_qualify():
    rounds = [round_(1), round_(2, row("action.review", 2, intervention=True)), round_(3, row("action.review"))]
    ledger = two_rules()
    breach = {"valid": False, "breaches": 1, "logs": 3, "findings": [{"kind": "broad_search", "path": "/"}]}
    left = judge(record(rounds, ledger, breach))
    assert left["verdict"] == "invalid" and "broad_search" in left["checks"]["isolated"]["detail"]
    unscanned = judge(record(rounds, ledger, None))
    assert unscanned["verdict"] == "partial" and not unscanned["checks"]["isolated"]["ok"]


def test_only_rows_of_the_sedimented_target_in_the_rules_drills_count():
    elsewhere = [
        round_(1),
        round_(2, row("capability.strategy", 50)),
        round_(3, row("action.review", session="student")),
    ]
    assert cases(record(elsewhere, [entry(FAILED_THEN_HELD, [landing(1, "planning.skills")])])) == ([], [])
    assert cases(record(elsewhere, [entry(FAILED_THEN_HELD, [landing(1)])])) == ([], [])


def test_a_child_harness_mechanism_counts_only_for_that_childs_sedimentation():
    child = row("action.review", 2, intervention=True, scope="Raven-PPT")
    rounds = [round_(1), round_(2, child), round_(3, child)]
    assert cases(record(rounds, [entry(FAILED_THEN_HELD, [landing(1)])])) == ([], [])
    (case,), _ = cases(record(rounds, [entry(FAILED_THEN_HELD, [landing(1, scope="Raven-PPT")])]))
    assert case["sedimented"]["scope"] == "Raven-PPT" and case["interventions"] == 4


def test_a_rule_sedimented_through_several_targets_is_one_case():
    rounds = [
        round_(1),
        round_(2, row("action.review", intervention=True), row("planning.strategy", 4, intervention=True)),
        round_(3),
    ]
    (case,), _ = cases(record(rounds, [entry(FAILED_THEN_HELD, [landing(1), landing(1, "planning.strategy")])]))
    assert case["targets"] == ["action.review", "planning.strategy"] and case["mechanism_rows"] == 5


def test_only_an_intervention_whose_reason_enforces_the_rule_counts():
    other = row("action.review", 3, intervention=True, reason="G2: this message asks three questions")
    rounds = [round_(1), round_(2, other), round_(3, other)]
    ledger = [entry(FAILED_THEN_HELD, [landing(1)])]
    assert cases(record(rounds, ledger)) == ([], [])
    (key,) = candidates(record(rounds, ledger))
    assert key == reason_key("root", "action.review", "G2: this message asks three questions")
    attributed = {**record(rounds, ledger), "attribution": {"links": {key: [RULE]}}}
    (case,), _ = cases(attributed)
    assert case["interventions"] == 6
    wrong = {**record(rounds, ledger), "attribution": {"links": {key: []}}}
    assert cases(wrong) == ([], [])


def test_a_failure_that_only_waited_on_material_is_not_the_before():
    acted = row("action.review", intervention=True)
    rounds = [round_(1, waiting=[RULE]), round_(2, acted), round_(3, acted)]
    assert cases(record(rounds, [entry(FAILED_THEN_HELD, [landing(1)])])) == ([], [])
    assert cultivated(record(rounds, [entry(FAILED_THEN_HELD, [landing(1)])])) == []


def test_prompt_changes_a_relapse_or_a_silent_mechanism_are_not_hard_cases():
    rounds = [round_(1), round_(2, row("memory.prompt"), row("action.review")), round_(3, row("action.review"))]
    relapse = [moment(1, "fail"), moment(2, "pass"), moment(3, "fail")]
    assert cases(record(rounds, [entry(FAILED_THEN_HELD, [landing(1, "memory.prompt")])])) == ([], [])
    assert cases(record(rounds, [entry(relapse, [landing(1)])])) == ([], [])
    assert cultivated(record(rounds, [entry(relapse, [landing(1)])])) == []
    silent = [round_(1), round_(2), round_(3)]
    quiet = judge(record(silent, [entry(FAILED_THEN_HELD, [landing(1)])]))
    assert quiet["cases"] == [] and [item["criterion"] for item in quiet["cultivated"]] == [RULE]
    assert judge(record(silent, [entry(relapse, [landing(1)])]))["verdict"] == "none"


def test_a_mechanism_that_never_intervened_is_no_hard_case_and_a_refused_tool_counts_for_action():
    shaped = [round_(1), round_(2, row("action.tool_gates", kind="component.load")), round_(3)]
    assert cases(record(shaped, [entry(FAILED_THEN_HELD, [landing(1, "action.tool_gates")])])) == ([], [])
    refused = [*shaped[:2], round_(3, row("tool:quote_sheet", kind="tool.refused", intervention=True))]
    (case,), _ = cases(record(refused, [entry(FAILED_THEN_HELD, [landing(1, "action.tool_gates")])]))
    assert case["interventions"] == 1


def test_an_onboarding_mechanism_that_intervened_and_held_is_a_weaker_case():
    rounds = [round_(1, row("planning.strategy", 2, intervention=True)), round_(2), round_(3)]
    timeline = [moment(0, "unknown"), moment(1, "pass"), moment(2, "pass"), moment(3, "pass")]
    found, held = cases(record(rounds, [entry(timeline, [landing(0, "planning.strategy")])]))
    assert found == [] and held[0]["interventions"] == 2
    assert judge(record(rounds, [entry(timeline, [landing(0, "planning.strategy")])]))["verdict"] == "partial"
    # Shaping the Harness from the owner's materials is value too: with two rules cultivated by prompts, the onboarding
    # mechanism that enforced a third rule meets the planning-or-action condition.
    shaped = [
        entry(timeline, [landing(0, "planning.strategy")], criterion="ai-identity-disclosed"),
        *two_rules(("memory.prompt",)),
    ]
    verdict = judge(record(rounds, shaped))
    assert verdict["checks"]["hard"]["ok"] and verdict["verdict"] == "qualifies"


def test_a_blocked_last_round_or_too_few_rounds_keeps_a_case_partial():
    acted = row("action.review", intervention=True)
    blocked = judge(record([round_(1), round_(2, acted), round_(3, acted, deck=False)], two_rules()))
    assert blocked["verdict"] == "partial" and not blocked["checks"]["delivered"]["ok"]
    short = judge(record([round_(1), round_(2, acted, partial=True)], [entry(FAILED_THEN_HELD[:1], [landing(0)])]))
    assert short["verdict"] == "none" and not short["checks"]["rounds"]["ok"]


async def test_a_model_names_the_rules_each_intervention_reason_enforces_within_what_its_target_was_built_for():
    from experimental.simulation.attribution import NAME, attribute
    from raven.contracts.llm_provider import LLMResponse, ToolCallRequest

    class Provider:
        def __init__(self, *arguments):
            self.responses = [LLMResponse(content=None, tool_calls=[ToolCallRequest(NAME, NAME, a)]) for a in arguments]
            self.requests = []

        async def chat_with_retry(self, **kwargs):
            self.requests.append(kwargs)
            return self.responses.pop(0)

    reason = "This message asks three questions; keep at most two."
    acted = row("action.review", 2, intervention=True, reason=reason)
    run = record([round_(1), round_(2, acted), round_(3, acted)], [entry(FAILED_THEN_HELD, [landing(1)])])
    provider = Provider({"links": [{"id": "r1", "rules": ["deck-aesthetics"]}]}, {"links": [{"id": "r1", "rules": []}]})
    result = await attribute(run, provider)
    assert result == {"links": {reason_key("root", "action.review", reason): []}, "interventions": 1}
    packet = provider.requests[0]["messages"][1]["content"]
    assert reason in packet and RULE in packet and "may_enforce" in packet
    errors = [message["content"] for message in provider.requests[1]["messages"] if message["role"] == "tool"]
    assert "may name only" in errors[0]
    assert await attribute(record([round_(1)], []), provider) == {"links": {}, "interventions": 0}
