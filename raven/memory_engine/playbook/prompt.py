"""Prompt assembly for playbook generation — the tunable part of the module.

Everything the generation LLM is told lives here; ``generator.py`` holds no
prompt text. Three pieces:

- :data:`SYSTEM_PROMPT` — the static skeleton: the field definition's
  filling rules plus two compact few-shot sketches, one per mode.
- :func:`build_generation_prompt` — renders the live inventories (agent
  roster, retrieved skill candidates, mcps) and the user input.
- :func:`build_repair_prompt` — the validation-failure follow-up. Asks for
  a minimal edit, not a rewrite, so already-correct regions stay put.

The output is requested through a forced tool call whose parameter schema
is :func:`emit_tool` — derived from the pydantic contract at call time so
the two can never drift apart.
"""

from __future__ import annotations

import json
from typing import Any

from raven.memory_engine.playbook.types import PlaybookSpec

EMIT_TOOL_NAME = "emit_playbook"

# LLM-produced fields only; code-filled ones are stripped from the schema so
# the model cannot fight the generator over them. name is code-derived
# (slug), version is the format version, status/provenance-lifecycle are
# generator-side.
_CODE_FILLED_TOP = {"version", "status"}
_CODE_FILLED_PROVENANCE = {"source_input", "missing_capabilities"}


def emit_tool() -> list[dict[str, Any]]:
    """The forced-call tool schema, derived from :class:`PlaybookSpec`."""
    schema = PlaybookSpec.model_json_schema(by_alias=True)
    for key in _CODE_FILLED_TOP:
        schema.get("properties", {}).pop(key, None)
    schema["required"] = [r for r in schema.get("required", []) if r not in _CODE_FILLED_TOP]
    prov = schema.get("$defs", {}).get("Provenance", {})
    for key in _CODE_FILLED_PROVENANCE:
        prov.get("properties", {}).pop(key, None)
    return [
        {
            "type": "function",
            "function": {
                "name": EMIT_TOOL_NAME,
                "description": "Emit the generated playbook.",
                "parameters": schema,
            },
        }
    ]


SYSTEM_PROMPT = """\
你是 playbook 生成器。用户给一段输入，你产出一份可复用的任务模板（playbook），\
通过 emit_playbook 工具提交。playbook 描述"这一族任务怎么由子 agent 分步完成"，\
供之后同族任务复用执行。字段用 camelCase。

# mode 判定（最重要的规则）

- 用户输入**显式给出了步骤**（"先A，再B，最后C"、编号清单、一段 SOP）→ `mode: "dag"`，图写死在 `nodes`。
  你只做**忠实转译**：每个用户步骤转成一个节点，不增加步骤、不删除步骤、不改变顺序。
  你自己可以加的只有：参数识别、每个节点绑定哪个 agent/skills。
- 用户输入**没有给出步骤** → `mode: "prompt"`，不要替用户猜死一张图。
  写 `prompts`：**组图指导**——运行时由模型按它当场拼出一张图再执行。
- dag 不写 prompts；prompt 不写 nodes。两者拿到图之后走完全相同的执行链。

# name / description

- `name`：小写连字符 slug（如 competitor-scan）。
- `description`：≤200 字，写"什么时候该用我"（供检索匹配），不是体裁标签。

# triggers

3~8 个种子词进 `keywords`（单词和短语都放这里）：领域专名、动作短语（"发推""对比竞品"）。
禁收机制词（playbook/流程/模板/自动化）和日常高频词（文章/报告/数据）。

# params

每次运行会变的输入抽成 params（map，键=参数名）。`description` 必填——它同时是缺参时
向用户追问的话术，要写成能直接问出口的样子。有 default 的参数永不追问。
模板里用 ${params.<键>} 引用。生成时能定死的不要做成参数。

# nodes（dag 模式）

- `agent` 只能从下面"可用 agent"清单里选。
- 配置挂节点不挂角色：`skills`/`mcps` 是**本步**要注入的，同一 agent 跑多步可以每步不同。
  skills 只能引用候选清单里的名字。
- `promptTemplate` 是本步任务书：写判据、禁令、格式硬约束，说清这一步做什么。
  引用上游输出用 {{ 上游id.output }}（内容）或 {{ 上游id.output_path }}（文件路径），
  **上游必须列在本节点 dependsOn 里**。
- `dependsOn` 是全部图语言：空=起点；多个=汇合；无依赖关系的节点自动并行。
- 同一 agent 连续步骤要延续上一步记忆（改稿要记得原稿）→ 相同 `instance`，且两节点间必须有依赖；
  需要换视角（审、批判）→ 不共享 instance。同 instance 的节点 skills/mcps 必须一致。
- 不可逆动作的节点（发布、提单、发信、写库）标 `confirm: true`。

# prompts（prompt 模式）

写**怎么拼这张图**：分几层、每层几个节点、各用什么 agent 配什么 skills/mcps、
谁 dependsOn 谁、节点间怎么用 {{ }} 传数据、每个节点任务书要写明什么判据。
不要写"跑起来之后怎么判断"——图组装一次就固定，运行期没有决策点，
循环和条件回退表达不了，不要写进去。

# provenance（如实自报）

- `specifiedByUser`：用户明说的事实，逐条列出（之后修订时这些不可动）。
- `inferred`：你推断的每一处设计，带 reason 和 confidence；confidence=low 的应改为提问。
- `blockingQuestions`：缺了就没法定稿的问题。宁可问，不要静默编造。
- `assumptions`：你采用的默认，用户可推翻。

# 例（dag：用户给了步骤，忠实转译）

输入："每周反馈分析：先拉 slack 和 intercom 的反馈，然后聚类出主题，最后写份给 PM 的周报"
要点：mode=dag；三个节点 pull→cluster→report 对应用户三步（不加不减）；
pull 用 data 类 agent 挂数据 skills，report 用 content 类 agent；params: {week_of}；
report 的模板引用 {{ cluster.output }} 且 dependsOn: [cluster]。

# 例（prompt：用户没给步骤）

输入："帮我做一个尽调 playbook"
要点：mode=prompt；prompts 写组图指导——"第一层一个 research 节点广度扫描产出三栏；
第二层按 focus 铺开若干并行节点全部 dependsOn 第一层、各自独立不共享 instance；
第三层一个 content 节点汇总 dependsOn 全部第二层"；params: {target, focus}；
blockingQuestions 问尽调用途。
"""


