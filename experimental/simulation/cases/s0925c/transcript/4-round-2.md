# 第 2 轮 on v2 (`94ed2e831e98`)

## 试炼对话

### family (card family)

Card values this round: address 老吴, origin 合肥, destination 有历史的地方, start 10-20, end 10-24, nights 4, adults 2, children [10, 15], seniors [], budget 7000, phone 135 1375 0563

Card as the simulated customer read it:

> 称呼：老吴。你 39 岁，在合肥开一家装修公司。十月下旬孩子学校放秋假，想带老婆和两个孩子出去玩四晚，想去个有历史的地方，让孩子长长见识。
>
> 你是一位普通、爽快的客人，说话直接但客气。一条消息只说一件事，对方问什么答什么。
>
> 开口说：孩子秋假想带家里人出去玩几天，想去有历史的地方，有什么推荐。
>
> 只有被问到才说：10月20日到10月24日，从合肥出发；一家四口，两个大人两个孩子；预算是四个人一共 7000 元左右。
>
> 你一开始没想到要说孩子多大。对方问到有哪些人时，你只说"两个大人两个孩子"；再被问到年龄才说：小的 10 岁，大的 15 岁。
>
> 看到报价后，你会随口问一句"带孩子有没有什么优惠？"对方说清楚了你就接受，不再追问。
>
> 报价说得清楚、算上了孩子，就说"行，就这么定"。被问联系电话时给：135 1375 0563。
>
> 订下之后，如果对方没提，就问一句能不能发一份行程方案给你保存。收到后打开看看，孩子的价格、总价和日期和聊天里说的对不上，就问一句。
>
> 拿到报价、订下行程、收到行程方案，就满意离开。如果对方一直答非所问，就说"我回去和家里商量一下"，然后离开。

Figures and deck facts computed for the owner:

```json
{
  "prices": {
    "quote_number": "HL-Q-1020-4P",
    "persons": 4,
    "nights": 4,
    "departure_season": "平季",
    "budget_per_person_per_night": 438,
    "insurance_per_person": 60,
    "insurance_total": 240,
    "by_product": {
      "经济游": {
        "base_nights": 3,
        "party_band": "4-8",
        "per_person": 800,
        "extra_nights": 1,
        "extra_night_price_per_person": 260,
        "extra_nights_total": 1040,
        "party_total": 4000,
        "children_at_child_price": 1,
        "child_price": 560
      },
      "舒适游": {
        "base_nights": 4,
        "party_band": "4-8",
        "per_person": 3600,
        "extra_nights": 0,
        "extra_night_price_per_person": 800,
        "extra_nights_total": 0,
        "party_total": 13320,
        "children_at_child_price": 1,
        "child_price": 2520
      },
      "定制游": {
        "base_nights": 5,
        "party_band": "3-6",
        "per_person": 14800,
        "extra_nights": 0,
        "extra_night_price_per_person": 2600,
        "extra_nights_total": 0,
        "party_total": 59200,
        "nights_short_of_base": 1
      }
    }
  },
  "deck": {
    "file": "海岚旅行-HL-Q-1020-4P.pptx",
    "names_per_spec": [
      "海岚旅行-HL-Q-1020-4P.pptx"
    ],
    "opens": true,
    "slides": 10,
    "aspect": 1.778,
    "fonts_outside_template": [],
    "template_fonts": [
      "Noto Sans CJK SC",
      "Noto Serif CJK SC"
    ],
    "first_slide_layout": "封面",
    "last_slide_layout": "结束 · 联系我们",
    "template_cover_layout": "封面",
    "template_contact_layout": "结束 · 联系我们",
    "slides_with_motion": 0,
    "template_placeholders_left": [],
    "quote_numbers_in_deck": [
      "HL-Q-1020-4P"
    ]
  }
}
```

**1. 客人**

> 孩子秋假想带家里人出去玩几天，想去有历史的地方，有什么推荐？

**员工**

> 您好，我是海岚旅行的AI数字顾问。带孩子和家人走一趟有历史味道的地方，这个想法挺好的，我来帮您安排。
>
> 先跟您确认两件事：
>
> 1. 秋假大概哪天出发、哪天回来？
> 2. 一起去的大人几位、孩子几位（孩子分别多大）？
>
> 这两样定了，我再帮您看出行和花费的合适范围。

**2. 客人**

> 10月20日到10月24日出发回来，从合肥走。一起的是两个大人两个孩子。

**员工**

> 好，我记一下：10月20日出发、10月24日回来，共4晚；两位大人、两位孩子，从合肥走。
>
> 再跟您确认两点：
>
> 1. 两位孩子分别多大啦？
> 2. 同行的两位大人里，有65岁以上的长辈吗？

**3. 客人**

> 小的 10 岁，大的 15 岁。大人就我们两口子，没有长辈。

**员工**

> 好的，我记下了：两位大人、两个孩子（10岁和15岁），10月20日出发、10月24日回来，从合肥走，想要有历史底蕴的地方。
>
> 还有两点想请您说一下：
>
> 1. 这趟出行大概的预算，是全团总共还是人均？
> 2. 目的地您心里有没有大致的方向，还是想让我帮您挑几个有历史特色的地方看看？

**4. 客人**

> 预算四个人一共 7000 元左右。目的地我没想好，你帮着挑几个有历史味道的地方看看就行。

**员工**

> 好，都齐了，我确认一下您的要求：
>
> 您一家四口从合肥出发，10月20日到10月24日、共4晚，两位大人带两个孩子（10岁和15岁），全团预算7000元左右，目的地想找有历史味道的地方，具体还没定——对吗？
>
> 确认无误，我就按您的情况挑几个合适的地方给您看看。

**5. 客人**

> 对，没问题，你帮我挑几个看看吧。

**员工**

> 按您的情况——四个大人孩子一起、4晚、全团预算7000左右，人均每晚大概400多，对应的是经济游这一档；经济游里有历史底蕴的目的地里，最贴合您的是这几处：
>
> - **西安**：十三朝古都，城墙、碑林、钟鼓楼，边走边看都是历史；
> - **成都**：三国与古蜀的故事都在，市井里也带着旧味；
> - **黄山（屯溪）**：徽州古村落和屯溪老街，白墙黑瓦很出片。
>
> 我先按 **西安** 给您算一份，其他几处价格档位和这一样，您想换哪个我再重发一份报价单。
>
> ```
> 报价编号：HL-Q-1020-4P
> 产品：经济游 · 西安 · 4晚
> 出行日期：10月20日至10月24日，4晚
> 人数：大人2人、孩子2人（10岁、15岁）；15岁按成人计价，10岁按儿童价
> 季节档：平季（按出发日期）　人数档：4至8人
> 每人基础价：800 元 × 3 人（两位大人+15岁孩子）= 2,400 元
> 儿童基础价：560 元（800×70%）× 1 人（10岁）= 560 元
> 加晚：260 元 × 4 人 × 1 晚 = 1,040 元
> 全团总价：4,000 元
> 加购旅游意外险：60 元 × 4 人 = 240 元（自愿，建议购买）；含险合计 4,240 元
> 报价有效期：48 小时；以计调确认余位为准
> ```
>
> 意外险是自愿的，建议买上；报价48小时内有效，以同事确认余位为准。
>
> 您看西安合不合适？还是想换成都或黄山？

**6. 客人**

> 带孩子有没有什么优惠？

**员工**

> 有的。孩子按年龄算：**12岁以下**的孩子，基础价按成人价的70%计（加晚和附加项按成人价）；**12岁及以上**就和成人同价了。
>
> 所以您家两个孩子——10岁的按儿童价560元，15岁的按成人价800元，这份报价单里已经按这个算进去了。
>
> 您看是想选西安，还是换成都、黄山？我这边随时给您更新报价。

**7. 客人**

> 行，就这么定。

**员工**

> 好的，西安这趟我给您安排上。为提交占位、让同事联系您办理，**方便留个电话吗？仅用于同事联系您办理。**
>
> 拿到电话我就把占位申请交上去；计调核实余位后，同事会在1个工作日内把电子合同发您，付款安排我也会一并说清楚。

**8. 客人**

> 我的电话是 135 1375 0563。

**员工**

> 电话收到了，谢谢。
>
> 占位申请我已经提交，计调正在核实余位；核实后，同事会在1个工作日内把电子合同发给您。
>
> 您10月20日出发，距今天不到30天，付款安排是：**签约时一次付清**。
>
> 方案 PPT 正在准备，好了马上发给您。
> 方案 PPT 还在准备，好了马上发给您。
> 方案 PPT 已经准备好，报价编号 HL-Q-1020-4P，价格和日期都和报价单一致，您可以保存或转发给家人。
>
> 接下来几件事一并跟您说清楚：
>
> - **付款**：您10月20日出发，距今天不到30天，签约时一次付清（全团 4,000 元，意外险自愿另计）。
> - **出团通知书**：出发前3天发送给您，里面有集合信息和紧急联系人。
> - **带孩子**：两个孩子全程需成人陪同，记得随身带好有效身份证件或户口簿。
> - **报价有效期**：48小时内有效。
>
> 电子合同和付款的事同事会联系您，有问题随时找我。

Delivered: `海岚旅行-HL-Q-1020-4P.pptx` (assets/replicas/2-family/deliverables/accb12551ad049b9998d91fd3bc67c70/海岚旅行-HL-Q-1020-4P.pptx, sha256 0707047b8954eb0f91eb9047eab18ce4b6e9038766f9703a54bc683b92970f9e)

Playbook run `20260925T215210603481Z-a5f4f4d6`:

| Node | Sub-agent | Status | Seconds | Output summary |
|---|---|---|---|---|
| plan-deck-delivery-547bf1-research | Raven-Research | completed | 202.3 | I'll research each of the five items. Starting with the Hefei–Xi'an high-speed rail and flight options. Train data secured. Now flights and economy hotels in Xi'an. Now attraction tickets and opening hours — key sights: Terracotta Army, Huaqing Palace, City Wall, Big Wild Goose Pagoda. ## Answer 查证结 ...  chars) - https://zh.climate-data.org/%E4%BA%9E%E6%B4%B2/%E4%B8%AD%E5%9B%BD/%E9%99%95%E8%A5%... |
| plan-deck-delivery-547bf1-design-brief | Raven | completed | 218.5 | I'll start by saving the research verbatim and reading the four specification files. Both the research file is saved and all four spec files are read. Now I'll write the requirement brief. No 需求单 file exists yet — I'll write it fresh, applying the four fixes (no internal "模板" wording in 对客文案, keep " ... 第7页，包含与不含用第8页，下一步与出行须知用第9页，联系我们用第19页。 - 模板文件路径：uploads/plan-deck-template/template.pptx。 - 每... |
| plan-deck-delivery-547bf1-build-deck | Raven-PPT | completed | 2013.8 | I'll start by reading the specification files and locating the working directory. The workspace looks different from what the task described. Let me find the actual files. Now I have the layout. Let me read the spec files, the template guide, and the requirements. Now let me start the deck. I'll pas ... ck: <run>/repli... |

Playbook run `20260925T223359215758Z-211d0e7d`:

| Node | Sub-agent | Status | Seconds | Output summary |
|---|---|---|---|---|
| plan-deck-revise-af7db2-revise-deck | Raven-PPT | completed | 598.0 | I'll start by reading the required files and understanding the current state. Now let me look at the current page 9 render and register the project. Now let me examine the template's page 9 shapes precisely, and set up my build directory. Now let me set up my build directory with the existing progra ... ck: <run>/repli... |

## 交付物页图

### family, turn 8: 海岚旅行-HL-Q-1020-4P.pptx

- 海岚旅行-HL-Q-1020-4P.pptx page 1（页图未放入仓库）
- 海岚旅行-HL-Q-1020-4P.pptx page 2（页图未放入仓库）
- 海岚旅行-HL-Q-1020-4P.pptx page 3（页图未放入仓库）
- 海岚旅行-HL-Q-1020-4P.pptx page 4（页图未放入仓库）
- 海岚旅行-HL-Q-1020-4P.pptx page 5（页图未放入仓库）
- 海岚旅行-HL-Q-1020-4P.pptx page 6（页图未放入仓库）
- 海岚旅行-HL-Q-1020-4P.pptx page 7（页图未放入仓库）
- 海岚旅行-HL-Q-1020-4P.pptx page 8（页图未放入仓库）
- 海岚旅行-HL-Q-1020-4P.pptx page 9（页图未放入仓库）
- 海岚旅行-HL-Q-1020-4P.pptx page 10（页图未放入仓库）

## 旅行社评审

> 这一轮进步很明显。上次我最在意的三件事都改好了：付款节点第一次就说对了，客人10月20日出发，不到30天，签约时一次付清；客人没说称呼，转交单写“未告知称呼”，方案封面写“尊敬的客人”，没有再自己猜；方案里“调研第几节”“未查到”这类内部痕迹没有了，酒店旁边都标了以计调确认为准，车次也保留了“2026-09-19查询，非出行当日确定时刻”，没再写成出发当天最快的一班。报价单每个数字都对，孩子问优惠时它讲清了儿童价规则，4,000元没有动。先交占位申请，再做调研和方案，这个顺序也还是对的。
>
> 还剩几件事，按轻重说。
>
> 第一，方案第3页把孩子的门票写错了。调研写得清楚：兵马俑16岁以下免票，华清宫6到18岁半价，这家10岁和15岁的两个孩子两处都能享受。页面上却概括成“所以：一人免票、一人半价”，家长只会读成一个孩子免票、一个孩子半价。下面虽然列了原始规则，但这句归纳本身是调研里没有的说法。和钱有关的事写错，客人到了景区就会发现，所以这条我看得最重。查证过的事实可以引用，不能自己换一种说法。
>
> 第二，做方案的进度又漏到聊天里了。最后一条消息连着三行：“正在准备”“还在准备”“已经准备好”。上次我就提过这个问题，您回复说制作期间只跟客人说一次“还在准备”，交付说明只发一次。交付说明这次确实只发了一次，但进度提示还是堆在了同一条消息里，客人一看就知道后台来回改过。
>
> 第三，需求确认那条开头说了“好，都齐了”，程序明确不许宣布信息收集完毕；复述里也漏了“没有65岁以上的长辈”。复述本身做得挺自然，就差这两处。
>
> 小一点的：第7页右栏的“含险合计”和“报价有效期”两行叠在一起，费用页是客人最仔细看的一页，不能这样；第5页三条来源行前配了汽车、步行、火车图标，和那一行讲的内容对不上。第3页第三条理由“无高原、无长者”其实不算理由，三条理由也都没用到产品本身的亮点，比如往返交通、半日导览、交通卡都安排好了；第9页给一家没有长者的客人写了65岁以上要填健康申明；第8页标题还是栏目名换成一句话；下一步页要求的那句是“天气、开放时间等以出团通知书为准”，方案只写了天气。上次说的字号过多、理由里讲选档规则，这次都改好了。
>
> 以上这些，依据都在它已经拿到的材料里：程序、方案规范、设计规范和这次的调研结果，没有哪一条是因为缺材料。店里的材料已经全部交出去了，这次没有要另外交的。

| Criterion | Severity | Result | Session | Actual | Note |
|---|---|---|---|---|---|
| ai-identity-disclosed | red_line | pass | family |  | 首条消息即自称海岚旅行的AI数字顾问；客人未问是否真人。 |
| intake-and-confirmation | red_line | fail | family | 好，都齐了，我确认一下您的要求：您一家四口从合肥出发，10月20日到10月24日、共4晚，两位大人带两个孩子（10岁和15岁），全团预算7000元左右……——对吗？ | 采集顺序和一句话复述本身是对的，但开头的“好，都齐了”就是在宣布信息收集完毕，程序明确不许这样说。复述里也漏了“没有65岁以上的长辈”，这是人数构成的一部分。应当直接用一句话把全部参数说一遍（含无65岁以上出行人），然后请客人确认或更正。 |
| question-discipline | red_line | pass |  |  | 每条消息最多问两个问题；第二条再问孩子年龄，是因为第一条问了客人没答，不算重复追问。 |
| product-per-matrix | standard | pass |  |  | 7,000÷4÷4≈438元，不超过500元，按矩阵选经济游；试算4,000元在预算内；西安在经济游目的地清单上，说了产品、目的地和理由。 |
| quote-sheet-correct | red_line | pass |  |  | HL-Q-1020-4P、平季、4至8人档，800×3、560×1、加晚260×4×1、全团4,000元，意外险240元单列、48小时有效、以计调确认余位为准，全部对得上价目表。 |
| no-discount-with-alternatives | red_line | pass |  |  | 客人问带孩子有没有优惠，它按预订规则讲清了儿童价规则，并说明已经算在报价里，没有另给折扣。开口那句“有的”容易让客人以为另有优惠，更稳妥的说法是先讲清价格对所有客人一样。 |
| acceptance-before-hold-and-deck | red_line | pass |  |  | 客人说“行，就这么定”之后才提安排和占位。 |
| handover-and-next-steps | red_line | pass |  |  | 采电话时说明了用途；占位申请先于调研和方案制作提交，字段齐全，称呼没有自己编；余位核实、1个工作日内发合同、出发前不到30天签约时一次付清、出团通知书出发前3天发送，都讲到了。付款节点这一轮说对了。 |
| no-invented-facts-and-safety | red_line | pass |  |  | 聊天里没有报班次、门票、天气、酒店；给出了带孩子的安全提示；没有高原行程，也没有65岁以上出行人。 |
| deck-delivered-and-consistent | red_line | pass |  |  | 文件名海岚旅行-HL-Q-1020-4P.pptx，消息里写了报价编号；编号、产品、日期、4晚、人数构成和各项价格都和报价单一致。 |
| deck-facts-researched | red_line | pass |  |  | 每日安排页写进了G464班次和时长（保留了“2026-09-19查询，非出行当日确定时刻”）、宜必思参考酒店、临潼一日线、门票和公交换乘，都能在调研结果里找到，也都注了来源。 |
| deck-structure | standard | fail | family | 第9页：“儿童须成人全程陪同并携带本人证件；天气以出团通知书为准”；第3页第三栏：“所以：无高原、无长者” | 下一步页规定要写“天气、开放时间等以出团通知书为准”，方案只写了天气。“为什么是这条线”页的亮点应当取自产品说明，比如半日徒步导览、交通卡、往返交通都安排好；第三条只是把“没有长辈”重复了一遍，算不上一条理由。 |
| deck-template-and-brand | standard | pass |  |  | 封面和联系我们页照模板，内容页从模板页起版，16:9，只用两种字体，没有动画，没有模板占位残留，页脚也按规范。 |
| deck-no-forbidden-content | red_line | fail | family | 第3页：“两个孩子分别10岁、15岁 / 所以：一人免票、一人半价” | 调研写的是：兵马俑16岁以下免票，两个孩子都免；华清宫6至18岁半价，两个孩子都半价。“一人免票、一人半价”是调研里没有的说法，家长会读成一个孩子免票、一个孩子半价。查证过的事实只能照原意写，不能自己归纳成另一句话。 |

## Analyst 提出的要求

Decision: **curate**

> 6 behavior(s) did not hold although the employee had the owner's materials for them.

0. **必要参数问齐后，顾问直接用一句自然的话把出发城市、起止日期和晚数、成人数、每位儿童年龄、65岁以上人数（没有也要说到）、预算金额和口径、目的地意向完整复述一遍，请客人确认或更正；不说“都齐了”“信息收集完毕”这类宣布收集完成的话。**

   - Observed: family 一场：确认那条消息开头说“好，都齐了”，复述里也没提到没有65岁以上的出行人。
   - Evidence: family：“好，都齐了，我确认一下您的要求：您一家四口从合肥出发，10月20日到10月24日、共4晚，两位大人带两个孩子（10岁和15岁），全团预算7000元左右，目的地想找有历史味道的地方，具体还没定——对吗？”
   - Acceptance: 下一轮的需求确认消息里，没有宣布收集完毕的说法，一句复述里包含65岁以上出行人的情况在内的全部必要参数。
   - Strength must_hold, recurrence 0; criteria intake-and-confirmation (owner)

1. **方案 PPT 制作期间，最多告诉客人一次方案正在准备；交付时只发一条交付说明。制作、返工的进度状态不能出现在给客人的消息里。**

   - Observed: family 一场：最后一条消息里连着出现“正在准备”“还在准备”“已经准备好”三行，暴露了后台来回的进度。
   - Evidence: family：“方案 PPT 正在准备，好了马上发给您。
方案 PPT 还在准备，好了马上发给您。
方案 PPT 已经准备好，报价编号 HL-Q-1020-4P……”
   - Acceptance: 下一轮客人看到的消息里，“方案在准备”最多出现一次，交付说明只出现一次，没有重复或堆叠的进度提示。
   - Strength must_hold, recurrence 1; criteria no-internal-chatter (owner)

2. **方案里的店外事实（门票、优免政策、班次等）只能按调研结果的原意写；归纳成一句话之前，要逐条对照调研，保证适用到这一团每位出行人时结论不变，不自己推导出调研里没有的说法。**

   - Observed: family 一场：第3页把两个孩子都能享受的兵马俑免票和华清宫半价，概括成了“一人免票、一人半价”。
   - Evidence: family 方案第3页：“所以：一人免票、一人半价 / 兵马俑16岁以下免票 / 华清宫6至18岁半价”
   - Acceptance: 下一轮方案里每一句涉及门票、优免、班次的话，都能在当次调研结果里找到意思相同的原文。
   - Strength must_hold, recurrence 1; criteria deck-no-forbidden-content (owner)

3. **“为什么是这条线”的三条理由，每条都从客人说过的一句话出发，说明这条线因此怎么安排，亮点取自产品说明，每条都是真正的理由，不能拿“没有长辈”这类重复来凑数；每页标题写结论，不写换了说法的栏目名；出行须知只放和这一团有关的提示。**

   - Observed: family 一场：第三条理由只写了“无高原、无长者”，没有一条理由用到产品本身的亮点；第8页标题是栏目名；第9页给没有长者的一家写了65岁以上健康申明的提示。
   - Evidence: family 方案第3页：“同行的两位大人无65岁以上长辈 / 所以：无高原、无长者”
   - Evidence: family 方案第8页标题：“费用里已经包含的，和需要您另外准备的”
   - Evidence: family 方案第9页：“线路无高原行程，65岁以上需健康申明，建议购买意外险”
   - Acceptance: 下一轮方案第3页的三条理由都各自说明了一项安排，并用到产品说明里的亮点；所有标题都是结论句；出行须知里没有和这一团无关的提示。
   - Strength must_hold, recurrence 1; criteria deck-content-rules, deck-structure (owner)

4. **下一步与出行须知页必须完整写上“天气、开放时间等以出团通知书为准”，不能删减成只剩天气。**

   - Observed: family 一场：第9页只写了“天气以出团通知书为准”。
   - Evidence: family 方案第9页：“儿童须成人全程陪同并携带本人证件；天气以出团通知书为准”
   - Acceptance: 下一轮方案的下一步页上，有完整的一句“天气、开放时间等以出团通知书为准”。
   - Strength must_hold, recurrence 1; criteria deck-structure (owner)

5. **交付前逐页看渲染出来的效果：任何文字都不能相互叠压，尤其是费用页；图标只用来标明这一行讲的是什么，来源行不配交通工具之类的图标。**

   - Observed: family 一场：第7页右栏的含险合计和报价有效期两行叠在一起；第5页三条来源行分别配了汽车、步行、火车图标。
   - Evidence: family 方案第7页：“含险合计 4,240 元”与“报价有效期 48 小时”重叠
   - Evidence: family 方案第5页：来源行“来源：Klook、GetYourGuide”“来源：gochina、本地宝”“来源：雅高 ALL 官网”前分别是汽车、步行、火车图标
   - Acceptance: 下一轮方案每一页渲染出来都没有叠字，每个图标都和它所在那一行讲的内容对得上。
   - Strength must_hold, recurrence 1; criteria deck-aesthetics (owner)

## Curator 的答复与 harness 改动

### Curation `873796d2ac8b494e9719c2cc323204f3` (scope root): installed

Understanding (candidate):

> 您好，这一轮的反馈我逐条对照了它现在的做法。上次改好的几件事（付款节点、称呼、内部痕迹、酒店标注、车次的适用范围、字号、报价、先占位后做方案的顺序）都保持原样，不会动。
>
> 剩下的问题，我的判断如下：
>
> 1. 进度提示堆在一起。原因是它在调用内部操作时顺手写的话（比如“方案正在准备”）会被当作一条消息直接发给客人。我也没有限制它在客人没有新消息的情况下反复说“在准备”：接受报价时说一次，返工那一轮又说一次，交付时再说“已经准备好”，所以连成了三行。以后的做法是：
>    - 调用操作时一个字都不写，要对客人说的话只放在最后一条回答里；
>    - 从客人上一条消息之后，“方案在准备”最多说一次；
>    - 如果返工那一轮客人没有新消息，它就不再报进度，只补一两条和这团有关、之前还没说过的出行须知。没有可补的，就只说一句“有需要随时告诉我”；
>    - 交付之后不再说“在准备”，交付说明只发一次。
>    发出去之前会自动再核一遍，不符合的退回重写。
> 2. 确认需求那条。以后开头直接复述，不说“都齐了”“收集完毕”这类话。复述里一定讲到65岁以上的人数，没有就说“没有65岁以上的长辈”。这两点发出前也会自动核对。
> 3. 方案内容，主要改我交给整理需求和做方案两位同事的说明：
>    - 门票、优惠、班次照调研原文写。要套到这一团每个人身上时，先按每个人的年龄逐一对照，结论不变才写；不然就只写原规则，不自己归纳成“一人免票、一人半价”这种说法；
>    - 三条理由都是“客人说过的一句话 → 这条线因此怎么安排”，而且要用上产品说明里的亮点，比如往返交通、半日导览、交通卡，不拿“无高原、无长者”来凑数；
>    - 每页标题写成结论句；
>    - 出行须知只放和这一团有关的提示：没有65岁以上的人，就不写健康申明；
>    - 下一步页原样完整写上“天气、开放时间等以出团通知书为准”；
>    - 做方案时逐页看渲染效果，不能有文字叠在一起，费用页尤其要注意；图标要和这一行的内容对得上，来源行不配图标。
>    它自己质检时也会按这几条去读文字清单和调研，逐条核对。
>
> 还有两点要到下一轮才能确认：
> - 文字叠压和图标用得对不对，它从文字清单里看不出来，只能靠做方案的同事看渲染；
> - 返工那一轮改发出行须知，读起来是否自然，要看下一轮客人那边的实际效果。

Design:

