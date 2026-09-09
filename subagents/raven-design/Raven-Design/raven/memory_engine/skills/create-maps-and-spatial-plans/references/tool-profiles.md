# 地图与空间规划工具能力路由

本文件的九节是“能力路由”，不是九个 Tool Registry profile。每节列出可单独核验的 atomic candidate id；项目必须为每个 concern 选择一个 candidate，只有单个工具确实缺少另一项必要能力时才组成最小工具集，并在 authority graph 中逐项记录 owner 与派生边。

这些 ID 只是领域请求 Registry 解析的稳定键，不证明 profile 已登记、工具可用或项目已使用。G2 必须读取 Registry 中每个 atomic profile 的 availability、version、license、environment probe、native model 和 master format；未登记写 `considered` 并返回协调，商业/GUI/未安装工具写 `unavailable` 或 `human_handoff`。

只有依赖/安装、版本、许可、真实调用、可编辑母版、重建/导出和当前 consumer/pixel 七类证据齐全，某个 atomic candidate 才能记为 `used`。替代工具不可共用一条 usage record；选中谁，就继承谁的原生对象、样式、工程与导出，其他候选保持 `considered` 或 `rejected`。

## TP1 桌面 GIS 分析与制图

- **任务子型**：`reference`、`thematic`、`service_area`、中小尺度 `site_plan`。
- **需要能力**：数据导入、CRS、空间处理、规则符号、label engine、图例、布局和矢量/栅格导出。
- **Atomic candidate ids**：`qgis-desktop`、`arcgis-pro`。

| Atomic id | 原生语言/数据模型 | 可拥有的 authority concern 与母版 |
| --- | --- | --- |
| `qgis-desktop` | QGIS project、layer/source、CRS、processing model、QML/rule-based symbology、label rule、map theme、Print Layout/Atlas | `cartography`：`.qgz/.qgs`；若项目内数据实际可编辑，可由 GeoPackage/数据库另任 `spatial_master` owner |
| `arcgis-pro` | APRX、map/layer、geodatabase、geoprocessing history/model、symbology/label class、layout/map series | `cartography`：`.aprx`；`spatial_master`：file/enterprise geodatabase 或明确外部数据库 |

- **为何选**：需要在一个可重开的桌面 GIS 中闭合空间检查、制图和固定版人工判断。项目按已有权威数据、许可证、协作生态和所需算法选择其中一个，不把二者写成同一 profile。
- **何时不选**：服务级动态路由、复杂 Web 运行时或权威 BIM/CAD 施工模型；也不为“显得专业”强加 GUI。
- **替代**：TP2 的 headless 分析加 TP6/TP9 制图；若选替代组合，每个 concern 分别登记 owner。
- **真实使用证据**：所选 atomic id 的安装/席位、版本/许可、真实 project 操作、图层/CRS/样式/布局清单、可重开母版、export invocation/hash 和最终像素；未选工具无权继承证据。

## TP2 可复现地理处理与空间 QA

- **任务子型**：所有需要批量投影、裁剪、叠加、概化、几何修复、空间连接或自动 QA 的子型。
- **需要能力**：CRS-aware I/O、几何/栅格变换、空间索引、可测试计算和确定性导出。
- **Atomic candidate ids**：`gdal-cli`、`geopandas-shapely`、`grass-gis`。

| Atomic id | 原生语言/数据模型 | 可拥有的 authority concern 与母版 |
| --- | --- | --- |
| `gdal-cli` | GDAL/OGR dataset、driver、VRT、SRS/coordinate operation、CLI option graph | `transform_pipeline`：锁定命令/参数 + VRT/脚本；输出 dataset 只在 manifest 明确时拥有派生数据 concern |
| `geopandas-shapely` | Python source、GeoDataFrame/GeoSeries、geometry、CRS、spatial join/predicate、GeoPackage/GeoParquet I/O | `analysis_pipeline`：代码/lockfile/参数；`analysis_result`：版本化空间表 |
| `grass-gis` | location/mapset、region、raster/vector topology、module invocation 和 computational region | `analysis_pipeline`：GRASS project/mapset + module history；结果 map 拥有声明的分析 concern |

