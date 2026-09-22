"""Whether this host can draw Han at all, asked before a render is believed.

A .pptx carries font *names*, never fonts. So whether a deck's Chinese appears
as characters or as boxes is decided entirely by the machine doing the
rendering, and raven used to decide it by luck:

* macOS ships PingFang, yet a stock Mac still renders a Chinese deck as boxes.
  On every Mac here that drew them correctly, the face LibreOffice actually
  reached was one somebody had installed into ``~/Library/Fonts`` by hand.
* ``apt-get install libreoffice`` recommends the Latin Noto packages and never
  ``fonts-noto-cjk``, so a Linux host installed the way this project's own
  installer installed it had no Han face either.
* The container image does install one, which is why that path always worked,
  and why the failure looked like it could not be ours.

Every host that worked, worked because somebody had installed a CJK font by
hand. That is not a property a product can rely on, and the failure is silent:
LibreOffice reports success, the PDF is well formed, and the boxes are visible
only to whoever looks at the picture -- including the model that renders a deck
to check its own work, which was reviewing a page it could not read.

Installing the face belongs to the installer, not here: ``install.sh`` asks
this platform's own package manager for one. That is the only mechanism that
puts a font where every program on the machine finds it, needs no digest of
ours, and leaves the font maintained by whoever maintains the rest of the
system.

What is here is the part an installer cannot do, because it happens on a
machine the installer never ran on: asking at render time whether this host can
draw Han, so that a page about to come out as boxes says so rather than being
rendered, measured and approved unread. A deployment that manages its own fonts
points :data:`ENV_FONT_DIR` at them and a conversion sees them, without
touching the system's configuration or needing privileges.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ENV_FONT_DIR = "RAVEN_FONT_DIR"
"""Points the whole mechanism somewhere else, for a deployment that manages its
own fonts or a test that wants an empty directory."""

FONT_SUFFIXES = (".otf", ".ttf", ".ttc", ".otc")


def font_dir() -> Path | None:
    """The directory a deployment puts its own faces in, or None where none is named.

    The environment alone decides this. An earlier draft defaulted to a
    directory under raven's runtime data, which meant ``raven.utils`` importing
    ``raven.config`` -- and ``raven.config`` already imports ``raven.utils``, so
    the pair became a cycle. Nothing ever wrote to that default: the installer
    puts its fallback face in the platform's own user font directory, where
    fontconfig and CoreText find it without being told. The default was a cycle
    bought for nothing.
    """
    override = os.environ.get(ENV_FONT_DIR)
    return Path(override).expanduser() if override else None


def bundled_face() -> Path | None:
    """A face placed in raven's font directory, or None where there is none.

    Any font file counts. Pinning one filename would only describe what raven
    itself once downloaded, and nothing downloads a font any more -- what a
    deployment drops in here is its choice, and the renderer reads a directory.
    """
    directory = font_dir()
    if directory is None or not directory.is_dir():
        return None
    for candidate in sorted(directory.iterdir()):
        if candidate.is_file() and candidate.suffix.lower() in FONT_SUFFIXES:
            return candidate
    return None


SYSTEM_FONTCONFIG = "/etc/fonts/fonts.conf"


def _fontconfig_xml(directories: list[Path], cache: Path, *, inherit: str) -> str:
    """A fontconfig configuration naming the directories a render may draw from.

    The configuration already in force is included rather than replaced: raven
    adds a face, it does not take the host's away, and a machine with a better
    Han font than this one should go on using it.
    """
    # Escaped, because these are paths rather than literals: a directory with an
    # ampersand in its name would otherwise make the file malformed, and
    # fontconfig answers a malformed file by discarding all of it -- leaving the
    # render with no fonts at all, which is worse than the problem being fixed.
    dirs = "\n".join(f"  <dir>{escape(str(directory))}</dir>" for directory in directories)
    return (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
        "<fontconfig>\n"
        f"  <include ignore_missing='yes'>{escape(inherit)}</include>\n"
        f"{dirs}\n"
        f"  <cachedir>{escape(str(cache))}</cachedir>\n"
        "</fontconfig>\n"
    )


def render_env(base: dict[str, str] | None = None, *, scratch: Path | None = None) -> dict[str, str]:
    """The environment a conversion runs in, with raven's own faces reachable.

    Returns the environment unchanged where there is nothing to add -- no face
    placed, or macOS, which reads fonts through CoreText and takes no
    configuration file -- so a caller passes the result straight to ``Popen``
    without asking whether anything happened.
    """
    env = dict(os.environ if base is None else base)
    face = bundled_face()
    if face is None or sys.platform == "darwin":
        return env
    where = scratch or face.parent
    where.mkdir(parents=True, exist_ok=True)
    config = where / "fonts.conf"
    # Inherit whatever configuration was already in force, not the system
    # default: a deployment that set FONTCONFIG_FILE chose its fonts on purpose,
    # and silently swapping that for /etc/fonts would undo the choice while
    # appearing to respect it.
    inherit = env.get("FONTCONFIG_FILE") or SYSTEM_FONTCONFIG
    config.write_text(_fontconfig_xml([face.parent], where / "fc-cache", inherit=inherit), encoding="utf-8")
    env["FONTCONFIG_FILE"] = str(config)
    return env


def host_han_faces() -> list[str]:
    """The Han families the host itself offers, as far as it can be asked.

    Only fontconfig answers this cheaply, so a host without it answers with an
    empty list -- which is not the same as having none, and is why
    :func:`can_draw_han` does not treat an empty answer as a verdict on its own.
    """
    fc_list = shutil.which("fc-list")
    if not fc_list:
        return []
    try:
        out = subprocess.run(  # noqa: S603 - resolved above, fixed arguments
            [fc_list, ":lang=zh", "family"], capture_output=True, text=True, timeout=20, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.split(",")[0].strip() for line in out.splitlines() if line.strip()})


_HAN_NAME_HINTS = ("cjk", "notosanssc", "notosanstc", "notoserifsc", "sourcehan", "pingfang", "heiti", "msyh", "simsun")

_SYSTEM_HAN_FACES = (
    # Package paths, for a host that has the fontconfig library but not the
    # fc-list binary: there a file on disk does mean the renderer reaches it.
    # macOS is deliberately absent. PingFang is present on every Mac and
    # LibreOffice still does not draw from it -- that is the failure this
    # module exists for, so naming it here would answer "yes, this host can set
    # Chinese" for exactly the host that cannot.
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-VF.otf.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "C:/Windows/Fonts/msyh.ttc",
)


def user_font_dirs() -> tuple[Path, ...]:
    """Where a font can be installed on this platform without privileges.

    The same directories the installer writes its fallback into, and the ones
    both fontconfig and CoreText read without being told to.
    """
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Fonts", Path("/Library") / "Fonts")
    base = os.environ.get("XDG_DATA_HOME")
    return ((Path(base) if base else Path.home() / ".local" / "share") / "fonts",)


def user_han_faces() -> list[Path]:
    """CJK faces sitting in those directories, recognised by name.

    By name because opening every font file to read its coverage costs more
    than this answer is worth, and because the names here are the ones an
    installer wrote.
    """
    found: list[Path] = []
    for directory in user_font_dirs():
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.iterdir()):
            if candidate.suffix.lower() not in FONT_SUFFIXES:
                continue
            name = candidate.stem.lower().replace(" ", "").replace("-", "")
            if any(hint in name for hint in _HAN_NAME_HINTS):
                found.append(candidate)
    return found


def _fc_listed_han() -> Path | None:
    """The first file fontconfig lists as actually covering Chinese.

    ``fc-list`` rather than ``fc-match``: fc-match always answers, with its
    nearest approximation, so on a host with no Han face at all it returns a
    Latin font -- which would then be taken for a Han face and used to measure
    Chinese. fc-list names only fonts that carry the language.
    """
    fc_list = shutil.which("fc-list")
    if not fc_list:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - resolved above, fixed arguments
            [fc_list, ":lang=zh", "-f", "%{file}\n"], capture_output=True, text=True, timeout=20, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        candidate = Path(line.strip())
        if line.strip() and candidate.is_file():
            return candidate
    return None


def han_face() -> Path | None:
    """A font *file* on this host carrying Han glyphs, or None where there is none.

    Rendering asks the platform for a face by name; measuring has to open one,
    so it needs a path. Same question, different shape of answer -- and the
    same order: raven's own directory first because it does not depend on the
    machine, then where a privilege-free install puts a font, then whatever the
    system itself resolves for Chinese.
    """
    face = bundled_face()
    if face is not None:
        return face
    for candidate in user_han_faces():
        return candidate
    matched = _fc_listed_han()
    if matched is not None:
        return matched
    # The hardcoded paths answer only where fontconfig could not be asked at
    # all, which in practice means macOS. Where it could be asked and said no,
    # a font file sitting on disk is not a face the renderer will reach, and
    # answering with one would promise Chinese that comes out as boxes.
    if shutil.which("fc-list"):
        return None
    for path in _SYSTEM_HAN_FACES:
        if Path(path).is_file():
            return Path(path)
    return None


def can_draw_han() -> bool:
    """Whether a conversion on this host would draw Chinese rather than boxes.

    Raven's own directory counts first, because it is the one answer that does
    not depend on the machine. Then the host, by whichever means it can be
    asked. A host that cannot be asked at all is taken at its word only on
    Windows, which ships a Han face in every edition.
    """
    if han_face() is not None:
        return True
    if host_han_faces():
        return True
    return sys.platform == "win32"


def install_hint() -> str:
    """What to run to give this host a Han face, in its own package manager's words."""
    if sys.platform == "darwin":
        return "brew install --cask font-noto-sans-cjk"
    if sys.platform == "win32":
        return "Windows ships Microsoft YaHei; reinstall the system fonts if it is gone"
    if shutil.which("apt-get"):
        return "sudo apt-get install -y fonts-noto-cjk"
    return "install a CJK font (Noto Sans CJK) with this system's package manager"
