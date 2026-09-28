"""Why Kimi Code would not answer, when its ACP server does not say.

Kimi Code 2.1.0 reports two kinds of failure over ACP, measured 2026-09-24
against a stand-in endpoint answering each status, and against a real account
whose plan does not include it:

- A provider refusing the credential (401, 403) comes back as a JSON-RPC error,
  ``-32000 Authentication required: <status> <the provider's message>``, on
  ``session/prompt``.
- A session it cannot start -- never signed in, no model, a provider with no
  key, a config file it cannot parse, a default model that is not configured --
  comes back as a bare ``-32000 Authentication required`` on ``session/new``,
  with nothing saying which.

Everything else the provider says is swallowed. A sign-in the auth server will
no longer refresh, 402, 404, 400, a spent quota, a content filter: the turn ends
``end_turn`` (``refusal`` for the filter) with no content and nothing on stderr.
What it counts as retryable -- 429, 5xx, a host it cannot reach, a reply it
cannot read -- it retries for about two and a half minutes first, which the
connect's wait does not outlast; that one is the general `silent` verdict
(`presets.DIAGNOSE_HINTS`), not this module's.

`kimi -p` prints the same failures in words, as ``error: failed to run prompt:
...`` on stderr, and within a second for every one it does not retry. So where
the ACP answer carries no reason, this asks Kimi Code once more in print mode --
what a reader told to run it in a terminal would see -- and reads that line.
It is only ever done for the preset's own row, launched by the preset's own
command: a row's command is its operator's execution config, and running its
executable a second way is not this module's to decide.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import tempfile
from typing import Any

from raven.agent.subagent.probe_state import Remedy

PRESET = "kimi_code"

_ASK_TIMEOUT_S = 20.0
"""How long the print-mode ask may run.

Every failure Kimi Code does not retry was printed within about a second
(measured); a rate limit with a one-second ``Retry-After`` took ten, since it
retries those at the pace the provider asks for. The ones it retries with
backoff take about 150s and are not waited for: an ask that runs out says
nothing, and the connect's own verdict stands.
"""

_FAILED = re.compile(r"^error: failed to run prompt: (.+)$", re.MULTILINE)
# With FORCE_COLOR in its environment Kimi Code prints that whole line in bright
# red (measured), which hides it from the match above.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_URL = re.compile(r"https?://[^\s)\"'}\]]+")
# The status Kimi Code puts after "provider.auth_error:" or "Authentication
# required:". The space after the colon is what keeps a port (127.0.0.1:403) out.
_STATUS = re.compile(r"(?:^|:\s+)([45]\d\d)\b")
_BARE_AUTH = "authentication required"
# Kimi Code's own reading of an account that is out of money rather than out of
# requests, from its source (`KIMI_QUOTA_EXHAUSTED_MESSAGE_PATTERNS`).
_SPENT = re.compile(
    r"exceeded your current (?:token )?quota|check your account balance|insufficient balance"
    r"|recharge your account|please recharge|account (?:is )?in arrears",
    re.IGNORECASE,
)
# The rest are the membership's own refusals, as Kimi Code's error reference
# lists them (kimi.com/code/docs, "Error Reference"). A signed-in account gets
# them as 401 or 403, so without these a spent 5-hour window or a model above
# the plan's tier would read as a refused credential -- and a signed-in account
# has no key to replace. `_CREDENTIAL` is what makes a 403 one.
_USAGE_LIMIT = re.compile(r"usage limit|concurrent request limit|quota will reset", re.IGNORECASE)
_MODEL_TIER = re.compile(
    r"does not have access to (?!kimi code\b)|supports only \S+ up to|model id does not exist|higher-tier",
    re.IGNORECASE,
)
_CREDENTIAL = re.compile(r"invalid authentication|api key|credential|unauthori[sz]ed", re.IGNORECASE)
# The in-agent command that adds, deletes and refreshes providers ("Manage AI
# providers"), where a provider's API key is put right: there is no edit, so a
# wrong key is deleted and the provider added again.
_EDIT_PROVIDER = ("kimi", "/provider")


def applies(cfg: Any) -> bool:
    """Whether ``cfg`` is the Kimi Code preset, launched by the preset's own command."""
    if getattr(cfg, "preset", None) != PRESET or getattr(cfg, "kind", None) != "acp":
        return False
    from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS

    shipped = str((THIRD_PARTY_SUBAGENT_PRESETS.get(PRESET) or {}).get("command") or "").strip()
    return bool(shipped) and (getattr(cfg, "command", None) or "").strip() == shipped


