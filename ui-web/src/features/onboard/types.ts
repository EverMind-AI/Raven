/* The first-run setup flow's DataSource contract. */

export interface OnboardProvider {
  slug: string
  name?: string
  auth_type: string
  authenticated: boolean
  needs_api_base?: boolean
  models: string[]
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
