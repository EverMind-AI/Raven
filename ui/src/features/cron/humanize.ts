import { t } from '../../shell/bridge'

import type { CronJob } from './types'

/* What a schedule means, in words. The raw five-field expression stays in
   the editor for whoever writes one, but nobody should have to read it. */
export function cronExprHuman(expr: string): string {
  const p = String(expr || '')
    .trim()
    .split(/\s+/)
  const raw = `cron ${expr}`
  if (p.length !== 5) return raw
  const [m, h, dom, mon, dow] = p as [string, string, string, string, string]
  if (dom !== '*' || mon !== '*') return raw
  const pad = (n: string | number) => String(n).padStart(2, '0')
  let day: string
  if (dow === '*') day = t('gui.cron.h.daily')
  else if (dow === '1-5') day = t('gui.cron.h.weekdays')
  else if (dow === '0,6' || dow === '6,0') day = t('gui.cron.h.weekend')
  else if (/^[0-6](,[0-6])*$/.test(dow)) {
    day = dow
      .split(',')
      .map((d) => t('gui.cron.h.dow' + d))
      .join('、')
  } else return raw
  let time: string
  const mN = /^\d+$/.test(m)
  const hN = /^\d+$/.test(h)
  const hRange = h.match(/^(\d+)-(\d+)$/)
  const mStep = m.match(/^\*\/(\d+)$/)
  const hStep = h.match(/^\*\/(\d+)$/)
  const mList = /^\d+(,\d+)+$/.test(m) ? m.split(',').map(Number) : null
  if (mN && hN) time = `${pad(h)}:${pad(m)}`
  else if (mN && /^\d+(,\d+)+$/.test(h)) {
    time = h
      .split(',')
      .map((x) => `${pad(x)}:${pad(m)}`)
      .join('、')
  } else if (hStep && mN) time = t('gui.cron.h.every_h', { n: hStep[1]! })
  else if (mStep && h === '*') time = t('gui.cron.h.every_m', { n: mStep[1]! })
  else {
    const span = hRange
      ? `${pad(hRange[1]!)}:00–${pad(hRange[2]!)}:59`
      : h === '*'
        ? t('gui.cron.h.allday')
        : null
    let mm: string | null = null
    if (mList) {
      mm =
        mList.length === 2 && mList[0] === 0 && mList[1] === 30
          ? t('gui.cron.h.half')
          : t('gui.cron.h.at_min', { m: mList.map(pad).join('/') })
    } else if (mStep) mm = t('gui.cron.h.every_m', { n: mStep[1]! })
    else if (mN && hRange) mm = t('gui.cron.h.at_min', { m: pad(m) })
    if (span == null || mm == null) return raw
    time = `${span} ${mm}`
  }
  return `${day} ${time}`
}

/* Demo rows carry a hand-written `when`; a real cron expr goes through the
   translator. The live layer routes every kind through here via cronToRow. */
export function cronWhen(j: Pick<CronJob, 'freq' | 'at' | 'when'>): string {
  return j.freq === 'cron' && j.at ? cronExprHuman(j.at) : j.when
}
