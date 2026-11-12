# PPT image search follows the host's search choice

Date: 2026-11-11
Status: proposed

## Why

`ppt_image_search` speaks Serper only: the endpoint, the payload and the key
resolution are hardcoded in `plugins-dist/ppt-engine/raven_ppt/plugin/image_search.py`.
The rest of Raven's web tools have been vendor-selectable for a while, and the
host's own `image_search` already follows `tools.web.search.provider`. A
deployment that searches through SerpApi, Brave or Firecrawl still cannot build
decks, because the deck engine has no picture source.

## Decision

Asked and ruled by the owner: **fall back to Serper**. The deck engine uses the
host's selected search vendor when that vendor has a usable picture surface and
a key; otherwise it uses Serper's key; otherwise it has no picture tool.

## Goals

- `ppt_image_search` resolves its vendor the way the host's image search does,
  for these five vendors: `serper`, `serpapi`, `brave`, `firecrawl`, `serply`.
- One implementation of the five vendor requests, not a second copy inside the
  engine.
- The Settings > Tools > Search service panel says what the deck engine will do
  with the selected vendor: use it, fall back to Serper, or have no picture tool.
- Raven-Design's Agent Hub card says when the route to Raven-PPT is closed
  because no picture tool is available.
- The route to Raven-PPT uses the same rule as the engine, so the card and the
  dispatch cannot disagree.

## Non-goals

- No change to `web_search` / `web_fetch` vendor selection.
- No change to image generation (`image_generate`).
- No new HTTP dependency; no live vendor calls in tests.
- Tavily's degraded image payload (no dimensions, no source page) is not made
  to satisfy the deck filters.

## Anti-goals

- One vendor's key sent to another vendor's endpoint.
- Advertising `ppt_image_search` and failing every call.
- Opening the Design to PPT route when the engine has no picture tool.

## The design

### 1. Vendor surfaces

`raven/agent/tools/web.py`:

- Add `serply` to `IMAGE_SEARCH_VENDORS`: `serper`, `serpapi`, `brave`,
  `tavily`, `firecrawl`, `serply`. The host's `image_search` gains Serply and
  keeps Tavily's degraded form (unknown size allowed).
- Add a second, narrower tuple `PPT_IMAGE_SEARCH_VENDORS`: `serper`, `serpapi`,
  `brave`, `firecrawl`, `serply`. Tavily is excluded because its hits carry no
  dimensions and no source page, and the deck engine's filter drops them.
- Add `ppt_image_vendor(selected, key_for)`: the selected vendor when it is in
  `PPT_IMAGE_SEARCH_VENDORS` and a key resolves, else `serper`. The caller still
  checks that the returned vendor has a key; this mirrors `image_search_vendor`,
  which returns Serper as the fallback name.
- Serply request: `GET https://api.serply.io/v1/image`, params `q`, header
  `X-Api-Key`. Normalisation reads `image_results[].original_image.{src,width,height}`
  (width and height are strings; convert), `link.href`, `link.title`,
  `link.domain`. The Google-cached thumbnail at `image.src` is never used as
  the image URL.
- `ImageSearchTool` gains `require_dimensions` (default `False`). When true, a
  hit with no width or height is dropped, and the renderer never prints
  `size unknown`. The deck engine passes true; the host tool keeps its current
  behaviour.

### 2. The deck engine uses the host tool

`plugins-dist/ppt-engine/raven_ppt/plugin/image_search.py`:

- `PptImageSearchTool` subclasses `raven.agent.tools.web.ImageSearchTool`,
  overriding `name = "ppt_image_search"` and the deck-specific description.
  Parameters, filtering, rendering and per-vendor requests come from the host
  tool; `require_dimensions=True`.
- The engine no longer owns Serper request code.

`plugins-dist/ppt-engine/raven_ppt/plugin/__init__.py`:

- `image_search_vendor()` resolves the vendor on every call:
  - `imageSearch.provider` (new field) names a vendor: use it when it is a PPT
    vendor and a key resolves, else fall through to the host rule.
  - `imageSearch.provider == "host"` (the shipped value) or absent: read the
    live `tools.web.search.provider` through the existing `web_config` reader
    and call `ppt_image_vendor`.
- `image_search_key(vendor)` resolves four sources in order, live:
  1. the slice's `imageSearch.apiKey`, only when the vendor is `serper` (the
     legacy override);
  2. the host's vendor slot `tools.web.providers.<vendor>.apiKey`, through
     `web.vendor_key(vendor)`;
  3. Serper's pre-vendor `tools.web.search.apiKey` (inside `vendor_key`, and
     read directly when the host object has no `vendor_key`);
  4. the vendor's bare environment variable from `WEB_VENDOR_ENV_VARS` (for
     example `SERPER_API_KEY`), for every vendor.
  A key resolved for one vendor is never sent to another's endpoint.
- `image_search()` returns `None` when no vendor or no key resolves, so the
  tool is absent rather than failing per call.

`plugins-dist/ppt-engine/raven_ppt/plugin/config.py`:

- Read `imageSearch.provider` (string; `"host"` or one of the five). Reject an
  unknown name at config load, naming the accepted values.

### 3. Product folder and launcher

