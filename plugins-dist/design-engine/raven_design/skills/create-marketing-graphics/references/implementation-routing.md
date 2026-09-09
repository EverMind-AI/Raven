# 实现技术路由：每一层用什么做、怎么出图、怎么验

基底是 **HTML/CSS 画板（按物理尺寸或平台像素写）+ Raven `render_file` 定尺寸出图 + `preview_file` 自检**。版面与活字只在 HTML
里排；SVG 只做 logo 与符号的资产格式，不做画板。Chromium 是运行时里
最强的排版引擎（真实字体、字距、网格、混合模式），Noto CJK、ImageMagick、rsvg、poppler 都在镜像里。`image_generate` 只产
**素材层**（背景、主视觉、抠图），从不产整张海报，从不画文字。写进 `POSTER-BRIEF.md` 时引用本表的行，不重新论证。

## 画板尺寸口径（先算好再写 CSS）

Chromium 固定 96 CSS px = 1 in = 72 pt。画板用 **px** 写，px 由 mm 换算并四舍五入（误差 < 0.1 mm）；印刷件把出血直接加进画板，
不画裁切线；`spec.md` 同时记 mm 与 px。任一边 ≤ 8192 CSS px 都走 `render_file`，超过才自建脚本。

| 触点 | 成品尺寸 | 画板 CSS px（含出血） | `scale` | 成品从哪来 |
| --- | --- | --- | --- | --- |
| A4 海报 210×297 mm + 3 mm 出血 | 216×303 mm | 816×1145 | 4 | `pages/page-0001.png` = 2551×3579（300 dpi）；`document.pdf` = 612×859 pt |
| A3 297×420 mm + 3 mm | 303×426 mm | 1145×1610 | 4 | `pages/` 300 dpi |
| A2 / A1 / A0、易拉宝 800×2000 mm、灯箱、大牌 | 按站点 mm | mm ÷ 25.4 × 96 | 2 | `pages/` 300 dpi（有效约 190 dpi，远看件足够） |
| 社媒 1:1 / 4:5 / 9:16 / 横版 | 1080×1080 / 1080×1350 / 1080×1920 / 1920×1080 | 与成品相同 | 2 | `pages/` 用 ImageMagick 精确缩到 2× 与 1× 各一张 |
| 微信长图 | 1080×N（N ≤ 8192） | 与成品相同 | 2 | 同上 |

## 总表

| 层 / 交付物 | 主工具 | 做法 | 验证 |
| --- | --- | --- | --- |
| ① 画板 | `.board{width:<px>;height:<px>;position:relative;overflow:hidden}`，`html,body{margin:0}` | 每个比例一份 HTML；`tokens.css` 共用；五层固定 z 序：`bg`（背景）→ `kv`（主视觉）→ `gfx`（图形元素）→ `type`（全部活字）→ `brand`（logo、二维码、主办栏） | 探针帧 `pages/` 像素 = 上表换算值 |
| ② 背景层 | 默认 `image_generate` 生成：独立生成满版场景 / 环境 / 材质，或 `image_generate(images=[主视觉])` 外延主视觉世界；纯色只在主视觉需要留白时用 | 主视觉定稿后再做背景：edit 输入主视觉，prompt 写"把这个场景向四周延展成满版背景，保持同一光源、材质与景深，主体区域留空"；纯色时用 `convert hero.png -resize 1x1\! txt:-` 取样再微调，BRIEF 写明关系 | 背景与主视觉同一世界；换题材底色随之变；无文字、无水印感 |
| ③ 主视觉层 | `image_generate` | 照片、插画、3D 都由它出；抠图：让它在**纯色平底**上生成主体，再 `convert in.png -fuzz 8% -transparent '#00ff00' out.png`；同一世界用 `images=[上一张]` 参考图链 | 主体清晰、边缘干净、光线与背景一致 |
| ④ 图形元素层 | 内联 SVG / CSS 形状 | 色块、椭圆、波纹、徽章、撕纸边用 SVG 画，系列里作为固定符号 | 各张形状相同 |
| ⑤ 文字与品牌层 | HTML 活字 + 本地字体文件 | 标题、副标、信息层、CTA 全是 `<h1>/<p>`；logo 用 SVG/PNG `<img>`；标题 `text-box: trim-both cap alphabetic` 贴网格，标题 `text-wrap: balance`、正文 `text-wrap: pretty` | 逐字正确、字号符合距离表 |
| 出图 | `render_file(path, output_dir="proof/<触点>-<阶段>", viewport={画板 px}, scale=上表, motion_mode="static", asset_root="submission")` | 成品位图一律从 bundle 的 `pages/page-0001.png` 取：印刷件原样，数字件 `convert -resize WxH!` 到规格；印刷 PDF 直接拷 `document.pdf` | `identify` 像素 = 规格；`pdfinfo` 页面 pt = mm × 72 / 25.4 |
| 自检 | `preview_file(path, viewport, scale=1)` | 看整体；看细节用 `read_file` 读 `pages/` 的裁片（`convert -crop`） | 字体已加载、无 tofu、无占位 |
| 系列 | `data.json` + `series.html` 模板 | `render.mjs` 读每条记录填插槽，写出 `master/build/<id>.html`；逐个 `render_file` | 并排缩略像一套；不变项 diff 为零 |
| 多尺寸 | 一比例一 HTML，共用 tokens | 派生时只允许重排位置、改图片裁切、改行长；字体字号关系与元素清单不动 | 元素清单一致 |
| contact sheet | `montage` | `montage output/**/*.png -tile 5x -geometry 320x+8+8 output/contact-sheet.jpg` | 目检一致性 |

