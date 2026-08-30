"""FlowConfig: the research flow's knobs, ported from the fork's ``DRFlow*Config`` models.

Same fields, same defaults, same sub-model shapes as the fork's ``raven/config/raven.py``
(the frozen tree is the oracle); what changed is only the trunk seam it arrives through.
The slice reaches the plugin as a plain dict with camelCase keys exactly as the product
writes them in ``config.json`` (``plugins.config["research-flow"]``), so every model here
accepts both camelCase and snake_case (``alias_generator=to_camel`` +
``populate_by_name``) and ignores unknown keys instead of forbidding them - the slice
also carries plugin-only keys (``search.apiKey``, ``fetch.apiKey``, ``proxy``,
``stateRoot``) that the flow models do not own.

Four of the fork's fields are deliberately absent: ``toolsAllowlist``,
``minimalContext``, ``truncationWrapup`` and ``reactiveClamp`` were consumed by
the fork's LOOP, and no seam here reaches them. See ``_RETIRED_KEYS`` for who
owns each surface now and why they are not merely accepted and ignored.

``with_overlay`` is the per-mode entry point: a session mode's ``drFlow`` diff
(camelCase, arbitrary depth) is deep-merged over the base dict and the result
re-validated, so a mode changes exactly the knobs it names and nothing else.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class _Base(BaseModel):
    """Accepts both camelCase and snake_case keys; unknown keys are ignored."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class SearchSaturationConfig(_Base):
    """Stop or deepen a turn whose searches have stopped returning new documents."""

    enabled: bool = False
    identity: Literal["url", "docid"] = "url"
    k: int = 10
    on_saturate: Literal["widen", "paginate", "stop"] = "paginate"
    max_pages: int = 2


class SearchConfig(_Base):
    """Search-result shaping: strip everything that lets the model answer from
    the SERP without opening a page."""

    include_answer_box: bool = False
    include_knowledge_graph: bool = False
    include_snippets: bool = False
    snippet_dedup_by_docid: bool = False
    cross_query_dedup: bool = False
    search_depth: int = 20
    rendered_width: int = 5
    repeat_notice: bool = True
    saturation: SearchSaturationConfig = Field(default_factory=SearchSaturationConfig)


class DigestConfig(_Base):
    """Targeted extraction for web_fetch: long pages distilled to the request."""

    enabled: bool = True
    model: str | None = None
    threshold_chars: int = 8000
    timeout_seconds: float = 120.0
    verbatim_head_chars: int = 0


class VerifyConfig(_Base):
    """End-of-turn draft review gate (independent context, fail-open)."""

    enabled: bool = True
    model: str | None = None
    timeout_seconds: float = 360.0
    attempt_timeout_seconds: float = 120.0
    attempt_http_timeout_seconds: float | None = None
    reasoning_effort: str | None = None
    max_revisions: int = 1
    review_final_draft: bool = False
    max_tokens: int = 8192
    constraint_rubric: bool = False
    strict_reject_only: bool = False
    fail_open_on_elided_evidence: bool = True
    evidence_round: bool = False
    evidence_round_searches: int = 6
    evidence_round_depth: int = 50


class BudgetNoteConfig(_Base):
    """Budget visibility for the model: a budget line appended to tool results."""

    enabled: bool = True
    warn_ratio: float = 0.8


class ForceFinalizeConfig(_Base):
    """Forced-termination backstop: nudge an answerless terminal, then salvage."""

    enabled: bool = False
    max_nudges: int = 1
    model: str | None = None
    timeout_seconds: float = 240.0
    attempt_timeout_seconds: float = 240.0
    max_tokens: int = 8192
    reasoning_effort: str | None = "low"
    evidence_items: int = 8
    evidence_item_chars: int = 2000
    reasoning_excerpt_chars: int = 8000


class SpinBreakerConfig(_Base):
    """Restart-language circuit breaker on the iteration phases."""

    enabled: bool = False
    phrase_hits: int = 2
    min_budget_ratio: float = 0.5
    min_entity_overlap: int = 2
    max_triggers: int = 1


class FetchFloorConfig(_Base):
    """Search-without-fetch pathology guard: an in-history nudge to open pages."""

    enabled: bool = False
    min_searches: int = 5
    max_notes: int = 2


