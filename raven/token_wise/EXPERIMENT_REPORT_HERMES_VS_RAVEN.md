# Hermes ``system_and_3`` vs Raven ``CacheOptimizer`` — Head-to-Head

_Both strategies faithfully reproduced and benchmarked through the real ``AgentLoop.process_direct`` code path._

_Generated: 2026-04-16T05:21:47.013073+00:00 UTC_

Model: `anthropic/claude-sonnet-4-5` (via OpenRouter, pinned to Anthropic)

## Strategy definitions

| | Breakpoint 1 | Breakpoint 2 | Breakpoint 3 | Breakpoint 4 |
|:---|:---|:---|:---|:---|
| **V1 baseline** | — | — | — | — |
| **V2 Raven v2** | tools[-1] (if tools) | system tail | rolling msg[-2] | rolling msg[-1] |
| **V3 Hermes** | system[0] | non_sys[-3] | non_sys[-2] | non_sys[-1] |


---

## S1: Pure conversation (8 turns, no tools)

Medium SOUL.md (~600 tok). 8 single-token Q&A turns. No tools registered. Tests cross-turn history caching.

### Aggregate

| Variant | Strategy | LLM calls | Fresh | Cache W | Cache R | Completion | Cost | vs baseline | cache hit % |
|:--------|:---------|----------:|------:|--------:|--------:|-----------:|-----:|------------:|------------:|
| V1_baseline | none | 8 | 32,129 | 0 | 0 | 37 | $0.096942 | +0.0% | 0.0% |
| V2_raven | tools+sys+rolling_tail | 8 | 24 | 4,529 | 27,656 | 37 | $0.025908 | -73.3% | 85.9% |
| V3_hermes | sys+last_3 | 8 | 24 | 4,521 | 27,624 | 37 | $0.025868 | -73.3% | 85.9% |

### Per-call detail

#### V1_baseline (none, sys=12,990ch, tools=0)

| # | Fresh | Cache R | Cache W | Compl | Cost |
|--:|------:|--------:|--------:|------:|-----:|
| 1 | 3,955 | 0 | 0 | 4 | $0.011925 |
| 2 | 3,971 | 0 | 0 | 4 | $0.011973 |
| 3 | 3,988 | 0 | 0 | 5 | $0.012039 |
| 4 | 4,007 | 0 | 0 | 6 | $0.012111 |
| 5 | 4,026 | 0 | 0 | 5 | $0.012153 |
| 6 | 4,044 | 0 | 0 | 5 | $0.012207 |
| 7 | 4,061 | 0 | 0 | 4 | $0.012243 |
| 8 | 4,077 | 0 | 0 | 4 | $0.012291 |

#### V2_raven (tools+sys+rolling_tail, sys=12,990ch, tools=0)

| # | Fresh | Cache R | Cache W | Compl | Cost |
|--:|------:|--------:|--------:|------:|-----:|
| 1 | 3 | 0 | 3,962 | 4 | $0.014927 |
| 2 | 3 | 3,902 | 76 | 4 | $0.001525 |
| 3 | 3 | 3,918 | 77 | 5 | $0.001548 |
| 4 | 3 | 3,934 | 80 | 6 | $0.001579 |
| 5 | 3 | 3,952 | 81 | 5 | $0.001573 |
| 6 | 3 | 3,952 | 99 | 5 | $0.001641 |
| 7 | 3 | 3,990 | 78 | 4 | $0.001559 |
| 8 | 3 | 4,008 | 76 | 4 | $0.001556 |

#### V3_hermes (sys+last_3, sys=12,982ch, tools=0)

| # | Fresh | Cache R | Cache W | Compl | Cost |
|--:|------:|--------:|--------:|------:|-----:|
| 1 | 3 | 0 | 3,957 | 4 | $0.014908 |
| 2 | 3 | 3,898 | 75 | 4 | $0.001520 |
| 3 | 3 | 3,914 | 76 | 5 | $0.001543 |
| 4 | 3 | 3,930 | 79 | 6 | $0.001574 |
| 5 | 3 | 3,948 | 80 | 5 | $0.001568 |
| 6 | 3 | 3,962 | 84 | 5 | $0.001588 |
| 7 | 3 | 3,968 | 95 | 4 | $0.001616 |
| 8 | 3 | 4,004 | 75 | 4 | $0.001551 |

