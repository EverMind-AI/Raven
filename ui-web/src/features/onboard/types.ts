/* The first-run setup flow's DataSource contract. */

import type { ModelTagFacts } from '../../shell/model-tags'

export interface OnboardProvider {
  slug: string
  name?: string
  homepage?: string
  auth_type: string
  authenticated: boolean
  needs_api_base?: boolean
  /* Whether this provider takes a key at all -- false for an OAuth flow and
     for a local deployment reached by address alone. */
  accepts_api_key?: boolean
  api_base?: string
  default_api_base?: string
  models: string[]
  /* Straight off `model.options`, keyed by the ids in `models`. The first
     model a person ever picks is the one they know least about, so the icons
     belong here at least as much as in the picker. */
  model_labels?: Record<string, ModelTagFacts & { label?: string; description?: string }>
}

export interface OnboardOptions {
  model?: string
  provider?: string
  providers: OnboardProvider[]
}

export interface OnboardSource {
  options(): Promise<OnboardOptions>
  saveKey(slug: string, apiKey: string, apiBase: string): Promise<{ provider?: OnboardProvider }>
  setModel(model: string, provider: string): Promise<unknown>
  recheck(): Promise<boolean>
}
