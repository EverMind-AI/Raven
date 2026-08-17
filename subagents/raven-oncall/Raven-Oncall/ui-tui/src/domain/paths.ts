export const shortCwd = (cwd: string, max = 28) => {
  // Windows has no HOME; fall back to USERPROFILE so the cwd collapses to ~
  // instead of showing the full C:\Users\... path in the status bar.
  const h = process.env.HOME || process.env.USERPROFILE
  const p = h && cwd.startsWith(h) ? `~${cwd.slice(h.length)}` : cwd

  return p.length <= max ? p : `…${p.slice(-(max - 1))}`
}

export const fmtCwdBranch = (cwd: string, branch: null | string, max = 40) => {
  if (!branch) {
    return shortCwd(cwd, max)
  }

  const tag = ` (${branch.length > 16 ? `…${branch.slice(-15)}` : branch})`

  return `${shortCwd(cwd, Math.max(8, max - tag.length))}${tag}`
}
