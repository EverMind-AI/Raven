/* Agent branding for the roster, on the same terms as ProviderMark.tsx. */

import type { JSX } from 'react'

/* Keyed by preset, never by the row's name. What a row draws is a fact about
   the package behind it, and a configured row's name is the user's to change,
   so a renamed row keeps its brand and a hand-written row that merely spells
   itself `codex` gets none -- the boundary ACP_REGISTRY_INSTALL_HINTS draws for
   the same reason.

   Filenames are upstream's, verbatim, so one `cmp` against
   @lobehub/icons-static-svg settles provenance for every file here on the
   terms LICENSES/README.md already records for the provider marks. The one
   exception is miromind.svg, which that package does not carry and which is
   MiroMind's own favicon, also verbatim.

   `tone` says what the file is made of, which is a fact about the file rather
   than a preference -- tests/test_ui_agent_marks.py derives each one from the
   shapes in the SVG it names. Three answers, because an <img> resolves
   `currentColor` against its own document rather than the page and so renders
   it black:

   - absent: every shape carries its own fill.
   - `mono`: every shape is `currentColor`, so the file draws black.
   - `hybrid`: some shapes are, some are not. Only qoder: brand green beside one
     tone the vendor means to follow the page's text.

   No rule reads it back any more. Every mark sits on a light plate in both
   themes (page.css, `.agent-mark`), because recolouring a mark for a dark
   ground is a per-file guess: inverting a mono illustration gives its negative
   and rotating qoder's hue back lands on a different green. The attribute
   stays as the file's fact, for the test that ties each tone to its file.

   A `mirasim_` row is the agent of the same name run through mirasim, so it
   wears that agent's mark; the row's name already says the route. DSH (the
   DeepSeek Harness) publishes no mark of its own and wears DeepSeek's, whose
   models it is built around. */
const MARKS: Record<string, { file: string; tone?: 'mono' | 'hybrid' }> = {
  claude_code: { file: 'claudecode-color' },
  codebuddy: { file: 'codebuddy-color' },
  codex: { file: 'codex-color' },
  github_copilot: { file: 'copilot-color' },
  grok: { file: 'grok', tone: 'mono' },
  hermes: { file: 'hermesagent', tone: 'mono' },
  kimi_code: { file: 'kimi', tone: 'mono' },
  mirasim_antigravity: { file: 'antigravity-color' },
  mirasim_claude: { file: 'claudecode-color' },
  mirasim_codex: { file: 'codex-color' },
  mirasim_dsh: { file: 'deepseek-color' },
  mirasim_grok: { file: 'grok', tone: 'mono' },
  mirasim_kimi: { file: 'kimi', tone: 'mono' },
  mirasim_pi: { file: 'pi', tone: 'mono' },
  mirasim_qwen: { file: 'qwen-color' },
  mirasim_zcode: { file: 'zai', tone: 'mono' },
  mirothinker: { file: 'miromind' },
  openclaw: { file: 'openclaw-color' },
  opencode: { file: 'opencode', tone: 'mono' },
  pi: { file: 'pi', tone: 'mono' },
  qoder: { file: 'qoder-color', tone: 'hybrid' },
  qwen_code: { file: 'qwen-color' },
}

/* Raven's own agents, which carry no preset because there is no third-party
   package behind them to name: `builtin` is this process and `vendored` is a
   Discovered agent under `agents/`. Each is a fact the server asserts about what
   the row runs, exactly as a preset is, so it picks a mark on the same terms --
   and a hand-written row that merely spells itself `Raven-Code` still gets
   none, for the reason the table above is not keyed by name either.

   raven.svg is this project's own mark rather than upstream's, so the `cmp`
   against @lobehub/icons-static-svg that settles provenance for the files above
   does not apply to it and LICENSES/README.md counts it separately. It answers
   the theme from a prefers-color-scheme rule inside the file, the way
   miromind.svg does; the tile pins that scheme light, so on its light plate the
   raven is black under either theme, as the app icon draws it. */
const OWN_MARK: { file: string } = { file: 'raven' }

