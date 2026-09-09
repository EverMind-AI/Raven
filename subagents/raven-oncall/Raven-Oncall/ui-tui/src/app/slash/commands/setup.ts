import type { SlashCommand } from '../types.js'

import { launchRavenCommand } from '../../../lib/externalCli.js'
import { suspendForHandoff } from '../../../lib/handoff.js'
import { runExternalSetup } from '../../setupHandoff.js'

export const setupCommands: SlashCommand[] = [
  {
    // `raven setup` has never existed: the wizard is `raven onboard`, so this
    // command spawned a name the CLI rejects and reported the exit code.
    help: 'run full setup wizard (launches `raven onboard`)',
    name: 'setup',
    // Still kept out of the palette (the TUI has its own onboarding gate); this
    // change only stops the command spawning a name the CLI never had.
    supported: false,
    run: (arg, ctx) =>
      void runExternalSetup({
        args: ['onboard', ...arg.split(/\s+/).filter(Boolean)],
        ctx,
        done: 'setup complete — starting session…',
        launcher: launchRavenCommand,
        suspend: suspendForHandoff
      })
  }
]