class FetchGateConfig(_Base):
    """Search-without-fetch guard at the action-space layer: withhold web_search."""

    enabled: bool = False
    k: int = 15
    release_after_failed_fetches: int = 2


class SufficiencyConfig(_Base):
    """First-round sufficiency gate: judge retrieved evidence, note when it answers."""

    enabled: bool = False
    model: str | None = None
    min_searches: int = 1
    min_fetches: int = 1
    timeout_seconds: float = 60.0
    attempt_timeout_seconds: float = 30.0
    max_tokens: int = 2048
    reasoning_effort: str | None = "low"
    evidence_items: int = 4
    evidence_item_chars: int = 2000


class FinalShapeConfig(_Base):
    """Terminal-answer shaping: make the turn's ending an observable."""

    record: bool = True
    require_marker: bool = True
    report_structure: bool = True
    report_format_override: bool = True
    report_depth: bool = False
    report_reminder: bool = True
    report_bounce: bool = False
    process_appendix: bool = True


class ConversationConfig(_Base):
    """Multi-turn behaviour: the per-turn research decision and the memo."""

    enabled: bool = False
    gate: Literal["always", "agentic"] = "agentic"
    gate_model: str | None = None
    gate_max_tokens: int = 1024
    gate_reasoning_effort: str | None = "low"
    gate_timeout_seconds: float = 20.0
    gate_history_messages: int = 6
    gate_history_chars: int = 4000
    research_memo: bool = True
    memo_max_chars: int = 2000
    memo_max_sources: int = 12
    memo_max_queries: int = 12
    memo_max_opened: int = 200
    identity_scope: Literal["turn", "topic"] = "turn"


class AskUserConfig(_Base):
    """Turn-boundary user interaction: one clarify round, optionally with an outline.

    Resolved against ``conversation.enabled`` at assembly: without a second turn
    the handoff has nowhere to land.
    """

    enabled: bool = False
    mode: Literal["when_needed", "first_turn"] = "first_turn"
    delivery: Literal["handoff", "tool"] = "handoff"
    outline: bool = True
    max_rounds: int = 1
    first_iteration_only: bool = True
    brief_requires_answer_check: bool = True
    reply_overlap_threshold: float = 0.05
    max_questions: int = 3
    max_outline_items: int = 5
    prompt_clause: bool = True
    brief: bool = False


# One rejection class: the base label of a superseded build. A denylist, so an
# invented label passes; suffixed variants ("dr@3.5-filetools-askuser") are
# legitimate - they name a profile, not a semantics - and only the base is checked.
SUPERSEDED_VERSIONS: tuple[str, ...] = (
    "dr@1",
    "dr@1.0",
    "dr@1.1",
    "dr@1.2",
    "dr@1.3",
    "dr@1.4",
    "dr@1.5",
    "dr@1.6",
    "dr@1.7",
    "dr@1.8",
    "dr@1.9",
    "dr@2.0",
    "dr@2.1",
    "dr@2.2",
    "dr@2.3",
    "dr@2.4",
    "dr@2.5",
    "dr@2.6",
    "dr@2.7",
    "dr@2.8",
    "dr@2.9",
    "dr@3.0",
    "dr@3.1",
    "dr@3.2",
    "dr@3.3",
    "dr@3.4",
)


# Knobs the fork's LOOP consumed, which no seam here reaches. They are kept OFF
# the model rather than accepted and ignored: a field that validates reads as
# live, and ``toolsAllowlist`` reads as the tool fence -- it was the fence in the
# fork (``_apply_dr_tools_allowlist`` unregistered everything else) and the fence
# here is ``tools.disabledTools``, one config level up and owned by the trunk.
# Named with their owner so a ported fork slice is told where the knob went
# instead of being silently disarmed.
_RETIRED_KEYS: dict[str, tuple[str, str]] = {
    "toolsAllowlist": ("tools_allowlist", "tools.disabledTools, applied by the trunk's registry"),
    "minimalContext": ("minimal_context", "context.dropSegments, applied by the trunk's context engine"),
    "truncationWrapup": ("truncation_wrapup", "nothing - the loop's generation path has no hook seam"),
    "reactiveClamp": ("reactive_clamp", "nothing - the loop's generation path has no hook seam"),
}