- **为何选**：需要批量、复算、自动检查或代码审计时，按数据类型和算法选择一个；只有同一项目同时需要互补能力时才选最小组合，并记录单向调用。
- **何时不选**：精细标签、公共地图样式、印刷版式或交互是主要工作；这三个 candidate 都不能凭分析输出冒充视觉母版。
- **替代**：`qgis-desktop` 或 `arcgis-pro` 的原生 processing；数据库任务转 TP3。
- **真实使用证据**：每个实际 candidate 独立记录依赖、版本/许可、真实 CLI/import/module、输入输出 hash、CRS/几何断言、可重跑命令和下游消费者映射。

## TP3 持久空间数据库与规划分析

- **任务子型**：长期维护的 `thematic`、`service_area`、地块/分区 `site_plan` 和多人空间工作流。
- **需要能力**：空间表、SRID、约束、事务、拓扑/网络查询、情景版本和受控导出。
- **Atomic candidate ids**：`postgis`、`pgrouting`、`grass-gis`。

| Atomic id | 原生语言/数据模型 | 可拥有的 authority concern 与母版 |
| --- | --- | --- |
| `postgis` | PostgreSQL schema/table、geometry/geography、SRID、constraint、topology、SQL view/function、transaction/migration | `spatial_master`：database schema + migration + snapshot；查询结果默认派生 |
| `pgrouting` | PostGIS edge/vertex SQL contract、cost/reverse_cost、routing function 和 result set | `network_analysis`：SQL/profile/query record；依赖 `postgis` 拥有 edge master，不能单独冒充数据库 owner |
| `grass-gis` | location/mapset、topological vector/raster、region 和空间分析 module | `planning_analysis`：mapset + module history；不拥有外部数据库源事实 |

- **为何选**：多人维护、长期更新和多个消费者需要共享空间主库时选 `postgis`；只有确需数据库内网络算法才增加 `pgrouting`；复杂栅格/地形分析才增加 `grass-gis`。
- **何时不选**：一次性小范围交付，数据库运维超过收益；`pgrouting` 不能脱离已验证的 PostGIS edge schema 使用。
- **替代**：GeoPackage + `qgis-desktop`、`arcgis-pro` geodatabase 或 TP2 脚本 pipeline。
- **真实使用证据**：实际选中 atomic id 的服务/模块版本与许可、DDL/migration、真实 SQL/module invocation、数据 snapshot、拓扑/查询 proof、dump/export rebuild 和消费者证据。

## TP4 有限范围路网与路线分析

- **任务子型**：冻结路网的小范围 `route_network`、研究性 `service_area` 和离线可审计原型。
- **需要能力**：图获取/导入、最近 edge、connector、稳定 edge ID、有向路径、cost 和 route sequence。
- **Atomic candidate ids**：`osmnx-networkx`、`pgrouting`、`qgis-desktop`。

| Atomic id | 原生语言/数据模型 | 可拥有的 authority concern 与母版 |
| --- | --- | --- |
| `osmnx-networkx` | NetworkX MultiDiGraph、OSMnx node/`(u,v,key)` edge、GraphML、edge attributes、route node/edge sequence | `network_snapshot`：GraphML/hash；`route_analysis`：connector + ordered edge/cost ledger |
| `pgrouting` | PostGIS edge/vertex table 与 SQL routing result | `route_analysis`：query/profile/result；`postgis` 必须另任 network data owner |
| `qgis-desktop` | QGIS network layer、processing algorithm 参数、unroutable output 和 project | 只有真实使用 Network Analysis 时可拥有 `route_analysis`；`.qgz` 记录参数与制图，不能靠成图证明算法 |

