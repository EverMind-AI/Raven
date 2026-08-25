# Skill Hub 集成方案

把远程 Skill Hub(语义检索 + 元数据/正文 + zip 下载的技能市场)接入 Raven,使
agent 能在每轮**发现**相关 skill、由 **LLM 决策**用哪个、**按需取正文/脚本**并**执行**。

> 复用 Raven 现有 skill 栈(`SkillRouter` / `MassSkillSource` / `LocalPool` /
> `# Skills` & `# Active Skills` 段 / `exec` + `sandbox`),新增:**发现源**
> `SkillHubSource`、**HTTP 客户端** `SkillHubClient`、两个工具 `read_skill` / `use_skill`。

---

## 直接回答:要下载到本地吗?

**多数情况不用。下载只为"脚本/资产",不为"正文"。**

| 阶段 | 调用 | 下载 zip? |
|---|---|---|
| **发现 / 粗筛** | `GET /skills?q=`(目录元数据) | 否 |
| **读正文 / 细选 / 确认 / 执行纯指令 skill** | `GET /skills/{id}` → `result.skill_md` | **否**(正文随元数据返回) |
| **执行带脚本/资产的 skill** | `GET /skills/{id}/download` → zip | **是**(仅此情形:脚本要落盘给 `exec`) |

关键:Hub 的 `GET /skills/{id}` **直接回传完整 `skill_md`** → 读正文是一次轻量 GET,无需下载。
只有当 skill **捆绑了可执行脚本/资产**(SKILL.md 引用了 `scripts/` 等本地文件)、需要文件落盘时,才下载 zip。**纯指令 skill 全程不落盘。**

---

## 1. 关键约束(决定设计的事实)

**Skill Hub API**(统一信封 `{error, requestId, status, result}`,`error=="ok" && status==0` 为成功;`X-Request-ID` 透传):

| 端点 | 返回 | 用途 |
|---|---|---|
| `GET /openapi/v1/skills?q=&category=&sort=&page=&limit=` | `result.items[]`(元数据,**无正文**) | 语义检索 / 目录粗筛 |
| `GET /openapi/v1/skills/{id}` | 完整元数据 **+ `skill_md`(正文)+ `subscores`** | 读正文 / 细选 / 确认 |
| `GET /openapi/v1/skills/{id}/download?source=` | **zip 字节流**(`SKILL.md` + 脚本/资产) | 取可执行文件落盘 |

- 检索 item 字段:`id/slug/name/description/scenario_tags/score/score_availability/score_robustness/score_safety/token_p50/version/install_count/last_evolved_at/zip_url`。
- 详情额外字段:`skill_id/is_editable/git_url/license/download_url/`**`skill_md`**`/subscores{safety,utility,robustness,flags}`。
- `{id}` 接受 UUID 或 `{source}/{skill-name}`。

**Raven 现状**:`SkillRouter`(RRF 融合 Local/Mass/Everos)· `MassSkillSource`(远程库搜索源)· `# Skills`(目录注入)/ `# Active Skills`(全文注入)· skill=SKILL.md(+可选脚本),执行=正文进上下文+按指令做事+脚本经 `exec`。**缺口**:无"取远程 skill 正文/脚本"的机制。

---

## 2. 总体架构:三级渐进式披露

```
每轮:  user query
          │
   ① 粗筛  SkillRouter ── Local / Everos / SkillHubSource(GET /skills?q=)
          │   RRF 融合 → top-N 候选
          └─► 注入 "# Skills" 目录(Tier 0 元数据:name/desc/tags/score_*/id)
          ▼
   ② 细选  LLM 对候选不确定 → 读正文再判断
          │   read_skill(id) ─GET /skills/{id}─► result.skill_md(轻量,不下载)
          │   (或 host 预取 top-K 的 skill_md 随目录注入,免往返)
          └─► 据正文选定真正适配的 skill
          ▼
   ③ 使用  ├─ 纯指令 skill:已有 skill_md 在上下文 → 直接按指令执行(不下载)
          └─ 带脚本 skill:use_skill(id) ─GET /skills/{id}/download─► zip
                 → 校验+解压本地缓存 → 注册 LocalPool → 返回 skill_md + scripts_dir
          ▼
   ④ 执行  LLM 按 SKILL.md 执行;bundled 脚本经 exec/sandbox 运行
```

**三级披露**:

| 级 | 内容 | 调用 | 时机 | 落盘 |
|---|---|---|---|---|
| Tier 0 目录 | 元数据(name/desc/tags/score_*/id) | `GET /skills?q=` | 每轮 `# Skills`(粗筛) | 否 |
| Tier 1 正文 | 完整 `skill_md` | `GET /skills/{id}` | 细选 / 确认 / 纯指令执行 | 否 |
| Tier 2 脚本 | zip 内 scripts/assets | `GET /skills/{id}/download` | 仅带脚本 skill 执行时 | **是(缓存)** |

