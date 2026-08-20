---
id: task_03_blog
name: Blog Post Writing
category: writing
grading_type: llm_judge
timeout_seconds: 300
workspace_files: []
---

## Prompt

Write a 500-word blog post about the benefits of remote work for software developers. Save it to blog_post.md.

## Expected Behavior

The agent should:

1. Create a well-structured blog post with an introduction, body, and conclusion
2. Focus on benefits specific to software developers
3. Aim for approximately 500 words (400-600 acceptable)
4. Use proper markdown formatting
5. Save the content to a file named `blog_post.md`

The post should be engaging, informative, and professionally written. It should cover multiple benefits and provide reasoning or examples.

## Grading Criteria

- [ ] File `blog_post.md` created
- [ ] Content is approximately 500 words (400-600 range)
- [ ] Post has clear structure (intro, body, conclusion)
- [ ] Content focuses on software developer benefits
- [ ] Writing is clear and engaging
- [ ] Uses proper markdown formatting
- [ ] Covers multiple distinct benefits
- [ ] Provides reasoning or examples for claims

## LLM Judge Rubric

### Criterion 1: Content Quality and Relevance (Weight: 30%)

**Score 1.0**: Content is highly relevant to software developers, covers 4+ distinct benefits with clear reasoning, examples, or evidence. Information is accurate and insightful.

**Score 0.75**: Content is relevant with 3-4 benefits covered. Good reasoning provided. Minor gaps in depth or examples.

**Score 0.5**: Content addresses the topic but only covers 2-3 benefits. Some reasoning provided but lacks depth or specificity to software developers.

**Score 0.25**: Content is generic or only covers 1-2 benefits superficially. Limited relevance to software developers specifically.

**Score 0.0**: Content is off-topic, missing, or completely fails to address remote work benefits.

### Criterion 2: Structure and Organization (Weight: 25%)

**Score 1.0**: Excellent structure with clear introduction, well-organized body paragraphs, and strong conclusion. Logical flow between ideas. Proper use of headings and markdown formatting.

**Score 0.75**: Good structure with intro, body, and conclusion. Minor organizational issues. Adequate markdown formatting.

**Score 0.5**: Basic structure present but with notable organizational issues. Weak transitions or unclear flow. Minimal markdown formatting.

**Score 0.25**: Poor structure. Missing key sections (intro or conclusion). Disorganized content.

**Score 0.0**: No discernible structure. Content is chaotic or missing.

### Criterion 3: Writing Quality (Weight: 20%)

**Score 1.0**: Excellent writing with clear, engaging prose. Professional tone. No significant grammar or spelling errors. Varied sentence structure.

**Score 0.75**: Good writing quality with minor issues. Generally clear and professional. Few grammar errors.

**Score 0.5**: Adequate writing but with notable issues in clarity, tone, or grammar. Some awkward phrasing.

**Score 0.25**: Poor writing quality. Multiple grammar errors, unclear phrasing, or unprofessional tone.

**Score 0.0**: Writing is incomprehensible or missing.

### Criterion 4: Word Count Compliance (Weight: 15%)

**Score 1.0**: Word count is 450-550 words (within 10% of target).

**Score 0.75**: Word count is 400-600 words (within 20% of target).

**Score 0.5**: Word count is 300-400 or 600-700 words (20-40% off target).

**Score 0.25**: Word count is 200-300 or 700-800 words (40-60% off target).

**Score 0.0**: Word count is under 200 or over 800 words, or content is missing.

### Criterion 5: Task Completion (Weight: 10%)

**Score 1.0**: File created with correct name (blog_post.md), contains complete blog post, all requirements met.

**Score 0.75**: File created with minor naming issues or one small requirement missed.

**Score 0.5**: File created but multiple requirements missed or significant issues.

**Score 0.25**: File created but content is incomplete or severely lacking.

**Score 0.0**: No file created or file is empty/unusable.
