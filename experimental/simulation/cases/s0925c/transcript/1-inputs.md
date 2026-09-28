# 输入

## 旅行社情况（员工的任务说明）

> 海岚旅行是杭州的一家国内精品短途游旅行社。你是店里的AI数字顾问，通过线上渠道接待客人的咨询。今天是2026年9月24日，星期四。
>
> 店里的接待按《线上咨询与预订服务标准作业程序》执行：先分流，再采集并确认需求，按产品选择矩阵在三档产品中匹配并试算，按价目表出报价单，客人接受后提交转交单，先请调研同事查证目的地的真实信息，再委托设计同事按店里模板制作行程方案 PPT，质检通过后以文件交给客人。投诉、超范围业务和团队业务各有处理办法。报价只依据店里的价目表和预订规则，店外信息只用调研同事查证过的，没有查证的直说不掌握，绝不编造。
>
> 正式方案以行程方案 PPT 为准，聊天消息简洁即可。

## 入职对话

> 你好，我是海岚旅行的老板。店里新来的 AI 数字顾问交给你带。它的岗位情况见任务说明，店里做生意的资料我传到了店里工作目录下的 uploads 文件夹（下面是相对工作目录的路径）：
>
> - uploads/booking-policy/booking-policy.md
> - uploads/brand-design-guide/brand-design-guide.md
> - uploads/brand-design-guide/brand-kit.pptx
> - uploads/comfort-package/comfort-package.md
> - uploads/consultation-scripts/consultation-scripts.md
> - uploads/handover-ticket/handover-ticket.md
> - uploads/plan-deck-sample/plan-deck-sample.md
> - uploads/plan-deck-sample/sample.pptx
> - uploads/plan-deck-spec/plan-deck-spec.md
> - uploads/plan-deck-template/plan-deck-template.md
> - uploads/plan-deck-template/template.pptx
> - uploads/premium-package/premium-package.md
> - uploads/price-list/price-list.md
> - uploads/service-sop/service-sop.md
> - uploads/value-package/value-package.md
>
> 这些是店里做生意的规矩和底稿，以后它接待每一位客人都要照这个来。之后我会按客人的情形考它，哪里不对再跟你说。

## 资料

| Material | Given | Round | SHA-256 | Copy |
|---|---|---|---|---|
| booking-policy | opening |  | de4faa61b54fe893b81464fae56358797ecd42d15c6503f2d435ac8072496204 | assets/scenario/materials/booking-policy/SKILL.md |
| brand-design-guide | opening |  | 4b052fdf447a6f667c62f0aa92329df00a87b0a3a9d76caea4b3e6f254093cf0 | assets/scenario/materials/brand-design-guide/SKILL.md |
| comfort-package | opening |  | 9746ddaa303a0e3dbdea57dcff0a214ca626744cc8348e0cbc6127d36969ee3a | assets/scenario/materials/comfort-package/SKILL.md |
| consultation-scripts | opening |  | 951bc8d999f1cb8a8cadeac6d4035f277ae6d65336b1b7ef28df153734314772 | assets/scenario/materials/consultation-scripts/SKILL.md |
| handover-ticket | opening |  | 639fb268cda66884779a6771029b422496f1e21e11b7a6af445d94aa5820b815 | assets/scenario/materials/handover-ticket/SKILL.md |
| plan-deck-sample | opening |  | 2b725be9a0e0358b9e1289ec7721aab49109c9ee09b87be69718efdabe56de61 | assets/scenario/materials/plan-deck-sample/SKILL.md |
| plan-deck-spec | opening |  | f12b4525a726c6cba6b74c6d53ad29c7eb045635eead35742b5527c6341511f2 | assets/scenario/materials/plan-deck-spec/SKILL.md |
| plan-deck-template | opening |  | 42d090dd19338bf90f5fa1462a411d7c63d946e4bba8db9718555778b41995b0 | assets/scenario/materials/plan-deck-template/SKILL.md |
| premium-package | opening |  | ba7fd46f7ac9d1742a1bf9aa29c1704cd4566c9341d1cb16b802c2c061d612ce | assets/scenario/materials/premium-package/SKILL.md |
| price-list | opening |  | d40cb2bfdc67d8290058d5be7da7a1f10f1120847f67f62cbbc897e27acb58da | assets/scenario/materials/price-list/SKILL.md |
| service-sop | opening |  | f1cc947ba66c73640a0964e6c2beb1042efb27bc0a3a9027ab68bf1dd4260d51 | assets/scenario/materials/service-sop/SKILL.md |
| value-package | opening |  | c8f00b534706ed980c87ce11c990d5a2fa5b409b0fb680ac088ca448b3d898d8 | assets/scenario/materials/value-package/SKILL.md |

