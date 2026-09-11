/* Agent branding for the roster, on the same terms as provider-mark.tsx. */

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

   `tone` says how the file answers the theme, which is a fact about the file
   rather than a preference -- tests/test_ui_agent_marks.py derives each one
   from the shapes in the SVG it names, and the stylesheet reads it back off
   `data-tone`. Three answers, because an <img> resolves `currentColor` against
   its own document rather than the page and so renders it black:

   - absent: every shape carries its own fill. Theme-independent, touch nothing.
   - `mono`: every shape is `currentColor`. Black until the dark theme inverts
     the lot, which is what the provider marks already do.
   - `hybrid`: some shapes are, some are not. Only qoder, whose mark is brand
     green beside one tone the vendor means to follow the page's text -- so a
     plain invert would take the green to magenta with it, and the hue rotation
     puts it back. Measured: #2ADB5C -> #D524A3 inverted, -> #008203 with the
     rotation. Not the same green, and the closest a filter gets without
     constants fitted to one hex; the alternative that reproduces it exactly is
     inlining the file, which ui-web/build.py's assets comment rules out. */
const MARKS: Record<string, { file: string; tone?: 'mono' | 'hybrid' }> = {
  claude_code: { file: 'claudecode-color' },
  codebuddy: { file: 'codebuddy-color' },
  codex: { file: 'codex-color' },
  github_copilot: { file: 'copilot-color' },
  grok: { file: 'grok', tone: 'mono' },
  hermes: { file: 'hermesagent', tone: 'mono' },
  kimi_code: { file: 'kimi', tone: 'mono' },
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
   against @lobehub/icons-static-svg that settles provenance for the thirteen
   does not apply to it and LICENSES/README.md counts it separately. It answers
   the theme from a prefers-color-scheme rule inside the file, the way
   miromind.svg does: every shape carries its own fill, so no `tone` filter
   reaches it, and a raven is black -- which on the dark theme's surface is a
   silhouette at 1.2:1, two white eyes floating in an empty frame. */
const OWN_MARK: { file: string } = { file: 'raven' }

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
export function AgentMark({ preset, own }: { preset?: string | null; own?: boolean }): JSX.Element {
  const mark = markFor(preset, own)
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
    </span>
  )
}