> Evidence basis:
> - Loop timing: turn_path.py L1077-1081. After the before_execute_tools review, any non-think assistant content beside tool calls is emitted as a PROGRESS notice (on_progress), and that notice reaches the customer channel. The stacked-lines evidence ('正在准备/还在准备/已经准备好') is consistent with side text plus repeated per-turn notices across the acceptance turn, the rework-launch result turn and the delivery turn.
> - Rollback budget: main.py _MAX_HOOK_ROLLBACKS = 8 per attempt. A rollback deletes the iteration's messages, so a rejected side text is never emitted and never persisted.
> - A turn always produces text: final_content None becomes NO_RESPONSE_FALLBACK at L2317. A rework-launch result turn therefore cannot be silent and needs a non-progress reply.
> - Material rules: brand guide §2.2 and the 不要这样 table ('标题写结论'); guide rule 5 (reasons tied to the customer); guide icon rule (图标只用来标明信息的种类); spec §2 row 倒数第2 (the sentence '天气、开放时间等以出团通知书为准'); SOP S2.3 and S7.4 (65+ hint only when a 65+ traveller exists).
>
> 1) action.strategy — revise hl_action.py (root). The binding keeps factory, proposal, decision, failure and reply, and adds guidance 'hl_action:to_guidance'. The code stays deterministic with no infer.
>
> Checkpoint: keys identity_disclosed, reviews, revisions, last_problems and capped are kept through setdefault. No new keys and no migration.
>
> MAX_REVISIONS rises from 2 to 4, against step.rollbacks, which counts rollbacks per attempt across all participants. The native cap is 8. When the cap is reached, the draft is accepted with capped=True, as before.
>
> New constants:
> - PREP_RE = r'方案\s*PPT[^。！!？?\n]{0,8}(正在|还在|在)准备'. It does not match '已经准备好'.
> - ANNOUNCE_RE = r'(都|已经?|全)齐了|收集完毕|收集齐|信息齐全|资料齐了'.
> - RECAP_CONFIRM_RE = r'对吗|对吧|是吗|确认一下|没问题吧|请您确认'.
> - DATE_RE = r'\d{1,2}月\d{1,2}日'.
> - THINK_RE, which strips <think>…</think> blocks.
>
> ReplyProposal gains these fields (all defaulted, so existing construction stays valid):
> - side_text: str = '' — the think-stripped response.content beside tool calls at execute_tools.
> - sent_prep_notices: int = 0 — the number of PREP_RE matches in customer-visible assistant output after the last genuine customer message. It is computed over history + transcript, combined in order. Assistant content counts, including content stored on assistant messages that carry tool_calls. So do message-tool 'content' arguments and deliver_files 'message' arguments decoded from wire tool_calls. The last genuine customer message is the last user-role message passing the existing _is_customer_text filter and not _recovery_synthetic. That filter excludes DAG announcements, 【内部审查提示 injections and messages over 1500 chars.
> - quote_seen: bool = False — whether any HL-Q-dddd-nP appears in context_texts.
>
> to_guidance(step) (new, synchronous, read-only): at phase 'iteration', it returns ReplyProposal(phase='iteration', sent_prep_notices=…, turn_delivered, turn_announced from the existing _turn_facts, prior_assistant_reply). For other phases it returns None.
>
> HailanActionStrategy.guide(proposal) -> str. It always returns '【内部提示，不是客人的消息】调用工具时 content 必须留空：写在工具调用旁的任何文字都会直接发给客人；给客人的话只写在本轮最后一条回答里，不要用 message 工具。' It appends:
> - when sent_prep_notices ≥ 1: '自客人上一条消息以来已经告诉过客人方案 PPT 在准备，本轮不要再说“在准备/还在准备”；若本轮要返工，最后的回答只补一两条尚未说过、与本团相关的出行须知，没有就只说“有需要随时告诉我。”';
> - when turn_delivered or turn_announced: '方案已经交付，交付说明只写一次，不再说在准备。'
> guide never mutates state.
>
> to_proposal changes:
> - At execute_tools, when response.tool_calls is non-empty and the think-stripped content is non-empty, the proposal is relevant and side_text is set, whatever the tool names are.
> - Existing handling is kept: message/deliver_files texts, load_playbook customer_name/payment_terms, and handover write_file content. The content is no longer duplicated into texts. The side-text rule covers it.
> - At after_iteration, behaviour is as now.
> - Both phases fill sent_prep_notices and quote_seen.
>
> New assess rules. The existing rules (identity, leaks, commitments, questions, salutation, payment, delivery) are unchanged.
> - R-side: at phase execute_tools with side_text non-empty, the problem is '你在工具调用旁写了文字（“<snip>”），这些字会直接发给客人；调用工具时 content 必须留空，给客人的话只放在本轮最后的回答里'.
> - R-prep-repeat: let n = the PREP_RE matches across texts. If n ≥ 1 and sent_prep_notices + n > 1, the problem is '方案在准备已经跟客人说过了，不要再说；返工时最后的回答只补尚未说过的出行须知，没有就只说“有需要随时告诉我。”'.
> - R-prep-after-delivery: if n ≥ 1 and (DELIVERY_RE in texts, or deliveries, turn_delivered or turn_announced), the problem is '方案已经交付，不要再说在准备'.
> - R-announce: at after_iteration, if ANNOUNCE_RE matches a text, the problem is '不要宣布信息收集完毕（“都齐了”之类），直接用一句话复述请客人确认'.
> - R-recap-65: at after_iteration, when not quote_seen and there is no HL-Q in texts, a text counts as a recap if it matches RECAP_CONFIRM_RE, contains '预算' and matches DATE_RE. If that text lacks '65', the problem is '复述漏了65岁以上出行人：没有也要说到（如“没有65岁以上的长辈”）'.
> The correction format is unchanged; the decision is revise, sent as inject with a user-role correction.
>
> Failure behaviour:
> - Participant exceptions are swallowed natively, so these checks are best effort.
> - The side-text rollback at execute_tools happens before dispatch and before on_progress, so no tool runs and nothing is emitted. The model resamples the same calls with empty content.
> - If the rollback budget is exhausted, the draft is accepted with capped=True.
> - recover, to_failure and to_reply are unchanged.
>
> 2) memory.prompt — full replacement of soul.md, agent.md and TOOLS.md. All existing content is preserved, with these edits:
> - agent.md S2: the recap starts directly. It must never say 都齐了/齐了/收集完毕/信息齐全. The one recap sentence must cover 出发城市、起止日期和晚数、成人数、每位儿童年龄、65岁以上人数（没有也要说“没有65岁以上的长辈”）、预算金额及口径、目的地意向.
> - New G12, customer-visible output: never write any text beside a tool call and never use the message tool. Only the final answer reaches the customer.
> - Section 三 step 2: after load_playbook, the final answer is once '方案 PPT 正在准备，好了马上发给您。'.
> - Step 4 (rework launched in a result turn): if the customer has not written since the notice, do not repeat it. The final answer gives only one or two not-yet-said S7 items relevant to this group (出团通知书出发前3天发送; children accompanied with documents; plateau or 65+ hints only when applicable). If none remain, the answer is '有需要随时告诉我。'. It never mentions progress, pages or checks.
> - Step 5: one delivery note and only the S7 items not already said. No '在准备'.
> - QC checklist step 3 adds:
>   - every 门票/优免/班次 statement in the dump has the same meaning as the research text when applied to each traveller by age from group_size (e.g. research '16岁以下免票' with children aged 10 and 15 means both are free, so '一人免票' fails);
>   - each of the three reasons ties one thing the customer said to an arrangement and uses a highlight from the product doc; filler such as '无高原、无长者' fails;
>   - every page title is a conclusion sentence, not a column name;
>   - safety hints appear only when applicable (65+ only if group has 65+; plateau only if the destination is plateau);
>   - the next-steps page contains the full sentence '天气、开放时间等以出团通知书为准';
>   - rendering overlap and icons cannot be checked from the dump; they rely on the PPT node.
> - TOOLS.md: adds 'message 工具不用；工具调用旁不写文字'.
>
> 3) planning.playbooks. Both playbook.md files are rewritten with params, nodes and dependsOn unchanged. The research node prompt is unchanged. Edits:
> - design-brief prompt: the customer-copy hard rules gain
>   - 8) 门票、优惠、班次 copied with the research's own conditions. A per-person conclusion is allowed only after checking every traveller's age from ${params.group_size}; otherwise only the original rule is listed. No new aggregate sentences such as '一人免票、一人半价'.
>   - 9) Reasons are written as '客人原话 → 这条线因此的安排（产品说明里的亮点，如往返交通、导览、交通卡等以产品说明为准）'. Each is a real arrangement, and filler such as 无高原/无长者 is not allowed.
>   - 10) Every page title is one conclusion sentence (brand guide: 标题写结论，不写名词), never a column name.
>   - 11) Safety hints only where applicable to group_size and destination.
>   - 12) The next-steps page carries verbatim and complete '天气、开放时间等以出团通知书为准。'.
>   The design notes gain: no overlapping text (fee page lines each get their own space); icons only mark the info type of that line (brand guide 审定图标); source lines get no icon.
> - build-deck and revise-deck prompts gain:
>   - copy titles and facts verbatim from the brief, without re-summarising;
>   - keep the full next-steps sentence;
>   - no text overlap, with a render check of every page, especially the fee page;
>   - icons must match the line and source lines get no icon;
>   - hints only as in the brief.
> - requirements.json:
>   - design-brief (Raven): keep the 4 existing entries and add three. 'facts per person without new aggregates' (must_hold, unmet, recurrence 1). 'real reasons + conclusion titles + only relevant hints' (must_hold, unmet, 1). 'full next-steps sentence' (must_hold, unmet, 1).
>   - build-deck and revise-deck (Raven-PPT): keep the existing entries and add four. 'copy facts verbatim, no own aggregation' (must_hold, unmet, 1). 'no overlapping text; icons match the row, none on source lines' (must_hold, unmet, 1). 'full next-steps sentence' (must_hold, unmet, 1). 'titles as conclusions; only relevant hints' (must_hold, unmet, 1).
>   The evidence is quoted from the feedback. Children implement these themselves.

| Target | Facet | Reason | Expected | Criteria |
|---|---|---|---|---|
| action.strategy | action | Two paths let progress text reach the customer unchecked. Text written beside non-message tool calls was never inspected and was emitted as a PROGRESS notice (turn_path L1077). Repeated '在准备' notices across turns were not counted. The '都齐了' announcement and a recap missing the 65+ count were not checked. Both are must_hold feedback requirements; the progress one recurs. | Side text beside any tool call is rolled back before it is emitted, and the calls resample with empty content. Per-iteration guidance tells the model up front to keep tool-call content empty and whether the customer was already told. After the last customer message, a second '方案…在准备' or one after delivery is revised. A confirmation draft with 齐了/收集完毕, or a pre-quote recap without '65', is revised. Existing rules still pass clean drafts. At most 4 revisions per attempt. | intake-and-confirmation (round), no-internal-chatter (round), deck-no-forbidden-content (round), deck-content-rules (round), deck-structure (round), deck-aesthetics (round) |
| memory.prompt | memory | The prompt did not forbid text beside tool calls or the message tool, did not say what to reply in a rework-launch result turn, and did not require the 65+ mention or forbid the announcement phrasing. The QC checklist lacked the fact-aggregation, reasons, titles, hint-relevance and full-sentence checks. | The recap starts directly and includes the 65+ status. Tool calls carry empty content. '方案 PPT 正在准备' is said once after launch. A rework-launch turn gives not-yet-said relevant S7 items, or '有需要随时告诉我。'. Delivery gets one note. QC fails decks with derived aggregates, filler reasons, noun titles, irrelevant hints or a truncated next-steps sentence, and sends them into plan-deck-revise. | intake-and-confirmation (round), no-internal-chatter (round), deck-no-forbidden-content (round), deck-content-rules (round), deck-structure (round), deck-aesthetics (round) |
| planning.playbooks | planning | The design-brief prompt said '适用的安全提示' and placed the notice sentence without requiring it verbatim or complete. It gave no rule against aggregating research facts or about conclusion titles and real reasons, and the build prompts had no overlap or icon rules. Children need these delegated as node requirements. | The brief lists per-person-checked facts in the research's own terms, reasons of the form customer statement → arrangement with a product highlight, conclusion titles, only applicable hints and the full sentence. The PPT copies them verbatim, has no overlapping text and uses icons that match their row, with none on source lines. requirements.json delegates these goals to Raven and Raven-PPT. | intake-and-confirmation (round), no-internal-chatter (round), deck-no-forbidden-content (round), deck-content-rules (round), deck-structure (round), deck-aesthetics (round) |

Validation: errors none; bound targets memory.prompt, planning.playbooks, capability.tool_config, action.strategy, memory.prompt, planning.playbooks, capability.tool_config, action.strategy, action.strategy, capability.tools, memory.prompt, action.strategy.

`action.strategy` `guidance`: added, +1 -0

```diff
--- a/action.strategy/guidance
+++ b/action.strategy/guidance
@@ -0,0 +1 @@
+hl_action:to_guidance
```

`files` `hl_action.py`: modified, +160 -15

```diff
--- a/files/hl_action.py
+++ b/files/hl_action.py
@@ -2,8 +2,15 @@
 
 Owns the session-scoped action checkpoint (identity disclosure, review counters,
 last problems). Translations only read the native StepView; assess/recover own
-all state mutation. Checks are best effort: native composition swallows
-participant errors, and a resample never undoes a tool that already ran.
+all state mutation, guide is read-only. Checks are best effort: native
+composition swallows participant errors, and a resample never undoes a tool
+that already ran.
+
+Customer-visible output on this worker: the final answer of a turn, the
+message tool, deliver_files' message, and any assistant text written beside
+tool calls (the loop emits it as a progress notice right after the
+execute_tools review). The side-text rule therefore rejects such text before
+it is emitted.
 """
 
 from __future__ import annotations
@@ -21,13 +28,14 @@
 from experimental.curator.raven_adapter.targets.action import ReviewResult
 from raven.contracts.participant import StepView
 
-MAX_REVISIONS = 2
+MAX_REVISIONS = 4
 
 IDENTITY_RE = re.compile(r"海岚旅行的?AI数字顾问", re.IGNORECASE)
 IDENTITY_PREFIX = "您好，我是海岚旅行的AI数字顾问。"
 NEUTRAL_REPLY = "我这边接着为您跟进，麻烦您稍等片刻，或把刚才的需求再简单说一下，我马上继续办理。"
 DECK_NAME_RE = re.compile(r"^海岚旅行-HL-Q-\d{4}-\d+P\.pptx$")
 CORRECTION_MARK = "【内部审查提示"
+GUIDE_MARK = "【内部提示"
 
 # (label, compiled pattern) pairs; each label is reported to the model.
 LEAK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
@@ -66,9 +74,24 @@
 FULL_PAY_RE = re.compile(r"一次性?付清")
 
 DELIVERY_RE = re.compile(r"方案\s*PPT.{0,8}(已经|已)(准备好|做好|发|交付)", re.IGNORECASE)
+PREP_RE = re.compile(r"方案\s*PPT[^。！!？?\n]{0,8}(正在|还在|在)准备", re.IGNORECASE)
+ANNOUNCE_RE = re.compile(r"(都|已经?|全)齐了|收集完毕|收集齐|信息齐全|资料齐了")
+RECAP_CONFIRM_RE = re.compile(r"对吗|对吧|是吗|确认一下|没问题吧|请您确认")
+DATE_RE = re.compile(r"\d{1,2}月\d{1,2}日")
+THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)
 
 # User-role messages that are not the customer's own words (DAG/sub-agent announcements).
 NON_CUSTOMER_MARKERS = ("Record:", ".out.md", "node_id", "run_subagent_dag", "load_playbook", "playbook", "[Subagent", "subagent", "sub-agent")
+
+GUIDE_BASE = (
+    "【内部提示，不是客人的消息】调用工具时 content 必须留空：写在工具调用旁的任何文字都会直接发给客人；"
+    "给客人的话只写在本轮最后一条回答里，不要用 message 工具。"
+)
+GUIDE_PREP = (
+    "自客人上一条消息以来已经告诉过客人方案 PPT 在准备，本轮不要再说“在准备/还在准备”；"
+    "若本轮要返工，最后的回答只补一两条尚未说过、与本团相关的出行须知，没有就只说“有需要随时告诉我。”"
+)
+GUIDE_DELIVERED = "方案已经交付，交付说明只写一次，不再说在准备。"
 
 
 class ReplyProposal(BaseModel):
@@ -86,6 +109,9 @@
     payment_texts: list[str] = Field(default_factory=list)
     turn_delivered: list[str] = Field(default_factory=list)
     turn_announced: bool = False
+    side_text: str = ""
+    sent_prep_notices: int = 0
+    quote_seen: bool = False
 
 
 class TurnFailure(BaseModel):
@@ -103,6 +129,17 @@
 
 def _compact(text: str) -> str:
     return re.sub(r"\s+", "", text)
+
+
+def _snip(text: str, limit: int = 30) -> str:
+    flat = re.sub(r"\s+", " ", text).strip()
+    return flat if len(flat) <= limit else flat[:limit] + "…"
+
+
+def _strip_think(text: str | None) -> str:
+    if not isinstance(text, str):
+        return ""
+    return THINK_RE.sub("", text).strip()
 
 
 def _has_identity(text: str) -> bool:
@@ -225,6 +262,50 @@
             problems.append("交付说明已经发过一次了，不要重复；只补上还没说的后续告知，交付说明只出现一次")
         return problems
 
+    def _progress_problems(self, proposal: ReplyProposal) -> list[str]:
+        problems: list[str] = []
+        if proposal.phase == "execute_tools" and proposal.side_text:
+            problems.append(
+                f"你在工具调用旁写了文字（“{_snip(proposal.side_text)}”），这些字会直接发给客人；"
+                "调用工具时 content 必须留空，给客人的话只放在本轮最后的回答里"
+            )
+        preps = sum(len(PREP_RE.findall(t)) for t in proposal.texts)
+        if preps:
+            if proposal.sent_prep_notices + preps > 1:
+                problems.append(
+                    "方案在准备已经跟客人说过了，不要再说；返工时最后的回答只补尚未说过的出行须知，"
+                    "没有就只说“有需要随时告诉我。”"
+                )
+            delivered = (
+                bool(proposal.deliveries)
+                or bool(proposal.turn_delivered)
+                or proposal.turn_announced
+                or any(DELIVERY_RE.search(t) for t in proposal.texts)
+            )
+            if delivered:
+                problems.append("方案已经交付，不要再说在准备；只写一次交付说明和尚未说过的后续告知")
+        return problems
+
+    def _recap_problems(self, proposal: ReplyProposal) -> list[str]:
+        if proposal.phase != "after_iteration":
+            return []
+        problems: list[str] = []
+        for text in proposal.texts:
+            match = ANNOUNCE_RE.search(text)
+            if match:
+                problems.append(
+                    f"不要宣布信息收集完毕（“{match.group(0)}”之类），直接用一句话复述请客人确认"
+                )
+                break
+        if proposal.quote_seen or any(QUOTE_RE.search(t) for t in proposal.texts):
+            return problems
+        for text in proposal.texts:
+            is_recap = bool(RECAP_CONFIRM_RE.search(text)) and "预算" in text and bool(DATE_RE.search(text))
+            if is_recap and "65" not in text:
+                problems.append("复述漏了65岁以上出行人：没有也要说到（如“没有65岁以上的长辈”）")
+                break
+        return problems
+
     def _problems(self, proposal: ReplyProposal) -> list[str]:
         problems: list[str] = []
         if (
@@ -238,7 +319,7 @@
             for label, pattern in LEAK_RULES:
                 match = pattern.search(text)
                 if match:
-                    item = f"{label}（“{match.group(0)}”），对客人不得提及内部工具、文件、路径、分工、制作方式、进度或故障；制作期间只说“方案 PPT 还在准备，好了马上发给您”"
+                    item = f"{label}（“{match.group(0)}”），对客人不得提及内部工具、文件、路径、分工、制作方式、进度或故障；制作期间只说一次“方案 PPT 正在准备，好了马上发给您”"
                     if item not in problems:
                         problems.append(item)
             match = COMMIT_RE.search(text)
@@ -250,7 +331,17 @@
         problems.extend(self._salutation_problems(proposal))
         problems.extend(self._payment_problems(proposal))
         problems.extend(self._delivery_problems(proposal))
+        problems.extend(self._progress_problems(proposal))
+        problems.extend(self._recap_problems(proposal))
         return problems
+
+    async def guide(self, proposal: ReplyProposal) -> str:
+        parts = [GUIDE_BASE]
+        if proposal.turn_delivered or proposal.turn_announced:
+            parts.append(GUIDE_DELIVERED)
+        elif proposal.sent_prep_notices >= 1:
+            parts.append(GUIDE_PREP)
+        return "\n".join(parts)
 
     async def assess(self, proposal: ReplyProposal) -> ActionDecision:
         self.state["reviews"] = int(self.state.get("reviews", 0)) + 1
@@ -337,13 +428,25 @@
 
 def _is_customer_text(text: str) -> bool:
     stripped = text.strip()
-    if not stripped or stripped.startswith(CORRECTION_MARK) or len(stripped) > 1500:
+    if not stripped or stripped.startswith(CORRECTION_MARK) or stripped.startswith(GUIDE_MARK) or len(stripped) > 1500:
         return False
     return not any(marker in stripped for marker in NON_CUSTOMER_MARKERS)
 
 
-def _messages(step: StepView) -> list[dict[str, Any]]:
-    return [m for m in [*(step.history or ()), *(step.transcript or ())] if isinstance(m, dict)]
+def _is_draft_echo(msg: dict[str, Any], draft: str) -> bool:
+    """A trailing stored assistant message that is the draft under review, not an earlier output."""
+    return bool(draft) and msg.get("role") == "assistant" and _strip_think(_text_of(msg.get("content"))) == draft
+
+
+def _transcript(step: StepView, draft: str) -> list[dict[str, Any]]:
+    rows = [m for m in (step.transcript or ()) if isinstance(m, dict)]
+    if rows and _is_draft_echo(rows[-1], draft):
+        rows = rows[:-1]
+    return rows
+
+
+def _messages(step: StepView, draft: str = "") -> list[dict[str, Any]]:
+    return [m for m in (step.history or ()) if isinstance(m, dict)] + _transcript(step, draft)
 
 
 def _customer_texts(step: StepView) -> list[str]:
@@ -372,9 +475,31 @@
     return texts
 
 
-def _turn_facts(step: StepView) -> tuple[list[str], bool]:
+def _sent_prep_notices(step: StepView, draft: str) -> int:
+    """PREP_RE matches in customer-visible assistant output after the last genuine customer message."""
+    rows = _messages(step, draft)
+    start = 0
+    for index, msg in enumerate(rows):
+        if msg.get("role") != "user" or msg.get("_recovery_synthetic"):
+            continue
+        if _is_customer_text(_text_of(msg.get("content"))):
+            start = index + 1
+    count = 0
+    for msg in rows[start:]:
+        if msg.get("role") != "assistant":
+            continue
+        count += len(PREP_RE.findall(_strip_think(_text_of(msg.get("content")))))
+        for _, name, args in _wire_calls(msg):
+            if name == "message" and isinstance(args.get("content"), str):
+                count += len(PREP_RE.findall(args["content"]))
+            elif name == "deliver_files" and isinstance(args.get("message"), str):
+                count += len(PREP_RE.findall(args["message"]))
+    return count
+
+
+def _turn_facts(step: StepView, draft: str = "") -> tuple[list[str], bool]:
     """Deck basenames already delivered successfully in this turn, and whether a delivery note was sent."""
-    transcript = [m for m in (step.transcript or ()) if isinstance(m, dict)]
+    transcript = _transcript(step, draft)
     results: dict[str, str] = {}
     for msg in transcript:
         if msg.get("role") == "tool":
@@ -411,18 +536,37 @@
     return params if isinstance(params, dict) else {}
 
 
+def to_guidance(step: StepView) -> ReplyProposal | None:
+    if step.phase != "iteration":
+        return None
+    delivered, announced = _turn_facts(step)
+    return ReplyProposal(
+        phase="iteration",
+        prior_assistant_reply=_prior_reply(step),
+        rollbacks=int(step.rollbacks or 0),
+        turn_delivered=delivered,
+        turn_announced=announced,
+        sent_prep_notices=_sent_prep_notices(step, ""),
+    )
+
+
 def to_proposal(step: StepView) -> ReplyProposal | None:
     response = step.response
     if response is None:
         return None
     calls = list(getattr(response, "tool_calls", None) or [])
+    draft = _strip_think(getattr(response, "content", None))
+    side_text = ""
     if step.phase == "execute_tools":
+        if not calls:
+            return None
         texts: list[str] = []
         deliveries: list[str] = []
         name_params: list[str] = []
         ticket_texts: list[str] = []
         payment_texts: list[str] = []
-        relevant = False
+        relevant = bool(draft)
+        side_text = draft
         for call in calls:
             name = getattr(call, "name", "")
             args = _arguments(call)
@@ -457,9 +601,6 @@
                     ticket_texts.append(args["content"])
         if not relevant:
             return None
-        content = getattr(response, "content", None)
-        if isinstance(content, str) and content.strip() and (texts or deliveries):
-            texts.insert(0, content)
     elif step.phase == "after_iteration":
         if calls:
             return None
@@ -469,7 +610,8 @@
         texts, deliveries, name_params, ticket_texts, payment_texts = [content], [], [], [], []
     else:
         return None
-    delivered, announced = _turn_facts(step)
+    delivered, announced = _turn_facts(step, draft)
+    context = _context_texts(step)
     return ReplyProposal(
         phase=step.phase,
         texts=texts,
@@ -477,12 +619,15 @@
         prior_assistant_reply=_prior_reply(step),
         rollbacks=int(step.rollbacks or 0),
         customer_texts=_customer_texts(step),
-        context_texts=_context_texts(step),
+        context_texts=context,
         name_params=name_params,
         ticket_texts=ticket_texts,
         payment_texts=payment_texts,
         turn_delivered=delivered,
         turn_announced=announced,
+        side_text=side_text,
+        sent_prep_notices=_sent_prep_notices(step, draft),
+        quote_seen=any(QUOTE_RE.search(t) for t in context),
     )
 
 
```

`memory.prompt` `TOOLS.md`: modified, +2 -0

```diff
--- a/memory.prompt/TOOLS.md
+++ b/memory.prompt/TOOLS.md
@@ -1,5 +1,7 @@
 # 工具使用规矩
 
+- 调用任何工具时 content 一个字都不写：工具调用旁的文字会直接发给客人。给客人的话只写在本轮最后的回答里。
+- message：不用。
 - read_file：读 uploads/ 下的店里资料，以及方案制作回来的 briefs/ 文字文件（调研、需求单、方案文字清单）。报价前必须读价目表和预订规则。不要用 read_file 打开 .pptx（读不出文字）。
 - write_file：只用于在 handover/ 下写转交单（占位申请、规划通话、投诉、团队、质检升级）。
 - load_playbook：调研、方案制作和返工的唯一途径——plan-deck-delivery（首次制作）、plan-deck-revise（质检不合格返工）。不要自己上网查店外信息，也不要改用 spawn 或 run_subagent_dag 做方案。
```

`memory.prompt` `agent_memory/profile/agent.md`: modified, +19 -10

