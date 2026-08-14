"""``skillhub.*`` handlers — install path, archive safety, installed index.

Network is always stubbed: these cover what the handlers do with whatever the
hub returns, including archives that a hostile hub could serve.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods import skillhub


class _Resp:
    """Minimal stand-in for the parts of ``httpx.Response`` the module reads.

    ``content`` is derived from ``json_body`` rather than left empty: a real
    response always carries the bytes, and a fake that answers only ``json()``
    lets a handler that reads bytes pass a test it would fail in production.
    """

    def __init__(self, *, json_body=None, content=b"", ctype="application/json", status=200, location=None):
        self._json = json_body
        self.content = json.dumps(json_body).encode() if json_body is not None else content
        self.headers = {"content-type": ctype}
        if location is not None:
            self.headers["location"] = location
        self.status_code = status

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    # -- streaming half of the same response ---------------------------------
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self):
        # Two chunks so a cap that only looks at the final size cannot pass.
        mid = max(1, len(self.content) // 2)
        yield self.content[:mid]
        yield self.content[mid:]


def _envelope(result):
    return {"error": "success", "requestId": "r", "status": 0, "result": result}


def _zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buf.getvalue()


DETAIL = {
    "id": "uuid-1",
    "skill_id": "acme/pack/demo-skill",
    "name": "demo-skill",
    "description": "d",
    "source": "acme/pack",
    "category": "CODING",
    "quality_score": 0.7,
    "install_count": 3,
    "tags": ["a", "b"],
    "files": ["SKILL.md"],
    "skill_md": "# demo",
    "subscores": {"utility": 9, "robustness": 8, "safety": 7, "flags": ["no_steps"]},
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(skillhub, "_skills_dir", lambda: tmp_path / "skills")
    return tmp_path / "skills"


def _stub_hub(monkeypatch, *, detail=None, zip_bytes=b"", search=None, download=None, seen=None):
    async def _json(path, params=None):
        if path.endswith("/skills/search"):
            return search or {"items": [], "total": 0}
        return detail if detail is not None else DETAIL

    monkeypatch.setattr(skillhub, "_hub_json", _json)

    class _Client:
        def __init__(self, *a, **k):
            # httpx takes follow_redirects on the client and lets a request
            # override it; assert on the effective value, since the module must
            # follow hops itself so each one can be re-checked.
            self._follow = k.get("follow_redirects", False)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, params=None, follow_redirects=None):
            effective = self._follow if follow_redirects is None else follow_redirects
            assert effective is False, "the module must follow redirects itself"
            if seen is not None:
                seen.setdefault("urls", []).append(url)
            if download is not None:
                return download(url)
            return _Resp(content=zip_bytes, ctype="application/zip")

    monkeypatch.setattr(skillhub.httpx, "AsyncClient", _Client)


@pytest.mark.asyncio
async def test_install_unpacks_and_marks(workspace, monkeypatch):
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# demo", "demo-skill/refs/a.md": "a"}))

    out = await skillhub.skillhub_install({"id": "uuid-1"})

    assert out["name"] == "demo-skill"
    assert out["files"] == ["SKILL.md", "refs/a.md"] or sorted(out["files"]) == ["SKILL.md", "refs/a.md"]
    target = workspace / "demo-skill"
    # The wrapper directory is stripped: SKILL.md sits at the skill root, which
    # is where SkillRegistry looks for it.
    assert (target / "SKILL.md").read_text() == "# demo"
    assert (target / "refs" / "a.md").is_file()
    marker = json.loads((target / skillhub.MARKER).read_text())
    assert marker["id"] == "uuid-1"
    assert marker["skill_id"] == "acme/pack/demo-skill"


@pytest.mark.asyncio
async def test_search_flags_installed(workspace, monkeypatch):
    _stub_hub(
        monkeypatch,
        zip_bytes=_zip({"demo-skill/SKILL.md": "# demo"}),
        search={"items": [DETAIL, {**DETAIL, "id": "uuid-2", "skill_id": "other/x", "name": "other"}], "total": 2},
    )
    await skillhub.skillhub_install({"id": "uuid-1"})

    r = await skillhub.skillhub_search({"query": "demo"})

    by_name = {i["name"]: i for i in r["items"]}
    assert by_name["demo-skill"]["installed"] is True
    assert by_name["demo-skill"]["installed_name"] == "demo-skill"
    assert by_name["other"]["installed"] is False


@pytest.mark.asyncio
async def test_remove_only_touches_hub_installs(workspace, monkeypatch):
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# demo"}))
    await skillhub.skillhub_install({"id": "uuid-1"})
    handmade = workspace / "mine"
    handmade.mkdir(parents=True)
    (handmade / "SKILL.md").write_text("# mine")

    with pytest.raises(ConfigValidationError):
        await skillhub.skillhub_remove({"name": "mine"})
    assert handmade.is_dir()

    assert (await skillhub.skillhub_remove({"name": "demo-skill"}))["removed"] is True
    assert not (workspace / "demo-skill").exists()


@pytest.mark.asyncio
async def test_install_refuses_to_overwrite_a_local_skill(workspace, monkeypatch):
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# demo"}))
    (workspace / "demo-skill").mkdir(parents=True)
    (workspace / "demo-skill" / "SKILL.md").write_text("# hand written")

    with pytest.raises(ConfigValidationError):
        await skillhub.skillhub_install({"id": "uuid-1"})
    assert (workspace / "demo-skill" / "SKILL.md").read_text() == "# hand written"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "members",
    [
        {"../escape.md": "x", "demo-skill/SKILL.md": "# demo"},
        {"/abs.md": "x", "demo-skill/SKILL.md": "# demo"},
    ],
)
async def test_install_rejects_zip_slip(workspace, monkeypatch, members, tmp_path):
    _stub_hub(monkeypatch, zip_bytes=_zip(members))

    with pytest.raises(ConfigValidationError):
        await skillhub.skillhub_install({"id": "uuid-1"})
    assert not (tmp_path / "escape.md").exists()
    assert not (workspace / "demo-skill").exists()


@pytest.mark.asyncio
async def test_install_rejects_archive_without_skill_md(workspace, monkeypatch):
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/README.md": "nope"}))

    with pytest.raises(ConfigValidationError):
        await skillhub.skillhub_install({"id": "uuid-1"})
    assert not (workspace / "demo-skill").exists()


@pytest.mark.asyncio
async def test_install_rejects_oversize_archive(workspace, monkeypatch):
    monkeypatch.setattr(skillhub, "_MAX_ZIP_BYTES", 10)
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# demo" * 50}))

    with pytest.raises(ConfigValidationError):
        await skillhub.skillhub_install({"id": "uuid-1"})


@pytest.mark.asyncio
async def test_detail_normalizes_missing_subscores(workspace, monkeypatch):
    _stub_hub(monkeypatch, detail={**DETAIL, "subscores": None, "license": None})

    d = await skillhub.skillhub_detail({"id": "uuid-1"})

    assert d["subscores"] == {"utility": 0, "robustness": 0, "safety": 0, "flags": []}
    assert d["license"] == ""


@pytest.mark.asyncio
async def test_search_forwards_filters_and_paging(workspace, monkeypatch):
    seen = {}

    async def _json(path, params=None):
        seen["path"] = path
        seen["params"] = params
        return {"items": [], "total": 93007, "page": 3, "limit": 24}

    monkeypatch.setattr(skillhub, "_hub_json", _json)

    r = await skillhub.skillhub_search(
        {"query": "git", "category": "DEV", "tags": "vitest,ci", "min_score": 0.7, "page": 3, "limit": 24}
    )

    assert seen["path"] == "/openapi/v1/skills/search"
    assert seen["params"] == {
        "page": 3,
        "limit": 24,
        "q": "git",
        "category": "DEV",
        "tags": "vitest,ci",
        "min_score": 0.7,
    }
    assert (r["total"], r["page"], r["limit"]) == (93007, 3, 24)


@pytest.mark.asyncio
async def test_search_omits_empty_filters(workspace, monkeypatch):
    seen = {}

    async def _json(path, params=None):
        seen["params"] = params
        return {"items": [], "total": 0}

    monkeypatch.setattr(skillhub, "_hub_json", _json)

    await skillhub.skillhub_search({})

    # min_score=None and empty strings must not be sent: the hub 422s on them.
    assert seen["params"] == {"page": 1, "limit": 24}


@pytest.mark.asyncio
async def test_search_sorts_page_by_quality_score(workspace, monkeypatch):
    raw = [
        {"id": "a", "name": "mid", "quality_score": 0.7},
        {"id": "b", "name": "top", "quality_score": 0.9},
        {"id": "c", "name": "tie-first", "quality_score": 0.8},
        {"id": "d", "name": "tie-second", "quality_score": 0.8},
        {"id": "e", "name": "unscored"},
    ]

    async def _json(path, params=None):
        return {"items": raw, "total": 5}

    monkeypatch.setattr(skillhub, "_hub_json", _json)

    r = await skillhub.skillhub_search({"query": "anything"})

    # The hub returns relevance order and ignores sort params; each page is
    # reordered by score here, with the stable sort keeping relevance as the
    # tiebreak and unscored items last.
    assert [i["name"] for i in r["items"]] == ["top", "tie-first", "tie-second", "mid", "unscored"]


@pytest.mark.asyncio
async def test_search_surfaces_hub_error_code(workspace, monkeypatch):
    async def _json(path, params=None):
        # The envelope check lives in _unwrap_body, which _hub_json runs; call the
        # real thing so the hub's error code is what reaches the handler.
        return skillhub._unwrap_body(400, json.dumps({"error": "Invalid search params", "status": 60002}).encode())

    monkeypatch.setattr(skillhub, "_hub_json", _json)

    with pytest.raises(ConfigValidationError) as err:
        await skillhub.skillhub_search({"query": "x"})
    assert "Invalid search params" in str(err.value)


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("RAVEN_SKILLHUB_URL", "http://127.0.0.1:8000/")
    assert skillhub._base_url() == "http://127.0.0.1:8000"
    monkeypatch.delenv("RAVEN_SKILLHUB_URL")
    assert skillhub._base_url() == skillhub.DEFAULT_BASE_URL


def test_strip_root_only_when_single_wrapper():
    assert skillhub._strip_root(["pack/SKILL.md", "pack/a/b.md"]) == "pack"
    assert skillhub._strip_root(["SKILL.md", "a/b.md"]) == ""
    assert skillhub._strip_root(["p1/SKILL.md", "p2/SKILL.md"]) == ""


def test_safe_name_strips_path_characters():
    assert skillhub._safe_name("../../etc/passwd") == "etcpasswd"
    assert skillhub._safe_name("  a b/c  ") == "abc"
    assert skillhub._safe_name("...") == "skill"
    assert Path(skillhub._safe_name("ok-name_1.2")).name == "ok-name_1.2"


# ---------------------------------------------------------------------------
# What the archive is allowed to contain, and what an install may destroy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_skips_files_a_skill_is_not_made_of(workspace, monkeypatch):
    _stub_hub(
        monkeypatch,
        zip_bytes=_zip(
            {
                "demo-skill/SKILL.md": "# demo",
                "demo-skill/scripts/run.py": "print(1)",
                "demo-skill/payload.dylib": "MACH-O",
                "demo-skill/payload.so": "ELF",
            }
        ),
    )

    out = await skillhub.skillhub_install({"id": "uuid-1"})

    target = workspace / "demo-skill"
    assert sorted(out["files"]) == ["SKILL.md", "scripts/run.py"]
    # Reported, not silently dropped: "12 files" and "9 landed" must be tellable
    # apart by the caller.
    assert sorted(out["skipped"]) == ["payload.dylib", "payload.so"]
    assert not (target / "payload.dylib").exists()
    assert not (target / "payload.so").exists()


@pytest.mark.asyncio
async def test_install_skips_a_single_oversized_member(workspace, monkeypatch):
    monkeypatch.setattr(skillhub, "MAX_ZIP_ENTRY_BYTES", 32)
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# demo", "demo-skill/big.md": "x" * 400}))

    out = await skillhub.skillhub_install({"id": "uuid-1"})

    assert out["files"] == ["SKILL.md"]
    assert out["skipped"] == ["big.md"]


@pytest.mark.asyncio
async def test_a_failed_reinstall_leaves_the_previous_install_in_place(workspace, monkeypatch):
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# v1", "demo-skill/keep.md": "keep"}))
    await skillhub.skillhub_install({"id": "uuid-1"})

    # Same entry, but this time the hub serves an archive that is not a skill.
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/README.md": "nope"}))
    with pytest.raises(ConfigValidationError):
        await skillhub.skillhub_install({"id": "uuid-1"})

    target = workspace / "demo-skill"
    assert (target / "SKILL.md").read_text() == "# v1"
    assert (target / "keep.md").read_text() == "keep"
    assert json.loads((target / skillhub.MARKER).read_text())["id"] == "uuid-1"
    # No staging or backup directory left behind.
    assert sorted(p.name for p in workspace.iterdir()) == ["demo-skill"]


@pytest.mark.asyncio
async def test_reinstalling_the_same_entry_replaces_its_contents(workspace, monkeypatch):
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# v1", "demo-skill/gone.md": "old"}))
    await skillhub.skillhub_install({"id": "uuid-1"})

    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# v2"}))
    await skillhub.skillhub_install({"id": "uuid-1"})

    target = workspace / "demo-skill"
    assert (target / "SKILL.md").read_text() == "# v2"
    assert not (target / "gone.md").exists()


@pytest.mark.asyncio
async def test_install_refuses_to_take_over_another_hub_entrys_directory(workspace, monkeypatch):
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# mine"}))
    await skillhub.skillhub_install({"id": "uuid-1"})

    # A second entry whose `name` collides with the first. The directory name
    # comes from the hub, so without an ownership check this would replace an
    # unrelated installed skill.
    _stub_hub(
        monkeypatch,
        detail={**DETAIL, "id": "uuid-evil", "skill_id": "evil/pack/demo-skill"},
        zip_bytes=_zip({"demo-skill/SKILL.md": "# theirs"}),
    )
    with pytest.raises(ConfigValidationError) as err:
        await skillhub.skillhub_install({"id": "uuid-evil"})

    assert "uuid-1" in str(err.value.data)
    assert (workspace / "demo-skill" / "SKILL.md").read_text() == "# mine"


# ---------------------------------------------------------------------------
# Where the bytes come from
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_stops_at_the_cap_instead_of_measuring_afterwards(workspace, monkeypatch):
    monkeypatch.setattr(skillhub, "_MAX_ZIP_BYTES", 8)
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# demo" * 200}))

    with pytest.raises(ConfigValidationError) as err:
        await skillhub.skillhub_install({"id": "uuid-1"})
    assert "stopped" in str(err.value)
    assert not (workspace / "demo-skill").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "zip_url",
    [
        "http://cdn.example.com/a.zip",
        "https://127.0.0.1:9000/a.zip",
        "https://localhost/a.zip",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/a.zip",
        "file:///etc/passwd",
    ],
)
async def test_install_refuses_a_presigned_url_it_should_not_follow(workspace, monkeypatch, zip_url):
    reached = []

    def _download(url):
        reached.append(url)
        return _Resp(json_body=_envelope({"zip_url": zip_url}))

    _stub_hub(monkeypatch, download=_download)

    with pytest.raises(ConfigValidationError):
        await skillhub.skillhub_install({"id": "uuid-1"})
    # The refusal happens before the fetch: the second URL was never requested.
    assert reached == [f"{skillhub._base_url()}/openapi/v1/skills/uuid-1/download"]


@pytest.mark.asyncio
async def test_install_follows_a_public_https_presigned_url(workspace, monkeypatch):
    zip_bytes = _zip({"demo-skill/SKILL.md": "# demo"})
    seen: dict = {}

    def _download(url):
        if url.endswith("/download"):
            return _Resp(json_body=_envelope({"zip_url": "https://cdn.example.com/a.zip"}))
        return _Resp(content=zip_bytes, ctype="application/zip")

    _stub_hub(monkeypatch, download=_download, seen=seen)

    out = await skillhub.skillhub_install({"id": "uuid-1"})

    assert out["files"] == ["SKILL.md"]
    assert seen["urls"][-1] == "https://cdn.example.com/a.zip"


@pytest.mark.asyncio
async def test_a_skill_id_with_slashes_stays_one_path_segment(workspace, monkeypatch):
    seen: dict = {}

    async def _json(path, params=None):
        seen["path"] = path
        return DETAIL

    monkeypatch.setattr(skillhub, "_hub_json", _json)

    await skillhub.skillhub_detail({"id": "acme/pack/demo?x=1"})

    assert seen["path"] == "/openapi/v1/skills/acme%2Fpack%2Fdemo%3Fx%3D1"


def test_base_url_refuses_a_plaintext_remote_hub(monkeypatch):
    monkeypatch.setenv("RAVEN_SKILLHUB_URL", "http://hub.example.com")
    with pytest.raises(ConfigValidationError):
        skillhub._base_url()


def test_the_installed_index_ignores_a_staging_directory(workspace):
    """An install in flight writes its marker before the swap, so a hidden
    directory here is a name that is about to stop existing.

    The two entries carry different ids on purpose: with the same id the real
    directory would overwrite the staging one in the map (dots sort first) and
    the test would pass with no guard at all.
    """
    workspace.mkdir(parents=True)
    (workspace / ".demo.new.abc123").mkdir()
    (workspace / ".demo.new.abc123" / skillhub.MARKER).write_text(json.dumps({"id": "uuid-staging"}))
    (workspace / "demo").mkdir()
    (workspace / "demo" / skillhub.MARKER).write_text(json.dumps({"id": "uuid-1"}))

    index = skillhub._installed_index()

    assert index["uuid-1"]["name"] == "demo"
    assert "uuid-staging" not in index


@pytest.mark.asyncio
async def test_a_validated_download_url_cannot_redirect_somewhere_else(workspace, monkeypatch):
    """Following redirects erases the URL check.

    A hub answers with a zip_url that passes require_public_https, then 302s to a
    service on the user's own machine. The check is only worth anything if every
    hop is checked, so the second hop must be refused.
    """
    hops = []

    def _download(url):
        hops.append(url)
        if url.endswith("/download"):
            return _Resp(json_body=_envelope({"zip_url": "https://cdn.example.com/a.zip"}))
        if url == "https://cdn.example.com/a.zip":
            return _Resp(status=302, location="http://127.0.0.1:9001/admin/keys", content=b"")
        return _Resp(content=b"INTERNAL-SECRET", ctype="application/zip")

    _stub_hub(monkeypatch, download=_download)

    with pytest.raises(ConfigValidationError) as err:
        await skillhub.skillhub_install({"id": "uuid-1"})

    assert "127.0.0.1" in str(err.value) or "machine" in str(err.value)
    assert "http://127.0.0.1:9001/admin/keys" not in hops


@pytest.mark.asyncio
async def test_a_download_may_redirect_to_another_public_host(workspace, monkeypatch):
    """The check has to allow the ordinary case: hubs presign to a CDN."""
    zip_bytes = _zip({"demo-skill/SKILL.md": "# demo"})

    def _download(url):
        if url.endswith("/download"):
            return _Resp(status=302, location="https://cdn2.example.com/a.zip", content=b"")
        return _Resp(content=zip_bytes, ctype="application/zip")

    seen: dict = {}
    _stub_hub(monkeypatch, download=_download, seen=seen)

    out = await skillhub.skillhub_install({"id": "uuid-1"})

    assert out["files"] == ["SKILL.md"]
    assert seen["urls"][-1] == "https://cdn2.example.com/a.zip"


@pytest.mark.asyncio
async def test_a_redirect_loop_is_refused_rather_than_followed(workspace, monkeypatch):
    hops = []

    def _download(url):
        hops.append(url)
        return _Resp(status=302, location="https://cdn.example.com/next", content=b"")

    _stub_hub(monkeypatch, download=_download)

    with pytest.raises(ConfigValidationError):
        await skillhub.skillhub_install({"id": "uuid-1"})
    assert len(hops) <= skillhub_max_hops() + 1


def skillhub_max_hops() -> int:
    from raven.plughub.trust import MAX_REDIRECTS

    return MAX_REDIRECTS


@pytest.mark.asyncio
async def test_a_metadata_request_may_not_be_redirected_at_all(workspace, monkeypatch):
    """Unlike a download, a metadata GET hits the operator's own endpoint, which
    has no business pointing raven somewhere else."""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, params=None, follow_redirects=None):
            return _Resp(status=302, location="http://127.0.0.1:9001/", content=b"")

    monkeypatch.setattr(skillhub.httpx, "AsyncClient", _Client)

    with pytest.raises(ConfigValidationError) as err:
        await skillhub.skillhub_search({})
    assert "redirect" in str(err.value)


@pytest.mark.asyncio
async def test_a_flood_of_metadata_is_refused_instead_of_buffered(workspace, monkeypatch):
    monkeypatch.setattr(skillhub, "_MAX_JSON_BYTES", 64)
    read = {"bytes": 0}

    class _Flood(_Resp):
        def __init__(self):
            super().__init__(content=b"", ctype="application/json")

        async def aiter_bytes(self):
            for _ in range(1000):
                read["bytes"] += 1024
                yield b"x" * 1024

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, params=None, follow_redirects=None):
            return _Flood()

    monkeypatch.setattr(skillhub.httpx, "AsyncClient", _Client)

    from raven.rpc.errors import InternalError

    with pytest.raises(InternalError):
        await skillhub.skillhub_search({})
    # Stopped near the cap, not after the whole flood.
    assert read["bytes"] < 8 * 1024


@pytest.mark.asyncio
async def test_a_body_that_is_not_a_zip_is_a_refusal_not_an_internal_error(workspace, monkeypatch):
    """A hub answering with an HTML error page is a bad answer, and the caller
    should hear that rather than a traceback tail."""
    _stub_hub(monkeypatch, zip_bytes=b"<html>503 upstream</html>")

    with pytest.raises(ConfigValidationError) as err:
        await skillhub.skillhub_install({"id": "uuid-1"})
    assert "zip" in str(err.value)


@pytest.mark.asyncio
async def test_a_second_install_of_one_name_is_refused_while_the_first_runs(workspace, monkeypatch):
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# demo"}))
    workspace.mkdir(parents=True)
    (workspace / ".demo-skill.lock").mkdir()

    with pytest.raises(ConfigValidationError) as err:
        await skillhub.skillhub_install({"id": "uuid-1"})
    assert "already running" in str(err.value)


@pytest.mark.asyncio
async def test_a_stale_lock_does_not_wedge_installs_forever(workspace, monkeypatch):
    import os

    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# demo"}))
    workspace.mkdir(parents=True)
    lock = workspace / ".demo-skill.lock"
    lock.mkdir()
    old = 1_600_000_000
    os.utime(lock, (old, old))

    out = await skillhub.skillhub_install({"id": "uuid-1"})

    assert out["files"] == ["SKILL.md"]
    assert not lock.exists()


@pytest.mark.asyncio
async def test_a_plugin_install_may_not_replace_a_skill_the_user_already_had(workspace, monkeypatch):
    """`if_absent` is what keeps a plugin rollback from deleting the user's work.

    The rollback for a skill piece removes the directory, so the transaction must
    only ever undo a directory it created.
    """
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# mine"}))
    await skillhub.skillhub_install({"id": "uuid-1"})

    with pytest.raises(ConfigValidationError) as err:
        await skillhub.skillhub_install({"id": "uuid-1"}, if_absent=True)

    assert "already installed" in str(err.value)
    assert (workspace / "demo-skill" / "SKILL.md").read_text() == "# mine"


@pytest.mark.asyncio
async def test_an_encrypted_archive_is_a_refusal_not_an_internal_error(workspace, monkeypatch):
    """zipfile raises a bare RuntimeError for an encrypted member, which would
    reach the caller as internal_error with a traceback."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("demo-skill/SKILL.md", "# demo")
    raw = bytearray(buf.getvalue())
    # The encrypted bit lives in the general-purpose flags of both the local
    # header (offset +6) and the central directory entry (+8); infolist() reads
    # the central one, so setting only the local flag proves nothing.
    raw[6] |= 0x1
    central = raw.index(b"PK\x01\x02")
    raw[central + 8] |= 0x1
    _stub_hub(monkeypatch, zip_bytes=bytes(raw))

    with pytest.raises(ConfigValidationError) as err:
        await skillhub.skillhub_install({"id": "uuid-1"})
    assert "encrypted" in str(err.value)


