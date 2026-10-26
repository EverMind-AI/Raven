"""Third-party OpenAI-compatible HTTP agent backend (mirothinker, …) as a
spawned sub-agent.

Each call is one Chat Completions request. A resumed instance replays its
prior ``history`` as the leading messages instead of the endpoint holding any
session state itself; see ``run``'s ``history``/``on_messages`` pair. The API
key lives in config; the raw JSON is read directly so provider extension
fields survive. ``reasoning_steps`` is one of those: it is how a deep-research
endpoint reports the work behind an answer, and it reaches the run's own record
through :mod:`raven.agent.subagent.openai_steps`.

An entry that names a ``machine`` is posted to through that registered
machine: each turn opens an ssh tunnel to it (:func:`raven.acp_client.remote.tunnel`)
and the request goes to ``base_url`` as the machine resolves it, for an
endpoint that answers only on that machine's network, or only on the machine
itself. The tunnel ends with the turn. The files that came with the task
are named to such an entry by their address on this computer, nothing
being copied to the machine, as the acp and cli lanes to a machine say it
(:func:`raven.acp_client.remote.files_note`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json as jsonlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from loguru import logger

from raven.agent.subagent import activity
from raven.agent.subagent.attachments import with_attachment_note
from raven.agent.subagent.backends import turn_rows
from raven.agent.subagent.backends.base import bounded_delta, clamp_output
from raven.agent.subagent.openai_steps import OpenAIStepReader
from raven.contracts.subagent_backend import SubagentNoAnswerError
from raven.spine.message import Media

if TYPE_CHECKING:
    from raven.acp_client.remote import Machine
    from raven.contracts.llm_provider import LLMProvider


def _with_files(task: str, media: Sequence[Media], target: Machine | None) -> str:
    """The task with the files that came with it named at the end, the way the entry's reader can use them.

    From this computer, by absolute path (``with_attachment_note``). On a
    machine, by their address on this computer and with the word that nothing
    was copied there (``remote.files_note``), as the acp and cli lanes to a
    machine say it: the endpoint answers from the machine, where a path of
    this computer's names nothing unless the disk is shared.
    """
    if target is None:
        return with_attachment_note(task, media)
    from raven.acp_client import remote

    note = remote.files_note(media, target)
    return f"{task}\n\n{note}" if note else task


class OpenAIApiBackend:
    kind = "openai"
    streams = True

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str = "",
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
        max_output_chars: int = 128000,
        machine: str | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        # The registered machine the endpoint is reached from, or None for this computer.
        self.machine = (machine or "").strip() or None

    async def _placed(self) -> Machine | None:
        """The entry's machine, as the registry has it; ``None`` for an entry posted to from this computer."""
        if not self.machine:
            return None
        from raven.acp_client import remote

        try:
            return await asyncio.to_thread(remote.machine, self.machine)
        except remote.RemoteMachineError as exc:
            raise RuntimeError(f"OpenAI-API agent {self.name!r}: {exc}") from None

    @contextlib.asynccontextmanager
    async def _reached(
        self, url: str, headers: dict[str, str], target: Machine | None
    ) -> AsyncIterator[tuple[str, dict[str, str]]]:
        """``url`` and ``headers`` as this turn posts them.

        As they are from this computer, for no ``target``; through a tunnel
        from the entry's machine otherwise, opened for this turn and closed
        after it. The request then arrives at a port on this computer's
        loopback: ``Host`` carries the name the server knows itself by, and
        the proxy variables of this computer's environment are not read for
        it (``_post_chat``), since a proxy would take a loopback address
        somewhere else.
        """
        if target is None:
            yield url, headers
            return
        from raven.acp_client import remote

        try:
            log = await asyncio.to_thread(remote.ssh_log_path, f"{self.name}-tunnel")
        except remote.RemoteMachineError as exc:
            raise RuntimeError(f"OpenAI-API agent {self.name!r}: {exc}") from None
        try:
            # Emptied per turn, so a failure is read from this turn's lines.
            log.write_text("", encoding="utf-8")
        except OSError:
            pass
        try:
            async with remote.tunnel(target, url, ssh_log=log) as reached:
                yield reached.url, {**headers, "Host": reached.host}
        except remote.RemoteMachineError as exc:
            raise RuntimeError(f"OpenAI-API agent {self.name!r}: {exc}") from None
        except aiohttp.ClientConnectionError as exc:
            raise RuntimeError(
                f"OpenAI-API agent {self.name!r}: its endpoint did not answer through the tunnel from machine "
                f"{target.label}: {exc}"
            ) from None

    async def _post_chat(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: "aiohttp.ClientTimeout",
    ) -> dict[str, Any]:
        # Required, not defaulted to `None`: aiohttp reads a bare `None` (or an
        # omitted argument) as its own 5-minute default, not "no cap" -- see the
        # `client_timeout` construction at the call site. A required parameter
        # means a caller can't silently reintroduce that cap by forgetting it.
        # `trust_env` also reads HTTP_PROXY/HTTPS_PROXY/NO_PROXY, which aiohttp
        # otherwise ignores -- unlike httpx (the rest of the codebase), which honors
        # them by default. On a host that can only reach the provider through a
        # proxy, a direct connection is not a connection error but whatever the
        # provider says to an unexpected origin (mirothinker: HTTP 451). Not
        # for a row on a machine: its request goes to a port on this computer's
        # loopback (``_reached``), which a proxy would take somewhere else.
        async with aiohttp.ClientSession(timeout=timeout, trust_env=not self.machine) as session:
            async with session.post(url, json=json, headers=headers) as resp:
                await self._raise_for_status(resp)
                return await resp.json()

    async def _raise_for_status(self, resp: "aiohttp.ClientResponse") -> None:
        """Fail with the endpoint's own words, whichever way the reply was asked for.

        Shared by the buffered and streamed paths so the two cannot report the
        same upstream refusal differently.
        """
        if resp.status != 200:
            text = (await resp.text())[:2000]
            raise RuntimeError(f"OpenAI-API agent {self.name!r} HTTP {resp.status}: {text}")

    async def _stream_chat(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: "aiohttp.ClientTimeout",
        on_delta: Callable[[str], Awaitable[None]],
        reader: OpenAIStepReader,
        task_id: str,
        session_key: str | None,
    ) -> str:
        """Consume a Chat Completions SSE response, returning the assembled text.

        ``jsonlib`` rather than ``json``: the sibling ``_post_chat`` takes the
        request body in a parameter of that name.

        An unparsable or unrecognised frame is skipped rather than raised on --
        the endpoints this backend targets are only nominally OpenAI-compatible,
        and one keep-alive comment must not fail a turn that is answering fine.
        A stream that yields nothing at all still returns "", which ``run``
        treats exactly as an empty non-streamed reply.

        ``reader`` is the caller's rather than one built here: ``run`` owns one
        per call, so the transcript this loop republishes as the stream runs and
        the settled one published after it are built from one accumulator.
        """
        content: list[str] = []
        reasoning: list[str] = []
        usage: Any = None
        async with aiohttp.ClientSession(timeout=timeout, trust_env=not self.machine) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                await self._raise_for_status(resp)
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        frame = jsonlib.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    # Read before the delta guard below, which drops a frame
                    # carrying no choices -- and OpenAI's own `include_usage`
                    # frame is exactly that. Kept rather than reported here
                    # because `note_usage` sums: a provider that repeats its
                    # cumulative total on every frame would have the run's cost
                    # multiplied by the frame count, so the last report wins and
                    # is published once the stream ends.
                    if frame.get("usage"):
                        usage = frame["usage"]
                    choices = frame.get("choices")
                    first = (choices or [{}])[0]
                    delta = first.get("delta") if isinstance(first, dict) else None
                    if not isinstance(delta, dict):
                        continue
                    piece = delta.get("content")
                    if piece:
                        content.append(str(piece))
                        await on_delta(str(piece))
                    thought = delta.get("reasoning_content") or delta.get("reasoning")
                    if thought:
                        reasoning.append(str(thought))
                    # A different field from the two above, serving a different
                    # purpose: those are the answer's fallback, these are the work
                    # that produced it. Republished per step rather than once at
                    # the end, because a panel watching the run reads the activity
                    # and a transcript that only exists after the answer is not a
                    # live view of anything.
                    steps = delta.get("reasoning_steps")
                    for step in steps if isinstance(steps, list) else []:
                        reader.feed_delta(step)
                        activity.note_transcript(turn_rows.rows(reader.events()))
        await activity.note_provider_usage(
            usage,
            model=self.model,
            session_key=session_key,
            task_id=task_id,
        )
        # Same fallback as the non-streamed path: some reasoning models put the
        # answer only in a reasoning field. It is collected but never streamed --
        # the wire's reasoning event carries no instance tag, so a direct chat's
        # thinking would land in the main transcript.
        return "".join(content) or "".join(reasoning)

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        provider: LLMProvider | None = None,
        model: str | None = None,
        mcps: list[str] | None = None,
        mcp_grant: Any = None,
        mode: str | None = None,
        authored_task: str | None = None,
        history: list[dict[str, Any]] | None = None,
        on_messages: Callable[[list[dict[str, Any]]], None] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        media: Sequence[Media] = (),
    ) -> str:
        # The parent's provider/model are accepted and ignored: this backend
        # posts to its own configured endpoint under its own ``self.model``.
        url = self.base_url.rstrip("/") + "/chat/completions"
        # A resumed instance brings its own history, system prompt included;
        # rebuilding it here would append a second system turn.
        messages: list[dict[str, Any]] = list(history) if history else []
        if not messages and self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        target = await self._placed()
        messages.append({"role": "user", "content": _with_files(task, media, target)})
        # Always explicit, never omitted: a provider whose default is SSE
        # (mirothinker) answers an omitted `stream` with text/event-stream, which
        # `resp.json()` cannot read. A spawn asks for false and keeps the single
        # JSON response it has always parsed; only a caller watching the reply
        # form asks for true.
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": on_delta is not None}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        logger.info("Subagent [{}] OpenAI-API agent {!r}: {}", task_id, self.name, self.model)
        # aiohttp treats both a bare `timeout=None` and an omitted `timeout` as
        # "use aiohttp's own 5-minute default", so an explicit ClientTimeout
        # instance with total=None is the only way to mean "no automatic cap".
        # `total` stays unbounded as intended, but `connect` is still capped:
        # it covers both DNS resolution and the socket connect (`sock_connect`
        # alone would leave a hung resolver unbounded), so a stall before the
        # request is even sent can't hang forever -- a different failure than
        # a slow response body, which `total=None` still allows.
        client_timeout = aiohttp.ClientTimeout(total=self.timeout, connect=30)

        reader = OpenAIStepReader()
        async with self._reached(url, headers, target) as (url, headers):
            if on_delta is not None:
                content: Any = await self._stream_chat(
                    url,
                    payload=body,
                    headers=headers,
                    timeout=client_timeout,
                    on_delta=bounded_delta(on_delta, self.max_output_chars),
                    reader=reader,
                    task_id=task_id,
                    session_key=session_key,
                )
            else:
                data = await self._post_chat(url, json=body, headers=headers, timeout=client_timeout)
                try:
                    message = data["choices"][0]["message"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise RuntimeError(f"OpenAI-API agent {self.name!r}: unexpected response shape") from exc
                content = message.get("content")
                if not content:
                    # Some reasoning models put the answer only in a reasoning field.
                    content = message.get("reasoning_content") or message.get("reasoning") or ""
                reader.feed_steps(message.get("reasoning_steps"))
                await activity.note_provider_usage(
                    data.get("usage"),
                    model=self.model,
                    session_key=session_key,
                    task_id=task_id,
                )
        # The answer is deliberately not among these rows: the record keeps it and
        # its reader appends it as the closing message, the same contract the acp
        # lane follows.
        activity.note_transcript(turn_rows.rows(reader.events()))
        reply = await clamp_output(str(content).strip(), self.max_output_chars, agent=self.name, sink=on_delta)
        if not reply:
            # A reply with nothing visible in it is no answer, as the in-process
            # backend reads one: handed back as "", the run was recorded
            # completed with nothing as its result (manager.py, dag_runner.py).
            raise SubagentNoAnswerError(f"OpenAI-API agent {self.name!r}: the model's reply was empty")
        if on_messages is not None:
            on_messages([*messages, {"role": "assistant", "content": reply}])
        return reply
