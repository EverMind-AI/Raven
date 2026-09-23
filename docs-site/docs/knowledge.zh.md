# 知识库与文档检索

知识库保存你主动提供的文档，配置 embedding 后可检索相关段落。
它与记忆后端对对话内容的召回分开。

当前源码树提供知识库 Python 库和 `knowledge.*` RPC 处理函数，
但当前 WebUI 源码中**没有完整挂载的知识库管理功能**，也没有 `raven knowledge` CLI。
本页因此介绍可验证的库/RPC 流程，不假设每个安装都存在某些按钮。

## 选择文档、知识库或记忆 { #choose-documents-knowledge-or-memory }

| 需求 | 选择 |
| --- | --- |
| 为本次任务读取文件 | 附件或可读工作目录路径 |
| 反复从文档集合检索段落 | 已索引知识库 |
| 保存来源文档但不做向量检索 | 以 `embedding: false` 创建的库 |
| 召回用户偏好或 Agent 经验 | [记忆与技能](skills-and-extensions.md) |

创建知识库不会自动把所有内容加入每次模型 prompt。客户端需要检索目标库，
并显式将相关段落提供给任务。检索文本是待核实的证据，不是可信指令。

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

向量索引是内嵌的文件存储，不要求独立向量服务。库记录模型和维度；
后续不兼容的模型/维度变更会报 stale-base，而不是静默混用向量。
应新建库，并重新索引保留的来源。

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

以 `knowledge.status.extensions` 或 `supported_extensions()` 为准。
当前默认 parser 是结构化 Markdown/HTML 和纯文本系列，包括 CSV/JSON/YAML。
不能因为 Raven 其他功能支持预览 PDF 或 Office，就假定知识库也能索引它们。

笔记可用 `knowledge.documents.update_note` 更新后重新索引。
同一文档重新索引会替换向量，不累加重复项。修改切块设置后，需要重新索引已有文档；
改变 embedding 身份则需要重建库。

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
| WebUI 没有管理页 | 当前前端支持；使用兼容 RPC 客户端或库 |
| Embedding 未配置 | Provider/model 配对、endpoint 能力和凭据 |
| 索引失败 | 文档 error、真实 parser 支持和 embedding endpoint |
| 搜索为空 | 已索引 chunk、目标 base ID、是否启用 embedding |
| Stale base | 原始模型/维度与当前配置 |
| 来源改了但结果未变 | 存储副本与实时来源不同；更新/导入并重建索引 |
| 回答缺证据 | 显式提供检索段落，保留来源引用 |

实现位于 `raven/knowledge/`、`raven/rpc/methods/knowledge.py` 和
`raven/rpc/knowledge_preview.py`。源码/schema 校验不等于真实 embedding 服务兼容性测试。