def read(cfg: Any, said: str) -> tuple[str, str, Remedy] | None:
    """What Kimi Code's own words come to: what is wrong, what to do, and the fix as data.

    ``said`` is either its ACP refusal (``Authentication required: 401 ...``) or
    the reason `kimi -p` printed. ``None`` when the words name nothing this
    knows how to fix, which leaves them to be shown as they are. The order
    matters: the membership's own refusals share the status codes a refused
    credential has, so they are read first. Any other 401 is a refused key,
    however the provider words it (one provider's, measured with a made-up key,
    is "Missing Authentication header"); a 403 is one only when its words say so.
    """
    from raven.agent.subagent.presets import model_switch_hint_for, sign_in_hint_for

    text = said.strip()
    low = text.lower()
    sign_in = sign_in_hint_for(cfg)
    login = Remedy("sign_in", sign_in.local if sign_in else None)
    with_login = f" with `{login.command}`" if login.command else ""
    switch = model_switch_hint_for(cfg)
    how_to_switch = f"`{switch.then}` in `{switch.command}`" if switch and switch.then else "another model"

    def pick(kind: Any) -> Remedy:
        return Remedy(kind, switch.command, switch.then) if switch else Remedy(kind)

    if "does not have access to kimi code" in low:
        url = _URL.search(text)
        where = url.group(0).rstrip(".,;:") if url else None
        return (
            "it is signed in, but the account's plan does not include Kimi Code",
            f"upgrade the plan{f' at {where}' if where else ''} and connect again",
            Remedy("plan", where),
        )
    if "unable to verify your membership" in low:
        return (
            "it is signed in, but Kimi could not verify the account's membership",
            "check that the subscription is active and connect again",
            Remedy("plan"),
        )
    if _USAGE_LIMIT.search(text):
        return (
            "the account has used up its current usage window or hit its concurrent request limit",
            "wait for the window to reset, or add usage to the plan, and connect again",
            pick("quota"),
        )
    if _MODEL_TIER.search(text):
        return (
            "the model it is set to use is not one the account's plan or id allows",
            f"pick {how_to_switch} and connect again",
            pick("model"),
        )
    if "authorization grant is invalid" in low or "re-login required" in low:
        return (
            "its Kimi sign-in has expired and could not be renewed",
            f"sign in again{with_login} and connect again",
            login,
        )
    if missing := re.search(r'model "([^"]+)" is not configured', text, re.IGNORECASE):
        return (
            f"its default model {missing.group(1)!r} is not one its config defines",
            f"pick another with {how_to_switch} and connect again",
            pick("model"),
        )
    if keyless := re.search(r"provider (\S+) has no credential configured", text):
        if keyless.group(1) == "managed:kimi-code":
            return ("it is not signed in to a Kimi account", f"sign in{with_login} and connect again", login)
        return (
            f"its model provider {keyless.group(1)} has no API key",
            f"add one with `{_EDIT_PROVIDER[1]}` in `{_EDIT_PROVIDER[0]}` and connect again",
            Remedy("setup", *_EDIT_PROVIDER),
        )
    if low.startswith("no model configured") or low.startswith("no provider configured"):
        return (
            "it has no model to use: it is not signed in and has no model provider set up",
            f"sign in{with_login} and connect again",
            login,
        )
    if _SPENT.search(text):
        return (
            "its model provider refused the call: the account's balance or quota is spent",
            f"top it up with the provider, or pick {how_to_switch}, and connect again",
            pick("billing"),
        )
    status = _STATUS.search(text)
    code = status.group(1) if status else None
    if code == "401" or (code == "403" and _CREDENTIAL.search(text)):
        return (
            f"its model provider refused the API key it is set up with ({code})",
            f"delete the provider and add it again with a valid key with `{_EDIT_PROVIDER[1]}` in "
            f"`{_EDIT_PROVIDER[0]}`, and connect again",
            Remedy("setup", *_EDIT_PROVIDER),
        )
    if code == "402":
        return (
            "its model provider refused the call for want of credit",
            f"top it up with the provider, or pick {how_to_switch}, and connect again",
            pick("billing"),
        )
    if code == "404" or "provider.not_found" in low:
        return (
            "its model provider does not serve the model it is set to use",
            f"pick {how_to_switch} and connect again",
            pick("model"),
        )
    if code == "429" or "provider.rate_limit" in low:
        return (
            "its model provider is rate-limiting it or its quota is spent",
            f"wait and connect again, or pick {how_to_switch}",
            pick("quota"),
        )
    return None