### Conclusion

- **V2_raven** (tools+sys+rolling_tail): **73.3%** savings vs baseline
- **V3_hermes** (sys+last_3): **73.3%** savings vs baseline
- **Winner: V3_hermes** (Raven vs Hermes: +0.2%)


---

## S2: Intra-turn tool chain (3 tools × 3 turns)

Small SOUL.md (~300 tok). 3 registered tools (alpha/beta/gamma). System prompt instructs: call exactly one tool per LLM response, in order. Each turn = up to 4 LLM calls. Tests intra-turn prefix caching.

### Aggregate

| Variant | Strategy | LLM calls | Fresh | Cache W | Cache R | Completion | Cost | vs baseline | cache hit % |
|:--------|:---------|----------:|------:|--------:|--------:|-----------:|-----:|------------:|------------:|
| V1_baseline | none | 12 | 123,867 | 0 | 0 | 732 | $0.382581 | +0.0% | 0.0% |
| V2_raven | tools+sys+rolling_tail | 12 | 63 | 26,356 | 97,508 | 717 | $0.139031 | -63.7% | 78.7% |
| V3_hermes | sys+last_3 | 12 | 63 | 26,358 | 97,545 | 735 | $0.139320 | -63.6% | 78.7% |

### Per-call detail

#### V1_baseline (none, sys=13,000ch, tools=3)

| # | Fresh | Cache R | Cache W | Compl | Cost |
|--:|------:|--------:|--------:|------:|-----:|
| 1 | 4,670 | 0 | 0 | 79 | $0.015195 |
| 2 | 5,925 | 0 | 0 | 66 | $0.018765 |
| 3 | 7,166 | 0 | 0 | 66 | $0.022488 |
| 4 | 8,408 | 0 | 0 | 33 | $0.025719 |
| 5 | 8,450 | 0 | 0 | 79 | $0.026535 |
| 6 | 9,705 | 0 | 0 | 66 | $0.030105 |
| 7 | 10,946 | 0 | 0 | 66 | $0.033828 |
| 8 | 12,188 | 0 | 0 | 33 | $0.037059 |
| 9 | 12,230 | 0 | 0 | 79 | $0.037875 |
| 10 | 13,485 | 0 | 0 | 66 | $0.041445 |
| 11 | 14,726 | 0 | 0 | 66 | $0.045168 |
| 12 | 15,968 | 0 | 0 | 33 | $0.048399 |

#### V2_raven (tools+sys+rolling_tail, sys=13,000ch, tools=3)

| # | Fresh | Cache R | Cache W | Compl | Cost |
|--:|------:|--------:|--------:|------:|-----:|
| 1 | 3 | 0 | 4,677 | 79 | $0.018733 |
| 2 | 6 | 4,677 | 1,252 | 66 | $0.007106 |
| 3 | 6 | 4,677 | 2,493 | 66 | $0.011760 |
| 4 | 6 | 7,170 | 1,242 | 28 | $0.007247 |
| 5 | 3 | 4,303 | 4,149 | 79 | $0.018044 |
| 6 | 6 | 8,452 | 1,252 | 66 | $0.008239 |
| 7 | 6 | 9,704 | 1,241 | 66 | $0.008573 |
| 8 | 6 | 10,945 | 1,242 | 28 | $0.008379 |
| 9 | 3 | 8,395 | 3,832 | 79 | $0.018083 |
| 10 | 6 | 12,227 | 1,252 | 66 | $0.009371 |
| 11 | 6 | 13,479 | 1,241 | 66 | $0.009705 |
| 12 | 6 | 13,479 | 2,483 | 28 | $0.013793 |

#### V3_hermes (sys+last_3, sys=12,992ch, tools=3)

