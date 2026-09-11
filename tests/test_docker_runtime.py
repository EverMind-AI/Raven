"""Docker Compose runtime defaults and browser bootstrap contract."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _ignored(path: str) -> bool:
    """Does git ignore this path? Asked of git, because git is what decides.

    `--no-index` because the question is whether the rules cover the path, not
    whether this checkout happens to track it: `check-ignore` consults the index
    by default and never calls a tracked file ignored, which would make the
    guard below pass for a rule broad enough to swallow the committed template.

    `check-ignore` exits 0 when the path ends up ignored and 1 when it does not
    -- a trailing `!` negation resolves to 1, so the two outcomes are a clean
    boolean. Anything else (no git, no work tree) is not an answer, and the
    caller skips rather than guesses.
    """
    try:
        done = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "--", path],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git missing from the image
        pytest.skip("git is not on PATH, so its resolver cannot be asked")
    if done.returncode not in (0, 1):
        pytest.skip("not a git work tree, so there is no ignore rule to resolve")
    return done.returncode == 0


#: Every ``.env.local`` the repo tells a reader to write a credential into.
#: Docker's is the one the docs name five times; the agents ask for the same
#: neighbour through their ``.env.example`` files, and ``publish_beta`` already
#: refuses a wheel that packages one.
CREDENTIAL_FILES = (
    "docker/.env.local",
    ".env.local",
    "agents/raven-code/.env.local",
    "agents/raven-design/.env.local",
    "agents/raven-oncall/.env.local",
)


def test_every_env_local_the_docs_point_at_is_actually_ignored() -> None:
    """The promise five files make, checked against the rule that keeps it.

    `.gitignore` said "which the rule above still ignores" beside a rule that
    did not: `*.env` matches a name ending in `.env`, and these end in `.local`.
    Nothing reached them, so a reader who followed the instructions had a real
    key staged by the next `git add -A` with nothing warning them. A comment is
    not a gate, which is why this asks git instead of reading the file.
    """
    unguarded = [path for path in CREDENTIAL_FILES if not _ignored(path)]
    assert not unguarded, f"a credential file git would happily stage: {unguarded}"


def test_the_committed_compose_template_stays_tracked() -> None:
    """The other half: an over-broad rule would silently drop the template.

    `docker/.env` is committed on purpose -- it carries the defaults and empty
    placeholders -- and is re-included by name. Widening the family rule to
    something like `*.env*` would swallow it, and the failure would be a fresh
    clone with no settings rather than anything this suite otherwise notices.
    """
    assert not _ignored("docker/.env")
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", "docker/.env"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if tracked.returncode not in (0, 1):
        pytest.skip("not a git work tree, so tracking cannot be resolved")
    assert tracked.returncode == 0, "docker/.env is no longer committed"


def test_compose_publishes_nginx_and_defaults_to_gateway() -> None:
    compose = (ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8")
    env = (ROOT / "docker/.env").read_text(encoding="utf-8")

    assert '"127.0.0.1:${RAVEN_WEB_PORT:-18793}:80"' in compose
    assert re.search(r"^RAVEN_WEB_PORT=18793$", env, re.MULTILINE)
    assert re.search(r"^RAVEN_PAGE_PORT=18793$", env, re.MULTILINE)
    assert re.search(r"^RAVEN_AUTO_LOGIN=1$", env, re.MULTILINE)


def test_container_bootstraps_the_browser_after_the_engine_is_ready() -> None:
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    nginx = (ROOT / "docker/nginx/raven.conf.template").read_text(encoding="utf-8")

    assert entrypoint.index("wait_for_engine || boot=$?") < entrypoint.index("start_nginx\n        announce")
    assert 'raven gateway --page-port "${PAGE_PORT}" &' in entrypoint
    assert 'RAVEN_AUTO_LOGIN_COOKIE="raven_session_${PAGE_PORT}=' in entrypoint
    assert 'add_header Set-Cookie "${RAVEN_AUTO_LOGIN_COOKIE}" always;' in nginx


def test_container_exposes_no_engine_selector() -> None:
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    readme = (ROOT / "docker/README.md").read_text(encoding="utf-8")
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese_readme = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    env = (ROOT / "docker/.env").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "RAVEN_ENGINE" not in entrypoint
    assert "RAVEN_ENGINE" not in readme
    assert "RAVEN_ENGINE" not in root_readme
    assert "RAVEN_ENGINE" not in chinese_readme
    assert "RAVEN_ENGINE" not in env
    assert "RAVEN_ENGINE" not in dockerfile
    assert "raven serve --port" not in entrypoint


@pytest.mark.parametrize(
    ("api_key", "api_base", "expected"),
    [
        ("sk-test", "", "provider\nset\nopenrouter\n--api-key\nsk-test\n"),
        (
            "",
            "http://host.docker.internal:11434",
            "provider\nset\nollama-chat\n--api-base\nhttp://host.docker.internal:11434\n",
        ),
        (
            "optional-key",
            "http://host.docker.internal:11434",
            "provider\nset\nollama-chat\n--api-key\noptional-key\n--api-base\nhttp://host.docker.internal:11434\n",
        ),
    ],
)
def test_environment_seeding_passes_each_available_credential(
    tmp_path: Path,
    api_key: str,
    api_base: str,
    expected: str,
) -> None:
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    definitions = entrypoint.split("start_nginx()", 1)[0]
    harness = tmp_path / "seed-provider.sh"
    harness.write_text(f"{definitions}\nseed_provider\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    raven = bin_dir / "raven"
    raven.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n', encoding="utf-8")
    raven.chmod(0o755)
    capture = tmp_path / "arguments"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
        "RAVEN_PROVIDER": "ollama-chat" if "11434" in api_base else "openrouter",
        "RAVEN_API_KEY": api_key,
        "RAVEN_API_BASE": api_base,
    }

    subprocess.run(["sh", str(harness)], check=True, env=env, capture_output=True, text=True)

    assert capture.read_text(encoding="utf-8") == expected


def test_environment_seeding_skips_empty_credentials(tmp_path: Path) -> None:
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    definitions = entrypoint.split("start_nginx()", 1)[0]
    harness = tmp_path / "seed-provider.sh"
    harness.write_text(f"{definitions}\nseed_provider\n", encoding="utf-8")
    env = {
        **os.environ,
        "RAVEN_PROVIDER": "openrouter",
        "RAVEN_API_KEY": "",
        "RAVEN_API_BASE": "",
    }

    result = subprocess.run(["sh", str(harness)], check=True, env=env, capture_output=True, text=True)

    assert "configuring provider" not in result.stdout
