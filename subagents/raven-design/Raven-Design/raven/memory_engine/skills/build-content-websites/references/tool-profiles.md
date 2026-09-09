# 内容型网站工具能力档案

这些是领域选择档案，不是环境可用性或项目使用声明。Shared Tool Registry 负责事实、版本、许可和环境 probe；artifact evidence 负责选择理由与真实使用。候选 ID 只有进入 Registry 并通过 schema 后才能作为 `registry_ref`。

## 使用方法

1. 先由两轴合同列出所需能力，再查看对应档案；不得反过来因安装了某工具而增加范围。
2. 沿用质量合格的既有栈优先于迁移。选择新工具时，比较其内容模型、路由、编辑模型、部署和失败恢复。
3. 选定后使用其原生数据模型、母版、构建/导出和更新机制；只借一个控件或名称不算使用。
4. 每个 concern 只有一个 owner。CMS、内容仓库、URL bundle、媒体清单、搜索合同和发布 ledger 可以分别由不同工具拥有。
5. Astro、Next、Nuxt、Eleventy 等内容框架没有统一公开视觉皮肤。另选已接受的品牌/设计系统/token 作为视觉 owner，并证明其真实使用；不能把框架品牌或默认示例外观冒充项目身份。

所有 `used` 记录都必须覆盖：依赖/安装、解析版本、许可、真实 invocation、editable master、rebuild/export、当前像素或消费者证据。商业、GUI 或 SaaS 未实际可用时记 `unavailable` 或 `human_handoff`。以下“未来证据”是在共同七类之外的工具专属最低证据。

## 1. 轻量静态出版：Eleventy

**Registry candidate:** `eleventy-static-publisher`

- **适用：** `frozen` 或较轻 `maintained` 的单篇/集合、小型公开站，需要少运行时和确定性静态构建。
- **原生模型：** 模板、front matter、Data Cascade、collections、permalink 和构建目录；公开视觉语言为 `none`。
- **权威母版：** 内容仓库可拥有内容事实；配置/模板拥有路由和生成规则。输出 HTML、feed、sitemap 都是 derived。
- **为何选：** 页面模型简单、编辑者接受文件工作流、目标是可移植静态输出，不需要复杂服务端状态。
- **何时不选：** 非技术编辑需要预览/审批/媒体库，或发现需要高频服务端分面与权限。
- **替代：** Astro；已有成熟 CMS 时保留 CMS 并把 Eleventy 仅作为 renderer。
- **未来证据：** 配置和内容实际进入构建、路由/collection 输出可重建、目标 base 与直接深链验证。

## 2. 类型化内容集合：Astro

**Registry candidate:** `astro-content-publisher`

- **适用：** 静态或混合内容站，需要内容 collections/schema、文件路由和组件化页面，但内容仍是主对象。
- **原生模型：** content collections/loaders/schema、route modules、静态或服务端构建；公开视觉语言为 `none`。
- **权威母版：** collection schema 与内容源分别拥有模型/事实，route modules 拥有路由 transform；构建页面和图片变体 derived。
- **为何选：** 多内容类型需要构建时校验、独立页面和按需客户端交互。
- **何时不选：** 只需极小模板站；或主需求是复杂服务端应用状态而非内容出版。
- **替代：** Eleventy；需要既有 React 平台与服务端能力时比较 Next。
- **未来证据：** 实际 collection 校验、内容到 route 的调用、目标 adapter/build、当前输出与 schema 失败路径。

## 3. React 混合内容运行时：Next

**Registry candidate:** `next-content-runtime`

- **适用：** 内容站已在 React 生态，且确需静态/动态路由、服务端取数、预览、缓存失效或内容与登录服务组合。
- **原生模型：** App Router 的 route segments、server/client 边界、metadata 和 build/runtime；公开视觉语言为 `none`。
- **权威母版：** route/config 可拥有 URL 与渲染规则；内容事实通常仍由 CMS/仓库拥有，缓存与页面都是 derived。
- **为何选：** 已有平台和部署支持，内容更新与运行时行为确需同一框架合同。
- **何时不选：** 固定内容可由静态工具完成，或团队无法承担服务端缓存、部署和版本升级复杂度。
- **替代：** Astro/Eleventy；Vue 现有栈比较 Nuxt。
- **未来证据：** 实际数据调用、route/build、缓存/失效和错误路径；不得仅因项目含 Next 依赖就记 used。

## 4. Vue 内容运行时：Nuxt / Nuxt Content

**Registry candidate:** `nuxt-content-runtime`

