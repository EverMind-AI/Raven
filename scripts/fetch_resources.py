#!/usr/bin/env python3
"""Download the model weights and dictionaries raven reads at runtime.

Two things live outside git and arrive here instead:

* the word dictionary behind :mod:`raven.core.tokenizer`, 8 MB of word and
  frequency lines, without which Chinese text is segmented per character and a
  keyword search over it answers with noise;
* the deepdoc vision models behind the PDF parser -- layout detection, text
  detection and recognition, table structure -- about 103 MB of ONNX graphs.

They are not tracked for the reason AGENTS.md section 7 gives: the repository
caps a file at 1 MiB, and the smallest of these clears it four times over. So
they are fetched at install time and by the image build, into the package
directories the readers look in.

Idempotent and safe to re-run: a file already present at its published size is
left alone, which is what makes it cheap to call from `make install` every
time. Nothing here is imported by the runtime -- a deployment that never opens
a knowledge base never needs this to have run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The dictionary, from Infinity's resource repository (MIT). Fetched over
#: https rather than through a package: the file lives in a git submodule of a
#: project we do not otherwise depend on.
DICTIONARY_REPO = "https://raw.githubusercontent.com/infiniflow/resource/main/rag"

#: The vision models, from the Hugging Face repository RAGFlow publishes them
#: in (Apache-2.0). Named by file rather than snapshotted whole: the repo is
#: 663 MB, and all but 103 MB of that is the same models in a second
#: serialisation plus three genre-specific layout variants nothing here selects.
DEEPDOC_REPO = "InfiniFlow/deepdoc"


@dataclass(frozen=True)
class Resource:
    """One file to fetch, and where the reader of it looks."""

    name: str
    into: Path
    #: What it is for, in the one line the progress output prints.
    purpose: str
    #: Saved under a different name than it is published under, when the reader
    #: expects one the publisher does not use.
    saved_as: str = ""

    @property
    def path(self) -> Path:
        return self.into / (self.saved_as or self.name)


CORE_RES = ROOT / "raven" / "core" / "res"
KNOWLEDGE_RES = ROOT / "raven" / "knowledge" / "res"

DICTIONARY = (Resource("huqie.txt", CORE_RES, "the word dictionary"),)

#: The prebuilt trie published beside the dictionary is deliberately not
#: fetched. `datrie` serialises the C library's own memory image, so a trie
#: built against another libdatrie fails to load -- the published one does not
#: load here -- and the tokenizer then rebuilds a larger one under the same
#: name, which every later run of this script would see as the wrong size and
#: download again. Building it locally costs six seconds, once, and
#: :func:`warm_dictionary` spends them here rather than inside a reader's first
#: query.

DEEPDOC = (
    Resource("det.onnx", KNOWLEDGE_RES, "text detection"),
    Resource("rec.onnx", KNOWLEDGE_RES, "text recognition"),
    Resource("ocr.res", KNOWLEDGE_RES, "the recogniser's character set"),
    Resource("layout.onnx", KNOWLEDGE_RES, "page layout"),
    Resource("tsr.onnx", KNOWLEDGE_RES, "table structure"),
)

#: What NLTK needs on disk before the tokenizer's latin path will run: the
#: sentence splitter `word_tokenize` loads, and the corpus the lemmatizer reads.
#: Into the package rather than a user's home, so an install is self-contained
#: and two checkouts do not fight over one cache.
#: In this order on purpose: nltk >= 3.8.2 gates `wordnet` behind `omw-1.4`, so
#: a run that fetched wordnet alone still raises LookupError the first time the
#: lemmatizer is asked for anything.
NLTK_CORPORA = ("omw-1.4", "wordnet", "punkt_tab")
NLTK_DATA = CORE_RES / "nltk_data"


def fetch_nltk(*, force: bool) -> int:
    """Download the corpora the latin half of the tokenizer needs."""
    # nltk >= 3.10 refuses to download through a proxy unless told to: its SSRF
    # guard cannot tell an intentional corporate proxy from a redirect it was
    # tricked into following. This is an explicit fetch of two named corpora
    # from nltk's own index, run by an operator installing raven, which is the
    # case the opt-out is for. `setdefault`, so a host that has already decided
    # keeps its answer.
    os.environ.setdefault("NLTK_ALLOW_PROXIED_URLOPEN", "1")

    import nltk

    NLTK_DATA.mkdir(parents=True, exist_ok=True)
    written = 0
    for corpus in NLTK_CORPORA:
        # Either spelling counts as present: nltk unpacks some corpora into a
        # directory and leaves others as the zip it downloaded, and checking
        # only for the directory re-downloaded wordnet on every run.
        target = NLTK_DATA / ("tokenizers" if corpus == "punkt_tab" else "corpora")
        if not force and ((target / corpus).exists() or (target / f"{corpus}.zip").exists()):
            print(f"  {corpus}: already here")
            continue
        print(f"  {corpus}: downloading")
        # `halt_on_error=False` so one unreachable corpus does not abort the
        # batch inside nltk; the return value is what this reports on, and the
        # caller decides whether a failure is fatal (`--optional`).
        if not nltk.download(corpus, download_dir=str(NLTK_DATA), quiet=True, halt_on_error=False):
            raise RuntimeError(f"nltk could not download {corpus!r}")
        written += 1
    return written


#: The layout models RAGFlow swaps in by document genre. Not fetched by
#: default: each is another 76 MB, and nothing in raven selects between them
#: yet -- a parser that always loads `layout.onnx` would download three models
#: it can never reach.
DEEPDOC_LAYOUTS = tuple(
    Resource(f"layout.{genre}.onnx", KNOWLEDGE_RES, f"page layout ({genre})") for genre in ("laws", "manual", "paper")
)


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _present(resource: Resource, expected: int | None) -> bool:
    """Whether this file is already here, and whole.

    Size rather than a hash: the published size is what the index gives for
    free, and a truncated download -- the failure this actually guards, a
    connection dropped mid-file -- changes it. A file of the right length whose
    bytes are wrong is a problem a checksum would catch and this will not; it
    is also not a problem that has ever been reported for these two hosts.
    """
    path = resource.path
    if not path.is_file():
        return False
    if expected is None or path.stat().st_size == expected:
        return True
    print(f"  {resource.name}: {_human(path.stat().st_size)} on disk, {_human(expected)} published; refetching")
    return False


def fetch_dictionary(resources: "tuple[Resource, ...]", *, force: bool) -> int:
    """Download the tokenizer's dictionary. Returns how many files were written."""
    import httpx

    written = 0
    resources[0].into.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        for resource in resources:
            url = f"{DICTIONARY_REPO}/{resource.name}"
            expected: int | None = None
            try:
                head = client.head(url)
                expected = int(head.headers.get("content-length") or 0) or None
            except httpx.HTTPError:
                pass
            if not force and _present(resource, expected):
                print(f"  {resource.path.name}: already here ({resource.purpose})")
                continue
            print(f"  {resource.path.name}: downloading {_human(expected or 0)} ({resource.purpose})")
            # To a temporary name first: a half-written dictionary that keeps
            # the real name is one every later run treats as present.
            staged = resource.path.with_suffix(resource.path.suffix + ".part")
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with staged.open("wb") as handle:
                    for chunk in response.iter_bytes(1 << 20):
                        handle.write(chunk)
            staged.replace(resource.path)
            written += 1
    return written


