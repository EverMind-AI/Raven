/* The page's icon set: HugeIcons, plus the few glyphs the design draws itself.
 *
 * Every icon is a 24-unit drawing scaled to `size` and inked in
 * `currentColor`, so the row it sits in decides its colour through hover,
 * selection and both themes. `stroke` is in the drawing's own units: HugeIcons
 * draws at 1.5, which is a 1px line at the rail's 16px.
 *
 * The glyphs below are the design's own (Figma: Raven), for the places
 * HugeIcons has no match for.
 */

import { HugeiconsIcon } from '@hugeicons/react'

import type { IconSvgElement } from '@hugeicons/react'
import type { JSX } from 'react'

export function Icon({
  icon,
  size = 16,
  stroke,
  className,
}: {
  icon: IconSvgElement
  size?: number
  stroke?: number
  className?: string
}): JSX.Element {
  return (
    <HugeiconsIcon icon={icon} size={size} strokeWidth={stroke} aria-hidden="true"
      {...(className ? { className } : {})} />
  )
}

/** The Agents row: a small robot head with a flame of a crest. */
export function AgentsGlyph({ size = 16 }: { size?: number }): JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <g transform="translate(1.6 0.17)">
        <path fill="currentColor" d="M6.31035 0.46582C6.87855 0.914637 7.44039 1.52667 7.75371 2.25098C7.86994 2.05417 7.96684 1.85245 8.03984 1.64551L8.26543 1.00586L8.80937 1.41113C9.65798 2.04238 10.4021 3.20917 10.4021 4.32715H9.40215C9.40211 3.78247 9.10594 3.14297 8.67851 2.64941C8.46209 3.03975 8.18782 3.40389 7.86504 3.73535L7.02324 4.59961L7.00664 3.39355C6.9987 2.83307 6.70656 2.27569 6.26152 1.78418C6.22384 1.9168 6.1882 2.04739 6.14824 2.17188C6.02853 2.54471 5.883 2.91364 5.67168 3.23633C5.4557 3.56606 5.17117 3.84829 4.78203 4.04297C4.3972 4.23548 3.94083 4.32715 3.40215 4.32715V3.32715C3.82898 3.32715 4.12309 3.25433 4.33476 3.14844C4.54198 3.04472 4.69998 2.89424 4.83476 2.68848C4.97419 2.4756 5.08735 2.20489 5.19609 1.86621C5.30625 1.52307 5.3992 1.15374 5.51836 0.724609L5.71953 0L6.31035 0.46582Z" />
        <path fill="currentColor" d="M4.72168 3.33496C4.39981 3.76723 3.97855 4.10734 3.40234 4.3252L3.69336 4.35449C3.5765 4.35986 3.46719 4.36644 3.36426 4.375C2.91215 4.41263 2.62465 4.48262 2.39355 4.59668C1.89751 4.84159 1.49669 5.24413 1.25391 5.74121C1.14079 5.97284 1.07182 6.26049 1.03613 6.71289C1.00003 7.17076 1 7.75405 1 8.57031V10.085C1 10.9046 1.0008 11.4905 1.03711 11.9502C1.07298 12.4042 1.14124 12.6927 1.25488 12.9248C1.49897 13.423 1.90215 13.8262 2.40039 14.0703C2.63246 14.184 2.921 14.2522 3.375 14.2881C3.83466 14.3244 4.42058 14.3252 5.24023 14.3252H7.75977C8.57942 14.3252 9.16534 14.3244 9.625 14.2881C10.079 14.2522 10.3675 14.184 10.5996 14.0703C11.0979 13.8262 11.501 13.423 11.7451 12.9248C11.8588 12.6927 11.927 12.4042 11.9629 11.9502C11.9992 11.4905 12 10.9046 12 10.085V8.57031C12 7.75405 12 7.17076 11.9639 6.71289C11.9282 6.26049 11.8592 5.97284 11.7461 5.74121C11.5033 5.24413 11.1025 4.84159 10.6064 4.59668C10.3753 4.48262 10.0878 4.41263 9.63574 4.375C9.63056 4.37457 9.62533 4.37445 9.62012 4.37402C9.61935 4.13228 9.62012 3.89759 9.62012 3.84668C9.62011 3.68607 9.61165 3.52723 9.59766 3.37109C9.63876 3.37392 9.67918 3.37561 9.71875 3.37891C10.2305 3.42151 10.6592 3.50735 11.0498 3.7002C11.7441 4.04311 12.3057 4.60686 12.6455 5.30273C12.8365 5.69394 12.9196 6.12217 12.96 6.63379C12.9999 7.14011 13 7.76983 13 8.57031V10.085C13 10.8888 13.0001 11.5211 12.96 12.0293C12.9194 12.543 12.8357 12.9729 12.6436 13.3652C12.3018 14.0628 11.7376 14.627 11.04 14.9688C10.6477 15.1609 10.2178 15.2446 9.7041 15.2852C9.19589 15.3253 8.56358 15.3252 7.75977 15.3252H5.24023C4.43642 15.3252 3.80411 15.3253 3.2959 15.2852C2.78221 15.2446 2.35227 15.1609 1.95996 14.9688C1.26244 14.627 0.698163 14.0628 0.356445 13.3652C0.164254 12.9729 0.080639 12.543 0.0400391 12.0293C-0.00011773 11.5211 8.00282e-09 10.8888 8.00282e-09 10.085V8.57031C-1.09254e-07 7.76983 0.000102925 7.14011 0.0400391 6.63379C0.0804164 6.12218 0.16355 5.69394 0.354492 5.30273C0.694312 4.60686 1.25586 4.04312 1.9502 3.7002C2.34079 3.50735 2.76946 3.42151 3.28125 3.37891C3.678 3.3459 4.15047 3.33878 4.72168 3.33496Z" />
        <path stroke="currentColor" strokeLinecap="round" d="M4.00337 7.75293L5.61976 8.71944C5.64762 8.73609 5.65143 8.77493 5.62736 8.79669L4.24475 10.046" />
        <path stroke="currentColor" strokeLinecap="round" d="M8.99995 10.0459L9 8.75293L9 7.75293" />
      </g>
    </svg>
  )
}