### `render_file` 的事实（写 Skill 时已用真实 Chromium 探针验证）

- 流程：等字体加载 → 视口截图（`scale` 倍像素）→ Chromium 打印 PDF → 与截图比对。带渐变或中文文字的海报几乎必然像素差 > 1%，
  此时工具**静默**改用截图位图重建 `document.pdf`，页面尺寸仍等于画板 px × 0.75 pt。所以 `document.pdf` 对海报是位图 PDF，真实分辨率
  由 `scale` 决定，不要向用户承诺矢量。
- `pages/` 按 300 dpi 栅格化，像素 = 画板 px × 3.125；印刷件用 `scale=4` 才是真 300 dpi，`scale=2` 有效约 190 dpi。
- 工具断网：`<link>` 到 Google Fonts 会被拦且不报错，字体只用 `master/fonts/` 与镜像内 Noto CJK。所有资源必须在 `asset_root` 内。
- `motion_mode` 一律 `static`；`viewport` 必须等于画板 px，否则会截到 body 底色。
- 视口任一边上限 8192 CSS px；`视口 × scale ≤ 16384`。超出才自建 Playwright 脚本，并在 BRIEF 写明原因。

## 命令样式

素材层生成（伪代码，字段按工具 schema）：

```text
image_generate(prompt="<图像调子一句话>. <世界设定>. <主体与构图>, no text, no letters, no watermark", size="1024x1536", quality="high")
image_generate(prompt="<同一骨架>. 主体改为 <…>, plain solid green background #00ff00, studio light", images=[assets/generated/hero-bg.png])
convert assets/generated/subject.png -fuzz 8% -transparent '#00ff00' assets/generated/subject-cut.png
```

生成后必看两件事：画面里有没有冒出文字或水印（有则重生成或裁掉），主体边缘是否干净。

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

出图与取成品：

```text
render_file(path="submission/master/hero.html", output_dir="proof/hero-final", viewport={"width":816,"height":1145}, scale=4, motion_mode="static", asset_root="submission")
cp proof/hero-final/*/pages/page-0001.png submission/output/hero/hero-A4-2551x3579.png
cp proof/hero-final/*/document.pdf        submission/output/print/hero-A4-bleed3mm.pdf
render_file(path="submission/master/social-4x5.html", output_dir="proof/social-4x5", viewport={"width":1080,"height":1350}, scale=2, motion_mode="static", asset_root="submission")
convert proof/social-4x5/*/pages/page-0001.png -resize 2160x2700! submission/output/social/post-4x5-2160x2700.png
convert proof/social-4x5/*/pages/page-0001.png -resize 1080x1350! submission/output/social/post-4x5-1080x1350.png
identify submission/output/**/*.png; pdfinfo submission/output/print/*.pdf | grep 'Page size'
```

