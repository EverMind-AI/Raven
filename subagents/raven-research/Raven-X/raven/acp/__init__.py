"""ACP (Agent Client Protocol) agent side: serve Raven-X over stdio JSON-RPC.

The consumer is the main raven's subagent backend (``kind: "acp"``); editor
compatibility is a by-product, not a design target. See ACP_INTEGRATION_PLAN.md
for the design record and the phase discipline.
"""
