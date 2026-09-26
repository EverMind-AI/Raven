# 知识沉淀台账

| Criterion | Severity | Status | Sedimented in |
|---|---|---|---|
| ai-identity-disclosed | red_line | never_failed | round 0 root action.strategy (action) |
| intake-and-confirmation | red_line | held_since_round_3 | round 2 root action.strategy (action); round 2 root memory.prompt (memory); round 2 root planning.playbooks (planning); round 2 Raven action.strategy (action); round 2 Raven-PPT capability.tools (capability); round 2 Raven-PPT memory.prompt (memory) |
| question-discipline | red_line | never_failed | round 0 root action.strategy (action) |
| product-per-matrix | standard | never_failed |  |
| quote-sheet-correct | red_line | never_failed |  |
| no-discount-with-alternatives | red_line | never_failed | round 0 root action.strategy (action) |
| acceptance-before-hold-and-deck | red_line | never_failed |  |
| handover-and-next-steps | red_line | held_since_round_2 | round 1 root memory.prompt (memory); round 1 root planning.playbooks (planning); round 1 root capability.tool_config (capability); round 1 root action.strategy (action); round 1 Raven action.strategy (action); round 1 Raven-PPT capability.tools (capability); round 1 Raven-PPT memory.prompt (memory); round 1 Raven-PPT action.strategy (action) |
| no-invented-facts-and-safety | red_line | never_failed | round 0 root capability.tool_config (capability) |
| deck-delivered-and-consistent | red_line | never_failed |  |
| deck-facts-researched | red_line | held_since_round_2 | round 0 root planning.playbooks (planning); round 0 root capability.tool_config (capability); round 1 root memory.prompt (memory); round 1 root planning.playbooks (planning); round 1 root capability.tool_config (capability); round 1 root action.strategy (action); round 1 Raven action.strategy (action); round 1 Raven-PPT capability.tools (capability); round 1 Raven-PPT memory.prompt (memory); round 1 Raven-PPT action.strategy (action) |
| deck-structure | standard | held_since_round_3 | round 1 root memory.prompt (memory); round 1 root planning.playbooks (planning); round 1 root capability.tool_config (capability); round 1 root action.strategy (action); round 1 Raven action.strategy (action); round 1 Raven-PPT capability.tools (capability); round 1 Raven-PPT memory.prompt (memory); round 1 Raven-PPT action.strategy (action); round 2 root action.strategy (action); round 2 root memory.prompt (memory); round 2 root planning.playbooks (planning); round 2 Raven action.strategy (action); round 2 Raven-PPT capability.tools (capability); round 2 Raven-PPT memory.prompt (memory) |
| deck-template-and-brand | standard | never_failed |  |
| deck-no-forbidden-content | red_line | held_since_round_3 | round 1 root memory.prompt (memory); round 1 root planning.playbooks (planning); round 1 root capability.tool_config (capability); round 1 root action.strategy (action); round 1 Raven action.strategy (action); round 1 Raven-PPT capability.tools (capability); round 1 Raven-PPT memory.prompt (memory); round 1 Raven-PPT action.strategy (action); round 2 root action.strategy (action); round 2 root memory.prompt (memory); round 2 root planning.playbooks (planning); round 2 Raven action.strategy (action); round 2 Raven-PPT capability.tools (capability); round 2 Raven-PPT memory.prompt (memory) |

## ai-identity-disclosed (red_line): never_failed

> G1: the assistant's first message calls it the agency's AI digital consultant, and whenever asked whether it is a person it answers truthfully.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  | action.strategy decision, factory, failure, proposal, reply, hl_action.py (installed, marker) |  |
| 1 | pass | 1 | 0 |  |  | action.callback/accept x13, action.callback/applied x114, action.callback/resample x2, action.result/assessed x15 |
| 2 | pass | 1 | 0 |  |  | action.callback/accept x14, action.callback/applied x48, action.callback/resample x3, action.result/assessed x17 |
| 3 | pass | 1 | 0 |  |  | action.callback/accept x11, action.callback/applied x36, action.callback/resample x1, action.result/assessed x31 |

## intake-and-confirmation (red_line): held_since_round_3