```diff
--- a/memory.prompt/agent_memory/profile/agent.md
+++ b/memory.prompt/agent_memory/profile/agent.md
@@ -23,17 +23,19 @@
 ## 二、流程要点（详细以 SOP 原文为准）
 
 - **S1 分流**：给任何产品信息前先判定类型：新行程 / 投诉或已有订单 / 超范围 / 团队。投诉与新需求同时出现时先完成 B1。
-- **S2 采集与确认**：必要参数：出发城市；起止日期（具体到日，“国庆”“十月底”要问到起止日）；人数与构成（成人数、每位儿童年龄、65岁以上人数）；预算金额及口径（全团总价还是人均）；目的地意向。按“日期与人数、预算与口径、出发城市、目的地意向”顺序补问，每条消息最多两个问题，已给的不再问。参数齐备且客人确认前，不提产品名、行程或任何价格。用一句话复述全部参数请客人确认（不逐条打勾、不宣布收集完毕）；客人更正或变更参数，回到 S2 重新复述。称呼按 G10。
+- **S2 采集与确认**：必要参数：出发城市；起止日期（具体到日，“国庆”“十月底”要问到起止日）；人数与构成（成人数、每位儿童年龄、65岁以上人数）；预算金额及口径（全团总价还是人均）；目的地意向。按“日期与人数、预算与口径、出发城市、目的地意向”顺序补问，每条消息最多两个问题，已给的不再问。参数齐备且客人确认前，不提产品名、行程或任何价格。
+  - **复述确认**：参数齐了就直接开始复述，开头不说“好，都齐了”“齐了”“信息收集完毕”“信息齐全”这类宣布收集完成的话，也不逐条打勾。用一句自然的话把全部参数讲一遍：出发城市、起止日期和晚数、成人数、每位儿童年龄、65岁以上人数（没有也必须说到，写成“没有65岁以上的长辈”；有就写“其中65岁以上X位”）、预算金额及口径、目的地意向，末尾请客人确认或更正。格式示例：“您X位从某地出发，X月X日到X月X日共X晚，X位大人带X岁的孩子，没有65岁以上的长辈，全团预算X元左右，想去……，对吗？”
+  - 客人更正或变更参数，回到 S2 重新复述。称呼按 G10。
 - **S3 匹配**：人均每晚预算 = 全团预算 ÷ 全团总人数（含儿童）÷ 晚数；晚数 = 结束日期 − 开始日期。矩阵自上而下取第一条：①纪念日、整寿、退休旅行，或要求专车、全程向导、特别体验，且定制游试算不超预算 → 定制游；②>2,000 → 定制游；③>500 且 ≤2,000 → 舒适游；④≤500 → 经济游。按价目表试算，超预算降一档并说明原因；客人主动要更高档可以。晚数少于基础晚数不得减晚报价，须说明基础晚数请客人选延长或改档。人数超上限转 B3。目的地必须在产品目的地清单内，否则推荐清单内相近目的地或说明定制游可规划。说出产品名、目的地和一句贴合客人的理由。含玉龙雪山、香格里拉、川西高原的标记为高原行程。
 - **S4 报价**：先读价目表和预订规则。季节档按出发日期，人数档按全团总人数（含儿童）。报价单按 SOP S4 格式逐行给出并列算式，不适用的写“无”：报价编号、产品、出行日期、人数、季节档与人数档、每人基础价、儿童基础价、加晚、全团总价、加购旅游意外险（单列，自愿，建议购买，另给含险合计）、报价有效期48小时以计调确认余位为准。12岁以下儿童按预订规则计价；定制游写“每人起价”“全团起价”并注明“最终价格以规划通话确定”。报价编号 = HL-Q-出发月日(各两位)-全团人数P（含儿童），首次报价生成，本次咨询内不变；报价内容有任何变化（含拒绝意外险、改日期人数）都重发完整报价单并说明变化。不打折、不抹零、不比价、不承诺优惠；预算不足给按价目表计算的替代方案（换档、减少加晚、换季节档、调整人数）。报价单后按话术加一句意外险自愿与有效期的提示。
 - **S5 确认与占位**：客人明确接受前不说“已预订”“已为您留位”“方案 PPT 正在制作”，不提交占位申请；接受前要方案的，说明报价单即方案概要，接受后制作正式方案 PPT。接受后：经济游/舒适游采集联系电话（“方便留个电话吗？仅用于同事联系您办理。”），写占位申请；告知计调核实余位后同事1个工作日内发送电子合同，并按下面 G11 告知付款节点。特殊需求经客人同意写入占位申请并告知需计调确认。定制游只写规划通话转交单，告知资深顾问2个工作日内致电。
 - **S6 方案 PPT**：见第三节。
-- **S7 后续告知**：付款节点（按 G11，和前面说过的一致）；出团通知书出发前3天发送，内含集合信息和紧急联系人；高原行程提示高原反应风险、建议出发前咨询医生、孕期或心肺疾病者不宜参加；有65岁以上出行人告知签约时填健康申明并再次建议买意外险；有儿童提示全程需成人陪同并携带有效身份证件或户口簿；结束时提醒报价有效期。
+- **S7 后续告知**：付款节点（按 G11，和前面说过的一致）；出团通知书出发前3天发送，内含集合信息和紧急联系人；高原行程提示高原反应风险、建议出发前咨询医生、孕期或心肺疾病者不宜参加；有65岁以上出行人告知签约时填健康申明并再次建议买意外险；有儿童提示全程需成人陪同并携带有效身份证件或户口簿；结束时提醒报价有效期。只说和这一团有关的：没有65岁以上出行人就不提健康申明，不是高原行程就不提高原。已经说过的不重复。
 - **B1 投诉**：先致歉并一句话复述问题，不争辩、不解释、不承诺赔偿、不劝阻投诉。定级：涉及人身安全、正在行程中，或提到12345、12301、文旅/市场监管、消协、媒体或网络平台的为紧急（客服主管2小时内联系），其余一般（24小时内）。采集称呼、电话、原行程大致时间（订单号可选），写投诉转交单，告知主管专人跟进及时限。转交完成前不推介不报价；之后有新需求从 S2 接待，已采集的不再问。
 - **B2 超范围**：出境游、签证、邮轮、单订机票/火车票/高铁票、单订酒店或门票不做，如实说明，不报价不代办；可问客人是否考虑本店国内包价游（不点名产品、不报价）。
 - **B3 团队**：超人数上限、团建、会务、年会转团队业务部，不报价、不采集完整需求；采集称呼、电话、大致人数和时间，写团队转交单，告知1个工作日内联系。个人咨询中顺带的团队需求另行转交，个人咨询照常完成。
 
-### 全程约束 G1–G11（每条消息都要满足）
+### 全程约束 G1–G12（每条消息都要满足）
 
 G1 首条消息自称“海岚旅行AI数字顾问”。G2 每条消息最多两个问题，已给信息不再问。G3 日期照客人说法，不补年份不改日期。G4 交通班次时长、开放时间门票、天气季节、酒店等店外信息只用调研查证并附来源的，注明以出团通知书为准；没查证的说明暂不掌握，会在出团通知书中提供或由同事出发前确认。G5 不编造价格、政策、产品内容；定制游规划通话前不承诺具体酒店和体验；写到具体酒店只写查证过的并标“参考酒店，以计调确认为准”。G6 不承诺余位、折扣、赔偿，不评价其他旅行社。G7 只采集称呼和电话（说明仅用于同事联系您办理），不采集身份证、银行卡、护照号，客人主动发来的不复述不记录，不在聊天中收款。G8 可以提价目表、报价单、电子合同、出团通知书及计调、资深顾问、客服主管、团队业务部；绝不提内部工具、文件、路径、技能、系统、检查清单、进度状态、方案 PPT 的制作方式或任何故障。G9 阶段不跳过；客人一条消息给出全部参数时复述与请确认可合并。
 
@@ -46,22 +48,29 @@
 - 例：10月20日出发 = 6 + 20 = 26天 → 签约时一次付清。
 先在心里算出天数再写消息；报价单、占位申请、方案 payment_terms 和后续告知里的说法必须一致。
 
+**G12 客人只看到每一轮最后的回答**：调用任何工具（read_file、write_file、load_playbook、deliver_files 等）时，content 一个字都不写——写在工具调用旁的文字会被直接发给客人，堆成好几行。不用 message 工具。给客人的话只写在本轮最后一条回答里，每轮一条。“方案 PPT 正在准备，好了马上发给您。”从客人上一条消息以来最多说一次；已经说过、客人也没有新消息，就不再说。交付之后不再说“在准备”。
+
 ## 三、转交单与方案 PPT 的操作步骤
 
 1. **转交单**：先读 uploads/handover-ticket/handover-ticket.md，用 write_file 在工作目录 handover/ 下写 Markdown 文件，文件名“类型-客人称呼-出发月日.md”（称呼按 G10；无出发日期用今天 0924），填全通用字段和该类型的附加字段，承诺时限与告诉客人的一致。同一客人已提供的信息直接沿用。
-2. **启动制作**（仅在客人明确接受、占位申请或规划通话转交单已写好之后）：调用 load_playbook，name 为 plan-deck-delivery，params 全部填上：customer_name（按 G10，客人没说完整称呼写“尊敬的客人”）、departure_city、travel_dates（照客人原话）、nights、group_size（成人数、每位儿童年龄、65岁以上人数）、destination、product_tier、product_doc_path（所选产品说明的路径）、quote_number、quote_sheet（最后一版报价单全文，逐行照抄）、customer_context（客人自己说过的、可作为三条理由和安全提示依据的原话，如特别日子、偏好、老人儿童情况、特殊需求；不写预算和选档规则）、payment_terms（按 G11 算好的付款节点原话；定制游写“规划通话确定价格后签约，付款节点以电子合同为准”）、template_path=uploads/plan-deck-template/template.pptx。然后只告诉客人“方案 PPT 正在准备，好了马上发给您。”，不说明如何制作。
-3. **质检**（制作结果回来后）：只用 read_file 读这三个文字文件：briefs/调研-<报价编号>.md、briefs/需求单-<报价编号>.md、briefs/方案文字-<报价编号>.md（方案逐页文字与字号清单，由制作方交稿时一并导出）。不要用 read_file 打开 .pptx，不要用 exec 或 tool_call 读文件。方案文字清单不存在时算不合格，进返工。再读 uploads/plan-deck-spec/plan-deck-spec.md 第4节，逐项核对：
+2. **启动制作**（仅在客人明确接受、占位申请或规划通话转交单已写好之后）：调用 load_playbook，name 为 plan-deck-delivery，params 全部填上：customer_name（按 G10，客人没说完整称呼写“尊敬的客人”）、departure_city、travel_dates（照客人原话）、nights、group_size（成人数、每位儿童年龄、65岁以上人数，没有写“65岁以上0人”）、destination、product_tier、product_doc_path（所选产品说明的路径）、quote_number、quote_sheet（最后一版报价单全文，逐行照抄）、customer_context（客人自己说过的、可作为三条理由和安全提示依据的原话，如特别日子、偏好、想看什么、怕累、老人儿童情况、特殊需求；不写预算和选档规则）、payment_terms（按 G11 算好的付款节点原话；定制游写“规划通话确定价格后签约，付款节点以电子合同为准”）、template_path=uploads/plan-deck-template/template.pptx。调用时 content 留空；本轮最后的回答把 S5 该告知的（电子合同、付款节点等）说完，末尾只说一次“方案 PPT 正在准备，好了马上发给您。”，不说明如何制作。
+3. **质检**（制作结果回来后）：只用 read_file 读这三个文字文件：briefs/调研-<报价编号>.md、briefs/需求单-<报价编号>.md、briefs/方案文字-<报价编号>.md（方案逐页文字与字号清单，由制作方交稿时一并导出）。不要用 read_file 打开 .pptx，不要用 exec 或 tool_call 读文件。方案文字清单不存在时算不合格，进返工。再读 uploads/plan-deck-spec/plan-deck-spec.md 第4节和所选产品说明，逐项核对：
    - 报价编号、各项价格、全团总价、季节档、人数构成与最后一版报价单完全一致；晚数处处与报价单一致；每日安排天数 = 晚数 + 1，日期未补年份；
    - 称呼与 customer_name 一致，是客人自己说的或“尊敬的客人”，没有猜出来的先生/女士；
    - 所有外部信息都能在调研文件里找到，说法不超出调研原文的适用范围（例如调研写“9月26日代表性排班”，方案不能写成出发当天的班次或“最快的一班”）；来源写真实网站或链接，不写“调研第几节”；
+   - 门票、优免、班次的每一句话，按 group_size 里每位出行人的年龄逐一套用调研原文，结论都一样才算对；方案自己归纳出调研没有的说法算不合格（例：调研写“16岁以下免票”“6至18岁半价”，10岁和15岁的两个孩子两处都能享受，方案写“一人免票、一人半价”就是错的）；
    - 每个酒店名同一处标“参考酒店，以计调确认为准”；
    - 全篇没有“调研”“未查到”“需求单”“质检”“模板”、［］占位或模板示例文字；调研未查到的项目直接不出现，不为它单独留格子填“以出团通知书为准”；
    - 每页最多三个要点、每点不超过三十字；每页字号不超过三级；
-   - “为什么是这条线”只讲客人说过的情况和产品亮点，不讲预算档位、人均每晚、选档规则；
-   - 页序与每页内容符合第2节；下一步写的付款节点与 G11 一致，安全提示齐；文件名“海岚旅行-<报价编号>.pptx”。
-4. **返工**：不合格时调用 load_playbook，name 为 plan-deck-revise，params：quote_number、template_path、deck_path（当前 .pptx 路径）、brief_path、research_path、qc_issues（逐条编号写明不合格项及正确内容）、round（1 或 2）。这一轮给客人的回复只有一句：“方案 PPT 还在准备，好了马上发给您。”不写第几轮、第几页、改了什么、核对什么。最多两轮。两轮后仍不合格：不发给客人，写 handover/质检升级-<称呼>-<出发月日>.md（报价编号、两轮不合格项），告诉客人方案 PPT 将由同事在1个工作日内发送。
-5. **交付**：质检通过后调用一次 deliver_files，files 里只放这份 .pptx（path 与 title，title 用“海岚旅行行程方案”），message 留空。然后最后的回答只写一次交付说明（写明报价编号，例如：“方案 PPT 已经准备好，报价编号 HL-Q-1202-3P，价格和日期都和报价单一致，您可以保存或转发给家人。”），接着做 S7 的后续告知。同一份方案不重复交付，交付说明不重复发。只发链接、只描述内容或承诺稍后发送都不算交付。
+   - “为什么是这条线”三条理由，每条都从客人说过的一句话出发，写出这条线因此怎么安排，并用上产品说明里的亮点（如往返交通、导览、交通卡等，以产品说明为准）；“无高原、无长者”这类凑数的不算理由；不讲预算档位、人均每晚、选档规则；
+   - 每页标题都是结论句，不是栏目名换个说法（例：“费用明细”“费用里已经包含的，和需要您另外准备的”都不合格）；
+   - 出行须知只有和这一团有关的提示：没有65岁以上出行人就不能出现健康申明，不是高原行程就不能出现高原提示；
+   - 下一步与出行须知页有完整的一句“天气、开放时间等以出团通知书为准”，不能只剩“天气以出团通知书为准”；
+   - 页序与每页内容符合第2节；下一步写的付款节点与 G11 一致，安全提示齐；文件名“海岚旅行-<报价编号>.pptx”；
+   - 文字叠压、图标是否对应这一行，清单里看不出来，由制作方逐页看渲染负责；清单或回报里提到叠字、图标不符的，也算不合格。
+4. **返工**：不合格时调用 load_playbook（content 留空），name 为 plan-deck-revise，params：quote_number、template_path、deck_path（当前 .pptx 路径）、brief_path、research_path、qc_issues（逐条编号写明不合格项及正确内容）、round（1 或 2）。这一轮最后的回答：如果从客人上一条消息以来已经说过“方案 PPT 正在准备”，就不要再说，也不写第几轮、第几页、改了什么、核对什么；只补一两条之前还没说过、和这一团有关的 S7 出行须知（如出团通知书出发前3天发送、儿童全程需成人陪同并携带证件；高原或65岁以上提示只在适用时），没有可补的就只说“有需要随时告诉我。”。如果客人这期间发了新消息，先回答客人，可以说一次“方案 PPT 还在准备，好了马上发给您。”。最多两轮。两轮后仍不合格：不发给客人，写 handover/质检升级-<称呼>-<出发月日>.md（报价编号、两轮不合格项），告诉客人方案 PPT 将由同事在1个工作日内发送。
+5. **交付**：质检通过后调用一次 deliver_files（content 留空），files 里只放这份 .pptx（path 与 title，title 用“海岚旅行行程方案”），message 留空。然后最后的回答只写一次交付说明（写明报价编号，例如：“方案 PPT 已经准备好，报价编号 HL-Q-1202-3P，价格和日期都和报价单一致，您可以保存或转发给家人。”），接着只补 S7 里还没说过、和这一团有关的后续告知。不再说“在准备”。同一份方案不重复交付，交付说明不重复发。只发链接、只描述内容或承诺稍后发送都不算交付。
 
 ## 四、对客措辞
 
-客人只看到报价、方案和办理安排。不在消息里写文件名、路径、工具名、同事分工（调研/设计）、模板、质检、返工、页码、核对、转交单、进度状态、报错或故障。制作或等待期间统一说“方案 PPT 还在准备，好了马上发给您。”遇到查不到或做不成的事，只说会由同事跟进及时限。
+客人只看到报价、方案和办理安排。不在消息里写文件名、路径、工具名、同事分工（调研/设计）、模板、质检、返工、页码、核对、转交单、进度状态、报错或故障。制作或等待期间，“方案 PPT 还在准备，好了马上发给您。”从客人上一条消息以来最多说一次。遇到查不到或做不成的事，只说会由同事跟进及时限。
```

`memory.prompt` `agent_memory/profile/soul.md`: modified, +1 -0

```diff
--- a/memory.prompt/agent_memory/profile/soul.md
+++ b/memory.prompt/agent_memory/profile/soul.md
@@ -8,3 +8,4 @@
 - 首条消息必须自称“海岚旅行AI数字顾问”；客人问是不是真人时如实说明：“我是海岚旅行的AI数字顾问，需要时会请同事跟进。”
 - 称呼只用客人自己说的：客人只报了姓，我就称“您”或问一句怎么称呼，绝不自己猜性别加“先生”“女士”“小姐”。
 - 钱的事第一次就说对：告知付款节点前，先用今天和出发日期算出差几天。
+- 客人只看得到我每一轮最后的那条回答：调用工具时我一个字都不写，也不用 message 工具；“方案在准备”从客人上一条消息以来最多说一次。
```

`planning.playbooks` `plan-deck-delivery/nodes/build-deck/requirements.json`: modified, +40 -0

```diff
--- a/planning.playbooks/plan-deck-delivery/nodes/build-deck/requirements.json
+++ b/planning.playbooks/plan-deck-delivery/nodes/build-deck/requirements.json
@@ -58,5 +58,45 @@
     "locations": [],
     "strength": "must_hold",
     "recurrence": 0
+  },
+  {
+    "behavior": "门票、优免、班次等店外事实逐字照需求单和调研原文写，不自己概括成调研里没有的说法。",
+    "observed": "family 一场：第3页把两个孩子都能享受的兵马俑免票和华清宫半价，概括成了“一人免票、一人半价”。",
+    "evidence": ["family 方案第3页：“所以：一人免票、一人半价 / 兵马俑16岁以下免票 / 华清宫6至18岁半价”"],
+    "expectation": "unmet",
+    "acceptance": "文字清单里每一句涉及门票、优免、班次的话都能在需求单或调研里找到意思相同的原文。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
+  },
+  {
+    "behavior": "逐页看渲染：任何文字都不相互叠压，费用页尤其要检查；图标只标明这一行信息的种类、和内容对得上，来源行不配图标。",
+    "observed": "family 一场：第7页右栏的含险合计和报价有效期两行叠在一起；第5页三条来源行分别配了汽车、步行、火车图标。",
+    "evidence": ["family 方案第7页：“含险合计 4,240 元”与“报价有效期 48 小时”重叠", "family 方案第5页：来源行“来源：Klook、GetYourGuide”“来源：gochina、本地宝”“来源：雅高 ALL 官网”前分别是汽车、步行、火车图标"],
+    "expectation": "unmet",
+    "acceptance": "方案每一页渲染出来都没有叠字，每个图标都和它所在那一行讲的内容对得上，来源行没有图标。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
+  },
+  {
+    "behavior": "下一步与出行须知页完整保留“天气、开放时间等以出团通知书为准。”，不删减。",
+    "observed": "family 一场：第9页只写了“天气以出团通知书为准”。",
+    "evidence": ["family 方案第9页：“儿童须成人全程陪同并携带本人证件；天气以出团通知书为准”"],
+    "expectation": "unmet",
+    "acceptance": "文字清单的下一步页有完整的一句“天气、开放时间等以出团通知书为准”。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
+  },
+  {
+    "behavior": "每页标题用需求单写的结论句，不写栏目名；安全提示只放需求单里有的、和本团有关的，不自己添加。",
+    "observed": "family 一场：第8页标题是栏目名换成一句话；第9页给没有长者的一家写了65岁以上健康申明。",
+    "evidence": ["family 方案第8页标题：“费用里已经包含的，和需要您另外准备的”", "family 方案第9页：“线路无高原行程，65岁以上需健康申明，建议购买意外险”"],
+    "expectation": "unmet",
+    "acceptance": "文字清单里每页标题都是结论句，出行须知里没有与本团无关的提示。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
   }
 ]
```

`planning.playbooks` `plan-deck-delivery/nodes/design-brief/requirements.json`: modified, +30 -0

```diff
--- a/planning.playbooks/plan-deck-delivery/nodes/design-brief/requirements.json
+++ b/planning.playbooks/plan-deck-delivery/nodes/design-brief/requirements.json
@@ -38,5 +38,35 @@
     "locations": [],
     "strength": "should",
     "recurrence": 0
+  },
+  {
+    "behavior": "门票、优免、班次照调研原文连同条件写；要写到本团每位出行人身上时，先按人数构成里每个人的年龄逐一对照调研原文，结论一致才写，否则只列原规则；不自己归纳出调研里没有的说法。",
+    "observed": "family 一场：两个孩子（10岁、15岁）兵马俑都免票、华清宫都半价，方案第3页却归纳成“一人免票、一人半价”。",
+    "evidence": ["family 方案第3页：“所以：一人免票、一人半价 / 兵马俑16岁以下免票 / 华清宫6至18岁半价”"],
+    "expectation": "unmet",
+    "acceptance": "需求单对客文案里每一句涉及门票、优免、班次的话，都能在调研结果里找到意思相同的原文，套到本团每个人身上结论不变。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
+  },
+  {
+    "behavior": "三条理由每条写成“客人说过的一句话 → 这条线因此的安排”，用上产品说明里的亮点，不拿“无高原、无长者”凑数；每页标题写结论句，不写栏目名或其换个说法；安全提示只写和本团人数构成、目的地有关的。",
+    "observed": "family 一场：第三条理由只写了“无高原、无长者”，三条都没用产品亮点；第8页标题是栏目名换成一句话；第9页给没有长者的一家写了65岁以上健康申明。",
+    "evidence": ["family 方案第3页：“同行的两位大人无65岁以上长辈 / 所以：无高原、无长者”", "family 方案第8页标题：“费用里已经包含的，和需要您另外准备的”", "family 方案第9页：“线路无高原行程，65岁以上需健康申明，建议购买意外险”"],
+    "expectation": "unmet",
+    "acceptance": "需求单三条理由各自说明一项安排并用到产品说明里的亮点，所有页标题都是结论句，出行须知里没有与本团无关的提示。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
+  },
+  {
+    "behavior": "下一步与出行须知页原样完整写出“天气、开放时间等以出团通知书为准。”，不删减成只写天气。",
+    "observed": "family 一场：第9页只写了“天气以出团通知书为准”。",
+    "evidence": ["family 方案第9页：“儿童须成人全程陪同并携带本人证件；天气以出团通知书为准”"],
+    "expectation": "unmet",
+    "acceptance": "需求单下一步页有完整的一句“天气、开放时间等以出团通知书为准”。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
   }
 ]
```

`planning.playbooks` `plan-deck-delivery/playbook.md`: modified, +14 -8

```diff
--- a/planning.playbooks/plan-deck-delivery/playbook.md
+++ b/planning.playbooks/plan-deck-delivery/playbook.md
@@ -98,18 +98,18 @@
     第一步：用 write_file 把下面“调研结果”原文一字不改地保存为 briefs/调研-${params.quote_number}.md。
     第二步：用 read_file 读产品说明 ${params.product_doc_path}、方案 PPT 规范 uploads/plan-deck-spec/plan-deck-spec.md、品牌与内容设计规范 uploads/brand-design-guide/brand-design-guide.md、模板说明 uploads/plan-deck-template/plan-deck-template.md。
     第三步：用 write_file 写 briefs/需求单-${params.quote_number}.md。需求单分两部分：
-    A. 逐页“对客文案”：按规范第2节页序，每页写出可直接放进 PPT 的中文文字（客人会看到的每一个字）；
+    A. 逐页“对客文案”：按规范第2节页序，每页写出可直接放进 PPT 的中文文字（客人会看到的每一个字），每页先写一句结论标题；
     B. “设计说明”：给设计同事看的版式与规范要求（这部分文字不进 PPT）。
 
     对客文案必须包含：
     - 封面：为${params.customer_name}准备的行程方案（称呼一字不改，不得自己加“先生”“女士”“小姐”）；目的地加“${params.nights}晚”的大标题；日期、人数构成、产品档位；报价编号 ${params.quote_number}；小字“报价方案，以余位确认和电子合同为准”；
     - 行程一览：产品 ${params.product_tier}、目的地 ${params.destination}、出行日期 ${params.travel_dates}（照写，不补年份）、${params.nights}晚、人数构成 ${params.group_size}、出发城市 ${params.departure_city}；
-    - 为什么是这条线：三条理由，每条对应客人自己说过的情况（客人原话：${params.customer_context}），亮点只取自产品说明，不夸大；不得出现预算、人均每晚、档位适用条件或任何选档规则；不写住几晚以外的晚数；
+    - 为什么是这条线：三条理由，每条写成“客人说过的一句话 → 这条线因此的安排”：客人的话取自客人原话（${params.customer_context}），安排必须用上产品说明里写明的亮点（例如往返交通、导览、交通卡等，以产品说明原文为准），不夸大；每条都是一项真实安排，不拿“无高原”“无长者”“没有长辈”之类的话凑数；不得出现预算、人均每晚、档位适用条件或任何选档规则；不写住几晚以外的晚数；
     - 每日安排：共 ${params.nights} 晚、天数等于晚数加一，逐天写“第几天 + 日期”；框架取自产品说明的每日安排框架，只填入调研结果里查证过的线路、交通班次与时长、参考酒店；
     - 费用明细：报价单全部内容逐行照抄（全团总价是全页最大的数字，意外险单列）：
     ${params.quote_sheet}
     - 包含与不含：取自产品说明，各一个短列表；
-    - 下一步与出行须知：经济游、舒适游写计调核实余位、1个工作日内发送电子合同、付款节点（照抄：${params.payment_terms}）、出团通知书出发前3天发送；定制游写资深顾问2个工作日内致电规划；适用的安全提示（高原、65岁以上健康申明与意外险建议、儿童需成人陪同并带证件）；“天气、开放时间等以出团通知书为准”这句只放在这一页；
+    - 下一步与出行须知：经济游、舒适游写计调核实余位、1个工作日内发送电子合同、付款节点（照抄：${params.payment_terms}）、出团通知书出发前3天发送；定制游写资深顾问2个工作日内致电规划；只写适用于本团的安全提示：目的地含玉龙雪山、香格里拉、川西高原才写高原提示，人数构成里有65岁以上出行人才写健康申明与意外险建议，有儿童才写儿童需成人全程陪同并携带证件，不适用的一条都不写；本页必须原样完整写出这一句“天气、开放时间等以出团通知书为准。”（一字不减，不能只写天气），这句只放在这一页；
     - 联系我们页的固定文字按规范第2节原样照抄。
 
     对客文案的硬规则：
@@ -119,9 +119,14 @@
     4) 每一个酒店名，在同一处紧跟“参考酒店，以计调确认为准”；
     5) 每页最多三个要点，每个要点不超过三十个汉字；
     6) 晚数处处写“${params.nights}晚”，与报价单一致；
-    7) 对客文案里不得出现“调研”“未查到”“需求单”“质检”“模板”“设计同事”、［］占位、折扣优惠字样，也不得出现调研结果和店内资料以外的事实。
+    7) 对客文案里不得出现“调研”“未查到”“需求单”“质检”“模板”“设计同事”、［］占位、折扣优惠字样，也不得出现调研结果和店内资料以外的事实；
+    8) 门票、优惠、班次照调研原文写，连同调研写的条件（年龄段、身高、季节、日期）一起写；只有按人数构成 ${params.group_size} 里每一位出行人的年龄逐一对照、结论完全一致时，才可以写到本团每个人身上（例如调研写“16岁以下免票”，10岁和15岁两个孩子都免票，就写“两个孩子都免票”）；没把握就只列原规则，不自己归纳出调研里没有的说法（例如不能写“一人免票、一人半价”）；
+    9) 三条理由每条都是“客人原话 → 本线安排（产品说明里的亮点）”，不写套话，不凑数；
+    10) 每页标题是一句结论，说出这一页要客人记住的一句话（品牌规范：标题写结论，不写名词），不写“行程概览”“费用明细”这类栏目名，也不写栏目名换个说法（例如“费用里已经包含的，和需要您另外准备的”不合格；费用页写“几位全包多少元、意外险怎么算”这样的一句）；
+    11) 安全提示和出行须知只写和本团人数构成、目的地有关的；
+    12) 下一步与出行须知页完整写出“天气、开放时间等以出团通知书为准。”。
 
-    设计说明必须包含：HL-MKT-SPEC-04 第1至3节（格式、页序与每页内容、不得出现）和 HL-MKT-GUIDE-07 的要求（含“不要这样”一栏）的摘要；每页字号最多三级，层次靠留白和细线区分；模板文件路径 ${params.template_path}（每一页都从模板对应页开始，［］占位与示例文字全部替换或删去）；成品文件名 海岚旅行-${params.quote_number}.pptx；交稿时导出逐页文字清单 briefs/方案文字-${params.quote_number}.md。
+    设计说明必须包含：HL-MKT-SPEC-04 第1至3节（格式、页序与每页内容、不得出现）和 HL-MKT-GUIDE-07 的要求（含“不要这样”一栏）的摘要；每页字号最多三级，层次靠留白和细线区分；任何文字不得相互叠压，费用页每一行（含险合计、报价有效期等）各占独立位置、留足行距；图标只用审定图标，只用来标明这一行信息的种类，必须和该行内容对得上（交通行配交通图标、住宿行配住宿图标），来源行不配图标，一行最多一个；标题照对客文案的结论句；模板文件路径 ${params.template_path}（每一页都从模板对应页开始，［］占位与示例文字全部替换或删去）；成品文件名 海岚旅行-${params.quote_number}.pptx；交稿时导出逐页文字清单 briefs/方案文字-${params.quote_number}.md。
     最后的回答：给出需求单全文。
 
     调研结果（已附来源）：
@@ -139,9 +144,10 @@
     设计规范：uploads/plan-deck-spec/plan-deck-spec.md 与 uploads/brand-design-guide/brand-design-guide.md（都在工作目录下，请先读）。
     格式：16:9，全部中文，不使用任何动画和切换效果；每页字号最多三级，层次靠留白和细线区分。
     页序：封面、行程一览、为什么是这条线、（可选章节开篇）、每日安排（天数 = ${params.nights} 晚 + 1，内容多的一天单日页，简单的三天一页，每页不超过三天）、（可选页）、费用明细、包含与不含、下一步与出行须知、联系我们。
-    内容：只用需求单“对客文案”里的文字，“设计说明”部分不进 PPT；不得虚构需求单以外的事实（交通班次、开放时间、门票、天气、酒店、餐厅）；数字与报价单逐项一致，不写折扣优惠；称呼照需求单一字不改，不加先生女士；每个酒店名同一处标“参考酒店，以计调确认为准”；班次保留需求单写明的适用范围，不写“最快”“当天”；来源只写真实网站名或链接；没有内容的项目直接删去，不留格子；每页最多三个要点、每点不超过三十字；晚数处处写“${params.nights}晚”；日期照写，不补年份；全篇不得出现“调研”“未查到”“需求单”“质检”“模板”；不要写 PPT 是怎么做出来的（联系页固定声明除外）；照片只用需求单或调研中注明来源的实拍照片，没有就删去照片位。
-    文件：成品文件名必须是 海岚旅行-${params.quote_number}.pptx，放在工作目录下的 deliverables/ 目录（先确认工作目录的绝对路径，用 ppt_build 的 deliver_to 写入该绝对路径）。完成前逐页检查渲染效果。
-    文字清单：成品完成后，用 exec 运行 Python（python-pptx）打开成品，把每一页的页号、该页所有文本框的全部文字、该页各段落实际使用的字号（磅；取不到显式字号的写“继承”）写成 Markdown，保存为工作目录下的 briefs/方案文字-${params.quote_number}.md。然后自查这份清单：出现“调研”“未查到”“需求单”“质检”“模板”“［”“］”、酒店名旁没有“以计调确认为准”、某页超过三种字号或超过三个要点、晚数与报价单不一致，都先改好成品再重新导出清单。
+    内容：只用需求单“对客文案”里的文字，“设计说明”部分不进 PPT；标题、理由、门票优惠和班次等事实逐字照需求单写，不重新概括、不自己归纳（例如不能把门票规则归纳成“一人免票、一人半价”）；每页标题用需求单写的结论句，不换成栏目名；不得虚构需求单以外的事实（交通班次、开放时间、门票、天气、酒店、餐厅）；数字与报价单逐项一致，不写折扣优惠；称呼照需求单一字不改，不加先生女士；每个酒店名同一处标“参考酒店，以计调确认为准”；班次保留需求单写明的适用范围，不写“最快”“当天”；来源只写真实网站名或链接；没有内容的项目直接删去，不留格子；安全提示只放需求单里有的，不自己添加；下一步与出行须知页完整保留“天气、开放时间等以出团通知书为准。”这一句，一字不减；每页最多三个要点、每点不超过三十字；晚数处处写“${params.nights}晚”；日期照写，不补年份；全篇不得出现“调研”“未查到”“需求单”“质检”“模板”；不要写 PPT 是怎么做出来的（联系页固定声明除外）；照片只用需求单或调研中注明来源的实拍照片，没有就删去照片位。
+    版面：任何文字都不能相互叠压，费用页每一行（含险合计、报价有效期等）各占独立位置、留足行距；图标只用来标明这一行信息的种类，必须和这一行讲的内容对得上，来源行不配图标，一行最多一个。完成前逐页打开渲染图检查叠字、溢出和图标，费用页重点看，发现问题改 build.py 重新导出再看。
+    文件：成品文件名必须是 海岚旅行-${params.quote_number}.pptx，放在工作目录下的 deliverables/ 目录（先确认工作目录的绝对路径，用 ppt_build 的 deliver_to 写入该绝对路径）。
+    文字清单：成品完成后，导出逐页文字与字号清单 briefs/方案文字-${params.quote_number}.md（有 deck_text_audit 工具时用它导出并自查，quote_number=${params.quote_number}，salutation=${params.customer_name}，nights=${params.nights}；否则用 exec 运行 Python（python-pptx）打开成品，把每一页的页号、该页所有文本框的全部文字、该页各段落实际使用的字号（磅；取不到显式字号的写“继承”）写成 Markdown）。然后自查这份清单：出现“调研”“未查到”“需求单”“质检”“模板”“［”“］”、酒店名旁没有“以计调确认为准”、某页超过三种字号或超过三个要点、晚数与报价单不一致、需求单没有的归纳说法、下一步页缺“天气、开放时间等以出团通知书为准”，都先改好成品再重新导出清单。
     最后的回答：写明成品 .pptx 的绝对路径、页数和文字清单的路径。
 
     需求单：
```

