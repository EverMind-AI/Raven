---
id: task_14_humanizer
name: Humanize AI-Generated Blog
category: content_transformation
grading_type: llm_judge
timeout_seconds: 120
workspace_files:
  - source: ai_blog.txt
    dest: ai_blog.txt
---

# Task: Humanize AI-Generated Blog

## Prompt

I have a blog post in `ai_blog.txt` that sounds way too robotic and AI-generated. First, install the "humanizer" skill from the skill registry using `/install humanizer`, then use it to make the text sound more natural and human-written. If the skill isn't available, you can manually rewrite it to sound more human. Save the humanized version to `humanized_blog.txt`.

---

## Expected Behavior

The agent should:

1. Read the provided AI-generated blog from `ai_blog.txt`
2. Use a humanizer skill/tool to transform the content
3. Save the humanized output to `humanized_blog.txt`

The humanizer should address common AI writing patterns such as:

- Overuse of transitional phrases ("Furthermore," "Moreover," "In conclusion")
- Excessive hedging language ("It's worth noting that," "It's important to understand")
- Repetitive sentence structures
- Lack of contractions
- Overly formal tone
- Generic filler phrases ("In today's fast-paced world")
- Bullet-point style paragraphs that feel like lists

---

## Grading Criteria

- [ ] Agent reads the input blog file
- [ ] Agent uses a humanizer skill/tool
- [ ] Output file is created with humanized content
- [ ] Content maintains the original meaning and key points
- [ ] Writing sounds more natural and human-like
- [ ] AI-typical phrases are reduced or eliminated

---

## LLM Judge Rubric

### Criterion 1: Skill Usage or Manual Rewrite (Weight: 25%)

**Score 1.0**: Agent correctly installed and used a humanizer skill, OR performed a quality manual rewrite.
**Score 0.75**: Agent attempted to install/use a skill and fell back to manual rewrite appropriately.
**Score 0.5**: Agent attempted the task but with suboptimal approach.
**Score 0.25**: Agent made minimal effort to transform the content.
**Score 0.0**: Agent did not attempt to humanize the content at all.

### Criterion 2: Output Quality - Natural Voice (Weight: 35%)

**Score 1.0**: Output reads naturally like human-written content. Uses contractions, varied sentence structures, conversational tone, and avoids robotic patterns.
**Score 0.75**: Output is mostly natural with minor robotic elements remaining.
**Score 0.5**: Output shows improvement but still has noticeable AI patterns or awkward phrasing.
**Score 0.25**: Output has minimal improvement over the original AI text.
**Score 0.0**: Output is unchanged, worse, or missing.

### Criterion 3: Content Preservation (Weight: 25%)

**Score 1.0**: All key information, advice, and meaning from the original is preserved accurately.
**Score 0.75**: Most content preserved with minor omissions or alterations.
**Score 0.5**: Significant content preserved but some key points lost or distorted.
**Score 0.25**: Major content loss or distortion of meaning.
**Score 0.0**: Content completely changed or missing.

### Criterion 4: Task Completion (Weight: 15%)

**Score 1.0**: Both files handled correctly - input read, output saved to correct location.
**Score 0.75**: Task completed with minor file handling issues.
**Score 0.5**: Partial completion - file created but in wrong location or format.
**Score 0.25**: Attempted but output not properly saved.
**Score 0.0**: Task not completed - no output file created.

---

## Additional Notes

The input blog intentionally contains common AI writing patterns including:

- Opening with "In today's [something] world"
- Excessive use of "Furthermore," "Moreover," "Additionally"
- Phrases like "It is important to note" and "It is worth mentioning"
- Lack of contractions ("do not" instead of "don't")
- Overly formal and generic tone
- Numbered/bulleted advice that sounds like a list
- Conclusion starting with "In conclusion"
- Ending with a call to action about "starting your journey"

A good humanizer should reduce or transform these patterns while keeping the core productivity advice intact.
