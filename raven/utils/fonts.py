"""Whether this host can draw Han at all, asked before a render is believed.

A .pptx carries font *names*, never fonts. So whether a deck's Chinese appears
as characters or as boxes is decided entirely by the machine doing the
rendering, and raven used to decide it by luck:

* macOS ships PingFang, yet a stock Mac renders a Chinese deck as boxes -- and
  so does a Mac somebody has installed a CJK face onto by hand, which is what
  made this look like a font problem for so long. LibreOffice's mac build reads
  fonts through the fontconfig it bundles, and that library was built for a
  ``/usr/local`` prefix. On an Apple Silicon host ``/usr/local/etc/fonts`` does
  not exist, so fontconfig starts with no configuration at all and the
  converter sees only the faces inside LibreOffice's own app bundle, none of
  which carries Han. The ``fc-list`` on such a host reads a different
  configuration and cheerfully names Chinese families the converter cannot
  reach.
* ``apt-get install libreoffice`` recommends the Latin Noto packages and never
  ``fonts-noto-cjk``, so a Linux host installed the way this project's own
  installer installed it had no Han face either.
* The container image does install one, which is why that path always worked,
  and why the failure looked like it could not be ours.

The failure is silent either way: LibreOffice reports success, the PDF is well
formed, and the boxes are visible only to whoever looks at the picture --
including the model that renders a deck to check its own work, which was
reviewing a page it could not read.

The two halves of the problem want different remedies. Where fontconfig is
configured, the host genuinely has no Han face and one has to be installed;
that belongs to the installer, and ``install.sh`` asks the platform's own
package manager. On a Mac no font is missing, so no download helps: what is
missing is a configuration, and :func:`render_env` writes one naming the
directories the Mac already keeps its fonts in. ``Arial Unicode.ttf``, stock in
``/System/Library/Fonts/Supplemental``, then draws the page.

The rest is the part an installer cannot do, because it happens on a machine
the installer never ran on: asking at render time whether this host can draw
Han, so that a page about to come out as boxes says so rather than being
rendered, measured and approved unread. A deployment that manages its own fonts
points :data:`ENV_FONT_DIR` at them and a conversion sees them, without
touching the system's configuration or needing privileges.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
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


def unconfigured_font_dirs() -> tuple[Path, ...]:
    """Directories holding fonts the converter would otherwise never be told about.

    Empty everywhere but macOS, where the converter's own fontconfig starts with
    no configuration and so reaches none of the machine's fonts. These are the
    four places a Mac keeps faces; naming them is what turns the host's Han
    coverage, stock or installed, into something a conversion can draw with.
    """
    if sys.platform != "darwin":
        return ()
    return (Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"), *user_font_dirs())


def render_env(base: dict[str, str] | None = None, *, scratch: Path | None = None) -> dict[str, str]:
    """The environment a conversion runs in, with the faces it needs reachable.

    Returns the environment unchanged where there is nothing to add, so a caller
    passes the result straight to ``Popen`` without asking whether anything
    happened.
    """
    env = dict(os.environ if base is None else base)
    face = bundled_face()
    directories = [face.parent] if face is not None else []
    directories += [directory for directory in unconfigured_font_dirs() if directory.is_dir()]
    if not directories:
        return env
    # The configuration and its cache have to land somewhere writable, which
    # rules out the system directories above; a conversion passes its own
    # scratch so the pair is thrown away with the run.
    where = scratch or (face.parent if face is not None else Path(tempfile.gettempdir()) / "raven-fontconfig")
    where.mkdir(parents=True, exist_ok=True)
    config = where / "fonts.conf"
    # Inherit whatever configuration was already in force, not the system
    # default: a deployment that set FONTCONFIG_FILE chose its fonts on purpose,
    # and silently swapping that for /etc/fonts would undo the choice while
    # appearing to respect it.
    inherit = env.get("FONTCONFIG_FILE") or SYSTEM_FONTCONFIG
    config.write_text(_fontconfig_xml(directories, where / "fc-cache", inherit=inherit), encoding="utf-8")
    env["FONTCONFIG_FILE"] = str(config)
    return env


def fontconfig_speaks_for_the_renderer() -> bool:
    """Whether what fontconfig lists is what a conversion will draw from.

    Everywhere but macOS, yes: LibreOffice and fc-list read the same
    configuration there, so a family fc-list names is a family a page can be set
    in. On a Mac they read different ones -- the converter's bundled fontconfig
    has none, the host's fc-list comes from Homebrew and has its own -- and
    measured here (macOS 15, LibreOffice 26.8) a conversion drew no Han while
    fc-list named twenty-nine Chinese families. Reading that list as an answer
    is how a Mac is taken for a host whose font situation has been settled, when
    what settles it is :func:`render_env` naming the directories instead.
    """
    return sys.platform != "darwin"


def host_han_faces() -> list[str]:
    """The Han families the host itself offers, as far as it can be asked.

    Only fontconfig answers this cheaply, so a host without it answers with an
    empty list -- which is not the same as having none, and is why
    :func:`can_draw_han` does not treat an empty answer as a verdict on its own.
    A Mac answers empty for the reason
    :func:`fontconfig_speaks_for_the_renderer` gives.
    """
    fc_list = shutil.which("fc-list") if fontconfig_speaks_for_the_renderer() else None
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
    # Paths a file on disk really does mean the renderer reaches: on a host with
    # the fontconfig library but not the fc-list binary, and on macOS, where
    # render_env names the directory these two sit in. Arial Unicode leads
    # because it is a plain TrueType and measurement has to open what it gets;
    # PingFang is a collection and answers only where that one is gone.
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/PingFang.ttc",
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
    fc_list = shutil.which("fc-list") if fontconfig_speaks_for_the_renderer() else None
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
    # Where fontconfig could be asked and said no, a font file sitting on disk
    # is not a face the renderer will reach, and answering with one would
    # promise Chinese that comes out as boxes. The hardcoded paths are for the
    # hosts where it could not be asked -- and for macOS, where the answer comes
    # from render_env naming the directory rather than from fc-list.
    if fontconfig_speaks_for_the_renderer() and shutil.which("fc-list"):
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
