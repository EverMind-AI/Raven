export interface CronRun {
  at: string
  ok: boolean
  note: string
  ms?: number
  sid?: string | null
}

export interface CronJob {
  id: string
  name: string
  what: string
  freq: 'hour' | 'day' | 'week' | 'once' | 'cron'
  at: string
  at_local?: string
  wd?: number
  on: boolean
  deliver: string
  when: string
  next: string
  runs: CronRun[]
  fresh?: boolean
  kind?: string
  every_ms?: number
  at_ms?: number
  tzv?: string
}

/* The edit copy: `blank` marks a refused empty save, `bad` carries the i18n
   key of a refused schedule. Both are cleared by the edit that fixes them. */
export interface CronDraft extends CronJob {
  blank?: boolean | null
  bad?: string | null
}

/* The DS.cron contract both the fixture source (demo shell) and the rpc
   source (live layer) implement. The island only ever talks to this. */
export interface CronSource {
  rows(): Promise<CronJob[]>
  toggle(j: CronJob): Promise<unknown>
  remove(j: CronJob): Promise<unknown>
  save(draft: CronDraft): Promise<CronJob>
  runs(j: CronJob): Promise<CronRun[]>
  runNow(j: CronJob): Promise<unknown>
  openRun(j: CronJob, run?: CronRun): Promise<unknown>
}