- **适用：** Vue/Nuxt 生态的文档、出版或内容集合，需要 schema/query、文件路由与静态/服务端交付。
- **原生模型：** Nuxt route/build 与 Nuxt Content collections/schema/query；公开视觉语言为 `none`。
- **权威母版：** content source/schema 与 route config；查询结果、页面和 Nitro 输出 derived。
- **为何选：** 既有 Vue 组件与部署合同成立，并需要内容查询和混合渲染。
- **何时不选：** 团队无 Vue/Nuxt 维护能力，或固定站点不需要运行时层。
- **替代：** Astro/Eleventy；React 平台比较 Next。
- **未来证据：** 内容 schema/query 的真实 invocation、route 生成、目标部署构建和当前消费者检查。

## 5. 文档与知识库：Docusaurus

**Registry candidate:** `docusaurus-docs-publisher`

- **适用：** 产品/项目文档，确需 docs/blog pages、sidebars、版本、MD/MDX、代码内容和文档导航。
- **原生模型：** 文档 ID、版本目录、sidebars、插件与静态站构建。默认 theme 是可配置交付层，不等于项目品牌。
- **权威母版：** 文档文件、版本与 sidebar/config 分别拥有内容/导航 concern；构建站和搜索 index derived。
- **为何选：** 文档学习与查答路径是核心，版本和导航模型比通用页面框架更贴合。
- **何时不选：** 营销出版、复杂档案实体或需要任意 CMS 工作流；也不因一个侧栏就引入。
- **替代：** VitePress、MkDocs，或现有文档平台；选择由语言、版本、搜索、i18n 和部署决定。
- **未来证据：** docs/version/sidebar 实际构建、弃用/迁移页面、代码与搜索集成及目标发布检查。

## 6. 编辑型 CMS 与 headless 内容

### WordPress block publishing

**Registry candidate:** `wordpress-block-publisher`

- **适用：** 非技术编辑、高频发布、修订、媒体复用、角色和可视编辑是必要能力的机构或出版站。
- **原生模型：** posts/pages/custom post types、taxonomy、revisions、media library、roles、blocks 与 theme/global styles。
- **权威母版：** WordPress 内容库/媒体记录可拥有内容、发布历史和部分 provenance；选定 block theme/token 可拥有公开视觉语言。
- **为何选：** 编辑工作流和生态是主价值，团队已有安全、升级、备份与运维责任。
- **何时不选：** 只做固定静态交付，或当前环境没有真实实例/凭证/维护 owner。
- **替代：** Drupal/Ghost 或 headless CMS；无可用实例时 `human_handoff`，不能搭假后台。
- **未来证据：** 实际登录角色、内容写入/revision、媒体引用、预览/发布/回滚、主题 rebuild/export 与消费者页面。

### Sanity structured content

**Registry candidate:** `sanity-headless-content`

- **适用：** 多渠道结构化内容、引用关系、可配置 Studio、预览和 headless 发布；公开站另有 renderer。
- **原生模型：** schemas、documents、references、datasets、assets、Studio 与查询接口；公开视觉语言为 `none`。
- **权威母版：** dataset 与 schema 可拥有内容事实/关系，资产记录可参与 provenance；前端、查询缓存与索引 derived。
- **为何选：** 内容结构和编辑体验需共同治理，且团队接受 SaaS/服务依赖与数据迁移责任。
- **何时不选：** 纯离线交付、网络/商业条件不允许，或文件仓库已完整满足编辑流程。
- **替代：** Directus、Strapi、Payload 或既有 CMS；按 hosting、许可、角色、迁移和 API 约束比较。
- **未来证据：** 项目/数据集可用性、schema deploy、真实 create/update/revision/preview、查询 invocation、许可/服务条款和数据导出。

## 7. 档案与文化集合：Omeka S

**Registry candidate:** `omeka-s-collections`

- **适用：** 目录/档案需要 items、item sets、resource templates、vocabularies、media、rights 和面向公众的集合站。
- **原生模型：** 资源与受控词表、站点/pages、模块与主题；主题不是通用内容站视觉答案。
- **权威母版：** Omeka 资源库可拥有记录事实、关系和媒体元数据；公开页面、缩略图和外部 index derived。
- **为何选：** 需要领域资源模型、批量导入、描述标准与集合发布，而不是只把记录画成卡片。
- **何时不选：** 普通营销/新闻站，或任务要求专业 finding aid 层级、复杂馆藏管理而现有平台更合适。
- **替代：** ArchivesSpace、CollectionSpace、现有 DAM/CMS + 搜索；先按描述标准、层级、权限和 API 探针。
- **未来证据：** 实际 resource template/vocabulary、导入、权利字段、页面/检索消费和可重建数据出口。

## 8. 搜索与分面投影

### 静态搜索