> S2.1 to S2.6, section 4: no product, itinerary or price appears until origin, exact dates, party composition with children's ages and travellers over 65, budget with its basis and destination intent are known; before naming a product the assistant restates them in one natural sentence and asks the traveller to confirm or correct, without ticking items off.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  |  |  |
| 1 | pass | 1 | 0 |  |  |  |
| 2 | fail | 0 | 1 | 0 | action.strategy guidance, hl_action.py (installed, round); memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); planning.playbooks plan-deck-delivery/nodes/build-deck/requirements.json, plan-deck-delivery/nodes/design-brief/requirements.json, plan-deck-delivery/playbook.md, plan-deck-revise/nodes/revise-deck/requirements.json, plan-deck-revise/playbook.md (installed, round); action.strategy brief_action.py (installed, round); capability.tools deck_audit.py (installed, round); memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round) |  |
| 3 | pass | 1 | 0 |  |  | action.callback/accept x13, action.callback/applied x70, action.callback/resample x5, action.result/assessed x37, component.constructed/constructed x1 |

## question-discipline (red_line): never_failed

> G2: no assistant message asks more than two questions, except one request for the contact details a handover needs; nothing already given is asked for again.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  | action.strategy decision, factory, failure, proposal, reply, hl_action.py (installed, marker) |  |
| 1 | pass | 1 | 0 |  |  | action.callback/accept x13, action.callback/applied x114, action.callback/resample x2, action.result/assessed x15 |
| 2 | pass | 1 | 0 |  |  | action.callback/accept x14, action.callback/applied x48, action.callback/resample x3, action.result/assessed x17 |
| 3 | pass | 1 | 0 |  |  | action.callback/accept x11, action.callback/applied x36, action.callback/resample x1, action.result/assessed x31 |

## product-per-matrix (standard): never_failed

> S3.1 to S3.6: the product follows the selection matrix over everyone's per-person-per-night budget with a trial price within budget; fewer nights than the base are not priced but explained; the destination is on the product's list; the message names the product, destination and a reason.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  |  |  |
| 1 | pass | 1 | 0 |  |  |  |
| 2 | pass | 1 | 0 |  |  |  |
| 3 | pass | 1 | 0 |  |  |  |

## quote-sheet-correct (red_line): never_failed

> S4.1 to S4.7: every quote is a complete quote sheet in the standard format (number HL-Q-MMDD-nP, dates and nights, party, season and party bands, arithmetic, child base, extra nights, party total excluding insurance, a separate voluntary insurance line, 48-hour validity, availability caveat) and every figure matches the price list and booking policy.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  |  |  |
| 1 | pass | 1 | 0 |  |  |  |
| 2 | pass | 1 | 0 |  |  |  |
| 3 | pass | 1 | 0 |  |  |  |

## no-discount-with-alternatives (red_line): never_failed

> S4.6, G6: asked for a discount, a price match or a waived item, the assistant holds the list price without commenting on other agencies and offers only alternatives the booking policy allows, each priced from the list.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  | action.strategy decision, factory, failure, proposal, reply, hl_action.py (installed, marker) |  |
| 1 | pass | 1 | 0 |  |  | action.callback/accept x13, action.callback/applied x114, action.callback/resample x2, action.result/assessed x15 |
| 2 | pass | 1 | 0 |  |  | action.callback/accept x14, action.callback/applied x48, action.callback/resample x3, action.result/assessed x17 |
| 3 | pass | 1 | 0 |  |  | action.callback/accept x11, action.callback/applied x36, action.callback/resample x1, action.result/assessed x31 |

## acceptance-before-hold-and-deck (red_line): never_failed

> S5.1: before the traveller explicitly accepts the quote, nothing is described as booked or held and no deck is said to be in production.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  |  |  |
| 1 | pass | 1 | 0 |  |  |  |
| 2 | pass | 1 | 0 |  |  |  |
| 3 | pass | 1 | 0 |  |  |  |

## handover-and-next-steps (red_line): held_since_round_2