## 试炼题卡

| Card | Played | SHA-256 |
|---|---|---|
| family | yes | cb59e5c48db31226baced0c8a6397d243e6d0d98ada757b1482b3626e9b71567 |
| premium | no | 76aa0f58f2697cfabee86eeb10664d0594c3437d33403f1749043ac98493d616 |
| professional | no | 36995c629be741daf0b35053217f1ca8ceb5420a6d2895de3bbf78911436cd98 |
| returning | no | 9678f0762ef471ad068f8c3591f61a8fe7986fa237a01c6cfaba64107c2e8796 |
| student | no | 62e7b01277f963e69ab93e9b0a76beafbe19f483ed842c7b572a28ef2b9dbd7d |

## 员工的初始 home

13 files fingerprinted at the start of the run.

| Path | SHA-256 |
|---|---|
| playbooks/plan-deck-delivery/playbook.md | 161387d6587123971e6cea53a98dd6d8cb59e82bbd2216becb6b742a67fcef09 |
| subagents/Raven-PPT/HEARTBEAT.md | fc159df7a2921240c526ae458d0b488cdc81acc35e9de739dc8cacd014c48ac6 |
| subagents/Raven-PPT/TOOLS.md | fe51bc017ecc69ac6109eb3601fba9fece80c6b7632868438958c23ce25689ce |
| subagents/Raven-PPT/agent_memory/procedural/case.md | e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 |
| subagents/Raven-PPT/agent_memory/procedural/skills.md | e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 |
| subagents/Raven-PPT/agent_memory/profile/agent.md | 57d56e5374f5c105abf270e5e9d4f15f314bd8cdcd15ca818f3b263501447eba |
| subagents/Raven-PPT/agent_memory/profile/soul.md | 8dcc0ed76a30fe3f6aab91d47c8bc8994b4152d5902a879d231f03a25c605e9a |
| subagents/Raven-PPT/user_memory/attention.md | e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 |
| subagents/Raven-PPT/user_memory/behaviors.md | e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 |
| subagents/Raven-PPT/user_memory/episodic/episodes.md | e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 |
| subagents/Raven-PPT/user_memory/profile/user.md | e311669a708b0af153dc41b7e7a955e2b9ccac0ee20f63fee2d92fddc8326003 |
| subagents/Raven-Research/agent_memory/profile/agent.md | d840743c3f749e13e101652600cd9a0710826938c95c77cc34a9bab9baa1c54c |
| subagents/Raven-Research/agent_memory/profile/soul.md | ace1fd70510fa2cbc4a471ddaf79cba54b977c89267a5fca6d32a5d8f8c2ea8d |

## 运行设置

```json
{
  "curator": "improve",
  "chain": "documents",
  "scenario": "travel_agency",
  "deliver": "dialog",
  "disclose": "all",
  "teach": "documents",
  "targets": "all",
  "judge": "round",
  "rounds": 3,
  "turns": 14,
  "repeats": 1,
  "cards": [
    "family"
  ],
  "concurrent_drills": 1,
  "seed": 11,
  "without": null,
  "models": {
    "employee": "deepseek/deepseek-flash",
    "curator": "openrouter/anthropic/claude-opus-5.5",
    "analyst": "openrouter/anthropic/claude-opus-5.5",
    "simulation": "openrouter/anthropic/claude-opus-5.5",
    "traveller": "deepseek/deepseek-flash",
    "subagents": "deepseek/deepseek-flash"
  },
  "efforts": {
    "employee": "low",
    "employee_tier": "medium",
    "curator": "high",
    "traveller": "low"
  },
  "starting_harness": {
    "raven_commit": "<redacted>",
    "raven_modified": false,
    "raven_diff": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "products": {
      "raven-code": "4b79902aa99362c6ec8ff989dbf4b8d559ae85cbc2c30811a62ff3dbb04eaf11",
      "raven-design": "440b5f4466251f18e4e2c4aaaf7338d59a48898d10f19daec479edff8d489e79",
      "raven-oncall": "008adbbcb5ca14bc017464557da75e8f018b5cd8b6b9ca159d7b266b5ba627da",
      "raven-ppt": "0a1f972ade7565750a9c9675b04e31f2075845d483dd99532ed8ea0800349a8f",
      "raven-research": "318cb314cdcf14bbeb4bac65eac02b96de8b38c7f607947e2e8a2a76f4efceaa"
    }
  },
  "started": 1790366509.0493903
}
```
