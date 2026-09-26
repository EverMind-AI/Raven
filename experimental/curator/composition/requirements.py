"""Read node requirements beside native playbooks without duplicating their graph schema."""

import json
from pathlib import PurePosixPath

from pydantic import TypeAdapter

from ...requirements import Requirement


def requirement_node(path):
    parts = PurePosixPath(path).parts
    if len(parts) == 4 and parts[1] == "nodes" and parts[3] == "requirements.json":
        return f"{parts[0]}/{parts[2]}"
    return None


def read_requirements(text):
    requirements = TypeAdapter(list[Requirement]).validate_python(json.loads(text))
    if not requirements:
        raise ValueError("node requirements must be nonempty; explicitly remove the file to withdraw management")
    return [item.model_dump(mode="json") for item in requirements]


def feedback_for(name, feedback):
    """Route located requirements; keep unlocated requirements and original evidence intact."""
    if not isinstance(feedback, dict) or "requirements" not in feedback:
        return feedback
    return {
        **feedback,
        "requirements": [
            item
            for item in feedback["requirements"]
            if not item.get("locations") or f"child/{name}" in item["locations"]
        ],
    }
