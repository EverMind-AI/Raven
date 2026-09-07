# 海报交付骨架（成品平面稿 + 母版文件夹）

交付两样东西：**成品平面稿**（每个触点每个尺寸一张最终像素，像真的一样，无水印无占位）和**母版文件夹**（别人能改内容重出图）。
路径相对 `submission/`。全 campaign 档位的骨架如下，其他档位按表裁剪；`master/ output/ README.md` 与定调帧任何档位都存在。

```text
submission/
├── README.md                 档位、每个触点的尺寸与用途、如何重出图（一条命令）、字体与素材来源、《替换清单》（替身事实逐项）
├── output/                   成品平面稿：按触点分目录，文件名含尺寸；PNG 为主，印刷件另出 PDF
│   ├── hero/                 定调帧（主海报）300 dpi 原尺寸 + 2000 px 预览
│   ├── print/                印刷 PDF（render_file 的 document.pdf，含出血，位图 RGB）
│   ├── series/               系列各张
│   ├── social/               1:1 / 4:5 / 9:16 / 横版
│   ├── display/              易拉宝、展板、灯箱、大牌（按 mm 换算的像素）
│   └── contact-sheet.jpg     全部成品缩略并排（一致性目检用）
├── master/                   HTML/CSS 画板母版：一个比例一份 HTML，共用 tokens.css 与 fonts/
│   ├── tokens.css            色、字、间距变量的唯一来源
│   ├── fonts/                实际使用的字体文件（含中文子集）
│   ├── hero.html             定调帧母版
│   ├── series.html + data.json   系列模板与内容插槽（每张一条记录）
│   ├── social-*.html         每个比例一份
│   ├── sizes.json            每份 HTML 的画板 px 与 scale（render_file 与 render.mjs 共用）
│   └── render.mjs            接收方重出图脚本（Node Playwright）：填 data.json 写 build/，按 sizes.json 截图到 output/rebuild/
├── assets/                   素材层：生成的背景 / 主视觉 / 抠图，每张附 prompt.txt 与来源
│   ├── generated/
│   └── logos/                用户提供或干净重建的 logo（SVG/PNG）
└── spec.md                   一页规格：画板尺寸（mm 与 px）、出血、scale、字号层级、色值、信息层内容、系列不变项
```

工作区根目录（`submission/` 之外）的 `proof/` 存放每次 `render_file` 的 bundle（`<触点>-<阶段>/*/pages/ document.pdf preview/`），
探针帧、定调帧、终帧、contact sheet 都在这里留证；成品是从这些 bundle 拷贝或精确缩放出来的。

## 各档位裁剪

| 档位 | 成品平面稿 | 必须存在 | 可省略 |
| --- | --- | --- | --- |
| `single` | 定调帧 1 张（+ 用户点名的第二尺寸） | `output/hero/ master/hero.html tokens.css sizes.json spec.md README.md` | `series/ social/ display/` |
| `series` | 定调帧 + 系列 N 张（N 由用户或内容决定，≥ 3） | `single` + `master/series.html data.json render.mjs output/series/ contact-sheet` | `social/ display/` |
| `social_kit` | 定调帧 + 1:1、4:5、9:16 三帧（+ 横版封面若用户要） | `single` + `master/social-*.html output/social/` | `series/ display/` |
| `display_set` | 定调帧 + 用户列出的每种展示物各一张（按真实 mm） | `single` + `output/display/` + spec 里的尺寸换算表 | `series/ social/` |
| `campaign_full` | 以上全部 | 全部 | 无 |

## 同一套的规则

- 色值只在 `tokens.css` 定义一次；所有 HTML 只引用变量。
- 字体只在 `master/fonts/` 出现一次；spec 写字面与字重；标题字距、中西文间距在 CSS 里统一。
- 系列的不变项（画板、背景、图形元素、标题位置、信息层位置、logo 位置）写进 `spec.md`，`data.json` 只允许改可变项。
- `assets/generated/` 每张图附 prompt 与参考图链；成品里的每个像素都能指回 master 或 assets。
- `README.md` 的清单与文件夹实际内容一致；`node master/render.mjs` 能从 master 重出全部成品像素，agent 交付前至少跑一次核对尺寸。