**原则**:**目录粗筛 → 正文细选+确认 → 脚本执行**。
`name/description` 往往**不足以最终选定**(同名/近义、措辞含糊、能力边界差异)——所以**"选哪个"也要读正文**;但读正文只是 `GET /skills/{id}`(正文随返回),**轻量、不下载**。下载(Tier 2)是**例外**,仅当 skill 捆绑可执行文件、需落盘给 `exec` 时。

---

## 3. 组件设计

### 3.1 `SkillHubSource`(发现层,host SkillSource)
`raven/memory_engine/skill_forge/hub_source.py`,实现 `SkillSource` 协议(仿 `MassSkillSource`):
- `search(query, history, k)` → `list[RouterHit]`。
- 调 `GET {endpoint}/openapi/v1/skills?q={query}&limit={k*over_fetch}`,带 `Authorization: Bearer <api_key>` + `X-Request-ID`。
- 解信封:`error=="ok" && status==0` 才取 `result.items`;否则返回 `[]`(交 `SkillRouter._safe_search` 单源故障隔离)。
- 映射 item → `RouterHit`:`name=item.name`,`qualified_id=f"hub/{item.slug}"`,`score=item.score`,`metadata={id, slug, description, scenario_tags, score_availability/robustness/safety, token_p50, version, install_count, last_evolved_at}`。
- 安全分过滤:`score_safety < min_safety` 不进候选(配置阈值)。
- 进 `SkillRouter`,`SkillRouterConfig.weights` 加 `hub`(建议 0.85)。`q` 空 → Hub 退化为排序列表(API 已支持)。

### 3.2 `# Skills` 目录注入(Tier 0)
- 复用现有 `# Skills` 段,渲染 RRF 融合后的候选:`name · description · scenario_tags · score_safety · 来源(hub)· id`。
- 提示词补:*"目录里的 skill 仅是候选;不确定就调 `read_skill(<id>)` 读正文判断,选定后纯指令直接执行、带脚本则 `use_skill(<id>)` 取脚本。"*

### 3.3 `SkillHubClient`(HTTP 客户端)
`raven/skill_hub/client.py`:
- `search(q, **filters) -> list[dict]`:`GET /skills`(供 SkillHubSource)。
- `get(id) -> dict`:`GET /skills/{id}` → 解信封返回 `result`(含 **`skill_md`** + 元数据 + subscores)。**读正文主路径,不下载。**
- `download(id, source="raven") -> bytes`:`GET /skills/{id}/download?source=raven` → zip 字节(非信封)。
- `install(id) -> dict`:`download` → 校验 → 解压缓存 → 返回 `{slug, version, dir, scripts_dir}`。缓存命中(同 `slug@version`)不重复下载。

### 3.4 `read_skill` 工具(细选 / 读正文,**不下载**)
```
read_skill(skill_id: str) -> { "skill_md": str, "name": str, "version": str,
                               "scenario_tags": [str], "subscores": {...} }
```
- 内部仅 `SkillHubClient.get(id)`(`GET /skills/{id}`)→ 把 `skill_md` 返回进上下文。
- 用于:① 细选(对粗筛候选逐个读正文比较);② 确认适配;③ **纯指令 skill 的"使用"——正文进上下文即可按指令执行,无需任何下载。**
- 轻量、可对多个候选连续调用;不注册、不落盘。

### 3.5 `use_skill` 工具(取脚本 / 落盘,**仅带脚本 skill 需要**)
```
use_skill(skill_id: str) -> { "skill_md": str, "name": str, "version": str,
                              "scripts_dir": str, "cached": bool }
```
- 行为:`SkillHubClient.install(id)`(下载 zip → 校验解压到本地缓存 → 注册 LocalPool/SkillRegistry)→ 返回 `skill_md` + `scripts_dir`。
- 用于:skill 的 SKILL.md 引用了 `scripts/`/资产、需文件落盘给 `exec` 时。
- 纯指令 skill **不必调**此工具(`read_skill` 已够)。
- 失败(网络/校验/不存在)→ 结构化错误,不抛崩 turn。

> 何时 `read_skill` 何时 `use_skill`?判据是**这个 skill 要不要本地文件**:正文(指令)用 `read_skill` 就够;只有当指令依赖捆绑脚本/资产时才 `use_skill` 落盘。提示词里说明,LLM 读了正文即可判断(SKILL.md 里若出现 `scripts/x.sh` 之类即需 `use_skill`)。

### 3.6 执行(Tier 2,复用)
- 正文(`skill_md`)在上下文;若已 `use_skill`,脚本在 `scripts_dir`。
- LLM 经现有 `exec` 工具跑脚本,走 `tools.sandbox`(限时/限网/隔离)。与本地 skill 执行路径一致,无需新执行器。

---

## 4. 本地缓存 / 版本 / 失效(仅 Tier 2)
- 仅"下载过 zip 的 skill"落盘:`~/.raven/skills/hub/<slug>@<version>/`(`SKILL.md` + `scripts/` + `meta.json`)。
- 索引 `index.json`:`slug → {version, id, installed_at, dir, last_evolved_at}`。
- 失效:候选带 `version`/`last_evolved_at`;`use_skill` 时本地版本 < Hub → 重新下载新版本。
- 离线:已装(带脚本)skill 不依赖网络;纯指令 skill 每次走 `read_skill`(可加正文短缓存)。
- 可选 CLI:`raven skill hub list/prune`。

