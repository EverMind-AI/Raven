"""The large-file gate: size ceiling, blocked asset types, and the source-tree exemptions."""

from __future__ import annotations

from pathlib import Path

from scripts import check_large_files


def test_detects_oversized_changed_files(tmp_path: Path) -> None:
    small = tmp_path / "small.txt"
    large = tmp_path / "assets" / "large.png"
    large.parent.mkdir()
    small.write_bytes(b"x" * 1024)
    large.write_bytes(b"x" * 1025)

    violations = check_large_files.find_oversized_files(
        ["small.txt", "assets/large.png", "deleted.mov"],
        max_bytes=1024,
        root=tmp_path,
    )

    assert violations == [
        check_large_files.FileSizeViolation(
            path="assets/large.png",
            size=1025,
            limit=1024,
        )
    ]


def test_detects_blocked_assets_even_when_small(tmp_path: Path) -> None:
    image = tmp_path / "docs" / "screenshot.png"
    video = tmp_path / "demos" / "clip.mp4"
    svg = tmp_path / "docs" / "diagram.svg"
    pdf = tmp_path / "report.pdf"
    audio = tmp_path / "demo.mp3"
    html = tmp_path / "public" / "index.html"
    manifest = tmp_path / "public" / "site.webmanifest"
    image.parent.mkdir(parents=True)
    video.parent.mkdir(parents=True)
    html.parent.mkdir(parents=True)
    image.write_bytes(b"x")
    video.write_bytes(b"x")
    svg.write_text("<svg></svg>")
    pdf.write_bytes(b"%PDF-1.7")
    audio.write_bytes(b"x")
    html.write_text("<!doctype html>")
    manifest.write_text("{}")

    violations = check_large_files.find_blocked_asset_files(
        [
            "docs/screenshot.png",
            "demos/clip.mp4",
            "docs/diagram.svg",
            "report.pdf",
            "demo.mp3",
            "public/index.html",
            "public/site.webmanifest",
        ],
        root=tmp_path,
    )

    assert violations == [
        check_large_files.BlockedAssetViolation(path="docs/screenshot.png", extension=".png"),
        check_large_files.BlockedAssetViolation(path="demos/clip.mp4", extension=".mp4"),
        check_large_files.BlockedAssetViolation(path="docs/diagram.svg", extension=".svg"),
        check_large_files.BlockedAssetViolation(path="report.pdf", extension=".pdf"),
        check_large_files.BlockedAssetViolation(path="demo.mp3", extension=".mp3"),
        check_large_files.BlockedAssetViolation(path="public/index.html", extension=".html"),
        check_large_files.BlockedAssetViolation(path="public/site.webmanifest", extension=".webmanifest"),
    ]


def test_allows_blocked_extensions_inside_application_source(tmp_path: Path) -> None:
    entry = tmp_path / "ui-tui" / "index.html"
    logo = tmp_path / "ui-tui" / "src" / "assets" / "logo.svg"
    tui_icon = tmp_path / "ui-tui" / "src" / "icon.svg"
    bridge_page = tmp_path / "bridge" / "src" / "panel.html"
    for target in (entry, logo, tui_icon, bridge_page):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")

    violations = check_large_files.find_blocked_asset_files(
        [
            "ui-tui/index.html",
            "ui-tui/src/assets/logo.svg",
            "ui-tui/src/icon.svg",
            "bridge/src/panel.html",
        ],
        root=tmp_path,
    )

    assert violations == []


def test_allows_only_jpg_raven_design_skill_reference_images(tmp_path: Path) -> None:
    prefix = "plugins-dist/design-engine/raven_design/skills/example"
    paths = [
        f"{prefix}/references/benchmark.jpg",
        f"{prefix}/references/nested/benchmark.jpg",
        f"{prefix}/references/benchmark.png",
        f"{prefix}/assets/benchmark.jpg",
        "plugins-dist/other-engine/raven_design/skills/example/references/benchmark.jpg",
        "docs/benchmark.jpg",
    ]
    for path in paths:
        candidate = tmp_path / path
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"x")

    violations = check_large_files.find_blocked_asset_files(paths, root=tmp_path)

    assert violations == [
        check_large_files.BlockedAssetViolation(path=f"{prefix}/references/benchmark.png", extension=".png"),
        check_large_files.BlockedAssetViolation(path=f"{prefix}/assets/benchmark.jpg", extension=".jpg"),
        check_large_files.BlockedAssetViolation(
            path="plugins-dist/other-engine/raven_design/skills/example/references/benchmark.jpg",
            extension=".jpg",
        ),
        check_large_files.BlockedAssetViolation(path="docs/benchmark.jpg", extension=".jpg"),
    ]


