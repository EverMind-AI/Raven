<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/ff05474a-03f5-4ec2-b42b-55f1508ede06?raw=true)

<p align="center">
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge&logo=huggingface&logoColor=white" alt="Hugging Face"></a>
  <a href="https://discord.gg/gYep5nQRZJ"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FgYep5nQRZJ%3Fwith_counts%3Dtrue&query=%24.approximate_presence_count&suffix=%20online&label=Discord&color=404EED&labelColor=gray&style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/EverMind-AI/EverOS/discussions/67"><img src="https://img.shields.io/badge/WeCom-EverMind_Community-07C160?labelColor=gray&style=for-the-badge&logo=wechat&logoColor=white" alt="WeCom"></a>
</p>

[Website](https://raven.evermind.ai) · [Documentation](https://evermind-ai.github.io/Raven/) · [中文](README.zh-CN.md)

</div>

<br>

# What is Raven

<p align="center">
  <a href="https://github.com/user-attachments/assets/4ae8ac91-2f0c-4c81-aa7c-c15526bd77af"><img src="https://github.com/user-attachments/assets/4ae8ac91-2f0c-4c81-aa7c-c15526bd77af" alt="Raven one surface, all agents workflow" width="100%"></a>
</p>

<p align="center"><em>One Surface, All Agents: Raven generates DAGs and orchestrates multiple specialized agents for complex tasks.</em></p>

Raven is **The Harness of Harnesses**—a self-evolving multi-agent orchestration ecosystem. As a **Host Agent**, it brings specialized agents together through one unified surface to delegate tasks, coordinate execution, and integrate results. Its long-term vision is to extend this orchestration across devices, environments, and domains.

Built on EverMind’s self-evolving harness engine and powered by [EverOS](https://github.com/EverMind-AI/EverOS), Raven preserves context across sessions and continuously improves agent harnesses and collaborative workflows.

**Built-in Agents: Raven-Research**, **Raven-Code**, **Raven-Design**, and **Raven-Oncall** support research, coding, visual design, and unattended workflow automation.

> Raven is pre-alpha. Interfaces and configuration may change quickly.

<p align="center">
  <a href="https://github.com/user-attachments/assets/e333694a-0f4c-4f27-8bfe-8120ff5339a0"><img src="https://github.com/user-attachments/assets/e333694a-0f4c-4f27-8bfe-8120ff5339a0" alt="Multi-Agent Orchestration Benchmark: Node F1, Edge F1, Partial Order Accuracy, and Exact Match Rate" width="100%"></a>
</p>

<p align="center"><em>Raven's Performance on the Multi-Agent Orchestration Benchmark</em></p>

## ❯❯ Built-in Agents

Raven's modular architecture is designed for harness self-evolution and subagent creation. Its four built-in agents deliver **state-of-the-art (SOTA) performance in their respective domains**, combining reusable harness components with domain-specific tools, skills, and agent loops. Raven can delegate a focused task to a single agent or orchestrate multiple agents within a shared workflow. The harness they share is refined by the **Raven Evolver**, a separate tool that consumes Raven as a library and evaluates candidate harness changes against benchmarks; it develops the agents rather than running inside them.

> All four agents are built in and ready for orchestration out of the box.

### ❯ Raven-Research

**Raven-Research** enables **autonomous deep research** for complex questions, literature reviews, and technical analysis. It delivers clear, structured reports with traceable sources, helping users understand unfamiliar domains, compare alternatives, and make informed decisions.

<p align="center">
  <a href="https://github.com/user-attachments/assets/d072a514-58fd-41c7-8fcd-40782ebb4c80"><img src="https://github.com/user-attachments/assets/d072a514-58fd-41c7-8fcd-40782ebb4c80" alt="DeepResearch Mixed: Accuracy, Input Tokens, Output Tokens, and Cost" width="100%"></a>
</p>

<p align="center"><em>Raven-Research's performance on the DeepResearch Mixed benchmark</em></p>

### ❯ Raven-Code

**Raven-Code** enables **agentic software development**, turning requirements into working, tested code. It supports feature implementation, debugging, refactoring, data processing, and data analysis, helping users build new capabilities, resolve issues, and improve code quality while following their project's conventions.

<p align="center">
  <a href="https://github.com/user-attachments/assets/a516ebc2-f0b4-47d4-b970-7dfd492adc0e"><img src="https://github.com/user-attachments/assets/a516ebc2-f0b4-47d4-b970-7dfd492adc0e" alt="Coding Benchmarks: SWE-bench Pro, SWE-bench Verified, WorkBuddy-Code Reward, and SWE-Refactor" width="95%"></a>
</p>

<p align="center"><em>Raven-Code's performance on coding benchmarks</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/75f60aa2-3e2f-42fb-aa51-aa111bf078d0"><img src="https://github.com/user-attachments/assets/75f60aa2-3e2f-42fb-aa51-aa111bf078d0" alt="DataAgentBench (2026-08-24 Live): Raven-Code with Opus-5 achieves 0.8762 Pass@1" width="95%"></a>
</p>

<p align="center"><em>Raven-Code tops on DataAgentBench for data analysis (2026-08-24 Live)</em></p>

### ❯ Raven-Design

**Raven-Design** performs **visual design**, turning ideas and content into polished visual deliverables. It creates PowerPoint slide decks, brand assets, charts, diagrams, and web interfaces, refining layout, typography, and visual consistency to help users communicate clearly and bring their ideas to life.

<p align="center">
  <a href="https://github.com/user-attachments/assets/c42ff722-8aab-40ec-82e3-0655a859a9d9"><img src="https://github.com/user-attachments/assets/c42ff722-8aab-40ec-82e3-0655a859a9d9" alt="PresentBench: Raven-Design, Claude Code, and public leaderboard scores" width="95%"></a>
</p>

<p align="center"><em>Raven-Design tops on PresentBench for slide generation</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/061b5818-595b-4392-b9b6-53a1effb8347"><img src="https://github.com/user-attachments/assets/061b5818-595b-4392-b9b6-53a1effb8347" alt="Visual Design: Raven-Design, Claude Code, and Hermes on ArtifactsBench Dashboard, ArtifactsBench SVG, and GDPVal" width="95%"></a>
</p>

<p align="center"><em>Raven-Design's performance on visual design benchmarks</em></p>

### ❯ Raven-Oncall

**Raven-Oncall** enables **unattended workflow automation** for experimentation, optimization, and continuous monitoring. It autonomously manages workflows from start to completion, sustaining progress over hours or overnight, delivering results, and involving users only when human judgment is needed.

<p align="center">
  <a href="https://github.com/user-attachments/assets/bffd0fa2-e750-4f09-b74a-09452765a99d"><img src="https://github.com/user-attachments/assets/bffd0fa2-e750-4f09-b74a-09452765a99d" alt="AI4AI (Nanochat 50M Pretraining): Bits Per Byte (BPB), Runtime, Tokens, and Cost" width="95%"></a>
</p>

<p align="center"><em>Raven-Oncall significantly outperforms Claude Code on both quality and cost for AI4AI tasks</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/aafe30ca-6693-4d30-956e-73debf3ef2c9"><img src="https://github.com/user-attachments/assets/aafe30ca-6693-4d30-956e-73debf3ef2c9" alt="AI4S Internal Benchmark: Success Rate, Average Total Runtime, Average Total Tokens, and Average Cost" width="95%"></a>
</p>

<p align="center"><em>Raven-Oncall significantly outperforms Claude Code on both success rate and cost for AI4S tasks</em></p>

## ❯❯ Showcase

Real runs, each captured from Raven's task graph. The graph shows the
orchestration Raven chose for the task; below it is what the run produced.

<table>
<tr>
<td width="50%" valign="top">
<p align="center"><b>Song-dynasty domestic aesthetics</b></p>
<a href="https://github.com/user-attachments/assets/8fa50c96-3d2d-4107-8a8c-7d041d1ac8b4"><img src="https://github.com/user-attachments/assets/8fa50c96-3d2d-4107-8a8c-7d041d1ac8b4" alt="Task graph: three Raven-Research nodes running in parallel into a synthesis node, then one Raven-Design node" width="100%"></a>
<a href="https://github.com/user-attachments/assets/379328ea-e3ee-430f-865d-68ed90dae69f"><img src="https://github.com/user-attachments/assets/379328ea-e3ee-430f-865d-68ed90dae69f" alt="Cover, slides and closing slide of the Song-dynasty aesthetics deck" width="100%"></a>
</td>
<td width="50%" valign="top">
<p align="center"><b>How ancient Greece was whitewashed</b></p>
<a href="https://github.com/user-attachments/assets/c8e9ac77-0377-487e-9d07-fb4fe51a0257"><img src="https://github.com/user-attachments/assets/c8e9ac77-0377-487e-9d07-fb4fe51a0257" alt="Task graph: three Raven-Research nodes running in parallel into a synthesis node, then one Raven-Design node" width="100%"></a>
<a href="https://github.com/user-attachments/assets/3c5a2a9a-d6d7-40ad-b3dc-70e9f5c095df"><img src="https://github.com/user-attachments/assets/3c5a2a9a-d6d7-40ad-b3dc-70e9f5c095df" alt="Cover, slides and closing slide of the Greek polychromy deck" width="100%"></a>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<p align="center"><b>How pop music is manufactured</b></p>
<a href="https://github.com/user-attachments/assets/032fdf6e-8b65-49a0-92ba-2ea3066a7451"><img src="https://github.com/user-attachments/assets/032fdf6e-8b65-49a0-92ba-2ea3066a7451" alt="Task graph: three Raven-Research nodes running in parallel into a synthesis node, then one Raven-Design node" width="100%"></a>
<a href="https://github.com/user-attachments/assets/9ab75945-81ba-4437-aec0-9ef6a633ed55"><img src="https://github.com/user-attachments/assets/9ab75945-81ba-4437-aec0-9ef6a633ed55" alt="Cover, slides and closing slide of the pop music deck" width="100%"></a>
</td>
<td width="50%" valign="top">
<p align="center"><b>A century of abstract art</b></p>
<a href="https://github.com/user-attachments/assets/4b606bae-667e-4f2f-ae7d-2880b47ddbf6"><img src="https://github.com/user-attachments/assets/4b606bae-667e-4f2f-ae7d-2880b47ddbf6" alt="Task graph: three Raven-Research nodes running in parallel into a synthesis node, then one Raven-Design node" width="100%"></a>
<a href="https://github.com/user-attachments/assets/b3dfce6c-366d-4e31-9a57-7c98df054618"><img src="https://github.com/user-attachments/assets/b3dfce6c-366d-4e31-9a57-7c98df054618" alt="Cover, slides and closing slide of the abstract art deck" width="100%"></a>
</td>
</tr>
</table>

Runs that end in something other than a deck.

<table>
<tr>
<td width="50%" valign="top">
<p align="center"><b>Six orchestration frameworks, compared</b></p>
<a href="https://github.com/user-attachments/assets/92ff2c83-3b0f-49eb-9995-c1ad4c298452"><img src="https://github.com/user-attachments/assets/92ff2c83-3b0f-49eb-9995-c1ad4c298452" alt="Task graph: two Raven-Research nodes running in parallel into one Raven-Design node" width="100%"></a>
<a href="https://github.com/user-attachments/assets/75beba36-a112-46f2-949d-015856dae588"><img src="https://github.com/user-attachments/assets/75beba36-a112-46f2-949d-015856dae588" alt="Comparison board: six orchestration frameworks against four dimensions, colour-coded for explicit-graph versus code-or-model orchestration" width="100%"></a>
</td>
<td width="50%" valign="top">
<p align="center"><b>A parameter sweep, written, run and plotted</b></p>
<a href="https://github.com/user-attachments/assets/e842677a-8b71-4f1b-ae02-0162a440a1e8"><img src="https://github.com/user-attachments/assets/e842677a-8b71-4f1b-ae02-0162a440a1e8" alt="Task graph: Raven-Code into Raven-Oncall into Raven-Design, run in sequence" width="100%"></a>
<a href="https://github.com/user-attachments/assets/2b03cc53-e707-4728-8d68-327fcf7d112a"><img src="https://github.com/user-attachments/assets/2b03cc53-e707-4728-8d68-327fcf7d112a" alt="Retrieval sweep: recall@k is set by top_k alone, and latency stays flat across the grid" width="100%"></a>
</td>
</tr>
</table>

## ❯❯ Connect Third-Party Agents

Raven can connect to and orchestrate agents via ACP, CLI, or OpenAI-compatible APIs, with presets for 13 **third-party agents** to simplify setup, task delegation, and coordination across shared workflows.

<p align="center">
  <img src="https://github.com/user-attachments/assets/3370c883-00ac-4471-97ef-f4312df77202" width="80%" alt="Third-party agents: Claude Code, Codex, OpenCode, Hermes Agent, OpenClaw, MiroThinker, GitHub Copilot, Qwen Code, CodeBuddy, Qoder, Grok Build, Kimi Code, and Pi">
</p>

## ❯❯ Quick Start

### 📦 Install

Linux, macOS, or WSL2:

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

Native Windows PowerShell:

```powershell
irm https://raven.evermind.ai/install.ps1 | iex
```

Windows PowerShell 5.1 may reject the redirect. Use the direct installer URL instead:

```powershell
irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex
```

Or install from a source checkout, to develop against the code or to run what
has not been released yet:

```bash
git clone https://github.com/EverMind-AI/Raven.git
cd Raven
./install.sh
```

Run as a file, `install.sh` installs that checkout in editable mode: raven and
its bundled plugins link back to your tree, and the TUI bundle and the served
page are built from it. A piped run installs the published wheel even from
inside a clone, so that a one-line install never picks up whatever a working
tree happens to contain. Set `RAVEN_LOCAL_SRC=<dir>` to force the editable
install through a pipe.

The agent products ship with raven itself: a wheel carries the `agents/`
product tree and copies it out to your raven home on first use, and a source
checkout reads the tree in place. Setup asks about each product and registers
the ones you take up, on the model it is tuned for or on this raven's LLM.
See [`agents/README.md`](agents/README.md).

Everything past the first run lives on the documentation site: self-hosting,
Docker deployment, the WebUI, the command reference, the runtime architecture
and the repository layout, in English and Chinese.

**[Read the documentation](https://evermind-ai.github.io/Raven/)**

## ❯❯ Core Systems

| System | What it adds |
| --- | --- |
| **Agent Orchestration** | Coordinates agents, manages task dependencies and parallel execution, and turns multi-step collaboration into reusable workflows. |
| **Evolver** | Drives harness self-evolution by diagnosing failures, testing candidate improvements, and retaining changes that outperform the baseline in reproducible evaluations. |
| **EverOS Memory** | Preserves user context, agent experience, and world knowledge across sessions, recalling relevant memories and reusable skills for future tasks. |
| **SkillForge** | Retrieves relevant skills from local libraries, EverOS memory, and [SkillHub's catalog of **114,190 skills**](https://github.com/EverMind-AI/SkillCorpus#public-artifacts), giving agents specialized expertise on demand. |
| **Proactivity** | Combines event monitoring and scheduled execution to anticipate user needs, deliver timely reminders, and initiate follow-up work. |

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>


## ❯❯ Launch WebUI

Raven's WebUI brings conversations, multi-agent collaboration, and workspace management into your browser. Chat with agents, follow task progress, inspect files and outputs, and browse memory and skills in one place.

```bash
raven web
```

The command opens the WebUI in your browser and keeps Raven running in the background. Use `raven web --stop` to stop the background service.

> **Screenshot placeholder 1:** Conversations and workspace.

> **Screenshot placeholder 2:** Agent collaboration and task graph.

> **Screenshot placeholder 3:** Memory and skill management.

## ❯❯ EverMind Ecosystem

<p align="center">
  <a href="https://github.com/user-attachments/assets/bb13d4d5-e254-4ba0-9ed0-b2c0ccd4bc6a"><img src="https://github.com/user-attachments/assets/bb13d4d5-e254-4ba0-9ed0-b2c0ccd4bc6a" alt="The EverMind ecosystem: the EverMind mark and its slogan on an orbital field" width="100%"></a>
</p>

[EverMind](https://evermind.ai/) connects memory research, production-ready products, and practical
integrations into one open-source ecosystem.

<table>
<tr>
<th colspan="2">Products</th>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverOS">EverOS</a></strong></td>
<td>A local-first, Markdown-native long-term memory runtime for agents and users.</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/Raven">Raven</a></strong></td>
<td>A memory-first, self-improving agent harness with proactivity, context control, and skill evolution.</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverMe">EverMe (CLI)</a></strong></td>
<td>A CLI and agent plugin suite for cross-device, cross-agent personal memory.</td>
</tr>
<tr>
<th colspan="2">Research &amp; Evaluation</th>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/SkillCorpus">SkillCorpus</a></strong></td>
<td>Curated, retrieval-ready agent skill corpora with retrieval and evaluation tooling.</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverAlgo">EverAlgo</a></strong></td>
<td>Stateless extraction, ranking, parsing, and memory operators that power EverOS.</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/HyperMem">HyperMem</a></strong></td>
<td>Hypergraph-based hierarchical memory for coarse-to-fine long-term conversation retrieval.</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/MSA">MSA</a></strong></td>
<td>Memory Sparse Attention for scalable latent memory and 100M-token contexts.</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EverMemBench">EverMemBench</a></strong></td>
<td>Evaluation of factual recall, applied reasoning, and personalized generalization in memory systems.</td>
</tr>
<tr>
<td><strong><a href="https://github.com/EverMind-AI/EvoAgentBench">EvoAgentBench</a></strong></td>
<td>Longitudinal evaluation of agent self-evolution, transfer efficiency, error avoidance, and skill use.</td>
</tr>
<tr>
<th colspan="2"><a href="https://github.com/EverMind-AI/plugins">Integrations</a></th>
</tr>
<tr>
<td><strong><a href="https://docs.openclaw.ai">OpenClaw</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/openclaw">OpenClaw plugin</a> for automatic recall, capture, and session-memory lifecycle management.</td>
</tr>
<tr>
<td><strong><a href="https://github.com/NousResearch/hermes-agent">Hermes Agent</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/hermes">Hermes plugin</a> for persistent memory across Hermes sessions.</td>
</tr>
<tr>
<td><strong><a href="https://github.com/deepseek-ai/DeepSeek-Harness">DeepSeek Harness</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/dsh">DSH plugin</a> for memory-aware DeepSeek Harness agents.</td>
</tr>
<tr>
<td><strong><a href="https://dify.ai">Dify</a></strong></td>
<td><a href="https://github.com/EverMind-AI/plugins/tree/main/dify">Self-hosted</a> and <a href="https://github.com/EverMind-AI/plugins/tree/main/dify_cloud">cloud</a> tools for explicit memory search and storage in workflows and agents.</td>
</tr>
</table>

Together, these projects form EverMind's research-to-runtime stack: methods
and benchmarks become reusable memory infrastructure, products, and agent
integrations.

<br>
<div align="right">

[![](https://img.shields.io/badge/-Back_to_top-gray?style=flat-square)](#readme-top)

</div>

## ❯❯ Contributing

Issues and pull requests are welcome. Start with the [developer workflow](docs/dev.md), follow [AGENTS.md](AGENTS.md) for repository rules, and use [GitHub Discussions](https://github.com/EverMind-AI/Raven/discussions) for design conversations.

## ❯❯ License

[Apache License 2.0](LICENSE)
