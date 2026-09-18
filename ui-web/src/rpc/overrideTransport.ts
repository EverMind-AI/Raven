import type { ParamsOf, ResultOf, RpcMethod } from './generated'
import type { PushMethod } from './notifications'
import type {
  BinaryHandler,
  NotificationHandler,
  RpcTransport,
  StateListener,
} from './transport'

/* A handler for one method, given the call's params and the transport it sits
   in front of. Ignore `next` to answer instead of the gateway; call it to
   answer alongside -- which is what the tier wrapper needs, since the rung the
   conversation is on is the real gateway's to change and only the canvas's to
   remember. */
export type Override<M extends RpcMethod> = (
  params: ParamsOf<M>,
  next: () => Promise<ResultOf<M>>,
) => ResultOf<M> | Promise<ResultOf<M>>

export type Overrides = { [M in RpcMethod]?: Override<M> }

/**
 * One transport wearing another, with a handful of methods answered here.
 *
 * This is what `?onboard=demo` and `?desk-demo=1` are: a canned answer for the
 * few methods one screen reads, over whichever transport the page chose -- so
 * both entrances work on a live page (which is where `?desk-demo=1` has always
 * been applied) as well as on the offline one, and neither has to be a second
 * data source the rest of the page can see.
 */
export class OverrideTransport implements RpcTransport {
  /* Every handler the page registered through this canvas, so `push` below can
     reach them. Registered with the transport underneath as well: a frame the
     real gateway sends still has to arrive. */
  private readonly pushed = new Map<string, Set<NotificationHandler>>()

  constructor(
    private readonly inner: RpcTransport,
    private readonly overrides: Overrides,
  ) {}

  async call<M extends RpcMethod>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>> {
    // Widened to unknown before the call, for the reason FixtureTransport
    // widens: narrowing a function union distributed over every method entry
    // blows TS's complexity budget.
    const override: unknown = this.overrides[method]
    if (override === undefined) return await this.inner.call(method, params)
    const handler = override as (p: ParamsOf<M>, next: () => Promise<ResultOf<M>>) => ResultOf<M> | Promise<ResultOf<M>>
    return await handler(params, () => this.inner.call(method, params))
  }

  callUnchecked(method: string, params: Record<string, unknown>): Promise<unknown> {
    return this.inner.callUnchecked(method, params)
  }

  on(method: PushMethod, handler: NotificationHandler): () => void {
    const set = this.pushed.get(method) ?? new Set()
    set.add(handler)
    this.pushed.set(method, set)
    const off = this.inner.on(method, handler)
    return () => {
      set.delete(handler)
      off()
    }
  }

  /**
   * A frame the canvas itself pushes, to the handlers registered through it.
   *
   * A canvas that answers a call whose real effect arrives as a push has to
   * push one, or the offline route is a second route: the live gateway stamps
   * the frame and the page reads it through the handler `installPipeline`
   * registers, and this is the same door for a canned answer.
   */
  push(method: PushMethod, params: unknown): void {
    for (const handler of this.pushed.get(method) ?? []) handler(params)
  }

  binary(handler: BinaryHandler): () => void {
    return this.inner.binary(handler)
  }

  onState(listener: StateListener): () => void {
    return this.inner.onState(listener)
  }

  connect(): Promise<boolean> {
    return this.inner.connect()
  }

  close(): void {
    this.inner.close()
  }

  /** The transport underneath, for a test that wants to reach past the canvas. */
  get wrapped(): RpcTransport {
    return this.inner
  }
}
