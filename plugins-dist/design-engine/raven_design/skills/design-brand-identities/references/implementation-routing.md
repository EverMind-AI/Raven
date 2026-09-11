# 实现技术路由：每个交付物用什么做、怎么验

我们交付的是以假乱真的示例与方向，不是印刷厂文件。主生产工具是 `image_generate`，标志、产品与文字实体进场景靠
`image_generate(images=[底图, 标志 PNG, 参考帧])` 多图融合；HTML/CSS 加 `render_file` 负责排版类的帧与指南；Inkscape、
librsvg 用于处理官方母版，或从既有来源图、选定生成概念图描出干净的标志矢量，ImageMagick 只做裁切与格式转换，**不做叠加或透视贴图**。只列实验镜像里
真实存在的工具。写进 `BRAND-BRIEF.md` 时引用本表的行，不重新论证。

## 总表

| 交付物 | 主工具 | 做法 | 验证 |
| --- | --- | --- | --- |
| 既有标志／直接字形派生 | 官方母版或按三档阶梯提取、描摹 | 头像可从已有字形直接提取、等比缩放；不增造轮廓，不为次数重生成 | 来源对应、字形保持、小尺寸辨识 |
| 新增品牌符号概念 | 有品牌资产时用 `image_generate(images=[品牌资产])` 生成／编辑；无品牌资产时先文生图 | 按最小系统合同出 2–3 个方向，各一张干净背景图；保留已有标志 | 并排缩略比较结构、品牌关联与小尺寸辨识，不比颜色 |
| 标志干净版 | 从既有来源图或选定的生成概念图用 `potrace`（单色）或 `vtracer`（多色）描出矢量，Inkscape CLI 清理，标准字在 HTML/CSS 排 → `render_file` 出 PNG；命令见共享卡 `svg-and-vector.md` | 选定方向重建为清楚的横版、竖版、单色、反白 | 32 px 与 1024 px 都能认 |
| 定调帧（产品图 / 平铺 / 空间实景） | `image_generate` | prompt = 图像调子一句话 + 世界设定 + 触点描述 + 标志与文案描述；出图后检查标志与文字 | B7 与 B1–B4；近邻并排 |
| 后续场景帧（门店、包装、货架、开箱、人物） | `image_generate` + `images=[定调帧或上一帧]` | 参考图链保持同一光线、材质、色域；每帧只改触点与构图 | 全部帧 contact sheet 像同一家 |
| 排版类帧（官网首屏、社媒帖、名片、海报） | HTML/CSS（`tokens.css` + 选型字体）+ `render_file` 定尺寸出图 | 图像来自生成场景；排版帧里的标志与文字是活字与矢量资产，场景帧里的标志靠多图 edit 融合保证清晰 | 尺寸 = 平台规格；元素可溯源 |
| 生成图里标志或文字不合格 | 再做一次 `image_generate(images=[该帧, 标志 PNG])` edit 修正拼写与形状；仍错才允许一次登记进 `KIT-MANIFEST` 的最小叠加 | 用 `composite`/透视贴图把矢量标志压到场景上 | 重看 B7，重看两帧的光影是否一致 |
| 色表与 tokens | Python 脚本从 `palette.md` 生成 | `palette.png`、`tokens.css` | HEX 子集核对 |
| 字体建议与样张 | 有网时 npm `@fontsource/*` 或官方仓库下载；HTML 排样张 → 截图 | `type-spec.md` 写字面与理由 | 样张里字体确为所选 |
| 图像调子样张与图案 | `image_generate`，同一 prompt 骨架；派生用参考图模式 | 3–6 张样张 | 一句话可述 |
| 指南 | HTML 分页母版 → Chromium `page.pdf` | 同一 `tokens.css` 与字体 | 页数在档位内；`pdftoppm` 逐页目检 |
| README 与一致性 | 脚本枚举文件夹；HEX 子集核对；contact sheet | 清单与 `find` 一致 | 并排目检为最终裁决 |

## 命令样式

生成链（伪代码，字段按工具 schema）：

```text
image_generate(prompt = "<图像调子一句话>. <世界设定>. <触点与构图>. 标志 '<名称>' 清晰可读位于 <位置>, 文案 '<标语>' …", size=…)
image_generate(prompt = "<同一骨架>. 触点改为 <门店招牌 / 包装货架 / 开箱>", images=[examples/01-hero.png])
```

生成后必看两件事：标志形状是否与干净版一致、文字是否逐字正确；任一不合格就重生成或用 edit 修正，不解释。

标志干净版与导出：

```bash
rsvg-convert -w 1024 logo/primary.svg -o logo/primary.png
rsvg-convert -w 256  logo/mark.svg    -o logo/mark-256.png
```

排版类帧定尺寸出图（Raven `render_file`，成品从 bundle 的 `pages/` 取，bundle 留在 `proof/`）：

