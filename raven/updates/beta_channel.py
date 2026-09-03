"""The beta channel: pre-release builds served from a GitLab package registry.

Stable Raven upgrades itself from GitHub releases (``upgrade_commands``). Test
builds never reach GitHub, so they need a second source: a GitLab generic
package registry holding the wheels, plus one pointer file naming the newest of
them. The pointer exists because a read-only deploy token cannot list a
registry -- it can only fetch a path it already knows -- so the publisher
overwrites a fixed path instead of the reader searching.

An install joins the channel by having ``~/.raven/beta.json``, which the beta
installer writes and nothing else does. Delete it and every entry point below
falls back to GitHub, which is also how a tester leaves the channel.

The token in that file can fetch packages from one project and nothing else:
the repository endpoints answer 404 to it and ``git clone`` is refused. It
still reaches ``uv`` inside the wheel URL, so it shows up in that process's
argv -- acceptable for a credential whose whole power is downloading builds we
are handing to the holder anyway, and the reason the file is 0600.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from raven.updates.upgrade import ReleaseInfo, UpgradeError

_STATE_NAME = "beta.json"
_POINTER_PATH = "latest/latest.json"
_TIMEOUT_S = 10.0
_HOST = "gitlab.com"
_NOT_MODIFIED = 304

_BETA_VERSION_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:b([1-9][0-9]*))?$")

_FINAL = 1
_PRERELEASE = 0


@dataclass(frozen=True)
class BetaChannel:
    """Where beta builds live, and the credential that may read them."""

    project: str
    username: str
    token: str

    @property
    def api_base(self) -> str:
        return f"https://{_HOST}/api/v4/projects/{self.project}/packages/generic/raven"


def release_key(value: str) -> tuple[int, int, int, int, int]:
    """Order ``0.1.12b1`` < ``0.1.12b2`` < ``0.1.12`` < ``0.1.13b1``.

    ``upgrade_commands._version_key`` cannot be widened to cover this: it also
    rebuilds a version string by joining what it returns, so its arity is part
    of its contract. This is the comparison the beta channel uses instead, and
    it reads a plain release too -- both channels' versions land in the same
    ordering, which is what lets a tester's beta compare against a stable one.
    """
    match = _BETA_VERSION_RE.fullmatch(value.strip())
    if match is None:
        raise UpgradeError(f"Unsupported Raven version: {value}")
    major, minor, patch, serial = match.groups()
    if serial is None:
        return int(major), int(minor), int(patch), _FINAL, 0
    return int(major), int(minor), int(patch), _PRERELEASE, int(serial)


def _state_path() -> Path:
    from raven.config.loader import raven_home

    return raven_home() / _STATE_NAME


def channel() -> BetaChannel | None:
    """The configured beta channel, or ``None`` for a stable install.

    Every field must be a non-empty string. A half-written file reads as "not
    on the channel" rather than raising, so a bad edit costs the notice, not
    the launch.
    """
    try:
        raw = _state_path().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    values = []
    for key in ("project", "username", "token"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        values.append(value.strip())
    project, username, token = values
    if not project.isdigit():
        return None
    return BetaChannel(project=project, username=username, token=token)


def is_active() -> bool:
    return channel() is not None


def _authorized(url: str, chan: BetaChannel) -> str:
    """Put the deploy token in the URL, the one auth style ``uv`` will carry.

    A header would keep it out of argv, but nothing between here and the wheel
    download accepts one: the URL travels through the upgrade helper into
    ``uv tool install``, which reads credentials only from the URL itself.
    """
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{quote(chan.username, safe='')}:{quote(chan.token, safe='')}@{rest}"


def _parse_pointer(payload: object, chan: BetaChannel) -> ReleaseInfo:
    """Read ``latest.json`` into a release, building the URL rather than trusting one.

    The pointer names a version and nothing else that reaches the network. The
    wheel URL is derived here from the configured project, so a rewritten
    pointer can at worst name a version that does not exist -- it cannot send
    the installer to another host. This mirrors the host check the GitHub
    parser applies to its own payload.
    """
    if not isinstance(payload, dict):
        raise UpgradeError("Malformed beta pointer")
    version = payload.get("version")
    if not isinstance(version, str):
        raise UpgradeError("Malformed beta pointer")
    release_key(version)

    wheel_name = f"raven-{version}-py3-none-any.whl"
    url = f"{chan.api_base}/{quote(version, safe='')}/{wheel_name}"
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != _HOST:
        raise UpgradeError(f"Untrusted beta wheel URL: {url}")
    return ReleaseInfo(version=version, wheel_url=_authorized(url, chan))


@dataclass(frozen=True)
class PointerRead:
    """What one read of the pointer learned.

    ``release`` is ``None`` exactly when ``unchanged`` is true: a 304 carries no
    body, so the caller keeps the version it already had rather than deriving a
    new one. ``etag`` is the validator to send next time, present on both
    answers -- a 304 leaves the one we sent current.
    """

    release: ReleaseInfo | None
    etag: str | None
    unchanged: bool = False


def read_pointer(
    chan: BetaChannel | None = None,
    client: httpx.Client | None = None,
    *,
    etag: str | None = None,
) -> PointerRead:
    """Read ``latest.json``, conditionally when ``etag`` names an earlier read.

    The pointer is 23 bytes and the registry answers ``If-None-Match`` with a
    304, so looking while nothing has changed costs a request and no body. That
    is what makes a one-minute poll affordable: the cost of asking stopped
    scaling with how often we ask, which is the whole reason a tester hears
    about a new build in a minute instead of half an hour.
    """
    resolved = chan if chan is not None else channel()
    if resolved is None:
        raise UpgradeError("This Raven installation is not on the beta channel")

    url = f"{resolved.api_base}/{_POINTER_PATH}"
    headers = {"DEPLOY-TOKEN": resolved.token}
    if etag:
        headers["If-None-Match"] = etag

    def _read(http: httpx.Client) -> PointerRead:
        response = http.get(url, headers=headers)
        if response.status_code == _NOT_MODIFIED:
            return PointerRead(release=None, etag=response.headers.get("etag") or etag, unchanged=True)
        response.raise_for_status()
        return PointerRead(release=_parse_pointer(response.json(), resolved), etag=response.headers.get("etag"))

    if client is not None:
        return _read(client)
    with httpx.Client(timeout=_TIMEOUT_S, follow_redirects=True) as owned:
        return _read(owned)


def fetch_latest(chan: BetaChannel | None = None, client: httpx.Client | None = None) -> ReleaseInfo:
    """The newest published beta build. Raises ``UpgradeError`` with why not.

    Unconditional by construction: ``raven upgrade`` has to name the version it
    will install, so it may not be answered "unchanged". Callers that keep a
    cached version -- the update notice, and through it the gateway's announcer
    -- use ``read_pointer`` instead.
    """
    read = read_pointer(chan, client)
    if read.release is None:
        raise UpgradeError("The beta pointer answered 304 to a request that sent no validator")
    return read.release