@pytest.mark.asyncio
async def test_a_failed_swap_that_cannot_be_restored_keeps_the_backup(workspace, monkeypatch):
    """The cleanup used to be unconditional, so a swap that failed *and* whose
    restore also failed deleted the user's only copy of the skill.

    Driven through the real install: the first rename (target -> backup) is
    allowed, every rename after it fails, which is the ordering that leaves the
    pool empty and the backup holding everything.
    """
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# v1"}))
    await skillhub.skillhub_install({"id": "uuid-1"})
    target = workspace / "demo-skill"
    assert (target / "SKILL.md").read_text() == "# v1"

    real_rename = Path.rename
    calls = {"n": 0}

    def _flaky(self, dest):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_rename(self, dest)
        raise OSError(5, "EIO")

    monkeypatch.setattr(Path, "rename", _flaky)
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# v2"}))
    with pytest.raises(Exception):
        await skillhub.skillhub_install({"id": "uuid-1"})
    monkeypatch.undo()

    kept = [p for p in workspace.glob(".demo-skill.old.*/demo-skill/SKILL.md")]
    assert kept, f"the only copy was deleted; pool now: {sorted(p.name for p in workspace.iterdir())}"
    assert kept[0].read_text() == "# v1"


# ---------------------------------------------------------------------------
# the declared shape, against what these handlers really return
# ---------------------------------------------------------------------------
# Same gap as in the plughub module: `test_rpc_contract_shapes` skips this
# group because it is "covered in its own module", and the tests above do drive
# the real handlers -- but they assert on individual keys and never validate a
# payload against `METHOD_MODELS`, so the four declarations here had nothing
# checking them against the code.


def _shape(method: str, payload: dict):
    from raven.rpc.models import METHOD_MODELS

    _, model = METHOD_MODELS[method]
    return model.model_validate(payload)


@pytest.mark.asyncio
async def test_the_four_methods_match_their_declared_models(workspace, monkeypatch):
    _stub_hub(monkeypatch, zip_bytes=_zip({"demo-skill/SKILL.md": "# demo"}))

    _shape("skillhub.search", await skillhub.skillhub_search({}))
    _shape("skillhub.detail", await skillhub.skillhub_detail({"id": "uuid-1"}))
    _shape("skillhub.install", await skillhub.skillhub_install({"id": "uuid-1"}))
    _shape("skillhub.remove", await skillhub.skillhub_remove({"name": "demo-skill"}))