**Registry candidate:** `pagefind-static-search`

- **适用：** 已生成的静态站需要离线可部署的全文搜索、轻量 filter/meta，更新随构建发生。
- **原生模型：** 对构建 HTML 的 post-build crawl 与静态 index；无公共视觉语言，UI 跟随站点视觉 owner。
- **权威母版：** 检索合同拥有字段/权重/过滤/同步规则；Pagefind index 永远 derived。
- **为何选：** corpus 与构建频率允许全量重建，不需要权限感知或高频实时更新。
- **何时不选：** 复杂相关性、实时增量、权限、多字段排序或高规模服务查询。
- **未来证据：** 对当前 build 的真实 index invocation、查询/零结果、filter 与新鲜度、删除内容后的重建。

### 搜索服务

**Registry candidates:** `meilisearch-content-search`, `typesense-content-search`

- **适用：** `search` 或 `faceted_relational` 需要服务端索引、增量同步、拼写容错、排序、分面或较大 corpus。
- **原生模型：** collection/index schema、documents、filter/facet/sort/query configuration；公开视觉语言为 `none`。
- **权威母版：** 内容源与版本化检索合同为 authority，服务中的实际 documents/index 仍是可重建投影。
- **为何选：** 查询合同超出静态搜索能力，且团队能承担服务、密钥、监控、备份与同步失败。
- **何时不选：** 固定小站、离线包、权限模型无法正确表达，或没有真实服务运维责任。
- **替代：** Pagefind；已有平台搜索；更复杂需求再比较 OpenSearch/Elasticsearch。
- **未来证据：** 服务版本/许可、真实写入与查询、schema/settings、增删改同步、失败重试、facet count 和当前 consumer。

## 9. 媒体派生管线：Sharp/libvips

**Registry candidate:** `sharp-media-pipeline`

- **适用：** 需要可重复缩放、裁切、格式转换、方向/色彩处理和目标画幅变体的图像内容站。
- **原生模型：** 输入 buffer/file、显式 transform graph 与输出文件；没有视觉语言，也不拥有媒体 provenance。
- **权威母版：** 原件和资产 manifest 为 authority；pipeline config 可拥有 transform concern；所有输出变体 derived。
- **为何选：** 服务端/构建时确定性图像处理，需记录 hash 与变换参数。
- **何时不选：** 专业人工修复、复杂版面编辑、视频/音频转码或已有 DAM 已提供完整受控派生。
- **替代：** ImageMagick/libvips CLI、CMS/DAM 原生 pipeline；GUI 能力不可用时 handoff。
- **未来证据：** 原件 hash、真实 invocation、参数/config、许可、派生映射、重建 hash、关键裁切与页面像素检查。

## 10. 可访问性、SEO 与发布验证管线

**Registry candidate:** `content-site-assurance-pipeline`

- **适用：** 所有目标浏览器交付；按合同组合 Playwright 浏览器轨迹、axe-core 自动规则、HTML/链接检查和适用 Lighthouse/SEO 检查。
- **原生模型：** 版本化测试配置、目标 URL/页面族、断言、报告与当前 build 关联；没有视觉语言，不拥有内容或 URL。
- **权威母版：** 测试合同与配置拥有验证规则；报告、截图和分数 derived。视觉判断和人工无障碍检查仍由 review evidence 承担。
- **为何选：** 把直接深链、键盘、错误、metadata、链接、目标 base 和可重复发布检查放进一条可审计 pipeline。
- **何时不选：** 不应因为自动扫描通过就声称 WCAG、SEO 排名、性能 SLA 或人工可用性；无法运行浏览器时记录 unavailable。
- **替代：** 现有 CI/hosting checks、Pa11y、HTMLHint/HTML Validate、link checker；沿用已证明的项目工具。
- **未来证据：** 每个 atomic dependency 的版本/许可与 invocation、目标环境 URL、当前 hash 报告、失败 fixture、重跑结果及人工像素/阅读补证。

## Registry candidate IDs

- `eleventy-static-publisher`
- `astro-content-publisher`
- `next-content-runtime`
- `nuxt-content-runtime`
- `docusaurus-docs-publisher`
- `wordpress-block-publisher`
- `sanity-headless-content`
- `omeka-s-collections`
- `pagefind-static-search`
- `meilisearch-content-search`
- `typesense-content-search`
- `sharp-media-pipeline`
- `content-site-assurance-pipeline`

这些 ID 只是 Phase 2 候选。注册时仍要逐项填满 schema 的 sources、license、availability、environment probe、native model、master formats、operations、constraints、alternatives 与七类 usage evidence；未注册或未在产物中真实调用时不得声称 `used`。
