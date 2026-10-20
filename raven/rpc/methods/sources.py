"""``sources.*`` RPC handlers -- the source registry.

Three methods back the registry:

* ``sources.list``       -- the rows: what each source's terms permit.
* ``sources.check``      -- may this deployment use this source for this use,
  and if not, why not and what would change the answer.
* ``sources.optin.set``  -- record, or withdraw, why this deployment may use a
  source at all.

The registry is the reason a run can say where a fact came from. It is also
the reason a run can be told no: a source whose terms do not permit a use is
refused, with the remedy written down in the registry rather than restated by
every caller that trips over it.

``sources.check`` never refuses. It answers, because the question "may I?"
has an answer either way and a caller that has to catch an exception to learn
"no" will catch it and carry on. The refusal that *does* raise is the one on
``sources.optin.set``: recording an acceptance for a source that is not in
the registry would put a permission on record for terms nobody has looked at.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.rpc.errors import InvalidParamsError, SourcePolicyRefusedError
from raven.rpc.models import (
    SourceOptInPayload,
    SourcePolicyPayload,
    SourcesCheckParams,
    SourcesListParams,
    SourcesOptinSetParams,
)

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


def _parse(model: Any, params: dict | None) -> Any:
    """The params, validated. A frame the handler can reject is -32602."""
    try:
        return model.model_validate(params if params is not None else {})
    except Exception as exc:  # noqa: BLE001 - pydantic's message is the report
        raise InvalidParamsError(f"{model.__name__}: {str(exc)[:400]}") from exc


def _policy_payload(policy: Any) -> SourcePolicyPayload:
    return SourcePolicyPayload(
        source=policy.source,
        label=policy.label,
        tier=policy.tier.value,
        citation=policy.citation,
        cache_allowed=policy.cache_allowed,
        store_allowed=policy.store_allowed,
        attribution_required=policy.attribution_required,
        derivation_allowed=policy.derivation_allowed,
        commercial_use=policy.commercial_use,
        retention=policy.retention,
        restrictions={use: reason for use, reason in policy.restrictions},
    )


def _optin_payload(grant: dict) -> SourceOptInPayload:
    return SourceOptInPayload(
        source=grant.get("source", ""),
        use=grant.get("use", "") or "",
        basis=grant.get("basis", ""),
        granted_at=grant.get("granted_at", ""),
        granted_by=grant.get("granted_by", "") or "",
    )


async def sources_list(params: dict) -> dict:
    """``sources.list`` -- the rows: what each source's terms permit."""
    parsed = _parse(SourcesListParams, params)
    from raven.sources.policy import SOURCE_POLICIES

    policies = list(SOURCE_POLICIES.values())
    if parsed.tier:
        policies = [policy for policy in policies if policy.tier.value == parsed.tier]
    return {
        "items": [_policy_payload(policy).model_dump() for policy in policies],
        "total": len(policies),
    }


async def sources_check(params: dict) -> dict:
    """``sources.check`` -- may this deployment use this source for this use.

    The answer is never an exception. A source that is not in the registry,
    a tier that needs a recorded reason, and a use the source does not permit
    all come back as ``allowed: false`` with the reason and the remedy, so
    the caller can show the operator what to change.
    """
    parsed = _parse(SourcesCheckParams, params)
    from raven.sources.policy import SourceRefusedError, consult, policy_for

    has_opt_in = parsed.has_opt_in
    if has_opt_in is None:
        from raven.sources.optins import has_opt_in as recorded

        has_opt_in = recorded(parsed.source, parsed.use)

    policy = policy_for(parsed.source)
    try:
        policy = consult(parsed.source, use=parsed.use, has_opt_in=has_opt_in)
    except SourceRefusedError as exc:
        # The tier is still worth reporting when the source is known: a
        # caller that has to look the row up again to learn it is restricted
        # is doing the registry's work twice.
        return {
            "allowed": False,
            "source": exc.source,
            "tier": policy.tier.value if policy is not None else "",
            "restriction": policy.restriction_for(parsed.use) if policy is not None else None,
            "reason": exc.reason,
            "remedy": exc.remedy,
            "citation": policy.citation if policy is not None else "",
        }

    return {
        "allowed": True,
        "source": policy.source,
        "tier": policy.tier.value,
        "restriction": policy.restriction_for(parsed.use),
        "reason": "",
        "remedy": "",
        "citation": policy.citation,
    }


async def sources_optin_set(params: dict) -> dict:
    """``sources.optin.set`` -- record, or withdraw, why this may be used.

    A source that is not in the registry is refused. Recording an acceptance
    for terms nobody has looked at would put a permission on record for a
    source the registry has never seen, and the next reader would take it as
    a checked deployment.
    """
    parsed = _parse(SourcesOptinSetParams, params)
    from raven.sources.optins import delete_optin, read_optins, write_optin
    from raven.sources.policy import policy_for

    if policy_for(parsed.source) is None:
        raise SourcePolicyRefusedError(
            f"the source {parsed.source!r} is not in the registry, so its terms have not been "
            f"looked at. Add a row for it before recording an acceptance."
        )

    use = parsed.use or ""
    if parsed.allowed:
        if not (parsed.basis or "").strip():
            raise InvalidParamsError(
                "recording an acceptance needs a basis: say why this deployment may use the source"
            )
        write_optin(
            parsed.source,
            basis=parsed.basis or "",
            use=use,
            granted_by=parsed.granted_by or "",
        )
        changed = True
    else:
        changed = delete_optin(parsed.source, use if use else None)

    opt_ins = read_optins().get(parsed.source, [])
    return {
        "opt_ins": [_optin_payload(grant).model_dump() for grant in opt_ins],
        "changed": changed,
    }


def register_sources_methods(dispatcher: "Dispatcher") -> None:
    dispatcher.register("sources.list", sources_list)
    dispatcher.register("sources.check", sources_check)
    dispatcher.register("sources.optin.set", sources_optin_set)


__all__ = [
    "register_sources_methods",
    "sources_check",
    "sources_list",
    "sources_optin_set",
]
