import { useStore } from '@nanostores/react'

import type { ChatStreamRpcClient } from './app/chatStream.js'
import { GatewayProvider } from './app/gatewayContext.js'
import { $uiState } from './app/uiStore.js'
import { useMainApp } from './app/useMainApp.js'
import { AppLayout } from './components/appLayout.js'
import type { GatewayClient } from './gatewayClientStub.js'

export function App({ gw, rpcClient }: { gw: GatewayClient; rpcClient?: ChatStreamRpcClient }) {
  const { appActions, appComposer, appProgress, appStatus, appTranscript, gateway } = useMainApp(gw, rpcClient)
  const { mouseTracking } = useStore($uiState)

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