def _told(verdict: tuple[str, str, Remedy], said: str) -> tuple[str, Remedy]:
    lead, advice, remedy = verdict
    return f"{lead}; {advice}. It said: {said}", remedy


def _unstarted(shown: str) -> tuple[str, Remedy]:
    return (
        "it started but never finished its ACP handshake, which Kimi Code does in under a second, so the `kimi` "
        f"on PATH is not a Kimi Code that speaks ACP; install the current one and connect again. It said: {shown}",
        Remedy("upgrade"),
    )


async def explain(cfg: Any, failure: BaseException | None, *, prompt: str) -> tuple[str, Remedy | None] | None:
    """What a Kimi Code ping that did not answer comes to, or ``None`` to leave it to the general reading.

    ``failure`` is what the ping raised, or ``None`` for one that finished and
    said nothing; ``prompt`` is the ping's own, which the print-mode ask sends
    too. The detail is the English record; the remedy, when one is known, is
    the same verdict as data.
    """
    if not applies(cfg):
        return None
    from raven.acp_client.acp_agent import AcpEmptyTurnError
    from raven.acp_client.protocol import AcpTimeoutError, reason_of, remote_error_in

    shown = str(failure) if failure is not None else "it started and then answered nothing"
    refusal = remote_error_in(failure) if failure is not None else None
    if refusal is not None:
        said = reason_of(refusal)
        if (known := read(cfg, said)) is not None:
            return _told(known, shown)
        if said.strip().rstrip(".").lower() == _BARE_AUTH:
            return await _ask(cfg, shown, prompt, unread=_login(cfg))
        return None
    timed_out = _in_chain(failure, AcpTimeoutError)
    if timed_out is not None and timed_out.method == "initialize":
        return _unstarted(shown)
    if failure is None or _in_chain(failure, AcpEmptyTurnError) is not None:
        return await _ask(cfg, shown, prompt, unread=None)
    return None


async def explain_refusal(cfg: Any, detail: str, *, needs_auth: bool, prompt: str) -> tuple[str, Remedy | None] | None:
    """A handshake Kimi Code refused, explained the way a refused ping is.

    ``detail`` is the snapshot's, e.g. "connected, but no session could be
    opened: Authentication required (auth methods: login)". Its bare refusal is
    asked about, a refusal with words of its own is read as it is, and a start
    that never finished its handshake is named the way the connect names it.
    """
    if not applies(cfg):
        return None
    if not needs_auth:
        return _unstarted(detail) if "initialize timed out" in detail else None
    reason = detail.split("no session could be opened:", 1)[-1].split(" (auth methods:", 1)[0].strip()
    if (known := read(cfg, reason)) is not None:
        return _told(known, detail)
    if reason.rstrip(".").lower() == _BARE_AUTH:
        return await _ask(cfg, detail, prompt, unread=_login(cfg))
    return None