接收方重出图（`master/render.mjs`，Node Playwright，镜像内 `NODE_PATH` 已含 playwright，Chromium 在 `/usr/bin/chromium`）：

```js
import { chromium } from "playwright"; import { readFileSync } from "node:fs";
const sizes = JSON.parse(readFileSync("master/sizes.json"));           // {"hero.html":{"w":816,"h":1145,"scale":4}, ...}
const browser = await chromium.launch({ executablePath: "/usr/bin/chromium" });
for (const [file, s] of Object.entries(sizes)) {
  const page = await (await browser.newContext({ viewport: { width: s.w, height: s.h }, deviceScaleFactor: s.scale })).newPage();
  await page.goto(`file://${process.cwd()}/master/${file}`); await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `output/rebuild/${file.replace(".html", "")}-${s.w * s.scale}x${s.h * s.scale}.png` });
}
await browser.close();
```

系列：`render.mjs` 先读 `data.json` 把每条记录填进 `series.html` 写出 `master/build/<id>.html`，再按上面循环出图；agent 在实验环境
对每个 `build/<id>.html` 调 `render_file`，两条路出的像素应一致。

字体：离线用镜像内 `/usr/share/fonts/opentype/noto/NotoSansCJK-*.ttc`，用 `pyftsubset` 出中文子集放进 `master/fonts/`，`@font-face`
用相对路径；有网时 `npm i @fontsource/noto-sans-sc @fontsource/noto-serif-sc lxgw-wenkai-webfont @fontsource/inter` 后仍复制进
`master/fonts/`，不留 CDN `<link>`。中文排版 CSS 底线：中西文间距 0.25em、标题 `letter-spacing: -0.02em`、标点悬挂
`hanging-punctuation`、数字与英文用拉丁字体、竖排用 `writing-mode: vertical-rl`。

一致性核对（终检必跑，结果贴进 BRIEF）：

```bash
grep -rhoE '#[0-9a-fA-F]{6}' master --include='*.css' --include='*.html' | tr 'A-F' 'a-f' | sort -u > /tmp/used.txt
grep -oE '#[0-9a-fA-F]{6}' master/tokens.css | tr 'A-F' 'a-f' | sort -u > /tmp/declared.txt
comm -23 /tmp/used.txt /tmp/declared.txt        # 必须为空
```

## 生产级附录（只在用户明确要求印刷厂或平台投放文件时读取）

- 印刷位图：`document.pdf` 已是含出血的正确尺寸位图 PDF（RGB）。要 CMYK 时 `gs -sDEVICE=pdfwrite -dPDFSETTINGS=/prepress
  -sColorConversionStrategy=CMYK -dProcessColorModel=/DeviceCMYK`；要 TrimBox 用 PyMuPDF `set_trimbox`；`pdfinfo` 核对页面尺寸。
- 印刷矢量（用户要 CMYK 矢量文字时）：同一份 `master/*.html` 用 Playwright `page.pdf(width/height=画板 px, print_background=True,
  margin=0)` 出矢量 PDF，再用上面的 Ghostscript 命令转 CMYK。验收：`pdfinfo` 页面尺寸等于规格，`pdffonts` 列出嵌入字体，文字可提取，内容流无 DeviceRGB 算子。带 `backdrop-filter` / `mix-blend-mode` 的层会让 Chromium 打印结果偏离
  屏幕，印刷矢量版避开这些效果或接受位图版。不承诺 PDF/X-4。这条路线不构成改用 SVG 排版面的理由。
- 数字广告位：按平台带日期的规格页出精确像素与文件大小上限；安全区内放标题与 CTA。
- 字体许可：商用字体写明许可来源；思源、霞鹜文楷、Inter 可商用。
- 这些文件同样必须与成品平面稿同一套；生产级文件不改变判据。
