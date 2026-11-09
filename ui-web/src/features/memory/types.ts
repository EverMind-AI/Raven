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
  /* The backend the section is reading, as memory.backend names it; null when
     backend-driven memory is off. */
  backend?: string | null
  /* EverOS backends this install can read through, local first. A name absent
     here has no plugin behind it, and the switch draws that segment disabled. */
  available_backends?: string[]
  /* Whether the hosted backend has a key. False with the cloud selected is the
     one state the section replaces with its key-entry panel: the store answers
     401 there, and "cannot reach the service" would hide the fix. */
  cloud_key_set?: boolean
  /* Whether the local server answers. False with the local backend selected is
     the one state the section replaces with its start panel. */
  local_running?: boolean
  /* Whether the plugin starts the local server when it is down. False for a
     root the user manages: the start panel says whose step it is instead of
     offering Start. */
  local_startable?: boolean
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

/* What settings.set answers for a backend or key write. `warning` is the
   server's sentence for a write whose effect waits for something else; the
   memory switch applies live through `applyRuntime`, so it is normally absent
   here and the section has no sentence to show. */
export interface BackendSwitchReply {
  applied: boolean
  previous?: unknown
  warning?: string | null
}

/* What settings.everosCloud answers about the key just written: the health
   check's verdict and the sentence to show when it is not ``ok``. */
export interface CloudProbe {
  status?: string | null
  hint?: string | null
  api_key_set?: boolean
}

/* What memory.switchStatus answers: whether a live switch would wait for a
   turn in flight, and how many are running. */
export interface SwitchStatus {
  active: boolean
  turns: number
}

/* How a live apply reached the running loop: now, when the running turn ends,
   or not at all because the agent is shutting down. */
export type ApplyState = 'applied' | 'queued' | 'closed'

/* What memory.startLocal answers: whether the server now answers, the sentence
   to show when it does not, and how the start reached the loop -- the server
   comes up through the local backend the loop is pointed at. */
export interface LocalStartReply {
  ok: boolean
  running: boolean
  base_url: string
  detail?: string | null
  state?: ApplyState | null
}

/* What memory.applyRuntime answers. `queued` is not done yet: `detail` says
   when it will be. */
export interface ApplyRuntimeReply {
  ok: boolean
  backend?: string | null
  detail?: string | null
  state?: ApplyState | null
}

/* The DS.memory contract both the offline fixture library and the rpc
   source (live layer) implement. The island only ever talks to this. The
   fixture source has no engine behind it and rejects list with
   `{ down: true }`, which the island renders as the page's down note. */
export interface MemorySource {
  stats(): Promise<MemStats | null>
  list(req: MemListRequest): Promise<MemListResult>
  /* Writes memory.backend through settings.set. The memory section follows the
     file per read; the loop serving chat memory is pointed at it by
     `applyRuntime`, a live swap with no reload. */
  setBackend(backend: string): Promise<BackendSwitchReply>
  /* Writes the hosted backend's key through the same settings door. */
  setCloudKey(key: string): Promise<BackendSwitchReply>
  /* Asks the hosted backend whether the key on file works. Called after the
     write and before the section leaves its key panel. */
  probeCloud(): Promise<CloudProbe>
  /* Whether a switch would wait for a turn in flight. Asked before anything is
     written, so the section can put the question to the reader first. */
  switchStatus(): Promise<SwitchStatus>
  /* Points the running agent loop at the configured backend. A live reference
     swap, not a runtime rebuild: the document's socket stays up and the
     prompt layout is untouched. */
  applyRuntime(): Promise<ApplyRuntimeReply>
  /* Starts the local EverOS server from the section's start panel, by pointing
     the loop at a freshly built local backend whose own start brings it up. */
  startLocal(): Promise<LocalStartReply>
}
