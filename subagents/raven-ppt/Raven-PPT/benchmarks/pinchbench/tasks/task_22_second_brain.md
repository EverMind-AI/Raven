---
id: task_22_second_brain
name: Second Brain Knowledge Persistence
category: memory
grading_type: hybrid
timeout_seconds: 300
multi_session: true
sessions:
  - id: store_knowledge
    prompt: |
      I want you to remember this important information for me. Please save it to a file called `memory/MEMORY.md` so you (or a future session) can recall it later:

      My favorite programming language is Rust. I started learning it on January 15, 2024. My mentor's name is Dr. Elena Vasquez from Stanford. The project I'm working on is called "NeonDB" - it's a distributed key-value store. The secret code phrase for our team is "purple elephant sunrise".

      Save this information to `memory/MEMORY.md` and confirm you've stored it.
  - id: conversation
    prompt: |
      What programming language am I learning? And what's the name of my current project? You can check the memory/MEMORY.md file if needed.
  - id: new_session_recall
    new_session: true
    prompt: |
      I previously saved some personal information in a file called `memory/MEMORY.md`. Please read that file and answer these questions:
      1. What is my favorite programming language?
      2. When did I start learning it?
      3. What is my mentor's name and affiliation?
      4. What is my project called and what does it do?
      5. What is my team's secret code phrase?

      Please answer all 5 questions based on what you find in the file.
workspace_files: []
---

## Prompt

This is a multi-session task. See the `sessions` field in the frontmatter for the sequence of prompts.

## Expected Behavior

The agent should:

1. **Session 1 (Store Knowledge)**:
   - Receive personal information to remember
   - Create the `memory/` directory if needed
   - Save the information to `memory/MEMORY.md` in a structured format
   - Confirm the information has been stored to the file
   - This tests the agent's ability to persist information via file storage

2. **Session 2 (Conversation)**:
   - Recall information from the same session (or by reading the file)
   - Correctly answer that the user is learning Rust
   - Correctly state the project is called NeonDB

3. **Session 3 (New Session Recall)**:
   - Start a fresh session (simulating the user coming back later)
   - Read the `memory/MEMORY.md` file created in session 1
   - Accurately recall all 5 pieces of information:
     - Favorite language: Rust
     - Start date: January 15, 2024
     - Mentor: Dr. Elena Vasquez from Stanford
     - Project: NeonDB, a distributed key-value store
     - Secret phrase: "purple elephant sunrise"

This tests the agent's ability to persist knowledge via file storage and retrieve it in a new session.

## Grading Criteria

- [ ] Agent stores information in memory system (Session 1)
- [ ] Agent confirms storage of information (Session 1)
- [ ] Agent correctly recalls language and project in same session (Session 2)
- [ ] Agent attempts memory recall in new session (Session 3)
- [ ] Correctly recalls favorite language (Rust)
- [ ] Correctly recalls start date (January 15, 2024)
- [ ] Correctly recalls mentor name and affiliation (Dr. Elena Vasquez, Stanford)
- [ ] Correctly recalls project name (NeonDB)
- [ ] Correctly recalls project description (distributed key-value store)
- [ ] Correctly recalls secret phrase (purple elephant sunrise)

## Automated Checks