| # | Fresh | Cache R | Cache W | Compl | Cost |
|--:|------:|--------:|--------:|------:|-----:|
| 1 | 3 | 0 | 4,672 | 79 | $0.018714 |
| 2 | 6 | 4,672 | 1,252 | 69 | $0.007150 |
| 3 | 6 | 4,696 | 2,472 | 69 | $0.011732 |
| 4 | 6 | 5,924 | 2,489 | 28 | $0.011549 |
| 5 | 3 | 4,299 | 4,154 | 79 | $0.018061 |
| 6 | 6 | 8,453 | 1,252 | 69 | $0.008284 |
| 7 | 6 | 9,705 | 1,244 | 69 | $0.008630 |
| 8 | 6 | 10,949 | 1,245 | 28 | $0.008391 |
| 9 | 3 | 8,397 | 3,837 | 79 | $0.018102 |
| 10 | 6 | 12,234 | 1,252 | 69 | $0.009418 |
| 11 | 6 | 13,486 | 1,244 | 69 | $0.009764 |
| 12 | 6 | 14,730 | 1,245 | 28 | $0.009526 |

### Conclusion

- **V2_raven** (tools+sys+rolling_tail): **63.7%** savings vs baseline
- **V3_hermes** (sys+last_3): **63.6%** savings vs baseline
- **Winner: V2_raven** (Raven vs Hermes: -0.2%)


---

## S3: Mixed — one tool per turn (6 turns)

Small SOUL.md (~300 tok). 1 tool (data_lookup). Each turn = 2 LLM calls (decide + respond). Tests the common real-world agent workload.

### Aggregate

| Variant | Strategy | LLM calls | Fresh | Cache W | Cache R | Completion | Cost | vs baseline | cache hit % |
|:--------|:---------|----------:|------:|--------:|--------:|-----------:|-----:|------------:|------------:|
| V1_baseline | none | 12 | 99,126 | 0 | 0 | 432 | $0.303858 | +0.0% | 0.0% |
| V2_raven | tools+sys+rolling_tail | 12 | 54 | 20,296 | 78,962 | 444 | $0.106621 | -64.9% | 79.5% |
| V3_hermes | sys+last_3 | 12 | 54 | 19,027 | 80,171 | 444 | $0.102225 | -66.4% | 80.8% |

### Per-call detail

#### V1_baseline (none, sys=12,826ch, tools=1)

| # | Fresh | Cache R | Cache W | Compl | Cost |
|--:|------:|--------:|--------:|------:|-----:|
| 1 | 4,480 | 0 | 0 | 67 | $0.014445 |
| 2 | 5,721 | 0 | 0 | 5 | $0.017238 |
| 3 | 5,744 | 0 | 0 | 67 | $0.018237 |
| 4 | 6,985 | 0 | 0 | 5 | $0.021030 |
| 5 | 7,008 | 0 | 0 | 67 | $0.022029 |
| 6 | 8,249 | 0 | 0 | 5 | $0.024822 |
| 7 | 8,272 | 0 | 0 | 67 | $0.025821 |
| 8 | 9,513 | 0 | 0 | 5 | $0.028614 |
| 9 | 9,536 | 0 | 0 | 67 | $0.029613 |
| 10 | 10,777 | 0 | 0 | 5 | $0.032406 |
| 11 | 10,800 | 0 | 0 | 67 | $0.033405 |
| 12 | 12,041 | 0 | 0 | 5 | $0.036198 |

#### V2_raven (tools+sys+rolling_tail, sys=12,826ch, tools=1)

| # | Fresh | Cache R | Cache W | Compl | Cost |
|--:|------:|--------:|--------:|------:|-----:|
| 1 | 3 | 0 | 4,487 | 68 | $0.017855 |
| 2 | 6 | 4,487 | 1,239 | 6 | $0.006100 |
| 3 | 3 | 4,104 | 1,649 | 68 | $0.008444 |
| 4 | 6 | 5,753 | 1,239 | 6 | $0.006480 |
| 5 | 3 | 5,687 | 1,332 | 68 | $0.007730 |
| 6 | 6 | 7,019 | 1,239 | 6 | $0.006860 |
| 7 | 3 | 6,953 | 1,332 | 68 | $0.008110 |
| 8 | 6 | 6,953 | 2,571 | 6 | $0.011835 |
| 9 | 3 | 8,219 | 1,332 | 68 | $0.008490 |
| 10 | 6 | 9,485 | 1,305 | 6 | $0.007847 |
| 11 | 3 | 9,485 | 1,332 | 68 | $0.008870 |
| 12 | 6 | 10,817 | 1,239 | 6 | $0.007999 |

