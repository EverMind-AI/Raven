---
id: task_21_openclaw_comprehension
name: OpenClaw Report Comprehension
category: comprehension
grading_type: automated
timeout_seconds: 300
workspace_files:
  - source: "OpenClaw Agent Use Cases and Gap Analysis for PinchBench.pdf"
    dest: "openclaw_report.pdf"
---

## Prompt

I have a research report about OpenClaw agent use cases in my workspace as `openclaw_report.pdf`. I need you to extract several pieces of information from it and write them to `answer.txt`. Please answer the following questions, one answer per line:

1. How many community-built skills were in the public registry before filtering?
2. How many skills remained after filtering out spam, duplicates, non-English, crypto/finance/trading, and malicious content?
3. What is the largest skill category by count, and how many skills does it have? (format: "Category Name: count")
4. What is the second-largest skill category by count, and how many skills does it have? (format: "Category Name: count")
5. What is the name of the file that defines an OpenClaw skill?
6. What type of API does the OpenClaw gateway expose?
7. What date was the skills registry data collected?
8. How many new benchmark tasks does the paper propose? (just the number)

## Expected Behavior

The agent should:

1. Read and parse the PDF file `openclaw_report.pdf`
2. Find the skills ecosystem statistics section identifying 5,705 total and 2,999 filtered skills
3. Locate the skill category table identifying "AI & LLMs" (287) as largest and "Search & Research" (253) as second
4. Find that skills are defined by `SKILL.md` files
5. Identify the gateway uses a "typed WebSocket API"
6. Find the data collection date of February 7, 2026
7. Count 6 proposed benchmark tasks
8. Write all answers to `answer.txt`, one per line

## Grading Criteria

- [ ] Agent reads the PDF file
- [ ] Output file `answer.txt` is created
- [ ] Total skills count (5705) is correct
- [ ] Filtered skills count (2999) is correct
- [ ] Top category (AI & LLMs: 287) is correct
- [ ] Second category (Search & Research: 253) is correct
- [ ] Skill filename (SKILL.md) is identified
- [ ] API type (typed WebSocket) is identified
- [ ] Data collection date (February 7, 2026) is correct
- [ ] Proposed task count (6) is correct

## Automated Checks

```python
def grade(transcript: list, workspace_path: str) -> dict:
    from pathlib import Path
    import re

    scores = {}
    workspace = Path(workspace_path)
    answer_file = workspace / "answer.txt"

    if not answer_file.exists():
        return {
            "file_created": 0.0,
            "total_skills_correct": 0.0,
            "filtered_skills_correct": 0.0,
            "top_category_correct": 0.0,
            "second_category_correct": 0.0,
            "skill_filename_correct": 0.0,
            "api_type_correct": 0.0,
            "date_correct": 0.0,
            "proposed_tasks_correct": 0.0,
        }

    scores["file_created"] = 1.0
    content = answer_file.read_text().strip()
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    full_text = content.lower()

    # Helper to extract numbers from text
    def extract_number(text):
        numbers = re.findall(r'[\d,]+', text)
        for n in numbers:
            val = int(n.replace(',', ''))
            if val > 0:
                return val
        return None

    # Line 1: Total skills (5705)
    line1 = lines[0] if len(lines) >= 1 else ""
    total = extract_number(line1)
    scores["total_skills_correct"] = 1.0 if total == 5705 else 0.0

    # Line 2: Filtered skills (2999)
    line2 = lines[1] if len(lines) >= 2 else ""
    filtered = extract_number(line2)
    scores["filtered_skills_correct"] = 1.0 if filtered == 2999 else 0.0

    # Line 3: Top category (AI & LLMs: 287)
    line3 = lines[2].lower() if len(lines) >= 3 else ""
    has_ai_llm = "ai" in line3 and "llm" in line3
    has_287 = extract_number(line3) == 287
    scores["top_category_correct"] = 1.0 if (has_ai_llm and has_287) else (0.5 if has_ai_llm or has_287 else 0.0)

    # Line 4: Second category (Search & Research: 253)
    line4 = lines[3].lower() if len(lines) >= 4 else ""
    has_search_research = "search" in line4 and "research" in line4
    has_253 = extract_number(line4) == 253
    scores["second_category_correct"] = 1.0 if (has_search_research and has_253) else (0.5 if has_search_research or has_253 else 0.0)

    # Line 5: Skill filename (SKILL.md)
    line5 = lines[4] if len(lines) >= 5 else ""
    scores["skill_filename_correct"] = 1.0 if "skill.md" in line5.lower() else 0.0

    # Line 6: API type (typed WebSocket)
    line6 = lines[5].lower() if len(lines) >= 6 else ""
    has_websocket = "websocket" in line6 or "web socket" in line6
    has_typed = "typed" in line6
    scores["api_type_correct"] = 1.0 if (has_websocket and has_typed) else (0.5 if has_websocket else 0.0)

    # Line 7: Date (February 7, 2026)
    line7 = lines[6].lower() if len(lines) >= 7 else ""
    date_patterns = [
        r"february\s+7[,\s]+2026",
        r"feb\.?\s+7[,\s]+2026",
        r"2026[-/]02[-/]07",
        r"02[-/]07[-/]2026",
        r"7\s+february[,\s]+2026",
        r"7\s+feb\.?[,\s]+2026",
    ]
    scores["date_correct"] = 1.0 if any(re.search(pattern, line7) for pattern in date_patterns) else 0.0

    # Line 8: Proposed tasks count (6)
    line8 = lines[7] if len(lines) >= 8 else ""
    task_count = extract_number(line8)
    scores["proposed_tasks_correct"] = 1.0 if task_count == 6 else 0.0

    return scores
```
