---
id: task_06_events
name: Tech Conference Research
category: research
grading_type: llm_judge
timeout_seconds: 300
workspace_files: []
---

## Prompt

Find 5 upcoming tech conferences and create events.md with name, date, location, and website for each.

## Expected Behavior

The agent should:

1. Use web search or research tools to find legitimate tech conferences
2. Identify 5 conferences happening this year
3. Extract key information: name, date, location, website
4. Create a file named `events.md` with this information
5. Format the information clearly and consistently
6. Ensure the conferences are real and information is accurate

The agent may use various search strategies and should verify information from reliable sources.

## Grading Criteria

- [ ] File `events.md` created
- [ ] Contains exactly 5 conference entries
- [ ] Each entry has conference name
- [ ] Each entry has date information
- [ ] Each entry has location information
- [ ] Each entry has website/URL
- [ ] Information appears accurate and verifiable
- [ ] Formatting is clear and consistent

## LLM Judge Rubric

### Criterion 1: Information Accuracy (Weight: 35%)

**Score 1.0**: All 5 conferences are real, verifiable tech conferences. All information (names, dates, locations, websites) is accurate and can be verified. No fabricated events.

**Score 0.75**: 4-5 conferences are verifiable with accurate information. Minor inaccuracies in dates or details that don't affect overall validity.

**Score 0.5**: 3-4 conferences are verifiable. Some information may be outdated or contain inaccuracies, but events are real.

**Score 0.25**: Only 1-2 conferences are verifiable, or multiple entries contain significant inaccuracies. May include fabricated events.

**Score 0.0**: All or most conferences appear fabricated, or information is completely inaccurate.

### Criterion 2: Completeness (Weight: 25%)

**Score 1.0**: All 5 conferences have complete information: name, specific dates (not just month), location (city/country), and working website URL.

**Score 0.75**: All 5 conferences present with mostly complete information. Minor gaps (e.g., approximate dates, missing specific venue).

**Score 0.5**: 4-5 conferences present but with notable information gaps. Some entries missing dates, locations, or websites.

**Score 0.25**: Fewer than 4 conferences, or multiple entries have significant missing information.

**Score 0.0**: Fewer than 3 conferences, or most entries are severely incomplete.

### Criterion 3: Relevance and Quality (Weight: 20%)

**Score 1.0**: All conferences are significant, well-known tech conferences (e.g., major industry events, established conference series). Good variety in topics or regions.

**Score 0.75**: Most conferences are notable tech events. Good selection with minor issues in significance or variety.

**Score 0.5**: Mix of notable and lesser-known conferences. Limited variety or some questionable relevance to "tech."

**Score 0.25**: Mostly obscure or questionable conferences. Poor relevance to tech industry.

**Score 0.0**: Conferences are not tech-related or completely inappropriate.

### Criterion 4: Formatting and Presentation (Weight: 15%)

**Score 1.0**: Excellent formatting with consistent structure. Uses markdown effectively (headings, lists, links). Easy to read and well-organized.

**Score 0.75**: Good formatting with minor inconsistencies. Generally clear and readable.

**Score 0.5**: Basic formatting but with notable inconsistencies or organizational issues. Harder to read.

**Score 0.25**: Poor formatting. Inconsistent structure makes information difficult to parse.

**Score 0.0**: No formatting or completely unreadable.

### Criterion 5: Task Completion (Weight: 5%)

**Score 1.0**: File created with correct name (events.md), contains 5 conferences, all requirements met.

**Score 0.75**: File created with minor issues but essentially complete.

**Score 0.5**: File created but missing significant requirements.

**Score 0.25**: File created but severely lacking.

**Score 0.0**: No file created or file is empty.