#### V3_hermes (sys+last_3, sys=12,818ch, tools=1)

| # | Fresh | Cache R | Cache W | Compl | Cost |
|--:|------:|--------:|--------:|------:|-----:|
| 1 | 3 | 0 | 4,482 | 68 | $0.017836 |
| 2 | 6 | 4,482 | 1,239 | 6 | $0.006099 |
| 3 | 3 | 4,100 | 1,648 | 68 | $0.008439 |
| 4 | 6 | 5,748 | 1,239 | 6 | $0.006479 |
| 5 | 3 | 5,683 | 1,331 | 68 | $0.007725 |
| 6 | 6 | 7,014 | 1,239 | 6 | $0.006858 |
| 7 | 3 | 6,949 | 1,331 | 68 | $0.008105 |
| 8 | 6 | 8,206 | 1,313 | 6 | $0.007494 |
| 9 | 3 | 8,215 | 1,331 | 68 | $0.008485 |
| 10 | 6 | 9,546 | 1,239 | 6 | $0.007618 |
| 11 | 3 | 9,481 | 1,331 | 68 | $0.008865 |
| 12 | 6 | 10,747 | 1,304 | 6 | $0.008222 |

### Conclusion

- **V2_raven** (tools+sys+rolling_tail): **64.9%** savings vs baseline
- **V3_hermes** (sys+last_3): **66.4%** savings vs baseline
- **Winner: V3_hermes** (Raven vs Hermes: +4.1%)


---

## Overall verdict

| Scenario | Winner | Margin |
|:---------|:-------|-------:|
| S1: Pure conversation (8 turns, no tools) | Tie | 0.2% |
| S2: Intra-turn tool chain (3 tools × 3 turns) | Tie | 0.2% |
| S3: Mixed — one tool per turn (6 turns) | Hermes | 4.1% |

---

## Raw JSON