def _render_skills(candidates: list[tuple[str, str]], user_pinned: list[str]) -> str:
    if not candidates and not user_pinned:
        return "（无候选，skills 留空）"
    lines = [f"- {name}: {desc}" for name, desc in candidates]
    if user_pinned:
        lines.insert(0, f"用户显式指定（必须使用）: {', '.join(user_pinned)}")
    return "\n".join(lines)


def build_generation_prompt(
    user_input: str,
    *,
    agent_roster: dict[str, str],
    skill_candidates: list[tuple[str, str]],
    user_pinned_skills: list[str],
    known_mcp: list[str],
    inline_skill_docs: list[tuple[str, str]] | None = None,
) -> str:
    """Render the user message for one generation call.

    ``agent_roster`` maps agent name -> capability description;
    ``skill_candidates`` are ``(name, one-line description)`` from the
    three-way retrieval; ``inline_skill_docs`` are ``(name, content)`` for
    skill *files* the user handed in directly.
    """
    roster = "\n".join(f"- {name}: {desc}" for name, desc in agent_roster.items())
    parts = [
        "# 可用 agent（nodes[].agent 只能从这里选）\n" + roster,
        "# 候选 skills（只能引用这些名字）\n" + _render_skills(skill_candidates, user_pinned_skills),
        "# 可用 mcp\n" + (", ".join(known_mcp) if known_mcp else "（无——mcps 留空）"),
    ]
    for name, content in inline_skill_docs or []:
        parts.append(f"# 用户提供的 skill 文件：{name}\n{content}")
    parts.append("# 用户输入\n" + user_input.strip())
    return "\n\n".join(parts)


def build_repair_prompt(spec_json: dict[str, Any], errors: list[str]) -> str:
    """Follow-up message after a failed validation round."""
    numbered = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(errors))
    return (
        "你上一次提交的 playbook 未通过校验。逐条修复下面的错误，"
        "重新调用 emit_playbook 提交完整结果。只改错误涉及的部分，"
        "其余字段保持原样，不要重写。\n\n"
        f"# 校验错误\n{numbered}\n\n"
        f"# 你上一次的提交\n{json.dumps(spec_json, ensure_ascii=False, indent=2)}"
    )


def build_revise_prompt(current: PlaybookSpec, user_feedback: str) -> str:
    """User message for a revise round on an existing playbook."""
    immutable = current.provenance.specified_by_user
    immutable_block = "\n".join(f"- {item}" for item in immutable) if immutable else "（无）"
    return (
        "下面是一份已有的 playbook 和用户对它的反馈。按反馈修订，"
        "重新调用 emit_playbook 提交完整结果。\n\n"
        "# 不可动区（用户此前明确要求的，必须原样保留在 specifiedByUser 里且设计不得违背；"
        "本次反馈新明确的事实要追加进去）\n"
        f"{immutable_block}\n\n"
        f"# 当前 playbook\n{json.dumps(current.model_dump(by_alias=True), ensure_ascii=False, indent=2)}\n\n"
        f"# 用户反馈\n{user_feedback.strip()}"
    )


COMPOSE_TOOL_NAME = "emit_graph"


def compose_tool() -> list[dict[str, Any]]:
    """Forced-call schema for prompt-mode graph composition: a bare node list."""
    schema = PlaybookSpec.model_json_schema(by_alias=True)
    node_schema = schema.get("$defs", {}).get("NodeSpec", {})
    return [
        {
            "type": "function",
            "function": {
                "name": COMPOSE_TOOL_NAME,
                "description": "Submit the composed graph.",
                "parameters": {
                    "type": "object",
                    "properties": {"nodes": {"type": "array", "items": node_schema, "minItems": 1}},
                    "required": ["nodes"],
                },
            },
        }
    ]


def build_compose_prompt(prompts_filled: str, agent_roster: dict[str, str], param_names: list[str]) -> str:
    """The one-shot graph-composition request for a prompt-mode playbook."""
    roster = "\n".join(f"- {name}: {desc}" for name, desc in agent_roster.items())
    return (
        "按下面的组图指导拼一张任务图，通过 emit_graph 提交 nodes 列表（camelCase 字段：\n"
        "id / agent / promptTemplate / dependsOn / skills / mcps / instance / confirm）。\n"
        "规则：agent 只能从可用清单选；{{ 上游id.output }} 引用的上游必须列进该节点 dependsOn；\n"
        "无依赖关系的节点会自动并行；图组装后固定执行，不要设计任何运行期分支或循环。\n"
        f"参数已替换完毕（原参数名：{', '.join(param_names) if param_names else '无'}），"
        "promptTemplate 里不要再出现 ${params.*}。\n\n"
        f"# 可用 agent\n{roster}\n\n"
        f"# 组图指导\n{prompts_filled}"
    )
