"""Each channel is called the same thing in English by the CLI and by the web UI.

The CLI prints an adapter's ``display_name`` (``raven/cli/channel_commands.py``),
and the web UI names the same entrance from the shared message catalogue,
``i18n/messages.json``, under ``gui.chan.<adapter>``. Two sources for one name
and nothing tying them: the message catalogue carried a Chinese word in its English
slot for one channel while the CLI spelled that brand its own way, and each
looked right from where it was read.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.channels.registry import discover_channel_names, discover_specs

_MESSAGES = Path(__file__).resolve().parents[1] / "i18n" / "messages.json"


def test_the_cli_and_the_web_ui_agree_on_every_english_name() -> None:
    words = json.loads(_MESSAGES.read_text(encoding="utf-8"))["ui"]
    specs = discover_specs()
    assert set(specs) == set(discover_channel_names()), "a spec did not import, so its name went unchecked"
    wrong = {
        name: (spec.display_name, words.get(f"gui.chan.{name}", {}).get("en"))
        for name, spec in specs.items()
        if words.get(f"gui.chan.{name}", {}).get("en") != spec.display_name
    }
    assert not wrong
