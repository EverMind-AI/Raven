# Ravenx-PPT

把素材和一份 `.pptx` 模板交给它，它交回一份 deck。

[English](README.md)

Ravenx-PPT 是 [Raven](https://github.com/EverMind-AI/Raven) 的一个专做 deck 的形态。
给它一篇论文、一份报告、一组文档或一个网址，再（可选）给一份你自己的 PowerPoint
模板：它读素材、逐页决定这份 deck 要论证什么、写出 deck、看一遍自己做出来的每一页，
把看到的问题改掉，然后把文件交给你。

> 预览阶段，接口与配置可能变动。

## 交付的是什么

一份真正的 `.pptx`。每一页都是能在 PowerPoint 或 Keynote 里直接编辑的形状与文字 ——
没有任何一页是整页截图，也没有任何一张"图表"其实是图表的图片。从源 PDF 里取出的插图
按插图放置，表格按表格重建。

模型写的是一段 python-pptx 程序，而不是往槽位里填内容，所以一页的版式是按这一页要说
的话来定的。让这件事不至于失控的是测量：每次构建之后引擎会渲染每一页，同时测量文件和
渲染结果，把发现的问题交回去 —— 字号低于下限、字压在字上、文字跑出画布、插图被卡片盖
住、某页引用了素材里根本没有的数字。多数问题回灌进下一轮；少数直接拒绝导出。

## 前置条件

| | 用途 |
| --- | --- |
| Python 3.13 | |
| [uv](https://docs.astral.sh/uv/) | 依赖管理（也可以用 pip） |
| **LibreOffice** | 把每一页渲染出来，让模型和门禁能看见。没有它 deck 仍然能构建，但没有任何东西能看它 |
| CJK 字体 | 做中日韩文 deck 必需 —— 缺字体时渲染出来是豆腐块，所有视觉检查都失去意义。Debian/Ubuntu：`apt install fonts-noto-cjk` |
| poppler-utils | 可选。pypdfium2 不可用时用它做页面光栅化与取词位置 |
| 一个大模型 API key | OpenRouter、Anthropic、OpenAI，或任何 Raven 支持的 provider |

## 安装

```bash
git clone git@github.com:Tchen-data/Ravenx-PPT.git
cd Ravenx-PPT
uv sync --extra ppt
```

用 pip：`pip install -e ".[ppt]"`。

确认工具链找到了 LibreOffice：

```bash
uv run raven doctor
```

## 配置

`config.example.json` 是一份可直接用的 OpenRouter 配置。复制它，填上自己的 key：

```bash
mkdir -p ~/.raven
cp config.example.json ~/.raven/config.json
$EDITOR ~/.raven/config.json
```

```json
{
  "agents": {
    "defaults": {
      "provider": "openrouter",
      "model": "anthropic/claude-opus-5",
      "contextWindowTokens": 1000000,
      "maxToolIterations": 500
    }
  },
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-REPLACE_WITH_YOUR_OWN_KEY",
      "apiBase": "https://openrouter.ai/api/v1"
    }
  },
  "tools": { "ppt": { "enabled": true, "designer": { "enabled": false } } }
}
```

这份示例配置固定使用 1,000,000 token 上下文和 500 次工具迭代。PPT 默认关闭独立设计环，
由主 Agent 在三页一组的渲染回灌中逐页修改；需要额外精修时再显式打开
`tools.ppt.designer.enabled`。

deck 这件事需要能力强的模型 —— 它要一边看页面渲染图，一边反复改一段几百行的程序，
是整条链路里最难的活。

不想手改文件的话，`raven onboard` 提供交互式的 provider 配置。

## 运行

```bash
uv run raven tui
```

然后用你想要的语言直接说：

> 把 `papers/tarvis.pdf` 做成一份 12 页的中文 deck，给公司内部技术评审用，
> 模板用 `~/templates/house.pptx`。

素材按路径给它。你指给它的东西 —— 一个 PDF、一个文档目录、一个网址 —— 都会先被读进
这个 deck 的项目里。

只跑一轮、不进交互界面：`uv run raven agent -m "..."`。

## 产物在哪

在 workspace 下（默认 `~/.raven/workspace`）：

```
ppt_projects/<名字>/
  sources/     这份 deck 依据的每一份文档
  ingest/      抽出来的文本、插图与表格
  build/       build.py —— 画出这份 deck 的程序 —— 以及它构建出的 deck
  review/      逐页渲染图
  state/       brief 与 outline
exports/<名字>/deck.pptx    最终交付的文件
```

`build/build.py` 值得一提：它就是这份 deck 的全部代码。改它再构建，改动就生效；在对
话里提要求，被改写的也是它。

## 用自己的模板

把 `.pptx` 的路径给它，deck 就在这份模板的母版、主题、版式与画布里构建。

它的封面、目录、章节页与封底是**克隆**的 —— 这四页是读者认出一套模板的地方，而大多数
模板是用渐变、自定义几何和半透明填充做出来的，没有哪个库能重画。内容页是**自由构图**，
但落在从模板量出来的风格里：它的标题行位置、字号阶梯、配色、安全区。模板自带的示例内容
（配图、占位文案、厂商水印）如果残留在 deck 里，会被报出来。

## 什么会被拒绝导出

大多数测量结果是警告，进入下一轮修改。有七类直接拒绝，因为重新排版解决不了：

| | |
| --- | --- |
| `fact`、`citation` | 素材支持不了的数字或图号 |
| `page_budget`、`language` | 不是当初谈定的那份 deck |
| `band` | 什么都不承载的色块条 —— "一眼看出是生成的"最主要来源 |
| `house_style` | deck 的主题不是模板的主题 |
| `unmapped_page` | outline 里没有计划过的页 |

另外逐页拒绝的还有：文字压在文字上、形状盖住别的形状的内容、文字被自己的框裁掉、
转义序列被当成字符印出来。

## 配置项参考

以下全部可选，表中的值就是不写时代码使用的默认值。

| 配置键 | 默认 | |
| --- | --- | --- |
| `tools.ppt.enabled` | `true` | 设为 false 就退回通用 agent |
| `tools.ppt.profile` | `script_author` | 目前唯一实现的路线 |
| `tools.ppt.renderDpi` | `144` | 页面渲染图交给模型前的光栅化精度 |
| `tools.ppt.renderConcurrency` | `2` | 并发的 LibreOffice 转换数 |
| `tools.ppt.deckName` | `deck.pptx` | 交付文件名 |
| `tools.ppt.designer.enabled` | `true` | 对完整 deck 自动运行设计环；只有显式关闭时才跳过 |
| `tools.ppt.designer.model` | `""` | 空表示用主模型 |
| `agents.defaults.workspace` | `~/.raven/workspace` | |

## 许可与归属

Apache-2.0。基于 EverMind AI 的 [Raven](https://github.com/EverMind-AI/Raven)，后者
又基于 [nanobot](https://github.com/HKUDS/nanobot) 与
[hermes-agent](https://github.com/NousResearch/hermes-agent)。第三方声明见
[`NOTICES.md`](NOTICES.md)。

有一个依赖不是宽松许可：**PyMuPDF 是 AGPL-3.0**。它负责读源 PDF —— 带版面的文本，
以及页面里的插图和表格 —— 这里没有别的东西能做到。它只被 import，没有被修改，也没有
被再分发。AGPL 的义务针对"分发这个库"或"通过网络提供它的服务"，不针对 import，所以
本地或内部部署不产生额外义务；如果要基于它做对外服务，请先读 AGPL，或向 Artifex 取
商业授权。光栅化与取词位置刻意用的是 pdfium（Apache-2.0），把 AGPL 的接触面限制在
读源文档这一件事上。
