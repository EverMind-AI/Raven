/* A title is whatever the prompt or the job name started with, and a leading
   emoji turns a dense column of rows into a column of stickers. Display only:
   the stored title keeps its icon, and a title that is nothing BUT an icon
   stays as it is rather than rendering an empty row. */
const LEAD_ICO =
  /^(?:[\s\u00A0]*[\p{Extended_Pictographic}\p{Emoji_Modifier}\p{Regional_Indicator}\u{1F3FB}-\u{1F3FF}\uFE0F\u200D]+)+[\s\u00A0]*/u

export function plainTitle(t: unknown): string {
  const raw = t == null ? '' : String(t)
  return raw.replace(LEAD_ICO, '').trim() || raw
}
