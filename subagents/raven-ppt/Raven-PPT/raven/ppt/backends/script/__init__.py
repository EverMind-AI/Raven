"""The route where the author writes the deck as a python-pptx program.

The alternative was a schema of semantic regions the engine compiled. It bounded
the deck at what the schema could say and filled the rest in itself -- a band
across every title, a panel behind a narrow one, a tile under every icon -- so
seventeen different compositions still read as one template. A script has no such
ceiling: the author defines its own page furniture once and uses it throughout,
which is what a designed deck is.

The cost is that the program is now the thing under management, and three
problems follow from that. Which part of it drew which page (blocks). Whether a
submission is a program at all (submission). And whether a build that died can be
attributed to a page (blocks.broken_page). Each has a module.
"""

from raven.ppt.backends.script.blocks import broken_page, page_blocks, page_sources
from raven.ppt.backends.script.edit import (
    applied_lines,
    apply_verified,
    block_rejection,
    blocks_rejection,
    first_marker,
    prelude_rejection,
    slide_creators,
    with_banner,
)
from raven.ppt.backends.script.runner import ScriptBackend, run_script
from raven.ppt.backends.script.submission import carries_a_program, submission_refusal
from raven.ppt.backends.script.workspace import (
    HelperSources,
    asset_helpers,
    provision,
    script_path,
    with_template_helpers,
)

__all__ = [
    "HelperSources",
    "asset_helpers",
    "applied_lines",
    "apply_verified",
    "block_rejection",
    "blocks_rejection",
    "ScriptBackend",
    "broken_page",
    "carries_a_program",
    "first_marker",
    "page_blocks",
    "page_sources",
    "prelude_rejection",
    "provision",
    "run_script",
    "script_path",
    "slide_creators",
    "submission_refusal",
    "with_banner",
    "with_template_helpers",
]