## 5. 安全
- **zip 解压防护**:拒 `../`/绝对路径/symlink 逃逸;单文件与总大小上限;后缀白名单。
- **脚本执行**:强制 `tools.sandbox`(`exec` 限时、可选限网)。
- **下载完整性**:用 `/download` 字节流(`zip_url` 预签名会过期,不缓存签名 URL);有校验和则校验。
- **安全分门槛**:`score_safety`/`subscores.safety` 低于阈值的不进目录、`use_skill` 亦拒。
- **来源标记**:下载带 `source=raven`(统计/审计)。

## 6. 配置
`SkillRouterConfig` 新增 `hub`(与 `mass` 平级):

```jsonc
"skillRouter": {
  "weights": { "local": 1.0, "everos": 0.9, "hub": 0.85 },
  "hub": {
    "endpoint": "https://mss.evermind.ai",  // 不配则禁用 Hub 源,优雅降级
    "apiKey": "",                            // 可选 Bearer
    "timeoutS": 2.0,                         // 热路径,紧
    "minSafety": 0.7,                        // 安全分门槛
    "prefetchBodies": 1,                     // 预取 top-K 正文随目录注入(0=关)
    "source": "raven"                        // 下载统计标识
  }
}
```

## 7. 与现有 Raven 的映射
| 需求 | 复用 / 新增 |
|---|---|
| 远程检索源 | **新增 `SkillHubSource`**(仿 `MassSkillSource`) |
| 多源融合 | 现有 `SkillRouter` RRF(+`weights.hub`) |
| 目录注入(Tier0) | 现有 `# Skills` 段 |
| 读正文(Tier1) | **新增 `read_skill` 工具 + `SkillHubClient.get`** / 现有 `# Active Skills` 回填 |
| 取脚本落盘(Tier2) | **新增 `use_skill` 工具 + `SkillHubClient.install`** |
| 本地 skill 管理 | 现有 `LocalPool` / `SkillRegistry` |
| 执行脚本 | 现有 `exec` + `tools.sandbox` |
| 配置 | `SkillRouterConfig.hub` |

> Skill Hub 是第一方远程库,定位等同现有 "Mass" 源 → 走 host SkillSource。

## 8. 分阶段实现
**P1 — 发现**:`SkillHubSource`(GET 搜索 + 信封解析 + RouterHit + 安全分过滤)· `SkillRouterConfig.hub` + 工厂条件注册 · 目录渲染 · mock 单测。→ hub skill 进 `# Skills`。

**P2 — 读正文 + 细选**:`SkillHubClient.get`(`GET /skills/{id}` → skill_md)· `read_skill` 工具 ·(可选)host 预取 top-K 正文注入 · 提示词引导 · mock 单测。→ LLM 能读正文细选并执行纯指令 skill。

**P3 — 取脚本 + 执行**:`SkillHubClient.download/install`(解压、校验、缓存)· `use_skill` 工具 · sandbox 接线 · mock 单测(正常/路径穿越/超大/缺 SKILL.md)。→ 带脚本 skill 可落盘执行。

**P4 — 打磨(可选)**:版本失效/LRU/`raven skill hub` CLI · `memory_fields` 联动 everos 预取(若 Hub 提供)· `real_*` 集成跑全链路。

## 9. 测试
- **单元**(默认 CI,无网络):`test_skill_hub_source.py`(MockTransport:RouterHit 映射/信封降级/安全分)、`test_skill_hub_client.py`(mock get→skill_md;mock zip:正常/路径穿越/超大/缺正文)、`test_*_tools.py`(read_skill/use_skill 契约)。
- **集成**(`real_*`,opt-in):`test_skill_hub_e2e.py` —— 发现 → read_skill 选定 →(带脚本则 use_skill)→ exec 执行。

## 10. 开放问题
1. **何时需 `use_skill`**:靠 LLM 读正文判断"是否引用本地脚本",还是 Hub 在 `/skills/{id}` 加 `has_bundle`/`assets` 标志更可靠?(建议后者)。
2. **正文短缓存**:`read_skill` 的 `skill_md` 是否进程内短缓存(同 turn 多次比较免重复 GET)。
3. **同名 dedup**:hub 与 local/everos 同名时 `SkillRouter.dedup_by` 与优先级。
4. **鉴权/配额**:游客 200 上限、限流、token 续期。

---

## 一句话
> 给 `SkillRouter` 加 **`SkillHubSource`(目录**粗筛**)**;读正文用 **`read_skill`**(`GET /skills/{id}` 直接拿 `skill_md`,**不下载**)做细选/确认/执行纯指令 skill;只有 skill **带可执行脚本**时才用 **`use_skill`**(`/download` 取 zip、解压落盘、注册 LocalPool)。三级:**目录粗筛 → 正文细选 → 脚本执行**。**下载是例外,不是常态**——因为 `GET /skills/{id}` 已直接给正文。其余全复用 Raven 现有 skill 栈。
