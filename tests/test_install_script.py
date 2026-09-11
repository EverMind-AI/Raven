"""Tripwires for the two root installers (install.sh and install.ps1).

install.sh builds uv option pairs in scalar variables and expands them
unquoted (POSIX sh has no arrays), so a requirement carried that way must
stay a single word: word splitting hands uv each space-separated piece as
its own argument, uv exits 2, and the retry ladder silently degrades to a
bare install with the memory plugin and channel extras dropped. The quoted
positional requirement legitimately keeps the spaced PEP 508 form, so the
spaced spelling looks natural and keeps getting reintroduced -- this pin
turns that red in CI instead.

The optional capability steps (the chromium download, the LibreOffice offer,
the doctor summary) get pins of their own: they stay skippable via
RAVEN_MINIMAL, they read the interactive answer and sudo's password prompt
from /dev/tty (stdin is the script itself under `curl | sh`), and they stay
above the closing "All set" block -- tests/test_cli_onboard_commands.py scans
that block for first-run wording, so new code after it would land inside a
region another test owns.

install.ps1 mirrors the capability steps for Windows (Install-Browser,
Install-Office, Show-CapabilitySummary): same decisions, same degrade-loudly
warns, with the /dev/tty gate traded for a console gate -- under `irm | iex`
Read-Host still reads the console, but CI has none, so the guard must make a
non-interactive run skip the winget offer cleanly instead of hanging on it.
"""

from __future__ import annotations

from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"
INSTALL_PS1 = Path(__file__).resolve().parents[1] / "install.ps1"


def test_the_installer_is_where_this_tripwire_thinks_it_is() -> None:
    assert INSTALL_SH.is_file()


def test_the_everos_with_requirement_stays_one_word() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "everos-memory@$everos_url" in text
    assert "everos-memory @ " not in text


def test_the_optional_capability_steps_exist_and_are_skippable() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    for fn in ("install_browser", "install_office", "print_capability_summary"):
        assert f"{fn}() {{" in text
    assert '[ -n "${RAVEN_MINIMAL:-}" ] || install_browser' in text
    assert '[ -n "${RAVEN_MINIMAL:-}" ] || install_office' in text


def test_the_summary_speaks_through_doctor() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "doctor --install-summary" in text


def test_the_office_prompt_and_sudo_both_read_the_tty() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "read -r answer < /dev/tty" in text
    assert "sudo apt-get install -y libreoffice < /dev/tty" in text


def test_the_capability_steps_stay_above_the_closing_hint() -> None:
    closing = INSTALL_SH.read_text(encoding="utf-8")
    closing = closing[closing.index("All set") :].lower()
    assert "playwright" not in closing
    assert "libreoffice" not in closing


def test_the_windows_installer_is_where_this_tripwire_thinks_it_is() -> None:
    assert INSTALL_PS1.is_file()


def test_the_windows_capability_steps_exist_and_are_skippable() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    for fn in ("Install-Browser", "Install-Office", "Show-CapabilitySummary"):
        assert f"function {fn}" in text
    assert "if (-not $env:RAVEN_MINIMAL) { Install-Browser $uv }" in text
    assert "if (-not $env:RAVEN_MINIMAL) { Install-Office }" in text


def test_the_windows_summary_speaks_through_doctor() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "doctor --install-summary" in text


def test_the_windows_office_offer_needs_a_real_console() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "[Environment]::UserInteractive -and -not [Console]::IsInputRedirected" in text


def test_the_windows_capability_steps_stay_above_the_closing_hint() -> None:
    closing = INSTALL_PS1.read_text(encoding="utf-8")
    closing = closing[closing.index("All set") :].lower()
    assert "playwright" not in closing
    assert "libreoffice" not in closing
