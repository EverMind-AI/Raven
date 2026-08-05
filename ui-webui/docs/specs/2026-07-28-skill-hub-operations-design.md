# Skill Hub 接入 + Skill 操作页 —— 需求 / 设计

Date: 2026-07-28
Status: draft(待评审)
Owner: zhengwei.wu
前置:`2026-07-27-skillforge-config-page-design.md`(已实现的只读配置页)在此之上扩展。

## 0. 范围

在现有 `/raven-skills` 配置页(只读 skill 表)之上,新增**两块能力**:

- **Req2 接 Skill Hub**:不止能填 Hub 配置,还能测通、浏览、搜索、一键安装。
- **Req3 操作 skill**:看正文、从 Hub 安装、删除/清理、启用/禁用单个 skill。

**不在本 spec 范围**(单列/暂缓):Req1 的配置字段扩展(`router.topK` 等)、编辑/新建本地 skill(写 SKILL.md)、深层 model knob 进 UI。

底层 SkillForge 引擎与 Skill Hub 客户端(`raven/skill_hub/`、`raven/memory_engine/skill_forge/`)已存在(见 `docs/skill-hub-integration-design.md`);本 spec 只做**面向 web 的操作面**(RPC + REST + 前端),尽量复用现成函数。

---

## 1. 功能需求(逐条,带验收标准)

### R2 — 接 Skill Hub

| # | 需求 | 验收标准 |
|---|------|---------|
| R2.1 | Hub 配置 | (已有)endpoint / apiKey(密文)/ minSafety / timeoutS 可读写。 |
| R2.2 | **测试连接** | 页面有「测试连接」按钮;点击后用当前 endpoint+apiKey 打一次 Hub → 返回「连通/失败+原因」。不写配置也能测(用表单当前值)。 |
| R2.3 | **浏览 Hub 目录** | 能拉取 Hub 技能列表(分页/排序),每条显示 name、description、tags、score_safety、version、install_count、是否已装。 |
| R2.4 | **搜索 Hub** | 输入关键词 → `GET /skills?q=` → 结果同 R2.3 卡片。空关键词退化为排序列表。 |
| R2.5 | **一键安装** | 浏览/搜索结果里点「安装」→ 下载解压到 `workspace/skills/hub/<slug>@<version>` → 安装成功后该 skill 出现在本地列表、可用;`min_safety` 以下的拒装并提示。 |

### R3 — 操作 skill

| # | 需求 | 验收标准 |
|---|------|---------|
| R3.1 | **查看正文** | 点开任一 skill(本地或已装 Hub)→ 弹层/抽屉展示其 SKILL.md 正文(markdown 渲染)。 |
| R3.2 | 从 Hub 安装 | 同 R2.5(Req2/Req3 重叠,合并实现)。 |
| R3.3 | **删除 / 清理** | 已装的 Hub skill 可「移除」(删本地目录 + 刷新 registry,移除后不再出现/被检索);可选「prune 旧版本」。本地手放的 skill(localDirs)不在此删除范围。 |
| R3.4 | **启用 / 禁用单个 skill** | 列表每行一个开关,关闭后该 skill **不参与检索/注入**(但**不删文件**,可再开回);状态持久化,重启后仍生效。 |

---

## 2. 后端就绪度(决定工作量)

调研结论(file:line 见附录),分两档:

### 🟢 A 档 — 函数现成,只缺一层 RPC(小工作量)
- **看本地正文**:`SkillRegistry.get_body` / `LocalSkillCatalog.load_skill` 现成;`SkillMeta.content` 已是去 frontmatter 正文。
- **看 Hub 正文**:`SkillHubClient.get(id)` 返回 `skill_md`。
- **Hub 搜索/安装**:`SkillHubClient.search` / `.install` 纯异步、**不需要 agent 上下文**,RPC handler 里自建 client(`cache_dir=workspace/skills/hub`)即可,装完 registry 自动发现 + `invalidate_source("hub")` 刷新。
- **测连**:复用 `search` 打一下即可。

### 🔴 B 档 — 后端零支撑,需新建(大工作量)
- **删除/清理**:无任何 prune/uninstall、无 index.json、`raven skill hub list/prune` CLI 未实现。需**新建**:删目录 helper + `invalidate_source("hub")` + 缓存索引(可选)。
- **启用/禁用单个 skill**:raven **只有目录级** `localDirs[].enabled`,**无 per-skill 开关概念**。需**新建**:配置模型 `skillForge.disabledSkillIds` + registry/router 在 gather 时过滤 + 读写路径。
- **热应用**:skill 配置变更目前**全部 restart_required**(无 `apply_skillforge` hook,不像 subagents 有热应用)。

---

## 3. 接口设计(提议)

### 3.1 gateway RPC(`raven/web_rpc/methods_config.py`)
| RPC | 入参 | 出参 | 档 |
|-----|------|------|----|
| `raven.skills.list`(扩展) | — | `skills:[{name,source,description,id,path,enabled,version?}]` | A |
| `raven.skills.body` | `{name?, id?, source}` | `{skillMd, name, source, path?}` | A |
| `raven.skills.hub.test` | `{endpoint, apiKey}` | `{ok, detail}` | A |
| `raven.skills.hub.search` | `{q, category?, sort?, page?, limit?}` | `{items:[...], page, total?}` | A |
| `raven.skills.hub.install` | `{id}` | `{ok, slug, version, dir}` | A |
| `raven.skills.hub.remove` | `{slug, version?}` | `{ok, removed:[...]}` | B |
| `raven.skills.enable` / `.disable` | `{id\|name, source}` | `{ok, restart_required?}` | B |