- **为何选**：单机透明图审计通常选 `osmnx-networkx`；已有 PostGIS 网络选 `pgrouting`；需要 GUI 人工复核且算法满足合同可选 `qgis-desktop`。每次只能有一个 route analysis owner。
- **何时不选**：实时、多模式大区域、生产导航、室内网络或高保证无障碍；开放路网不能替代现场事实。
- **替代**：TP5 服务引擎，或用户提供的权威 route ledger；替代后显示层仍不得创造 edge。
- **真实使用证据**：所选 candidate 的图构建/导入、graph/data hash、connector candidates、ordered edge、cost/restriction、不可路由样例、独立复算和显示 segment 映射。

## TP5 服务级路由与多模式成本

- **任务子型**：大区域或动态 `route_network`、矩阵、等时区、多模式、定制 costing 和导航指令。
- **需要能力**：路由图构建、location correlation、turn restriction、profile、route/isochrone/matrix、maneuver 与服务接口。
- **Atomic candidate ids**：`valhalla-routing`、`graphhopper`、`osrm`、`pgrouting`。

| Atomic id | 原生语言/数据模型 | 可拥有的 authority concern 与母版 |
| --- | --- | --- |
| `valhalla-routing` | graph tiles、build config、costing JSON、correlated location、trip legs/shape/maneuver | `routing_graph/profile`：tiles + config；`route_analysis`：hash-bound response lineage |
| `graphhopper` | graph cache、encoding/profile/custom model、route/isochrone response 与 instruction | `routing_graph/profile`：graph + config/custom model；结果为分析派生 |
| `osrm` | contracted graph、Lua profile、extract/partition/customize artifacts、route/table/match response | `routing_graph/profile`：dataset build + Lua profile；结果为分析派生 |
| `pgrouting` | PostGIS edge schema、SQL cost 与 route result | 仅在项目另有受控服务层时拥有 `route_analysis`；不自动拥有 HTTP/运行时合同 |

- **为何选**：根据 mode、custom costing、turn restriction、更新方式、离线/服务部署和组织运维能力选择一个，不把四个引擎混成“service routing”使用记录。
- **何时不选**：少量静态路线、部署或数据许可不可承担；引擎成功也不证明入口、现场或无障碍。
- **替代**：TP4，或合规商业 API 的独立 Registry atomic profile；未登记 API 不可借本节名义使用。
- **真实使用证据**：实际引擎安装/版本/许可、真实数据构图、config/profile、请求响应、edge/shape/maneuver 映射、确定性复放、错误/不可路由和运行 proof。

## TP6 Web 地图原生制图

- **任务子型**：需要缩放、查询、筛选、路线/楼层/时间状态的 Web 空间交付。
- **需要能力**：地图 source、分层样式、尺度依赖表达、标签/要素状态、相机、查询和依赖合同。
- **Atomic candidate ids**：`maplibre-gl-js`、`openlayers`、`leaflet`。

| Atomic id | 原生语言/数据模型 | 可拥有的 authority concern 与母版 |
| --- | --- | --- |
| `maplibre-gl-js` | Style Spec source/layer/expression、sprite/glyph、camera、feature-state 与 Map instance | `web_cartography`：style JSON + map config；上游 tile/GeoJSON 仍有自己的 spatial owner |
| `openlayers` | source/layer/view/projection/style/interaction/event object graph | `web_cartography`：versioned source/layer/style/view config，适合复杂投影或编辑 |
| `leaflet` | map/layer/control/event、tile/GeoJSON layer 与 plugin contract | `web_cartography`：轻量 layer/style config；只在简单地图能力足够时选择 |

下列能力只在 TP6 主地图引擎确实需要时增加，并分别记录职责：

| Atomic id | 原生语言/数据模型 | 可拥有的 authority concern 与母版 |
| --- | --- | --- |
| `maptiler-map-style` | GL Style、MapStyle variant、sprite/glyph、vector/raster tile source 与 Map Designer 项目 | `cartographic_style`：版本化 style JSON 与资源；在线服务、API key 和数据许可另行记录 |
| `carto-basemap-style` | CARTO/MapLibre 兼容 style、palette、label 与 basemap source | `cartographic_style`：被允许并固定的 style/source 配置；不能凭远程 URL 声称离线 |
| `pmtiles` | PMTiles archive、header/directory、tile source 与 MapLibre protocol | `tile_distribution`：归档与 manifest；不拥有上游空间事实或制图语义 |
| `deck-gl` | Layer、view、accessor、GPU aggregation 与 picking state | `spatial_overlay_rendering`：版本化 layer config；底图、空间分析和产品 UI 仍由各自 owner 掌握 |

