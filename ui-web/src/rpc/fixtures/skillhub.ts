/* The skill hub, as much of it as the offline canvas carries.
 *
 * Ten rows behind `skillhub.search` and `skillhub.detail`, which is the market
 * the skills island draws. Installing one grows `ext.list`'s skill array the
 * same way the real hub does, so the installed shelf answers the change.
 */

import type { ExtFixture } from './ext'
import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

type Item = ResultOf<'skillhub.search'>['items'][number]

/* One catalogue row, in the few fields a hub entry is written with; the rest of
   the wire shape is filled in below, since a canned hub has no install counts
   or stars to report. */
interface Entry {
  id: string
  name: string
  category: string
  source: string
  source_url?: string
  quality_score: number
  tags: string[]
  description: string
}

const HUB_FIXTURE: Entry[] = [
  { id: 'sh-code-review', name: '代码审查清单', category: 'DEV', source: 'skillhub',
    quality_score: 0.92, tags: ['review', 'quality'],
    description: '按你团队的规矩审代码，而不是通用建议' },
  { id: 'sh-commit-doctor', name: 'commit-message-doctor', category: 'DEV', source: 'skillhub',
    quality_score: 0.88, tags: ['git', 'conventions'],
    description: '把改动整理成规范的提交信息，附带范围与理由' },
  { id: 'sh-sql-style', name: 'SQL 规范', category: 'DATA', source: 'skillhub',
    quality_score: 0.81, tags: ['sql'],
    description: '命名、缩进、禁用写法按你们的库来' },
  { id: 'sh-meeting-notes', name: '会议纪要格式', category: 'WRITING', source: 'skillhub',
    quality_score: 0.86, tags: ['notes'],
    description: '决议、责任人、截止时间三段式' },
  { id: 'sh-compete', name: '竞品调研方法', category: 'PRODUCTIVITY', source: 'skillhub',
    quality_score: 0.74, tags: ['research'],
    description: '一套做市场调研的步骤与输出格式' },
  { id: 'sh-pdf-extract', name: 'pdf-extractor', category: 'DOC-PROC', source: 'github/acme',
    source_url: 'https://example.com/pdf-extractor', quality_score: 0.79, tags: ['pdf', 'ocr'],
    description: '从 PDF 里抽结构化数据，表格也能对齐' },
  { id: 'sh-a11y-audit', name: 'a11y-audit', category: 'FRONTEND-UI', source: 'skillhub',
    quality_score: 0.83, tags: ['accessibility'],
    description: '按 WCAG 走查页面，输出可执行的修复清单' },
  { id: 'sh-incident-rb', name: 'incident-runbook', category: 'DEVOPS-INFRA', source: 'skillhub',
    quality_score: 0.9, tags: ['oncall'],
    description: '事故响应的分级、通报与复盘模板' },
  { id: 'sh-prompt-eval', name: 'prompt-eval', category: 'AI-ML', source: 'skillhub',
    quality_score: 0.77, tags: ['eval'],
    description: '给提示词跑一组回归用例，报告漂移' },
  { id: 'sh-threat-model', name: 'threat-model', category: 'SECURITY', source: 'skillhub',
    quality_score: 0.85, tags: ['security'],
    description: '按 STRIDE 给新功能画威胁模型' },
];

export interface SkillhubFixture {
  fixtures: Fixtures
}

export function createSkillhub(_env: FixtureEnv, ext: ExtFixture): SkillhubFixture {
  const item = (e: Entry): Item => {
    const installed = ext.skills.find((s) => s.name === e.name)
    return {
      id: e.id, skill_id: e.id, name: e.name, description: e.description,
      source: e.source, source_url: e.source_url || '', category: e.category,
      quality_score: e.quality_score, install_count: 0, github_star: 0,
      license: 'MIT', tags: e.tags, installed: !!installed,
      installed_name: installed ? installed.name : '',
    }
  }

  return {
    fixtures: {
      'skillhub.search': (p) => {
        const params = p as { query?: string; category?: string; page?: number; limit?: number }
        const q = (params.query || '').toLowerCase()
        const rows = HUB_FIXTURE.filter((x) => (!params.category || x.category === params.category)
          && (!q || `${x.name} ${x.description}`.toLowerCase().includes(q)))
        const limit = params.limit || 24
        const page = params.page || 1
        const from = (page - 1) * limit
        return {
          items: rows.slice(from, from + limit).map(item),
          total: rows.length, page, limit, base_url: 'https://example.invalid/skills',
        }
      },
      'skillhub.detail': (p) => {
        const e = HUB_FIXTURE.find((h) => h.id === p.id)
        if (!e) throw new Error(`no skill ${p.id}`)
        return {
          ...item(e),
          files: ['SKILL.md'], skill_md: `# ${e.name}\n\n${e.description}`,
          body_tokens: 1200, subscores: { utility: 8, robustness: 7, safety: 9, flags: [] },
        } as ResultOf<'skillhub.detail'>
      },
      'skillhub.install': (p) => {
        const e = HUB_FIXTURE.find((h) => h.id === p.id)
        if (e && !ext.skills.some((s) => s.name === e.name)) {
          ext.skills.push({ name: e.name, description: e.description, source: e.source,
            always: false, hub: true, hub_id: e.id })
        }
        return {
          name: e ? e.name : '', path: `~/.raven/skills/${e ? e.name : ''}`, files: ['SKILL.md'],
          skipped: [], replaced: false, size_bytes: 1200, install_count: 0,
        }
      },
      'skillhub.remove': (p) => {
        const at = ext.skills.findIndex((s) => s.name === p.name)
        if (at >= 0) ext.skills.splice(at, 1)
        return { removed: true, name: p.name }
      },
    },
  }
}
