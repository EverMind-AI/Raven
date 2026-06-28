---
id: task_05_summary
name: Document Summarization
category: comprehension
grading_type: llm_judge
timeout_seconds: 240
workspace_files:
  - path: "summary_source.txt"
    content: |
      The Rise of Artificial Intelligence in Modern Healthcare

      Artificial intelligence (AI) has emerged as a transformative force in healthcare, revolutionizing how medical professionals diagnose diseases, develop treatment plans, and manage patient care. Over the past decade, machine learning algorithms have demonstrated remarkable capabilities in analyzing medical imaging, predicting patient outcomes, and identifying patterns that might escape human observation.

      One of the most significant applications of AI in healthcare is in medical imaging analysis. Deep learning models can now detect cancerous tumors, identify fractures, and diagnose conditions like diabetic retinopathy with accuracy rates that match or exceed those of experienced radiologists. These systems process thousands of images during training, learning to recognize subtle patterns and anomalies that indicate disease. For instance, Google's DeepMind has developed AI systems that can detect over 50 eye diseases from retinal scans with 94% accuracy.

      Beyond imaging, AI is making strides in drug discovery and development. Traditional drug development is a lengthy and expensive process, often taking over a decade and costing billions of dollars. AI algorithms can analyze vast databases of molecular structures, predict how different compounds will interact with disease targets, and identify promising drug candidates much faster than traditional methods. During the COVID-19 pandemic, AI played a crucial role in accelerating vaccine development and identifying potential treatments.

      Predictive analytics represents another frontier where AI is proving invaluable. By analyzing electronic health records, genetic information, and lifestyle factors, AI systems can predict which patients are at high risk for conditions like heart disease, diabetes, or hospital readmission. This enables healthcare providers to intervene earlier with preventive care, potentially saving lives and reducing healthcare costs. Some hospitals have implemented AI systems that predict patient deterioration hours before it becomes clinically apparent, allowing medical teams to take proactive measures.

      However, the integration of AI in healthcare is not without challenges. Privacy concerns are paramount, as these systems require access to sensitive patient data. There are also questions about algorithmic bias, as AI systems trained on non-diverse datasets may perform poorly for underrepresented populations. Additionally, the "black box" nature of some AI algorithms makes it difficult for doctors to understand how the system reached a particular conclusion, which can be problematic in medical decision-making where transparency is crucial.

      Regulatory frameworks are still evolving to keep pace with AI innovation. The FDA and other regulatory bodies worldwide are developing new guidelines for approving AI-based medical devices and ensuring they meet safety and efficacy standards. There's also the question of liability: when an AI system makes an error, who is responsible—the developer, the healthcare provider, or the institution?

      Despite these challenges, the future of AI in healthcare looks promising. Researchers are working on explainable AI systems that can provide clear reasoning for their recommendations. Federated learning approaches allow AI models to be trained on distributed datasets without compromising patient privacy. As these technologies mature and regulatory frameworks solidify, AI is expected to become an increasingly integral part of healthcare delivery.

      The key to successful AI integration lies in viewing these systems as tools to augment, rather than replace, human medical expertise. The most effective healthcare delivery will likely combine the pattern recognition and data processing capabilities of AI with the empathy, ethical judgment, and contextual understanding that human healthcare providers bring to patient care. This collaborative approach promises to improve outcomes, reduce costs, and make quality healthcare more accessible to populations around the world.
---

## Prompt

Read the document in summary_source.txt and write a concise 3-paragraph summary to summary_output.txt.

## Expected Behavior

The agent should:

1. Read the content from `summary_source.txt` (provided in workspace)
2. Comprehend the main points and key themes
3. Write a concise 3-paragraph summary that captures:
   - Main topic and overview (paragraph 1)
   - Key applications and benefits (paragraph 2)
   - Challenges and future outlook (paragraph 3)
4. Save the summary to `summary_output.txt`
5. Maintain accuracy while being concise

The summary should be significantly shorter than the original while preserving the essential information.

## Grading Criteria

- [ ] File `summary_output.txt` created
- [ ] Summary has exactly 3 paragraphs
- [ ] Summary accurately captures main topic (AI in healthcare)
- [ ] Summary mentions key applications (imaging, drug discovery, predictive analytics)
- [ ] Summary addresses challenges (privacy, bias, regulation)
- [ ] Summary is concise (significantly shorter than original)
- [ ] Writing is clear and coherent
- [ ] No significant factual errors or misrepresentations

## LLM Judge Rubric

### Criterion 1: Accuracy and Completeness (Weight: 35%)

**Score 1.0**: Summary accurately captures all major themes: AI applications in healthcare (imaging, drug discovery, predictive analytics), benefits, challenges (privacy, bias, regulation), and future outlook. No factual errors or misrepresentations.

**Score 0.75**: Summary captures most major themes with minor omissions. One key area may be underrepresented. No significant factual errors.

**Score 0.5**: Summary captures some major themes but misses important aspects. May have minor factual inaccuracies or overemphasis on less important points.

**Score 0.25**: Summary misses multiple major themes or contains significant factual errors. Poor representation of the source material.

**Score 0.0**: Summary is completely inaccurate, off-topic, or missing.

### Criterion 2: Conciseness (Weight: 25%)

**Score 1.0**: Summary is appropriately concise (150-250 words), capturing essential information without unnecessary detail. Excellent information density.

**Score 0.75**: Summary is reasonably concise (250-350 words) with good information density. Minor verbosity.

**Score 0.5**: Summary is somewhat verbose (350-450 words) or too brief (under 100 words), missing the balance between conciseness and completeness.

**Score 0.25**: Summary is excessively long (over 450 words) or far too brief (under 75 words) to be useful.

**Score 0.0**: Summary length is completely inappropriate or content is missing.

### Criterion 3: Structure and Coherence (Weight: 20%)

**Score 1.0**: Exactly 3 well-organized paragraphs with clear logical flow. First paragraph introduces topic, second covers applications/benefits, third addresses challenges/future. Excellent transitions and coherence.

**Score 0.75**: 3 paragraphs with good organization. Minor issues with flow or paragraph focus. Generally coherent.

**Score 0.5**: 3 paragraphs but with organizational issues, or wrong number of paragraphs (2 or 4) but otherwise well-structured. Some coherence problems.

**Score 0.25**: Poor structure with significant coherence issues. May have wrong number of paragraphs and disorganized content.

**Score 0.0**: No discernible structure or content is missing.

### Criterion 4: Writing Quality (Weight: 15%)

**Score 1.0**: Excellent writing with clear, professional prose. No grammar or spelling errors. Appropriate vocabulary and tone.

**Score 0.75**: Good writing quality with minor issues. Few grammar errors. Generally clear and professional.

**Score 0.5**: Adequate writing but with notable grammar issues, awkward phrasing, or clarity problems.

**Score 0.25**: Poor writing quality with multiple errors or unclear expression.

**Score 0.0**: Writing is incomprehensible or missing.

### Criterion 5: Task Completion (Weight: 5%)

**Score 1.0**: File created with correct name (summary_output.txt), agent read the source file, all requirements met.

**Score 0.75**: File created with minor issues (e.g., slight naming variation) but task essentially complete.

**Score 0.5**: File created but with significant issues or missing requirements.

**Score 0.25**: File created but content is severely lacking or incorrect.

**Score 0.0**: No file created or file is empty.
