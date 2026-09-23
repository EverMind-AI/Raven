/**
 * WebSocket server for Python-Node.js bridge communication.
 * Security: binds to 127.0.0.1 only; requires BRIDGE_TOKEN auth; rejects browser Origin headers.
 */

import { WebSocketServer, WebSocket } from 'ws'

import { WhatsAppClient } from './whatsapp.js'

interface SendCommand {
  type: 'send'
  to: string
  text: string
}

interface SendMediaCommand {
  type: 'send_media'
  to: string
  filePath: string
  mimetype: string
  caption?: string
  fileName?: string
}

type BridgeCommand = SendCommand | SendMediaCommand

interface BridgeMessage {
  type: 'message' | 'status' | 'qr' | 'error'
  [key: string]: unknown
}

export class BridgeServer {
  private wss: WebSocketServer | null = null
  private wa: WhatsAppClient | null = null
  private clients: Set<WebSocket> = new Set()
  private lastQr: string | null = null
  private lastStatus: string | null = null

  constructor(
    private port: number,
    private authDir: string,
    private token: string
  ) {}

  async start(): Promise<void> {
    if (!this.token.trim()) {
      throw new Error('BRIDGE_TOKEN is required')
    }

    // Bind to localhost only — never expose to external network
    this.wss = new WebSocketServer({
      host: '127.0.0.1',
      port: this.port,
      verifyClient: (info, done) => {
        const origin = info.origin || info.req.headers.origin
        if (origin) {
          console.warn(`Rejected WebSocket connection with Origin header: ${origin}`)
          done(false, 403, 'Browser-originated WebSocket connections are not allowed')
          return
        }
        done(true)
      }
    })
    console.log(`🌉 Bridge server listening on ws://127.0.0.1:${this.port}`)
    console.log('🔒 Token authentication enabled')

    this.wa = new WhatsAppClient({
      authDir: this.authDir,
      onMessage: msg => this.broadcast({ type: 'message', ...msg }),
      onQR: qr => {
        this.lastQr = qr
        this.broadcast({ type: 'qr', qr })
      },
      onStatus: status => {
        this.lastStatus = status
        if (status === 'connected') {
          this.lastQr = null
        }
        this.broadcast({ type: 'status', status })
      }
    })

    this.wss.on('connection', ws => {
      // Require auth handshake as first message
      const timeout = setTimeout(() => ws.close(4001, 'Auth timeout'), 5000)
      ws.once('message', data => {
        clearTimeout(timeout)
        try {
          const msg = JSON.parse(data.toString())
          if (msg.type === 'auth' && msg.token === this.token) {
            console.log('🔗 Python client authenticated')
            this.setupClient(ws)
          } else {
            ws.close(4003, 'Invalid token')
          }
        } catch {
          ws.close(4003, 'Invalid auth message')
        }
      })
    })

    await this.wa.connect()
  }

  private setupClient(ws: WebSocket): void {
    this.clients.add(ws)

    // Baileys emits a QR (and a pairing status) once, roughly every 60s. A client
    // that attaches between two of those would otherwise show nothing until the
    // next one, so replay what the bridge knows right now.
    if (this.lastStatus) {
      ws.send(JSON.stringify({ type: 'status', status: this.lastStatus }))
    }
    if (this.lastQr) {
      ws.send(JSON.stringify({ type: 'qr', qr: this.lastQr }))
    }

    ws.on('message', async data => {
      try {
        const cmd = JSON.parse(data.toString()) as BridgeCommand
        await this.handleCommand(cmd)
        ws.send(JSON.stringify({ type: 'sent', to: cmd.to }))
      } catch (error) {
        console.error('Error handling command:', error)
        ws.send(JSON.stringify({ type: 'error', error: String(error) }))
      }
    })

    ws.on('close', () => {
      console.log('🔌 Python client disconnected')
      this.clients.delete(ws)
    })

    ws.on('error', error => {
      console.error('WebSocket error:', error)
      this.clients.delete(ws)
    })
  }

  private async handleCommand(cmd: BridgeCommand): Promise<void> {
    if (!this.wa) {
      return
    }

    if (cmd.type === 'send') {
      await this.wa.sendMessage(cmd.to, cmd.text)
    } else if (cmd.type === 'send_media') {
      await this.wa.sendMedia(cmd.to, cmd.filePath, cmd.mimetype, cmd.caption, cmd.fileName)
    }
  }

  private broadcast(msg: BridgeMessage): void {
    const data = JSON.stringify(msg)
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(data)
      }
    }
  }

  async stop(): Promise<void> {
    for (const client of this.clients) {
      client.close()
    }
    this.clients.clear()

    if (this.wss) {
      this.wss.close()
      this.wss = null
    }

    if (this.wa) {
      await this.wa.disconnect()
      this.wa = null
    }
  }
}