/** Archive: a lidded box with a handle. */
export function ArchiveGlyph({ size = 16 }: { size?: number }): JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <g transform="translate(2 2)" stroke="currentColor">
        <path d="M2.8125 0.5H9.1875C9.48961 0.5 9.60467 0.500742 9.69629 0.509766C10.6456 0.603394 11.3966 1.35437 11.4902 2.30371C11.4993 2.39533 11.5 2.51039 11.5 2.8125C11.5 2.99802 11.4993 3.05497 11.4951 3.09766C11.4484 3.57243 11.0724 3.94836 10.5977 3.99512C10.555 3.99929 10.498 4 10.3125 4H1.6875C1.50198 4 1.44503 3.99929 1.40234 3.99512C0.927567 3.94836 0.551644 3.57243 0.504883 3.09766C0.500706 3.05497 0.5 2.99802 0.5 2.8125C0.5 2.51039 0.500742 2.39533 0.509766 2.30371C0.603394 1.35437 1.35437 0.603394 2.30371 0.509766C2.39533 0.500742 2.51039 0.5 2.8125 0.5Z" />
        <path d="M1 4V7C1 8.88562 1 9.82843 1.58579 10.4142C2.17157 11 3.11438 11 5 11H7C8.88562 11 9.82843 11 10.4142 10.4142C11 9.82843 11 8.88562 11 7V4" />
        <path strokeLinecap="round" d="M5 7L7 7" />
      </g>
    </svg>
  )
}

/** Usage: three bars in a rounded square. */
export function UsageGlyph({ size = 16 }: { size?: number }): JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <g transform="translate(1.5 1.5)" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3.5 9.5V7.5M6.5 9.5V3.5M9.5 9.5V5.5" />
        <path d="M0.5 6.5C0.5 3.67157 0.5 2.25736 1.37868 1.37868C2.25736 0.5 3.67157 0.5 6.5 0.5C9.32843 0.5 10.7426 0.5 11.6213 1.37868C12.5 2.25736 12.5 3.67157 12.5 6.5C12.5 9.32843 12.5 10.7426 11.6213 11.6213C10.7426 12.5 9.32843 12.5 6.5 12.5C3.67157 12.5 2.25736 12.5 1.37868 11.6213C0.5 10.7426 0.5 9.32843 0.5 6.5Z" />
      </g>
    </svg>
  )
}

/** Model providers: a cube. */
export function CubeGlyph({ size = 16 }: { size?: number }): JSX.Element {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <g stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
        <path transform="translate(1.5 4.5)" d="M6.5 10.5C6.70941 10.5 6.90407 10.4058 7.29341 10.2173L10.4228 8.70271C11.8076 8.03247 12.5 7.69736 12.5 7.16667V0.5M6.5 10.5C6.29059 10.5 6.09593 10.4058 5.70659 10.2173L2.57717 8.7027C1.19239 8.03247 0.5 7.69736 0.5 7.16667V0.5M6.5 10.5V3.83333" />
        <path transform="translate(1.5 0.5)" d="M5.70659 0.79679C6.09593 0.59893 6.29059 0.5 6.5 0.5C6.70941 0.5 6.90407 0.59893 7.29341 0.79679L10.4228 2.38716C11.8076 3.09091 12.5 3.44278 12.5 4C12.5 4.55722 11.8076 4.90909 10.4228 5.61284L7.29341 7.20321C6.90407 7.40107 6.70941 7.5 6.5 7.5C6.29059 7.5 6.09593 7.40107 5.70659 7.20321L2.57717 5.61284C1.19239 4.90909 0.5 4.55722 0.5 4C0.5 3.44278 1.19239 3.09091 2.57717 2.38716L5.70659 0.79679Z" />
      </g>
    </svg>
  )
}