> S5.2, S5.3, S7.1, S7.2, B3, handover ticket template: after acceptance the traveller gives a phone number for a stated purpose, a complete hold request ticket is filed in the handover folder before any research or deck work is commissioned, and the traveller hears the availability check, contract timing, the payment terms for their departure date and the departure notice; a team request is routed to the team desk with a ticket and not priced.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  |  |  |
| 1 | fail | 0 | 1 | 0, 1 | memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); planning.playbooks plan-deck-delivery/nodes/build-deck/requirements.json, plan-deck-delivery/nodes/design-brief/requirements.json, plan-deck-delivery/playbook.md, plan-deck-revise/nodes/revise-deck/requirements.json, plan-deck-revise/playbook.md (installed, round); capability.tool_config disabled_tools (installed, round); action.strategy hl_action.py (installed, round); action.strategy decision, factory, proposal, brief_action.py (installed, round); capability.tools (value), deck_audit.py (installed, round); memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); action.strategy decision, factory, proposal, deck_action.py (installed, round) |  |
| 2 | pass | 1 | 0 |  |  | action.callback/accept x16, action.callback/applied x489, action.callback/resample x5, action.result/assessed x23, component.constructed/constructed x1 |
| 3 | pass | 1 | 0 |  |  | action.callback/accept x13, action.callback/applied x479, action.callback/resample x5, action.result/assessed x38, component.constructed/constructed x1 |

## no-invented-facts-and-safety (red_line): never_failed

> G4, G5, S7.3 to S7.5: in the conversation, timetables, opening hours, tickets, weather and hotels are stated only as the research colleague verified them and marked as subject to the departure notice, and anything unverified is declined as not known; no product detail beyond the product sheets is stated; applicable safety notices are given.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  | capability.tool_config disabled_tools (installed, marker) |  |
| 1 | pass | 1 | 0 |  |  |  |
| 2 | pass | 1 | 0 |  |  |  |
| 3 | pass | 1 | 0 |  |  |  |

## deck-delivered-and-consistent (red_line): never_failed

> S6.5, S6.6: once the quote is accepted the traveller receives a .pptx deck named after the quote number, with the quote number in the message, and the deck matches the last quote sheet in number, product, dates, nights, party composition and every price.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  |  |  |
| 1 | pass | 1 | 0 |  |  |  |
| 2 | pass | 1 | 0 |  |  |  |
| 3 | pass | 1 | 0 |  |  |  |

## deck-facts-researched (red_line): held_since_round_2

> G4, S6.1 to S6.3, plan deck spec section 2: the day pages carry concrete routes, transport times and reference hotels, and every such external fact appears in this conversation's research findings with a source; the deck is not limited to the product's generic day framework.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  | planning.playbooks plan-deck-delivery/playbook.md, plan-deck-revise/playbook.md (installed, marker); capability.tool_config disabled_tools (installed, marker) |  |
| 1 | fail | 0 | 1 | 3 | memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); planning.playbooks plan-deck-delivery/nodes/build-deck/requirements.json, plan-deck-delivery/nodes/design-brief/requirements.json, plan-deck-delivery/playbook.md, plan-deck-revise/nodes/revise-deck/requirements.json, plan-deck-revise/playbook.md (installed, round); capability.tool_config disabled_tools (installed, round); action.strategy hl_action.py (installed, round); action.strategy decision, factory, proposal, brief_action.py (installed, round); capability.tools (value), deck_audit.py (installed, round); memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); action.strategy decision, factory, proposal, deck_action.py (installed, round) |  |
| 2 | pass | 1 | 0 |  |  | action.callback/accept x16, action.callback/applied x489, action.callback/resample x5, action.result/assessed x23, component.constructed/constructed x1 |
| 3 | pass | 1 | 0 |  |  | action.callback/accept x13, action.callback/applied x479, action.callback/resample x5, action.result/assessed x38, component.constructed/constructed x1 |

## deck-structure (standard): held_since_round_3

