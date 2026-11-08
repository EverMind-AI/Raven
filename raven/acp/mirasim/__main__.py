"""``python -m raven.acp.mirasim --agent <id>``: serve one Mirasim agent over ACP on stdio."""

from raven.acp.mirasim.server import main

raise SystemExit(main())
