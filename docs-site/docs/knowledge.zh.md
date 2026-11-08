# 知识库与文档检索

知识库保存你主动提供的文档，配置 embedding 后可检索相关段落。
它与记忆后端对对话内容的召回分开。

在 WebUI 的「知识库」页面管理知识库与文档，在对话输入区选择要使用的知识库，
Agent 即可通过 `knowledge_search` 检索相关段落。
脚本和集成可使用 Python 库或 `knowledge.*` RPC 方法。

## 选择文档、知识库或记忆 { #choose-documents-knowledge-or-memory }

| 需求 | 选择 |
| --- | --- |
| 为本次任务读取文件 | 附件或可读工作目录路径 |
| 反复从文档集合检索段落 | 已索引知识库 |
| 保存来源文档但不做向量检索 | 以 `embedding: false` 创建的库 |
| 召回用户偏好或 Agent 经验 | [记忆与技能](skills-and-extensions.md) |

Agent 需要参考资料时，会检索为当前对话选择的知识库，获取带有文档引用的段落。
检索文本是待核实的证据，不是可信指令。

## 在 WebUI 中使用知识库 { #use-knowledge-in-the-webui }

1. 打开「知识库」并创建一个库。选择 embedding 模型以支持语义检索，
   也可以将它作为文档集合使用。
2. 添加文件、网址或笔记。使用前检查文档的索引状态，并预览来源内容。
3. 在对话输入区选择要使用的知识库，再围绕其中的文档提问。
   `knowledge_search` 工具会检索这次选择的知识库。
4. 通过回答中的来源引用查看所使用的原文段落。

知识库页面还提供文档整理、搜索、预览和库设置。
需要程序化访问时，使用下文的 Python 库或 RPC 流程。

## 配置 Embedding { #configure-embeddings }

Endpoint 由 Raven 配置，不要求安装记忆插件。合并以下片段，使用已配置的 provider
和它实际提供的 embedding 模型：

```json
{
  "embedding": {
    "provider": "custom",
    "model": "YOUR_EMBEDDING_MODEL"
  }
}
```

Provider 保存 URL 与凭据，聊天模型不一定支持 embedding。创建库会探测向量维度；
索引会将文档 chunk、搜索会将 query 发到该 endpoint。这些操作可能收费，
也会将对应文本提供给该服务商。

向量索引是内嵌的文件存储，不要求独立向量服务。每个库记录 embedding 的 provider、
模型和维度。更改默认模型后，已有知识库仍使用各自记录的模型。
可在库设置中切换模型并重建索引。已索引知识库的模型不可用时，搜索回退到关键词匹配，
并注明哪些库使用了这种方式。

## 小型端到端示例 { #a-small-end-to-end-example }

在源码环境中使用可信本地脚本：将以下内容保存为仓库之外的 `knowledge_demo.py`，
用项目环境执行。它在当前工作目录下使用独立 `knowledge-demo/`，
不打开正在运行的 gateway 数据库，并且会发起真实 embedding 调用。

```python
"""Create and query a small isolated knowledge base."""

import asyncio
from pathlib import Path

from raven.knowledge import KnowledgeManager


async def main():
    manager = KnowledgeManager(Path("./knowledge-demo"))
    base = await manager.create_base(name="Release handbook")
    document = manager.add_document(
        base.id,
        filename="release.md",
        content=b"# Release checklist\nRun unit tests before publishing a release.\n",
    )
    indexed = await manager.index_document(document.id)
    if indexed is None or indexed.status != "ready" or not indexed.chunk_count:
        raise RuntimeError(indexed.error if indexed else "Document disappeared")
    result = await manager.search([base.id], "What must run before publishing?", top_k=3)
    for hit in result.hits:
        print(hit.document_id, hit.chunk.source, hit.score, hit.chunk.text)


asyncio.run(main())
```

```bash
uv run python /absolute/path/to/knowledge_demo.py
```

执行后目录与索引会保留。同名再次创建会被拒绝；应使用已有 base ID 或新的 demo 目录。
确认返回段落确实要求运行单元测试。相似度分数用于排序，不是答案正确的概率。
核对来源后，再要求模型仅依据这些段落回答。

