# TokenWise Step 1+2 — Ablation Experiment Report

_Driven through the real ``AgentLoop.process_direct`` so the experiment exercises session management, context assembly, TokenWise hooks, and provider invocation as a complete stack._

_Generated: 2026-04-15T14:42:43.349178+00:00 UTC_


## Setup

- Model: `anthropic/claude-sonnet-4-5` (via OpenRouter, pinned to Anthropic backend)
- Turns per variant: 6
- Driver: ``AgentLoop.process_direct`` (real production code path)
- Workspace: per-variant tmp dir with seeded SOUL.md / AGENTS.md / USER.md / TOOLS.md
- Default tools cleared on the loop (no tool-call noise)
- Memory consolidator threshold raised to 200K tokens (won't trigger)
- EverOS / MCP / channels: disabled
- Cost guard: $0.50 (hard abort)

## Variants

- **V1_baseline** — No cache_control. AgentLoop with empty CacheOptimizer; provider auto-cache OFF.
  - Assembled system prompt: 21,483 chars
- **V2_provider_auto** — LiteLLMProvider built-in cache_control (system + last tool); no CacheOptimizer.
  - Assembled system prompt: 21,503 chars
- **V3_tokenwise** — TokenWise CacheOptimizer (4 breakpoints) installed in the AgentLoop's StrategyRegistry; provider auto-cache OFF.
  - Assembled system prompt: 21,487 chars

## Aggregate results

| Variant | Fresh prompt | Cache write | Cache read | Completion | Total cost | vs baseline |
|:--------|-------------:|------------:|-----------:|-----------:|-----------:|------------:|
| V1_baseline | 35,047 | 0 | 0 | 28 | $0.105561 | +0.0% |
| V2_provider_auto | 613 | 11,498 | 22,996 | 28 | $0.052275 | -50.5% |
| V3_tokenwise | 371 | 11,637 | 23,069 | 28 | $0.052092 | -50.7% |

## Per-turn detail

### V1_baseline

| Turn | Fresh | Cache R | Cache W | Completion | Cost (USD) |
|-----:|------:|--------:|--------:|-----------:|-----------:|
| 1 | 5,800 | 0 | 0 | 4 | $0.017460 |
| 2 | 5,815 | 0 | 0 | 4 | $0.017505 |
| 3 | 5,831 | 0 | 0 | 5 | $0.017568 |
| 4 | 5,849 | 0 | 0 | 6 | $0.017637 |
| 5 | 5,867 | 0 | 0 | 5 | $0.017676 |
| 6 | 5,885 | 0 | 0 | 4 | $0.017715 |

### V2_provider_auto

| Turn | Fresh | Cache R | Cache W | Completion | Cost (USD) |
|-----:|------:|--------:|--------:|-----------:|-----------:|
| 1 | 61 | 0 | 5,749 | 4 | $0.021802 |
| 2 | 76 | 5,749 | 0 | 4 | $0.002013 |
| 3 | 92 | 5,749 | 0 | 5 | $0.002076 |
| 4 | 110 | 0 | 5,749 | 6 | $0.021979 |
| 5 | 128 | 5,749 | 0 | 5 | $0.002184 |
| 6 | 146 | 5,749 | 0 | 4 | $0.002223 |

### V3_tokenwise

| Turn | Fresh | Cache R | Cache W | Completion | Cost (USD) |
|-----:|------:|--------:|--------:|-----------:|-----------:|
| 1 | 60 | 0 | 5,745 | 4 | $0.021784 |
| 2 | 61 | 5,745 | 14 | 4 | $0.002019 |
| 3 | 62 | 0 | 5,774 | 5 | $0.021914 |
| 4 | 63 | 5,774 | 17 | 6 | $0.002075 |
| 5 | 62 | 5,759 | 51 | 5 | $0.002180 |
| 6 | 63 | 5,791 | 36 | 4 | $0.002121 |

## Conclusions

- **Cheapest variant: `V3_tokenwise`** at $0.052092
- **Savings vs `V1_baseline`: 50.7%** ($0.053469 absolute)
- `V2_provider_auto`: 97.4% of input tokens served from cache
- `V3_tokenwise`: 98.4% of input tokens served from cache

## Caveat — OpenRouter routing affinity

OpenRouter's default routing distributes Anthropic requests across multiple backend instances, which empirically destroys the prompt cache: ``cache_write`` fires every call but ``cache_read`` stays 0. The variant runner pins the backend with ``provider={'order': ['Anthropic'], 'allow_fallbacks': False}`` (passed through ``LiteLLMProvider.extra_body``) to restore normal cache semantics. Direct Anthropic API users do not need this.

## Raw data (JSON)

```json
{
  "V1_baseline": {
    "description": "No cache_control. AgentLoop with empty CacheOptimizer; provider auto-cache OFF.",
    "sys_prompt_chars": 21483,
    "error": null,
    "turns": [
      {
        "turn": 1,
        "fresh_prompt": 5800,
        "cache_read": 0,
        "cache_write": 0,
        "completion": 4,
        "cost_usd": 0.01746
      },
      {
        "turn": 2,
        "fresh_prompt": 5815,
        "cache_read": 0,
        "cache_write": 0,
        "completion": 4,
        "cost_usd": 0.017505000000000003
      },
      {
        "turn": 3,
        "fresh_prompt": 5831,
        "cache_read": 0,
        "cache_write": 0,
        "completion": 5,
        "cost_usd": 0.017568
      },
      {
        "turn": 4,
        "fresh_prompt": 5849,
        "cache_read": 0,
        "cache_write": 0,
        "completion": 6,
        "cost_usd": 0.017637
      },
      {
        "turn": 5,
        "fresh_prompt": 5867,
        "cache_read": 0,
        "cache_write": 0,
        "completion": 5,
        "cost_usd": 0.017676
      },
      {
        "turn": 6,
        "fresh_prompt": 5885,
        "cache_read": 0,
        "cache_write": 0,
        "completion": 4,
        "cost_usd": 0.017715
      }
    ],
    "totals": {
      "fresh_prompt": 35047,
      "cache_write": 0,
      "cache_read": 0,
      "completion": 28,
      "cost_usd": 0.10556100000000002
    }
  },
  "V2_provider_auto": {
    "description": "LiteLLMProvider built-in cache_control (system + last tool); no CacheOptimizer.",
    "sys_prompt_chars": 21503,
    "error": null,
    "turns": [
      {
        "turn": 1,
        "fresh_prompt": 61,
        "cache_read": 0,
        "cache_write": 5749,
        "completion": 4,
        "cost_usd": 0.02180175
      },
      {
        "turn": 2,
        "fresh_prompt": 76,
        "cache_read": 5749,
        "cache_write": 0,
        "completion": 4,
        "cost_usd": 0.0020127
      },
      {
        "turn": 3,
        "fresh_prompt": 92,
        "cache_read": 5749,
        "cache_write": 0,
        "completion": 5,
        "cost_usd": 0.0020756999999999998
      },
      {
        "turn": 4,
        "fresh_prompt": 110,
        "cache_read": 0,
        "cache_write": 5749,
        "completion": 6,
        "cost_usd": 0.021978750000000002
      },
      {
        "turn": 5,
        "fresh_prompt": 128,
        "cache_read": 5749,
        "cache_write": 0,
        "completion": 5,
        "cost_usd": 0.0021837000000000002
      },
      {
        "turn": 6,
        "fresh_prompt": 146,
        "cache_read": 5749,
        "cache_write": 0,
        "completion": 4,
        "cost_usd": 0.0022227
      }
    ],
    "totals": {
      "fresh_prompt": 613,
      "cache_write": 11498,
      "cache_read": 22996,
      "completion": 28,
      "cost_usd": 0.052275300000000004
    }
  },
  "V3_tokenwise": {
    "description": "TokenWise CacheOptimizer (4 breakpoints) installed in the AgentLoop's StrategyRegistry; provider auto-cache OFF.",
    "sys_prompt_chars": 21487,
    "error": null,
    "turns": [
      {
        "turn": 1,
        "fresh_prompt": 60,
        "cache_read": 0,
        "cache_write": 5745,
        "completion": 4,
        "cost_usd": 0.02178375
      },
      {
        "turn": 2,
        "fresh_prompt": 61,
        "cache_read": 5745,
        "cache_write": 14,
        "completion": 4,
        "cost_usd": 0.0020190000000000004
      },
      {
        "turn": 3,
        "fresh_prompt": 62,
        "cache_read": 0,
        "cache_write": 5774,
        "completion": 5,
        "cost_usd": 0.021913500000000002
      },
      {
        "turn": 4,
        "fresh_prompt": 63,
        "cache_read": 5774,
        "cache_write": 17,
        "completion": 6,
        "cost_usd": 0.0020749500000000003
      },
      {
        "turn": 5,
        "fresh_prompt": 62,
        "cache_read": 5759,
        "cache_write": 51,
        "completion": 5,
        "cost_usd": 0.0021799500000000004
      },
      {
        "turn": 6,
        "fresh_prompt": 63,
        "cache_read": 5791,
        "cache_write": 36,
        "completion": 4,
        "cost_usd": 0.0021213
      }
    ],
    "totals": {
      "fresh_prompt": 371,
      "cache_write": 11637,
      "cache_read": 23069,
      "completion": 28,
      "cost_usd": 0.05209245
    }
  }
}
```