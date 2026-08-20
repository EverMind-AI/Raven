"""Unit tests for ``scripts/refresh_models_dev_snapshot.py``.

The script's own docstrings name two silent failure modes: reading only the
flat ``models/`` level drops every nested row (siliconflow and openrouter are
entirely nested), and an unresolved ``base_model`` leaves rows present but
nameless -- "every total looks right, the rows just have no names". Neither
was covered; the snapshot assertions in ``test_provider_catalog.py`` check
the shipped artifact, not the code that produces it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "refresh_models_dev_snapshot.py"
_spec = importlib.util.spec_from_file_location("refresh_models_dev_snapshot", _SCRIPT)
assert _spec is not None and _spec.loader is not None
refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh)


def _tar_bytes(files: dict[str, str]) -> bytes:
    """A gzipped tarball shaped like the models.dev repository archive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_nested_model_paths_are_read_alongside_flat_ones() -> None:
    """A gateway files ids like ``moonshotai/Kimi-K2.6`` two directories deep;
    reading only the flat level returns the provider with zero models."""
    archive = _tar_bytes(
        {
            "root/providers/flatco/provider.toml": 'name = "Flat Co"',
            "root/providers/flatco/models/plain.toml": 'name = "Plain"',
            "root/providers/nestco/provider.toml": 'name = "Nest Co"',
            "root/providers/nestco/models/moonshotai/kimi-k2.6.toml": 'name = "Kimi K2.6"',
        }
    )

    catalogue = refresh.read_catalogue(archive)

    assert catalogue["flatco"]["models"]["plain"]["name"] == "Plain"
    assert catalogue["nestco"]["models"]["moonshotai/kimi-k2.6"]["name"] == "Kimi K2.6"


def test_a_row_inherits_from_its_base_and_keeps_its_own_fields() -> None:
    """A reseller row states only its own price and points ``base_model`` at
    the vendor's shared definition for the name and description."""
    archive = _tar_bytes(
        {
            "root/models/vendorx/base-model.toml": ('name = "Base Model"\ndescription = "The vendor description"'),
            "root/providers/reseller/provider.toml": 'name = "Reseller"',
            "root/providers/reseller/models/base-model.toml": (
                'base_model = "vendorx/base-model"\n[cost]\ninput = 2.0'
            ),
        }
    )

    row = refresh.read_catalogue(archive)["reseller"]["models"]["base-model"]

    assert row["name"] == "Base Model"
    assert row["description"] == "The vendor description"
    assert row["cost"] == {"input": 2.0}


def test_a_three_level_chain_resolves_through_the_middle() -> None:
    """A base can itself derive; fields must flow down the whole chain, with
    the nearer definition winning where both state one."""
    archive = _tar_bytes(
        {
            "root/models/vendorx/leaf.toml": ('name = "Leaf Name"\ndescription = "Leaf description"'),
            "root/models/vendorx/mid.toml": ('base_model = "vendorx/leaf"\ndescription = "Mid description"'),
            "root/providers/chainco/provider.toml": 'name = "Chain Co"',
            "root/providers/chainco/models/top.toml": 'base_model = "vendorx/mid"',
        }
    )

    row = refresh.read_catalogue(archive)["chainco"]["models"]["top"]

    assert row["name"] == "Leaf Name"
    assert row["description"] == "Mid description"


def test_a_base_cycle_terminates_and_is_not_cached_for_later_lookups() -> None:
    """Rows pointing at each other must not hang -- and the cycle-truncated
    intermediate must not be cached.

    The row loop seeds ``seen`` with a ref that has no recursion frame, so
    the bare row cached at the cycle hit is never overwritten by a full
    merge. Resolving ``m1`` first therefore used to poison the cache for
    ``pc/m1``, and ``m3`` -- inheriting the same base through a path with no
    cycle of its own -- came back without the field only ``m2`` carries.
    """
    archive = _tar_bytes(
        {
            "root/providers/pc/provider.toml": 'name = "PC"',
            "root/providers/pc/models/m1.toml": ('base_model = "pc/m2"\nname = "M1 Name"'),
            "root/providers/pc/models/m2.toml": ('base_model = "pc/m1"\ndescription = "M2 description"'),
            "root/providers/pc/models/m3.toml": 'base_model = "pc/m1"',
        }
    )

    models = refresh.read_catalogue(archive)["pc"]["models"]

    assert models["m3"]["name"] == "M1 Name"
    assert models["m3"]["description"] == "M2 description"


_FLATCO_ARCHIVE = {
    "root/providers/flatco/provider.toml": 'name = "Flat Co"',
    "root/providers/flatco/models/plain.toml": 'name = "Plain"',
}


def _wire_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, sha: str) -> list[str]:
    """Point ``main()`` at fakes; returns the list of URLs it fetches."""
    urls: list[str] = []
    archive = _tar_bytes(_FLATCO_ARCHIVE)

    def fake_fetch(url: str, *, accept: str = "*/*") -> bytes:
        urls.append(url)
        if "api.github.com" in url:
            return json.dumps({"sha": sha}).encode("utf-8")
        return archive

    monkeypatch.setattr(refresh, "_fetch", fake_fetch)
    monkeypatch.setattr(refresh, "reachable_providers", lambda catalogue: {"flatco"})
    monkeypatch.setattr(refresh, "SNAPSHOT", tmp_path / "out.json")
    monkeypatch.chdir(tmp_path)
    return urls


def test_main_downloads_the_tarball_by_the_resolved_sha(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two requests to a moving branch can straddle a push; downloading by the
    sha just resolved is what makes the recorded provenance verifiable."""
    sha = "feedc0de" * 5
    urls = _wire_main(monkeypatch, tmp_path, sha=sha)

    assert refresh.main() == 0

    tarball_url = urls[1]
    assert sha in tarball_url
    assert "refs/heads" not in tarball_url
    written = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert written["_source"]["sha"] == sha


def test_main_refuses_to_write_a_snapshot_over_the_size_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 1 MiB gate must fire before the write: an oversized snapshot must
    never land in the working tree, not land and then be complained about."""
    _wire_main(monkeypatch, tmp_path, sha="a" * 40)
    oversized = {"flatco": {"models": {"big": {"description": "x" * 1_100_000}}}}
    monkeypatch.setattr(refresh, "build", lambda providers, *, wanted: oversized)

    assert refresh.main() == 1
    assert not (tmp_path / "out.json").exists()
