"""``skillhub.*`` RPC handlers — search and install skills from SkillHub.

Four methods back the GUI's skill marketplace:

* ``skillhub.search`` — the hub's ``GET /openapi/v1/skills/search`` (paged, filterable).
* ``skillhub.detail`` — one skill plus its SKILL.md body.
* ``skillhub.install`` — download the zip and unpack it into the workspace.
* ``skillhub.remove`` — delete a previously installed skill directory.

The client never talks to the hub itself: the browser has no credentials, the
hub sends no CORS headers, and installing means writing files. The base URL is
``RAVEN_SKILLHUB_URL`` or the default below.

Installed skills land in ``<workspace>/skills/<name>/``, which is the pool
``SkillRegistry`` already scans, next to a ``.skillhub.json`` marker recording
which hub entry the directory came from -- that is how ``installed`` is
answered for search results, and what keeps ``remove`` from touching a
hand-written skill.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from loguru import logger
from pydantic import ValidationError

from raven.config.loader import load_config
from raven.skill_hub.client import ALLOWED_SUFFIXES, MAX_ZIP_ENTRY_BYTES, MAX_ZIP_TOTAL_BYTES
from raven.tui_rpc.errors import ConfigValidationError, InternalError
from raven.tui_rpc.models import (
    SkillhubDetailParams,
    SkillhubInstallParams,
    SkillhubRemoveParams,
    SkillhubSearchParams,
)

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher

DEFAULT_BASE_URL = "https://skillhub.evermind.ai"
MARKER = ".skillhub.json"
_TIMEOUT = 20.0
# A skill is documentation plus a few scripts. The caps are what separates that
# from a zip bomb: refuse rather than fill the user's disk.
_MAX_ZIP_BYTES = 20 * 1024 * 1024
_MAX_UNPACKED_BYTES = MAX_ZIP_TOTAL_BYTES
_MAX_MEMBERS = 2000
# A metadata response is a page of records; a hub that answers with 400 MB of
# JSON would otherwise be buffered whole into the gateway process.
_MAX_JSON_BYTES = 8 * 1024 * 1024
# Total wall clock for one download. httpx's timeout is per-read, so without this
# a hub can hold the transaction open one byte at a time.
_DOWNLOAD_DEADLINE = 180.0
# An install lock older than this outlived any download the deadline allows, so
# its holder is gone rather than slow.
_LOCK_STALE_S = 2 * _DOWNLOAD_DEADLINE


def _base_url() -> str:
    from raven.plughub.trust import HubTrustError, hub_endpoint

    try:
        return hub_endpoint(os.environ.get("RAVEN_SKILLHUB_URL"), DEFAULT_BASE_URL, what="RAVEN_SKILLHUB_URL")
    except HubTrustError as exc:
        raise ConfigValidationError(str(exc)) from exc


def _parse(model_cls: type, params: dict) -> Any:
    try:
        return model_cls.model_validate(params)
    except ValidationError as exc:
        raise ConfigValidationError(
            f"invalid params for {model_cls.__name__}",
            data={"errors": exc.errors(include_url=False)},
        ) from exc


def _skills_dir() -> Path:
    return Path(load_config().workspace_path) / "skills"


def _refresh_pool(agent_loop_factory) -> None:
    """Drop the running loop's skill cache so the new directory is visible now.

    The registry has a file watcher, but it is not guaranteed to have fired by
    the time the client re-reads ``ext.list`` right after an install -- which
    would show the user an install that apparently did nothing.
    """
    if agent_loop_factory is None:
        return
    try:
        loop = agent_loop_factory()
        catalog = getattr(getattr(loop, "context", None), "skills", None)
        if catalog is not None:
            catalog.invalidate_skill_cache()
    except Exception:  # best effort: a stale cache is not worth failing the install
        pass


def _installed_index() -> dict[str, dict]:
    """Map hub id and skill_id to the local directory that came from it."""
    out: dict[str, dict] = {}
    root = _skills_dir()
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        marker = child / MARKER
        # A dotted directory is never a skill, and an install in flight has a
        # staging directory here whose marker is already written -- indexing that
        # would answer a concurrent search with a name about to stop existing.
        if child.name.startswith(".") or not child.is_dir() or not marker.is_file():
            continue
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entry = {"name": child.name, "path": str(child)}
        for key in ("id", "skill_id"):
            value = data.get(key)
            if value:
                out[str(value)] = entry
    return out


async def _hub_json(path: str, params: dict | None = None) -> Any:
    """One metadata GET against the configured hub, unwrapped.

    Capped and non-redirecting: the endpoint is the operator's own URL, so it has
    no business pointing raven somewhere else, and a metadata body has no reason
    to be larger than a page of records.
    """
    url = f"{_base_url()}{path}"
    try:
        async with asyncio.timeout(_DOWNLOAD_DEADLINE):
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
                async with client.stream("GET", url, params=params) as resp:
                    if 300 <= resp.status_code < 400:
                        raise ConfigValidationError(
                            "the skill hub redirected a metadata request; point RAVEN_SKILLHUB_URL at the hub itself",
                            data={"url": url},
                        )
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) > _MAX_JSON_BYTES:
                            raise InternalError(
                                f"the skill hub sent more than {_MAX_JSON_BYTES} bytes of metadata",
                                data={"url": url},
                            )
                    return _unwrap_body(resp.status_code, bytes(buf))
    except TimeoutError as exc:
        raise InternalError(
            f"the skill hub did not answer within {_DOWNLOAD_DEADLINE:.0f}s", data={"url": url}
        ) from exc
    except httpx.HTTPError as exc:
        raise InternalError(f"skill hub unreachable: {exc}", data={"url": url}) from exc


def _unwrap_body(status_code: int, raw: bytes) -> Any:
    """Unwrap the hub's envelope, turning its error codes into RPC errors."""
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise InternalError("skill hub returned a non-JSON body", data={"status": status_code}) from exc
    if not isinstance(body, dict):
        raise InternalError("skill hub returned an unexpected body")
    status = body.get("status")
    if status not in (0, None):
        raise ConfigValidationError(
            str(body.get("error") or "skill hub rejected the request"),
            data={"hub_status": status, "request_id": body.get("requestId")},
        )
    if status_code >= 400:
        raise InternalError(f"skill hub HTTP {status_code}", data={"body": str(body)[:400]})
    return body.get("result")