def _login(cfg: Any) -> Remedy:
    """The sign-in the general reading gives a bare credential refusal, kept when the ask says nothing known."""
    from raven.agent.subagent.presets import sign_in_hint_for

    hint = sign_in_hint_for(cfg)
    return Remedy("sign_in", hint.local if hint else None)


async def _ask(cfg: Any, shown: str, prompt: str, *, unread: Remedy | None) -> tuple[str, Remedy | None] | None:
    """Ask Kimi Code in print mode why, and read its answer the way its ACP refusal is read.

    ``unread`` is the remedy to keep when Kimi Code answers in words this does
    not know: a bare "Authentication required" still reads as a sign-in, as it
    did before it was asked about, while an empty turn has no such default.
    """
    code, said = await ask(cfg, prompt)
    if code is None:
        return None
    if code == 0 and not said:
        return (
            f"{shown}; asked again with `kimi -p`, it answered, so the failure may have passed: connect again",
            None,
        )
    if not said:
        return None
    if said.lower().startswith("no model configured") and (broken := await doctor(cfg)) is not None:
        # A config file that does not parse reads as "no model" to `kimi -p`
        # (measured: a TOML syntax error), and signing in again would not fix it.
        return (
            f"it cannot read its config file; `kimi doctor config` says where. It said: {broken}",
            Remedy("config", "kimi doctor config"),
        )
    if (known := read(cfg, said)) is not None:
        return _told(known, said)
    return f"{shown}; asked again with `kimi -p`, it said: {said}", unread


async def ask(cfg: Any, prompt: str) -> tuple[int | None, str]:
    """Kimi Code's own account of why it will not answer ``prompt``: `kimi -p`'s exit code and error line.

    ``(None, "")`` when it could not be run or did not finish in time. Like the
    connect's own ping, each ask is a session in Kimi Code's own history.
    """
    code, out = await _run(cfg, "-p", prompt)
    found = _FAILED.search(_ANSI.sub("", out))
    return code, found.group(1).strip() if found else ""


async def doctor(cfg: Any) -> str | None:
    """What `kimi doctor config` finds wrong with Kimi Code's config file, or ``None`` for nothing."""
    code, out = await _run(cfg, "doctor", "config")
    if not code:
        return None
    lines = [line.strip() for line in _ANSI.sub("", out).splitlines() if line.strip()]
    # "ERROR config.toml <path>", then the reason: one line for a file that does
    # not parse, a heading and the issues under it for one that does not validate.
    for i, line in enumerate(lines):
        if line.startswith("ERROR"):
            return " ".join(lines[i + 1 : i + 5]) or line
    return None


async def _run(cfg: Any, *args: str) -> tuple[int | None, str]:
    """Run the row's `kimi` with ``args`` in the environment its ACP launch gets, in a fresh directory.

    The process group goes on any way out -- a timeout, or the connect itself
    being cancelled when the page that asked goes away -- so an ask never
    outlives the question.
    """
    from raven.agent.subagent.backends.env import host_identity_env, login_shell_env
    from raven.agent.subagent.role import subagent_role_env

    # The map `AcpClient.launch` builds, so the `kimi` asked is the one the
    # connect ran, reading the same home and config.
    base = await asyncio.to_thread(login_shell_env)
    env = {**base, **host_identity_env(), **subagent_role_env(), **(getattr(cfg, "env", None) or {})}
    with tempfile.TemporaryDirectory(prefix="raven_subagent_ask_") as cwd:
        try:
            proc = await asyncio.create_subprocess_exec(
                "kimi",
                *args,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            return None, ""
        try:
            out, err = await asyncio.wait_for(proc.communicate(), _ASK_TIMEOUT_S)
        except TimeoutError:
            return None, ""
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), 1.0)
    return proc.returncode, (out + err).decode("utf-8", "replace")


def _in_chain(exc: BaseException | None, kind: type[BaseException]) -> Any:
    """``exc`` or the first of its causes that is a ``kind``, else ``None``."""
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, kind):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


__all__ = ["PRESET", "applies", "ask", "doctor", "explain", "explain_refusal", "read"]
