"""Associate effective components with sourced behavior, never infer authority from a role."""

from inspect import getdoc

from ...harness.strategies import ActionStrategy, CapabilityStrategy, MemoryStrategy, PlanningStrategy
from ...harness.view import Mechanism
from ..targets import catalogue

STRATEGIES = {
    "memory": MemoryStrategy,
    "planning": PlanningStrategy,
    "capability": CapabilityStrategy,
    "action": ActionStrategy,
}


def describe(facts, sources, declaration):
    result = []
    granted = {target.name: target for target in declaration.targets}

    def add(name, roles, channels, description, components, references, targets=(), gaps=()):
        missing = [f"Missing mechanism reference: {ref}" for ref in references if ref not in sources]
        result.append(
            Mechanism(
                name=name,
                roles=roles,
                channels=channels,
                description=description,
                components=components,
                sources=tuple(ref for ref in references if ref in sources),
                targets=tuple(target for target in targets if target in granted),
                gaps=(*gaps, *missing),
            )
        )

    native = {
        "memory": (
            ("model_input",),
            "Context assembly and history/window handling; the native component is not the complete semantic Memory responsibility.",
            "context-and-resources",
        ),
        "planning": (
            ("model_input",),
            "Native message preparation and participant advice composition. This component alone does not establish whether tools or hooks maintain a plan.",
            "strategies-and-channels",
        ),
        "capability": (
            ("model_input", "tool_interaction"),
            "Expose registered tool definitions to the model. Visibility, resource availability and execution permission remain distinct.",
            "tools-and-extensions",
        ),
        "action": (
            ("model_decision", "execution_control"),
            "Model decision and participant control decisions are consumed by the native Loop, with its configured limits and origin conditions.",
            "participation-and-control",
        ),
    }
    for role, (channels, description, reference) in native.items():
        if role in facts.get("native_modules", {}):
            add(
                f"native.{role}",
                (role,),
                channels,
                description,
                (("native_modules", role),),
                (f"instance.{role}", f"reference.{reference}"),
            )
    if "turn_profile" in facts:
        add(
            "session.mode",
            ("action",),
            ("model_decision", "execution_control"),
            "The prepared agent's declared mode is applied through Raven session policy before each turn. Its iteration cap and reasoning effort can override connection defaults; plugin hooks receive its overlay. Inspect this profile when changing overlapping configuration.",
            (("turn_profile",),),
            ("host.mode_catalogue", "host.session_policy"),
        )
    for i, tool in enumerate(facts.get("tools", ())):
        name = tool["function"]["name"]
        add(
            f"tool.{name}",
            ("capability",),
            ("model_input", "tool_interaction"),
            f"Registered tool {name}; the component reference provides its definition and usage contract. Execution uses the registry's parameter and permission checks.",
            (("tools", i),),
            (f"tool.{name}", "reference.tools-and-extensions"),
            ("capability.select_tools", "capability.tool_config"),
        )
    for name in facts.get("bootstrap", {}):
        add(
            f"bootstrap.{name}",
            ("memory",),
            ("model_input",),
            "Current bootstrap content; its consumer and composition are described by the context reference. Presence does not prove every request includes it.",
            (("bootstrap", name),),
            (f"bootstrap.{name}", "reference.context-and-resources"),
            ("memory.prompt",),
        )
    for i, skill in enumerate(facts.get("skills", ())):
        ref = f"skill.{skill['source']}/{skill['name']}"
        add(
            ref,
            ("capability",),
            ("model_input", "tool_interaction"),
            "Discovered skill and its usage knowledge. Registry availability does not establish that its body was read or its procedure executed.",
            (("skills", i),),
            (ref, "reference.context-and-resources"),
            ("planning.skills", "planning.skill_config"),
        )
    for i, hook in enumerate(facts.get("hooks", ())):
        add(
            f"hook.{i}",
            (),
            (),
            f"Loaded hook {hook['name']}; inspect its implementation to determine conditional behavior and responsibilities.",
            (("hooks", i),),
            (f"hook.{i}", "reference.participation-and-control"),
            gaps=(
                "Hook responsibilities are not inferred from its name. Resolve affected behavior before modifying it.",
            ),
        )
    for i, plugin in enumerate(facts.get("plugins", ())):
        identifier = plugin["id"]
        references = tuple(name for name in sources if name.startswith(f"plugin.{identifier}."))
        add(
            f"plugin.{identifier}",
            ("capability",),
            ("tool_interaction", "execution_control"),
            "Activated plugin manifest and configuration. Factories may decline; contributed names alone do not prove construction or execution. Disabling a plugin affects all its contributions.",
            (("plugins", i),),
            (*references, "reference.tools-and-extensions"),
            ("capability.plugins",),
            gaps=() if references else ("Plugin implementation package was not located.",),
        )
    authored = facts.get("authored", {}).get("values", {})
    for target in catalogue():
        if target.name not in authored:
            continue
        refs = (target.name, *(key for key in sources if key.startswith(f"{target.name}.knowledge.")))
        add(
            f"authored.{target.name}",
            target.roles,
            target.channels,
            target.effect
            + " This is an installed binding contract; actual execution and task benefit require observations. Unchanged native mechanisms remain active unless explicitly disabled.",
            (("authored", "values", target.name),),
            refs,
            (target.name,),
        )
    code_hook = next(
        (i for i, h in enumerate(facts.get("hooks", ())) if h.get("type", "").startswith("code_flow.")), None
    )
    todo = next(
        (
            i
            for i, t in enumerate(facts.get("tools", ()))
            if t["function"]["name"] == "todo" and "code_flow" in str(sources.get("tool.todo", {}).get("path", ""))
        ),
        None,
    )
    if code_hook is not None and todo is not None:
        add(
            "code.plan",
            ("planning", "capability", "memory"),
            ("tool_interaction", "model_input"),
            "The todo tool and CodeParticipant share a session-bound TodoStore. Reads are detached; writes replace the full list and persist before acknowledgement. Statuses are agent reports, not independently verified completion. The hook restores the saved plan only when its current revision is absent from the visible transcript. A generated PlanningStrategy does not automatically replace this mechanism or acquire its store.",
            (("tools", todo), ("hooks", code_hook)),
            ("tool.todo", f"hook.{code_hook}", "reference.code-planning"),
            ("capability.plugins", "capability.tool_config"),
        )
    return tuple(result)


def roles():
    return {
        name: {"responsibility": getdoc(contract) or "", "source": f"strategy.{name}"}
        for name, contract in STRATEGIES.items()
    }