成熟 Style 是制图起点，不是免设计通行证。选择时查看真实目标尺度和任务图层，删除与任务竞争的
底图信息，并把标签、主对象、风险、未知和产品视觉 token 调整为一个系统。离线交付必须本地固定
style、sprite、glyph、字体、tile/GeoJSON 和 worker；仅能在线访问的漂亮样张不能进入离线候选。

- **为何选**：MapLibre 适合 style-spec 与矢量瓦片；OpenLayers 适合复杂投影/编辑；Leaflet 适合简单栅格、点和小型 GeoJSON。项目选择满足合同的最小一个。
- **何时不选**：固定印刷母版、精确分析或权威 CAD/BIM；不得用 DOM/SVG 重写引擎已有图层并称同一 profile。
- **替代**：TP1/TP9 静态输出；三维地球等能力需要另建具体 atomic profile，不能塞入本路由。
- **真实使用证据**：所选库的依赖/版本/许可、真实 map instance、source→layer/style、资源许可与请求轨迹、代表 zoom/state、交互/离线 proof 和当前像素。

## TP7 现场空间采集与事实复核

- **任务子型**：入口、门、台阶、坡道、路面、封闭、设备和场地状态的实地记录。
- **需要能力**：离线项目、表单、位置精度、附件、时间/观察者、stable feature ID、同步和冲突。
- **Atomic candidate ids**：`qfield`、`arcgis-field-maps`、`mergin-maps`。

| Atomic id | 原生语言/数据模型 | 可拥有的 authority concern 与母版 |
| --- | --- | --- |
| `qfield` | QGIS project/layer/form/relation、offline edit、attachment、GNSS metadata 与 change log | `field_observation`：QField package/GeoPackage；无现成工程时最小组合还需 `qgis-desktop` 负责项目配置 |
| `arcgis-field-maps` | Web map、feature layer、form、offline area、attachment、location metadata 与 sync | `field_observation`：feature service/geodatabase；Web map 只拥有采集配置 |
| `mergin-maps` | QGIS project、GeoPackage、form、sync project/version/change history | `field_observation`：同步项目/GeoPackage；通常与 `qgis-desktop` 组成明确最小组合 |

- **为何选**：按组织数据生态、离线方式、表单、同步、设备和许可选择一个采集系统；已有 master 决定 owner 连接方式。
- **何时不选**：无法真实到场、需要有资质测量/法定检查，或只有合成数据；不能生成虚假现场 proof。
- **替代**：组织已登记的 field GIS atomic profile，或受控人工表单 `human_handoff` 后由权威 owner 导入。
- **真实使用证据**：实际应用/服务版本与许可、真实项目/表单调用、设备观测、精度/时间/附件、sync/handoff、冲突处置、回写 master 和当前地图映射。

## TP8 室内、场地与 BIM/CAD 空间母版

- **任务子型**：`indoor_level`、需要权威建筑/场地几何的 `site_plan`，以及室内 `route_network`。
- **需要能力**：坐标控制、site/building/storey/space、墙/门/走廊、level、尺寸、revision、portal 和 vertical transition。
- **Atomic candidate ids**：`revit-bim`、`ifcopenshell`、`autocad`、`arcgis-indoors`。

| Atomic id | 原生语言/数据模型 | 可拥有的 authority concern 与母版 |
| --- | --- | --- |
| `revit-bim` | RVT project、level/grid、family/element、room/space、door、shared coordinate、view/sheet | `building_geometry`：RVT；只有真实编辑/导出证据时可拥有该 concern |
| `ifcopenshell` | IFC entity/relationship、placement、property set、geometry iterator 与 scripted transform | `ifc_transform/qa`：代码 + IFC input/output；不因读取 IFC 自动成为上游 building owner |
| `autocad` | DWG/DXF、layer、block、unit、coordinate、dimension、layout/xref | `site_or_plan_geometry`：DWG/DXF；layout concern 可独立分配 |
| `arcgis-indoors` | site/facility/level/unit/detail、floor-aware layer、pathway/transition、network dataset | `indoor_gis/network`：Indoors workspace；BIM/CAD 源几何仍由其原生 owner 掌握 |

