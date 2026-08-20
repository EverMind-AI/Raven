// SPDX-License-Identifier: MIT
// Portions Copyright (c) original ink contributors (vadimdemedes/ink, MIT).
// Portions Copyright (c) 2025 Nous Research (hermes-agent / hermes-ink, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-{hermes-agent,ink}.txt.

let _npmSemver: typeof import('semver') | undefined

function getNpmSemver(): typeof import('semver') {
  if (!_npmSemver) {
    _npmSemver = require('semver') as typeof import('semver')
  }

  return _npmSemver
}

export function gt(a: string, b: string): boolean {
  if (typeof Bun !== 'undefined') {
    return Bun.semver.order(a, b) === 1
  }

  return getNpmSemver().gt(a, b, { loose: true })
}

export function gte(a: string, b: string): boolean {
  if (typeof Bun !== 'undefined') {
    return Bun.semver.order(a, b) >= 0
  }

  return getNpmSemver().gte(a, b, { loose: true })
}

export function lt(a: string, b: string): boolean {
  if (typeof Bun !== 'undefined') {
    return Bun.semver.order(a, b) === -1
  }

  return getNpmSemver().lt(a, b, { loose: true })
}

export function lte(a: string, b: string): boolean {
  if (typeof Bun !== 'undefined') {
    return Bun.semver.order(a, b) <= 0
  }

  return getNpmSemver().lte(a, b, { loose: true })
}

export function satisfies(version: string, range: string): boolean {
  if (typeof Bun !== 'undefined') {
    return Bun.semver.satisfies(version, range)
  }

  return getNpmSemver().satisfies(version, range, { loose: true })
}

export function order(a: string, b: string): -1 | 0 | 1 {
  if (typeof Bun !== 'undefined') {
    return Bun.semver.order(a, b)
  }

  return getNpmSemver().compare(a, b, { loose: true }) as -1 | 0 | 1
}