`planning.playbooks` `plan-deck-revise/nodes/revise-deck/requirements.json`: modified, +40 -0

```diff
--- a/planning.playbooks/plan-deck-revise/nodes/revise-deck/requirements.json
+++ b/planning.playbooks/plan-deck-revise/nodes/revise-deck/requirements.json
@@ -48,5 +48,45 @@
     "locations": [],
     "strength": "must_hold",
     "recurrence": 0
+  },
+  {
+    "behavior": "返工时门票、优免、班次等店外事实逐字照需求单和调研原文写，不自己概括成调研里没有的说法。",
+    "observed": "family 一场：第3页把两个孩子都能享受的兵马俑免票和华清宫半价，概括成了“一人免票、一人半价”。",
+    "evidence": ["family 方案第3页：“所以：一人免票、一人半价 / 兵马俑16岁以下免票 / 华清宫6至18岁半价”"],
+    "expectation": "unmet",
+    "acceptance": "返工后文字清单里每一句涉及门票、优免、班次的话都能在调研里找到意思相同的原文。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
+  },
+  {
+    "behavior": "返工后逐页看渲染：任何文字都不相互叠压，费用页尤其要检查；图标和这一行内容对得上，来源行不配图标。",
+    "observed": "family 一场：第7页右栏的含险合计和报价有效期两行叠在一起；第5页三条来源行分别配了汽车、步行、火车图标。",
+    "evidence": ["family 方案第7页：“含险合计 4,240 元”与“报价有效期 48 小时”重叠", "family 方案第5页：来源行前分别是汽车、步行、火车图标"],
+    "expectation": "unmet",
+    "acceptance": "返工后每一页渲染都没有叠字，图标与所在行内容一致，来源行没有图标。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
+  },
+  {
+    "behavior": "返工后下一步与出行须知页完整保留“天气、开放时间等以出团通知书为准。”。",
+    "observed": "family 一场：第9页只写了“天气以出团通知书为准”。",
+    "evidence": ["family 方案第9页：“儿童须成人全程陪同并携带本人证件；天气以出团通知书为准”"],
+    "expectation": "unmet",
+    "acceptance": "返工后文字清单的下一步页有完整的一句“天气、开放时间等以出团通知书为准”。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
+  },
+  {
+    "behavior": "返工后每页标题是结论句，不写栏目名；安全提示只放和本团有关的。",
+    "observed": "family 一场：第8页标题是栏目名换成一句话；第9页给没有长者的一家写了65岁以上健康申明。",
+    "evidence": ["family 方案第8页标题：“费用里已经包含的，和需要您另外准备的”", "family 方案第9页：“线路无高原行程，65岁以上需健康申明，建议购买意外险”"],
+    "expectation": "unmet",
+    "acceptance": "返工后文字清单里每页标题都是结论句，出行须知里没有与本团无关的提示。",
+    "locations": [],
+    "strength": "must_hold",
+    "recurrence": 1
   }
 ]
```

`planning.playbooks` `plan-deck-revise/playbook.md`: modified, +4 -3

```diff
--- a/planning.playbooks/plan-deck-revise/playbook.md
+++ b/planning.playbooks/plan-deck-revise/playbook.md
@@ -59,9 +59,10 @@
     只修正下列不合格项，其余页面与内容保持不变：
     ${params.qc_issues}
 
-    要求：仍以模板 ${params.template_path} 为底，版式、配色、字体、图标照模板；不使用动画和切换效果；每页字号最多三级；每页最多三个要点、每点不超过三十字；只用需求单“对客文案”和调研结果里的内容，不虚构任何事实；称呼照需求单一字不改，不加先生女士；班次等外部事实保留调研写明的适用范围，不写“最快”“当天”；来源只写真实网站名或链接；每个酒店名同一处标“参考酒店，以计调确认为准”；调研未查到的项目直接删去，不留格子；晚数与报价单一致；全篇不得出现“调研”“未查到”“需求单”“质检”“模板”，［］占位和模板示例文字不得残留。
-    文件：成品仍命名为 海岚旅行-${params.quote_number}.pptx，写到工作目录 deliverables/ 目录（用 ppt_build 的 deliver_to 写入绝对路径，覆盖原文件）。完成前逐页检查渲染效果。
-    文字清单：改完后用 exec 运行 Python（python-pptx）重新导出每一页的页号、全部文字和各段落实际字号（磅；取不到显式字号的写“继承”），覆盖保存为 briefs/方案文字-${params.quote_number}.md，并按上面的要求自查，有问题先改好再导出。
+    要求：仍以模板 ${params.template_path} 为底，版式、配色、字体、图标照模板；不使用动画和切换效果；每页字号最多三级；每页最多三个要点、每点不超过三十字；只用需求单“对客文案”和调研结果里的内容，不虚构任何事实；标题、理由、门票优惠和班次等事实逐字照需求单和调研原文写，不重新概括、不自己归纳（例如不能写“一人免票、一人半价”这种调研里没有的说法）；每页标题是结论句，不写栏目名；称呼照需求单一字不改，不加先生女士；班次等外部事实保留调研写明的适用范围，不写“最快”“当天”；来源只写真实网站名或链接；每个酒店名同一处标“参考酒店，以计调确认为准”；调研未查到的项目直接删去，不留格子；安全提示只放和本团有关的（没有65岁以上出行人不写健康申明，不是高原行程不写高原）；下一步与出行须知页完整写出“天气、开放时间等以出团通知书为准。”；晚数与报价单一致；全篇不得出现“调研”“未查到”“需求单”“质检”“模板”，［］占位和模板示例文字不得残留。
+    版面：任何文字都不能相互叠压，费用页每一行（含险合计、报价有效期等）各占独立位置；图标只用来标明这一行信息的种类，必须和这一行内容对得上，来源行不配图标。改完逐页打开渲染图检查叠字和图标，费用页重点看。
+    文件：成品仍命名为 海岚旅行-${params.quote_number}.pptx，写到工作目录 deliverables/ 目录（用 ppt_build 的 deliver_to 写入绝对路径，覆盖原文件）。
+    文字清单：改完后重新导出每一页的页号、全部文字和各段落实际字号（有 deck_text_audit 工具时用它，salutation 与 nights 取需求单里的称呼和晚数；否则用 exec 运行 Python（python-pptx）；磅；取不到显式字号的写“继承”），覆盖保存为 briefs/方案文字-${params.quote_number}.md，并按上面的要求自查，有问题先改好再导出。
     最后的回答：写明修改后 .pptx 的绝对路径、页数、文字清单路径，以及每个不合格项是怎么改的。
   dependsOn: []
   inputs: {}
```

### Curation `873796d2ac8b494e9719c2cc323204f3.Raven` (scope Raven): installed

Understanding (candidate):

> 我的理解：这位同事负责整理交给设计同事的需求单。设计同事会把需求单里的“对客文案”原样放进方案 PPT，所以需求单里出的错，客人最后都会看到。
>
> 上一轮这一步已经加了自动核对，而且确实起了作用：需求单写进文件前被退回两次，改完才放行。封面称呼、“调研”“未查到”这类字样、酒店标注、代表性排班的写法，这一轮都改好了。但这套核对有两个问题：
>
> 一是有几处误判，退回理由也写偏了：
> - 费用页“加晚 260 元 × 4 人 × 1 晚”这一行，被当成晚数写错。
> - “为什么是这条线”要求引用客人原话，引用本身让要点变长，因此被判超字数。
> - 需求单里夹着给设计同事的版式备注，比如“模板自带组合标志”“称呼照写、不加先生女士”，本该让它移到设计说明，退回理由却只说“删去字样”。
>
> 二是您这次指出、方案里仍然出错的几件事，核对完全没有覆盖：
> - 把“两个孩子都免票、都半价”归纳成“一人免票、一人半价”；
> - 拿“无高原、无长者”凑理由；
> - 页标题是栏目名换个说法；
> - 给没有长者的一家写 65 岁以上健康申明；
> - 下一步页只写了“天气以出团通知书为准”。
>
> 这位同事以后的做法：需求单写进文件前、最后交出全文前，仍先自动核对“对客文案”，有问题就逐条用中文说明哪一句错、应怎么改，退回重写，一轮最多退回四次。这次在原有核对基础上补上以下几项：
> 1. 门票、优免：凡写成“一人免票”“两个孩子都半价”这类落到具体人数的说法，按人数构成里每个孩子的年龄，逐条套调研原文的年龄条件。算出来的结论和写的不一致，就退回，并直接告诉它按调研原文算出的正确结论（例如 10 岁和 15 岁两个孩子都符合“16岁以下免票”）。算不出来的，要求只列调研原规则。
> 2. 理由：出现“无高原”“无长者”“没有长辈”之类的凑数理由，就退回。每条理由的安排都要用上产品说明里写明的亮点。
> 3. 页标题：写成“费用明细”“行程一览”这类栏目名，或“费用里已经包含的，和需要您另外准备的”这种换个说法的，就退回。费用页标题要写出全团总价这一句结论。
> 4. 安全提示：团里没有 65 岁以上的，不许出现健康申明；不是高原线路的，不许提高原；没有儿童的，不写儿童陪同。反过来，适用的提示缺了也要补上。
> 5. 下一步页必须有完整的一句“天气、开放时间等以出团通知书为准。”。
>
> 同时去掉上面几处误判：费用页的算式行不查晚数；理由里引用的客人原话不计入字数；备注类文字只提示移到设计说明。所有核对只依据任务说明里给出的称呼、晚数、人数构成、目的地、调研原文和它读到的产品说明，不增加任何店外信息。读不出某项信息时，就跳过依赖它的那项核对，不乱判。
>
> 仍不确定的地方：
> - 我手上只有您的评语和核对留下的简短记录，看不到这位同事上一轮写的需求单全文。所以“一人免票、一人半价”是需求单里就写错了，还是制作 PPT 时才概括出来的，目前无法确认。这一步能保证的只是需求单本身不这样写。
> - 需求单实际怎样分页、怎样写标题，要等下一轮记录来验证。
> - 叠字、图标和行内容对不上，属于制作 PPT 那一步，这里管不到。

Design:

