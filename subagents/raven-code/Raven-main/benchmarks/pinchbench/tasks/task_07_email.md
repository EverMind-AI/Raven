---
id: task_07_email
name: Professional Email Drafting
category: writing
grading_type: llm_judge
timeout_seconds: 180
workspace_files: []
---

## Prompt

Write a professional email declining a meeting request due to schedule conflicts. Save it to email_draft.txt.

## Expected Behavior

The agent should:

1. Create a professional email with appropriate structure
2. Include key elements: greeting, explanation, decline, alternative/apology, closing
3. Maintain a polite and professional tone
4. Be concise but complete
5. Save to a file named `email_draft.txt`

The email should balance being direct about declining while maintaining positive professional relationships.

## Grading Criteria

- [ ] File `email_draft.txt` created
- [ ] Email has proper structure (greeting, body, closing)
- [ ] Clearly declines the meeting request
- [ ] Provides reason (schedule conflict)
- [ ] Maintains professional and polite tone
- [ ] Offers alternative or expresses willingness to reschedule
- [ ] Includes appropriate closing
- [ ] Length is appropriate (not too brief, not too verbose)

## LLM Judge Rubric

### Criterion 1: Professional Tone and Courtesy (Weight: 30%)

**Score 1.0**: Tone is consistently professional, polite, and courteous. Expresses appropriate regret about declining. Maintains positive relationship focus. No overly casual or stiff language.

**Score 0.75**: Generally professional and polite with minor tone issues. Slightly too casual or formal but still appropriate.

**Score 0.5**: Adequate professionalism but with notable tone issues. May be too abrupt, overly apologetic, or inconsistent.

**Score 0.25**: Poor tone. Too casual, rude, or inappropriately formal. Lacks courtesy.

**Score 0.0**: Completely unprofessional or missing.

### Criterion 2: Completeness and Clarity (Weight: 25%)

**Score 1.0**: Email clearly declines the meeting, provides reason (schedule conflict), and offers alternative (reschedule, different time, or other solution). Message is unambiguous.

**Score 0.75**: Email declines clearly with reason provided. May lack strong alternative offer but intent is clear.

**Score 0.5**: Email declines but lacks clarity in reason or alternative. Message may be somewhat ambiguous.

**Score 0.25**: Email is unclear about declining or missing key elements (reason or any alternative).

**Score 0.0**: Email fails to decline meeting or is completely unclear.

### Criterion 3: Structure and Format (Weight: 20%)

**Score 1.0**: Perfect email structure with greeting (Dear/Hi [Name]), clear body paragraphs, and professional closing (Best regards, Sincerely, etc.) with signature line. Well-organized.

**Score 0.75**: Good structure with all key elements present. Minor formatting issues.

**Score 0.5**: Basic structure but missing elements (e.g., no greeting or closing) or poorly organized.

**Score 0.25**: Poor structure. Multiple missing elements or very disorganized.

**Score 0.0**: No discernible email structure or missing.

### Criterion 4: Conciseness and Appropriateness (Weight: 15%)

**Score 1.0**: Email is appropriately concise (3-6 sentences or 50-150 words). Conveys all necessary information without unnecessary detail or excessive brevity.

**Score 0.75**: Good length (2-3 or 6-8 sentences). Minor verbosity or slight brevity but still effective.

**Score 0.5**: Too brief (1-2 sentences) or too long (over 200 words). Still functional but not optimal.

**Score 0.25**: Extremely brief (single sentence) or excessively long (over 300 words). Poor balance.

**Score 0.0**: Length is completely inappropriate or content missing.

### Criterion 5: Task Completion (Weight: 10%)

**Score 1.0**: File created with correct name (email_draft.txt), contains complete email, all requirements met.

**Score 0.75**: File created with minor issues but essentially complete.

**Score 0.5**: File created but missing significant requirements.

**Score 0.25**: File created but content severely lacking.

**Score 0.0**: No file created or file is empty.