/* A staged file's type, as a coloured badge -- the one place the icon set
   carries colour of its own, because the colour is the type. Only the kinds the
   design draws a badge for; anything else wears a plain file glyph instead. */
export function FileBadge({ kind }: { kind: 'pdf' | 'doc' }): JSX.Element {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <rect width="14" height="14" rx="3" fill={kind === 'pdf' ? '#FB4C4B' : '#407DF5'} />
      {kind === 'pdf' ? (
        <path stroke="white" strokeWidth="0.6" d="M3.65716 8.9545C2.79173 9.40014 2.75653 10.2684 3.22444 10.5917C4.66682 11.5882 5.09954 8.95451 6.63759 5.60896C8.53289 1.48633 4.73894 2.40577 5.96497 5.18186C6.81252 7.10098 8.38796 8.60921 9.78728 8.9545C11.2297 9.31041 11.4521 7.21527 9.00001 7.5C7.03836 7.72778 4.90125 8.31386 3.65716 8.9545Z" />
      ) : (
        <path fill="white" d="M4.6193 10.3182L3.00566 4.50001H3.92896L5.06248 9.00569H5.11646L6.29543 4.50001H7.21021L8.38918 9.00853H8.44316L9.57384 4.50001H10.5L8.8835 10.3182H7.99998L6.77555 5.96023H6.73009L5.50566 10.3182H4.6193Z" />
      )}
    </svg>
  )
}

/* A delivered file's type, as the design's page-with-a-folded-corner mark
   (Figma: Raven / File_ICON): a body and a fold in the type's own two tones,
   and the extension printed on it. The kinds the design draws have its
   colours; the rest take the nearest family, and anything unknown the slate
   the design gives markdown. */
const FILE_TONES: Record<string, [string, string, string, string]> = {
  red: ['#C8201F', '#C8201F', '#F06060', '#CC1918'],
  blue: ['#6D9CFA', '#497CE0', '#4B7BF7', '#1A4DD1'],
  green: ['#3FB36B', '#23904E', '#5ACB84', '#1C7A41'],
  slate: ['#8492AC', '#677692', '#A1ADC7', '#7C8EB3'],
}
const FILE_TONE_OF: Record<string, string> = {
  ppt: 'red', pptx: 'red', key: 'red', pdf: 'red',
  html: 'blue', htm: 'blue', doc: 'blue', docx: 'blue',
  xls: 'green', xlsx: 'green', csv: 'green',
}

export function FileMark({ ext, width = 29, height = 34 }: {
  ext: string; width?: number; height?: number
}): JSX.Element {
  const key = ext.toLowerCase()
  const [b0, b1, f0, f1] = FILE_TONES[FILE_TONE_OF[key] || 'slate'] as [string, string, string, string]
  const id = `fm-${key || 'x'}`
  const label = key.slice(0, 4).toUpperCase()
  return (
    <svg width={width} height={height} viewBox="0 0 33 40" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id={`${id}-b`} x1="17" y1="2" x2="17" y2="44" gradientUnits="userSpaceOnUse">
          <stop stopColor={b0} />
          <stop offset="1" stopColor={b1} />
        </linearGradient>
        <linearGradient id={`${id}-f`} x1="27" y1="0" x2="27" y2="11.5" gradientUnits="userSpaceOnUse">
          <stop stopColor={f0} />
          <stop offset="1" stopColor={f1} />
        </linearGradient>
        <filter id={`${id}-s`} x="18" y="-3" width="18" height="18" filterUnits="userSpaceOnUse">
          <feDropShadow dx="0.24" dy="0.12" stdDeviation="1.47" floodOpacity="0.33" />
        </filter>
      </defs>
      <path fill={`url(#${id}-b)`} d="M33 11.3848V32C33 34.8 33 36.2 32.4551 37.2695C31.9757 38.2103 31.2103 38.9757 30.2695 39.4551C29.2 40 27.8 40 25 40H8C5.2 40 3.8 40 2.73047 39.4551C1.78966 38.9757 1.02429 38.2103 0.544922 37.2695C0 36.2 0 34.8 0 32V8C0 5.2 0 3.79905 0.544922 2.72949C1.0243 1.78879 1.78973 1.02425 2.73047 0.544922C3.8 0 5.2 0 8 0H21.6152L33 11.3848Z" />
      <path fill={`url(#${id}-f)`} filter={`url(#${id}-s)`} d="M33 11.5H24.0882C22.6588 11.5 21.5 10.3412 21.5 8.9118V0L33 11.5Z" />
      {label ? (
        <text x="16.5" y="30" textAnchor="middle" fill="#fff" fontSize="8" letterSpacing="0.56"
          fontFamily="'Varela Round', ui-rounded, system-ui, sans-serif">{label}</text>
      ) : null}
    </svg>
  )
}
