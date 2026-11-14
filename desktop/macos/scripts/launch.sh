#!/bin/sh
# Raven Desktop bootstrap (portable).
#
# Prints the one-time authenticated page URL on stdout. Intended to be called by
# the native macOS app; `raven web --print-url` opens no browser because the app
# itself is the window.
#
# The CLI owns the whole gateway lifecycle: it attaches when one is running,
# waits for one that is starting, and starts one when there is none. This script
# deliberately never signals a gateway -- an empty probe can mean "still
# booting", and stopping it there killed the launch it was trying to join.

set -u

# Finder-launched apps get a minimal PATH. Make sure ~/.local/bin is visible.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin${PATH:+:$PATH}"

# Resolve the `raven` CLI. Prefer PATH, then the conventional uv location.
if command -v raven >/dev/null 2>&1; then
    RAVEN="$(command -v raven)"
else
    RAVEN="$HOME/.local/bin/raven"
fi

if [ ! -x "$RAVEN" ]; then
    echo "raven CLI not found on PATH or at ~/.local/bin/raven" >&2
    exit 1
fi

exec "$RAVEN" web --print-url