### 3.2 service REST 代理(`ui-webui/service/raven_config_routes.py`)
- `GET /raven/skills/available`(扩展字段)
- `POST /raven/skills/body`(`{name,id,source}`)
- `POST /raven/skills/hub/test`
- `GET /raven/skills/hub/search?q=&page=&limit=`
- `POST /raven/skills/hub/install`(`{id}`)
- `DELETE /raven/skills/hub`(`{slug,version?}`)
- `POST /raven/skills/toggle`(`{id\|name,source,enabled}`)

### 3.3 前端(`ui-webui/frontend/src/pages/raven-skills/`)
在现有页面下追加两个区:
- **Skill Hub 区**:endpoint/apiKey 配置 + 「测试连接」按钮(状态徽标)+ 搜索框 + 结果卡片列表(name/desc/tags/safety/version/install_count + 「安装」按钮;已装标记)。
- **已装 / 本地 skill 区**:表格,每行 name · source(Badge)· version · **启用开关** · 「查看正文」· 「删除」(仅 Hub 装的可删)。
- **正文查看**:Sheet/Dialog 里 markdown 渲染 SKILL.md。

---

## 4. 关键决策(需拍板)

| # | 决策 | 提议 | 理由 |
|---|------|------|------|
| D1 | per-skill 启停如何建模 | 新增 `skillForge.disabledSkillIds: []`(按稳定 id;无 id 退回 `source/name`),registry/router gather 时过滤 | 不改单个 SKILL.md 文件,干净、可逆、易读写 |
| D2 | 删除机制 | 删 `workspace/skills/hub/<slug>@<version>` 目录 + `invalidate_source("hub")`;维护 `index.json` 便于 prune | 复用现有缓存布局与 registry 刷新 |
| D3 | 生效方式(热 vs 重启) | **安装/删除**靠 registry `invalidate_source` **即时生效,无需重启**;**启停 / Hub 配置变更**一期 `restart_required`,二期再加 `apply_skillforge` hook | 安装类改的是磁盘(watcher/invalidate 能感知);配置类改内存需 hook |
| D4 | zip 安全 | 安装复用 `_safe_extract`(已防路径穿越/限大小/后缀白名单),**补 symlink 显式拒绝 + 缺失测试** | BUGS 报告已指出 symlink 是"侥幸安全" |

---

## 5. 安全

- 安装走既有 `SkillHubClient._safe_extract` zip 防护;**本 spec 要求补齐**:显式拒绝 symlink 条目、绝对路径条目;补 zip 安全测试(超大单文件 skip / 总量超限 raise / 缺 SKILL.md / 路径穿越 / symlink)。
- `min_safety` 门槛:低于阈值的 skill 不进浏览列表、`install` 亦拒。
- Hub apiKey:`get` 时脱敏(占位回传,写路径遇占位跳过 patch),对齐 channels 密钥处理(BUGS 报告项)。
- 已装 skill 的脚本执行沿用现有 `exec` + `tools.sandbox`,本 spec 不改执行路径。

---

## 6. 分期

- **一期(A 档)**:R2.1–R2.5 全部 + R3.1 看正文 + R3.2 安装。→ Req2 完整 + Req3 的"看/装"。改动集中在薄 RPC + REST + 前端,不碰 raven 核心模型。
- **二期(B 档)**:R3.3 删除/prune + R3.4 单 skill 启停(新增 `disabledSkillIds` 模型 + registry 过滤)+(可选)`apply_skillforge` 热应用。

---

## 7. Non-goals

- 编辑 / 新建本地 skill(写 SKILL.md 到磁盘)。
- 深层 model knob(embeddingModel / scanMaxDepth / evolveModel / reranker)进 UI。
- Req1 的配置字段扩展(`router.topK` 等)——另行单列。
- 删除 localDirs 里用户手放的本地 skill(只删 Hub 装的)。

---

## 8. 测试

- **RPC 单测**(mock hub client):`body` / `hub.search` / `hub.install` / `hub.remove` / `enable-disable` 契约;`list` 扩展字段。
- **zip 安全测试**(补齐):超大单文件 skip、总量超限 raise、缺 SKILL.md、路径穿越、symlink。
- **service**:curl 验证各 REST(参考现有 skillforge 页做法)。
- **前端 gate**:`pnpm -C ui-webui/frontend lint` + `build`;e2e smoke:测连→搜索→安装→看正文→(二期)禁用/删除。

---

## 附录 · 后端就绪度证据(file:line)

- 本地正文:`raven/memory_engine/skill_local/registry.py:340`(`get_body`)、`skill_forge/catalog.py:198`(`load_skill`)、`skill_local/types.py:23-26`(`SkillMeta.content/path`)。
- Hub 客户端:`raven/skill_hub/client.py:125`(`search`)、`:150`(`get`→skill_md)、`:168`(`install`)、`:218`(`_safe_extract`)。
- 安装落盘可发现:`raven/agent/loop/main.py:728,739`(cache_dir=workspace/skills/hub)、`registry.py:403`(rglob 发现)。
- 现有 RPC:`raven/web_rpc/methods_config.py:210-226`;`list_skills` 只回 name/source/description:`raven/config/update_skills.py:129`。
- 目录级开关(无 per-skill):`raven/config/raven.py:772`(`LocalDirConfig.enabled`)、`catalog.py:53-54`(跳过)。
- 无 prune / index.json / hub CLI:`raven/cli/skill_commands.py`(仅 `list`/`get`)。
- 无热应用:`methods_config.py:219`(restart_required);对照 subagents `main.py:1235`(`apply_third_party_subagents`)。