- **为何选**：已有 RVT 选 Revit；已有受控 DWG/DXF 选 AutoCAD；需要可复现 IFC 解析/QA 选 IfcOpenShell；需要室内 GIS/导航选 ArcGIS Indoors。跨模型时只组合必要 atomic tools并记录导入边。
- **何时不选**：普通室外地图、只有未经核验的截图、无席位却声称 GUI 使用；IfcOpenShell 不能冒充 BIM 人工审查。
- **替代**：Registry 中另行核验的 Archicad、BricsCAD、FreeCAD/BIM 或室内 GIS atomic profile；未登记名称只能 considered/handoff。
- **真实使用证据**：每个所选 candidate 的安装/席位/版本/许可、真实 import/GUI/CLI、可编辑 level/door/portal/transition、坐标/revision、重开、导出、拓扑和最终消费者证据。

## TP9 印刷地图与固定版生产

- **任务子型**：需要独立纸张、地图册、手持导向图或固定安装版的任意空间子型。
- **需要能力**：物理页面、地图框、比例、grid、legend、north arrow、字体、矢量和 PDF/SVG preflight。
- **Atomic candidate ids**：`qgis-desktop`、`arcgis-pro`、`autocad`。

| Atomic id | 原生语言/数据模型 | 可拥有的 authority concern 与母版 |
| --- | --- | --- |
| `qgis-desktop` | Print Layout/Atlas、map item、extent/scale、legend、scale bar、template/export setting | `print_layout`：`.qgz/.qpt`；地图数据与分析仍由各自 owner 掌握 |
| `arcgis-pro` | layout/map frame、map series、legend/scale bar、page/export setting | `print_layout`：`.aprx` layout；geodatabase concern 分开登记 |
| `autocad` | paper/model space、viewport、plot style、layout/page setup、xref | 技术场地/平面交付可拥有 `print_layout`；不得重画上游 GIS claim 后仍称同一事实 |

- **为何选**：地图型印刷在 QGIS/ArcGIS 中选择一个；技术场地平面且 DWG 已是 geometry owner 时可选择 AutoCAD layout。项目不为同一 print concern 同时选择多个 owner。
- **何时不选**：未要求打印/安装，或只有 Web；工具不可用时使用 `human_handoff`，不得模拟 GUI。
- **替代**：另行登记并核验的专业排版 atomic profile只能拥有非空间版式 concern，必须放置锁定且可追溯的地图派生。
- **真实使用证据**：所选工具/版本/许可、真实 layout/page setup、纸张/比例/字体设置、可编辑母版、export/preflight、PDF MediaBox/字体/矢量/hash 和物理/安装语境 proof。

## 最小组合与 pipeline id

项目可选择多个 atomic candidates，但必须先列缺失能力，再给每个 concern 唯一 owner，并记录 `atomic A master → invocation/import → atomic B master → derivative`。例如 `osmnx-networkx` 可拥有 route ledger，`qgis-desktop` 可拥有 cartography/print；二者的临时组合仍是两个 atomic tool-use records，不自动获得 pipeline id。

本版本不保留可选固定 pipeline ID。以后只有共享 Registry 明确冻结组件 ID、版本约束、步骤顺序、输入输出、每步 authority 和 rebuild evidence 时，才能增加具体 pipeline profile；领域 Skill 不在任务中临时命名。

## 自定义边界

只有候选在可运行条件下对当前能力 probe 为 `fail` 才可自定义。记录 candidate id、输入、执行方法、观察结果、阻断 gap、最小自定义 owner、禁止重造的原生能力，以及数据、交互、视觉和导出一致性证据；`unavailable` 或 `inconclusive` 只返回其他 atomic candidate 或 `human_handoff`。