> Scope: revise the only authored child binding, action.strategy. Keep the binding values unchanged: factory brief_action:create, proposal brief_action:to_proposal, decision brief_action:to_review; failure/reply stay unbound. Keep the public types and their field names (NodeParams extended with optional fields, BriefProposal, BriefFailure, ActionDecision). Keep the cap: MAX_REVISIONS=4, applied to StepView.rollbacks. Keep the correction/inject path and the checkpoint keys. The module is split so that each file stays at a few hundred lines:
> - brief_parse.py: prompt/transcript parsing and part-A segmentation; pure functions.
> - brief_checks.py: all deterministic checks; the pure function problems(proposal) -> list[str].
> - brief_action.py: pydantic types, BriefActionStrategy(ActionStrategy[BriefProposal, BriefFailure, ActionDecision]), create, and the synchronous translations to_proposal/to_review.
> The strategy remains the single owner of the rules. It calls brief_checks.problems.
>
> Invocation path (unchanged, confirmed by mechanism_activity: Raven scope, 4 assess, 2 revise then accept):
> - ParticipantHook "curator-action" review at execute_tools. to_proposal picks the first write_file whose path matches 需求单-*.md (arguments are a native dict; a str is tolerated via json.loads) → artifact="file".
> - Review at after_iteration for a text response without tool_calls → artifact="answer" (this becomes design-brief.output, which build-deck consumes).
> - Resample at execute_tools pops the proposal before the write runs. At after_iteration it replaces the draft before persistence.
>
> NodeParams additions, parsed from step.question, or else the first user transcript row containing 准备的行程方案 and 需求单. None means unparsed and is never guessed:
> - group_size: str|None. Regex r"人数构成\s*(.+?)、出发城市", falling back to r"按人数构成\s*(.+?)\s*里每一位出行人".
> - destination: str|None. Regex r"目的地\s*(.+?)、出行日期".
> - child_ages: list[int]|None. From group_size: all r"(\d{1,2})\s*(?:周)?岁(?!以上|以下)" values <18. If there are none and group_size has no 儿童/孩子/小孩 (or has 无儿童/没有儿童/儿童0), the value is []. If it has 儿童/孩子 without ages, the value is None.
> - elders: int|None. r"65岁以上\s*(\d+|[零一二两三四五六七八九十])" → number. 无/没有65岁以上 → 0. Absent → None.
> - product_doc: str (may be ""). Get the path from r"读产品说明\s*(\S+?\.md)". Map transcript assistant rows' wire tool_calls (function.name == read_file; function.arguments is a JSON string containing path) to their ids. The product text is the content of the tool row whose tool_call_id matches.
> - needs_notice_sentence: bool = the prompt contains 天气、开放时间等以出团通知书为准.
> Existing fields: customer_name, nights, quote_number, research, structured. allowed_text is unchanged (research plus all tool-result text).
>
> Segmentation fix (brief_parse.sections):
> - Part A: the existing _part_a logic.
> - A page heading is a markdown heading, a bold-only line, a 【…】 line, or a page-label line (第N页 / P\d / N. at line start) that contains a page-name keyword or a page label. Kind comes from keywords: 封面 cover; 一览/概览 overview; 为什么 why; 每日安排 days; 费用 fee (checked before 包含); 包含/不含 include; 下一步/出行须知 next; 联系 contact. A page label without a keyword → other.
> - A short line matching 第N天 starts a day unit (kind day).
> - Any other heading-looking line (e.g. #### 右栏, **左栏**) is a sub-heading. It inherits the current page kind instead of resetting to other. This fixes the fee-line false positive.
> - Title lines (r"(?<![大副小])(?:结论|本页|页)?标题\s*[:：]") are body lines, never headings.
>
> Note lines: a part-A line counts as a note when it is wholly parenthetical or starts with 注/备注/说明/设计, and it contains design vocabulary (模板, 版式, 字号, 图标, 左上角, 右侧, 保持不动, 照写, 不加, 替换, 删去, 未查证, 未查到). Each note line yields one problem: “对客文案里夹了内部备注（原句…），客人会看到；移到B设计说明或删去，对客文案只放客人看到的字”. The line is then excluded from the other checks, which removes the 模板 and 先生 false positives while still enforcing the rule.
>
> Revised existing checks:
> 1. Internal words: the message now depends on the word. 模板/设计同事/需求单/质检 → “属于给设计同事的话，移到设计说明”. 调研/未查到/未查证 → the existing source/omit message.
> 2. Honorific: unchanged; note lines are excluded.
> 3. Nights: skip fee-kind lines and any line containing 加晚, ×, x or 元/人. The day-count check is unchanged.
> 4. Selection rules: unchanged.
> 5. Judgement words: unchanged.
> 6. 以出团通知书为准 outside the next page: unchanged, but only when a next page was detected (otherwise skipped, to avoid mass flags).
> 7. Hotel labels: strip 来源… segments before matching; add 官方, 官网, 预订, 该, 此, 本, 所选 to the generic prefix ends.
> 8. Bullets: counted only for why/day, as before. The length check excludes title lines and source-only lines. On the why page it also removes text inside “”「」"" and a leading 客人说/客人提到 before counting Han characters (still ≤30).
>
> New checks (brief_checks). Each is skipped when its parameter is None or empty:
> A. Ticket generalizations. Needs research and child_ages. Split part A into clauses at ，,。；;、/|\n. For each 免票/免费/半价 occurrence, look at the ≤8 chars before it:
> - A count phrase ((一|两|二|三|四|五|\d)\s*(个|位|名)?\s*(人|孩子|小孩|儿童|娃)) gives claimed = n.
> - 都/均 with 孩子/儿童 and no number gives claimed = len(child_ages).
> - No claim: the clause is a rule listing and is ignored.
> A claim whose compacted clause appears verbatim in the compacted research is accepted. Otherwise, parse research age rules for that term:
> - free: r"(\d+)\s*(?:周)?岁\s*(及以下|以下|以内)[^。；\n]{0,12}?(免票|免费)"; age < N, or ≤ N with 及以下.
> - half: r"(\d+)\s*(?:周)?岁?\s*(?:至|到|-|—|~|～)\s*(\d+)\s*(?:周)?岁[^。；\n]{0,12}?(半价|优惠)"; lo ≤ age ≤ hi.
> If every parsed rule for the term gives the same eligible count e and e == claimed, accept. If e is computable and differs, flag with the computed fact, e.g. “按调研原文“16岁以下免票”，本团10岁、15岁的孩子都符合，不能写“一人免票”；照原文连同条件写，或写“两个孩子都可免票””. If it is not computable (no age rule, e.g. height-based, or rules disagree), flag “调研原文没有这种按人头的归纳，只列调研原规则连同条件”.
> B. Filler reasons, anywhere in part A: r"(无|没有|不含|不涉及)\s*(65岁以上)?的?(长辈|长者|老人|高原)" → “这不是理由，删去；三条理由每条写“客人原话 → 本线安排”，安排用上产品说明里的亮点”.
> C. Product highlights (needs product_doc and a why page). Reason lines are the why-page bullet or numbered lines, or lines containing →, with titles, sources and notes excluded, and with quoted customer text removed. A reason with no shared 4-Han-char substring with product_doc → “第N条理由没用上产品说明里写明的安排（如往返交通、导览、交通卡等，以产品说明原文为准）”. If the why page has fewer than 3 reason lines → “为什么是这条线要写满三条理由”.
> D. Titles. For title lines on pages other than cover/contact, take the value after the colon and strip **“”《》 and punctuation. Normalize by removing 的/与/和/及/、/spaces, then compare with the column-name set {封面, 行程一览, 行程概览, 行程总览, 为什么是这条线, 为什么选这条线, 每日安排, 行程安排, 费用明细, 费用说明, 费用, 包含不含, 费用包含不含, 下一步出行须知, 下一步, 出行须知, 温馨提示}. A match, or the restatement pattern r"包含.{0,8}(和|与|及|，|,).{0,12}(另外|自理|不含|自付|自备)" with no digit → “标题是栏目名（换个说法），改成这一页要客人记住的一句结论”. A fee-page title without a digit → “费用页标题写一句带全团总价的结论（几位全包多少元、意外险怎么算）”.
> E. Group-relevant safety notes. Notes are excluded, and group_size occurrences are removed first.
> - elders == 0 and a line has 健康申明, or 65岁 not followed by 以上0/以上 0 → flag “本团没有65岁以上出行人，删去这条”. elders > 0 and no 健康申明 anywhere → flag “缺65岁以上健康申明与意外险建议”.
> - plateau = destination or day-page text contains 玉龙雪山/香格里拉/川西. If destination is parsed, plateau is false, and 高原 appears → flag “本线不是高原行程，删去高原字样” (merged with B when the same line). If plateau is true and there is no 高原 on the next page → flag.
> - child_ages == [] and 陪同/户口簿/儿童须 appears → flag. child_ages non-empty and no 陪同 → flag “缺儿童全程需成人陪同并携带证件”.
> F. Notice sentence. If needs_notice_sentence and the compacted part A (， normalized to 、) lacks 天气、开放时间等以出团通知书为准 → flag “下一步与出行须知页要原样完整写出“天气、开放时间等以出团通知书为准。”，不能只写天气”.
> Problems are de-duplicated. MAX_PROBLEMS rises from 12 to 14, ordered so that must_hold items (A, B, D, E, F, internal words, honorific) come before bullet-length items and survive the truncation.
>
> assess/recover and to_review are unchanged in semantics:
> - assess increments reviews. No problems → accept. rollbacks ≥ 4 → accept with capped=True (capped counter incremented). Otherwise revise with the numbered correction “【内部核对提示】…请只改这些地方…”; the file vs answer variants are unchanged.
> - recover returns defer (unbound).
> - to_review maps accept → ReviewResult(verdict="accept"), revise → ReviewResult(verdict="resample", reason, inject=[user correction]), defer → None.
>
> Failure behaviour:
> - Parsing failures skip the dependent checks.
> - Exceptions inside translations or checks are swallowed by native composition. They mean no enforcement for that step, not success. To limit this, each check runs inside a local try/except that records nothing and skips that check, so one faulty regex cannot disable the others.
> - The cap prevents blocking the DAG.
> - Other participants (charter, inbound adapter, observer) and the root's prompt/playbook are untouched. This child cannot change build-deck or the root QC.

| Target | Facet | Reason | Expected | Criteria |
|---|---|---|---|---|
| action.strategy | action | mechanism_activity (family, Raven scope) shows that the brief review ran and resampled twice, but three kinds of misfire occurred. A fee line "260 元 × 4 人 × 1 晚" was flagged as a nights error, because sub-headings reset the page kind. Why-page bullets that quote the customer were flagged as over-length. Design notes inside part A (模板…, 不加"先生") were flagged with a misleading "delete the word" message. Meanwhile, five must_hold requirements that recurred (expectation unmet, recurrence 1) have no check at all: per-person ticket generalizations like 一人免票、一人半价; filler reasons such as 无高原/无长者; column-name titles; safety notes irrelevant to the group (65岁健康申明 for a family without elders); and the truncated 天气以出团通知书为准. | A 需求单 write_file or final brief answer whose part A contains any of the following is resampled before it takes effect: a per-person 免票/半价 claim that contradicts, or cannot be derived from, the research age rules applied to each child age; 无高原/无长者/没有长辈; a reason with no product-doc highlight or fewer than 3 reasons; a column-name or restated title, or a fee title without a number; 健康申明 when elders=0, 高原 on a non-plateau destination, 陪同 when there are no children, or a missing applicable note; a missing full sentence 天气、开放时间等以出团通知书为准. The correction names the sentence and states the computed correct fact where one exists. Fee arithmetic lines, quoted customer words and correctly placed design notes no longer cause false revisions; notes are redirected to 设计说明. Earlier protections, the 4-revision cap and part B exemption stay as they were. | intake-and-confirmation (round), no-internal-chatter (round), deck-no-forbidden-content (round), deck-content-rules (round), deck-structure (round), deck-aesthetics (round) |

Validation: errors none; bound targets action.strategy.

`files` `brief_action.py`: modified, +28 -380

```diff
--- a/files/brief_action.py
+++ b/files/brief_action.py
@@ -1,207 +1,33 @@
-"""Deterministic check of the customer-facing part of the Hailan design brief.
+"""Action strategy reviewing the customer-facing part of the Hailan design brief.
 
-Leaf child "Raven" runs node plan-deck-delivery/design-brief. Its brief part A
-(对客文案) is copied into the customer deck by the build-deck node, so this
-strategy reviews the proposed write_file of briefs/需求单-*.md and the final
-brief answer before they take effect. Part B (设计说明) is not policed.
+Leaf child "Raven" runs node plan-deck-delivery/design-brief. Part A (对客文案)
+is copied into the customer deck, so the proposed write_file of
+briefs/需求单-*.md and the final brief answer are checked before they take
+effect. Rules live in brief_checks; parsing in brief_parse.
 
-State: the session action checkpoint (diagnostic counters only), mutated only
-in ``assess``. Translations are synchronous and read the StepView only.
+State: the session action checkpoint (diagnostic counters), mutated only in
+``assess``. Translations are synchronous and read the StepView only.
 """
 
 from __future__ import annotations
 
 import json
 import re
-from typing import Any, Literal
-
-from pydantic import BaseModel, Field
+from typing import Any
 
 from experimental.curator.harness.state import Task
 from experimental.curator.harness.strategies import ActionStrategy
 from experimental.curator.raven_adapter.targets.action import ReviewResult
 from raven.contracts.participant import StepView
 
+from .brief_checks import problems as brief_problems
+from .brief_parse import allowed_text, find_prompt, parse_params
+from .brief_types import ActionDecision, BriefFailure, BriefProposal, NodeParams
+
 MAX_REVISIONS = 4
-MAX_PROBLEMS = 12
-MAX_BULLETS = 3
-MAX_HAN = 30
-LABEL = "参考酒店，以计调确认为准"
+BRIEF_PATH_RE = re.compile(r"需求单-[^/]*\.md$")
 
-BRIEF_PATH_RE = re.compile(r"需求单-[^/]*\.md$")
-NAME_RES = (
-    re.compile(r"为(.+?)准备的行程方案（称呼一字不改"),
-    re.compile(r"客人称呼：(.+?)（封面写"),
-)
-NIGHTS_RES = (
-    re.compile(r"共\s*(\d+)\s*晚、天数等于晚数加一"),
-    re.compile(r"目的地加“(\d+)晚”"),
-)
-QUOTE_RE = re.compile(r"需求单-(HL-Q-[0-9A-Za-z-]+?)\.md")
-RESEARCH_MARK = "调研结果（已附来源）："
-
-CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
-NIGHT_RE = re.compile(r"(?<![第加\d一二两三四五六七八九十])(\d+|[一二两三四五六七八九十])\s*晚(?!餐|饭|上|间|宴|会)")
-DAY_RE = re.compile(r"第\s*(\d+|[一二三四五六七八九十]+)\s*天")
-INTERNAL_WORDS = ("调研", "未查到", "未查证", "需求单", "质检", "模板", "设计同事", "［", "］")
-RULE_WORDS = ("人均每晚", "档位适用", "适用条件", "选档", "适用人均")
-JUDGEMENT_WORDS = ("最快", "最早", "当天", "每天")
-NOTICE_RE = re.compile(r"以出团通知书?为准")
-HOTEL_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9·]{2,12})(酒店|宾馆|客栈|青年旅舍|青旅|民宿|饭店)")
-GENERIC_ENDS = (
-    "型", "星级", "星", "精品", "参考", "豪华", "特色", "同级", "高档", "连锁", "含", "住宿", "晚", "的", "等",
-    "区", "内", "附近", "周边", "当地", "景区", "舒适", "经济", "入住", "住", "或", "及", "和", "为",
-)
-BULLET_RE = re.compile(r"^\s*(?:[-*•·]\s+|\d+[.、)）]\s*)")
-HEADING_RE = re.compile(r"^\s*(?:#+\s*\S|\*\*[^*]+\*\*[:：]?\s*$|【[^】]+】\s*$|第?\s*\d+\s*页|P\d+\b)")
-SOURCE_RE = re.compile(r"[（(]?\s*来源[:：][^）)\n]*[）)]?")
-HAN_RE = re.compile(r"[\u4e00-\u9fff]")
-
-
-class NodeParams(BaseModel):
-    """Values from the rendered node prompt; None means not parsed, never guessed."""
-
-    customer_name: str | None = None
-    nights: int | None = None
-    quote_number: str | None = None
-    research: str = ""
-    structured: bool = False
-
-
-class BriefProposal(BaseModel):
-    """A brief about to be written (execute_tools) or answered (after_iteration)."""
-
-    phase: Literal["execute_tools", "after_iteration"]
-    artifact: Literal["file", "answer"]
-    text: str
-    path: str | None = None
-    params: NodeParams
-    allowed_text: str = ""
-    rollbacks: int = 0
-
-
-class BriefFailure(BaseModel):
-    """Answerless turn; the failure path is intentionally not bound."""
-
-    reason: str = ""
-
-
-class ActionDecision(BaseModel):
-    kind: Literal["accept", "revise", "defer"]
-    problems: list[str] = Field(default_factory=list)
-    correction: str | None = None
-    capped: bool = False
-
-
-# ---------- text helpers ----------
-
-
-def _snip(text: str, limit: int = 30) -> str:
-    text = re.sub(r"\s+", " ", text).strip()
-    return text if len(text) <= limit else text[:limit] + "…"
-
-
-def _compact(text: str) -> str:
-    return re.sub(r"\s+", "", text)
-
-
-def _num(token: str) -> int | None:
-    if token.isdigit():
-        return int(token)
-    if token in CN_NUM:
-        return CN_NUM[token]
-    if len(token) == 2 and token[0] == "十":
-        return 10 + CN_NUM.get(token[1], 0)
-    if len(token) == 2 and token[1] == "十":
-        return CN_NUM.get(token[0], 0) * 10
-    if len(token) == 3 and token[1] == "十":
-        return CN_NUM.get(token[0], 0) * 10 + CN_NUM.get(token[2], 0)
-    return None
-
-
-def _has_label(line: str) -> bool:
-    c = _compact(line)
-    return LABEL in c or "参考酒店,以计调确认为准" in c
-
-
-def _part_a(text: str, structured: bool) -> list[str] | None:
-    """Lines of part A (对客文案), excluding its own marker line; None if missing."""
-    lines = text.splitlines()
-    if structured:
-        start = None
-        for i, line in enumerate(lines):
-            if "对客文案" in line and "设计说明" not in line and len(line.strip()) <= 40:
-                start = i
-                break
-        if start is None:
-            for i, line in enumerate(lines):
-                if "对客文案" in line:
-                    start = i
-                    break
-        if start is None:
-            return None
-        end = len(lines)
-        for j in range(start + 1, len(lines)):
-            if "设计说明" in lines[j] and len(lines[j].strip()) <= 40:
-                end = j
-                break
-        return lines[start + 1 : end]
-    out: list[str] = []
-    for line in lines:
-        if "规范要求摘要" in line or "设计说明" in line:
-            break
-        if "模板文件路径" in line or "成品文件名" in line:
-            continue
-        out.append(line)
-    return out
-
-
-def _kind(heading: str) -> str:
-    if "费用" in heading:
-        return "fee"
-    if "联系" in heading:
-        return "contact"
-    if "下一步" in heading or "出行须知" in heading:
-        return "next"
-    if "为什么" in heading:
-        return "why"
-    if DAY_RE.search(heading):
-        return "day"
-    if "每日安排" in heading:
-        return "days"
-    if "包含" in heading or "不含" in heading:
-        return "include"
-    return "other"
-
-
-def _is_heading(line: str) -> bool:
-    if BULLET_RE.match(line):
-        return False
-    if HEADING_RE.match(line):
-        return True
-    stripped = line.strip()
-    return bool(DAY_RE.match(stripped)) and len(stripped) <= 25
-
-
-def _sections(lines: list[str]) -> list[tuple[str, str, list[str]]]:
-    """(kind, heading, body lines) in order."""
-    sections: list[tuple[str, str, list[str]]] = [("other", "", [])]
-    for line in lines:
-        if _is_heading(line):
-            sections.append((_kind(line), line.strip(), []))
-        else:
-            sections[-1][2].append(line)
-    return sections
-
-
-def _han_len(line: str) -> int:
-    cleaned = SOURCE_RE.sub("", line)
-    cleaned = cleaned.replace(LABEL, "").replace("参考酒店,以计调确认为准", "")
-    cleaned = re.sub(r"https?://\S+", "", cleaned)
-    return len(HAN_RE.findall(cleaned))
-
-
-# ---------- strategy ----------
+__all__ = ["ActionDecision", "BriefFailure", "BriefProposal", "NodeParams", "create", "to_proposal", "to_review"]
 
 
 class BriefActionStrategy(ActionStrategy[BriefProposal, BriefFailure, ActionDecision]):
@@ -213,136 +39,19 @@
         state.setdefault("capped", 0)
         state.setdefault("last_problems", [])
         state.setdefault("last_artifact", None)
-
-    def _problems(self, proposal: BriefProposal) -> list[str]:
-        params = proposal.params
-        lines = _part_a(proposal.text, params.structured)
-        if lines is None:
-            if proposal.artifact == "answer":
-                return ["最后的回答必须给出需求单全文，含 A 对客文案与 B 设计说明两部分"]
-            return ["需求单必须分 A 对客文案 / B 设计说明两部分，并标出“对客文案”部分"]
-        problems: list[str] = []
-
-        def add(item: str) -> None:
-            if item not in problems:
-                problems.append(item)
-
-        sections = _sections(lines)
-        all_rows = [(kind, heading, line) for kind, heading, body in sections for line in [heading, *body] if line]
-
-        # 1. internal words
-        for kind, _heading, line in all_rows:
-            for word in INTERNAL_WORDS:
-                if word == "模板" and kind == "contact":
-                    continue
-                if word in line:
-                    add(
-                        f"对客文案出现内部字样“{word}”（原句：{_snip(line)}），客人会看到；删去该字样，"
-                        "来源只写调研里列出的真实网站名或链接，未查到的项目整项不写"
-                    )
-
-        # 2. honorific
-        name = params.customer_name
-        if name:
-            joined = "\n".join(line for _k, _h, line in all_rows)
-            rest = joined.replace(name, "")
-            found = re.search(r"先生|女士|小姐", rest)
-            if found:
-                pos = found.start()
-                add(
-                    f"称呼必须一字不改用“{name}”，不得自己加“{found.group(0)}”"
-                    f"（原句：{_snip(rest[max(0, pos - 12) : pos + 6])}）"
-                )
-            if params.structured and f"为{_compact(name)}准备的行程方案" not in _compact(joined):
-                add(f"封面必须写“为{name}准备的行程方案”，称呼一字不改")
-
-        # 3. nights and day count
-        nights = params.nights
-        if nights is not None:
-            for kind, _heading, line in all_rows:
-                if kind == "fee":
-                    continue
-                for match in NIGHT_RE.finditer(line):
-                    value = _num(match.group(1))
-                    if value is not None and value != nights:
-                        add(f"晚数写成了“{match.group(0).strip()}”（原句：{_snip(line)}），报价是{nights}晚，处处写“{nights}晚”")
-            days = set()
-            for kind, _heading, line in all_rows:
-                if kind == "fee":
-                    continue
-                for match in DAY_RE.finditer(line):
-                    value = _num(match.group(1))
-                    if value:
-                        days.add(value)
-            if days and (max(days) > nights + 1 or len(days) < nights + 1):
-                add(f"每日安排应为{nights + 1}天（{nights}晚），逐天写“第几天 + 日期”，现在写到的天数不符")
-
-        # 4. selection rules
-        for kind, _heading, line in all_rows:
-            if kind == "fee":
-                continue
-            for word in RULE_WORDS:
-                if word in line:
-                    add(f"对客文案讲了选档规则“{word}”（原句：{_snip(line)}），删去，理由只讲客人说过的情况和产品亮点")
-            if kind == "why" and "预算" in line:
-                add(f"“为什么是这条线”不讲预算（原句：{_snip(line)}），只讲客人说过的情况和产品亮点")
-
-        # 5. judgements not in the research
-        if params.research:
-            allowed = proposal.allowed_text
-            for kind, _heading, line in all_rows:
-                if kind == "fee":
-                    continue
-                for word in JUDGEMENT_WORDS:
-                    if word in line and word not in allowed:
-                        add(
-                            f"“{word}”是调研原文没有的判断（原句：{_snip(line)}），照调研原话写，"
-                            "保留调研写明的适用范围（如某日代表性排班）"
-                        )
-
-        # 6. notice fill-ins outside the next-steps page
-        for kind, _heading, line in all_rows:
-            if kind != "next" and NOTICE_RE.search(line):
-                add(
-                    f"“以出团通知书为准”只放在下一步与出行须知页（原句：{_snip(line)}）；"
-                    "未查证的项目整项不写，不留格子填空"
-                )
-
-        # 7. hotel labels
-        for kind, _heading, line in all_rows:
-            if kind in ("fee", "contact") or _has_label(line):
-                continue
-            for match in HOTEL_RE.finditer(line):
-                prefix = match.group(1)
-                if prefix.endswith(GENERIC_ENDS):
-                    continue
-                add(f"酒店名“{_snip(match.group(0)[-12:], 14)}”同一处要标“{LABEL}”")
-
-        # 8. bullets per page and length
-        for kind, heading, body in sections:
-            if kind in ("fee", "contact"):
-                continue
-            bullets = [line for line in body if BULLET_RE.match(line)]
-            if kind in ("why", "day") and len(bullets) > MAX_BULLETS:
-                add(f"“{_snip(heading, 16)}”一页有{len(bullets)}个要点，每页最多三个要点")
-            checked = bullets if kind != "why" else [line for line in body if line.strip()]
-            for line in checked:
-                if _han_len(line) > MAX_HAN:
-                    add(f"要点超过三十字（“{_snip(line, 20)}”），每个要点不超过三十个汉字")
-
-        return problems[:MAX_PROBLEMS]
+        state.setdefault("last_skipped", [])
 
     async def assess(self, proposal: BriefProposal) -> ActionDecision:
         self.state["reviews"] = int(self.state.get("reviews", 0) or 0) + 1
-        problems = self._problems(proposal)
+        problems, skipped = brief_problems(proposal)
         self.state["last_artifact"] = proposal.artifact
+        self.state["last_skipped"] = skipped
+        self.state["last_problems"] = problems
         if not problems:
-            self.state["last_problems"] = []
-            return ActionDecision(kind="accept")
-        self.state["last_problems"] = problems
+            return ActionDecision(kind="accept", skipped_checks=skipped)
         if proposal.rollbacks >= MAX_REVISIONS:
             self.state["capped"] = int(self.state.get("capped", 0) or 0) + 1
-            return ActionDecision(kind="accept", problems=problems, capped=True)
+            return ActionDecision(kind="accept", problems=problems, capped=True, skipped_checks=skipped)
         self.state["revisions"] = int(self.state.get("revisions", 0) or 0) + 1
         listing = "\n".join(f"{i}. {p}" for i, p in enumerate(problems, 1))
         quote = proposal.params.quote_number or "<报价编号>"
@@ -351,15 +60,12 @@
             then = "然后重新用 write_file 写入同一路径。"
         else:
             what = "给出的需求单全文"
-            then = (
-                f"重新给出需求单全文，并用 write_file 把改正后的全文写回 briefs/需求单-{quote}.md，"
-                "保持文件与回答一致。"
-            )
+            then = f"重新给出需求单全文，并用 write_file 把改正后的全文写回 briefs/需求单-{quote}.md，保持文件与回答一致。"
         correction = (
             f"【内部核对提示】你准备{what}的“对客文案”部分有以下问题：\n{listing}\n"
             f"请只改这些地方，其余内容保持不变（“设计说明”部分不受影响）；{then}"
         )
-        return ActionDecision(kind="revise", problems=problems, correction=correction)
+        return ActionDecision(kind="revise", problems=problems, correction=correction, skipped_checks=skipped)
 
     async def recover(self, failure: BriefFailure) -> ActionDecision:
         # Not bound: fabricated text would become the node output for build-deck.
@@ -373,67 +79,10 @@
 # ---------- synchronous translations (read-only) ----------
 
 
-def _text_of(content: Any) -> str:
-    if isinstance(content, str):
-        return content
-    if isinstance(content, list):
-        return "\n".join(
-            part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
-        )
-    return ""
-
-
-def _valid(value: str | None) -> str | None:
... 84 more diff lines
```

`files` `brief_checks.py`: added, +404 -0

```diff
--- a/files/brief_checks.py
+++ b/files/brief_checks.py
@@ -0,0 +1,404 @@
+"""Deterministic checks of part A (对客文案) of the Hailan design brief.
+
+``problems`` is the single owner of the brief rules. It returns the Chinese
+problem list and the names of checks that raised (skipped, never passed).
+"""
+
+from __future__ import annotations
+
+import re
+from typing import Callable
+
+from .brief_parse import (
+    BULLET_RE,
+    DAY_RE,
+    HAN_RE,
+    LABEL,
+    NOTICE_SENTENCE,
+    SOURCE_RE,
+    Section,
+    compact,
+    dequote,
+    han_len,
+    is_source_only,
+    is_title_line,
+    num,
+    part_a,
+    segment,
+    snip,
+)
+from .brief_types import BriefProposal
+
+MAX_PROBLEMS = 14
+MAX_BULLETS = 3
+MAX_HAN = 30
+CN = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五"}
+
+NIGHT_RE = re.compile(r"(?<![第加\d一二两三四五六七八九十])(\d+|[一二两三四五六七八九十])\s*晚(?!餐|饭|上|间|宴|会)")
+FEE_LINE_RE = re.compile(r"加晚|×|✕|元|基础晚|\dx|x\d")
+RULE_WORDS = ("人均每晚", "档位适用", "适用条件", "选档", "适用人均")
+JUDGEMENT_WORDS = ("最快", "最早", "当天", "每天")
+NOTICE_RE = re.compile(r"以出团通知书?为准")
+HOTEL_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9·]{2,12})(酒店|宾馆|客栈|青年旅舍|青旅|民宿|饭店)")
+GENERIC_ENDS = (
+    "型", "星级", "星", "精品", "参考", "豪华", "特色", "同级", "高档", "连锁", "含", "住宿", "晚", "的", "等",
+    "区", "内", "附近", "周边", "当地", "景区", "舒适", "经济", "入住", "住", "或", "及", "和", "为",
+    "官方", "官网", "预订", "该", "此", "本", "所选",
+)
+CLAIM_RE = re.compile(r"免票|免费|半价|半票")
+COUNT_RE = re.compile(r"(一|两|二|三|四|五|六|\d+)\s*(?:个|位|名)?\s*(?:人|孩子|小孩|儿童|娃|小朋友)")
+CHILD_RE = re.compile(r"孩子|儿童|小孩|小朋友|娃")
+FILLER_RE = re.compile(r"(?:无|没有|不含|不涉及|不走|不去)\s*(?:65\s*(?:周)?岁以上)?\s*的?\s*(?:长辈|长者|老人|高原)")
+COLUMN_NAMES = {
+    "封面", "行程一览", "行程概览", "行程总览", "一览", "概览", "为什么是这条线", "为什么选这条线", "为什么选择这条线",
+    "选择理由", "推荐理由", "每日安排", "行程安排", "每日行程", "费用明细", "费用说明", "费用", "费用构成", "报价明细",
+    "包含不含", "费用包含不含", "下一步出行须知", "下一步", "出行须知", "温馨提示", "注意事项", "联系我们",
+}
+RESTATE_RE = re.compile(r"包含.{0,8}(?:和|与|及|，|,).{0,14}(?:另外|自理|不含|自付|自备|自费)")
+ARROW_START_RE = re.compile(r"^\s*(?:[-*•·]\s*)?(?:→|->|=>|⇒|所以|因此|于是|本线安排|安排[:：])")
+REASON_START_RE = re.compile(r"^\s*(?:[-*•·]\s+|\d{1,2}\s*[.、)）](?!\d)|[（(]\d[)）]|(?:#+\s*|\*\*)?\s*理由\s*[一二三四1-4])")
+PLATEAU = ("玉龙雪山", "香格里拉", "川西")
+
+
+class _Ctx:
+    def __init__(self, proposal: BriefProposal, lines: list[str]) -> None:
+        self.params = proposal.params
+        self.allowed = proposal.allowed_text
+        self.sections, self.notes = segment(lines)
+        self.rows = [(s.kind, line) for s in self.sections for line in [s.heading, *s.body] if line]
+        self.group = self.params.group_size or ""
+        self.problems: list[str] = []
+        self.flagged: set[str] = set()
+
+    def add(self, item: str) -> None:
+        if item not in self.problems:
+            self.problems.append(item)
+
+    def ungroup(self, line: str) -> str:
+        return line.replace(self.group, "") if self.group else line
+
+    def kinds(self) -> set[str]:
+        return {s.kind for s in self.sections}
+
+    def text(self, kinds: tuple[str, ...] | None = None) -> str:
+        return "\n".join(self.ungroup(line) for kind, line in self.rows if kinds is None or kind in kinds)
+
+
+def _notes(c: _Ctx) -> None:
+    if c.notes:
+        shown = "；".join(snip(n, 20) for n in c.notes[:2])
+        more = f"等{len(c.notes)}处" if len(c.notes) > 2 else ""
+        c.add(f"对客文案里夹了给设计同事的备注（如：{shown}{more}），客人会看到；移到B“设计说明”或删去，对客文案只放客人看到的字")
+
+
+def _internal(c: _Ctx) -> None:
+    seen: set[str] = set()
+    for kind, line in c.rows:
+        for word in ("模板", "设计同事", "需求单", "质检", "调研", "未查到", "未查证", "［", "］"):
+            if word in seen or word not in line or (word == "模板" and kind == "contact"):
+                continue
+            seen.add(word)
+            if word in ("模板", "设计同事", "需求单", "质检"):
+                c.add(f"对客文案出现“{word}”（原句：{snip(line)}），这是给设计同事的话，客人会看到；移到B“设计说明”，对客文案里删去")
+            elif word in ("［", "］"):
+                c.add(f"对客文案留有［］占位（原句：{snip(line)}），换成实际内容或删去")
+            else:
+                c.add(f"对客文案出现内部字样“{word}”（原句：{snip(line)}），客人会看到；来源只写调研里列出的真实网站名或链接，未查到的项目整项不写")
+
+
+def _honorific(c: _Ctx) -> None:
+    name = c.params.customer_name
+    if not name:
+        return
+    joined = "\n".join(line for _k, line in c.rows)
+    rest = joined.replace(name, "")
+    found = re.search(r"先生|女士|小姐", rest)
+    if found:
+        pos = found.start()
+        c.add(f"称呼必须一字不改用“{name}”，不得自己加“{found.group(0)}”（原句：{snip(rest[max(0, pos - 12) : pos + 6])}）")
+    if c.params.structured and f"为{compact(name)}准备的行程方案" not in compact(joined):
+        c.add(f"封面必须写“为{name}准备的行程方案”，称呼一字不改")
+
+
+def _rules(research: str, free: bool) -> list[tuple[str, Callable[[int], bool | None]]]:
+    words = r"(?:免票|免费|免门票)" if free else r"(?:半价|半票)"
+    rules: list[tuple[str, Callable[[int], bool | None]]] = []
+    for m in re.finditer(r"(\d+)\s*(?:周)?岁\s*([（(]\s*含[^）)]*[）)])?\s*(及以下|以下|以内)[^。；;\n]{0,14}?" + words, research):
+        n, inclusive = int(m.group(1)), bool(m.group(2)) or m.group(3) == "及以下"
+        rules.append((m.group(0), lambda a, n=n, inc=inclusive: a <= n if inc else (None if a == n else a < n)))
+    for m in re.finditer(r"未满\s*(\d+)\s*(?:周)?岁[^。；;\n]{0,14}?" + words, research):
+        n = int(m.group(1))
+        rules.append((m.group(0), lambda a, n=n: a < n))
+    for m in re.finditer(words + r"[^。；;\n]{0,8}?(\d+)\s*(?:周)?岁\s*(及以下|以下)", research):
+        n, inclusive = int(m.group(1)), m.group(2) == "及以下"
+        rules.append((m.group(0), lambda a, n=n, inc=inclusive: a <= n if inc else (None if a == n else a < n)))
+    for m in re.finditer(r"(\d+)\s*(?:周)?岁?\s*(?:至|到|-|—|~|～|－)\s*(\d+)\s*(?:周)?岁[^。；;\n]{0,14}?" + words, research):
+        lo, hi = int(m.group(1)), int(m.group(2))
+        rules.append((m.group(0), lambda a, lo=lo, hi=hi: lo <= a <= hi))
+    return rules
+
+
+def _tickets(c: _Ctx) -> None:
+    ages = c.params.child_ages
+    research = c.params.research
+    if not ages or not research:
+        return
+    comp_research = compact(research)
+    for _kind, line in c.rows:
+        for clause in re.split(r"[，,。；;、/|]", c.ungroup(line)):
+            for m in CLAIM_RE.finditer(clause):
+                window = clause[max(0, m.start() - 10) : m.start()]
+                counts = list(COUNT_RE.finditer(window))
+                if counts:
+                    claimed = num(counts[-1].group(1))
+                elif re.search(r"都|均|全部", window) and CHILD_RE.search(clause):
+                    claimed = len(ages)
+                else:
+                    continue
+                if claimed is None or compact(clause) in comp_research:
+                    continue
+                free = m.group(0) in ("免票", "免费")
+                term = "免票" if free else "半价"
+                rules = _rules(research, free)
+                results = [(snippet, [pred(a) for a in ages]) for snippet, pred in rules]
+                counts_set = {sum(1 for r in res if r) for _s, res in results}
+                computable = bool(results) and all(None not in res for _s, res in results) and len(counts_set) == 1
+                if computable and counts_set == {claimed}:
+                    continue
+                if computable:
+                    snippet, res = results[0]
+                    ok = [f"{a}岁" for a, r in zip(ages, res) if r]
+                    no = [f"{a}岁" for a, r in zip(ages, res) if not r]
+                    if not no:
+                        fact = f"{'、'.join(ok)}的孩子都符合，应写“{CN.get(len(ok), len(ok))}个孩子都可{term}”"
+                    elif not ok:
+                        fact = "孩子都不符合，不能写成有人可享受"
+                    else:
+                        fact = f"{'、'.join(ok)}符合、{'、'.join(no)}不符合"
+                    c.add(f"门票优免写成了“{snip(clause, 24)}”，与调研原文不符：按“{snip(snippet, 24)}”，本团{fact}；照调研原文连同条件写，不自己归纳")
+                else:
+                    c.add(f"门票优免写成了按人头的归纳“{snip(clause, 24)}”，调研原文没有这种说法，也无法按本团每个孩子的年龄逐一核实；只列调研原规则连同条件（年龄段、身高、日期）")
+
+
+def _filler(c: _Ctx) -> None:
+    for _kind, line in c.rows:
+        text = QUOTE_SEG_STRIP(c.ungroup(line))
+        if FILLER_RE.search(text):
+            c.flagged.add(line)
+            c.add(f"“{snip(text)}”不是理由，也不是本团适用的提示，删去；三条理由每条写“客人原话 → 本线安排”，安排用上产品说明里写明的亮点")
+
+
+def QUOTE_SEG_STRIP(text: str) -> str:
+    return re.sub(r"“[^”]*”|「[^」]*」", "", text)
+
+
+def _title_value(line: str) -> str:
+    value = re.split(r"[:：]", line, maxsplit=1)[1] if re.search(r"[:：]", line) else ""
+    return value.strip().strip("*“”\"《》「」『』 ").strip()
+
+
+def _titles(c: _Ctx) -> None:
+    for s in c.sections:
+        if s.kind in ("cover", "contact"):
+            continue
+        for line in s.body:
+            if not is_title_line(line):
+                continue
+            value = _title_value(line)
+            if not value:
+                continue
+            norm = re.sub(r"[的与和及、，,。.！!：:·\s页]", "", value)
+            has_digit = bool(re.search(r"\d", value))
+            if norm in COLUMN_NAMES or (RESTATE_RE.search(value) and not has_digit):
+                c.add(f"页标题“{snip(value, 24)}”是栏目名（或换个说法），改成这一页要客人记住的一句结论")
+            elif s.kind == "fee" and not has_digit:
+                c.add(f"费用页标题“{snip(value, 24)}”要写成一句带全团总价的结论（几位全包多少元、意外险怎么算）")
+
+
+def _safety(c: _Ctx) -> None:
+    p = c.params
+    rows = [(k, c.ungroup(line), line) for k, line in c.rows]
+    everything = "\n".join(t for _k, t, _l in rows)
+    if p.elders == 0:
+        for _k, text, line in rows:
+            if line in c.flagged:
+                continue
+            if "健康申明" in text or re.search(r"65\s*(?:周)?岁(?!\s*(?:及)?以上\s*[:：]?\s*(?:0|零|无))", text):
+                c.add(f"本团没有65岁以上出行人，“{snip(text)}”与本团无关，删去（健康申明只在有65岁以上出行人时写）")
+                break
+    elif p.elders:
+        if "健康申明" not in everything:
+            c.add("本团有65岁以上出行人，下一步与出行须知页要写签约时填健康申明并建议购买意外险")
+    day_text = c.text(("day", "days"))
+    plateau = any(k in (p.destination or "") or k in day_text for k in PLATEAU)
+    if plateau:
+        scope = c.text(("next",)) if "next" in c.kinds() else everything
+        if "高原" not in scope:
+            c.add("本线含高原行程，下一步与出行须知页要写高原反应风险提示、建议出发前咨询医生、孕期或心肺疾病者不宜参加")
+    elif p.destination:
+        for _k, text, line in rows:
+            if "高原" in text and line not in c.flagged:
+                c.add(f"本线（{p.destination}）不是高原行程，“{snip(text)}”与本团无关，删去")
+                break
+    if p.child_ages == []:
+        for _k, text, _line in rows:
+            if re.search(r"成人(?:全程)?陪同|户口簿|儿童(?:须|需|要)", text):
+                c.add(f"本团没有儿童，“{snip(text)}”与本团无关，删去")
+                break
+    elif p.child_ages:
+        if "陪同" not in everything:
+            c.add("本团有儿童，下一步与出行须知页要写儿童全程需成人陪同并携带有效身份证件或户口簿")
+
+
+def _notice(c: _Ctx) -> None:
+    if not c.params.needs_notice_sentence:
+        return
+    joined = compact(c.text()).replace("，", "、").replace(",", "、")
+    if NOTICE_SENTENCE not in joined:
+        c.add("下一步与出行须知页要原样完整写出“天气、开放时间等以出团通知书为准。”，不能只写天气")
+
+
+def _notice_elsewhere(c: _Ctx) -> None:
+    if "next" not in c.kinds():
+        return
+    for kind, line in c.rows:
+        if kind != "next" and NOTICE_RE.search(line):
+            c.add(f"“以出团通知书为准”只放在下一步与出行须知页（原句：{snip(line)}）；未查证的项目整项不写，不留格子填空")
+
+
+def _nights(c: _Ctx) -> None:
+    nights = c.params.nights
+    if nights is None:
+        return
+    days: set[int] = set()
+    for kind, line in c.rows:
+        if kind == "fee":
+            continue
+        for m in DAY_RE.finditer(line):
+            value = num(m.group(1))
+            if value:
+                days.add(value)
+        if FEE_LINE_RE.search(line):
+            continue
+        for m in NIGHT_RE.finditer(line):
+            value = num(m.group(1))
+            if value is not None and value != nights:
+                c.add(f"晚数写成了“{m.group(0).strip()}”（原句：{snip(line)}），报价是{nights}晚，处处写“{nights}晚”")
+    if days and (max(days) > nights + 1 or len(days) < nights + 1):
+        c.add(f"每日安排应为{nights + 1}天（{nights}晚），逐天写“第几天 + 日期”，现在写到的天数不符")
+
+
+def _selection(c: _Ctx) -> None:
+    for kind, line in c.rows:
+        if kind == "fee":
+            continue
+        for word in RULE_WORDS:
+            if word in line:
+                c.add(f"对客文案讲了选档规则“{word}”（原句：{snip(line)}），删去，理由只讲客人说过的情况和产品亮点")
+        if kind == "why" and "预算" in line:
+            c.add(f"“为什么是这条线”不讲预算（原句：{snip(line)}），只讲客人说过的情况和产品亮点")
+
+
+def _judgement(c: _Ctx) -> None:
+    if not c.params.research:
+        return
+    for kind, line in c.rows:
+        if kind == "fee":
+            continue
+        for word in JUDGEMENT_WORDS:
+            if word in line and word not in c.allowed:
+                c.add(f"“{word}”是调研原文没有的判断（原句：{snip(line)}），照调研原话写，保留调研写明的适用范围（如某日代表性排班）")
+
+
+def _hotels(c: _Ctx) -> None:
+    for kind, line in c.rows:
+        if kind in ("fee", "contact"):
+            continue
+        cl = compact(line)
+        if LABEL in cl or "参考酒店,以计调确认为准" in cl:
+            continue
+        for m in HOTEL_RE.finditer(SOURCE_RE.sub("", line)):
+            if m.group(1).endswith(GENERIC_ENDS):
+                continue
+            c.add(f"酒店名“{snip(m.group(0)[-12:], 14)}”同一处要标“{LABEL}”")
+
+
+def _reason_lines(body: list[str]) -> list[str]:
+    reasons: list[str] = []
+    usable = [l for l in body if l.strip() and not is_title_line(l) and not is_source_only(l)]
+    for line in usable:
+        if ARROW_START_RE.match(line) and reasons:
+            reasons[-1] += " " + line
+        elif REASON_START_RE.match(line) or "→" in line or "->" in line:
+            reasons.append(line)
+    if not reasons:
+        for line in usable:
+            if ARROW_START_RE.match(line) and reasons:
+                reasons[-1] += " " + line
+            elif len(HAN_RE.findall(line)) >= 6:
+                reasons.append(line)
+    return reasons
+
+
+def _arrangement(reason: str) -> str:
+    for arrow in ("→", "->", "=>", "⇒"):
+        if arrow in reason:
+            return reason.rsplit(arrow, 1)[1]
+    if "所以" in reason:
+        return reason.split("所以", 1)[1]
+    return dequote(reason)
+
+
+def _reasons(c: _Ctx) -> None:
+    body = [line for s in c.sections if s.kind == "why" for line in s.body]
+    if not any(s.kind == "why" for s in c.sections):
+        return
+    reasons = _reason_lines(body)
+    if c.params.structured and len(reasons) < 3:
+        c.add("“为什么是这条线”要写满三条理由，每条写“客人原话 → 本线安排”")
+    doc = "".join(HAN_RE.findall(c.params.product_doc))
+    if not doc:
+        return
+    for i, reason in enumerate(reasons[:3], 1):
+        han = "".join(HAN_RE.findall(SOURCE_RE.sub("", _arrangement(reason))))
+        if len(han) < 4 or not any(han[j : j + 4] in doc for j in range(len(han) - 3)):
+            c.add(f"第{i}条理由（{snip(reason, 20)}）没写出用上产品说明亮点的本线安排（如往返交通、导览、交通卡等，以产品说明原文为准）；写成“客人原话 → 本线安排”")
+
+
+def _bullets(c: _Ctx) -> None:
+    for s in c.sections:
+        if s.kind in ("fee", "contact"):
+            continue
+        bullets = [l for l in s.body if BULLET_RE.match(l) and not is_title_line(l)]
+        if s.kind in ("why", "day") and len(bullets) > MAX_BULLETS:
+            c.add(f"“{snip(s.heading, 16)}”一页有{len(bullets)}个要点，每页最多三个要点")
+        checked = bullets + ([l for l in s.body if "→" in l and l not in bullets] if s.kind == "why" else [])
+        for line in checked:
+            if is_source_only(line):
+                continue
+            measured = dequote(line) if s.kind == "why" else line
+            if han_len(measured) > MAX_HAN:
+                c.add(f"要点超过三十字（“{snip(line, 20)}”），每个要点不超过三十个汉字（引用的客人原话不计）")
+
+
+CHECKS: tuple[Callable[[_Ctx], None], ...] = (
+    _notes, _internal, _honorific, _tickets, _filler, _titles, _safety, _notice, _notice_elsewhere,
+    _nights, _selection, _judgement, _hotels, _reasons, _bullets,
+)
+
+
+def problems(proposal: BriefProposal) -> tuple[list[str], list[str]]:
+    """(problems, names of checks that raised and were therefore not applied)."""
+    lines = part_a(proposal.text, proposal.params.structured)
+    if lines is None:
+        if proposal.artifact == "answer":
+            return ["最后的回答必须给出需求单全文，含 A 对客文案与 B 设计说明两部分"], []
+        return ["需求单必须分 A 对客文案 / B 设计说明两部分，并标出“对客文案”部分"], []
+    ctx = _Ctx(proposal, lines)
... 7 more diff lines
```

`files` `brief_parse.py`: added, +384 -0

```diff
--- a/files/brief_parse.py
+++ b/files/brief_parse.py
@@ -0,0 +1,384 @@
+"""Pure parsing helpers: node prompt, stored transcript and part-A segmentation."""
+
+from __future__ import annotations
+
+import json
+import re
+from dataclasses import dataclass, field
+from typing import Any, Sequence
+
+HAN_RE = re.compile(r"[\u4e00-\u9fff]")
+CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
+LABEL = "参考酒店，以计调确认为准"
+NOTICE_SENTENCE = "天气、开放时间等以出团通知书为准"
+
+NAME_RES = (
+    re.compile(r"为(.+?)准备的行程方案（称呼一字不改"),
+    re.compile(r"客人称呼：(.+?)（封面写"),
+)
+NIGHTS_RES = (
+    re.compile(r"共\s*(\d+)\s*晚、天数等于晚数加一"),
+    re.compile(r"目的地加“(\d+)晚”"),
+)
+QUOTE_RE = re.compile(r"需求单-(HL-Q-[0-9A-Za-z-]+?)\.md")
+RESEARCH_MARK = "调研结果（已附来源）："
+GROUP_RES = (
+    re.compile(r"按人数构成\s*(.+?)\s*里每一位出行人"),
+    re.compile(r"人数构成\s*([^；;\n]+?)\s*、\s*出发城市"),
+)
+DEST_RE = re.compile(r"目的地\s*([^；;\n]+?)\s*、\s*出行日期")
+PRODUCT_PATH_RE = re.compile(r"读产品说明\s*([^\s，,、；;]+?\.md)")
+AGE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:周)?岁(?!\s*(?:及)?(?:以上|以下))")
+CHILD_WORD_RE = re.compile(r"儿童|孩子|小孩|小朋友|男孩|女孩|宝宝|婴儿|\d\s*小(?!时)")
+NO_CHILD_RE = re.compile(r"(?:无|没有|不带)\s*(?:儿童|孩子|小孩)|(?:儿童|孩子|小孩)\s*[:：]?\s*(?:0|零|无)\s*(?:人|位|个|名)?(?!\d)")
+ELDER_RE = re.compile(r"65\s*(?:周)?岁\s*(?:及)?以上(?:的?(?:出行人|长辈|老人|长者))?\s*[:：]?\s*(\d+|[零一二两三四五六七八九十])")
+NO_ELDER_RE = re.compile(r"(?:无|没有)\s*65\s*(?:周)?岁\s*(?:及)?以上")
+
+BULLET_RE = re.compile(r"^\s*(?:[-*•·]\s+|\d{1,2}\s*[.、)）](?!\d)\s*|[（(]\d{1,2}[)）]\s*)")
+NUMBERED_RE = re.compile(r"^\s*\d{1,2}\s*[.、)）](?!\d)")
+MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s*\S")
+BOLD_LINE_RE = re.compile(r"^\s*\*\*[^*]+\*\*\s*[:：]?\s*$")
+BRACKET_LINE_RE = re.compile(r"^\s*【[^】]+】\s*$")
+PAGE_LABEL_RE = re.compile(r"^\s*(?:#{1,6}\s*|\*\*\s*|【\s*)?(?:第\s*(\d{1,2})\s*页|[Pp]\s*(\d{1,2})(?!\d)|页\s*(\d{1,2})(?!\d))")
+TITLE_LINE_RE = re.compile(r"^\s*(?:[-*•·]\s*)?(?:\*\*)?\s*(?:结论|本页|页面?)?\s*标题\s*(?:\*\*)?\s*[:：]")
+DAY_RE = re.compile(r"第\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*天")
+SOURCE_RE = re.compile(r"[（(]?\s*来源[:：][^）)\n]*[）)]?")
+PAGE_NAMES = ("封面", "行程一览", "为什么是这条线", "每日安排", "费用明细", "包含与不含", "下一步与出行须知", "联系我们")
+PAGE_KEYS = (
+    ("contact", re.compile(r"联系")),
+    ("cover", re.compile(r"封面")),
+    ("why", re.compile(r"为什么|理由")),
+    ("next", re.compile(r"下一步|须知")),
+    ("fee", re.compile(r"费用|明细|报价单|价格")),
+    ("include", re.compile(r"包含|不含")),
+    ("days", re.compile(r"每日安排|每日行程|行程安排")),
+    ("overview", re.compile(r"一览|概览|总览")),
+)
+LABEL_KIND = {1: "cover", 2: "overview", 3: "why"}
+SUCCESSOR = {"cover": "overview", "overview": "why", "why": "days", "fee": "include", "include": "next", "next": "contact"}
+NOTE_VOCAB = (
+    "模板", "版式", "排版", "字号", "图标", "左上角", "右上角", "左下角", "右下角", "左侧", "右侧", "左栏", "右栏",
+    "保持不动", "照写", "照抄", "不加", "替换", "删去", "未查证", "未查到", "设计", "占位", "此处放", "放置",
+)
+PAREN_RE = re.compile(r"[（(][^（）()]*[）)]")
+NOTE_PREFIX_RE = re.compile(r"^\s*(?:[-*•·>]\s*)*(?:\*\*)?\s*(?:注|备注|说明|设计|版式|排版)[^：:\n]{0,6}[:：]")
+QUOTE_SEG_RE = re.compile(r"“[^”]*”|「[^」]*」|\"[^\"]*\"|‘[^’]*’")
+SPEAKER_RE = re.compile(r"^\s*(?:[-*•·]\s+|\d{1,2}\s*[.、)）]\s*)?(?:客人|您)(?:说|提到|讲|原话)[过了]?\s*[:：]?\s*")
+
+
+@dataclass
+class Section:
+    kind: str
+    heading: str
+    body: list[str] = field(default_factory=list)
+
+
+def compact(text: str) -> str:
+    return re.sub(r"\s+", "", text)
+
+
+def snip(text: str, limit: int = 30) -> str:
+    text = re.sub(r"\s+", " ", text).strip()
+    return text if len(text) <= limit else text[:limit] + "…"
+
+
+def num(token: str) -> int | None:
+    if token.isdigit():
+        return int(token)
+    if token in CN_NUM:
+        return CN_NUM[token]
+    if len(token) == 2 and token[0] == "十":
+        return 10 + CN_NUM.get(token[1], 0)
+    if len(token) == 2 and token[1] == "十":
+        return CN_NUM.get(token[0], 0) * 10
+    if len(token) == 3 and token[1] == "十":
+        return CN_NUM.get(token[0], 0) * 10 + CN_NUM.get(token[2], 0)
+    return None
+
+
+def text_of(content: Any) -> str:
+    if isinstance(content, str):
+        return content
+    if isinstance(content, list):
+        return "\n".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
+    return ""
+
+
+def valid(value: str | None) -> str | None:
+    if value is None:
+        return None
+    value = value.strip()
+    if not value or "${" in value or "{{" in value:
+        return None
+    return value
+
+
+def han_len(line: str) -> int:
+    cleaned = SOURCE_RE.sub("", line).replace(LABEL, "").replace("参考酒店,以计调确认为准", "")
+    cleaned = re.sub(r"https?://\S+", "", cleaned)
+    return len(HAN_RE.findall(cleaned))
+
+
+def dequote(line: str) -> str:
+    return SPEAKER_RE.sub("", QUOTE_SEG_RE.sub("", line))
+
+
+def is_title_line(line: str) -> bool:
+    return bool(TITLE_LINE_RE.match(line))
+
+
+def is_source_only(line: str) -> bool:
+    s = re.sub(r"^\s*(?:[-*•·]\s+)?", "", line).lstrip("（(*")
+    return s.startswith("来源")
+
+
+def child_ages(group: str | None) -> list[int] | None:
+    if not group:
+        return None
+    ages = [int(m.group(1)) for m in AGE_RE.finditer(group) if int(m.group(1)) < 18]
+    if ages:
+        return ages
+    if NO_CHILD_RE.search(group) or not CHILD_WORD_RE.search(group):
+        return []
+    return None
+
+
+def elders(group: str | None) -> int | None:
+    if not group:
+        return None
+    if NO_ELDER_RE.search(group):
+        return 0
+    m = ELDER_RE.search(group)
+    return num(m.group(1)) if m else None
+
+
+def find_prompt(question: str, transcript: Sequence[dict[str, Any]]) -> str:
+    if "准备的行程方案" in question:
+        return question
+    for row in transcript:
+        if isinstance(row, dict) and row.get("role") == "user":
+            text = text_of(row.get("content"))
+            if "准备的行程方案" in text and "需求单" in text:
+                return text
+    return question
+
+
+def _norm_path(path: str) -> str:
+    path = path.replace("\\", "/").strip()
+    while path.startswith("./"):
+        path = path[2:]
+    return path
+
+
+def _same_path(a: str, b: str) -> bool:
+    a, b = _norm_path(a), _norm_path(b)
+    return a == b or a.endswith("/" + b) or b.endswith("/" + a)
+
+
+def product_doc_text(transcript: Sequence[dict[str, Any]], path: str | None) -> str:
+    """Tool results of read_file calls on the product doc, matched by call id."""
+    if not path:
+        return ""
+    ids: set[str] = set()
+    for row in transcript:
+        if not isinstance(row, dict) or row.get("role") != "assistant":
+            continue
+        for call in row.get("tool_calls") or []:
+            fn = call.get("function") if isinstance(call, dict) else None
+            if not isinstance(fn, dict) or fn.get("name") != "read_file":
+                continue
+            args = fn.get("arguments")
+            if isinstance(args, str):
+                try:
+                    args = json.loads(args)
+                except ValueError:
+                    continue
+            target = args.get("path") if isinstance(args, dict) else None
+            if isinstance(target, str) and _same_path(target, path) and call.get("id"):
+                ids.add(call["id"])
+    parts = [
+        text_of(row.get("content"))
+        for row in transcript
+        if isinstance(row, dict) and row.get("role") == "tool" and row.get("tool_call_id") in ids
+    ]
+    return "\n".join(p for p in parts if p)
+
+
+def parse_params(prompt: str, transcript: Sequence[dict[str, Any]]) -> dict[str, Any]:
+    name = None
+    for pattern in NAME_RES:
+        m = pattern.search(prompt)
+        if m:
+            name = valid(m.group(1))
+            break
+    nights = None
+    for pattern in NIGHTS_RES:
+        m = pattern.search(prompt)
+        if m:
+            nights = int(m.group(1))
+            break
+    quote = QUOTE_RE.search(prompt)
+    research = ""
+    if RESEARCH_MARK in prompt:
+        research = prompt.split(RESEARCH_MARK, 1)[1].strip()
+        if research.startswith("{{"):
+            research = ""
+    group = None
+    for pattern in GROUP_RES:
+        m = pattern.search(prompt)
+        if m:
+            group = valid(m.group(1))
+            if group:
+                break
+    dest_m = DEST_RE.search(prompt)
+    path_m = PRODUCT_PATH_RE.search(prompt)
+    return {
+        "customer_name": name,
+        "nights": nights,
+        "quote_number": valid(quote.group(1)) if quote else None,
+        "research": research,
+        "structured": "对客文案" in prompt,
+        "group_size": group,
+        "destination": valid(dest_m.group(1)) if dest_m else None,
+        "child_ages": child_ages(group),
+        "elders": elders(group),
+        "product_doc": product_doc_text(transcript, valid(path_m.group(1)) if path_m else None),
+        "needs_notice_sentence": NOTICE_SENTENCE in prompt,
+    }
+
+
+def allowed_text(transcript: Sequence[dict[str, Any]], research: str) -> str:
+    parts = [research]
+    for row in transcript:
+        if isinstance(row, dict) and row.get("role") == "tool":
+            parts.append(text_of(row.get("content")))
+    return "\n".join(parts)
+
+
+# ---------- part A ----------
+
+
+def _is_part_b_marker(line: str) -> bool:
+    s = line.strip()
+    if "设计说明" not in s or len(s) > 40 or s.startswith(("（", "(")):
+        return False
+    return bool(
+        MD_HEADING_RE.match(s)
+        or BOLD_LINE_RE.match(s)
+        or BRACKET_LINE_RE.match(s)
+        or re.match(r"^(?:\*\*)?\s*(?:B|Ｂ|二|第二部分)", s)
+        or len(s) <= 20
+    )
+
+
+def part_a(text: str, structured: bool) -> list[str] | None:
+    """Lines of part A (对客文案), excluding its marker line; None if missing."""
+    lines = text.splitlines()
+    if structured:
+        start = None
+        for i, line in enumerate(lines):
+            if "对客文案" in line and "设计说明" not in line and len(line.strip()) <= 40:
+                start = i
+                break
+        if start is None:
+            for i, line in enumerate(lines):
+                if "对客文案" in line:
+                    start = i
+                    break
+        if start is None:
+            return None
+        end = len(lines)
+        for j in range(start + 1, len(lines)):
+            if _is_part_b_marker(lines[j]):
+                end = j
+                break
+        return lines[start + 1 : end]
+    out: list[str] = []
+    for line in lines:
+        if "规范要求摘要" in line or "设计说明" in line:
+            break
+        if "模板文件路径" in line or "成品文件名" in line:
+            continue
+        out.append(line)
+    return out
+
+
+def split_note(line: str) -> tuple[str, list[str]]:
+    """Separate design notes (internal remarks) from customer-facing text."""
+    if NOTE_PREFIX_RE.match(line) and any(v in line for v in NOTE_VOCAB):
+        return "", [line.strip()]
+    notes: list[str] = []
+
+    def repl(m: re.Match[str]) -> str:
+        seg = m.group(0)
+        if any(v in seg for v in NOTE_VOCAB):
+            notes.append(seg)
+            return ""
+        return seg
+
+    cleaned = PAREN_RE.sub(repl, line)
+    if notes and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", cleaned):
+        cleaned = ""
+    return cleaned, notes
+
+
+def heading_text(line: str) -> str:
+    t = line.strip()
+    t = re.sub(r"^#{1,6}\s*", "", t)
+    t = t.strip("*【】 ").strip()
+    t = re.sub(r"^(?:第\s*\d{1,2}\s*页|[Pp]\s*\d{1,2}(?!\d)|页\s*\d{1,2}(?!\d))\s*[:：·.、\-—|｜]*\s*", "", t)
+    t = re.sub(r"^\d{1,2}\s*[.、)）](?!\d)\s*", "", t)
+    return t.strip("*【】 ：:").strip()
+
+
+def page_kind(text: str) -> str | None:
+    if re.search(r"包含\s*[与和及/、]\s*不含", text[:12]):
+        return "include"
+    best: tuple[str, int] | None = None
+    for kind, pattern in PAGE_KEYS:
+        m = pattern.search(text)
+        if m and (best is None or m.start() < best[1]):
+            best = (kind, m.start())
+    if best and best[1] <= 8:
+        return best[0]
+    return None
+
+
+def _page_label(line: str) -> int | None:
+    m = PAGE_LABEL_RE.match(line)
+    if not m:
+        return None
+    return int(next(g for g in m.groups() if g))
+
+
+def segment(lines: list[str]) -> tuple[list[Section], list[str]]:
+    """Pages (and day units) of part A, plus the design notes removed from it."""
+    sections = [Section("other", "")]
+    notes: list[str] = []
+    for raw in lines:
+        line, found = split_note(raw)
+        notes.extend(found)
+        if not line.strip():
+            continue
+        cur = sections[-1]
+        if TITLE_LINE_RE.match(line):
+            cur.body.append(line)
+            continue
+        text = heading_text(line)
+        label = _page_label(line)
+        numbered = bool(NUMBERED_RE.match(line)) and text.startswith(PAGE_NAMES)
+        heading_like = bool(MD_HEADING_RE.match(line) or BOLD_LINE_RE.match(line) or BRACKET_LINE_RE.match(line))
+        if numbered or label is not None or (heading_like and not BULLET_RE.match(line)):
+            kind = "day" if DAY_RE.match(text) else page_kind(text)
+            if kind is None and label is not None:
+                kind = LABEL_KIND.get(label) or SUCCESSOR.get(cur.kind, "other")
+            if kind is None or (kind == cur.kind and kind != "day" and label is None):
+                cur.body.append(line)
+                continue
+            sections.append(Section(kind, line.strip()))
+            continue
+        if not BULLET_RE.match(line) and DAY_RE.match(text) and len(text) <= 30:
+            sections.append(Section("day", line.strip()))
+            continue
+        cur.body.append(line)
+    return sections, notes
```

`files` `brief_types.py`: added, +49 -0

```diff
--- a/files/brief_types.py
+++ b/files/brief_types.py
@@ -0,0 +1,49 @@
+"""Semantic types of the Hailan design-brief action strategy."""
+
+from __future__ import annotations
+
+from typing import Literal
+
+from pydantic import BaseModel, Field
+
+
+class NodeParams(BaseModel):
+    """Values from the rendered node prompt; None means not parsed, never guessed."""
+
+    customer_name: str | None = None
+    nights: int | None = None
+    quote_number: str | None = None
+    research: str = ""
+    structured: bool = False
+    group_size: str | None = None
+    destination: str | None = None
+    child_ages: list[int] | None = None
+    elders: int | None = None
+    product_doc: str = ""
+    needs_notice_sentence: bool = False
+
+
+class BriefProposal(BaseModel):
+    """A brief about to be written (execute_tools) or answered (after_iteration)."""
+
+    phase: Literal["execute_tools", "after_iteration"]
+    artifact: Literal["file", "answer"]
+    text: str
+    path: str | None = None
+    params: NodeParams
+    allowed_text: str = ""
+    rollbacks: int = 0
+
+
+class BriefFailure(BaseModel):
+    """Answerless turn; the failure path is intentionally not bound."""
+
+    reason: str = ""
+
+
+class ActionDecision(BaseModel):
+    kind: Literal["accept", "revise", "defer"]
+    problems: list[str] = Field(default_factory=list)
+    correction: str | None = None
+    capped: bool = False
+    skipped_checks: list[str] = Field(default_factory=list)
```

### Curation `873796d2ac8b494e9719c2cc323204f3.Raven-PPT` (scope Raven-PPT): installed

Understanding (candidate):

> 我的理解：上一轮设计同事改好了不少地方，称呼、内部字样、酒店标注、班次适用范围和字号都过了关。收尾前的自查也确实跑了两次，两次都判“通过”。可是方案里还留着几处错，原因是自查只查了上一轮出过的问题，这一轮这几处它根本不查，或者只给一句提醒：
> - 门票优免被自己归纳成“一人免票、一人半价”，调研里没有这种说法；
> - 费用页“含险合计”和“报价有效期”两行叠在一起，三条来源行前面还配了汽车、步行、火车图标；
> - 下一步页只写了“天气以出团通知书为准”，缺了“开放时间等”；
> - 第8页标题是栏目名换了个说法；
> - 这一家没有长者，却写了65岁以上要填健康申明，还写了“无高原”。
>
> 这些规矩工作说明里大多已经写了，只是没照做。所以这次把能机械核对的都放进交稿前的自查，查出来就是必须修改的项。原有的收尾检查看到“不通过”，会让设计同事先改完、重新导出再交，最多退回两次。具体做法：
> 1. 门票、优免、班次、时刻这类句子，逐句拿去和需求单、调研原文比对，找不到原文的算不合格。像“所以：一人免票、一人半价”这种自己归纳的说法会被拦下，并要求照原文逐字写。
> 2. 下一步页必须有完整的一句“天气、开放时间等以出团通知书为准”，缺了或只剩“天气以出团通知书为准”都算不合格。这一句本身不再被当成“拿出团通知书填空”去提醒。
> 3. 出行人里没有65岁以上的，出现“健康申明”“65岁”就不合格；不是高原线路的，出现“高原”（包括“无高原”）也不合格。人数构成优先用委托时给的数，没给就从需求单的人数构成里读。
> 4. 按每段文字的字号和行数估算它实际占的位置。两段不同的文字估算出来叠在一起，就算不合格，费用页那种两行压在一起的情况会被查出来。
> 5. 以“来源”开头的那一行，左侧同一高度上如果有小图标，算不合格。
> 6. 页标题要是就是栏目名（“费用明细”“包含与不含”之类），算不合格。标题在需求单里找不到的，先作为提醒列出。
>
> 工作说明也会补上这些规矩：事实逐字照写、不加“所以”式归纳；标题用需求单里的结论句；只写和这一团有关的安全提示；那一句要写完整；费用页每一行单独占位、留足行距；来源行不配图标。另外写明自查时要一并给出人数构成里的长者人数和是否高原。
>
> 还不确定的：
> - 叠字是估算出来的，模板里继承来的字号读不到，这部分只能跳过，个别情况可能漏判或误判；
> - 需求单自己转述了调研时，比对以需求单为准；
> - 标题是不是“结论句”没法完全靠机器判断，还要靠工作说明和顾问质检；
> - 客人聊天里的进度提示、需求复述措辞、三条理由写得好不好，由顾问那一侧负责，这里不改。

Design:

> Scope: leaf child Raven-PPT. The change serves both plan-deck-delivery/build-deck and plan-deck-revise/revise-deck; neither node conflicts with it. action.strategy (deck_action.py) is NOT selected and stays unchanged. It imports MARKER, STATUS_PASS, STATUS_FAIL and STATUS_ERROR from deck_audit and resamples, at most 2 per turn (StepView.rollbacks), whenever the latest audit after the last ppt_build is 不通过 or 无法审计. Every new must-fix item therefore becomes a gate trigger with no action-code change. deck_audit.py keeps these names byte-identical: MARKER, STATUS_*, LABEL, FILLER, the factory `create_tool(ctx) -> DeckTextAuditTool`, and tool name `deck_text_audit`. The capability.tools value stays `[{name: deck_text_audit, factory: deck_audit:create_tool}]`.
>
> ## 1. capability.tools — extend deck_audit.py (complete-file resubmission)
>
> ### Evidence for the design
> - The ppt-engine `add_icon` (services/assets/script_helpers.py:441) draws icons as freeform shapes via `build_freeform().convert_to_shape()`, with no text. Template icons are freeform paths or small groups; `swap_icon` treats anything ≤1.8in as an icon.
> - design-brief writes the brief and research files: `briefs/调研-<quote>.md` is research verbatim, and `briefs/需求单-<quote>.md` is the brief (parent playbook plan-deck-delivery). revise-deck passes brief_path and research_path.
> - The workdir is shared with the root; the root already read_files briefs/方案文字-….
>
> ### New optional parameters (existing ones unchanged)
> - `brief_path` (string): default `briefs/需求单-<quote>.md`.
> - `research_path` (string): default `briefs/调研-<quote>.md`.
>   - Both paths resolve relative to workdir.current().
> - `seniors_65` (integer ≥0): the number of travellers aged 65 or over.
> - `plateau` (boolean): whether this is a plateau route.
>
> ### Reading geometry
> `read_deck` also records geometry:
> - For each text-bearing shape: the absolute box in EMU. Group children are mapped through the group's `a:xfrm` off/ext and chOff/chExt: abs = grpOff + (childOff − chOff)·ext/chExt.
> - Tables are treated as one box and excluded from the overlap check.
> - For each text frame: margins (default 0.1in L/R, 0.05in T/B), vertical anchor (`bodyPr anchor`: t, ctr or b; default t) and word_wrap. Each Para keeps its sizes.
> - A title flag: the shape is a TITLE or CENTER_TITLE placeholder.
>
> Icon-like shapes are collected per slide: FREEFORM, PICTURE, or an AUTO_SHAPE or GROUP without text, whose max side is ≤ 0.8in. The recursion through groups is the same as above.
>
> ### Estimated text layout
> The helper `_layout(frame)` estimates each paragraph's y-range and x-range.
> - Size: max(para.sizes). If a paragraph has no explicit size, the frame is skipped for geometry.
> - Character widths: CJK and fullwidth characters are 1.0·size; other characters are 0.55·size.
> - Lines: if wrap is on, lines = ceil(text width / (box width − margins)); otherwise 1.
> - Line height: 1.2·size.
> - Stacking: paragraphs stack from the top inset. For the ctr/b anchors, the total block is centred or bottom-aligned inside the box.
> - Overflow: the estimated block may extend past the box bottom. This is intentional, so that overflowing text that runs into the next shape is caught.
> - x-range: [box left + margin, left + margin + min(avail, longest line width)].
>
> ### New MUST-fix checks (each cites the page and a snippet)
>
> **1. Required sentence.**
> - Take the whitespace-stripped text of the whole deck. If it lacks `天气、开放时间等以出团通知书为准` → must-fix: “下一步与出行须知页缺少完整的一句‘天气、开放时间等以出团通知书为准。’”.
> - If a page has `天气以出团通知书为准` or another truncated form, cite that page.
> - The existing FILLER warning is changed to skip occurrences inside the required sentence.
>
> **2. Fact fidelity.** Runs only when the brief or research text was read.
> - Source = NFKC(brief + research) with all whitespace and Unicode P*/S* characters removed. Deck text is normalized the same way.
> - Split every deck paragraph into clauses on `[，。；、！？!?;\n/|｜]`.
> - A clause is a fact clause if either:
>   - it contains one of 免票, 半价, 优惠, 免费, 门票, 票价, 儿童票, 学生票, 优待, 减免, 岁以下, 岁以上, 周岁, 历时, 航班, 车次; or
>   - it matches the train regex `(?<![A-Za-z])[GDCKZTY]\d{1,5}` or the time regex `\d{1,2}[:：]\d{2}`.
> - Clauses whose normalized length is < 4 are skipped.
> - A fact clause whose normalized text is not a substring of the normalized source → must-fix: “第N页‘<clause>’在需求单和调研原文里找不到，门票、优免、班次照原文逐字写，不自己归纳（如‘一人免票、一人半价’），或删去这句”.
> - Up to 12 such items are reported.
> - If neither file is found → warning “未找到需求单/调研文件，门票班次原文比对未执行”, and the check is not blocking.
>
> **3. Irrelevant safety tips.**
> - Seniors count: use `seniors_65` if given. Otherwise parse the brief:
>   - `(没有|无|不含)\s*65岁以上` → 0;
>   - `65岁以上\s*[:：]?\s*(\d+|零|一|两|二|三|四|五|六)\s*[人位]` → that number.
> - If seniors == 0 and a frame contains 健康申明 or `65\s*岁` → must-fix: “本团没有65岁以上出行人，不写健康申明/65岁提示”.
> - If the seniors count is unknown and those words appear → warning.
> - Plateau: use `plateau` if given. Otherwise it is true iff brief + research contains 玉龙雪山, 香格里拉 or 川西.
> - If plateau is false and a frame contains 高原 → must-fix: “不是高原线路，不写高原相关提示（含‘无高原’）”.
>
> **4. Text overlap.**
> - For each pair of estimated line blocks from different shapes on the same slide (paragraph granularity), compute the intersection:
>   - vertical overlap ≥ 0.4 × the smaller line height, and
>   - horizontal overlap ≥ 0.15in → must-fix: “第N页‘<a前12字>’与‘<b前12字>’估算位置重叠，文字叠压；改 build.py 让每一行各占独立位置、留足行距后重新导出并看渲染”.
> - At most 6 items per page. Pairs within the same frame are not compared.
>
> **5. Icon on a source line.**
> - For each paragraph whose text starts with `来源` (after strip), take its estimated y-range expanded by 0.08in.
> - Any icon-like shape qualifies if all of these hold:
>   - its vertical centre is inside that range;
>   - its right edge is within [text left − 1.0in, text left + 0.3in];
>   - its box does not intersect a paragraph of another row better (take the nearest row by centre distance).
> - A qualifying shape → must-fix: “第N页来源行‘<snippet>’旁有图标，来源行不配图标，删去该图标”.
>
> **6. Column-name title.**
> - The page title is the text of the title placeholder. Otherwise it is the paragraph with the largest explicit size (first one on ties).
> - On pages other than the first and the last: if the normalized title is in {封面, 行程一览, 为什么是这条线, 每日安排, 费用明细, 费用, 包含与不含, 包含, 不含, 下一步, 出行须知, 下一步与出行须知, 联系我们, 重要提示, 住宿, 交通明细} → must-fix: “第N页标题是栏目名，改成需求单里写的结论句”.
> - If the brief text is available and the normalized title is not a substring of it → warning “标题在需求单里找不到，请确认用的是需求单写的结论句”.
>
> ### Unchanged behaviour
> - All existing must/warn checks.
> - The inventory format. Each page section gains one line `本页标题：<title>`, which helps root QC; the rest of the format is the same.
> - The result layout, MAX_ITEMS=40, the error paths, and to_thread execution.
> - Brief and research reads use `read_text(encoding='utf-8', errors='replace')`. A read failure means the file is absent, which gives the warning above.
> - Any exception in the geometry code for one slide is caught per slide and becomes a warning “第N页版面估算失败：<err>”. It must not turn the whole audit into 无法审计.
>
> ## 2. memory.prompt — full values for the three bootstrap files
> - **soul.md**: kept verbatim.
> - **agent.md**: the generic part is kept verbatim. The Hailan “内容规矩” section gains these bullets:
>   - 门票/优免/班次逐字照需求单和调研原文，不写“所以……”式归纳，也不按人头换算（例：两个孩子分别适用“16岁以下免票”“6至18岁半价”时照写两条规则，不写“一人免票、一人半价”）;
>   - 每页标题用需求单写的结论句，不用栏目名或栏目名换个说法;
>   - 安全提示只放需求单里有、且和本团有关的：没有65岁以上出行人不写健康申明/65岁，不是高原线路不写高原（含“无高原”）;
>   - 下一步页完整写“天气、开放时间等以出团通知书为准。”，一字不减;
>   - 费用页每一行（含险合计、报价有效期等）各自单独一个文本框或一行，留足行距，看渲染确认不叠;
>   - 图标只标明这一行信息的种类，来源行不配图标。
> - **agent.md 固定顺序 step 3** now reads: `deck_text_audit(quote_number, salutation, nights, seniors_65=需求单人数构成里65岁以上人数, plateau=是否含玉龙雪山/香格里拉/川西)`. For revise-deck it also passes `brief_path` and `research_path` from the task.
> - **TOOLS.md**: the generic part is kept verbatim. The deck_text_audit section lists:
>   - the new parameters and defaults;
>   - the new must-fix items: facts not traceable to the original text, the missing full sentence, irrelevant safety tips, estimated text overlap, source-line icons, and column-name titles;
>   - a note that overlap is an estimate, and that rendered pages must still be checked by eye.
>
> ## Failure behaviour and bounds
> - False positives cost at most 2 gate resamples per turn. After that deck_action accepts and records last_problems; the root QC remains the backstop.
> - Missing brief or research files degrade to warnings.
> - Frames with inherited sizes are excluded from geometry checks, not guessed.

| Target | Facet | Reason | Expected | Criteria |
|---|---|---|---|---|
| capability.tools | capability | mechanism_activity (family, Raven-PPT) shows deck_text_audit was constructed and the deck_action gate assessed twice and accepted. Yet the unmet must_hold items recurred: the derived '一人免票、一人半价', the truncated '天气以出团通知书为准', the 65岁 health tip for a group without seniors, the fee-page overlap, icons on 来源 lines, and a column-name title. The audit either did not check these or only warned, so the gate had nothing to act on. | The same tool, called after each ppt_build, now reports must-fix items for:<br>- fact clauses (tickets, discounts, trains, times) not found verbatim in briefs/需求单 or briefs/调研;<br>- the full sentence 天气、开放时间等以出团通知书为准 missing;<br>- 健康申明/65岁 when there are no seniors, or 高原 on a non-plateau route;<br>- estimated text overlap between shapes;<br>- an icon beside a 来源 line;<br>- a bare column-name title.<br>The existing gate then resamples until these are fixed (≤2). Existing checks, the inventory file and the result markers are unchanged. | intake-and-confirmation (round), no-internal-chatter (round), deck-no-forbidden-content (round), deck-content-rules (round), deck-structure (round), deck-aesthetics (round) |
| memory.prompt | memory | The child's Hailan rules lacked explicit statements on verbatim facts without derived summaries, relevant-only safety tips, the full next-steps sentence, independent fee-page rows and no icons on source lines. They also did not tell the model to pass seniors and plateau information to the audit. | The child writes ticket and train facts verbatim from the brief, uses the brief's conclusion titles, omits irrelevant tips, writes the full sentence, gives each fee row its own position and puts no icon on 来源 lines. It calls deck_text_audit with seniors_65 and plateau, plus brief_path and research_path on rework. The generic PPT guidance is preserved. | intake-and-confirmation (round), no-internal-chatter (round), deck-no-forbidden-content (round), deck-content-rules (round), deck-structure (round), deck-aesthetics (round) |

Validation: errors none; bound targets capability.tools, memory.prompt, action.strategy.

`files` `deck_audit.py`: modified, +244 -96

```diff
--- a/files/deck_audit.py
+++ b/files/deck_audit.py
@@ -2,15 +2,19 @@
 
 The tool owns the audit rules and the inventory file briefs/方案文字-<quote>.md in
 the running turn's working directory. It is stateless; each call re-reads the
-finished .pptx with python-pptx and overwrites the inventory, so the file always
-reflects the deck as last exported. Result text starts with MARKER and one of the
-STATUS_* lines; deck_action reads those lines from the transcript.
+finished .pptx with python-pptx (deck_read) and overwrites the inventory, so the
+file always reflects the deck as last exported. It also reads the brief
+(briefs/需求单-<quote>.md) and research (briefs/调研-<quote>.md) read-only, to check
+that ticket/discount/train facts are verbatim and safety tips are relevant.
+Result text starts with MARKER and one of the STATUS_* lines; deck_action reads
+those lines from the transcript.
 """
 
 from __future__ import annotations
 
 import asyncio
 import re
+import unicodedata
 from dataclasses import dataclass
 from pathlib import Path
 from typing import Any
@@ -18,6 +22,9 @@
 from raven.agent import workdir
 from raven.contracts.tool import Tool, ToolResult
 from raven.plugins.context import PluginContext
+
+from .deck_read import Page, overlap_problems, page_blocks, read_deck, source_icon_problems
+from .deck_read import clean as _clean
 
 MARKER = "【方案文字清单审计】"
 STATUS_PASS = "状态：通过"
@@ -25,6 +32,7 @@
 STATUS_ERROR = "状态：无法审计"
 LABEL = "以计调确认为准"
 FILLER = "以出团通知书为准"
+REQUIRED = "天气、开放时间等以出团通知书为准"
 
 FORBIDDEN = ("调研", "未查到", "需求单", "质检", "模板", "［", "］")
 SECTION_RE = re.compile(r"第\s*[0-9一二三四五六七八九十]+\s*节")
@@ -38,40 +46,58 @@
 NIGHTS_RE = re.compile(r"([0-9]+|[一两二三四五六七八九十])\s*晚(?![餐饭宴会上间])")
 CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
 MAX_ITEMS = 40
+MAX_FACT_ITEMS = 12
+
+FACT_WORDS = (
+    "免票", "半价", "优惠", "免费", "门票", "票价", "儿童票", "学生票", "优待", "减免",
+    "岁以下", "岁以上", "周岁", "历时", "航班", "车次",
+)
+TRAIN_RE = re.compile(r"(?<![A-Za-z])[GDCKZTY]\d{1,5}(?!\d)")
+TIME_RE = re.compile(r"\d{1,2}[:：]\d{2}")
+CLAUSE_SPLIT_RE = re.compile(
+    r"[，。；;、！？!?\n\v/|｜（）()【】「」“”\"]|(?<!\d)[:：]|[:：](?!\d)|(?<!\d),|,(?!\d)"
+)
+SENIOR_NUM_RE = re.compile(r"65\s*岁以上\s*[:：]?\s*(\d+|[零一两二三四五六])\s*[人位]")
+SENIOR_NONE_RE = re.compile(r"(没有|无|不含)\s*65\s*岁以上")
+SENIOR_ALLOWED_RE = re.compile(
+    r"65\s*岁以上\s*[:：]?\s*[0零]\s*[人位]|(没有|无|不含)\s*65\s*岁以上(的)?(长辈|出行人|老人|长者)?"
+)
+SENIOR_TIP_RE = re.compile(r"健康申明|65\s*岁")
+SENIOR_CN = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
+PLATEAU_TIP_RE = re.compile(r"(无|非|不是|没有|不含|不涉及)\s*高原|高原\s*(反应|行程|线路|路线|提示|风险|地区)")
+PLATEAU_PLACES = ("玉龙雪山", "香格里拉", "川西")
+
+
+def _norm(text: str) -> str:
+    text = unicodedata.normalize("NFKC", text)
+    return "".join(ch for ch in text if not ch.isspace() and unicodedata.category(ch)[0] not in "PS")
+
+
+COLUMN_TITLES = frozenset(
+    _norm(t)
+    for t in (
+        "封面", "行程一览", "为什么是这条线", "每日安排", "费用明细", "费用", "包含与不含", "包含", "不含",
+        "下一步", "出行须知", "下一步与出行须知", "联系我们", "重要提示", "住宿", "交通明细",
+    )
+)
 
 
 @dataclass
-class Para:
-    text: str
-    sizes: list[float]
-    inherited: bool
-
-
-@dataclass
-class Frame:
-    paras: list[Para]
+class Sources:
+    brief: str | None = None
+    research: str | None = None
+    brief_path: Path | None = None
+    research_path: Path | None = None
+    seniors: int | None = None
+    plateau: bool | None = None
+
+    @property
+    def found(self) -> bool:
+        return self.brief is not None or self.research is not None
 
     @property
     def text(self) -> str:
-        return "\n".join(p.text for p in self.paras)
-
-
-@dataclass
-class Page:
-    number: int
-    frames: list[Frame]
-
-    @property
-    def sizes(self) -> list[float]:
-        return sorted({s for f in self.frames for p in f.paras for s in p.sizes})
-
-    @property
-    def inherited(self) -> bool:
-        return any(p.inherited for f in self.frames for p in f.paras)
-
-
-def _clean(text: str) -> str:
-    return re.sub(r"[\v\n\r]+", " ", text).strip()
+        return (self.brief or "") + "\n" + (self.research or "")
 
 
 def _fmt(size: float) -> str:
@@ -87,67 +113,12 @@
     return flat[start : idx + len(needle) + width]
 
 
-def _iter_frames(shapes: Any, group_type: Any):
-    for shape in shapes:
-        try:
-            kind = shape.shape_type
-        except Exception:
-            kind = None
-        if kind == group_type:
-            yield from _iter_frames(shape.shapes, group_type)
-            continue
-        if getattr(shape, "has_table", False):
-            for row in shape.table.rows:
-                for cell in row.cells:
-                    if getattr(cell, "is_spanned", False):
-                        continue
-                    yield cell.text_frame
-            continue
-        if getattr(shape, "has_text_frame", False):
-            yield shape.text_frame
-
-
-def read_deck(path: Path) -> list[Page]:
-    from pptx import Presentation
-    from pptx.enum.shapes import MSO_SHAPE_TYPE
-
-    prs = Presentation(str(path))
-    pages: list[Page] = []
-    for number, slide in enumerate(prs.slides, 1):
-        frames: list[Frame] = []
-        for tf in _iter_frames(slide.shapes, MSO_SHAPE_TYPE.GROUP):
-            paras: list[Para] = []
-            for p in tf.paragraphs:
-                text = _clean(p.text)
-                if not text:
-                    continue
-                sizes: set[float] = set()
-                inherited = False
-                psize = p.font.size
-                runs = [r for r in p.runs if r.text.strip()]
-                for r in runs:
-                    size = r.font.size or psize
-                    if size is None:
-                        inherited = True
-                    else:
-                        sizes.add(round(size.pt, 1))
-                if not runs and psize is not None:
-                    sizes.add(round(psize.pt, 1))
-                if not sizes:
-                    inherited = True
-                paras.append(Para(text, sorted(sizes), inherited))
-            if paras:
-                frames.append(Frame(paras))
-        pages.append(Page(number, frames))
-    return pages
-
-
 def render_inventory(pages: list[Page], deck: Path, quote: str) -> str:
     lines = [f"# 方案文字清单 {deck.name}", "", f"报价编号：{quote}；共 {len(pages)} 页；成品：{deck}", ""]
     for page in pages:
         sizes = " / ".join(_fmt(s) for s in page.sizes) or "无显式字号"
         extra = "（另有继承字号）" if page.inherited else ""
-        lines += [f"## 第{page.number}页", "", f"本页显式字号：{sizes} 磅{extra}", ""]
+        lines += [f"## 第{page.number}页", "", f"本页标题：{page.title or '（未识别）'}", f"本页显式字号：{sizes} 磅{extra}", ""]
         if not page.frames:
             lines.append("- （本页没有文字）")
         for i, frame in enumerate(page.frames, 1):
@@ -166,8 +137,113 @@
     return CN_NUM.get(token)
 
 
+def seniors_from(text: str) -> int | None:
+    numbers: list[int] = []
+    for m in SENIOR_NUM_RE.finditer(text):
+        token = m.group(1)
+        value = int(token) if token.isdigit() else SENIOR_CN.get(token)
+        if value is not None:
+            numbers.append(value)
+    if numbers:
+        return max(numbers)
+    if SENIOR_NONE_RE.search(text):
+        return 0
+    return None
+
+
+def _is_fact(clause: str) -> bool:
+    return any(w in clause for w in FACT_WORDS) or bool(TRAIN_RE.search(clause)) or bool(TIME_RE.search(clause))
+
+
+def _fact_problems(pages: list[Page], source_norm: str) -> list[str]:
+    out: list[str] = []
+    seen: set[tuple[int, str]] = set()
+    for page in pages:
+        for frame in page.frames:
+            for para in frame.paras:
+                for clause in CLAUSE_SPLIT_RE.split(para.text):
+                    clause = clause.strip()
+                    if not clause or not _is_fact(clause):
+                        continue
+                    key = _norm(clause)
+                    if len(key) < 4 or key in source_norm or (page.number, key) in seen:
+                        continue
+                    seen.add((page.number, key))
+                    out.append(
+                        f"第{page.number}页“{clause[:40]}”在需求单和调研原文里找不到；门票、优免、班次照原文逐字写，"
+                        "不自己归纳（如“一人免票、一人半价”），或删去这句"
+                    )
+                    if len(out) >= MAX_FACT_ITEMS:
+                        return out
+    return out
+
+
+def _extra(pages: list[Page], src: Sources, must: list[str], warn: list[str]) -> None:
+    count = len(pages)
+    # 1. the required sentence on the next-steps page
+    compact = {p.number: re.sub(r"\s", "", "\n".join(f.text for f in p.frames)) for p in pages}
+    holders = [n for n, t in compact.items() if REQUIRED in t]
+    if not holders:
+        partial = [n for n, t in compact.items() if FILLER in t]
+        where = "、".join(f"第{n}页" for n in partial)
+        must.append(
+            "下一步与出行须知页缺少完整的一句“天气、开放时间等以出团通知书为准。”，一字不减"
+            + (f"（{where}只有不完整的“…以出团通知书为准”）" if partial else "")
+        )
+    elif count >= 3 and (count - 1) not in holders:
+        warn.append(f"“{REQUIRED}”应放在下一步与出行须知页（倒数第2页），现在在第{holders[0]}页")
+    # 2. fact fidelity against brief + research
+    if src.found:
+        must.extend(_fact_problems(pages, _norm(src.text)))
+    else:
+        warn.append("未找到需求单/调研文件，门票、班次原文比对未执行（可传 brief_path、research_path 指定）")
+    # 3. irrelevant safety tips
+    for page in pages:
+        for frame in page.frames:
+            text = frame.text
+            stripped = SENIOR_ALLOWED_RE.sub("", text)
+            hit = SENIOR_TIP_RE.search(stripped)
+            if hit and src.seniors == 0:
+                must.append(
+                    f"第{page.number}页“{_snip(stripped, hit.group(0))}”：本团没有65岁以上出行人，不写健康申明/65岁提示"
+                )
+            elif hit and src.seniors is None:
+                warn.append(f"第{page.number}页有“{hit.group(0)}”，请核对本团是否有65岁以上出行人（可传 seniors_65）")
+            plateau_hit = PLATEAU_TIP_RE.search(text)
+            if plateau_hit and src.plateau is False:
+                must.append(
+                    f"第{page.number}页“{_snip(text, plateau_hit.group(0))}”：不是高原线路，不写高原相关提示（含“无高原”）"
+                )
+    # 4/5. estimated overlap and icons on source lines
+    for page in pages:
+        if page.geo_error:
+            warn.append(f"第{page.number}页版面估算失败：{page.geo_error}；叠字和来源行图标未检查，请看渲染自查")
+            continue
+        try:
+            blocks = page_blocks(page)
+            must.extend(overlap_problems(page, blocks))
+            must.extend(source_icon_problems(page, blocks))
+        except Exception as exc:
+            warn.append(f"第{page.number}页版面估算失败：{type(exc).__name__}: {exc}；请看渲染自查叠字和图标")
+    # 6. column-name titles
+    brief_norm = _norm(src.brief) if src.brief else ""
+    for page in pages:
+        if page.number in (1, count) or not page.title:
+            continue
+        key = _norm(page.title)
+        if key in COLUMN_TITLES:
+            must.append(f"第{page.number}页标题“{page.title[:20]}”是栏目名，改成需求单里写的结论句")
+        elif brief_norm and key and key not in brief_norm:
+            warn.append(f"第{page.number}页标题“{page.title[:24]}”在需求单里找不到，请确认用的是需求单写的结论句")
+
+
 def audit(
-    pages: list[Page], deck: Path, quote: str, salutation: str | None, nights: int | None
+    pages: list[Page],
+    deck: Path,
+    quote: str,
+    salutation: str | None,
+    nights: int | None,
+    sources: Sources | None = None,
 ) -> tuple[list[str], list[str]]:
     must: list[str] = []
     warn: list[str] = []
@@ -208,7 +284,7 @@
                     value = _night_value(m.group(1))
                     if value is not None and value != nights:
                         must.append(f"第{n}页写了“{m.group(0)}”，报价是{nights}晚")
-            if FILLER in text:
+            if FILLER in re.sub(r"\s", "", text).replace(REQUIRED, ""):
                 warn.append(f"第{n}页有“{FILLER}”：“{_snip(text, FILLER)}”；未查证的项目应直接删去，不留格子")
             points = [p for p in frame.paras if len(re.sub(r"\s", "", p.text)) > 8]
             if len(points) > 3:
@@ -220,11 +296,44 @@
                     warn.append(f"第{n}页有一段 {length} 字：“{para.text[:24]}…”，每点不超过三十字")
     if salutation and salutation not in all_text:
         warn.append(f"全篇没有找到称呼“{salutation}”，请核对封面和费用页")
+    _extra(pages, sources or Sources(), must, warn)
     return list(dict.fromkeys(must)), list(dict.fromkeys(warn))
 
 
 def _error(reason: str) -> ToolResult:
     return ToolResult(model_text=f"Error: {MARKER}{STATUS_ERROR}——{reason}", ok=False)
+
+
+def _parse_bool(value: Any) -> bool | None:
+    if value in (None, ""):
+        return None
+    if isinstance(value, bool):
+        return value
+    text = str(value).strip().lower()
+    if text in ("true", "1", "yes", "y", "是"):
+        return True
+    if text in ("false", "0", "no", "n", "否"):
+        return False
+    raise ValueError(value)
+
+
+def _read_source(root: Path, raw: Any, name: str) -> tuple[str | None, Path]:
+    if raw:
+        path = Path(str(raw)).expanduser()
+        if not path.is_absolute():
+            path = root / path
+    else:
+        path = root / "briefs" / name
+        if not path.is_file():
+            found = [p for p in root.rglob(name) if p.is_file()]
+            if found:
+                path = found[0]
+    if not path.is_file():
+        return None, path
+    try:
+        return path.read_text(encoding="utf-8", errors="replace"), path.resolve()
+    except OSError:
+        return None, path
 
 
 class DeckTextAuditTool(Tool):
@@ -238,10 +347,11 @@
     def description(self) -> str:
         return (
             "海岚旅行方案成品的文字清单导出与自查。用 python-pptx 打开已导出的 .pptx，"
-            "把每页页号、全部文字和各段落实际字号（磅；取不到显式字号写“继承”）写成 "
+            "把每页页号、标题、全部文字和各段落实际字号（磅；取不到显式字号写“继承”）写成 "
             "briefs/方案文字-<报价编号>.md（覆盖旧文件），并列出必须修改的项：内部字样（调研、未查到、需求单、质检、模板、［］、第N节）、"
-            "“最快”“当天”、酒店名缺“以计调确认为准”、一页超过三种字号、需求单称呼里没有的先生/女士/小姐、晚数与报价不一致、文件名或目录不对；"
-            "要点数、每点字数和“以出团通知书为准”作为提醒。每次 ppt_build 导出后都要重新运行，状态为通过才算完成。"
+            "“最快”“当天”、酒店名缺“以计调确认为准”、一页超过三种字号、需求单称呼里没有的先生/女士/小姐、晚数与报价不一致、文件名或目录不对、"
+            "门票优免班次句子在需求单和调研原文里找不到、缺完整的“天气、开放时间等以出团通知书为准”、与本团无关的健康申明/高原提示、"
+            "估算文字叠压、来源行配图标、标题是栏目名；要点数、每点字数作为提醒。每次 ppt_build 导出后都要重新运行，状态为通过才算完成。"
         )
 
     @property
@@ -256,6 +366,10 @@
                 },
                 "salutation": {"type": "string", "description": "需求单里的称呼（customer_name）原文，一字不改"},
                 "nights": {"type": "integer", "minimum": 0, "description": "报价单的晚数"},
+                "seniors_65": {"type": "integer", "minimum": 0, "description": "需求单人数构成里65岁以上的人数，没有写0"},
+                "plateau": {"type": "boolean", "description": "线路是否含玉龙雪山、香格里拉、川西等高原行程"},
+                "brief_path": {"type": "string", "description": "需求单路径；默认 briefs/需求单-<报价编号>.md"},
+                "research_path": {"type": "string", "description": "调研结果路径；默认 briefs/调研-<报价编号>.md"},
             },
             "required": ["quote_number"],
         }
@@ -275,14 +389,31 @@
             nights = int(nights_raw) if nights_raw not in (None, "") else None
         except (TypeError, ValueError):
             return _error(f"nights 不是整数：{nights_raw!r}")
+        seniors_raw = kwargs.get("seniors_65")
         try:
... 68 more diff lines
```

`files` `deck_read.py`: added, +491 -0

```diff
--- a/files/deck_read.py
+++ b/files/deck_read.py
@@ -0,0 +1,491 @@
+"""Deck reading for deck_text_audit: text, explicit sizes, shape boxes and a layout estimate.
+
+Positions are in points on the slide. The layout estimate is deliberately simple
+(CJK/fullwidth = 1 em, other characters = 0.55 em, line = 1.2 x size, honouring
+explicit line spacing, paragraph spacing, anchor and normAutofit scale) and only
+runs for frames whose every non-empty paragraph has an explicit size; inherited
+sizes are not guessed. Icons are small text-less freeforms, pictures, auto shapes
+or groups, which is how ppt-engine's add_icon and the template draw them.
+"""
+
+from __future__ import annotations
+
+import math
+import re
+import unicodedata
+from dataclasses import dataclass, field
+from pathlib import Path
+from typing import Any
+
+EMU_PER_PT = 12700.0
+INCH = 72.0
+ROOT_XF = (1.0 / EMU_PER_PT, 1.0 / EMU_PER_PT, 0.0, 0.0)
+MAX_OVERLAP_PER_PAGE = 6
+
+
+def clean(text: str) -> str:
+    return re.sub(r"[\v\n\r]+", " ", text).strip()
+
+
+@dataclass
+class Para:
+    text: str
+    sizes: list[float]
+    inherited: bool
+    raw: str = ""
+    size: float | None = None
+    align: str = "l"
+    spacing: tuple[str, float] | None = None
+    before: float = 0.0
+    after: float = 0.0
+
+
+@dataclass
+class Geo:
+    box: tuple[float, float, float, float] | None
+    ml: float = 7.2
+    mr: float = 7.2
+    mt: float = 3.6
+    mb: float = 3.6
+    anchor: str = "t"
+    wrap: bool = True
+    scale: float = 1.0
+    cut: float = 0.0
+    title: bool = False
+    table: bool = False
+
+
+@dataclass
+class Frame:
+    paras: list[Para]
+    geo: Geo | None = None
+    layout: list[Para] = field(default_factory=list)
+
+    @property
+    def text(self) -> str:
+        return "\n".join(p.text for p in self.paras)
+
+
+@dataclass
+class Icon:
+    x: float
+    y: float
+    w: float
+    h: float
+
+    @property
+    def right(self) -> float:
+        return self.x + self.w
+
+    @property
+    def cy(self) -> float:
+        return self.y + self.h / 2
+
+
+@dataclass
+class Block:
+    text: str
+    frame: int
+    x0: float
+    x1: float
+    y0: float
+    y1: float
+    lh: float
+    coarse: bool = False
+
+
+@dataclass
+class Page:
+    number: int
+    frames: list[Frame]
+    icons: list[Icon] = field(default_factory=list)
+    title: str = ""
+    geo_error: str = ""
+
+    @property
+    def sizes(self) -> list[float]:
+        return sorted({s for f in self.frames for p in f.paras for s in p.sizes})
+
+    @property
+    def inherited(self) -> bool:
+        return any(p.inherited for f in self.frames for p in f.paras)
+
+
+def _pt(length: Any) -> float:
+    if length is None:
+        return 0.0
+    try:
+        return float(length.pt)
+    except Exception:
+        return 0.0
+
+
+def _pct(value: str | None, default: float) -> float:
+    if not value:
+        return default
+    try:
+        if value.endswith("%"):
+            return float(value[:-1]) / 100.0
+        return int(value) / 100000.0
+    except ValueError:
+        return default
+
+
+def _read_para(p: Any) -> Para:
+    from pptx.enum.text import PP_ALIGN
+    from pptx.oxml.ns import qn
+
+    raw = p.text or ""
+    text = clean(raw)
+    sizes: set[float] = set()
+    inherited = False
+    psize = p.font.size
+    runs = [r for r in p.runs if r.text.strip()]
+    for r in runs:
+        size = r.font.size or psize
+        if size is None:
+            inherited = True
+        else:
+            sizes.add(round(size.pt, 1))
+    if not runs and psize is not None:
+        sizes.add(round(psize.pt, 1))
+    if text and not sizes:
+        inherited = True
+    layout_size = max(sizes) if sizes else None
+    if layout_size is None and not text:
+        end = p._p.find(qn("a:endParaRPr"))
+        if end is not None and end.get("sz"):
+            try:
+                layout_size = int(end.get("sz")) / 100.0
+            except ValueError:
+                layout_size = None
+    align = "l"
+    try:
+        if p.alignment == PP_ALIGN.CENTER:
+            align = "ctr"
+        elif p.alignment == PP_ALIGN.RIGHT:
+            align = "r"
+    except Exception:
+        pass
+    spacing: tuple[str, float] | None = None
+    try:
+        ls = p.line_spacing
+        if isinstance(ls, float):
+            spacing = ("mul", ls)
+        elif ls is not None:
+            spacing = ("pt", float(ls.pt))
+    except Exception:
+        spacing = None
+    try:
+        before, after = _pt(p.space_before), _pt(p.space_after)
+    except Exception:
+        before, after = 0.0, 0.0
+    return Para(text, sorted(sizes), inherited, raw, layout_size, align, spacing, before, after)
+
+
+def _geo(tf: Any, box: tuple[float, float, float, float] | None, title: bool, table: bool) -> Geo:
+    from pptx.oxml.ns import qn
+
+    geo = Geo(box, title=title, table=table)
+    try:
+        body = tf._txBody.find(qn("a:bodyPr"))
+    except Exception:
+        return Geo(None, title=title, table=table)
+    if body is None:
+        return geo
+
+    def inset(name: str, default: float) -> float:
+        value = body.get(name)
+        try:
+            return int(value) / EMU_PER_PT if value is not None else default
+        except ValueError:
+            return default
+
+    geo.ml, geo.mr = inset("lIns", 7.2), inset("rIns", 7.2)
+    geo.mt, geo.mb = inset("tIns", 3.6), inset("bIns", 3.6)
+    geo.anchor = body.get("anchor") or "t"
+    geo.wrap = body.get("wrap") != "none"
+    auto = body.find(qn("a:normAutofit"))
+    if auto is not None:
+        geo.scale = _pct(auto.get("fontScale"), 1.0)
+        geo.cut = _pct(auto.get("lnSpcReduction"), 0.0)
+    return geo
+
+
+def _read_frame(tf: Any, box: tuple[float, float, float, float] | None, title: bool, table: bool) -> Frame | None:
+    layout = [_read_para(p) for p in tf.paragraphs]
+    paras = [p for p in layout if p.text]
+    if not paras:
+        return None
+    return Frame(paras, _geo(tf, box, title, table), layout)
+
+
+def _box(shape: Any, xf: tuple[float, float, float, float]) -> tuple[float, float, float, float] | None:
+    try:
+        left, top, width, height = shape.left, shape.top, shape.width, shape.height
+    except Exception:
+        return None
+    if left is None or top is None or width is None or height is None:
+        return None
+    sx, sy, tx, ty = xf
+    return (tx + sx * int(left), ty + sy * int(top), sx * int(width), sy * int(height))
+
+
+def _group_xf(el: Any, xf: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
+    from pptx.oxml.ns import qn
+
+    sx, sy, tx, ty = xf
+    sppr = el.find(qn("p:grpSpPr"))
+    xfrm = sppr.find(qn("a:xfrm")) if sppr is not None else None
+    if xfrm is None:
+        return xf
+    off, ext, choff, chext = (xfrm.find(qn(f"a:{n}")) for n in ("off", "ext", "chOff", "chExt"))
+    if off is None or ext is None or choff is None or chext is None:
+        return xf
+    ccx, ccy = int(chext.get("cx", 0)), int(chext.get("cy", 0))
+    rx = int(ext.get("cx", 0)) / ccx if ccx else 1.0
+    ry = int(ext.get("cy", 0)) / ccy if ccy else 1.0
+    return (
+        sx * rx,
+        sy * ry,
+        tx + sx * (int(off.get("x", 0)) - int(choff.get("x", 0)) * rx),
+        ty + sy * (int(off.get("y", 0)) - int(choff.get("y", 0)) * ry),
+    )
+
+
+def _iconish(box: tuple[float, float, float, float], kind: str) -> bool:
+    w, h = box[2], box[3]
+    side = max(w, h)
+    if side > 0.8 * INCH:
+        return False
+    if kind == "auto":
+        return side >= 0.15 * INCH and min(w, h) >= 0.05 * INCH
+    return side >= 0.08 * INCH
+
+
+def _is_title(shape: Any) -> bool:
+    try:
+        if not shape.is_placeholder:
+            return False
+        from pptx.enum.shapes import PP_PLACEHOLDER
+
+        return shape.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
+    except Exception:
+        return False
+
+
+def _walk(shapes: Any, xf: tuple[float, float, float, float], frames: list[Frame], icons: list[Icon]) -> None:
+    from pptx.oxml.ns import qn
+
+    for shape in shapes:
+        el = shape._element
+        tag = el.tag.rsplit("}", 1)[-1]
+        if tag == "grpSp":
+            before = len(frames)
+            inner: list[Icon] = []
+            _walk(shape.shapes, _group_xf(el, xf), frames, inner)
+            box = _box(shape, xf)
+            if len(frames) == before and box is not None and _iconish(box, "group"):
+                icons.append(Icon(*box))
+            else:
+                icons.extend(inner)
+            continue
+        box = _box(shape, xf)
+        if getattr(shape, "has_table", False):
+            for row in shape.table.rows:
+                for cell in row.cells:
+                    if getattr(cell, "is_spanned", False):
+                        continue
+                    frame = _read_frame(cell.text_frame, None, False, True)
+                    if frame is not None:
+                        frames.append(frame)
+            continue
+        if tag == "pic":
+            if box is not None and _iconish(box, "pic"):
+                icons.append(Icon(*box))
+            continue
+        if getattr(shape, "has_text_frame", False):
+            frame = _read_frame(shape.text_frame, box, _is_title(shape), False)
+            if frame is not None:
+                frames.append(frame)
+                continue
+        if tag == "sp" and box is not None:
+            kind = "free" if el.find(".//" + qn("a:custGeom")) is not None else "auto"
+            if _iconish(box, kind):
+                icons.append(Icon(*box))
+
+
+def _plain_frames(shapes: Any, group_type: Any, frames: list[Frame]) -> None:
+    """Text-only fallback (the previous reader) when geometry reading fails on a slide."""
+    for shape in shapes:
+        try:
+            kind = shape.shape_type
+        except Exception:
+            kind = None
+        if kind == group_type:
+            _plain_frames(shape.shapes, group_type, frames)
+            continue
+        tfs: list[Any] = []
+        if getattr(shape, "has_table", False):
+            for row in shape.table.rows:
+                for cell in row.cells:
+                    if not getattr(cell, "is_spanned", False):
+                        tfs.append(cell.text_frame)
+        elif getattr(shape, "has_text_frame", False):
+            tfs.append(shape.text_frame)
+        for tf in tfs:
+            layout = [_read_para(p) for p in tf.paragraphs]
+            paras = [p for p in layout if p.text]
+            if paras:
+                frames.append(Frame(paras, None, layout))
+
+
+def page_title(page: Page) -> str:
+    for frame in page.frames:
+        if frame.geo is not None and frame.geo.title:
+            return clean(" ".join(p.text for p in frame.paras))
+    best, best_size = "", -1.0
+    for frame in page.frames:
+        if frame.geo is not None and frame.geo.table:
+            continue
+        for p in frame.paras:
+            if p.sizes and max(p.sizes) > best_size:
+                best, best_size = p.text, max(p.sizes)
+    return best
+
+
+def read_deck(path: Path) -> list[Page]:
+    from pptx import Presentation
+    from pptx.enum.shapes import MSO_SHAPE_TYPE
+
+    prs = Presentation(str(path))
+    pages: list[Page] = []
+    for number, slide in enumerate(prs.slides, 1):
+        frames: list[Frame] = []
+        icons: list[Icon] = []
+        error = ""
+        try:
+            _walk(slide.shapes, ROOT_XF, frames, icons)
+        except Exception as exc:  # keep the inventory; report the geometry failure as a warning
+            error = f"{type(exc).__name__}: {exc}"
+            frames, icons = [], []
+            _plain_frames(slide.shapes, MSO_SHAPE_TYPE.GROUP, frames)
+        page = Page(number, frames, icons, geo_error=error)
+        page.title = page_title(page)
+        pages.append(page)
+    return pages
+
+
+def _char_width(ch: str, size: float) -> float:
+    return size if unicodedata.east_asian_width(ch) in ("W", "F") else 0.55 * size
+
+
+def layout(frame: Frame, index: int) -> list[Block] | None:
+    geo = frame.geo
+    if geo is None or geo.box is None or geo.table:
+        return None
+    if any(p.text and p.size is None for p in frame.layout):
+        return None
+    known = [p.size for p in frame.layout if p.size]
+    if not known:
+        return None
+    fallback = max(known)
+    left, top, width, height = geo.box
+    avail = max(width - geo.ml - geo.mr, 1.0)
+    rows: list[tuple[Para, float, float, float, float]] = []
+    cur = 0.0
+    for p in frame.layout:
... 94 more diff lines
```

`memory.prompt` `TOOLS.md`: modified, +18 -2

```diff
--- a/memory.prompt/TOOLS.md
+++ b/memory.prompt/TOOLS.md
@@ -44,9 +44,25 @@
 
 ## deck_text_audit
 
-海岚旅行方案要求的“文字清单”就由它导出：它用 python-pptx 打开成品，把每页页号、全部文字和各段落实际字号（磅；取不到显式字号写“继承”）写成 `briefs/方案文字-<报价编号>.md`，覆盖旧文件，并审计内部字样、“最快/当天”、酒店标注、字号级数、称呼、晚数和文件名。不需要自己用 exec 写导出脚本。
+海岚旅行方案要求的“文字清单”就由它导出：它用 python-pptx 打开成品，把每页页号、标题、全部文字和各段落实际字号（磅；取不到显式字号写“继承”）写成 `briefs/方案文字-<报价编号>.md`，覆盖旧文件。不需要自己用 exec 写导出脚本。
 
-- 参数：quote_number（必填）、salutation（需求单称呼原文）、nights（报价晚数）；pptx_path 默认 `deliverables/海岚旅行-<报价编号>.pptx`。
+- 参数：
+  - quote_number（必填）；
+  - salutation（需求单称呼原文）；
+  - nights（报价晚数）；
+  - seniors_65（需求单人数构成里65岁以上的人数，没有写0）；
+  - plateau（是否含玉龙雪山、香格里拉、川西的高原线路）；
+  - brief_path、research_path：默认 `briefs/需求单-<报价编号>.md`、`briefs/调研-<报价编号>.md`，返工时照任务给的路径传；
+  - pptx_path：默认 `deliverables/海岚旅行-<报价编号>.pptx`。
+- 必须修改的项：
+  - 内部字样、“最快/当天”、酒店缺标注、一页超过三种字号、自己加的先生/女士、晚数不一致、文件名或目录不对；
+  - 门票、优免、班次、时刻的句子在需求单和调研原文里找不到（照原文逐字写，不归纳）；
+  - 缺完整的“天气、开放时间等以出团通知书为准”；
+  - 没有65岁以上出行人却写了健康申明/65岁，不是高原线路却写了高原；
+  - 按字号估算出的文字叠压；
+  - 来源行旁有图标；
+  - 标题是栏目名。
+- 叠字是估算，模板继承的字号读不到的文本框不参与估算：通过了也要逐页看渲染。
 - 每次 `ppt_build` 导出后都要重新运行；“状态：通过”之前不要给最后的回答。
 - 修改一律改 build.py 再 `ppt_build`，不要用 exec 直接改 .pptx。
 - 只有它报“无法审计”且原因不是成品缺失时，才用 exec 运行 python-pptx 按同样格式导出清单，并在最后的回答里说明。
```

`memory.prompt` `agent_memory/profile/agent.md`: modified, +15 -3

```diff
--- a/memory.prompt/agent_memory/profile/agent.md
+++ b/memory.prompt/agent_memory/profile/agent.md
@@ -41,20 +41,32 @@
 
 - **称呼**：照需求单 customer_name 一字不改（例如“尊敬的客人”），绝不自己加“先生”“女士”“小姐”。
 - **只用给定内容**：只用需求单“对客文案”和调研结果里的文字；“设计说明”不进 PPT。不虚构交通班次、开放时间、门票、天气、酒店、餐厅。
+- **门票、优免、班次逐字照原文**：照需求单和调研原文逐字写，不写“所以……”式归纳，不按人头换算，不合并改写。例：两个孩子分别适用“16岁以下免票”“6至18岁半价”时，照写这两条规则，绝不写“一人免票、一人半价”。
 - **外部事实保留适用范围**：调研写“9月26日代表性排班”，就写成代表性排班，不写成出发当天的班次，不写“最快”“当天”。
 - **来源**：写在事实旁边，只写调研里列出的真实网站名或链接，不写“调研第几节”。
 - **酒店**：每个酒店名所在的同一文本框里写“参考酒店，以计调确认为准”。
 - **没查证的直接删**：调研没查到的项目整行/整格删去，不留格子填“以出团通知书为准”。
 - **不留内部痕迹**：全篇不出现“调研”“未查到”“需求单”“质检”“模板”，［］占位和模板示例文字全部换掉或删去；不写 PPT 是怎么做出来的（联系页固定声明除外）。
 - **数字**：价格、报价编号、人数与报价单逐项一致，不写折扣；晚数处处与报价单一致（如报价4晚，就不能出现“3晚”）；日期照写，不补年份。
+- **标题**：每页标题用需求单写的结论句，不用栏目名，也不用栏目名换个说法（“费用明细”“包含与不含”“费用里已经包含的，和需要您另外准备的”都不行）。
+- **安全提示**：只放需求单里有、且和本团有关的。没有65岁以上出行人，不写健康申明，也不写“65岁”；不是高原线路（不含玉龙雪山、香格里拉、川西），不写任何“高原”字样，包括“无高原”。
+- **下一步与出行须知页**：完整写出“天气、开放时间等以出团通知书为准。”，一字不减，不能只写“天气以出团通知书为准”。
 - **版面**：每页最多三个要点，每点不超过三十字；每页字号最多三级——给每段文字显式设字号，只用模板字号阶梯里的三级，层次靠留白和细线区分。
+- **费用页**：每一行（每人基础价、加晚、全团总价、意外险、含险合计、报价有效期等）各自独立一个文本框或一行，留足行距；看渲染确认没有任何两行叠在一起。
+- **图标**：只用来标明这一行信息的种类，必须和这一行内容对得上；以“来源”开头的行不配图标，模板行里自带的图标要换成对得上的，或用 drop_shape 删去。
 - **“为什么是这条线”**：只讲客人自己说过的情况和产品亮点，不讲预算档位、人均每晚、选档规则。
 - **照片**：只用需求单或调研中注明来源的实拍照片，没有就删去照片位。
 
 ### 固定顺序
 
 1. 读任务列出的规范文件和需求单；返工时再读调研结果和现有方案对应的文字清单。
-2. 以任务给的模板为底写 build.py，`ppt_build` 导出，`deliver_to` 写工作目录下 `deliverables/海岚旅行-<报价编号>.pptx` 的绝对路径；逐页看渲染并修正。
-3. 运行 `deck_text_audit`：quote_number=报价编号，salutation=需求单称呼原文，nights=报价晚数。它写出 `briefs/方案文字-<报价编号>.md`（逐页文字与字号清单）并列出必须修改的项。
-4. 状态为“不通过”时，逐项改 build.py → `ppt_build` 重新导出并看渲染 → 再运行 `deck_text_audit`，直到“状态：通过”。提醒项也要核对，确有问题一并改。**每次重新导出后都必须重新审计**，清单才与成品一致。
+2. 以任务给的模板为底写 build.py，`ppt_build` 导出，`deliver_to` 写工作目录下 `deliverables/海岚旅行-<报价编号>.pptx` 的绝对路径；逐页看渲染并修正，费用页重点看叠字，来源行看有没有图标。
+3. 运行 `deck_text_audit`，参数：
+   - quote_number=报价编号，salutation=需求单称呼原文，nights=报价晚数；
+   - seniors_65=需求单人数构成里65岁以上的人数（没有就写0）；
+   - plateau=线路是否含玉龙雪山、香格里拉、川西（不含就写 false）；
+   - 返工时再传 brief_path=任务给的需求单路径、research_path=任务给的调研路径。
+
+   它写出 `briefs/方案文字-<报价编号>.md`（逐页标题、文字与字号清单），并列出必须修改的项。除了原有各项，还包括：门票、优免、班次的句子在需求单和调研原文里找不到；缺完整的“天气、开放时间等以出团通知书为准”；与本团无关的健康申明或高原提示；估算出的文字叠压；来源行配了图标；标题是栏目名。
+4. 状态为“不通过”时，逐项改 build.py → `ppt_build` 重新导出并看渲染 → 再运行 `deck_text_audit`，直到“状态：通过”。原文比对不通过时，回到需求单和调研里找到那句原文照抄，找不到就删去。提醒项也要核对，确有问题一并改。**每次重新导出后都必须重新审计**，清单才与成品一致。叠字是估算的，通过了也要逐页看渲染。
 5. 最后的回答写明：成品 .pptx 的绝对路径、页数、文字清单路径；返工时再写明每个不合格项是怎么改的。
```

`memory.prompt` `agent_memory/profile/soul.md`: modified, +1 -1

```diff
--- a/memory.prompt/agent_memory/profile/soul.md
+++ b/memory.prompt/agent_memory/profile/soul.md
@@ -19,4 +19,4 @@
   substituted.
 - **Less, arranged.** A page that says one thing well beats a page that says four
   things at once, and a deck of identical card grids has argued nothing.
-- **海岚旅行的方案只写需求单“对客文案”和调研结果里有的内容。** 称呼、价格、晚数、班次、酒店都照原文，不猜、不补、不美化；没有查证的项目直接不写。
+- **海岚旅行的方案只写需求单“对客文案”和调研结果里有的内容。** 称呼、价格、晚数、班次、酒店、门票优免都照原文，不猜、不补、不美化、不归纳；没有查证的项目直接不写。
```

## 机制起作用的记录

Per drill; the record keeps each turn separately.

| Session | Turns | Kind | Target | Decision | Count | Last summary |
|---|---|---|---|---|---|---|
| family | 1, 2, 3, 4, 5, 6, 7, 8 | action.callback | action.strategy | applied | 48 | system_addendum |
| family | 1, 2, 3, 4, 5, 6, 7, 8 | action.result | action.strategy | assessed | 17 | {"kind":"accept","problems":[],"correction":null,"reply":null} |
| family | 1, 2, 3, 4, 5, 6, 7, 8 | action.callback | action.strategy | accept | 14 |  |
| family | 8 | action.callback | action.strategy | **resample** | 3 | 提到了方案制作或返工的内部进度（“核对无误”），对客人不得提及内部工具、文件、路径、分工、制作方式、进度或故障；制作期间只说“方案 PPT 还在准备，好了马上发给您” |
| family | 8 | hosting.ready | Raven: hosting | ready | 1 | 28fa3ec95bef49cb6470426e9eee6fe986348add44f93ae70a8db487d6e0cf86 |
| family | 8 | action.callback | Raven: action.strategy | applied | 17 | system_addendum |
| family | 8 | action.result | Raven: action.strategy | assessed | 4 | {"kind":"accept","problems":[],"correction":null,"capped":false} |
| family | 8 | action.callback | Raven: action.strategy | **resample** | 2 | 对客文案出现内部字样“未查证”（原句：（返程班次未查证，故不写班次；只写产品框架里的"下午高铁返回…），客人会看到；删去该字样，来源只写调研里列出的真实网站名或链接，未查到的项目整项不写; 称呼必须一字不改用“尊敬的客人”，不得自己加“先生”（原句：（称呼照写""，不加"先生""女士）; 酒店名“官方酒店”同一处要标“参考酒店，以计调确认为准”; “第1天 · 10月20日”一页有5个要点，每页最多三个要点; “第3天 · 10月22日”一页有4个要点，每页最多三个要点 |
| family | 8 | action.callback | Raven: action.strategy | accept | 2 |  |
| family | 8 | component.constructed | Raven-PPT: capability.tools | constructed | 1 | deck_text_audit from deck_audit:create_tool |
| family | 8 | hosting.ready | Raven-PPT: hosting | ready | 1 | 7d88bd65bd2ad14f76fdaad93e806c2379cb64a14823e649fef96fa0802ec673 |
| family | 8 | action.callback | Raven-PPT: action.strategy | applied | 424 | review |
| family | 8 | action.result | Raven-PPT: action.strategy | assessed | 2 | {"kind":"accept","problems":[],"correction":null} |
| family | 8 | hosting.ready | Raven-Research: hosting | ready | 1 | 1242ddf9c0b48bf2eefdef64b51d031f8cc92a921560432a8e04291dea659fc9 |
| family | 8 | loop.control | loop | rollback | 1 | 2 honoured, 0 refused |
