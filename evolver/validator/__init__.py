"""Agent package validator: decide whether an agent folder can ship with Raven.

A folder passes when it stays inside the agent scope the shipped agents define,
runs end to end through the host's ACP dispatch path in an isolated ``RAVEN_HOME``,
and returns results a judge model accepts on an acceptance suite kept outside the
folder. Entry point: ``python -m evolver validate``. The package imports no other
``evolver`` module, and agent-folder code runs only in child processes.
"""
