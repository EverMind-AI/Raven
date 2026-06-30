import { activeColorTier, oscColor, type StdinProps, useStdin } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import type { ChatStreamRpcClient } from './app/chatStream.js'
import { GatewayProvider } from './app/gatewayContext.js'
import { $uiState, $uiTheme, applyTerminalBackground } from './app/uiStore.js'
import { useMainApp } from './app/useMainApp.js'
import { AppLayout } from './components/appLayout.js'
import type { GatewayClient } from './gatewayClientStub.js'
import { cursorColorHex } from './theme.js'

// Ask the terminal for its real background color (OSC 11) so light/dark
// detection doesn't have to guess from TERM_PROGRAM. The reply rides back on
// stdin, but ink's keypress parser recognizes OSC responses and routes them to
// the querier — it never emits them as input, so the `rgb:...` payload can't
// leak into the composer. The DA1 sentinel from flush() bounds the wait when
// the terminal ignores the query (it stays on the env-based scheme).
function useTerminalBackgroundProbe() {
  // `as StdinProps`: useStdin()'s inferred return collapses `querier` to
  // `unknown` across the package boundary (its internal `.js` import of
  // StdinContext resolves differently than the public `.ts` export path). The
  // public StdinProps type carries the correct `TerminalQuerier | null`.
  const { querier } = useStdin() as StdinProps

  useEffect(() => {
    if (!querier) {
      return
    }

    let cancelled = false

    void Promise.all([querier.send(oscColor(11)), querier.flush()]).then(([reply]) => {
      if (!cancelled && reply) {
        applyTerminalBackground(reply.data)
      }
    })

    return () => {
      cancelled = true
    }
  }, [querier])
}

// Paint the terminal's hardware cursor with the theme's primary color. On a
// focused TTY the input cursor is the terminal's own cursor (positioned via
// useDeclaredCursor), so it can only be recolored with OSC 12 — a text SGR
// can't touch it. Re-emitted when the OSC 11 probe flips the scheme (light
// #B87900 / dark #fbe23f); reset via OSC 112 on unmount, and also in
// resetTerminalModes() so signal/crash exits restore the user's cursor color.
const setCursorColorSeq = (hex: string) => `]12;${hex}`
const RESET_CURSOR_COLOR_SEQ = ']112'

function useHardwareCursorColor() {
  const hex = cursorColorHex(useStore($uiTheme))

  useEffect(() => {
    // tier 0 = colors disabled (NO_COLOR / FORCE_COLOR=0) — leave the cursor alone.
    if (activeColorTier() === 0) {
      return
    }

    process.stdout.write(setCursorColorSeq(hex))
  }, [hex])

  // Reset only on unmount (not on every re-emit) so a scheme flip doesn't flash
  // the default cursor color between writes.
  useEffect(
    () => () => {
      if (activeColorTier() !== 0) {
        process.stdout.write(RESET_CURSOR_COLOR_SEQ)
      }
    },
    []
  )
}

export function App({ gw, rpcClient }: { gw: GatewayClient; rpcClient?: ChatStreamRpcClient }) {
  const { appActions, appComposer, appProgress, appStatus, appTranscript, gateway } = useMainApp(gw, rpcClient)
  const { mouseTracking } = useStore($uiState)

  useTerminalBackgroundProbe()
  useHardwareCursorColor()

  return (
    <GatewayProvider value={gateway}>
      <AppLayout
        actions={appActions}
        composer={appComposer}
        mouseTracking={mouseTracking}
        progress={appProgress}
        status={appStatus}
        transcript={appTranscript}
      />
    </GatewayProvider>
  )
}