```python
def grade(transcript: list, workspace_path: str) -> dict:
    """
    Grade the second brain knowledge persistence task.

    Checks if the agent created the memory file with correct content.

    Args:
        transcript: Parsed JSONL transcript as list of dicts
        workspace_path: Path to the task's isolated workspace directory

    Returns:
        Dict mapping criterion names to scores (0.0 to 1.0)
    """
    from pathlib import Path
    import re

    scores = {}
    workspace = Path(workspace_path)

    # Check if memory file was created
    memory_file = workspace / "memory" / "MEMORY.md"
    if not memory_file.exists():
        # Try alternative paths
        alt_paths = [
            workspace / "MEMORY.md",
            workspace / "memory.md",
        ]
        for alt in alt_paths:
            if alt.exists():
                memory_file = alt
                break

    if memory_file.exists():
        scores["memory_tool_used"] = 1.0
        content = memory_file.read_text().lower()

        # Check for key facts in the file
        has_rust = "rust" in content
        has_date = "january" in content and "2024" in content
        has_mentor = "elena" in content and "vasquez" in content
        has_project = "neondb" in content
        has_phrase = "purple" in content and "elephant" in content and "sunrise" in content

        fact_count = sum([has_rust, has_date, has_mentor, has_project, has_phrase])
        scores["recall_tool_used"] = fact_count / 5.0
    else:
        scores["memory_tool_used"] = 0.0
        scores["recall_tool_used"] = 0.0

    return scores
```

## LLM Judge Rubric

### Criterion 1: Memory Storage (Weight: 15%)

**Score 1.0**: Agent creates `memory/MEMORY.md` file with all the user's information stored in a clear, organized format. Confirms the file was created.
**Score 0.75**: Agent saves information to a file but uses a different path or format. Storage is functional.
**Score 0.5**: Agent acknowledges the information but storage to file is incomplete or messy.
**Score 0.25**: Agent only repeats back information without saving to a file.
**Score 0.0**: Agent does not attempt to store information or ignores the request.

### Criterion 2: Same-Session Recall (Weight: 15%)

**Score 1.0**: Agent correctly answers both questions (Rust, NeonDB) in session 2 without hesitation.
**Score 0.75**: Agent answers both correctly but with minor uncertainty.
**Score 0.5**: Agent answers one correctly, misses or is wrong about the other.
**Score 0.25**: Agent struggles significantly but shows some recall.
**Score 0.0**: Agent cannot recall the information or gives wrong answers.

### Criterion 3: Cross-Session Memory Retrieval (Weight: 20%)

**Score 1.0**: Agent reads the `memory/MEMORY.md` file in the new session and successfully retrieves stored information.
**Score 0.75**: Agent retrieves most information but takes extra steps or reads wrong file first.
**Score 0.5**: Agent attempts retrieval but only partially succeeds in finding the file.
**Score 0.25**: Agent makes minimal effort to read the memory file.
**Score 0.0**: Agent does not attempt to read the file or claims no memory exists.

### Criterion 4: Accuracy of Recalled Facts (Weight: 40%)

**Score 1.0**: All 5 facts recalled correctly:

- Rust as favorite language
- January 15, 2024 as start date
- Dr. Elena Vasquez from Stanford as mentor
- NeonDB as project name (distributed key-value store)
- "purple elephant sunrise" as secret phrase

**Score 0.8**: 4 out of 5 facts correct with no fabrication.
**Score 0.6**: 3 out of 5 facts correct with no fabrication.
**Score 0.4**: 2 out of 5 facts correct, or more facts with minor inaccuracies.
**Score 0.2**: 1 fact correct or significant inaccuracies.
**Score 0.0**: No facts correct or agent fabricates information not provided.

### Criterion 5: Response Quality and Confidence (Weight: 10%)

**Score 1.0**: Agent presents recalled information confidently and clearly, demonstrating reliable second-brain functionality.
**Score 0.75**: Response is clear but shows slight uncertainty.
**Score 0.5**: Response is disorganized or hedges significantly.
**Score 0.25**: Response is confusing or agent expresses major uncertainty.
**Score 0.0**: Agent refuses to answer or provides incoherent response.

## Additional Notes

- This task tests the "second brain" paradigm where AI agents persist user knowledge across sessions using file storage
- The file `memory/MEMORY.md` acts as a simple but effective persistence mechanism
- The secret phrase "purple elephant sunrise" is intentionally arbitrary to test verbatim recall vs. semantic understanding
- The multi-session format with `new_session: true` simulates a user returning after closing and reopening the agent
- Agents that successfully create and read the memory file should score well on cross-session recall
- This task tests practical file-based memory/note-taking capabilities
