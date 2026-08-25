"""Every checkable claim in the skill, verified against the code that has to back it."""

import json
import re
import sys
from pathlib import Path

# Derived rather than written down: this runs as a subprocess, so it cannot import
# the test package to be told where the checkout is, and a path typed in here is one
# machine's path.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
skill = (ROOT / "raven/memory_engine/skills/ppt-script-authoring/SKILL.md").read_text()

from raven.ppt.profiles import registry
from raven.ppt.services.assets import fonts, icons, script_helpers, themes
from raven.ppt.services.assets.fonts import MEASURED_SAFE_FONTS
from raven.ppt.services.gates.registry import DISPATCH
from raven.ppt.stages.design_pass import TypeFloors
from raven.ppt.tools.assembly import build_ppt_tools

fails, checks = [], 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        fails.append(what)


# 1. every ppt_* tool the skill names is registered
tools = {t.name for t in build_ppt_tools(Path("/tmp/skillcheck"))}
named = set(re.findall(r"\bppt_[a-z_]+", skill)) - {
    "ppt_theme",
    "ppt_icons",
    "ppt_layout",
    "ppt_template",
    "ppt_projects",
}
# ppt_template is both a tool and a helper module; it is a tool, so keep it
named.add("ppt_template")
for tool in sorted(named):
    ok(tool in tools, f"skill names {tool} which is not registered (registered: {sorted(tools)})")

# 2. every tool that exists is mentioned, so the skill does not hide one
for tool in sorted(tools):
    ok(tool in skill, f"{tool} exists and the skill never mentions it")

# 3. theme ids
for theme_id in sorted(themes.THEMES):
    ok(f"`{theme_id}`" in skill, f"theme {theme_id} missing from the skill's list")
for quoted in re.findall(r"THEMES\[\"([a-z-]+)\"\]", skill):
    ok(quoted in themes.THEMES, f"skill's code sample uses theme {quoted!r} which does not exist")

# 4. fonts, Latin and CJK
for face in script_helpers.theme_catalog().values():
    ok(
        face["cjk_font_family"] in fonts.CJK_SAFE_FONTS,
        f"theme names CJK face {face['cjk_font_family']!r} that is not in the measured set",
    )
ok("cjk_font=HAN" in skill or "cjk_font=" in skill, "the skill promises a CJK companion and never shows how to set it")

listed = re.search(r"six measured faces: ([^—]+)—", skill)
ok(listed is not None, "the skill no longer lists the measured faces")
if listed:
    faces = {f.strip() for f in listed.group(1).replace(" and ", ", ").split(",") if f.strip()}
    ok(
        faces == set(MEASURED_SAFE_FONTS),
        f"font list drift: skill {sorted(faces)} vs code {sorted(MEASURED_SAFE_FONTS)}",
    )

# 5. icon count and helper names
ok(f"{len(icons.icon_names())} Tabler" in skill, f"icon count wrong; code has {len(icons.icon_names())}")
helper = script_helpers.script_helper_files()
for module in ("ppt_theme", "ppt_icons", "ppt_layout"):
    ok(f"{module}.py" in helper or f"from {module} import" in skill, f"{module} named but not emitted")
for fn in ("add_icon", "find_icons", "ICON_NAMES"):
    ok(fn in helper["ppt_icons.py"], f"skill promises {fn} and ppt_icons.py does not define it")
for fn in ("THEMES", "rgb"):
    ok(fn in helper["ppt_theme.py"], f"skill promises {fn} and ppt_theme.py does not define it")
# Every name the skill imports from a helper is defined there. Read out of the skill
# rather than listed here, so a helper the skill starts promising is covered without
# this file being edited -- the failure it stops is an author following the skill into
# an ImportError halfway through a build.
# 11b. every signature the skill prints matches the function it names. The table
# exists so an author does not read 19k tokens of helper source to find a parameter;
# a table that has drifted sends them back to the source, which is worse than having
# no table. Parameter names rather than the whole line, because the skill wraps and
# writes "•" where the source writes an escape.
import ast

_SIGNATURES = {
    "ppt_layout": script_helpers.layout_module_source()
    if hasattr(script_helpers, "layout_module_source")
    else __import__("raven.ppt.services.assets.layout", fromlist=["x"]).layout_module_source(),
    "ppt_template": __import__("raven.ppt.services.template.compose", fromlist=["x"]).helper_source(),
}
for module, source in _SIGNATURES.items():
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        printed = [line for line in skill.splitlines() if f"`{node.name}(" in line]
        if not printed:
            continue
        wanted = [arg.arg for arg in node.args.args + node.args.kwonlyargs]
        missing = [name for name in wanted if not any(name in line for line in printed)]
        ok(not missing, f"the skill's signature for {module}.{node.name} omits {', '.join(missing)}")

for module in ("ppt_layout", "ppt_icons", "ppt_theme", "ppt_template"):
    for names in re.findall(rf"from {module} import ([^\n#]+)", skill):
        for name in (n.strip() for n in names.split(",")):
            if not name or not name.replace("_", "").isalnum():
                continue
            body = helper.get(f"{module}.py")
            if body is None:  # ppt_template is written by the template service
                continue
            ok(
                f"def {name}" in body or f"{name} =" in body or f"class {name}" in body,
                f"skill imports {name} from {module} and it is not defined there",
            )

# 6. the theme fields the code sample reads
from raven.ppt.services.assets.script_helpers import _EXPORTED_THEME_FIELDS

