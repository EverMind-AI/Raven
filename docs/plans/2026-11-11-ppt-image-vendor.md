# PPT image search follows the host's search vendor: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ppt_image_search` speak the host's selected search vendor (Serper, SerpApi, Brave, Firecrawl, Serply), fall back to Serper, and say in the UI when no picture tool is available.

**Architecture:** The deck engine stops owning a Serper-only tool and subclasses the host's `ImageSearchTool`, which already implements five vendor requests. A narrower `PPT_IMAGE_SEARCH_VENDORS` tuple excludes Tavily (no dimensions, no source page). The route-readiness probe and the UI use the same resolver, so the card, the Settings note and the dispatch cannot disagree.

**Tech Stack:** Python 3.12 (pytest), TypeScript/React (vitest), OpenRPC-generated client.

**Spec:** `docs/specs/2026-11-11-ppt-image-vendor-design.md`

## Global Constraints

- Fallback rule (owner ruling): the selected vendor when it is a PPT vendor with a key, else Serper, else no tool.
- `IMAGE_SEARCH_VENDORS` keeps Tavily; `PPT_IMAGE_SEARCH_VENDORS` is exactly `serper, serpapi, brave, firecrawl, serply`.
- A key resolved for one vendor is never sent to another vendor's endpoint.
- No new dependency; tests never call a vendor network endpoint (monkeypatched clients only).
- Source, comments, log lines and test fixtures are English. New UI copy goes in `i18n/messages.json` with `en` and `zh`; CJK only there.
- pytest command in this worktree: `PYTHONPATH=$PWD /Evermind/sh_evermind/xuedizhan/Raven/.venv/bin/python -m pytest <paths> -q`.
- UI command: `npm test --prefix ui-web -- <paths>`; generation: `npm run gen --prefix ui-web`.
- No commit steps in this plan: `AGENTS.md` 3.4 forbids committing without the owner's explicit word. Leave the tree staged-free and report.

## Review Focus

- A Serper key reaching a SerpApi/Brave/Firecrawl/Serply endpoint (the deck slice still carries `imageSearch.apiKey`).
- `ppt_image_search` offered with no usable vendor or key, so every call fails.
- The route saying ready while the engine has no tool, or the reverse (two rules drifting).
- Serply's `image.src` (a Google thumbnail) used as the picture instead of `original_image.src`.
- A dimension-less hit reaching the deck renderer (`size unknown` in a deck result).

---

### Task 1: Serply on the host image surface

**Files:**
- Modify: `raven/agent/tools/web.py` (`IMAGE_SEARCH_VENDORS`, `ImageSearchTool._provider_request`, `ImageSearchTool.normalise_hits`)
- Test: `tests/test_agent_loop_web_tools.py` (near the existing `test_every_vendor_with_an_image_surface_is_read_into_the_same_hit`)

**Interfaces:**
- Consumes: `ImageSearchTool`, `ImageHit`, `normalise_hits(data, vendor)`.
- Produces: `"serply"` accepted by `ImageSearchTool(provider=...)`; a hit with `image_url=original_image.src`, `width/height=int(...)`, `source=link.domain`, `page=link.href`.

- [ ] **Step 1: Write the failing test**

```python
def test_serply_image_results_are_read_from_the_original_image() -> None:
    hit = ImageSearchTool(api_key="k", provider="serply").normalise_hits(
        {
            "image_results": [
                {
                    "image": {"src": "https://encrypted-tbn0.gstatic.com/thumb.jpg", "alt": "bike"},
                    "link": {"href": "https://shop.example/bike", "title": "Vintage Bicycle", "domain": "shop.example"},
                    "original_image": {"src": "https://shop.example/bike.png", "width": "1170", "height": "764"},
                }
            ]
        }
    )[0]

    assert (hit.title, hit.image_url, hit.width, hit.height) == (
        "Vintage Bicycle",
        "https://shop.example/bike.png",
        1170,
        764,
    )
    assert hit.source == "shop.example" and hit.page == "https://shop.example/bike"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONPATH=$PWD /Evermind/sh_evermind/xuedizhan/Raven/.venv/bin/python -m pytest tests/test_agent_loop_web_tools.py::test_serply_image_results_are_read_from_the_original_image -q`
