// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Pins chalk's colour level for the suite. Without it the renderer runs at
// level 0, where colour and dim are never emitted -- which silently turns any
// test about styling into a test that nothing is styled. Set here rather than
// in a test file because chalk resolves the level when `@hermes/ink` is first
// imported, which for a module-scope import is before any test body runs.
process.env.HERMES_TUI_LEVEL ??= '3'
