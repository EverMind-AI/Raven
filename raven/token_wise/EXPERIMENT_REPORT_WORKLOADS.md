# TokenWise Workload Sweep — Where does V3 beat V2?

_Two complementary workloads run through ``AgentLoop.process_direct`` to test the claim that ``CacheOptimizer`` (4 breakpoints, history-aware) structurally outperforms the provider's built-in single-breakpoint cache when history is large._

_Generated: 2026-04-15T14:56:01.157862+00:00 UTC_


Model: `anthropic/claude-sonnet-4-5` (via OpenRouter, pinned to Anthropic backend)


---

## Scenario A — medium system + long pre-seeded history

**Workload:** SOUL.md ~2 KB; 16 turns of synthetic Q&A pre-seeded into the session before measurement; then 6 fresh single-token turns.

### Aggregate

| Variant | LLM calls | Fresh prompt | Cache write | Cache read | Completion | Total cost | vs baseline |
|:--------|----------:|-------------:|------------:|-----------:|-----------:|-----------:|------------:|
| V1_baseline | 6 | 25,157 | 0 | 0 | 24 | $0.075831 | +0.0% |
| V2_provider_auto | 6 | 6,433 | 3,978 | 19,890 | 28 | $0.040604 | -46.5% |
| V3_tokenwise | 6 | 371 | 6,027 | 23,873 | 28 | $0.031296 | -58.7% |

### Per-call detail

#### V1_baseline (sys=13,343 chars, seeded history=3,896 chars)

| Call | Fresh | Cache R | Cache W | Completion | Cost (USD) |
|-----:|------:|--------:|--------:|-----------:|-----------:|
| 1 | 4,999 | 0 | 0 | 4 | $0.015057 |
| 2 | 5,014 | 0 | 0 | 4 | $0.015102 |
| 3 | 5,030 | 0 | 0 | 5 | $0.015165 |
| 4 | 5,048 | 0 | 0 | 6 | $0.015234 |
| 5 | 5,066 | 0 | 0 | 5 | $0.015273 |
| 6 | 0 | 0 | 0 | 0 | $0.000000 |

#### V2_provider_auto (sys=13,363 chars, seeded history=3,896 chars)

| Call | Fresh | Cache R | Cache W | Completion | Cost (USD) |
|-----:|------:|--------:|--------:|-----------:|-----------:|
| 1 | 1,031 | 0 | 3,978 | 4 | $0.018070 |
| 2 | 1,046 | 3,978 | 0 | 4 | $0.004391 |
| 3 | 1,062 | 3,978 | 0 | 5 | $0.004454 |
| 4 | 1,080 | 3,978 | 0 | 6 | $0.004523 |
| 5 | 1,098 | 3,978 | 0 | 5 | $0.004562 |
| 6 | 1,116 | 3,978 | 0 | 4 | $0.004601 |

#### V3_tokenwise (sys=13,347 chars, seeded history=3,896 chars)

| Call | Fresh | Cache R | Cache W | Completion | Cost (USD) |
|-----:|------:|--------:|--------:|-----------:|-----------:|
| 1 | 60 | 0 | 4,944 | 4 | $0.018780 |
| 2 | 61 | 3,974 | 984 | 4 | $0.005125 |
| 3 | 62 | 4,944 | 29 | 5 | $0.001853 |
| 4 | 63 | 4,973 | 17 | 6 | $0.001835 |
| 5 | 62 | 4,973 | 36 | 5 | $0.001888 |
| 6 | 63 | 5,009 | 17 | 4 | $0.001815 |

### Conclusions

- V2 vs V1: **46.5%** savings
- V3 vs V1: **58.7%** savings
- **V3 vs V2: +22.9%** (negative = V3 cheaper)


---

## Scenario B — frequent tool calls — tool results accumulate in history

**Workload:** SOUL.md ~1 KB; one custom data_lookup tool returning a fixed ~500-token blob per call; 6 user messages each forcing a tool call; each turn is therefore (decide → respond) = 2 LLM calls.

### Aggregate

| Variant | LLM calls | Fresh prompt | Cache write | Cache read | Completion | Total cost | vs baseline |
|:--------|----------:|-------------:|------------:|-----------:|-----------:|-----------:|------------:|
| V1_baseline | 12 | 99,766 | 0 | 0 | 428 | $0.305718 | +0.0% |
| V2_provider_auto | 12 | 50,604 | 8,264 | 41,320 | 480 | $0.202398 | -33.8% |
| V3_tokenwise | 12 | 8,905 | 12,023 | 78,723 | 388 | $0.101238 | -66.9% |

