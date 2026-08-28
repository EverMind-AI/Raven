"""
PinchBench Task Loader — standalone copy for bot_runner.

Reuses PinchBench's task markdown format (YAML frontmatter + sections).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class Task:
    """A single benchmark task."""

    task_id: str
    name: str
    category: str
    grading_type: str  # automated | llm_judge | hybrid
    timeout_seconds: int
    workspace_files: List[Dict[str, str]]
    prompt: str
    expected_behavior: str
    grading_criteria: List[str]
    automated_checks: Optional[str] = None
    llm_judge_rubric: Optional[str] = None
    grading_weights: Optional[Dict[str, float]] = None
    file_path: Optional[Path] = None
    frontmatter: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "category": self.category,
            "grading_type": self.grading_type,
            "timeout_seconds": self.timeout_seconds,
            "prompt": self.prompt,
            "expected_behavior": self.expected_behavior,
            "grading_criteria": self.grading_criteria,
            "frontmatter": self.frontmatter,
        }


def load_all_tasks(tasks_dir: Path) -> List[Task]:
    """Load all task_*.md files from a directory."""
    tasks = []
    for task_file in sorted(tasks_dir.glob("task_*.md")):
        try:
            task = _load_task(task_file)
            tasks.append(task)
        except Exception as e:
            logger.error("Failed to load %s: %s", task_file.name, e)
    logger.info("Loaded %d tasks from %s", len(tasks), tasks_dir)
    return tasks


def _load_task(task_file: Path) -> Task:
    content = task_file.read_text(encoding="utf-8")
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if not fm_match:
        raise ValueError(f"No YAML frontmatter in {task_file}")

    metadata = yaml.safe_load(fm_match.group(1))
    sections = _parse_sections(fm_match.group(2))

    return Task(
        task_id=metadata.get("id", ""),
        name=metadata.get("name", ""),
        category=metadata.get("category", ""),
        grading_type=metadata.get("grading_type", "automated"),
        timeout_seconds=metadata.get("timeout_seconds", 120),
        workspace_files=metadata.get("workspace_files", []) or [],
        prompt=sections.get("Prompt", "").strip(),
        expected_behavior=sections.get("Expected Behavior", "").strip(),
        grading_criteria=_extract_criteria(sections.get("Grading Criteria", "")),
        automated_checks=sections.get("Automated Checks"),
        llm_judge_rubric=sections.get("LLM Judge Rubric"),
        grading_weights=metadata.get("grading_weights"),
        file_path=task_file,
        frontmatter=metadata,
    )


def _parse_sections(body: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current: Optional[str] = None
    lines: List[str] = []

    for line in body.split("\n"):
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            if current:
                sections[current] = "\n".join(lines).strip()
            current = m.group(1)
            lines = []
        elif current is not None:
            lines.append(line)

    if current:
        sections[current] = "\n".join(lines).strip()
    return sections


def _extract_criteria(text: str) -> List[str]:
    criteria = []
    for line in text.split("\n"):
        m = re.match(r"^-\s+\[[ x]\]\s+(.+)$", line.strip())
        if m:
            criteria.append(m.group(1))
    return criteria