`agents/raven-ppt/config.json`:

- `plugins.config["ppt-engine"].imageSearch = {"provider": "host"}`.

`agents/raven-ppt/run.py`:

- Keep the existing Serper-key bridge into `imageSearch.apiKey` (it is now the
  legacy override, and only applies when the resolved vendor is Serper).
- No other change: the rendered config already carries every inherited vendor
  key under `tools.web.providers`, which the engine reads live.

### 4. Routing

`raven/agent/subagent/vendored_agents.py`:

- `product_picture_vendor()` learns the host-following declaration: a product
  whose plugin config says `imageSearch.provider == "host"` returns `"host"`
  (the product's pictures follow the host's selected vendor) instead of the
  folder's static `tools.web.search.provider`.
- `"host"` is a new return value; the function's docstring states the three
  answers: `""` (use the host rule as-is), `"host"` (the deck rule, with the
  PPT vendor set), a vendor name (the folder's own).

`raven/agent/loop/wiring.py`:

- `_lane_searches_images()` dispatches on that answer:
  - `"host"`: `ppt_image_vendor(self.web_search_provider, key_for)`.
  - a vendor name: that vendor.
  - `""`: `image_search_vendor(self.web_search_provider, key_for)`.
- The key check stays: a resolved vendor with no key closes the route.

### 5. The card's data

`raven/rpc/methods/subagents.py`:

- Each row gains `routes`: the route declarations with the picture-search
  readiness the router would apply, `[{to, image_search_ready}]`.
  `image_search_ready` is `true` or `false` from `picture_search_ready`, and
  `null` on a route that does not declare `image_search`. Computed from the
  current config and the product folder's own secrets, with the same
  `product_picture_vendor` / `ppt_image_vendor` / `image_search_vendor` helpers
  the router uses.
- `routes` is optional on the schema (`SubagentRow.routes?`) and defaults to an
  empty list; `_rows` writes a list on every row. `rpc-schema/openrpc.json`
  gains the property; `ui-web/src/rpc/generated.ts` is regenerated.

`ui-web`:

- `features/settings/source.ts` takes the deck's picture state from
  `settings.get`'s `picture_search`; it holds no copy of the vendor rule. The
  rule has only one home, the backend's `settings.get`
  (`raven/rpc/methods/console.py`, through `picture_search_status`), and
  `ui-web/src/rpc/fixtures/settings.ts` is its explicit offline mirror:
  `PPT_VENDORS` and `pictureSearchOf` stand in for the live answer.
  `tests/test_agent_tools_web_providers.py::test_the_offline_fixture_ppt_picture_set_matches_the_deck_tool`
  parses `PPT_VENDORS` out of that fixture and holds it equal to
  `PPT_IMAGE_SEARCH_VENDORS`.
- `features/settings/pages/Tools.tsx`: under the Search service vendor picker:
  - selected vendor is a PPT vendor with a key: no note.
  - selected vendor is not a PPT vendor, Serper key present: info note, "Deck
    generation cannot use this search API's pictures. It will use {name}
    instead."
  - no usable vendor: warning note, "Deck generation cannot use this search
    API's pictures and no Serper key is configured, so it cannot run."
- `features/extAgents/ExtAgentsPage.tsx`: the agent sheet shows the route
  warning for a row whose `routes` holds a not-ready `image_search` route.
- `i18n/messages.json`: the new strings, en and zh.

## Compatibility and risks

- The host's `image_search` gains Serply and keeps Tavily; the deck engine
  gains the five and never uses Tavily.
- A deployment on Tavily with a Serper key keeps working, on Serper; the UI says
  so. A deployment on Tavily without one sees the warning and the closed route.
- The engine's picture request drops from 20 results to 10 (the host tool's
  `max(count, 10)`); the tool still renders `count` (default 5). A deck that
  relied on the deeper candidate list may see fewer alternatives.
- `imageSearch.apiKey` remains supported for Serper only; a stale Serper key
  can never be sent to another vendor.
- Rollback: revert the change; the engine returns to Serper-only.

## Test plan

- `tests/test_agent_tools_web_providers.py`: Serply payload normalisation
  (documented shape), `ppt_image_vendor` precedence and Serper fallback, Tavily
  present in the host set and absent from the PPT set.
- `tests/test_agent_loop_web_tools.py`: `require_dimensions` drops unknown
  sizes and the renderer has no `size unknown` line for the deck tool.
- `tests/test_ppt_engine_plugin.py` (or the nearest engine test): the tool
  follows the host selection, falls back to Serper, is absent with no key, and
  ignores the slice key for a non-Serper vendor.
- `tests/test_agents_ppt_launcher.py`: the rendered config carries
  `imageSearch.provider = "host"` and the inherited vendor keys.
- `tests/test_subagent_routed_lane_readiness.py`: route open for an adapted
  vendor, Serper fallback, and closed with neither; the row payload carries the
  readiness.
- `ui-web` tests: Tools note states and agent sheet note. The offline fixture's
  vendor list is pinned on the Python side, in
  `test_the_offline_fixture_ppt_picture_set_matches_the_deck_tool`.

## Open items

None. The builder may choose names and file placement inside these boundaries.