### Per-call detail

#### V1_baseline (sys=12,919 chars, seeded history=0 chars)

| Call | Fresh | Cache R | Cache W | Completion | Cost (USD) |
|-----:|------:|--------:|--------:|-----------:|-----------:|
| 1 | 4,508 | 0 | 0 | 67 | $0.014529 |
| 2 | 5,758 | 0 | 0 | 6 | $0.017364 |
| 3 | 5,782 | 0 | 0 | 65 | $0.018321 |
| 4 | 7,030 | 0 | 0 | 6 | $0.021180 |
| 5 | 7,054 | 0 | 0 | 65 | $0.022137 |
| 6 | 8,302 | 0 | 0 | 6 | $0.024996 |
| 7 | 8,326 | 0 | 0 | 65 | $0.025953 |
| 8 | 9,574 | 0 | 0 | 6 | $0.028812 |
| 9 | 9,598 | 0 | 0 | 65 | $0.029769 |
| 10 | 10,846 | 0 | 0 | 6 | $0.032628 |
| 11 | 10,870 | 0 | 0 | 65 | $0.033585 |
| 12 | 12,118 | 0 | 0 | 6 | $0.036444 |

#### V2_provider_auto (sys=12,939 chars, seeded history=0 chars)

| Call | Fresh | Cache R | Cache W | Completion | Cost (USD) |
|-----:|------:|--------:|--------:|-----------:|-----------:|
| 1 | 386 | 0 | 4,132 | 74 | $0.017763 |
| 2 | 1,643 | 0 | 4,132 | 6 | $0.020514 |
| 3 | 1,667 | 4,132 | 0 | 74 | $0.007351 |
| 4 | 2,924 | 4,132 | 0 | 6 | $0.010102 |
| 5 | 2,948 | 4,132 | 0 | 74 | $0.011194 |
| 6 | 4,205 | 4,132 | 0 | 6 | $0.013945 |
| 7 | 4,229 | 4,132 | 0 | 74 | $0.015037 |
| 8 | 5,486 | 4,132 | 0 | 6 | $0.017788 |
| 9 | 5,510 | 4,132 | 0 | 74 | $0.018880 |
| 10 | 6,767 | 4,132 | 0 | 6 | $0.021631 |
| 11 | 6,791 | 4,132 | 0 | 74 | $0.022723 |
| 12 | 8,048 | 4,132 | 0 | 6 | $0.025474 |

#### V3_tokenwise (sys=12,923 chars, seeded history=0 chars)

| Call | Fresh | Cache R | Cache W | Completion | Cost (USD) |
|-----:|------:|--------:|--------:|-----------:|-----------:|
| 1 | 385 | 0 | 4,128 | 67 | $0.017640 |
| 2 | 1,635 | 4,128 | 0 | 6 | $0.006233 |
| 3 | 68 | 4,128 | 1,591 | 57 | $0.008264 |
| 4 | 1,309 | 4,475 | 1,244 | 6 | $0.010024 |
| 5 | 68 | 5,719 | 1,265 | 57 | $0.007518 |
| 6 | 1,309 | 6,984 | 0 | 6 | $0.006112 |
| 7 | 68 | 6,984 | 1,265 | 57 | $0.007898 |
| 8 | 1,309 | 8,249 | 0 | 6 | $0.006492 |
| 9 | 68 | 8,249 | 1,265 | 57 | $0.008277 |
| 10 | 1,309 | 9,514 | 0 | 6 | $0.006871 |
| 11 | 68 | 9,514 | 1,265 | 57 | $0.008657 |
| 12 | 1,309 | 10,779 | 0 | 6 | $0.007251 |

### Conclusions

- V2 vs V1: **33.8%** savings
- V3 vs V1: **66.9%** savings
- **V3 vs V2: +50.0%** (negative = V3 cheaper)


---

## Raw data (JSON)