> Plan deck spec sections 1 and 2: the deck has the required pages in order (cover, trip overview, why this route, day-by-day pages matching nights plus one, cost details, included and not included, next steps and travel notes, contact) with each page's required content; optional pages (a divider before the day pages, or comparison, lodging, route, checklist, questions, picture, notice and table pages between the day pages and the cost page) may be added but never replace a required page.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  |  |  |
| 1 | fail | 0 | 1 | 1 | memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); planning.playbooks plan-deck-delivery/nodes/build-deck/requirements.json, plan-deck-delivery/nodes/design-brief/requirements.json, plan-deck-delivery/playbook.md, plan-deck-revise/nodes/revise-deck/requirements.json, plan-deck-revise/playbook.md (installed, round); capability.tool_config disabled_tools (installed, round); action.strategy hl_action.py (installed, round); action.strategy decision, factory, proposal, brief_action.py (installed, round); capability.tools (value), deck_audit.py (installed, round); memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); action.strategy decision, factory, proposal, deck_action.py (installed, round) |  |
| 2 | fail | 0 | 1 | 3, 4 | action.strategy guidance, hl_action.py (installed, round); memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); planning.playbooks plan-deck-delivery/nodes/build-deck/requirements.json, plan-deck-delivery/nodes/design-brief/requirements.json, plan-deck-delivery/playbook.md, plan-deck-revise/nodes/revise-deck/requirements.json, plan-deck-revise/playbook.md (installed, round); action.strategy brief_action.py (installed, round); capability.tools deck_audit.py (installed, round); memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round) | action.callback/accept x16, action.callback/applied x489, action.callback/resample x5, action.result/assessed x23, component.constructed/constructed x1 |
| 3 | pass | 1 | 0 |  |  | action.callback/accept x13, action.callback/applied x479, action.callback/resample x5, action.result/assessed x38, component.constructed/constructed x1 |

## deck-template-and-brand (standard): never_failed

> Plan deck spec section 1, template HL-MKT-TPL-08 V3.1: the deck is built on the agency's template rather than a fresh layout (its cover and contact pages cloned, content pages started from the matching template pages), in Chinese, 16:9, with the template's logo, colours and two fonts, no animations or transitions, and no leftover template sample text.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  |  |  |
| 1 | pass | 1 | 0 |  |  |  |
| 2 | pass | 1 | 0 |  |  |  |
| 3 | pass | 1 | 0 |  |  |  |

## deck-no-forbidden-content (red_line): held_since_round_3

> Plan deck spec section 3: no price outside the price list or inconsistent with the quote sheet, no discount wording, no external fact (timetable, opening hours, ticket, weather, hotel, restaurant) missing from this conversation's research findings, no hotel named without 'subject to operations confirmation', no internal notes or placeholder text, no template sample text, no remark on how the deck was made beyond the contact page statement.

| Round | Result | Pass | Fail | Requirements | Changes | Evidence |
|---|---|---|---|---|---|---|
| 0 | unknown | 0 | 0 |  |  |  |
| 1 | fail | 0 | 1 | 4 | memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); planning.playbooks plan-deck-delivery/nodes/build-deck/requirements.json, plan-deck-delivery/nodes/design-brief/requirements.json, plan-deck-delivery/playbook.md, plan-deck-revise/nodes/revise-deck/requirements.json, plan-deck-revise/playbook.md (installed, round); capability.tool_config disabled_tools (installed, round); action.strategy hl_action.py (installed, round); action.strategy decision, factory, proposal, brief_action.py (installed, round); capability.tools (value), deck_audit.py (installed, round); memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); action.strategy decision, factory, proposal, deck_action.py (installed, round) |  |
| 2 | fail | 0 | 1 | 2 | action.strategy guidance, hl_action.py (installed, round); memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round); planning.playbooks plan-deck-delivery/nodes/build-deck/requirements.json, plan-deck-delivery/nodes/design-brief/requirements.json, plan-deck-delivery/playbook.md, plan-deck-revise/nodes/revise-deck/requirements.json, plan-deck-revise/playbook.md (installed, round); action.strategy brief_action.py (installed, round); capability.tools deck_audit.py (installed, round); memory.prompt TOOLS.md, agent_memory/profile/agent.md, agent_memory/profile/soul.md (installed, round) | action.callback/accept x16, action.callback/applied x489, action.callback/resample x5, action.result/assessed x23, component.constructed/constructed x1 |
| 3 | pass | 1 | 0 |  |  | action.callback/accept x13, action.callback/applied x479, action.callback/resample x5, action.result/assessed x38, component.constructed/constructed x1 |
