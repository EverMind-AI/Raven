"""The ops tool set's manifest factories: one per contributed tool, one gate.

Every factory runs the same two steps before constructing its face: the
product gate (an absent or disabled slice casts no surface at all -- the D6
shape, same as the watcher's factory), and the shared wiring (the campaign
root from the slice's ``stateRoot``, installed once into ``tools.base``).
Declining is returning ``None``; the plugin lane skips a decliner quietly.
Every constructed face is adopted onto the roster in ``tools.base`` on the way
out: the part-2c turn hook contexts and reads the SAME instances the loop
registered, and the roster is the two lanes' one meeting point.

The scheduling faces (ops_submit, ops_check_later, ops_ask_owner, ops_finish)
additionally declare ``bind_runtime`` (on ``tools.base._OpsScheduler``) and
hold the namespaced wake grant the loop mints at bind time -- the same
namespace the watcher's service grant uses, which is what lets a wake one of
these tools schedules be the very wake the watcher advances. A host that
lends no scheduler leaves the grant ``None`` and every schedule answers in
the fork's own "no scheduler" prose; the tool stays registered, because
reading a campaign never needed a timer.

``ops_tune_launch`` has no factory here on purpose (fork parity: full-auto
stays off the agent's menu; ``python -m oncall_flow.tune`` is the D5 entry).
The machine face is not contributed at all: trunk exec carries the fork's
``machine`` parameter itself, which closed the same-name-shadow ruling the
draft's deviation table left open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oncall_flow.tools import base

if TYPE_CHECKING:
    from raven.plugins.context import PluginContext


def _wired(ctx: "PluginContext") -> bool:
    """The product gate plus the shared wiring; False means every factory declines."""
    from oncall_flow.config import FlowConfig, state_root

    raw = dict(ctx.config or {})
    if not FlowConfig.from_slice(raw).enabled:
        return False
    base.set_home(state_root(raw, ctx.services.workspace))
    return True


def make_ops_tune_status(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops import OpsTuneStatusTool

    return base.adopt(OpsTuneStatusTool())


def make_ops_submit(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops import OpsSubmitTool

    return base.adopt(OpsSubmitTool())


def make_ops_check_later(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops import OpsCheckLaterTool

    return base.adopt(OpsCheckLaterTool())


def make_ops_note(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops import OpsNoteTool

    return base.adopt(OpsNoteTool())


def make_ops_campaigns(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops import OpsCampaignsTool

    return base.adopt(OpsCampaignsTool())


def make_ops_kill(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops import OpsKillTool

    return base.adopt(OpsKillTool())


def make_ops_connections(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops_connections import OpsConnectionsTool

    return base.adopt(OpsConnectionsTool())


def make_ops_declare(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops_declare import OpsDeclareTool

    return base.adopt(OpsDeclareTool())


def make_ops_outputs(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops_observe import OpsOutputsTool

    return base.adopt(OpsOutputsTool())


def make_ops_edit_case_dict(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops_case_dict import OpsEditCaseDictTool

    return base.adopt(OpsEditCaseDictTool())


def make_ops_case_changes(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops_case_dict import OpsCaseChangesTool

    return base.adopt(OpsCaseChangesTool())


def make_ops_ask_owner(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops_escalation import OpsAskOwnerTool

    return base.adopt(OpsAskOwnerTool())


def make_ops_finish(ctx: "PluginContext"):
    if not _wired(ctx):
        return None
    from oncall_flow.tools.ops_escalation import OpsFinishTool

    return base.adopt(OpsFinishTool())