def _item(raw: dict, installed: dict[str, dict]) -> dict:
    hit = installed.get(str(raw.get("id"))) or installed.get(str(raw.get("skill_id")))
    tags = raw.get("tags")
    return {
        "id": str(raw.get("id") or ""),
        "skill_id": str(raw.get("skill_id") or ""),
        "name": str(raw.get("name") or ""),
        "description": str(raw.get("description") or "")[:400],
        "source": str(raw.get("source") or ""),
        "source_url": str(raw.get("source_url") or ""),
        "category": str(raw.get("category") or ""),
        "quality_score": float(raw.get("quality_score") or 0.0),
        "install_count": int(raw.get("install_count") or 0),
        "github_star": int(raw.get("github_star") or 0),
        "license": str(raw.get("license") or ""),
        "tags": [str(t) for t in tags][:12] if isinstance(tags, list) else [],
        "installed": bool(hit),
        "installed_name": hit["name"] if hit else "",
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def skillhub_search(params: dict) -> dict:
    parsed = _parse(SkillhubSearchParams, params)
    # ``/skills/search``, not ``/skills``: the latter is a fixed semantic top-10
    # that ignores every filter, while this one paginates the whole corpus and
    # honours category / tags / min_score.
    query: dict[str, Any] = {"page": parsed.page, "limit": parsed.limit}
    if parsed.query:
        query["q"] = parsed.query
    if parsed.category:
        query["category"] = parsed.category
    if parsed.tags:
        query["tags"] = parsed.tags
    if parsed.min_score is not None:
        query["min_score"] = parsed.min_score
    result = await _hub_json("/openapi/v1/skills/search", query)
    if not isinstance(result, dict):
        result = {}
    raw_items = result.get("items")
    installed = await asyncio.to_thread(_installed_index)
    items = [_item(r, installed) for r in (raw_items or []) if isinstance(r, dict)]
    # The hub ranks query results by relevance and ignores sort params, so
    # order each page by score here; the stable sort keeps relevance as the
    # tiebreak.
    items.sort(key=lambda i: i.get("quality_score") or 0.0, reverse=True)
    return {
        "items": items,
        "total": int(result.get("total") or len(items)),
        "page": int(result.get("page") or parsed.page),
        "limit": int(result.get("limit") or parsed.limit),
        "base_url": _base_url(),
    }


async def skillhub_detail(params: dict) -> dict:
    parsed = _parse(SkillhubDetailParams, params)
    raw = await _hub_json(f"/openapi/v1/skills/{quote(parsed.id, safe='')}") or {}
    if not isinstance(raw, dict):
        raise InternalError("skill hub returned an unexpected detail body")
    installed = await asyncio.to_thread(_installed_index)
    out = _item(raw, installed)
    files = raw.get("files")
    sub = raw.get("subscores") if isinstance(raw.get("subscores"), dict) else {}
    flags = sub.get("flags")
    out.update(
        {
            "files": [str(f) for f in files][:200] if isinstance(files, list) else [],
            "skill_md": str(raw.get("skill_md") or "")[:20000],
            "body_tokens": int(raw.get("body_tokens") or 0),
            "subscores": {
                "utility": int(sub.get("utility") or 0),
                "robustness": int(sub.get("robustness") or 0),
                "safety": int(sub.get("safety") or 0),
                "flags": [str(f) for f in flags][:12] if isinstance(flags, list) else [],
            },
        }
    )
    return out


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Members that are safe to extract, with the wrapper directory stripped.

    Rejects absolute paths and ``..`` segments (zip slip) and refuses a zip
    whose uncompressed size or member count is out of proportion to a skill.
    """
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos:
        raise ConfigValidationError("the downloaded skill archive is empty")
    if len(infos) > _MAX_MEMBERS:
        raise ConfigValidationError(f"the skill archive has too many files ({len(infos)})")
    total = sum(max(0, i.file_size) for i in infos)
    if total > _MAX_UNPACKED_BYTES:
        raise ConfigValidationError(f"the skill archive unpacks to {total} bytes, which is too large")
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ConfigValidationError(f"the skill archive contains an unsafe path: {info.filename}")
        # zipfile raises a bare RuntimeError for an encrypted member, which would
        # reach the caller as internal_error. A skill nobody can read is a bad
        # answer from the hub, and it is refused as one.
        if info.flag_bits & 0x1:
            raise ConfigValidationError(f"the skill archive is encrypted: {info.filename}")
    return infos


def _allowed_member(rel: str, info: zipfile.ZipInfo) -> bool:
    """Whether one member is the kind of file a skill is made of.

    Same policy as :mod:`raven.skill_hub.client`, including its choice to skip
    rather than refuse: a stray binary asset should not make an entire skill
    uninstallable. What is skipped is reported back to the caller, so the gap
    between "the archive had 12 files" and "9 landed" is visible rather than
    silent.
    """
    if Path(rel).suffix.lower() not in ALLOWED_SUFFIXES:
        return False
    return info.file_size <= MAX_ZIP_ENTRY_BYTES


def _strip_root(names: list[str]) -> str:
    """The single wrapper directory every hub zip has, or '' when it has none."""
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(tops) == 1 and all("/" in n for n in names):
        return tops.pop()
    return ""


def _extract(data: bytes, target: Path) -> tuple[list[str], list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = _safe_members(zf)
        names = [i.filename.replace("\\", "/") for i in infos]
        root = _strip_root(names)
        written: list[str] = []
        skipped: list[str] = []
        for info, name in zip(infos, names, strict=True):
            rel = name[len(root) + 1 :] if root else name
            if not rel:
                continue
            dest = (target / rel).resolve()
            if not dest.is_relative_to(target.resolve()):
                raise ConfigValidationError(f"the skill archive contains an unsafe path: {name}")
            if not _allowed_member(rel, info):
                skipped.append(rel)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            written.append(rel)
    if not any(r.split("/")[-1] == "SKILL.md" for r in written):
        raise ConfigValidationError("the downloaded archive has no SKILL.md, so it is not a skill")
    return sorted(written), sorted(skipped)


def _safe_name(name: str) -> str:
    cleaned = "".join(c for c in name.strip() if c.isalnum() or c in "-_.")
    cleaned = cleaned.lstrip(".") or "skill"
    return cleaned[:80]


async def _fetch_capped(url: str, *, check, params: dict | None = None, what: str = "the download") -> tuple:
    """GET with a hard ceiling on bytes and on elapsed time.

    Three properties the plain ``client.get`` did not have:

    * streamed, so the size cap is a refusal rather than a measurement taken
      once the oversized body is already in memory;
    * a total deadline, because ``httpx.Timeout`` is per-operation -- a hub
      trickling one byte per read holds the handler (and, through a plugin's
      skill piece, the whole install transaction) open indefinitely;
    * redirects followed by hand. With ``follow_redirects=True`` a URL that
      passed ``check`` can 302 anywhere, which erases the check; each hop is
      validated first.
    """
    from raven.plughub.trust import MAX_REDIRECTS, HubTrustError, redirect_target

    buf = bytearray()
    target = url
    try:
        async with asyncio.timeout(_DOWNLOAD_DEADLINE):
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    async with client.stream("GET", target, params=params) as resp:
                        location = resp.headers.get("location") if 300 <= resp.status_code < 400 else None
                        if location is None:
                            ctype = resp.headers.get("content-type", "")
                            if resp.status_code < 400:
                                async for chunk in resp.aiter_bytes():
                                    buf += chunk
                                    if len(buf) > _MAX_ZIP_BYTES:
                                        raise ConfigValidationError(
                                            f"{what} exceeded {_MAX_ZIP_BYTES} bytes and was stopped",
                                            data={"url": target},
                                        )
                            return resp.status_code, ctype, bytes(buf)
                    target = redirect_target(target, location, check=check, what=what)
                    params = None
        raise ConfigValidationError(f"{what} redirected more than {MAX_REDIRECTS} times")
    except HubTrustError as exc:
        raise ConfigValidationError(str(exc)) from exc
    except TimeoutError as exc:
        raise InternalError(f"{what} did not finish within {_DOWNLOAD_DEADLINE:.0f}s", data={"url": url}) from exc
    except httpx.InvalidURL as exc:
        # Not an HTTPError, so it would otherwise escape as internal_error: the
        # hub handed back something that is not a usable URL at all.
        raise ConfigValidationError(f"{what} is not a usable URL: {exc}", data={"url": target}) from exc
    except httpx.HTTPError as exc:
        raise InternalError(f"skill download failed: {exc}", data={"url": target}) from exc


def _marker_owner(marker: Path) -> str:
    try:
        return str(json.loads(marker.read_text(encoding="utf-8")).get("id") or "")
    except (OSError, ValueError):
        return ""


def _take_lock(lock: Path) -> bool:
    """Claim the lock directory, or report that somebody else holds it."""
    try:
        os.mkdir(lock)
        return True
    except FileExistsError:
        return False


@contextmanager
def _install_lock(root: Path, name: str):
    """Serialise installs of one skill name across processes.

    Checking the target and swapping it are two steps, and between them another
    install can move the directory away -- after which the second install's
    "nothing here" reading is wrong and the first one's rollback deletes the
    only remaining copy. ``mkdir`` is the atomic primitive available on every
    filesystem raven runs on.
    """
    root.mkdir(parents=True, exist_ok=True)
    lock = root / f".{name}.lock"
    if not _take_lock(lock):
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            # The holder released it between the two attempts, so there is nothing
            # to be blocked by -- reporting "already running" about an install that
            # has finished would be a lie the caller cannot act on.
            age = _LOCK_STALE_S + 1
        if age < _LOCK_STALE_S and not _take_lock(lock):
            raise ConfigValidationError(
                f"an install of {name} is already running; try again in a moment",
                data={"name": name},
            )
        if age >= _LOCK_STALE_S:
            # Older than any install could legitimately take, so the holder died
            # mid-swap (a crash between the two renames leaves exactly this).
            #
            # The takeover has to be one atomic step. Removing the directory and
            # then creating it lets every process that saw the same stale lock
            # proceed together -- which is the interleaving the lock exists to
            # prevent, and it stayed possible while the takeover was a log line
            # and a fall-through. Only the process whose rename succeeds owns it.
            logger.warning("skillhub: taking over a stale install lock for '{}' ({:.0f}s old)", name, age)
            claimed = lock.with_name(f"{lock.name}.taken.{os.getpid()}")
            try:
                os.rename(lock, claimed)
            except OSError:
                raise ConfigValidationError(
                    f"an install of {name} is already running; try again in a moment",
                    data={"name": name},
                ) from None
            shutil.rmtree(claimed, ignore_errors=True)
            if not _take_lock(lock):
                raise ConfigValidationError(
                    f"an install of {name} is already running; try again in a moment",
                    data={"name": name},
                )
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


async def skillhub_install(params: dict, *, agent_loop_factory=None, if_absent: bool = False) -> dict:
    """Download one hub skill and unpack it into the pool.

    ``if_absent`` refuses when the directory already exists instead of upgrading
    it. The plugin transaction passes it: a rollback there deletes the skill
    directory, so the transaction must only ever be undoing something it created
    -- otherwise a hostile catalogue entry naming a skill the user already had
    turns a failed install into deletion of their work.
    """
    parsed = _parse(SkillhubInstallParams, params)
    hub_id = quote(parsed.id, safe="")
    detail = await _hub_json(f"/openapi/v1/skills/{hub_id}") or {}
    if not isinstance(detail, dict) or not detail.get("name"):
        raise ConfigValidationError("skill not found on the hub", data={"id": parsed.id})

    from raven.plughub.trust import HubTrustError, require_public_https

    url = f"{_base_url()}/openapi/v1/skills/{hub_id}/download"
    # Redirects are expected here (the hub hands the bytes to a CDN), so they are
    # followed -- but every hop has to pass the same check as a zip_url would.
    status, ctype, data = await _fetch_capped(
        url, params={"source": "raven"}, check=require_public_https, what="the skill download"
    )
    if status >= 400:
        raise InternalError(f"skill download failed with HTTP {status}")
    if "json" in ctype:
        # Contract per the hub's OpenAPI: a presigned URL instead of the bytes.
        # The hub picks that URL, so it is checked before raven follows it.
        meta = _unwrap_body(status, data) or {}
        zip_url = meta.get("zip_url") if isinstance(meta, dict) else None
        if not zip_url:
            raise InternalError("skill download returned neither a zip nor a zip_url")
        try:
            require_public_https(str(zip_url), what="the hub's zip_url")
        except HubTrustError as exc:
            raise ConfigValidationError(str(exc)) from exc
        status, _, data = await _fetch_capped(str(zip_url), check=require_public_https, what="the skill download")
        if status >= 400:
            raise InternalError(f"skill download failed with HTTP {status}")

    entry_id = str(detail.get("id") or parsed.id)
    name = _safe_name(str(detail.get("name")))
    root = _skills_dir()
    target = root / name
    marker_text = json.dumps(
        {
            "id": entry_id,
            "skill_id": str(detail.get("skill_id") or ""),
            "name": name,
            "source": str(detail.get("source") or ""),
            "hub": _base_url(),
            "installed_at": int(time.time() * 1000),
        },
        ensure_ascii=False,
        indent=1,
    )

    def _write() -> tuple[list[str], list[str], bool]:
        """Check the target, unpack beside it, then swap -- all under the lock.

        Extracting in place would mean deleting a working skill before knowing
        whether its replacement unpacks, so a corrupt download would take the
        installed copy with it. The checks live in here rather than at the caller
        because a check the lock does not cover is a check another install can
        invalidate.
        """
        with _install_lock(root, name):
            replaced = target.exists()
            if replaced:
                if not (target / MARKER).is_file():
                    raise ConfigValidationError(
                        f"a local skill named {name} already exists; rename it first",
                        data={"path": str(target)},
                    )
                if if_absent:
                    raise ConfigValidationError(
                        f"a skill named {name} is already installed; remove it before installing this plugin",
                        data={"path": str(target)},
                    )
                # The directory name comes from the hub's `name`, so two entries
                # can claim it. Reinstalling the same entry is an upgrade; a
                # different entry would be a silent replacement.
                owner = _marker_owner(target / MARKER)
                if owner and owner != entry_id:
                    raise ConfigValidationError(
                        f"the skill directory {name} already holds hub entry {owner}; remove it first",
                        data={"path": str(target), "owner": owner},
                    )

            staging = Path(tempfile.mkdtemp(prefix=f".{name}.new.", dir=root))
            backup: Path | None = None
            try:
                files, skipped = _extract(data, staging)
                (staging / MARKER).write_text(marker_text, encoding="utf-8")
                if target.exists():
                    backup = Path(tempfile.mkdtemp(prefix=f".{name}.old.", dir=root)) / name
                    target.rename(backup)
                try:
                    staging.rename(target)
                except BaseException:
                    if backup is not None:
                        backup.rename(target)
                    raise
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            finally:
                # Only once something is in place. If the swap failed *and* the
                # restore failed too, that backup is the user's only copy of the
                # skill, and deleting it turns a failed upgrade into data loss.
                if backup is not None and target.exists():
                    shutil.rmtree(backup.parent, ignore_errors=True)
                elif backup is not None:
                    logger.error(
                        "skillhub: {} could not be restored after a failed swap; its files are in {}",
                        name,
                        backup,
                    )
            return files, skipped, replaced

    try:
        files, skipped, replaced = await asyncio.to_thread(_write)
    except zipfile.BadZipFile as exc:
        # A hub serving an error page, or a truncated body, is a bad answer -- not
        # a raven fault to report with a traceback.
        raise ConfigValidationError(
            f"the download is not a usable zip archive ({exc})", data={"id": parsed.id}
        ) from exc
    except OSError as exc:
        raise InternalError(f"the skill could not be written: {exc}", data={"path": str(target)}) from exc
    await asyncio.to_thread(_refresh_pool, agent_loop_factory)
    return {
        "name": name,
        "path": str(target),
        "files": files[:200],
        "skipped": skipped[:200],
        "replaced": replaced,
        "size_bytes": len(data),
        "install_count": int(detail.get("install_count") or 0) + 1,
    }


async def skillhub_remove(params: dict, *, agent_loop_factory=None) -> dict:
    parsed = _parse(SkillhubRemoveParams, params)
    name = _safe_name(parsed.name)
    root = _skills_dir().resolve()
    target = (root / name).resolve()
    if target.parent != root or not target.is_dir():
        raise ConfigValidationError("no such installed skill", data={"name": parsed.name})
    if not (target / MARKER).is_file():
        raise ConfigValidationError(
            "that skill was not installed from the hub, so it is not removed here",
            data={"name": name},
        )
    await asyncio.to_thread(shutil.rmtree, target)
    await asyncio.to_thread(_refresh_pool, agent_loop_factory)
    return {"removed": True, "name": name}


def register_skillhub_methods(dispatcher: "Dispatcher", *, agent_loop_factory=None) -> None:
    """Register the four ``skillhub.*`` handlers on a dispatcher instance."""

    def bind(fn):
        async def _h(params: dict) -> dict:
            return await fn(params, agent_loop_factory=agent_loop_factory)

        return _h

    dispatcher.register("skillhub.search", skillhub_search)
    dispatcher.register("skillhub.detail", skillhub_detail)
    dispatcher.register("skillhub.install", bind(skillhub_install))
    dispatcher.register("skillhub.remove", bind(skillhub_remove))


__all__ = [
    "skillhub_search",
    "skillhub_detail",
    "skillhub_install",
    "skillhub_remove",
    "register_skillhub_methods",
]
