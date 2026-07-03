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
