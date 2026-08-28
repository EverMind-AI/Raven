import { spawn } from 'node:child_process'

export interface LaunchResult {
  code: null | number
  error?: string
}

// Set by `raven tui` to the entry that started this session. The commands run
// here write credentials, and PATH can name a different install than the one
// this TUI belongs to -- which would write them where it will not look.
const resolveRavenBin = () => process.env.RAVEN_BIN?.trim() || 'raven'

export const launchRavenCommand = (args: string[]): Promise<LaunchResult> =>
  new Promise(resolve => {
    const child = spawn(resolveRavenBin(), args, { stdio: 'inherit' })

    child.on('error', err => resolve({ code: null, error: err.message }))
    child.on('exit', code => resolve({ code }))
  })