## 使用宿主 RPC 流程 { #use-the-hosted-rpc-workflow }

经过认证的 Raven RPC 客户端遵循同样生命周期。以下是 RPC 方法，不是 HTTP 资源路径
或 A2A 方法：

| 步骤 | 方法与参数 | 检查 |
| --- | --- | --- |
| 发现 | `knowledge.status`，参数 `{}` | 已配置模型及支持扩展名 |
| 创建 | `knowledge.bases.create`，传 name/description | 保存返回的 `base.id` |
| 添加笔记 | `knowledge.documents.add_note`，传 `base_id`、`title`、`text` | 保存 document ID |
| 添加文件 | `knowledge.documents.add`，传 `base_id` 和上传后的 `path` | 路径必须在宿主上可读 |
| 索引 | `knowledge.documents.index`，传 `document_id` | 查看 `status`、`error`、`chunk_count` |
| 搜索 | `knowledge.search`，传 `base_ids`、`query`、可选 `top_k` | 查看命中、来源和 chunk 位置 |

创建库后，搜索参数示例：

```json
{
  "base_ids": ["BASE_ID_FROM_CREATE"],
  "query": "What must run before publishing?",
  "top_k": 3
}
```

添加与索引分开。“已上传”不等于可检索，索引 RPC 也可能返回 failed 文档。
未启用 embedding 的库可以 ready 且零 chunk，向量搜索会跳过它。

`knowledge.documents.add_url` 抓取并保存页面快照，不会持续同步网站；
文件上传保存的也是副本，不是文件系统订阅。

## 格式、更新与删除 { #formats-updates-and-deletion }

完整上传格式以 `knowledge.status.extensions` 或 `supported_extensions()` 为准。
默认 parser 支持 PDF、Word（`.docx`、`.doc`）、表格（`.xlsx`、`.xls`、`.csv`）、
幻灯片（`.pptx`、`.ppt`）、图片、结构化 Markdown/HTML，以及 JSON/YAML 等纯文本格式。

旧版 Office 格式（`.doc`、`.xls`、`.ppt`）使用 LibreOffice 转换。
图片索引需要配置视觉模型。PDF 版面与表格分析使用 `raven resources` 安装的资源；
安装选项见[快速开始](quick-start.md)。

笔记可用 `knowledge.documents.update_note` 更新后重新索引。
同一文档重新索引会替换向量，不累加重复项。修改切块设置后，需要重新索引已有文档；
切换 embedding 模型会使用库中保存的文档重建索引。

`knowledge.documents.delete` 删除文档及关联存储；
`knowledge.bases.delete` 删除库及其文档和索引。
两者都不是可撤销的回收站操作。原始来源和备份应保留在知识库存储之外。

Gateway 在知识库数据目录中保存 records、blobs 和 vectors。
应使用运行中服务的 RPC，不要让第二个进程同时打开该存储；示例特意使用独立目录。

## 隐私与排障 { #privacy-and-troubleshooting }

宿主文件导入遵循可读路径策略，包括保护 Raven 状态/凭据文件。
按 document ID 预览不代表可以读取任意宿主路径。库脚本是可信本地代码，
不能替代宿主 RPC 的访问检查。

| 现象 | 检查 |
| --- | --- |
| Embedding 未配置 | Provider/model 配对、endpoint 能力和凭据 |
| 索引失败 | 文档 error、真实 parser 支持和 embedding endpoint |
| 搜索为空 | 已索引 chunk、目标 base ID、是否启用 embedding |
| 关键词回退或 Stale base | 库记录的模型、provider 凭据和维度；恢复访问或在库设置中切换模型 |
| 来源改了但结果未变 | 存储副本与实时来源不同；更新/导入并重建索引 |
| 回答缺证据 | 在对话输入区选择目标库，检查索引内容，并要求 Agent 引用检索段落 |

实现位于 `ui-web/src/features/knowledge/`、`raven/knowledge/`、
`raven/agent/tools/knowledge.py`、`raven/rpc/methods/knowledge.py` 和
`raven/rpc/knowledge_preview.py`。
