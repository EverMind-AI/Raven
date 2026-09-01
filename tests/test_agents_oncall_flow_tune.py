"""The tune entry: the D5 landing of the fork's ``raven ops tune`` command.

The fork mounted a typer app on the host CLI; the plugin's full-auto tuner is
a module entry instead (``python -m oncall_flow.tune``), so the same pins
drive its argparse surface: the grid and adaptive options are on the menu, and
adaptive mode refuses to start without an LLM endpoint.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.tune import _parser, main  # noqa: E402


def _help_text() -> str:
    return _parser().format_help()


def test_tune_help_lists_key_options() -> None:
    text = _help_text()
    for opt in ("--host", "--k1", "--b", "--metric", "--remote-dir"):
        assert opt in text


def test_tune_help_lists_adaptive_options() -> None:
    text = _help_text()
    for opt in ("--adaptive", "--objective", "--max-rounds", "--llm-base-url", "--llm-model"):
        assert opt in text


def test_adaptive_requires_llm_endpoint(capsys) -> None:
    rc = main(["--host", "1.2.3.4", "--adaptive"])

    assert rc == 1
    assert "requires --llm-base-url and --llm-model" in capsys.readouterr().err