/* The group card's mark: one tile standing for the nine `mirasim_*` rows the
   reader has not picked an agent out of yet.

   Outside `MARKS` on purpose, and parsed separately in the test that reads this
   file, for the reason `OWN_MARK` is: that table's contract is that it equals
   the third-party presets one for one, and this stands for nine of them at
   once. It is mirasim's own file, from `https://mirasim.ai/icon.svg` -- the
   handwritten "m" the vendor's own `marks.tsx` draws -- copied byte for byte
   except for the comment the site carries. It answers the theme from a
   `prefers-color-scheme` rule inside itself, the way raven.svg and
   miromind.svg do. */
const GROUP_MARK: { file: string } = { file: 'mirasim' }

/* The preset key the group card is drawn under. Not a preset: no row carries
   it, and `subagents.list` never sends one. It exists so the group card can
   reach `AgentMark` through the same door every other mark does. */
export const MIRASIM_GROUP = 'mirasim'

/* Spelled once, because four call sites read it off three different row types
   and a fifth that forgets `vendored` loses the mark on every Discovered agent
   while `builtin` keeps working -- a difference nobody reviewing one line would
   see. */
export const isOwnAgent = (row?: { builtin?: boolean; vendored?: boolean } | null): boolean =>
  !!(row?.builtin || row?.vendored)

/* `hasOwn`, not a bare index: a preset reaches this from config, and every
   object literal answers `constructor`, `toString` and `__proto__` from its
   prototype -- truthy, with no `file`, which renders a broken image at
   assets/agents/undefined.svg. raven.config.schema._resolve_preset_provenance
   refuses an unknown preset today, so nothing reaches here through config; that
   is a guard in another language with nothing tying it to this one, and this
   costs a call. `noUncheckedIndexedAccess` does not cover it -- it models the
   miss, not the inherited hit. */
function markFor(preset?: string | null, own?: boolean): { file: string; tone?: 'mono' | 'hybrid' } | undefined {
  if (own) return OWN_MARK
  if (preset === MIRASIM_GROUP) return GROUP_MARK
  return preset && Object.hasOwn(MARKS, preset) ? MARKS[preset] : undefined
}

export function agentMarkPath(preset?: string | null, own?: boolean): string | null {
  const mark = markFor(preset, own)
  return mark ? `assets/agents/${mark.file}.svg` : null
}

/* The generic glyph, for a row with neither a preset nor one of raven's own
   flags behind it: an agent the user configured themselves, and a group that
   exists only because an instance reported it -- `InstanceRow` carries no
   preset, so a group with no registration has nothing to look up. */
function BotIcon(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="5" y="7" width="14" height="11" rx="3" />
      <path d="M9 12h.01M15 12h.01M12 7V4M9 18v2M15 18v2" />
    </svg>
  )
}

/* One slot for both shapes, which is what keeps a roster of presets and
   hand-written rows starting its names at one x. The size lives on the slot,
   not on what fills it.

   `own` is the row's `builtin || vendored`, not a name test: it is resolved by
   the caller because the two flags live on the row and their spelling is the
   server's, not this module's. */
export function AgentMark({
  preset, own, badge,
}: { preset?: string | null; own?: boolean; badge?: string | null }): JSX.Element {
  const mark = markFor(preset, own)
  /* The overlaid mark, for an agent that reaches the reader through another
     one: the plate keeps the route's mark and the badge names the agent. Both
     files are already in the table -- the route's and the agent's -- so this
     draws a composite out of what ships rather than adding a third file per
     agent, which is also why the pair is a prop and not a second table. */
  const over = badge ? markFor(badge) : undefined
  return (
    <span className="agent-mark" aria-hidden="true">
      {mark ? (
        <img
          src={`assets/agents/${mark.file}.svg`}
          alt=""
          draggable="false"
          data-agent={preset}
          data-tone={mark.tone}
        />
      ) : <BotIcon />}
      {over ? (
        <span className="agent-mark-badge">
          <img
            src={`assets/agents/${over.file}.svg`}
            alt=""
            draggable="false"
            data-agent={badge}
            data-tone={over.tone}
          />
        </span>
      ) : null}
    </span>
  )
}
