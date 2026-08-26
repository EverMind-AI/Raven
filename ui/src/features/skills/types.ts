/* A hub search row, the shape skillhub.search answers. The fixture source
   mirrors it, minus the fields the demo has no use for. */
export interface HubItem {
  id: string
  name: string
  description?: string
  category?: string
  source?: string
  source_url?: string
  quality_score?: number
  tags?: string[]
  installed?: boolean
  installed_name?: string
}

export interface HubSubscores {
  utility?: number
  robustness?: number
  safety?: number
  flags?: string[]
}

/* One hub entry in full, the shape skillhub.detail answers. */
export interface HubDetail {
  description?: string
  category?: string
  source?: string
  license?: string
  quality_score?: number | null
  subscores?: HubSubscores
  tags?: string[]
  files?: string[]
  body_tokens?: number
  skill_md?: string
}

/* An installed row. Each source owns its list: ext.list fills the live one,
   while the demo source owns its fixture list. `hub` marks a market install
   (removable); a
   hand-written or builtin skill has no hub entry and cannot be removed. */
export interface InstalledSkill {
  id: string
  name: string
  one?: string
  src?: string
  reach?: string
  hub?: boolean
  hubId?: string
}

export interface HubSearchQuery {
  query: string
  category: string
  page: number
  limit: number
}

export interface HubSearchResult {
  items?: HubItem[]
  total?: number
}

/* The DS.skills contract both the fixture source (demo shell) and the rpc
   source (live layer) implement. The island only ever talks to this.
   install/remove toast their own failures and reject `{handled: true}`;
   search rejections come back raw, because the island renders those in
   place with a retry instead of toasting. */
export interface SkillsSource {
  search(q: HubSearchQuery): Promise<HubSearchResult>
  detail(id: string): Promise<HubDetail>
  install(id: string): Promise<unknown>
  remove(name: string): Promise<unknown>
  installed(): InstalledSkill[]
  /* Whether `installed()` has ever been filled. Optional so a source that
     cannot be empty-by-failure -- the fixtures -- need not answer it. */
  loaded?(): boolean
}