def test_allows_jpg_plates_at_the_design_engine_wheel_seat(tmp_path: Path) -> None:
    """The C4 re-seat: the plates migrated into the design-engine wheel, and
    the same rule follows them -- .jpg only, only under a skill's
    references/, while the frozen fork seat stays allowed until retirement."""
    wheel = "plugins-dist/design-engine/raven_design/skills/example"
    paths = [
        f"{wheel}/references/plate.jpg",
        f"{wheel}/references/nested/plate.jpg",
        f"{wheel}/references/plate.png",
        f"{wheel}/assets/plate.jpg",
        "plugins-dist/design-engine/raven_design/plate.jpg",
    ]
    for path in paths:
        candidate = tmp_path / path
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"x")

    violations = check_large_files.find_blocked_asset_files(paths, root=tmp_path)

    assert violations == [
        check_large_files.BlockedAssetViolation(path=f"{wheel}/references/plate.png", extension=".png"),
        check_large_files.BlockedAssetViolation(path=f"{wheel}/assets/plate.jpg", extension=".jpg"),
        check_large_files.BlockedAssetViolation(
            path="plugins-dist/design-engine/raven_design/plate.jpg", extension=".jpg"
        ),
    ]


def test_raven_design_skill_reference_images_still_obey_size_limit(tmp_path: Path) -> None:
    path = "plugins-dist/design-engine/raven_design/skills/example/references/benchmark.jpg"
    candidate = tmp_path / path
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"x" * 1025)

    violations = check_large_files.find_oversized_files([path], max_bytes=1024, root=tmp_path)

    assert violations == [
        check_large_files.FileSizeViolation(path=path, size=1025, limit=1024),
    ]


def test_still_blocks_report_assets_that_merely_mention_an_app_tree(tmp_path: Path) -> None:
    shot = tmp_path / "docs" / "webui-overview.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"x")

    violations = check_large_files.find_blocked_asset_files(["docs/webui-overview.png"], root=tmp_path)

    assert violations == [check_large_files.BlockedAssetViolation(path="docs/webui-overview.png", extension=".png")]


def test_application_source_exemption_does_not_lift_the_size_limit(tmp_path: Path) -> None:
    bundle = tmp_path / "ui-tui" / "public" / "hero.png"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"x" * 32)

    violations = check_large_files.find_oversized_files(["ui-tui/public/hero.png"], max_bytes=16, root=tmp_path)

    assert violations == [check_large_files.FileSizeViolation(path="ui-tui/public/hero.png", size=32, limit=16)]


def test_changed_paths_reads_added_and_modified_files(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_check_output(cmd: list[str], text: bool) -> str:
        calls.append(cmd)
        assert text is True
        return "image.png\nvideo.mp4\n"

    monkeypatch.setattr(check_large_files.subprocess, "check_output", fake_check_output)

    assert check_large_files.changed_paths("base..HEAD") == ["image.png", "video.mp4"]
    assert calls == [["git", "diff", "--name-only", "--diff-filter=AM", "base..HEAD"]]


def test_main_reports_oversized_files(tmp_path: Path, monkeypatch, capsys) -> None:
    large = tmp_path / "demo.mp4"
    large.write_bytes(b"x" * 2048)

    monkeypatch.setattr(check_large_files, "changed_paths", lambda revision_range: ["demo.mp4"])

    result = check_large_files.main(["base..HEAD", "--max-bytes", "1024", "--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "Oversized files are not allowed in PRs" in captured.err
    assert "demo.mp4: 2.0 KiB > 1.0 KiB" in captured.err


def test_main_reports_blocked_asset_files(tmp_path: Path, monkeypatch, capsys) -> None:
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.7")

    monkeypatch.setattr(check_large_files, "changed_paths", lambda revision_range: ["report.pdf"])

    result = check_large_files.main(["base..HEAD", "--max-bytes", "1024", "--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "Blocked asset files are not allowed in PRs" in captured.err
    assert "report.pdf: .pdf files should be stored outside git" in captured.err


def test_main_skips_without_range(capsys) -> None:
    result = check_large_files.main([])

    captured = capsys.readouterr()
    assert result == 0
    assert "No commit range detected; skipping large file check" in captured.out
