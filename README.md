<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/ae944083-9c61-4218-8372-2c3c0d483d4b?raw=true)

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
  <a href="https://github.com/user-attachments/assets/f400d693-d8cf-46eb-b846-6948f66f6fed"><img src="https://github.com/user-attachments/assets/f400d693-d8cf-46eb-b846-6948f66f6fed" alt="Raven one surface, all agents workflow" width="100%"></a>
</p>

<p align="center"><em>One Surface, All Agents: Raven generates DAGs and orchestrates multiple specialized agents for complex tasks.</em></p>

Raven is **The Harness of Harnesses**—a self-evolving multi-agent orchestration ecosystem. As a **Host Agent**, it brings specialized agents together through one unified surface to delegate tasks, coordinate execution, and integrate results. Its long-term vision is to extend this orchestration across devices, environments, and domains.

Built on EverMind’s self-evolving harness engine and powered by [EverOS](https://github.com/EverMind-AI/EverOS), Raven preserves context across sessions and continuously improves agent harnesses and collaborative workflows.

**Built-in Agents: Raven-Research**, **Raven-Code**, **Raven-Design**, and **Raven-Oncall** support research, coding, visual design, and unattended workflow automation.

> Raven is pre-alpha. Interfaces and configuration may change quickly.

<p align="center">
  <a href="https://github.com/user-attachments/assets/1756ab94-706d-4c07-b8b3-3ef6dd8a147f"><img src="https://github.com/user-attachments/assets/1756ab94-706d-4c07-b8b3-3ef6dd8a147f" alt="Multi-Agent Orchestration Benchmark: Node F1, Edge F1, Partial Order Accuracy, and Exact Match Rate" width="100%"></a>
</p>

<p align="center"><em>Raven's Performance on the Multi-Agent Orchestration Benchmark</em></p>

## ❯❯ Built-in Agents

Raven's modular architecture is designed for harness self-evolution and subagent creation. Its four built-in agents deliver **state-of-the-art (SOTA) performance in their respective domains**, combining reusable harness components with domain-specific tools, skills, and agent loops. Raven can delegate a focused task to a single agent or orchestrate multiple agents within a shared workflow. The harness they share is refined by the **Raven Evolver**, a separate tool that consumes Raven as a library and evaluates candidate harness changes against benchmarks; it develops the agents rather than running inside them.

> All four agents are built in and ready for orchestration out of the box.

### ❯ Raven-Research

**Raven-Research** enables **autonomous deep research** for complex questions, literature reviews, and technical analysis. It delivers clear, structured reports with traceable sources, helping users understand unfamiliar domains, compare alternatives, and make informed decisions.

<p align="center">
  <a href="https://github.com/user-attachments/assets/9dbc1aaa-3477-4871-b3fd-a42405c6ef02"><img src="https://github.com/user-attachments/assets/9dbc1aaa-3477-4871-b3fd-a42405c6ef02" alt="DeepResearch Mixed: Accuracy, Input Tokens, Output Tokens, and Cost" width="100%"></a>
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
  <a href="https://github.com/user-attachments/assets/103c49f8-f31d-4735-8bfd-84cae9baee46"><img src="https://github.com/user-attachments/assets/103c49f8-f31d-4735-8bfd-84cae9baee46" alt="Visual Design: Raven-Design, Claude Code, and Hermes on ArtifactsBench Dashboard, ArtifactsBench SVG, and GDPVal" width="95%"></a>
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

## ❯❯ Runtime Self-Evolution (Experimental)

**Raven is built for this from the ground up.** Its agent loop is split into four decoupled strategy modules — **Memory**, **Planning**, **Capability**, and **Action** — so what an agent is told, what it treats as finished, which tools it reaches for, and which checks it must pass can all be rewritten **while it is running**. What changes belongs to that agent alone: a setting, or a small piece of judgement code written for it. The next turn of the same conversation already runs on it, with nothing restarted.

**A Persona is the first thing this builds.** Describe the assistant you want and Raven assembles one: a lead role that talks to you, and specialists drawn from the agents you already have. What you asked for does not stop at a prompt — it lands in that assistant's **harness**, as its division of work, the tools it may reach for, and the checks it must pass before it acts. Raven keeps reworking the assistant from your feedback until it does the job.

> Describe what you need once. Raven assembles the assistant, keeps improving it while you work, and afterwards a sentence is enough to put it to work again.

## ❯❯ Showcase

### ❯ THRESHOLD: from a spec to a playable FPS

**Humans wrote the spec. Raven handled the rest.** Working autonomously for about 4 days, Raven completed 42 rounds of planning, development, and verification to build a playable first-person shooter in Godot 4, centered on an arena boss fight. It also created the game's poster, presentation, and website.

<table>
<tr>
<td colspan="2" valign="top"><p align="center"><b>Gameplay video</b></p></td>
<td width="22.5%" valign="top"><p align="center"><b><a href="https://threshold-7bl.pages.dev/en/">Website ↗</a></b></p></td>
</tr>
<tr>
<td colspan="2" valign="top">

https://github.com/user-attachments/assets/44724e3e-6564-467a-b422-473aa8f474bd

</td>
<td rowspan="3" width="22.5%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/17f1de3b-abce-48df-8d04-78b9bfcea157"><img src="https://github.com/user-attachments/assets/17f1de3b-abce-48df-8d04-78b9bfcea157" alt="The THRESHOLD website as one long capture: hero, fight, kill cam, attack tells, evolution, making-of and footer" width="100%"></a></p></td>
</tr>
<tr>
<td width="46%" valign="top"><p align="center"><b>Poster</b></p></td>
<td width="31.5%" valign="top"><p align="center"><b>Presentation</b></p></td>
</tr>
<tr>
<td width="46%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/a12e76d1-e7b1-4a7e-a3ba-1f3b5492fb50"><img src="https://github.com/user-attachments/assets/a12e76d1-e7b1-4a7e-a3ba-1f3b5492fb50" alt="THRESHOLD poster: the Warden towers over the player on a molten arena floor" width="100%"></a></p></td>
<td width="31.5%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/3852c187-3412-4501-9b62-809a3dfd0b81"><img src="https://github.com/user-attachments/assets/3852c187-3412-4501-9b62-809a3dfd0b81" alt="Cover, slides and closing slide of the THRESHOLD deck" width="100%"></a></p></td>
</tr>
</table>

### ❯ Raven RSI: recursive self-improvement in action

**AI that improves AI.** Given a task and evaluation criteria it cannot modify, Raven RSI independently plans each round, writes code, runs experiments, and evaluates the results. In nanochat pre-training experiments, it completed 172 training runs across 7 rounds without a single crash, reducing `val_bpb` by 5.8% within the same 20-minute, single-GPU budget. The same process reduced overshoot in a dam-break simulation by three orders of magnitude and completed an FEA limit-load search in 8 rounds of bisection.

<table>
<tr>
<td width="35.8%" valign="top"><p align="center"><b>CFD dam-break simulation</b></p></td>
<td colspan="2" width="35.8%" valign="top"><p align="center"><b>FEA limit-load search</b></p></td>
<td width="28.4%" valign="top"><p align="center"><b><a href="https://livxue.github.io/raven-rsi/en/">Website ↗</a></b></p></td>
</tr>
<tr>
<td width="35.8%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/44f81db4-4c17-47be-ad3b-047e26762a24"><img src="https://github.com/user-attachments/assets/44f81db4-4c17-47be-ad3b-047e26762a24" alt="Dam-break solve: a collapsing water column resolved to fine free-surface structure, with the out-of-bounds water fraction falling from 1e0 to 1.36e-10 over seven rounds" width="100%"></a></p></td>
<td colspan="2" width="35.8%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/89878a01-ab5c-4070-8359-5bc34c3d58b7"><img src="https://github.com/user-attachments/assets/89878a01-ab5c-4070-8359-5bc34c3d58b7" alt="Limit-load search: a cantilever beam under rising load colored by von Mises stress, with the bisection bracket narrowing from 1800-2000 kN down to 3.125 kN over eight rounds" width="100%"></a></p></td>
<td rowspan="3" width="28.4%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/cddb15fa-2968-47b8-906a-c335c928e0eb"><img src="https://github.com/user-attachments/assets/cddb15fa-2968-47b8-906a-c335c928e0eb" alt="The Raven RSI website as one long capture: headline numbers, nanochat, dam-break CFD, FEA limit load and the low-cost-model comparison" width="100%"></a></p></td>
</tr>
<tr>
<td colspan="2" width="41.1%" valign="top"><p align="center"><b>Poster</b></p></td>
<td width="30.5%" valign="top"><p align="center"><b>Presentation</b></p></td>
</tr>
<tr>
<td colspan="2" width="41.1%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/c11b3422-3778-40c4-808b-506f950bb1b8"><img src="https://github.com/user-attachments/assets/c11b3422-3778-40c4-808b-506f950bb1b8" alt="Raven RSI poster: a spiral stone stair climbing into the light, with ravens circling it" width="100%"></a></p></td>
<td width="30.5%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/f2c368e3-789e-4a8d-890f-384240b36c25"><img src="https://github.com/user-attachments/assets/f2c368e3-789e-4a8d-890f-384240b36c25" alt="Cover, slides and closing slide of the Raven RSI deck" width="100%"></a></p></td>
</tr>
</table>

### ❯ Raven: a complete product launch kit

**One raven, a whole flock of specialists.** Raven's launch kit brings together a browser-based physics mini-game, a 16-slide product overview, a poster, and the README you are reading now.

<table>
<tr>
<td colspan="2" valign="top"><p align="center"><b>Physics mini-game</b> · <b><a href="https://livxue.github.io/angry-raven/">Play online ↗</a></b></p></td>
<td width="19%" valign="top"><p align="center"><b>README</b></p></td>
</tr>
<tr>
<td colspan="2" valign="top">

https://github.com/user-attachments/assets/dd186b6d-3752-4c59-89c7-eeea5c6fa962

</td>
<td rowspan="3" width="19%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/1bb3bef1-e701-4609-9b14-6de6877c9e5c"><img src="https://github.com/user-attachments/assets/1bb3bef1-e701-4609-9b14-6de6877c9e5c" alt="The top of the Raven README as one long capture: banner, introduction, the four built-in agents with their benchmarks, and runtime self-evolution" width="100%"></a></p></td>
</tr>
<tr>
<td width="46.5%" valign="top"><p align="center"><b>Poster</b></p></td>
<td width="34.5%" valign="top"><p align="center"><b>Presentation</b></p></td>
</tr>
<tr>
<td width="46.5%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/4044db80-10f6-4963-9800-218125630c2b"><img src="https://github.com/user-attachments/assets/4044db80-10f6-4963-9800-218125630c2b" alt="Raven poster: a raven on a standing stone above sea cliffs at sunset, over the line One raven. A whole flock of specialists." width="100%"></a></p></td>
<td width="34.5%" valign="top"><p align="center"><a href="https://github.com/user-attachments/assets/2e19674e-83a8-4a77-a114-39f906c240d0"><img src="https://github.com/user-attachments/assets/2e19674e-83a8-4a77-a114-39f906c240d0" alt="Cover, slides and closing slide of the Raven overview deck" width="100%"></a></p></td>
</tr>
</table>

**[More showcases](docs/showcase.md)**

## ❯❯ Connect Third-Party Agents

Raven can connect to and orchestrate agents via ACP, CLI, or OpenAI-compatible APIs, with presets for 13 **third-party agents** to simplify setup, task delegation, and coordination across shared workflows. Try these agents in Raven through a unified interface!

<p align="center">
  <img src="https://github.com/user-attachments/assets/3370c883-00ac-4471-97ef-f4312df77202" width="80%" alt="Third-party agents: Claude Code, Codex, OpenCode, Hermes Agent, OpenClaw, MiroThinker, GitHub Copilot, Qwen Code, CodeBuddy, Qoder, Grok Build, Kimi Code, and Pi">
</p>

## ❯❯ Quick Start

### 🤖 Install with Your Agent

Let your own agent install Raven for you. Copy this prompt into any agent that
can read a web page and run shell commands, such as Claude Code or Codex:

```text
Read https://evermind-ai.github.io/Raven/quick-start/ and follow it to install Raven, or to update it if it is already installed.
```

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

Or run it in a container, with nothing on the host but Git and Docker:

```bash
git clone https://github.com/EverMind-AI/Raven.git
cd Raven
docker compose -f docker/docker-compose.yml up
```

Then open `http://localhost:18793` in your browser. Prerequisites are
[Git](https://git-scm.com/) and [Docker](https://www.docker.com/) with
[Docker Compose](https://docs.docker.com/compose/). See
[`docker/README.md`](docker/README.md) for what comes up, how the page signs
itself in, and how to run it behind a remotely exposed port.

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
checkout reads the tree in place. Setup asks, for each product, whether it
runs on the model it is tuned for, which needs a key of its own, or on this
raven's LLM. Nothing is registered: a folder is on the roster because it is
there. See [`agents/README.md`](agents/README.md).

Learn more about Raven on the documentation site.

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

<p align="center">
  <a href="https://github.com/user-attachments/assets/572c6cfb-6659-44f9-98fd-5670e61623b8"><img src="https://github.com/user-attachments/assets/572c6cfb-6659-44f9-98fd-5670e61623b8" alt="Raven WebUI new task page" width="90%"></a>
</p>

<p align="center"><em>New task: one composer, with skills, playbooks, knowledge and memory a click away.</em></p>

<p align="center">
  <a href="https://github.com/user-attachments/assets/a7be5dca-b65a-4dea-9d88-2a4cb55f74b3"><img src="https://github.com/user-attachments/assets/a7be5dca-b65a-4dea-9d88-2a4cb55f74b3" alt="Raven WebUI subagents page" width="90%"></a>
</p>

<p align="center"><em>Subagents: every connected agent in one roster, built-in and third-party alike.</em></p>

## ❯❯ EverMind Ecosystem

<p align="center">
  <a href="https://github.com/user-attachments/assets/65a5e3f1-dccb-496f-94f5-9782070ec8b4"><img src="https://github.com/user-attachments/assets/65a5e3f1-dccb-496f-94f5-9782070ec8b4" alt="The EverMind ecosystem: the EverMind mark and its slogan on an orbital field" width="100%"></a>
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
