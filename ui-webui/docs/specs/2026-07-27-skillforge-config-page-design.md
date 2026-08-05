# SkillForge configuration page — design

Date: 2026-07-27
Status: draft (pending spec review)

## 1. Problem

MIGRATION.md lists a "skills" page as a remaining P4 Raven-concept config page
(`MIGRATION.md:62`, `:119`: "same pattern, channels/cron/skills/memory") but
gives no design — neither MIGRATION nor any spec/plan says what the page
configures. Today SkillForge can only be tuned by hand-editing the `skillForge`
block in `~/.raven/config.json`; there is no web surface for it, and the
existing AgentScope pages configure unrelated concepts.

"skills" in Raven means **SkillForge**: the retrieval/injection pipeline that
selects skills for the agent. Its config lives in the `skillForge` block; `raven
skill list/get` is a read-only view of the registry.

## 2. Goals

A compact config page — the same shape as the shipped `channels`/`cron` pages —
that lets a user, from the web UI:

1. Toggle SkillForge on/off and tune its retrieval sources.
2. Manage locally-mounted skill directories.
3. See (read-only) which skills SkillForge currently resolves.

Non-goals: expert-only knobs (`embedding_model`, `scan_max_depth`,
`evolve_model`, reranker internals) stay hand-edited; live hot-apply of
SkillForge config (see D1).

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **`restart_required`, not hot-apply.** `set` writes config and returns `restart_required: true`; the UI shows a "restart gateway to apply" banner. | `skillForge` config is read once when the `AgentLoop` is constructed (`raven/agent/loop/main.py:257,368` build the SkillForgeRouter); there is no `apply_*` hook like `apply_third_party_subagents`. Matches the `channels` page. Hot-apply would need a router-rebuild hook — deferred (YAGNI). |
| D2 | **Whitelist the exposed fields.** Only `enabled`, `router.weights.{local,everos,hub}`, `router.hub.{endpoint,apiKey,minSafety,timeoutS}`, `everos.enabled`, `local_dirs[]`. Writes patch only these keys; all other `skillForge` keys are preserved verbatim. | Keeps the page focused; avoids letting the UI corrupt advanced/derived fields. |
| D3 | **Read-only skill list is a separate RPC** (`raven.skills.list`), reusing the registry read behind `raven skill list`. It never participates in writes; it renders at the bottom of the same page. | Shows the effect of the config without coupling read and write paths. |

## 4. Architecture (four layers, same as channels/cron)

```
frontend pages/raven-skills/  --REST-->  service/raven_config_routes.py
  --gateway WS RPC-->  raven/web_rpc/methods_config.py
  -->  raven/config/update_skills.py  (validate + atomic write skillForge block)
```

## 5. Components

### 5.1 `raven/config/update_skills.py` (new)
- `get_skillforge() -> dict` — read the whitelisted fields (camelCase wire form).
- `set_skillforge(fields: dict) -> None` — validate, then atomically write only
  the whitelisted keys into the `skillForge` block of `~/.raven/config.json`,
  preserving every other key (read-modify-write, temp-file + `os.replace`).
- `list_skills() -> list[dict]` — reuse the registry read behind
  `raven/cli/skill_commands.py:list` (name, source, description).

### 5.2 `raven/web_rpc/methods_config.py`
- `raven.skills.get` -> `{skillforge: {...whitelist...}}`
- `raven.skills.set` -> validates + writes; returns `{ok: true, restart_required: true}`
- `raven.skills.list` -> `{skills: [{name, source, description}, ...]}`

### 5.3 `ui-webui/service/raven_config_routes.py`
- `GET /raven/skills` -> `raven.skills.get`
- `PUT /raven/skills` -> `raven.skills.set` (400 on validation error)
- `GET /raven/skills/available` -> `raven.skills.list`

### 5.4 Frontend `pages/raven-skills/`
One screen, top to bottom:
- Restart-required banner (shown after a successful save).
- Master toggle `enabled`.
- Retrieval sources: three weight inputs `local` / `everos` / `hub`.
- Hub card: `endpoint`, `apiKey` (secret), `minSafety`, `timeoutS`.
- `everos.enabled` toggle.
- `local_dirs` editable list (add/remove rows; list order = priority).
- Read-only skill table (from `/raven/skills/available`): name, source, description.

Plus: `src/api/ravenConfig.ts` client methods; sidebar entry in `AppSidebar.tsx`;
i18n strings in `src/i18n/locales/{en,zh}.json` (targeted edits, no full re-dump).

## 6. Validation / errors
- Weights: numbers in `[0, 1]`.
- Hub `endpoint`: non-empty, `http(s)://` URL when present.
- `local_dirs`: each entry has a non-empty `path`; existence is NOT required
  (allow configure-before-create). Order preserved as priority.
- Invalid input -> backend raises -> RPC error -> service returns 400 ->
  frontend shows inline error; nothing is written on failure.

## 7. Testing
- `tests/test_update_skills.py` (new, mirrors `test_update_subagents.py`):
  whitelist round-trip camelCase, preserve-other-keys, validation rejects,
  atomic write.
- Extend `tests/test_web_rpc_config.py`: the three `raven.skills.*` methods,
  including `set` returning `restart_required`.
- Frontend gate: `pnpm -C ui-webui/frontend lint` (0 errors) + `build`.

## 8. Out of scope (first version)
- Hot-apply of SkillForge config (D1).
- Advanced retrieval knobs (embedding model, scan depth, reranker, evolve model).
- Editing individual skills / the registry (list is read-only).