def _warn_retired(raw: dict[str, Any]) -> None:
    for camel, (snake, owner) in _RETIRED_KEYS.items():
        if camel in raw or snake in raw:
            logger.warning(
                "research-flow: drFlow.{} is no longer read by this flow; that surface is {}",
                camel,
                owner,
            )


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Overlay wins; dicts merge recursively; every other value replaces.

    An explicit ``null`` in the overlay replaces too (a mode may reset
    ``maxIterations`` to the agent default with ``"maxIterations": null``).
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class FlowConfig(_Base):
    """The research flow: observers, prompt texts, tool shaping and budgets.

    ``version`` names the flow semantics a batch ran under; the validator
    rejects a superseded base label on an enabled flow, exactly as the fork did.
    """

    enabled: bool = False
    version: str = "dr@3.5"
    max_iterations: int | None = None
    context_window_tokens: int | None = None
    think_closing_tag_required: bool = True
    prompt_section_override: str | None = None
    identity_override: str | None = None
    measured_guidance: bool = True
    fetch_max_chars: int = 14_000
    search: SearchConfig = Field(default_factory=SearchConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    budget_note: BudgetNoteConfig = Field(default_factory=BudgetNoteConfig)
    force_finalize: ForceFinalizeConfig = Field(default_factory=ForceFinalizeConfig)
    spin_breaker: SpinBreakerConfig = Field(default_factory=SpinBreakerConfig)
    fetch_floor: FetchFloorConfig = Field(default_factory=FetchFloorConfig)
    fetch_gate: FetchGateConfig = Field(default_factory=FetchGateConfig)
    sufficiency: SufficiencyConfig = Field(default_factory=SufficiencyConfig)
    final_shape: FinalShapeConfig = Field(default_factory=FinalShapeConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    ask_user: AskUserConfig = Field(default_factory=AskUserConfig)

    _SUPERSEDED_VERSIONS: ClassVar[tuple[str, ...]] = SUPERSEDED_VERSIONS

    @model_validator(mode="after")
    def _version_matches_build(self) -> "FlowConfig":
        """Reject a superseded base label on this build.

        Match the base label, not the whole string: suffixes name a profile, not
        a semantics, so ``dr@3.5-filetools-askuser`` passes while a bare
        superseded label - suffixed or not - is refused. The current label comes
        from the field default, never a literal, so a bump has one place to land.
        """
        base = self.version.split("-", 1)[0]
        if self.enabled and base in self._SUPERSEDED_VERSIONS:
            current = type(self).model_fields["version"].default
            raise ValueError(
                f"drFlow.version={self.version!r} predates this build's flow "
                f"semantics (base label {base!r}); set drFlow.version to {current!r} "
                "or later, keeping any profile suffix"
            )
        return self

    @property
    def ask_user_on(self) -> bool:
        """The clarify round, resolved the way the fork's assembly resolved it:
        a handoff needs a next turn to land in, and only the conversation
        surface has one."""
        return self.ask_user.enabled and self.conversation.enabled

    @classmethod
    def from_slice(cls, d: dict[str, Any] | None) -> "FlowConfig":
        """Validate the plugin's config slice as the product wrote it."""
        raw = d or {}
        _warn_retired(raw)
        return cls.model_validate(raw)

    def with_overlay(self, overlay: dict[str, Any] | None) -> "FlowConfig":
        """This config with a mode's ``drFlow`` diff deep-merged over it.

        The merge happens on the dict form and the result is re-validated, so an
        overlay is held to exactly the same schema as the base slice.
        """
        if not overlay:
            return self
        _warn_retired(overlay)
        base = self.model_dump(by_alias=True)
        return type(self).model_validate(_deep_merge(base, overlay))


__all__ = [
    "SUPERSEDED_VERSIONS",
    "AskUserConfig",
    "BudgetNoteConfig",
    "ConversationConfig",
    "DigestConfig",
    "FetchFloorConfig",
    "FetchGateConfig",
    "FinalShapeConfig",
    "FlowConfig",
    "ForceFinalizeConfig",
    "SearchConfig",
    "SearchSaturationConfig",
    "SpinBreakerConfig",
    "SufficiencyConfig",
    "VerifyConfig",
]
