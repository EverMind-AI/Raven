"""Filesystem names and places: safe filenames, path segments, the project slug."""

import os
import re
import struct
from pathlib import Path


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')


def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def safe_path_segment(name: str) -> str:
    """Like ``safe_filename``, but safe to use as a bare directory name.

    ``safe_filename`` leaves ``.`` and ``..`` intact. That is harmless where a
    suffix follows (``sessions/<chat_id>.jsonl`` turns ``..`` into ``...jsonl``)
    but not for a directory segment: ``<root>/<channel>/..`` resolves back out
    of the root, and every key that normalises the same way would then share
    one directory. Dot-only names are exactly that set -- any other name
    containing dots is an ordinary component -- so they fold to underscores,
    which is what ``safe_filename`` already produces for unsafe input.
    """
    cleaned = safe_filename(name)
    if cleaned and not cleaned.strip("."):
        return "_" * len(cleaned)
    return cleaned


_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9]")


# Above this many characters the slug is truncated and a hash of the full path
# is appended. Matches the reference implementation's cap.
_SLUG_MAX_LEN = 200


def _slug_hash(text: str) -> str:
    """Base36 of the reference implementation's 32-bit string hash.

    ``h = h * 31 + code_unit`` truncated to a signed 32-bit int each step, then
    ``abs()``. Iterates UTF-16 code units, not code points, because the
    reference reads ``charCodeAt`` -- for a path containing a non-BMP character
    the two disagree.
    """
    units = struct.unpack(f"<{len(text.encode('utf-16-le')) // 2}H", text.encode("utf-16-le"))
    value = 0
    for unit in units:
        value = (value * 31 + unit) & 0xFFFFFFFF
        if value >= 0x80000000:
            value -= 0x100000000
    value = abs(value)
    if value == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while value:
        value, remainder = divmod(value, 36)
        out = digits[remainder] + out
    return out


def project_slug(directory: str | Path) -> str:
    """Flatten a directory into one filesystem-safe segment naming that project.

    Reproduces the scheme Claude Code uses for ``~/.claude/projects/``: replace
    each non-alphanumeric character with ``-``, so ``/srv/work/my_app`` becomes
    ``-srv-work-my-app`` -- separators, dots and underscores alike. Characters
    are replaced one for one, not by runs, so ``/a//b`` keeps both dashes; the
    leading ``-`` is the root slash. Past 200 characters the result is truncated
    and a hash of the whole path appended, which is what keeps long paths apart
    once their tails are cut off.

    The slug is a label, not a reversible encoding, and short paths get no
    disambiguation at all: ``/srv/a_b`` and ``/srv/a/b`` produce the same
    segment, in the reference implementation too. That is safe here only
    because nothing treats the directory name as the project's identity -- the
    launch directory is recorded in each session's metadata
    (``SessionManager``), the same way the reference records ``cwd`` on every
    transcript entry.
    """
    # `os.path.expanduser`, not `Path(...)`: constructing a Path normalises the
    # string (`/a//b` -> `/a/b`, a trailing slash dropped), and the reference
    # slugs the raw directory string. Callers pass `Path.cwd()`, which is
    # already normalised, so this only matters for a hand-written path.
    path = os.path.expanduser(str(directory))
    slug = _SLUG_UNSAFE.sub("-", path)
    if len(slug) <= _SLUG_MAX_LEN:
        return slug
    return f"{slug[:_SLUG_MAX_LEN]}-{_slug_hash(path)}"


__all__ = ["ensure_dir", "project_slug", "safe_filename", "safe_path_segment"]