Expected: FAIL, `ValueError` naming an unknown image provider.

- [ ] **Step 3: Implement**

Add `"serply"` to `IMAGE_SEARCH_VENDORS`. In `_provider_request`, before the Firecrawl fallthrough:

```python
if vendor == "serply":
    return await client.get(
        "https://api.serply.io/v1/image",
        params={"q": query},
        headers={"Accept": "application/json", "X-Api-Key": key},
        timeout=15.0,
    )
```

In `normalise_hits`, add a `serply` branch reading `image_results`: `original_image.src` (never `image.src`), `number(original_image.width/height)`, `link.href`, `link.title` (else `image.alt`), `link.domain` (else the href's netloc).

- [ ] **Step 4: Run the test again**

Run the same command. Expected: PASS. Then run the whole file: `pytest tests/test_agent_loop_web_tools.py -q`.

---

### Task 2: The PPT vendor set and its resolver

**Files:**
- Modify: `raven/agent/tools/web.py` (beside `IMAGE_SEARCH_VENDORS` / `image_search_vendor`)
- Test: `tests/test_agent_tools_web_providers.py`

**Interfaces:**
- Produces: `PPT_IMAGE_SEARCH_VENDORS: tuple[str, ...] = ("serper", "serpapi", "brave", "firecrawl", "serply")`; `ppt_image_vendor(selected: str, key_for: Callable[[str], str | None] | None = None) -> str` returning the selected vendor when it is in the tuple and keyed, else `"serper"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_ppt_set_is_the_host_set_without_tavily() -> None:
    assert "tavily" in IMAGE_SEARCH_VENDORS
    assert set(PPT_IMAGE_SEARCH_VENDORS) == set(IMAGE_SEARCH_VENDORS) - {"tavily"}


def test_ppt_image_vendor_takes_a_keyed_ppt_vendor() -> None:
    assert ppt_image_vendor("serpapi", {"serpapi": "k"}.get) == "serpapi"


def test_ppt_image_vendor_falls_back_to_serper() -> None:
    assert ppt_image_vendor("tavily", {"tavily": "tv", "serper": "sp"}.get) == "serper"
    assert ppt_image_vendor("serpapi", {"serper": "sp"}.get) == "serper"
    assert ppt_image_vendor("exa", {}.get) == "serper", "the caller checks the key"
```

- [ ] **Step 2: Run and watch them fail**

Run: `pytest tests/test_agent_tools_web_providers.py -k "ppt_set or ppt_image_vendor" -q`
Expected: FAIL, name not importable.

- [ ] **Step 3: Implement** the tuple and the resolver next to `image_search_vendor`, with a docstring naming the fallback rule and why Tavily is excluded.

- [ ] **Step 4: Run** the same selection. Expected: PASS.

---

### Task 3: `require_dimensions` on the host tool

**Files:**
- Modify: `raven/agent/tools/web.py` (`ImageSearchTool.__init__`, `_search_images`)
- Test: `tests/test_agent_loop_web_tools.py`

**Interfaces:**
- Produces: `ImageSearchTool(..., require_dimensions: bool = False)`. When true, a hit with `width is None` or `height is None` is dropped and the renderer never prints `size unknown`.

- [ ] **Step 1: Write the failing test**

```python
def test_require_dimensions_drops_a_hit_whose_size_the_vendor_did_not_report() -> None:
    payload = {"images": [{"url": "https://cdn.example/a.jpg", "description": "no size"}]}
    permissive = ImageSearchTool(api_key="k", provider="tavily")
    strict = ImageSearchTool(api_key="k", provider="tavily", require_dimensions=True)

    assert [h.image_url for h in permissive.normalise_hits(payload)] == ["https://cdn.example/a.jpg"]
    assert strict.normalise_hits(payload) == []
```

Add a second assertion that the rendered text of a strict tool has no `size unknown` (monkeypatch the client the way `test_every_vendor_with_an_image_surface_is_read_into_the_same_hit` does, or call the tool's `_search_images` with a patched client). Keep it to one behavior: unknown size dropped before rendering.

- [ ] **Step 2: Run it — FAIL** with `TypeError: unexpected keyword argument 'require_dimensions'`.

- [ ] **Step 3: Implement**: store the flag; in `_search_images`, when true add `hit.width is not None and hit.height is not None` to the filter. No renderer change is needed — the filter makes the `size unknown` branch unreachable.

- [ ] **Step 4: Run** the file. Expected: PASS.

---

### Task 4: `imageSearch.provider` in the engine config

**Files:**
- Modify: `plugins-dist/ppt-engine/raven_ppt/plugin/config.py`
- Test: `tests/test_ppt_engine_plugin.py` (or `tests/test_ppt_engine_config.py` if that is where `EngineConfig.from_slice` is pinned)

**Interfaces:**
- Produces: `EngineConfig.image_search_provider: str` (`"host"` when absent), read from `imageSearch.provider`. A value other than `"host"` or a member of `PPT_IMAGE_SEARCH_VENDORS` raises `ValueError` naming the accepted values.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_engine_reads_the_image_search_provider() -> None:
    assert EngineConfig.from_slice({"imageSearch": {"provider": "serpapi"}}).image_search_provider == "serpapi"
    assert EngineConfig.from_slice({}).image_search_provider == "host"


def test_an_unknown_image_search_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown ppt image search provider"):
        EngineConfig.from_slice({"imageSearch": {"provider": "tavily"}})
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement** in `from_slice`: read the string, default `"host"`, validate against `{"host", *PPT_IMAGE_SEARCH_VENDORS}` (import the tuple from `raven.agent.tools.web` inside the function to keep the engine importing late).

- [ ] **Step 4: Run.** Expected: PASS.

---

### Task 5: The deck tool delegates to the host tool

**Files:**
- Modify: `plugins-dist/ppt-engine/raven_ppt/plugin/image_search.py`
- Modify: `plugins-dist/ppt-engine/raven_ppt/plugin/__init__.py`
- Test: `tests/test_ppt_engine_image_search.py`

**Interfaces:**
- Consumes: `ImageSearchTool` (Task 3), `ppt_image_vendor` (Task 2), `EngineConfig.image_search_provider` (Task 4).
- Produces: `PptImageSearchTool(ImageSearchTool)` with `name="ppt_image_search"`, the deck description, `require_dimensions=True`; module aliases `MAX_QUERIES = IMAGE_MAX_QUERIES`. `plugin.image_search()` returns `None` when no vendor/key resolves.

- [ ] **Step 1: Update the test harness and write the failing tests**

The engine module no longer imports `httpx`; patch `raven.agent.tools.web.httpx.AsyncClient` instead of `raven_ppt.plugin.image_search.httpx.AsyncClient`. Keep `_Client`'s `post` (Serper) and add a `get` for SerpApi/Serply shapes when a case needs it.

```python
def _engine(web, slice_cfg=None):
    """A plugin locator whose live web config is `web` (a WebToolsConfig)."""
    # Build the locator the way test_ppt_engine_plugin.py builds one; services.web_config returns `web`.
```

New tests:
- `test_the_deck_tool_follows_the_hosts_selected_vendor`: web provider `serpapi` with a key → `image_search()` yields a tool whose `provider == "serpapi"`.
- `test_the_deck_tool_falls_back_to_serper`: web provider `tavily` (keyed) with a Serper key → tool provider `"serper"`.
- `test_no_picture_key_anywhere_withholds_the_tool`: no provider keys → `image_search()` is `None`.
- `test_the_slice_key_is_spent_on_serper_only`: slice `imageSearch.apiKey = "slice-serper"`, web provider `serpapi` with `"serp-key"` → the call carries `serp-key`, never `slice-serper`.

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement**

`image_search.py` becomes a subclass: import `ImageSearchTool` and `IMAGE_MAX_QUERIES` from `raven.agent.tools.web`; override `name`, `description`; `__init__(api_key=None, max_results=5, proxy=None, provider="serper", require_dimensions=True)` calls `super().__init__(..., require_dimensions=True)`. Keep `MAX_QUERIES` exported as an alias.

`plugin/__init__.py`: replace `image_search_key()` with `image_search_vendor()` and `image_search_key(vendor)` as in the spec (slice key only for `serper`; every other vendor from `web.vendor_key`). `image_search()` resolves vendor then key; returns `None` when either is empty; builds `PptImageSearchTool(api_key=..., proxy=self.web_proxy(), provider=vendor)`.

- [ ] **Step 4: Run** `tests/test_ppt_engine_image_search.py -q` and `tests/test_ppt_engine_plugin.py -q`. Expected: PASS. Update the missing-key assertion to the host tool's text (`"API key not configured"` naming `tools.web.providers.<vendor>.apiKey`).

---

### Task 6: The product folder declares `host`

**Files:**
- Modify: `agents/raven-ppt/config.json`
- Test: `tests/test_agents_ppt_launcher.py`

**Interfaces:**
- Produces: the shipped folder's `plugins.config["ppt-engine"].imageSearch.provider == "host"`, preserved in the rendered config; the Serper key bridge remains.

- [ ] **Step 1: Write the failing test**

```python
def test_the_shipped_slice_lets_the_engine_follow_the_hosts_vendor(grounded):
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["plugins"]["config"]["ppt-engine"]["imageSearch"]["provider"] == "host"
```

In the same test, set a host Serper key and assert `imageSearch.apiKey` is still bridged (the legacy override).

- [ ] **Step 2: Run — FAIL** (`KeyError: 'imageSearch'` or `provider`).

- [ ] **Step 3: Implement**: add `"imageSearch": {"provider": "host"}` to the shipped slice in `agents/raven-ppt/config.json`.

- [ ] **Step 4: Run** `tests/test_agents_ppt_launcher.py -q`. Expected: PASS.

---

### Task 7: Route readiness uses the same rule

**Files:**
- Modify: `raven/agent/subagent/vendored_agents.py` (`product_picture_vendor`, new `picture_search_ready`)
- Modify: `raven/agent/loop/wiring.py` (`_lane_searches_images`)
- Test: `tests/test_subagent_routed_lane_readiness.py`

**Interfaces:**
- Produces: `product_picture_vendor(row)` returns `"host"` when a plugin slice in the folder's config says `imageSearch.provider == "host"`; `picture_search_ready(target: str, host_vendor: str, key_for: Callable[[str], str]) -> bool` applies the three-way rule.
- `_lane_searches_images` delegates to `picture_search_ready`.

- [ ] **Step 1: Write the failing tests**

- `test_a_host_following_lane_reads_the_hosts_selected_vendor`: folder config gains `plugins.config["ppt-engine"].imageSearch.provider = "host"`; host on `serpapi` with a SerpApi key and no Serper key → route open.
- `test_a_host_following_lane_falls_back_to_serper`: host on `tavily` with a Serper key only → route open.
- `test_a_host_following_lane_closes_with_neither`: host on `tavily` with no Serper key → route closed.
- Flip `test_a_host_on_a_vendor_with_an_image_surface_opens_the_lane_on_that_vendors_key`: with the lane declared host-following (or the shipped deck), a Tavily-only host now closes; update its docstring to state the fallback rule.

- [ ] **Step 2: Run — FAIL** on the new tests, and the flipped one fails with `True`.

- [ ] **Step 3: Implement** `product_picture_vendor`'s `"host"` branch (scan every plugin slice's `imageSearch.provider`), `picture_search_ready`, and the `_lane_searches_images` delegation. Import `ppt_image_vendor` beside `image_search_vendor`.

- [ ] **Step 4: Run** the file. Expected: PASS.

---

### Task 8: The row payload carries route readiness

**Files:**
- Modify: `raven/rpc/methods/subagents.py` (`_rows`)
- Modify: `rpc-schema/openrpc.json` (`SubagentRow`)
- Regenerate: `ui-web/src/rpc/generated.ts` (`npm run gen --prefix ui-web`)
- Modify: `ui-web/src/features/extAgents/source.ts` (`extAgentRowOf` is an explicit field mapping; it must carry `routes`)
- Test: `tests/test_rpc_subagents.py`, `tests/test_rpc_contract_shapes.py`

**Interfaces:**
- Produces: every row carries `routes: [{"to": str, "image_search_ready": bool | None}]`; `None` when the route does not declare `image_search`, `True`/`False` from `picture_search_ready` otherwise. Always present (`[]` for rows with no routes) and added to the schema's `required`.

- [ ] **Step 1: Write the failing test**

Build a product row whose folder declares the Design→PPT route and `imageSearch.provider = "host"`; with a host on Tavily and no Serper key, assert the row's `routes == [{"to": "Raven-PPT", "image_search_ready": False}]`; with a Serper key, `True`.

- [ ] **Step 2: Run — FAIL** (no `routes` key).

- [ ] **Step 3: Implement** in `_rows`: load the current config once, build a `key_for` from `product_secret`, `config.effective_web_vendor_key` and the vendor env var, then compute the list. Add the schema property and regenerate the client.

- [ ] **Step 4: Run** the RPC tests and `npm run gen:check --prefix ui-web`. Expected: PASS.

---

### Task 9: The Search service settings note

**Files:**
- Modify: `ui-web/src/features/settings/source.ts`
- Modify: `ui-web/src/features/settings/pages/Tools.tsx`
- Modify: `i18n/messages.json`
- Test: `ui-web/src/features/settings/pages/Tools.test.tsx`, `tests/test_agent_tools_web_providers.py` (the existing vendor-table pin parses `source.ts` from Python; extend it for `PPT_IMAGE_VENDORS`)

**Interfaces:**
- Produces: `PPT_IMAGE_VENDORS: Set<string>` and `pptImageVendor(raw): string` (`""` when no vendor or Serper key resolves); a note under the Search service picker.

- [ ] **Step 1: Write the failing tests**

- `tests/test_agent_tools_web_providers.py`: parse `PPT_IMAGE_VENDORS` out of `ui-web/src/features/settings/source.ts` and hold it equal to the Python tuple.
- `Tools.test.tsx`: Tavily selected + Serper key → the info string is rendered; Tavily selected + no Serper key → the warning string is rendered; SerpApi selected + key → neither.

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement** the helpers and the note in the `web_search` panel. Add the `en`/`zh` strings to `i18n/messages.json` under `gui.settings.tools.*`.

- [ ] **Step 4: Run** `npm test --prefix ui-web -- src/features/settings`. Expected: PASS.

---

### Task 10: The Raven-Design card note

**Files:**
- Modify: `ui-web/src/features/extAgents/ExtAgentsPage.tsx` (`AgentSheet` body)
- Modify: `i18n/messages.json`
- Test: `ui-web/src/features/extAgents/ExtAgentsPage.test.tsx`

**Interfaces:**
- Consumes: the row's `routes` from Task 8.
- Produces: a warning line in the sheet when any route has `image_search_ready === false`.

- [ ] **Step 1: Write the failing test**

Mount the page with a row whose `routes` is `[{"to": "Raven-PPT", "image_search_ready": false}]`, open its sheet, assert the warning string is visible; with `true`, assert it is absent.

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement** the note (reuse the sheet's existing warning styling). Add the `en`/`zh` strings under `gui.agent.*`.

- [ ] **Step 4: Run** `npm test --prefix ui-web -- src/features/extAgents`. Expected: PASS.

---

### Task 11: Verification

- [ ] Python: run every touched suite

```
PYTHONPATH=$PWD /Evermind/sh_evermind/xuedizhan/Raven/.venv/bin/python -m pytest \
  tests/test_agent_loop_web_tools.py tests/test_agent_tools_web_providers.py \
  tests/test_ppt_engine_image_search.py tests/test_ppt_engine_plugin.py \
  tests/test_agents_ppt_launcher.py tests/test_subagent_routed_lane_readiness.py \
  tests/test_rpc_subagents.py tests/test_rpc_contract_shapes.py -q
```

- [ ] UI: `npm run gen:check --prefix ui-web && npm run type-check --prefix ui-web && npm test --prefix ui-web`
- [ ] Lint: `ruff check` + `ruff format --check` on the changed Python files; `npm run lint --prefix ui-web` if the project runs it locally.
- [ ] Run the `pr-review-patterns` pre-submit sweep over `git diff origin/main...HEAD` before any push.
- [ ] Report the results; wait for the owner's word before committing or pushing.
