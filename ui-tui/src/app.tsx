import { oscColor, type StdinProps, useStdin } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import type { ChatStreamRpcClient } from './app/chatStream.js'
import { GatewayProvider } from './app/gatewayContext.js'
import { $uiState, applyTerminalBackground } from './app/uiStore.js'
import { useMainApp } from './app/useMainApp.js'
import { AppLayout } from './components/appLayout.js'
import type { GatewayClient } from './gatewayClientStub.js'

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

export function App({ gw, rpcClient }: { gw: GatewayClient; rpcClient?: ChatStreamRpcClient }) {
  const { appActions, appComposer, appProgress, appStatus, appTranscript, gateway } = useMainApp(gw, rpcClient)
  const { mouseTracking } = useStore($uiState)

  useTerminalBackgroundProbe()

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