def warm_dictionary() -> None:
    """Build the tokenizer's trie cache, so the first query does not.

    Best effort: a failure here costs six seconds later, not correctness, and
    an install must not stop because a cache could not be written.
    """
    from raven.core import tokenizer as tok

    cache = tok.dictionary_path().with_suffix(".txt.trie")
    if cache.is_file():
        print("  index: already built")
        return
    print("  index: building (a few seconds)")
    try:
        tok.Tokenizer()
    except Exception as exc:  # noqa: BLE001 - the dictionary still works without the cache
        print(f"  index: could not be built ({exc}); it will be built on first use", file=sys.stderr)
        return
    if cache.is_file():
        print(f"  index: built, {_human(cache.stat().st_size)}")


def fetch_deepdoc(resources: "tuple[Resource, ...]", *, force: bool) -> int:
    """Download the vision models. Returns how many files were written."""
    from huggingface_hub import hf_hub_download, model_info

    KNOWLEDGE_RES.mkdir(parents=True, exist_ok=True)
    sizes = {}
    try:
        sizes = {f.rfilename: f.size for f in model_info(DEEPDOC_REPO, files_metadata=True).siblings}
    except Exception as exc:  # noqa: BLE001 - the index is a convenience, not the download
        print(f"  (could not read the published sizes: {exc})")

    written = 0
    for resource in resources:
        expected = sizes.get(resource.name)
        if not force and _present(resource, expected):
            print(f"  {resource.path.name}: already here ({resource.purpose})")
            continue
        print(f"  {resource.path.name}: downloading {_human(expected or 0)} ({resource.purpose})")
        # Into the cache and then copied, rather than symlinked into place: an
        # image build throws the cache layer away, and a symlink into a
        # directory that no longer exists is worse than no file at all.
        cached = hf_hub_download(repo_id=DEEPDOC_REPO, filename=resource.name)
        shutil.copyfile(cached, resource.path)
        written += 1
    return written


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--only",
        choices=("dictionary", "deepdoc"),
        help="fetch one set rather than both",
    )
    parser.add_argument(
        "--all-layouts",
        action="store_true",
        help="also fetch the genre-specific layout models (3 x 76 MB), which nothing selects yet",
    )
    parser.add_argument("--force", action="store_true", help="download again even when the file is already here")
    parser.add_argument(
        "--optional",
        action="store_true",
        help="report a failure and exit 0; for an install that must not stop because a host is unreachable",
    )
    args = parser.parse_args(argv)

    plan: list[tuple[str, Callable[..., int], tuple[Resource, ...]]] = []
    if args.only != "deepdoc":
        plan.append(("word dictionary", fetch_dictionary, DICTIONARY))
        plan.append(("nltk corpora", lambda _r, *, force: fetch_nltk(force=force), ()))
        plan.append(("dictionary index", lambda _r, *, force: (warm_dictionary(), 0)[1], ()))
    if args.only != "dictionary":
        models = DEEPDOC + (DEEPDOC_LAYOUTS if args.all_layouts else ())
        plan.append(("deepdoc models", fetch_deepdoc, models))

    written = 0
    for title, fetch, resources in plan:
        print(f"{title}:")
        try:
            written += fetch(resources, force=args.force)
        except Exception as exc:  # noqa: BLE001 - reported, and optionally survivable
            print(f"  failed: {exc}", file=sys.stderr)
            if not args.optional:
                return 1
            print("  continuing without it; the features that read it will say so when asked", file=sys.stderr)
    print(f"resources: {written} file(s) written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
