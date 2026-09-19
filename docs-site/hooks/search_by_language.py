"""Give each language its own search index.

The i18n plugin builds the tree once per locale and then writes a single index
covering both, so a reader searching in one language is offered pages in the
other: results they cannot read, behind links that leave the language they
chose.

Splitting that index is not enough by itself. The theme resolves two things
against the `base` recorded in each page -- the index it fetches, and the href
it builds for every result -- so the Chinese pages are pointed one level
shallower, at their own root, and their entries are rewritten relative to it.

The merge lands in a plugin's own `on_post_build`, so the split has to be the
last thing that event does -- hence the priority. Running it on `on_shutdown`
instead looks equivalent and is not: `mkdocs serve` never shuts down, so the
preview would go on serving the merged index however often it rebuilt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mkdocs.plugins import event_priority

ZH = "zh/"

#: The theme reads its settings from one JSON block per page, and `base`
#: appears elsewhere in the markup, so only that block is rewritten.
CONFIG_BLOCK = re.compile(r'(<script id="__config" type="application/json">)(.*?)(</script>)', re.S)

def _shallower(base: str) -> str:
    """One directory up, expressed the way the theme writes it."""
    return base[3:] if base.startswith("../") else "."


def on_post_page(output: str, page: Any, config: Any) -> str:
    if not page.file.dest_uri.startswith(ZH):
        return output

    def rewrite(match: re.Match[str]) -> str:
        settings = json.loads(match.group(2))
        settings["base"] = _shallower(settings["base"])
        return f"{match.group(1)}{json.dumps(settings)}{match.group(3)}"

    return CONFIG_BLOCK.sub(rewrite, output, count=1)


@event_priority(-100)
def on_post_build(config: Any) -> None:
    index = Path(config["site_dir"]) / "search" / "search_index.json"
    if not index.exists():
        return

    data = json.loads(index.read_text(encoding="utf-8"))
    entries = data.get("docs", [])
    english = [entry for entry in entries if not entry["location"].startswith(ZH)]
    chinese = [
        {**entry, "location": entry["location"][len(ZH) :]} for entry in entries if entry["location"].startswith(ZH)
    ]
    # Between the two locale builds this file holds one language at a time, and
    # splitting then would publish an empty index for the other. Only the
    # merged index has both, so both being present is what says it is ready.
    if not english or not chinese:
        return

    index.write_text(json.dumps({**data, "docs": english}), encoding="utf-8")
    translated = index.parent.parent / ZH / "search" / "search_index.json"
    translated.parent.mkdir(parents=True, exist_ok=True)
    translated.write_text(json.dumps({**data, "docs": chinese}), encoding="utf-8")
