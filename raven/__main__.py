"""
Entry point for running Raven as a module: python -m raven
"""

from raven.cli.commands import app

if __name__ == "__main__":
    app()
