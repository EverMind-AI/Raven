# 品牌工具包文件夹骨架（示例级）

工具包是一个文件夹，不是一张图。它装的是这套品牌的方向与工具，让用户看懂并能让设计师接着做成
正式生产文件；它本身不是印刷厂文件。默认采用下面的全面包（`full`）骨架，即使用户没列具体触点或
没点名工具包项目也尽可能完整交付；未列触点时选择数字、印刷、空间三种情境的代表应用。
只有用户或主代理明确要求基础版或限定当前任务的标志、社媒、包装范围时，才按对应档位裁剪；窄 `edit` 只修改受影响部分。
`logo/ color/ type/ voice/ README.md` 与 `examples/` 定调帧在任何档位都存在。路径相对
`submission/brand-kit/`。

```text
brand-kit/
├── README.md                 档位、每个文件给谁看、这套方向的一句话、正式生产时由设计师按此制作
├── examples/                 全部示例帧原图：定调帧 + 每个触点一帧 + 三种情境；像真实照片或网页截图
│   ├── 01-hero.png           定调帧
│   ├── 02-<touchpoint>.png   …
│   └── contact-sheet.jpg     全部帧并排缩略（一致性目检用）
├── logo/
│   ├── concepts/             2–3 个方向的概念图（生成）
│   ├── primary.svg|png       选定方向的干净版（横版）
│   ├── stacked.png           竖版
│   ├── mark.png              纯图形标（若有）
│   ├── mono-black.png        单色
│   └── mono-white.png        反白
├── color/
│   ├── palette.md            每色：名称、角色、HEX；深浅背景下的文字色
│   ├── palette.png           色表一页（色块 + 数值）
│   └── tokens.css            同一组色值的 CSS 变量（HTML 类帧与指南只引用它）
├── type/
│   ├── type-spec.md          标题字 / 正文字的字面、字重关系、字号层级、中西文配对与理由
│   └── specimen.png          一页字体样张，用真实品牌文案
├── imagery/
│   ├── style.md              图像调子一句话 + 光线 / 背景 / 道具 / 裁切 / 人物规则；生成 prompt 骨架
│   ├── samples/              3–6 张调子样张
│   └── elements/             图案或表现型图标（只在示例里有去处时存在）
├── voice/
│   └── messaging.md          使命一句、价值主张一句、标语一句、语气三词、样句 3 条、禁用说法
├── applications/             示例帧按触点归档：每个触点一个目录，放该触点的帧与尺寸说明
└── guide.pdf                 ≤ 12 页：系统一页、标志、色、字、图像、语气、应用样张、禁忌
```

## 各档位裁剪

| 档位 | 示例帧 | 必须存在 | 可省略 |
| --- | --- | --- | --- |
| `logo_kit` | 定调帧 + 标志在两种载体上 | `logo/ color/ type/ README.md examples/` | `imagery/ voice/ applications/ guide.pdf` |
| `basic` | 定调帧 + 数字、印刷各一帧 | `logo/ color/ type/ voice/ imagery/ examples/`、≤ 6 页指南 | `imagery/elements/`、`applications/` 可只归档两帧 |
| `full` | 定调帧 + 用户每个触点一帧 + 补足三种情境 | 全部 | 无 |
| `social_kit` | 定调帧 + 头像、封面、帖子三帧 | `basic` + 社媒三类 | 其他情境 |
| `packaging_kit` | 定调帧 + 包装正面、货架、开箱三帧 | `basic` + 包装三类 | 其他情境 |

## 同一个世界的规则

- 色值只在 `color/palette.md` 定义一次；HTML 类帧与指南只引用 `tokens.css`。
- 标志在所有帧里是同一个形状；生成场景里的标志若变形，用干净版叠加替换。
- 所有场景帧共享 `imagery/style.md` 的 prompt 骨架与"世界设定"，并以定调帧或上一帧作参考图。
- `examples/` 里每个可见元素都能指向工具包里的一项；工具包里每项工具至少在一帧示例里被用到。
- README 的清单必须与文件夹实际内容一致。
