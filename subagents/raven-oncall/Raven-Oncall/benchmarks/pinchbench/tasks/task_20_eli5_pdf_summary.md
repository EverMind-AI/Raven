---
id: task_20_eli5_pdf_summary
name: ELI5 PDF Summarization
category: comprehension
grading_type: llm_judge
timeout_seconds: 300
workspace_files:
  - source: GPT4.pdf
    dest: GPT4.pdf
---

## Prompt

Read the file `GPT4.pdf` in my workspace. It's a technical paper about GPT-4. Write an "Explain Like I'm 5" (ELI5) summary of the paper and save it to `eli5_summary.txt`.

The summary should:
- Be understandable by a young child with no technical background
- Use simple words, short sentences, and everyday analogies
- Cover the main ideas of the paper (what GPT-4 is, what it can do, and why it matters)
- Be roughly 200-400 words

## Expected Behavior

The agent should:

1. Read and parse the PDF file `GPT4.pdf` (the GPT-4 Technical Report by OpenAI)
2. Identify the key themes and findings of the paper, including:
   - GPT-4 is a large multimodal model that accepts text and image inputs and produces text outputs
   - It demonstrates human-level performance on various professional and academic benchmarks
   - It was trained using a large-scale, predictable training process
   - Safety and alignment work was done (RLHF, red-teaming, etc.)
   - Limitations still exist (hallucinations, reasoning errors, etc.)
3. Translate these technical concepts into simple, child-friendly language using analogies and everyday comparisons
4. Write the ELI5 summary to `eli5_summary.txt`

The summary should avoid jargon like "multimodal", "transformer", "RLHF", "benchmark", "parameters", etc. Instead, it should use analogies a child could understand (e.g., "like a really smart helper that can read and look at pictures").

## Grading Criteria

- [ ] Agent successfully reads/parses the PDF file
- [ ] Output file `eli5_summary.txt` is created
- [ ] Summary explains what GPT-4 is in simple terms
- [ ] Summary covers what GPT-4 can do (understanding text, images, answering questions, exams)
- [ ] Summary mentions that people worked to make it safer/nicer
- [ ] Summary acknowledges limitations (it can still make mistakes)
- [ ] Language is genuinely simple — no technical jargon
- [ ] Uses analogies or comparisons a child could understand
- [ ] Summary length is appropriate (roughly 200-400 words)
- [ ] Writing is clear, coherent, and engaging

## LLM Judge Rubric

### Criterion 1: Simplicity and Accessibility (Weight: 35%)

**Score 1.0**: The summary is genuinely understandable by a young child. Sentences are short and simple. No technical jargon whatsoever. Uses concrete, everyday language and relatable analogies (e.g., comparing the model to a helpful friend, a very good student, or a toy that learns). A 5-year-old could follow along.
**Score 0.75**: The summary is mostly simple with one or two slightly advanced words or concepts that a child might not fully grasp, but overall accessible. Good use of analogies.
**Score 0.5**: The summary attempts simplicity but includes several technical terms or abstract concepts that would confuse a child. Analogies are weak or missing.
**Score 0.25**: The summary reads more like a simplified adult summary than an ELI5. Contains significant jargon or assumes background knowledge.
**Score 0.0**: The summary is fully technical, uses jargon throughout, or makes no effort to simplify for a young audience.

### Criterion 2: Accuracy and Coverage (Weight: 30%)

**Score 1.0**: The summary accurately captures the core ideas of the GPT-4 paper: what GPT-4 is (a large AI model), what it can do (understand text and images, perform well on exams/benchmarks), that effort was put into safety, and that it still has limitations. No factual errors.
**Score 0.75**: The summary covers most key ideas with minor omissions (e.g., missing the image understanding aspect or safety work). No significant factual errors.
**Score 0.5**: The summary covers some key ideas but misses important aspects of the paper, or includes minor inaccuracies.
**Score 0.25**: The summary only superficially touches the paper's content or contains notable factual errors.
**Score 0.0**: The summary is off-topic, factually wrong, or missing.

### Criterion 3: Engagement and Tone (Weight: 20%)

**Score 1.0**: The summary is warm, friendly, and engaging. It feels like someone is actually talking to a child — using a conversational tone, fun comparisons, and a sense of wonder. The reader would enjoy reading it.
**Score 0.75**: The summary has a generally friendly tone with some engaging elements, but could be more lively or conversational.
**Score 0.5**: The summary is functional but dry. It simplifies the content but reads like a bland synopsis rather than an ELI5 explanation.
**Score 0.25**: The summary is stilted, overly formal, or boring. No effort to make it engaging for a young audience.
**Score 0.0**: The tone is completely inappropriate for a child audience, or content is missing.

### Criterion 4: Task Completion (Weight: 15%)

**Score 1.0**: The agent successfully read the PDF, produced a summary saved to `eli5_summary.txt`, and the summary is within the target length range (200-400 words).
**Score 0.75**: The agent completed the task with minor issues (e.g., file saved with a slightly different name, or word count slightly outside the range but still reasonable).
**Score 0.5**: The agent completed the task but with notable issues (e.g., summary is far too short or too long, or the agent struggled significantly to read the PDF).
**Score 0.25**: The agent partially completed the task — file was created but content is severely lacking, or the PDF was not properly read.
**Score 0.0**: No output file created, or the agent failed to read the PDF entirely.

---

## Additional Notes

This task tests the agent's ability to:

- Parse a PDF document (the GPT-4 Technical Report, which is a substantial academic paper)
- Distill complex technical content into extremely simple language
- Produce creative, child-appropriate analogies for abstract concepts
- Balance accuracy with accessibility

The GPT-4 Technical Report is a real paper published by OpenAI. It covers the model's capabilities, training process, safety mitigations, and limitations. The challenge is not just summarization but radical simplification — the agent must strip away all technical complexity while preserving the core message.
