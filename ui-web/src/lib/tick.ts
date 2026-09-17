/* One self-stopping clock, for anything drawing an elapsed time that moves.
 *
 * Repaints once a second while `on`, and gives up after an hour: a number that
 * has been counting that long is a run nobody is still watching tick, and the
 * interval outlives every pane it was started for otherwise.
 *
 * Returns the elapsed milliseconds rather than a formatted string, because the
 * spelling is `lib/duration.ts`'s and a caller may want to compare before it
 * renders -- the transcript withholds the number below a second and shows an
 * ellipsis instead.
 */
import { useEffect, useState } from 'react'

export function useTick(on: boolean, t0: number): number {
  const [, setN] = useState(0)
  useEffect(() => {
    if (!on) return
    const tick = setInterval(() => {
      setN((n) => n + 1)
      if (Date.now() - t0 > 3600e3) clearInterval(tick)
    }, 1000)
    return () => clearInterval(tick)
  }, [on, t0])
  return on ? Date.now() - t0 : 0
}
