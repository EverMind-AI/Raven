# Raven: The Harness of Harnesses for Composable Agentic Intelligence

EverMind AI, September 2026. The full author list is in the report's appendix.

**[Download the PDF](https://github.com/EverMind-AI/Raven/releases/download/tech-report-v1/technical-report.pdf)** (v1, 82 pages), also on [arXiv](https://arxiv.org/abs/2609.33439) and [Hugging Face](https://huggingface.co/papers/2609.33439). The [release page](https://github.com/EverMind-AI/Raven/releases/tag/tech-report-v1) lists the file's SHA-256.

## Abstract

As large language models advance, AI agents are moving beyond isolated, domain-specific tasks toward long-horizon, cross-domain workflows. This transition exposes two challenges: increasing harness complexity makes manual design difficult to scale, while tighter coupling to specific domains limits the generality of a single harness. The central question thus shifts from how to engineer a stronger harness for one domain to how to autonomously construct specialized harnesses, improve them through experience, and orchestrate them across domains. We introduce Raven, *The Harness of Harnesses*, an open-source multi-agent ecosystem that automatically constructs and evolves modular harnesses for specific models and domains, treating each executable model&ndash;harness pair as a composable unit of intelligence. To support an *All-Domain Collaboration Network*, its Host Agent decomposes goals, matches subtasks to specialized agents, coordinates execution dependencies, and integrates results, while a host archive and EverOS preserve experience across tasks and Skill Forge makes that experience available as reusable procedures. Our theory establishes sufficient conditions for such composition to expand reliable task coverage beyond that of the available individual agents under a shared resource budget. On complex and long-horizon tasks, Raven significantly outperforms the state-of-the-art agent systems, pushing the frontier of composable agentic intelligence.

## Citation

```bibtex
@misc{evermind2026raven,
  title         = {{Raven: The Harness of Harnesses for Composable Agentic Intelligence}},
  author        = {{EverMind AI}},
  year          = {2026},
  eprint        = {2609.33439},
  archivePrefix = {arXiv},
  primaryClass  = {cs.AI},
  url           = {https://arxiv.org/abs/2609.33439}
}
```