```json
{
  "S1: Pure conversation (8 turns, no tools)": {
    "description": "Medium SOUL.md (~600 tok). 8 single-token Q&A turns. No tools registered. Tests cross-turn history caching.",
    "variants": {
      "V1_baseline": {
        "strategy": "none",
        "sys_chars": 12990,
        "tools_count": 0,
        "n_calls": 8,
        "totals": {
          "fresh": 32129,
          "cache_w": 0,
          "cache_r": 0,
          "completion": 37,
          "cost": 0.09694199999999999
        },
        "calls": [
          {
            "fresh": 3955,
            "cr": 0,
            "cw": 0,
            "comp": 4,
            "cost": 0.011925
          },
          {
            "fresh": 3971,
            "cr": 0,
            "cw": 0,
            "comp": 4,
            "cost": 0.011973
          },
          {
            "fresh": 3988,
            "cr": 0,
            "cw": 0,
            "comp": 5,
            "cost": 0.012039000000000001
          },
          {
            "fresh": 4007,
            "cr": 0,
            "cw": 0,
            "comp": 6,
            "cost": 0.012111
          },
          {
            "fresh": 4026,
            "cr": 0,
            "cw": 0,
            "comp": 5,
            "cost": 0.012153
          },
          {
            "fresh": 4044,
            "cr": 0,
            "cw": 0,
            "comp": 5,
            "cost": 0.012207
          },
          {
            "fresh": 4061,
            "cr": 0,
            "cw": 0,
            "comp": 4,
            "cost": 0.012243
          },
          {
            "fresh": 4077,
            "cr": 0,
            "cw": 0,
            "comp": 4,
            "cost": 0.012291
          }
        ],
        "error": null
      },
      "V2_raven": {
        "strategy": "tools+sys+rolling_tail",
        "sys_chars": 12990,
        "tools_count": 0,
        "n_calls": 8,
        "totals": {
          "fresh": 24,
          "cache_w": 4529,
          "cache_r": 27656,
          "completion": 37,
          "cost": 0.02590755
        },
        "calls": [
          {
            "fresh": 3,
            "cr": 0,
            "cw": 3962,
            "comp": 4,
            "cost": 0.0149265
          },
          {
            "fresh": 3,
            "cr": 3902,
            "cw": 76,
            "comp": 4,
            "cost": 0.0015246
          },
          {
            "fresh": 3,
            "cr": 3918,
            "cw": 77,
            "comp": 5,
            "cost": 0.00154815
          },
          {
            "fresh": 3,
            "cr": 3934,
            "cw": 80,
            "comp": 6,
            "cost": 0.0015792000000000002
          },
          {
            "fresh": 3,
            "cr": 3952,
            "cw": 81,
            "comp": 5,
            "cost": 0.0015733500000000003
          },
          {
            "fresh": 3,
            "cr": 3952,
            "cw": 99,
            "comp": 5,
            "cost": 0.0016408500000000003
          },
          {
            "fresh": 3,
            "cr": 3990,
            "cw": 78,
            "comp": 4,
            "cost": 0.0015585000000000002
          },
          {
            "fresh": 3,
            "cr": 4008,
            "cw": 76,
            "comp": 4,
            "cost": 0.0015564000000000003
          }
        ],
        "error": null
      },
      "V3_hermes": {
        "strategy": "sys+last_3",
        "sys_chars": 12982,
        "tools_count": 0,
        "n_calls": 8,
        "totals": {
          "fresh": 24,
          "cache_w": 4521,
          "cache_r": 27624,
          "completion": 37,
          "cost": 0.02586795
        },
        "calls": [
          {
            "fresh": 3,
            "cr": 0,
            "cw": 3957,
            "comp": 4,
            "cost": 0.014907749999999999
          },
          {
            "fresh": 3,
            "cr": 3898,
            "cw": 75,
            "comp": 4,
            "cost": 0.00151965
          },
          {
            "fresh": 3,
            "cr": 3914,
            "cw": 76,
            "comp": 5,
            "cost": 0.0015432000000000002
          },
          {
            "fresh": 3,
            "cr": 3930,
            "cw": 79,
            "comp": 6,
            "cost": 0.0015742500000000001
          },
          {
            "fresh": 3,
            "cr": 3948,
            "cw": 80,
            "comp": 5,
            "cost": 0.0015684000000000002
          },
          {
            "fresh": 3,
            "cr": 3962,
            "cw": 84,
            "comp": 5,
            "cost": 0.0015876000000000002
          },
          {
            "fresh": 3,
            "cr": 3968,
            "cw": 95,
            "comp": 4,
            "cost": 0.0016156500000000002
          },
          {
            "fresh": 3,
            "cr": 4004,
            "cw": 75,
            "comp": 4,
            "cost": 0.0015514500000000002
          }
        ],
        "error": null
      }
    }
  },
  "S2: Intra-turn tool chain (3 tools × 3 turns)": {
    "description": "Small SOUL.md (~300 tok). 3 registered tools (alpha/beta/gamma). System prompt instructs: call exactly one tool per LLM response, in order. Each turn = up to 4 LLM calls. Tests intra-turn prefix caching.",
    "variants": {
      "V1_baseline": {
        "strategy": "none",
        "sys_chars": 13000,
        "tools_count": 3,
        "n_calls": 12,
        "totals": {
          "fresh": 123867,
          "cache_w": 0,
          "cache_r": 0,
          "completion": 732,
          "cost": 0.382581
        },
        "calls": [
          {
            "fresh": 4670,
            "cr": 0,
            "cw": 0,
            "comp": 79,
            "cost": 0.015195
          },
          {
            "fresh": 5925,
            "cr": 0,
            "cw": 0,
            "comp": 66,
            "cost": 0.018765
          },
          {
            "fresh": 7166,
            "cr": 0,
            "cw": 0,
            "comp": 66,
            "cost": 0.022488
          },
          {
            "fresh": 8408,
            "cr": 0,
            "cw": 0,
            "comp": 33,
            "cost": 0.025719
          },
          {
            "fresh": 8450,
            "cr": 0,
            "cw": 0,
            "comp": 79,
            "cost": 0.026535
          },
          {
            "fresh": 9705,
            "cr": 0,
            "cw": 0,
            "comp": 66,
            "cost": 0.030105000000000003
          },
          {
            "fresh": 10946,
            "cr": 0,
            "cw": 0,
            "comp": 66,
            "cost": 0.033828
          },
          {
            "fresh": 12188,
            "cr": 0,
            "cw": 0,
            "comp": 33,
            "cost": 0.037059
          },
          {
            "fresh": 12230,
            "cr": 0,
            "cw": 0,
            "comp": 79,
            "cost": 0.037875
          },
          {
            "fresh": 13485,
            "cr": 0,
            "cw": 0,
            "comp": 66,
            "cost": 0.041444999999999996
          },
          {
            "fresh": 14726,
            "cr": 0,
            "cw": 0,
            "comp": 66,
            "cost": 0.045168
          },
          {
            "fresh": 15968,
            "cr": 0,
            "cw": 0,
            "comp": 33,
            "cost": 0.048399000000000005
          }
        ],
        "error": null
      },
      "V2_raven": {
        "strategy": "tools+sys+rolling_tail",
        "sys_chars": 13000,
        "tools_count": 3,
        "n_calls": 12,
        "totals": {
          "fresh": 63,
          "cache_w": 26356,
          "cache_r": 97508,
          "completion": 717,
          "cost": 0.1390314
        },
        "calls": [
          {
            "fresh": 3,
            "cr": 0,
            "cw": 4677,
            "comp": 79,
            "cost": 0.01873275
          },
          {
            "fresh": 6,
            "cr": 4677,
            "cw": 1252,
            "comp": 66,
            "cost": 0.007106100000000001
          },
          {
            "fresh": 6,
            "cr": 4677,
            "cw": 2493,
            "comp": 66,
            "cost": 0.011759850000000002
          },
          {
            "fresh": 6,
            "cr": 7170,
            "cw": 1242,
            "comp": 28,
            "cost": 0.0072465
          },
          {
            "fresh": 3,
            "cr": 4303,
            "cw": 4149,
            "comp": 79,
            "cost": 0.01804365
          },
          {
            "fresh": 6,
            "cr": 8452,
            "cw": 1252,
            "comp": 66,
            "cost": 0.0082386
          },
          {
            "fresh": 6,
            "cr": 9704,
            "cw": 1241,
            "comp": 66,
            "cost": 0.00857295
          },
          {
            "fresh": 6,
            "cr": 10945,
            "cw": 1242,
            "comp": 28,
            "cost": 0.008379000000000001
          },
          {
            "fresh": 3,
            "cr": 8395,
            "cw": 3832,
            "comp": 79,
            "cost": 0.0180825
          },
          {
            "fresh": 6,
            "cr": 12227,
            "cw": 1252,
            "comp": 66,
            "cost": 0.0093711
          },
          {
            "fresh": 6,
            "cr": 13479,
            "cw": 1241,
            "comp": 66,
            "cost": 0.009705450000000001
          },
          {
            "fresh": 6,
            "cr": 13479,
            "cw": 2483,
            "comp": 28,
            "cost": 0.01379295
          }
        ],
        "error": null
      },
      "V3_hermes": {
        "strategy": "sys+last_3",
        "sys_chars": 12992,
        "tools_count": 3,
        "n_calls": 12,
        "totals": {
          "fresh": 63,
          "cache_w": 26358,
          "cache_r": 97545,
          "completion": 735,
          "cost": 0.13932
        },
        "calls": [
          {
            "fresh": 3,
            "cr": 0,
            "cw": 4672,
            "comp": 79,
            "cost": 0.018714
          },
          {
            "fresh": 6,
            "cr": 4672,
            "cw": 1252,
            "comp": 69,
            "cost": 0.007149600000000001
          },
          {
            "fresh": 6,
            "cr": 4696,
            "cw": 2472,
            "comp": 69,
            "cost": 0.0117318
          },
          {
            "fresh": 6,
            "cr": 5924,
            "cw": 2489,
            "comp": 28,
            "cost": 0.01154895
          },
          {
            "fresh": 3,
            "cr": 4299,
            "cw": 4154,
            "comp": 79,
            "cost": 0.0180612
          },
          {
            "fresh": 6,
            "cr": 8453,
            "cw": 1252,
            "comp": 69,
            "cost": 0.0082839
          },
          {
            "fresh": 6,
            "cr": 9705,
            "cw": 1244,
            "comp": 69,
            "cost": 0.008629500000000002
          },
          {
            "fresh": 6,
            "cr": 10949,
            "cw": 1245,
            "comp": 28,
            "cost": 0.00839145
          },
          {
            "fresh": 3,
            "cr": 8397,
            "cw": 3837,
            "comp": 79,
            "cost": 0.018101850000000003
          },
          {
            "fresh": 6,
            "cr": 12234,
            "cw": 1252,
            "comp": 69,
            "cost": 0.009418200000000002
          },
          {
            "fresh": 6,
            "cr": 13486,
            "cw": 1244,
            "comp": 69,
            "cost": 0.0097638
          },
          {
            "fresh": 6,
            "cr": 14730,
            "cw": 1245,
            "comp": 28,
            "cost": 0.00952575
          }
        ],
        "error": null
      }
    }
  },
  "S3: Mixed — one tool per turn (6 turns)": {
    "description": "Small SOUL.md (~300 tok). 1 tool (data_lookup). Each turn = 2 LLM calls (decide + respond). Tests the common real-world agent workload.",
    "variants": {
      "V1_baseline": {
        "strategy": "none",
        "sys_chars": 12826,
        "tools_count": 1,
        "n_calls": 12,
        "totals": {
          "fresh": 99126,
          "cache_w": 0,
          "cache_r": 0,
          "completion": 432,
          "cost": 0.303858
        },
        "calls": [
          {
            "fresh": 4480,
            "cr": 0,
            "cw": 0,
            "comp": 67,
            "cost": 0.014445000000000001
          },
          {
            "fresh": 5721,
            "cr": 0,
            "cw": 0,
            "comp": 5,
            "cost": 0.017238
          },
          {
            "fresh": 5744,
            "cr": 0,
            "cw": 0,
            "comp": 67,
            "cost": 0.018237
          },
          {
            "fresh": 6985,
            "cr": 0,
            "cw": 0,
            "comp": 5,
            "cost": 0.02103
          },
          {
            "fresh": 7008,
            "cr": 0,
            "cw": 0,
            "comp": 67,
            "cost": 0.022029
          },
          {
            "fresh": 8249,
            "cr": 0,
            "cw": 0,
            "comp": 5,
            "cost": 0.024822
          },
          {
            "fresh": 8272,
            "cr": 0,
            "cw": 0,
            "comp": 67,
            "cost": 0.025821
          },
          {
            "fresh": 9513,
            "cr": 0,
            "cw": 0,
            "comp": 5,
            "cost": 0.028614
          },
          {
            "fresh": 9536,
            "cr": 0,
            "cw": 0,
            "comp": 67,
            "cost": 0.029613
          },
          {
            "fresh": 10777,
            "cr": 0,
            "cw": 0,
            "comp": 5,
            "cost": 0.032406
          },
          {
            "fresh": 10800,
            "cr": 0,
            "cw": 0,
            "comp": 67,
            "cost": 0.033405
          },
          {
            "fresh": 12041,
            "cr": 0,
            "cw": 0,
            "comp": 5,
            "cost": 0.036198
          }
        ],
        "error": null
      },
      "V2_raven": {
        "strategy": "tools+sys+rolling_tail",
        "sys_chars": 12826,
        "tools_count": 1,
        "n_calls": 12,
        "totals": {
          "fresh": 54,
          "cache_w": 20296,
          "cache_r": 78962,
          "completion": 444,
          "cost": 0.10662060000000002
        },
        "calls": [
          {
            "fresh": 3,
            "cr": 0,
            "cw": 4487,
            "comp": 68,
            "cost": 0.01785525
          },
          {
            "fresh": 6,
            "cr": 4487,
            "cw": 1239,
            "comp": 6,
            "cost": 0.006100350000000001
          },
          {
            "fresh": 3,
            "cr": 4104,
            "cw": 1649,
            "comp": 68,
            "cost": 0.00844395
          },
          {
            "fresh": 6,
            "cr": 5753,
            "cw": 1239,
            "comp": 6,
            "cost": 0.0064801500000000005
          },
          {
            "fresh": 3,
            "cr": 5687,
            "cw": 1332,
            "comp": 68,
            "cost": 0.0077301
          },
          {
            "fresh": 6,
            "cr": 7019,
            "cw": 1239,
            "comp": 6,
            "cost": 0.00685995
          },
          {
            "fresh": 3,
            "cr": 6953,
            "cw": 1332,
            "comp": 68,
            "cost": 0.0081099
          },
          {
            "fresh": 6,
            "cr": 6953,
            "cw": 2571,
            "comp": 6,
            "cost": 0.011835150000000001
          },
          {
            "fresh": 3,
            "cr": 8219,
            "cw": 1332,
            "comp": 68,
            "cost": 0.008489700000000001
          },
          {
            "fresh": 6,
            "cr": 9485,
            "cw": 1305,
            "comp": 6,
            "cost": 0.00784725
          },
          {
            "fresh": 3,
            "cr": 9485,
            "cw": 1332,
            "comp": 68,
            "cost": 0.0088695
          },
          {
            "fresh": 6,
            "cr": 10817,
            "cw": 1239,
            "comp": 6,
            "cost": 0.00799935
          }
        ],
        "error": null
      },
      "V3_hermes": {
        "strategy": "sys+last_3",
        "sys_chars": 12818,
        "tools_count": 1,
        "n_calls": 12,
        "totals": {
          "fresh": 54,
          "cache_w": 19027,
          "cache_r": 80171,
          "completion": 444,
          "cost": 0.10222455
        },
        "calls": [
          {
            "fresh": 3,
            "cr": 0,
            "cw": 4482,
            "comp": 68,
            "cost": 0.017836499999999998
          },
          {
            "fresh": 6,
            "cr": 4482,
            "cw": 1239,
            "comp": 6,
            "cost": 0.00609885
          },
          {
            "fresh": 3,
            "cr": 4100,
            "cw": 1648,
            "comp": 68,
            "cost": 0.008439
          },
          {
            "fresh": 6,
            "cr": 5748,
            "cw": 1239,
            "comp": 6,
            "cost": 0.006478650000000001
          },
          {
            "fresh": 3,
            "cr": 5683,
            "cw": 1331,
            "comp": 68,
            "cost": 0.00772515
          },
          {
            "fresh": 6,
            "cr": 7014,
            "cw": 1239,
            "comp": 6,
            "cost": 0.00685845
          },
          {
            "fresh": 3,
            "cr": 6949,
            "cw": 1331,
            "comp": 68,
            "cost": 0.00810495
          },
          {
            "fresh": 6,
            "cr": 8206,
            "cw": 1313,
            "comp": 6,
            "cost": 0.00749355
          },
          {
            "fresh": 3,
            "cr": 8215,
            "cw": 1331,
            "comp": 68,
            "cost": 0.00848475
          },
          {
            "fresh": 6,
            "cr": 9546,
            "cw": 1239,
            "comp": 6,
            "cost": 0.00761805
          },
          {
            "fresh": 3,
            "cr": 9481,
            "cw": 1331,
            "comp": 68,
            "cost": 0.00886455
          },
          {
            "fresh": 6,
            "cr": 10747,
            "cw": 1304,
            "comp": 6,
            "cost": 0.0082221
          }
        ],
        "error": null
      }
    }
  }
}
```
