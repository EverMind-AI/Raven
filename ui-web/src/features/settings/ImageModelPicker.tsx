/** Image generation presets and custom model selection, saved as one model/quality pair. */
import { useState } from 'react'

import { t } from '../../shell/bridge'
import * as store from './store'

import type { JSX } from 'react'

const PRESETS = [
  { model: 'openai/gpt-image-2.5-sunburst', quality: '', label: 'GPT Image 2.5 Sunburst' },
  { model: 'openai/gpt-image-2.5-flare', quality: '', label: 'GPT Image 2.5 Flare' },
  { model: 'openai/gpt-image-2', quality: 'low', label: 'gpt-image-2 · low' },
  { model: 'openai/gpt-image-2', quality: 'medium', label: 'gpt-image-2 · medium' },
  { model: 'openai/gpt-image-2', quality: 'high', label: 'gpt-image-2 · high' },
  { model: 'bytedance-seed/seedream-5-0-lite', quality: '', label: 'seedream-5-0-lite' },
  { model: 'bytedance-seed/seedream-5-0-pro', quality: '', label: 'seedream-5-0-pro' },
  { model: 'qwen/qwen-image-3', quality: '', label: 'qwen-image-3' },
  { model: 'qwen/qwen-image-3-pro', quality: '', label: 'qwen-image-3-pro' },
  { model: 'x-ai/grok-imagine-image-2.0', quality: '', label: 'grok-imagine-image-2.0' },
]

export function ImageModelPicker({
  model,
  quality,
  say,
}: {
  model: string
  quality: string | undefined
  say: () => void
}): JSX.Element {
  const effectiveModel = model || 'openai/gpt-image-2.5-sunburst'
  const effectiveQuality = effectiveModel.includes('gpt-image') ? (quality ?? (effectiveModel === 'openai/gpt-image-2' ? 'medium' : '')) : ''
  const preset = PRESETS.findIndex((p) => p.model === effectiveModel && p.quality === effectiveQuality)
  const [custom, setCustom] = useState(false)
  const [draft, setDraft] = useState(effectiveModel)
  const [busy, setBusy] = useState(false)
  const isCustom = custom || preset < 0
  const save = async (nextModel: string, nextQuality: string): Promise<void> => {
    if (busy || !nextModel.trim()) return
    setBusy(true)
    try {
      const result = await store.write('tools.media.image', { model: nextModel.trim(), quality: nextQuality })
      if (result === 'notlive') say()
      if (result === 'ok') setCustom(false)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="tkrow">
      <select
        className="mlend"
        aria-label={t('gui.caps.image_model')}
        value={isCustom ? 'custom' : String(preset)}
        disabled={busy}
        onChange={(e) => {
          const value = e.currentTarget.value
          if (value === 'custom') {
            setDraft(effectiveModel)
            setCustom(true)
          } else {
            const pick = PRESETS[Number(value)]!
            void save(pick.model, pick.quality)
          }
        }}
      >
        {PRESETS.map((p, i) => (
          <option key={i} value={String(i)}>
            {p.label}
          </option>
        ))}
        <option value="custom">{t('gui.caps.custom_model')}</option>
      </select>
      {isCustom && (
        <>
          <input
            aria-label={t('gui.caps.custom_model')}
            placeholder="provider/model-id"
            maxLength={500}
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void save(draft, '')
            }}
          />
          <button className="mini" disabled={busy || !draft.trim()} onClick={() => void save(draft, '')}>
            {t('gui.save')}
          </button>
        </>
      )}
    </div>
  )
}
