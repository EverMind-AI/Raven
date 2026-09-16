export type MemKind = 'episode' | 'profile' | 'agent_case' | 'agent_skill'

/* One memory row as memory.list answers it. Which optional fields are
   filled depends on the kind: profiles carry profile_data, cases carry
   key_insight and quality_score, skills carry the two meters. */
export interface MemItem {
  id: string
  kind: MemKind
  subject?: string
  summary?: string
  body?: string
  key_insight?: string
  timestamp?: string
  session_id?: string
  quality_score?: number | null
  confidence?: number | null
  maturity_score?: number | null
  score?: number
  profile_data?: Record<string, unknown>
}

export interface MemStats {
  episodes: number
  profiles: number
  agent_cases: number
  agent_skills: number
  /* Why the counts are all zero, when that is not a failure: no memory
     plugin installed, or one installed that memory.backend does not name. */
  note?: string | null
}

export interface MemListRequest {
  kind: MemKind
  page: number
  page_size: number
  q: string | null
}

export interface MemListResult {
  items?: MemItem[]
  total?: number
  /* Same note MemStats carries: an empty page with a reason rather than an
     error with a retry button that cannot help. */
  note?: string | null
}

/* The DS.memory contract both the offline fixture library and the rpc
   source (live layer) implement. The island only ever talks to this. The
   fixture source has no engine behind it and rejects list with
   `{ down: true }`, which the island renders as the page's down note. */
export interface MemorySource {
  stats(): Promise<MemStats | null>
  list(req: MemListRequest): Promise<MemListResult>
  remove(it: MemItem): Promise<unknown>
}