```text
render_file(path="submission/brand-kit/applications/web/hero.html", output_dir="proof/hero", viewport={"width":1440,"height":900}, scale=2, motion_mode="static", asset_root="submission")
cp proof/hero/*/pages/page-0001.png submission/brand-kit/examples/01-hero.png
render_file(path="submission/brand-kit/applications/social/post.html", output_dir="proof/post", viewport={"width":1080,"height":1080}, scale=2, motion_mode="static", asset_root="submission")
```

把标志融进生成场景（多图 edit，不贴图）：

```text
image_generate(prompt="Use the first image as the scene. Integrate the brand mark and wordmark from the second image onto the <载体，如 fascia / 布面 / 纸面> as a fabricated <材质> sign with real thickness, soft shadows and the same light as the scene. Keep the characters exactly as drawn, add no other text.", images=["assets/generated/storefront-base.png","logo/png/mark-wordmark-1600.png"], size="1536x1024", quality="high")
```

融合结果要看到：载体上的实体厚度与投影、与场景一致的光线、字形无错。融合后逐字核对；拼写错就再 edit 一次，
不许改用 `composite` / 透视贴图。

标志 / 图标从生成概念图描成矢量（镜像自带 `potrace` 与 `vtracer`；不用代码重建形状）：

```bash
# 1) 从概念板裁出选定方案，长边不足 1500 px 先放大
convert concepts.png -crop <W>x<H>+<X>+<Y> +repage -resize 1600x mark.png
# 2) 单色标志：二值化 → potrace（不二值化会把背景描成一块）
convert mark.png -colorspace Gray -threshold 50% mark.pbm
potrace mark.pbm --svg --alphamax 1.0 --turdsize 8 --opttolerance 0.2 -o mark-raw.svg
# 2') 多色图形：vtracer（python3 就是 /opt/raven 环境，已装）
python3 -c "import vtracer; vtracer.convert_image_to_svg_py('mark.png','mark-raw.svg', colormode='color', hierarchical='stacked', mode='spline', filter_speckle=8, corner_threshold=60, length_threshold=4.0, splice_threshold=45, path_precision=3)"
# 3) 清理：简化路径、导出 plain SVG；再手工设 viewBox、命名 <g>、删斑点
inkscape mark-raw.svg --export-plain-svg --export-filename=mark.svg   # 需要简化时先确认 `inkscape --action-list | grep path-simplify`，再加 --actions="select-all;path-simplify;export-do"
# 4) 32 px / 256 px / 印刷尺寸各看一次；换色改 fill，不重描
rsvg-convert -w 32 mark.svg -o /tmp/mark-32.png && rsvg-convert -w 256 mark.svg -o /tmp/mark-256.png
```

lineage 记进资产清单：概念图路径、裁切框、工具与参数。完整说明见 `$visual-artifact-design` 的 `references/svg-and-vector.md`「Vectorization tools」。

指南 PDF 与并排缩略：

```bash
python3 -c "…page.goto('file:///…/guide.html'); page.pdf(path='guide.pdf', prefer_css_page_size=True, print_background=True)"
pdfinfo guide.pdf | grep Pages; pdftoppm -r 40 -png guide.pdf /tmp/guide
montage examples/*.png -tile 4x -geometry 360x+8+8 examples/contact-sheet.jpg
```

一致性核对（终检必跑，结果贴进 BRIEF）：

```bash
grep -rhoE '#[0-9a-fA-F]{6}' brand-kit --include='*.css' --include='*.html' --include='*.md' --include='*.svg' | tr 'A-F' 'a-f' | sort -u > /tmp/used.txt
grep -oE '#[0-9a-fA-F]{6}' brand-kit/color/palette.md | tr 'A-F' 'a-f' | sort -u > /tmp/declared.txt
comm -23 /tmp/used.txt /tmp/declared.txt        # 必须为空
```

## 生产级附录（只在用户明确要求印刷厂或供应商可直接使用的文件时读取）

以下不是默认交付。用户要求生产文件时，在示例组之外另出：

- 字标转曲：`inkscape wordmark-src.svg --export-text-to-path --export-plain-svg --export-filename=logo/primary.svg`；
  `grep -c '<text' logo/primary.svg` 必须为 0；PDF：`inkscape logo/primary.svg --export-type=pdf --export-filename=logo/pdf/primary.pdf`。
- 字体文件与许可：下载 ttf 与 LICENSE 到 `type/fonts/`，`pyftsubset` 子集化，fonttools 读家族名核对。
- 印刷 PDF：HTML `@page { size: 96mm 60mm; margin: 0 }`（名片 90×54 + 3 mm 出血）→ Playwright `page.pdf(prefer_css_page_size=True)`
  → PyMuPDF `set_trimbox / set_bleedbox` → `gs -sDEVICE=pdfwrite -dPDFSETTINGS=/prepress -sColorConversionStrategy=CMYK -dProcessColorModel=/DeviceCMYK`
  → `pdfinfo` 页尺寸与 `pdffonts` 内嵌核对。
- 对比度：正文 ≥ 4.5:1，大字 ≥ 3:1，用相对亮度公式计算。
- 这些文件同样必须与示例组同一个世界；生产级文件不改变示例组的判据。
