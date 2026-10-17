"""Native Windows guard launches with inline and file-backed Mirasim prompts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest

from raven.acp.mirasim.uicli import Invocation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="native Windows launch limits"),
]

_READ_PROMPT = """
import hashlib, json, pathlib, sys
files = [arg.removeprefix('--file=') for arg in sys.argv[1:] if arg.startswith('--file=')]
text = next(arg.removeprefix('--text=') for arg in sys.argv[1:] if arg.startswith('--text='))
data = pathlib.Path(files[-1]).read_bytes() if files else text.encode('utf-8')
print(json.dumps({'digest': hashlib.sha256(data).hexdigest(), 'files': files}), flush=True)
"""


@pytest.mark.parametrize(
    ("text", "model", "staged"),
    [
        ("short", "chosen", False),
        ("x" * 40000, "chosen", True),
        ("\U0001f680" * 17000, "chosen", True),
        ('\\"' * 12000, "chosen", True),
        ("x" * 20000, "m" * 13000, True),
    ],
    ids=["short", "ascii", "utf16", "quoted", "flags"],
)
async def test_the_guard_reads_the_complete_prompt_within_the_native_limit(tmp_path, text, model, staged):
    invocation = Invocation(
        [sys.executable, "-c", _READ_PROMPT, f"--text={text}", f"--model={model}"],
        env=dict(os.environ),
        cwd=str(tmp_path),
    )
    outcome = await asyncio.wait_for(invocation.run(), 20)
    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.document["digest"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert bool(outcome.document["files"]) is staged
    assert all(not Path(path).exists() for path in outcome.document["files"])
