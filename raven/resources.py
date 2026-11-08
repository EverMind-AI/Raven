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

In the package rather than under ``scripts/``, because the wheel carries
``raven/**`` and nothing else: a one-line install has no checkout, so a fetcher
that lived beside the Makefile was reachable only by developers. Here it is
reachable three ways that all run the same code -- ``raven resources``,
``python -m raven.resources``, and ``scripts/fetch_resources.py``, which is now
a shim the Makefile and the Dockerfile still call.

The destinations are derived the way the readers derive them, relative to this
package, so an editable install writes into the checkout and a wheel install
writes beside the installed modules without either being told which it is.

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
from typing import Any
from urllib.parse import urlsplit

from raven.utils import mirrors

#: This package's own directory. The two resource trees hang off it, which is
#: exactly how ``core.tokenizer`` and the deepdoc ``_onnx`` reader find them --
#: one derivation, so the writer and the readers cannot disagree about where
#: the files went.
PACKAGE = Path(__file__).resolve().parent

#: The dictionary, from Infinity's resource repository (MIT). Fetched over
#: https rather than through a package: the file lives in a git submodule of a
#: project we do not otherwise depend on.
DICTIONARY_REPO = "https://raw.githubusercontent.com/infiniflow/resource/main/rag"

#: The same directory through jsDelivr, for when raw.githubusercontent.com
#: cannot be reached; from mainland China it times out more often than not.
DICTIONARY_MIRROR = f"{mirrors.JSDELIVR_GITHUB}/infiniflow/resource@main/rag"

#: The vision models, from the Hugging Face repository RAGFlow publishes them
#: in. Named by file rather than snapshotted whole: the repo is 663 MB, and all
#: but 103 MB of that is the same models in a second serialisation plus three
#: genre-specific layout variants nothing here selects.
#:
#: The repository is Apache-2.0 and the weights inside it are not uniformly so
#: -- `layout.onnx` stamps itself AGPL-3.0. Fetched here rather than vendored,
#: so no Raven artifact carries one; what that leaves for anyone who ships an
#: image with them already fetched is in NOTICES.md, under "deepdoc models".
DEEPDOC_REPO = "InfiniFlow/deepdoc"

#: The Hub to read that repository from when huggingface.co cannot be reached,
#: which from mainland China it cannot.
DEEPDOC_MIRROR = mirrors.HF_ENDPOINT


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


CORE_RES = PACKAGE / "core" / "res"
KNOWLEDGE_RES = PACKAGE / "knowledge" / "res"

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

    # Part of the segmenter extra. An install that did not take it has no
    # corpora to fetch, which is a step to skip and not a failure.
    try:
        import nltk
    except ImportError:
        print("  the segmenter is not installed (extra: segmenter); skipping the corpora")
        return 0

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


#: How long a download waits on its host, as it always has.
_TIMEOUT = 120.0
#: How long a host with a mirror behind it gets to accept a connection, and
#: then to send the next bytes, before it counts as unreachable. A dropped
#: packet is waited on rather than refused, and Linux keeps retrying a connect
#: for about two minutes, so the full wait would hold the download that long
#: before its mirror is asked.
_CONNECT_TIMEOUT = 15.0
_READ_TIMEOUT = 30.0


def _host(url: str) -> str:
    return urlsplit(url).hostname or url


def _switching(away_from: str, to: str, exc: BaseException) -> None:
    # The transport's own words when a library wrapped them: huggingface_hub's
    # wrapper says only "check your connection".
    reason = exc.__cause__ or exc
    print(f"  {_host(away_from)} could not be reached ({reason}); fetching from {_host(to)} instead")
    print(f"  (set {mirrors.OPT_OUT_ENV}=1 to never fall back to a mirror)")


def fetch_dictionary(resources: "tuple[Resource, ...]", *, force: bool) -> int:
    """Download the tokenizer's dictionary. Returns how many files were written.

    From its publisher, and through jsDelivr when the publisher cannot be
    reached at all.
    """
    import httpx

    hosts = [DICTIONARY_REPO, DICTIONARY_MIRROR] if mirrors.enabled() else [DICTIONARY_REPO]
    written = 0
    resources[0].into.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=_TIMEOUT) as client:
        for resource in resources:
            while True:
                base, last = hosts[0], len(hosts) == 1
                timeout = httpx.Timeout(_TIMEOUT) if last else httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT)
                try:
                    written += _fetch_dictionary_file(
                        client, f"{base}/{resource.name}", resource, force, last=last, timeout=timeout
                    )
                    break
                except Exception as exc:
                    if last or not mirrors.unreachable(exc):
                        raise
                    _switching(base, hosts[1], exc)
                    # The files after this one go straight to the mirror, rather
                    # than each waiting out the same timeout first.
                    hosts = hosts[1:]
    return written


def _fetch_dictionary_file(
    client: Any, url: str, resource: Resource, force: bool, *, last: bool, timeout: object
) -> int:
    import httpx

    expected: int | None = None
    try:
        head = client.head(url, timeout=timeout)
        expected = int(head.headers.get("content-length") or 0) or None
    except httpx.HTTPError as exc:
        # The size is a convenience, so failing to learn it is no reason to
        # stop -- unless the host could not be reached at all and another one
        # is waiting, when the download would only spend a second timeout
        # finding that out.
        if not last and mirrors.unreachable(exc):
            raise
    if not force and _present(resource, expected):
        print(f"  {resource.path.name}: already here ({resource.purpose})")
        return 0
    print(f"  {resource.path.name}: downloading {_human(expected or 0)} ({resource.purpose})")
    # To a temporary name first: a half-written dictionary that keeps the real
    # name is one every later run treats as present.
    staged = resource.path.with_suffix(resource.path.suffix + ".part")
    with client.stream("GET", url, timeout=timeout) as response:
        response.raise_for_status()
        with staged.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)
    staged.replace(resource.path)
    return 1


def warm_dictionary() -> None:
    """Build the tokenizer's trie cache, so the first query does not.

    Best effort: a failure here costs six seconds later, not correctness, and
    an install must not stop because a cache could not be written.

    The import is inside the guard, not above it. The segmenter is an extra --
    datrie compiles from source on every platform -- so an install that did
    not take it reaches this with the module unimportable, and that is a step
    to skip rather than an error to raise.
    """
    try:
        from raven.core import tokenizer as tok

        cache = tok.dictionary_path().with_suffix(".txt.trie")
        if cache.is_file():
            print("  index: already built")
            return
        print("  index: building (a few seconds)")
        tok.Tokenizer()
    except ImportError:
        print("  index: the segmenter is not installed (extra: segmenter); skipping", file=sys.stderr)
        return
    except Exception as exc:  # noqa: BLE001 - the dictionary still works without the cache
        print(f"  index: could not be built ({exc}); it will be built on first use", file=sys.stderr)
        return
    if cache.is_file():
        print(f"  index: built, {_human(cache.stat().st_size)}")


def _hub_mirror_allowed(exc: BaseException) -> bool:
    """Whether a failure to reach the Hub may be retried on its mirror.

    Not when HF_ENDPOINT names a Hub: that is the operator's choice of host,
    and the mirror would replace it with ours.
    """
    return mirrors.enabled() and not os.environ.get("HF_ENDPOINT") and mirrors.unreachable(exc)


def fetch_deepdoc(resources: "tuple[Resource, ...]", *, force: bool) -> int:
    """Download the vision models. Returns how many files were written.

    From the Hub huggingface_hub is configured for, and from its mirror once
    that Hub cannot be reached at all.
    """
    from huggingface_hub import HfApi, hf_hub_download, model_info

    KNOWLEDGE_RES.mkdir(parents=True, exist_ok=True)
    # None is the Hub huggingface_hub would pick on its own, so until a
    # fallback happens every call below is the one this function always made.
    endpoint: str | None = None
    sizes = {}
    try:
        sizes = {f.rfilename: f.size for f in model_info(DEEPDOC_REPO, files_metadata=True).siblings}
    except Exception as exc:  # noqa: BLE001 - the index is a convenience, not the download
        if not _hub_mirror_allowed(exc):
            print(f"  (could not read the published sizes: {exc})")
        else:
            _switching("https://huggingface.co", DEEPDOC_MIRROR, exc)
            endpoint = DEEPDOC_MIRROR
            try:
                # Without the user's token, like every call to the mirror (see _hub_download).
                info = HfApi(endpoint=endpoint, token=False).model_info(DEEPDOC_REPO, files_metadata=True)
                sizes = {f.rfilename: f.size for f in info.siblings}
            except Exception as again:  # noqa: BLE001 - as above
                print(f"  (could not read the published sizes: {again})")

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
        try:
            cached = _hub_download(hf_hub_download, resource.name, endpoint)
        except Exception as exc:
            if endpoint is not None or not _hub_mirror_allowed(exc):
                raise
            _switching("https://huggingface.co", DEEPDOC_MIRROR, exc)
            endpoint = DEEPDOC_MIRROR
            cached = _hub_download(hf_hub_download, resource.name, endpoint)
        shutil.copyfile(cached, resource.path)
        written += 1
    return written


def _hub_download(download: "Callable[..., str]", filename: str, endpoint: str | None) -> str:
    # huggingface_hub sends a logged-in user's token to whatever endpoint it is
    # given. That token is for huggingface.co, and these models are public, so
    # the mirror is asked without one.
    extra = {} if endpoint is None else {"endpoint": endpoint, "token": False}
    return download(repo_id=DEEPDOC_REPO, filename=filename, **extra)


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