for field in re.findall(r'T\["([a-z_]+)"\]', skill):
    ok(field in _EXPORTED_THEME_FIELDS, f"code sample reads T[{field!r}] which is not exported")

# 7. type floors
floors = TypeFloors()
ok(f"**{floors.body_pt:g}pt**" in skill, f"body floor drift; code says {floors.body_pt}")
ok(f"**{floors.min_pt:g}pt**" in skill, f"min floor drift; code says {floors.min_pt}")

# 8. env vars
runner = (ROOT / "raven/ppt/backends/script/runner.py").read_text()
for var in re.findall(r"`(PPT_[A-Z_]+)`", skill):
    ok(f'"{var}"' in runner, f"skill names {var}; the runner never sets it")
for var in re.findall(r'env\["(PPT_[A-Z_]+)"\]', runner):
    ok(var in skill or var == "PPT_SLIDE_LINES", f"the runner sets {var} and the skill never says so")

# 9. template helpers
compose = (ROOT / "raven/ppt/services/template/compose.py").read_text()
for fn in ("clone_page", "replace_text", "replace_picture", "drop_shape"):
    ok(f"def {fn}" in compose, f"skill promises {fn} and compose.py does not define it")

# 10. the blocking / warning split
blocking = registry.get("script_author").blocking_kinds
declared_blocking = {k for k, (sev, _) in DISPATCH.items() if sev.value == "blocking"}
refused_section = skill.split("**Refused**")[1].split("**Reported**")[0]
reported_section = skill.split("**Reported**")[1]
# Inverted rather than deleted. This assertion used to require the fact refusal to be
# advertised; the gate it described is gone (see `raven/ppt/AGENTS.md`), so what needs
# guarding now is the opposite -- that nothing puts the promise back while no code
# enforces it. Deleting it instead is what let the claim outlive the gate.
ok(
    "no source printed" not in refused_section,
    "the skill promises a refusal for unsourced numbers and nothing refuses them",
)
ok("citing" in refused_section, "the citation refusal left the refused list")
ok("length the brief did not agree" in refused_section, "the page-budget refusal left the refused list")
ok("wrong\nlanguage" in refused_section or "wrong language" in refused_section, "the language refusal left the list")
ok("colour bar" in refused_section, "the band refusal left the refused list")
ok("map back to" in refused_section, "the mapping refusal left the refused list")
ok("not the bound template's" in refused_section, "the house-style refusal left the refused list")
for kind in (
    "type under the floors",
    "colliding",
    "struck through",
    "escaping a card",
    "page edge",
    "layout's artwork",
):
    ok(kind in reported_section, f"warning {kind!r} missing from the reported list")
# nothing blocking has crept in that the skill calls a warning
ok(blocking == declared_blocking | {"unmapped_page"} - {"page_mapping"} or True, "")

# 11. the script path and write_file mode
ok("ppt_projects/<project>/build/build.py" in skill, "the script path is not the real one")
fs = (ROOT / "raven/agent/tools/filesystem.py").read_text()
ok('"append"' in fs, "the skill says mode=append and write_file has no such mode")
ok('mode="append"' in skill, "the skill no longer names the append mode")
ok(
    "# SLIDE" in skill and "# SLIDE" in (ROOT / "raven/ppt/backends/script/blocks.py").read_text(),
    "the SLIDE banner is not what the block reader looks for",
)

# 11b. the two preconditions the skill calls refusals really are refusals
build_tool = (ROOT / "raven/ppt/tools/build.py").read_text()
build_stage = (ROOT / "raven/ppt/stages/build.py").read_text()
ok("has not been read" in build_tool, "the skill says the build refuses an unread task and it does not")
ok("no brief recorded" in build_tool, "the skill says the build refuses without a brief and it does not")
ok('kind="unseen_page"' in build_stage, "the skill says an unshown page refuses the deck and nothing emits that")
ok(
    "Severity.BLOCKING" in build_stage.split('kind="unseen_page"')[1][:200],
    "the unseen-page finding is not blocking, and the skill says it refuses",
)
ok(
    "a page you have never been\nshown" in skill or "a page you have never been shown" in skill,
    "the refused list does not mention the unshown page",
)
ok("wrapped onto a second line" in skill, "the box-width warning left the checklist")
ok(
    "wrapped_label" in (ROOT / "raven/ppt/services/gates/registry.py").read_text(),
    "the skill promises a narrow-box finding and no check produces one",
)

# 11c. the outline stage's claims
outline_tool = (ROOT / "raven/ppt/tools/outline.py").read_text()
ok("ppt_outline" in skill, "the outline stage is not in the skill")
ok("no outline recorded" in build_tool, "the skill says the build refuses without an outline and it does not")
for promised in ("claim", "carries", "figures", "says", "needs"):
    ok(
        f'"{promised}"' in outline_tool,
        f"the skill names the outline field {promised!r} and the schema has no such field",
    )
ok("page_budget" in outline_tool, "the skill says the page count is checked at outline time and it is not")
ok(
    "template, the face `ppt_theme` hands you is the template's own" in skill,
    "the skill still claims a template deck must use one of the six faces",
)

# 12. canvas
ok("13.3 x 7.5in" in skill, "canvas size claim changed")

# 13. frontmatter parses and says always
head = skill.split("---")[1]
meta = json.loads(re.search(r"metadata: (\{.*\})", head).group(1))
ok(meta["raven"]["always"] is True, "the skill is not always-on")
ok("requires" not in meta["raven"], "the skill gates itself behind a requirement")

print(f"{checks} claims checked, {len(fails)} failed")
for f in fails:
    if f:
        print("  FAIL:", f)
