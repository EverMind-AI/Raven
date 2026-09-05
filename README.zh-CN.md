<div align="center" id="readme-top">

![Raven banner](https://github.com/user-attachments/assets/6c6f585a-21b6-4e7b-9187-acffe59d0c10)

<p align="center">
  <a href="https://x.com/evermind"><img src="https://img.shields.io/badge/EverMind-000000?labelColor=gray&style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://huggingface.co/EverMind-AI"><img src="https://img.shields.io/badge/HuggingFace-EverMind-F5C842?labelColor=gray&style=for-the-badge&logo=huggingface&logoColor=white" alt="Hugging Face"></a>
  <a href="https://discord.gg/gYep5nQRZJ"><img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FgYep5nQRZJ%3Fwith_counts%3Dtrue&query=%24.approximate_presence_count&suffix=%20online&label=Discord&color=404EED&labelColor=gray&style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/EverMind-AI/EverOS/discussions/67"><img src="https://img.shields.io/badge/WeCom-EverMind_Community-07C160?labelColor=gray&style=for-the-badge&logo=wechat&logoColor=white" alt="WeCom"></a>
</p>

[官网](https://raven.evermind.ai) · [English](README.md)

</div>

<br>

# Raven

Raven 是构建在 [EverOS](https://github.com/EverMind-AI/EverOS) 之上的
**The Self-Improving Agent Harness**，并内置可选 Deep Research，用于多来源深度研究。

Raven 会持续迭代支撑 Agent 的 harness：tools、skills、memory、code execution
runtime、policies 和工作环境。EverOS 为这个 harness 提供跨会话持久存在的用户
记忆、Agent 记忆和世界知识，让每一次运行都能改进 Agent 的行动方式、知识状态，
并把可重复工作流沉淀成可复用 Agent Templates 和 digital workers。

**Update：** Raven 新增 Deep Research。运行 `raven deep-research enable` 后，
Agent 可以在需要深度调查的任务中使用 MiroThinker-backed、多来源 research tool。

> Raven 目前处于 pre-alpha 阶段，接口和配置可能快速变化。

## 快速开始

### 安装

Linux、macOS 或 WSL2：

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

原生 Windows PowerShell：

```powershell
irm https://raven.evermind.ai/install.ps1 | iex
```

Windows PowerShell 5.1 可能拒绝重定向，请改用直连安装地址：

```powershell
irm https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.ps1 | iex
```

### 完成引导并运行

```bash
raven
```

首次运行只需这一条：尚未配置时，`raven` 会先带你走完引导，然后在同一次会话里直接进入 TUI。之后想重新配置，再显式运行 `raven onboard`。

双语 onboarding 向导会配置六个方面，无需手动编辑 `~/.raven/config.json`：

1. LLM provider 和模型
2. Sandbox 或执行位置
3. 聊天渠道
4. EverOS 长期记忆
5. Deep Research
6. 从其他 AI 工具进行冷启动导入

Provider 配置包含向导内连通性检查。可选步骤可以跳过，之后再配置。如果设置尚未完成，请运行：

```bash
raven doctor
```

### 升级

```bash
raven upgrade --check
raven upgrade
```

升级会保留配置、sessions 和 memory。Raven 不会自动更新。

## 文档

本页只保留简介与快速开始。规范性内容——仓库布局、命令参考、贡献规则——以英文
[README.md](README.md) 与 [AGENTS.md](AGENTS.md) 为准，不再在此维护中文副本，
以免两份文档漂移。完整文档见 `docs/` 目录，领域术语从 `CONTEXT-MAP.md` 入口查阅。

## 参与贡献

欢迎提交 issues 和 pull requests。请先阅读[开发工作流](docs/dev.md)，按照 [AGENTS.md](AGENTS.md) 中的仓库规则进行协作，并在 [GitHub Discussions](https://github.com/EverMind-AI/Raven/discussions) 讨论设计方案。

## 许可证

[Apache License 2.0](LICENSE)