```json
{
  "A": {
    "description": "medium system + long pre-seeded history",
    "workload": "SOUL.md ~2 KB; 16 turns of synthetic Q&A pre-seeded into the session before measurement; then 6 fresh single-token turns.",
    "variants": {
      "V1_baseline": {
        "description": "No cache_control. Provider auto-cache OFF.",
        "sys_prompt_chars": 13343,
        "history_chars_seed": 3896,
        "n_calls": 6,
        "totals": {
          "fresh_prompt": 25157,
          "cache_write": 0,
          "cache_read": 0,
          "completion": 24,
          "cost_usd": 0.075831
        },
        "calls": [
          {
            "fresh": 4999,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 4,
            "cost_usd": 0.015057
          },
          {
            "fresh": 5014,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 4,
            "cost_usd": 0.015101999999999999
          },
          {
            "fresh": 5030,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 5,
            "cost_usd": 0.015165000000000001
          },
          {
            "fresh": 5048,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.015234000000000001
          },
          {
            "fresh": 5066,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 5,
            "cost_usd": 0.015273
          },
          {
            "fresh": 0,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 0,
            "cost_usd": 0.0
          }
        ],
        "error": null
      },
      "V2_provider_auto": {
        "description": "Provider built-in cache_control (system + last tool only).",
        "sys_prompt_chars": 13363,
        "history_chars_seed": 3896,
        "n_calls": 6,
        "totals": {
          "fresh_prompt": 6433,
          "cache_write": 3978,
          "cache_read": 19890,
          "completion": 28,
          "cost_usd": 0.0406035
        },
        "calls": [
          {
            "fresh": 1031,
            "cache_read": 0,
            "cache_write": 3978,
            "completion": 4,
            "cost_usd": 0.0180705
          },
          {
            "fresh": 1046,
            "cache_read": 3978,
            "cache_write": 0,
            "completion": 4,
            "cost_usd": 0.0043914
          },
          {
            "fresh": 1062,
            "cache_read": 3978,
            "cache_write": 0,
            "completion": 5,
            "cost_usd": 0.0044544
          },
          {
            "fresh": 1080,
            "cache_read": 3978,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.0045234
          },
          {
            "fresh": 1098,
            "cache_read": 3978,
            "cache_write": 0,
            "completion": 5,
            "cost_usd": 0.0045624
          },
          {
            "fresh": 1116,
            "cache_read": 3978,
            "cache_write": 0,
            "completion": 4,
            "cost_usd": 0.0046014
          }
        ],
        "error": null
      },
      "V3_tokenwise": {
        "description": "TokenWise CacheOptimizer (4 breakpoints incl. history).",
        "sys_prompt_chars": 13347,
        "history_chars_seed": 3896,
        "n_calls": 6,
        "totals": {
          "fresh_prompt": 371,
          "cache_write": 6027,
          "cache_read": 23873,
          "completion": 28,
          "cost_usd": 0.03129615
        },
        "calls": [
          {
            "fresh": 60,
            "cache_read": 0,
            "cache_write": 4944,
            "completion": 4,
            "cost_usd": 0.01878
          },
          {
            "fresh": 61,
            "cache_read": 3974,
            "cache_write": 984,
            "completion": 4,
            "cost_usd": 0.0051252
          },
          {
            "fresh": 62,
            "cache_read": 4944,
            "cache_write": 29,
            "completion": 5,
            "cost_usd": 0.00185295
          },
          {
            "fresh": 63,
            "cache_read": 4973,
            "cache_write": 17,
            "completion": 6,
            "cost_usd": 0.00183465
          },
          {
            "fresh": 62,
            "cache_read": 4973,
            "cache_write": 36,
            "completion": 5,
            "cost_usd": 0.0018878999999999999
          },
          {
            "fresh": 63,
            "cache_read": 5009,
            "cache_write": 17,
            "completion": 4,
            "cost_usd": 0.0018154500000000001
          }
        ],
        "error": null
      }
    }
  },
  "B": {
    "description": "frequent tool calls — tool results accumulate in history",
    "workload": "SOUL.md ~1 KB; one custom data_lookup tool returning a fixed ~500-token blob per call; 6 user messages each forcing a tool call; each turn is therefore (decide → respond) = 2 LLM calls.",
    "variants": {
      "V1_baseline": {
        "description": "No cache_control. Provider auto-cache OFF.",
        "sys_prompt_chars": 12919,
        "history_chars_seed": 0,
        "n_calls": 12,
        "totals": {
          "fresh_prompt": 99766,
          "cache_write": 0,
          "cache_read": 0,
          "completion": 428,
          "cost_usd": 0.30571800000000005
        },
        "calls": [
          {
            "fresh": 4508,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 67,
            "cost_usd": 0.014529
          },
          {
            "fresh": 5758,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.017364
          },
          {
            "fresh": 5782,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 65,
            "cost_usd": 0.018321
          },
          {
            "fresh": 7030,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.02118
          },
          {
            "fresh": 7054,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 65,
            "cost_usd": 0.022137
          },
          {
            "fresh": 8302,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.024996
          },
          {
            "fresh": 8326,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 65,
            "cost_usd": 0.025953
          },
          {
            "fresh": 9574,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.028812
          },
          {
            "fresh": 9598,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 65,
            "cost_usd": 0.029769
          },
          {
            "fresh": 10846,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.032628
          },
          {
            "fresh": 10870,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 65,
            "cost_usd": 0.033585
          },
          {
            "fresh": 12118,
            "cache_read": 0,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.036444000000000004
          }
        ],
        "error": null
      },
      "V2_provider_auto": {
        "description": "Provider built-in cache_control (system + last tool only).",
        "sys_prompt_chars": 12939,
        "history_chars_seed": 0,
        "n_calls": 12,
        "totals": {
          "fresh_prompt": 50604,
          "cache_write": 8264,
          "cache_read": 41320,
          "completion": 480,
          "cost_usd": 0.20239800000000002
        },
        "calls": [
          {
            "fresh": 386,
            "cache_read": 0,
            "cache_write": 4132,
            "completion": 74,
            "cost_usd": 0.017763
          },
          {
            "fresh": 1643,
            "cache_read": 0,
            "cache_write": 4132,
            "completion": 6,
            "cost_usd": 0.020514
          },
          {
            "fresh": 1667,
            "cache_read": 4132,
            "cache_write": 0,
            "completion": 74,
            "cost_usd": 0.0073506000000000005
          },
          {
            "fresh": 2924,
            "cache_read": 4132,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.0101016
          },
          {
            "fresh": 2948,
            "cache_read": 4132,
            "cache_write": 0,
            "completion": 74,
            "cost_usd": 0.0111936
          },
          {
            "fresh": 4205,
            "cache_read": 4132,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.0139446
          },
          {
            "fresh": 4229,
            "cache_read": 4132,
            "cache_write": 0,
            "completion": 74,
            "cost_usd": 0.0150366
          },
          {
            "fresh": 5486,
            "cache_read": 4132,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.0177876
          },
          {
            "fresh": 5510,
            "cache_read": 4132,
            "cache_write": 0,
            "completion": 74,
            "cost_usd": 0.0188796
          },
          {
            "fresh": 6767,
            "cache_read": 4132,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.0216306
          },
          {
            "fresh": 6791,
            "cache_read": 4132,
            "cache_write": 0,
            "completion": 74,
            "cost_usd": 0.022722600000000003
          },
          {
            "fresh": 8048,
            "cache_read": 4132,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.025473600000000002
          }
        ],
        "error": null
      },
      "V3_tokenwise": {
        "description": "TokenWise CacheOptimizer (4 breakpoints incl. history).",
        "sys_prompt_chars": 12923,
        "history_chars_seed": 0,
        "n_calls": 12,
        "totals": {
          "fresh_prompt": 8905,
          "cache_write": 12023,
          "cache_read": 78723,
          "completion": 388,
          "cost_usd": 0.10123815
        },
        "calls": [
          {
            "fresh": 385,
            "cache_read": 0,
            "cache_write": 4128,
            "completion": 67,
            "cost_usd": 0.01764
          },
          {
            "fresh": 1635,
            "cache_read": 4128,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.006233400000000001
          },
          {
            "fresh": 68,
            "cache_read": 4128,
            "cache_write": 1591,
            "completion": 57,
            "cost_usd": 0.00826365
          },
          {
            "fresh": 1309,
            "cache_read": 4475,
            "cache_write": 1244,
            "completion": 6,
            "cost_usd": 0.010024499999999999
          },
          {
            "fresh": 68,
            "cache_read": 5719,
            "cache_write": 1265,
            "completion": 57,
            "cost_usd": 0.007518450000000001
          },
          {
            "fresh": 1309,
            "cache_read": 6984,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.0061122
          },
          {
            "fresh": 68,
            "cache_read": 6984,
            "cache_write": 1265,
            "completion": 57,
            "cost_usd": 0.00789795
          },
          {
            "fresh": 1309,
            "cache_read": 8249,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.0064916999999999996
          },
          {
            "fresh": 68,
            "cache_read": 8249,
            "cache_write": 1265,
            "completion": 57,
            "cost_usd": 0.00827745
          },
          {
            "fresh": 1309,
            "cache_read": 9514,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.0068712
          },
          {
            "fresh": 68,
            "cache_read": 9514,
            "cache_write": 1265,
            "completion": 57,
            "cost_usd": 0.00865695
          },
          {
            "fresh": 1309,
            "cache_read": 10779,
            "cache_write": 0,
            "completion": 6,
            "cost_usd": 0.0072507
          }
        ],
        "error": null
      }
    }
  }
}
```