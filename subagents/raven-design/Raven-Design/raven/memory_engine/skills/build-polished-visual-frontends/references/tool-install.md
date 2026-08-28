# 锁定工具运行手册

常用工具已经由实验镜像按哈希锁定并预装。“锁定”说明预装档位与正式构建入口，不代表一律
禁止联网外采。先服从当次运行环境的网络合同；用 `visual-worker-info --json` 查看正式 initializer、
构建入口和可用依赖模式，用 `visual-web-info --json` 查看 profile、设计系统与专业前端引擎。依赖模式只用：

- `locked`：项目只消费冻结 runtime 中已有的精确依赖；
- `project_lock`：环境允许出网且项目确需不同依赖图时，以项目完整、精确的 npm lockfile 在隔离
  临时目录执行 `npm ci`；lockfile integrity 与构建回执必须通过，第三方来源进入
  `THIRD_PARTY_NOTICES`，构建结束后清理，不把依赖目录放入交付物。

额外 npm 包只能在网络合同允许时进入项目精确 lockfile，再走 `project_lock`。模板 clone、字体和
其他外部资产同样服从网络与许可合同，但不冒充 npm 依赖模式；无网时不得假装取得，也不得访问宿主
仓库路径。手工复制依赖、建立链接或直接修改冻结 runtime 都不属于合法模式。

新建 Web 工程在取得视觉资产或开始页面实现前，先用 initializer 建一个临时空探针，并立即通过
正式 wrapper 构建：

```bash
visual-web-init <profile> runtime-probe [领域或设计系统选项]
visual-worker-build-web --dependency-mode locked runtime-probe
```

该探针只验证冻结底座，成功后删除，再建立实际工程。由 initializer 创建的工程必须使用
`visual-worker-build-web --dependency-mode <locked|project_lock> <target>`。`locked` 模式建立私有、
可写的冻结依赖解析视图；`project_lock` 模式要求项目已有完整精确 lockfile，在私有临时目录重建
该依赖图。两种模式都写入 `.vdlab/dependency-receipt.json`，成功或失败后清理项目链接、临时缓存
和依赖视图。直接执行底层
build、改缓存配置、复制 `node_modules` 或手工建立依赖链接，都不能用来绕过失败。

工具缺失或 profile 不覆盖需求时：有网且存在权威包时先建立项目 lockfile，使用 `project_lock`；
无网或外采仍不覆盖时，把原始错误与确切能力缺口记入 `DESIGN-BRIEF.md`，停止页面实现并修复 runtime
或改选有证据的 `implementation_base`。不得退回宿主 `Manual-Exp` 或 RavenX 路径，也不得让项目内
workaround 吸收 runtime 缺陷。

## 设计系统（选定一套后初始化）

| 系统 | initializer 选项 |
| --- | --- |
| Appica UI | `--design-system appica` |
| shadcn/ui | `--design-system shadcn` |
| Radix Themes | `--design-system radix-themes` |
| Mantine | `--design-system mantine` |
| Chakra UI | `--design-system chakra` |
| Material UI | `--design-system mui` |
| Ant Design | `--design-system ant-design` |
| Fluent 2 | `--design-system fluent` |
| Primer | `--design-system primer` |
| React Spectrum 2 | `--design-system spectrum` |
| React Aria Components | `--design-system react-aria` |
| Base UI | `--design-system base-ui` |

React 基座和所选系统的精确依赖由 initializer 从锁定合同写入 `package.json`。

## 专业引擎

| 能力 | 锁定初始化方式 |
| --- | --- |
| 数据图表 | `visual-web-init data submission --data-engine observable-plot|antv-g2|echarts|vega-lite` |
| 地图 | `visual-web-init maps submission --map-engine maplibre|leaflet` |
| 组件系统 | `visual-web-init components submission --design-system <id>` |
| 内容出版 | `visual-web-init content submission` |
| 产品界面 | `visual-web-init product submission --design-system <id>` |
| 交互图解 | `visual-web-init explainer submission` |
| 流程/节点图 | `visual-web-init diagrams submission` |
| 浏览器游戏 | `visual-web-init game submission` |

每个 profile 的包名、版本、engine id 与本地图版以 `visual-web-info --json` 为唯一权威来源，
不要从本文件猜版本。清单之外的能力只有镜像中已有可执行工具时才能采用；把实际命令与
验证结果记入 `DESIGN-BRIEF.md`，而不是写安装计划。
