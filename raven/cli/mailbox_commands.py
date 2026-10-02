"""Local mailbox administration and offline delivery commands."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import click
import typer
from pydantic import ValidationError

from raven.contracts.mailbox import MailboxClaim, MailboxError, MailboxInstanceRef, MailboxResult
from raven.mailbox.delivery import MailboxDelivery
from raven.mailbox.store import MailboxStore

_CONFLICT_CODES = {
    "ack_conflict",
    "agent_already_enrolled",
    "file_conflict",
    "id_conflict",
    "instance_reuse",
    "request_conflict",
}
_IDENTITY_CODES = {
    "artifacts_unavailable",
    "evidence_unavailable",
    "invalid_card",
    "invalid_identity",
    "invalid_policy",
    "instance_fenced",
    "kind_denied",
    "mailbox_not_initialized",
    "message_not_found",
    "root_identity_mismatch",
    "scope_denied",
    "sender_mismatch",
    "target_instance_mismatch",
    "unknown_agent",
    "unsafe_storage_path",
    "unsupported_schema",
}
_INVALID_CODES = {
    "artifact_mismatch",
    "created_in_future",
    "digest_mismatch",
    "duplicate_key",
    "envelope_too_large",
    "integer_out_of_range",
    "isolated_surrogate",
    "non_ascii_key",
    "quarantine_too_large",
    "receipt_too_large",
    "result_too_large",
    "too_deep",
    "unsupported_json_type",
    "unsupported_payload_schema",
    "unsupported_signature_profile",
    "unsupported_version",
    "float_or_nonfinite_not_allowed",
    "unsafe_artifact_path",
}
_IO_CODES = {
    "lock_busy",
    "output_error",
    "storage_busy",
    "storage_conflict",
    "storage_error",
    "storage_full",
    "storage_permission",
}


def _exit_code(code: str) -> int:
    if code in _CONFLICT_CODES:
        return 3
    if code in _IDENTITY_CODES:
        return 4
    if code in _IO_CODES:
        return 5
    if code == "quota_exceeded":
        return 6
    if code == "message_expired":
        return 7
    if code in {"stale_claim", "terminal_compacted"}:
        return 8
    if code == "commit_unknown":
        return 9
    if code in _INVALID_CODES or code.startswith("invalid_"):
        return 2
    return 9


def _payload(*, result=None, error=None) -> dict:
    return {"ok": error is None, "result": result, "error": error}


def _emit(value: dict, json_output: bool, *, error: bool = False) -> None:
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    elif error:
        typer.echo(f"mailbox error: {value['error']['code']}", err=True)
    else:
        typer.echo(json.dumps(value["result"], ensure_ascii=True, sort_keys=True, indent=2))


def _fail(code: str, retryable: bool = False, message_id: str | None = None) -> None:
    _emit(
        _payload(error={"code": code, "retryable": retryable, "message_id": message_id}),
        True,
        error=True,
    )
    raise typer.Exit(_exit_code(code))


def _execute(json_output: bool, action: Callable[[], object]) -> None:
    try:
        result = action()
    except MailboxError as exc:
        _emit(
            _payload(error={"code": exc.code, "retryable": exc.retryable, "message_id": exc.message_id}),
            json_output,
            error=True,
        )
        raise typer.Exit(_exit_code(exc.code)) from None
    except (ValueError, UnicodeDecodeError):
        _emit(
            _payload(error={"code": "invalid_argument", "retryable": False, "message_id": None}),
            json_output,
            error=True,
        )
        raise typer.Exit(2) from None
    _emit(_payload(result=result), json_output)


class _MailboxGroup(typer.core.TyperGroup):
    @staticmethod
    def _requests_json(args: list[str]) -> bool:
        return any(arg == "--json" or arg.startswith("--json=") for arg in args)

    def make_context(self, info_name: str, args: list[str], parent: click.Context | None = None, **extra):
        json_output = self._requests_json(list(args))
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        except click.ClickException:
            if not json_output:
                raise
            _fail("invalid_argument")

    def invoke(self, ctx: click.Context):
        json_output = bool(ctx.params.get("json_output")) or self._requests_json(ctx.args)
        try:
            return super().invoke(ctx)
        except click.ClickException:
            if not json_output:
                raise
            _fail("invalid_argument")


mailbox_app = typer.Typer(
    cls=_MailboxGroup,
    help="Manage the trusted-local OpenA2A mailbox.",
    no_args_is_help=True,
)


def _credential_bytes(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _read_credential(path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise MailboxError("invalid_credential_file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise MailboxError("invalid_credential_file")
            chunks = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except MailboxError:
        raise
    except OSError:
        raise MailboxError("invalid_credential_file") from None
    raw = b"".join(chunks)
    if len(raw) > max_bytes:
        raise MailboxError("invalid_credential_file")
    return raw


def _load_instance(path: Path) -> MailboxInstanceRef:
    try:
        return MailboxInstanceRef.model_validate(json.loads(_read_credential(path)))
    except MailboxError:
        raise
    except (ValueError, TypeError, ValidationError, json.JSONDecodeError):
        raise MailboxError("invalid_instance_file") from None


def _load_claims(path: Path) -> list[MailboxClaim]:
    try:
        value = json.loads(_read_credential(path))
        items = value if type(value) is list else [value]
        if not items:
            raise MailboxError("invalid_claim_file")
        return [MailboxClaim.model_validate(item) for item in items]
    except MailboxError:
        raise
    except (ValueError, TypeError, ValidationError, json.JSONDecodeError):
        raise MailboxError("invalid_claim_file") from None


def _claim_at(path: Path, index: int) -> MailboxClaim:
    if type(index) is not int or index < 0:
        raise MailboxError("invalid_argument")
    claims = _load_claims(path)
    if index >= len(claims):
        raise MailboxError("invalid_argument")
    return claims[index]


def _existing_credential(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise MailboxError("file_conflict")
            chunks = []
            while chunk := os.read(descriptor, 65536):
                chunks.append(chunk)
        finally:
            os.close(descriptor)
    except MailboxError:
        raise
    except OSError:
        raise MailboxError("file_conflict") from None
    return b"".join(chunks)


def _write_credential(path: Path, data: bytes) -> None:
    temporary: str | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if _existing_credential(path) != data:
                raise MailboxError("file_conflict") from None
            return
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except MailboxError:
        raise
    except OSError:
        raise MailboxError("output_error", retryable=True) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _write_model(path: Path | None, value: object) -> None:
    if path is not None:
        _write_credential(path, _credential_bytes(value))


def _scopes(task_ids: list[str], workspace_ids: list[str]) -> list[dict[str, str]]:
    if not task_ids or len(task_ids) != len(workspace_ids):
        raise MailboxError("invalid_policy")
    return [dict(task_id=task_id, workspace_id=workspace_id) for task_id, workspace_id in zip(task_ids, workspace_ids)]


def _envelope_file(path: Path) -> bytes:
    try:
        with path.open("rb") as source:
            raw = source.read(65537)
    except OSError:
        raise MailboxError("invalid_argument") from None
    if len(raw) > 65536:
        raise MailboxError("envelope_too_large")
    return raw


def _artifact_map(values: list[str]) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    for value in values:
        digest, separator, path = value.partition("=")
        if not separator or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest) or not path:
            raise MailboxError("invalid_argument")
        if digest in artifacts:
            raise MailboxError("invalid_argument")
        artifacts[digest] = Path(path)
    return artifacts


def _claim_payload(claims: list[MailboxClaim]) -> list[dict]:
    return [claim.model_dump() for claim in claims]


async def _receiver_rpc(credential, method, params):
    import aiohttp

    from raven.cli._terminal_rpc import _exchange, _TransportError

    request_started = False
    try:
        async with asyncio.timeout(60):
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(
                    credential["url"], headers={"X-Raven-Receiver": credential["token"]}, max_msg_size=24 * 1024 * 1024
                ) as ws:
                    request_started = True
                    frame = await _exchange(ws, method, params)
                    if "error" in frame:
                        details = frame["error"].get("data") or {}
                        raise MailboxError(
                            details.get("code", "receiver_rpc_error"),
                            retryable=details.get("retryable", False),
                            message_id=details.get("message_id"),
                        )
                    result = frame.get("result")
                    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
                        raise MailboxError("invalid_response")
                    return result["data"]
    except TimeoutError:
        raise MailboxError("commit_unknown", retryable=True) from None
    except _TransportError:
        raise MailboxError("commit_unknown", retryable=True) from None
    except (aiohttp.ClientError, OSError):
        raise MailboxError("commit_unknown" if request_started else "receiver_offline", retryable=True) from None


@mailbox_app.command("rpc")
def mailbox_rpc(
    credential_file: Path = typer.Option(..., "--credential-file"),
    method: str = typer.Option(..., "--method"),
    params_file: Path | None = typer.Option(None, "--params-file"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Call a scoped receiver method with a protected local credential."""

    def action():
        from raven.rpc.mailbox_models import RECEIVER_METHODS

        if method not in RECEIVER_METHODS:
            raise MailboxError("receiver_method_forbidden")
        try:
            credential = json.loads(_read_credential(credential_file))
            if type(credential) is not dict or set(credential) != {"url", "token"}:
                raise ValueError
            url = urlsplit(credential["url"])
            if (
                url.scheme not in {"http", "ws"}
                or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.path != "/rpc"
                or url.username
                or url.password
                or url.query
                or url.fragment
                or not url.port
                or not isinstance(credential["token"], str)
                or not 20 <= len(credential["token"]) <= 128
            ):
                raise ValueError
            params = json.loads(_read_credential(params_file)) if params_file else {}
            if type(params) is not dict:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise MailboxError("invalid_credential_file") from None
        return asyncio.run(_receiver_rpc(credential, method, params))

    _execute(json_output, action)


@mailbox_app.command("init")
def mailbox_init(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
    agent_id: str = typer.Option(..., "--agent-id"),
    request_id: str = typer.Option(..., "--request-id"),
    task_ids: list[str] = typer.Option([], "--task-id"),
    workspace_ids: list[str] = typer.Option([], "--workspace-id"),
    kinds: list[str] = typer.Option([], "--kind"),
    instance_id: str | None = typer.Option(None, "--instance-id"),
    instance_out: Path = typer.Option(..., "--instance-out"),
) -> None:
    def action():
        ref = MailboxStore(root).init(
            agent_id=agent_id,
            instance_id=instance_id,
            request_id=request_id,
            allowed_scopes=_scopes(task_ids, workspace_ids),
            allowed_kinds=kinds,
        )
        _write_model(instance_out, ref)
        return ref.model_dump()

    _execute(json_output, action)


@mailbox_app.command("resume")
def mailbox_resume(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
    agent_id: str | None = typer.Option(None, "--agent-id"),
    instance_file: Path | None = typer.Option(None, "--instance-file"),
    instance_id: str | None = typer.Option(None, "--instance-id"),
    request_id: str = typer.Option(..., "--request-id"),
    instance_out: Path = typer.Option(..., "--instance-out"),
) -> None:
    def action():
        if (agent_id is None) == (instance_file is None):
            raise MailboxError("invalid_argument")
        identity = _load_instance(instance_file) if instance_file is not None else agent_id
        ref = MailboxStore(root).resume(identity, instance_id=instance_id, request_id=request_id)
        _write_model(instance_out, ref)
        return ref.model_dump()

    _execute(json_output, action)


@mailbox_app.command("send")
def mailbox_send(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
    instance_file: Path = typer.Option(..., "--instance-file"),
    file: Path = typer.Option(..., "--file"),
    artifact: list[str] = typer.Option([], "--artifact"),
) -> None:
    def action():
        sender = _load_instance(instance_file)
        return MailboxStore(root).send(_envelope_file(file), sender, artifacts=_artifact_map(artifact))

    _execute(json_output, action)


@mailbox_app.command("poll")
def mailbox_poll(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
    instance_file: Path = typer.Option(..., "--instance-file"),
    request_id: str | None = typer.Option(None, "--request-id"),
    peek: bool = typer.Option(False, "--peek"),
    limit: int = typer.Option(1, "--limit"),
    lease_seconds: int = typer.Option(120, "--lease-seconds"),
    claim_out: Path | None = typer.Option(None, "--claim-out"),
) -> None:
    def action():
        ref = _load_instance(instance_file)
        store = MailboxStore(root)
        if peek:
            if request_id is not None or claim_out is not None:
                raise MailboxError("invalid_argument")
            return store.peek(ref, limit=limit)
        if request_id is None:
            raise MailboxError("invalid_argument")
        claims = MailboxDelivery(store).poll(
            ref,
            request_id=request_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        result = _claim_payload(claims)
        _write_model(claim_out, result)
        return result

    _execute(json_output, action)


@mailbox_app.command("ack")
def mailbox_ack(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
    instance_file: Path = typer.Option(..., "--instance-file"),
    claim_file: Path = typer.Option(..., "--claim-file"),
    claim_index: int = typer.Option(0, "--claim-index"),
    result_file: Path | None = typer.Option(None, "--result-file"),
    outcome: str | None = typer.Option(None, "--outcome"),
    summary: str | None = typer.Option(None, "--summary"),
    evidence: list[str] = typer.Option([], "--evidence"),
    retry: bool = typer.Option(False, "--retry"),
    reject: bool = typer.Option(False, "--reject"),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    def action():
        if retry and reject:
            raise MailboxError("invalid_argument")
        transition = "retry" if retry else "reject" if reject else "finish"
        claim = _claim_at(claim_file, claim_index)
        ref = _load_instance(instance_file)
        delivery = MailboxDelivery(MailboxStore(root))
        if transition in {"retry", "reject"}:
            if result_file is not None or outcome is not None or summary is not None or evidence or reason is None:
                raise MailboxError("invalid_argument")
            method = delivery.retry if transition == "retry" else delivery.reject
            return method(ref, claim, reason=reason)
        if reason is not None or (result_file is not None) == (outcome is not None):
            raise MailboxError("invalid_argument")
        if result_file is not None:
            if summary is not None or evidence:
                raise MailboxError("invalid_argument")
            try:
                raw = result_file.read_bytes()
                if len(raw) > 8192:
                    raise MailboxError("invalid_result")
                result = MailboxResult.model_validate(json.loads(raw))
            except MailboxError:
                raise
            except (OSError, ValueError, TypeError, ValidationError, json.JSONDecodeError):
                raise MailboxError("invalid_result") from None
        else:
            result = MailboxResult(outcome=outcome, summary=summary or "", evidence=evidence)
        return delivery.finish(ref, claim, result)

    _execute(json_output, action)


@mailbox_app.command("renew")
def mailbox_renew(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
    instance_file: Path = typer.Option(..., "--instance-file"),
    claim_file: Path = typer.Option(..., "--claim-file"),
    claim_index: int = typer.Option(0, "--claim-index"),
    request_id: str = typer.Option(..., "--request-id"),
    lease_seconds: int = typer.Option(120, "--lease-seconds"),
    claim_out: Path | None = typer.Option(None, "--claim-out"),
) -> None:
    def action():
        ref = _load_instance(instance_file)
        claim = _claim_at(claim_file, claim_index)
        renewed = MailboxDelivery(MailboxStore(root)).renew(
            ref,
            claim,
            request_id=request_id,
            lease_seconds=lease_seconds,
        )
        _write_model(claim_out, renewed)
        return renewed.model_dump()

    _execute(json_output, action)


@mailbox_app.command("status")
def mailbox_status(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
    agent_id: str = typer.Option(..., "--agent-id"),
    message_id: str = typer.Option(..., "--message-id"),
) -> None:
    _execute(json_output, lambda: MailboxStore(root).status(agent_id, message_id))


@mailbox_app.command("recover")
def mailbox_recover(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _execute(json_output, lambda: MailboxDelivery(MailboxStore(root)).recover())


@mailbox_app.command("flush-receipts")
def mailbox_flush_receipts(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
    instance_file: Path = typer.Option(..., "--instance-file"),
) -> None:
    def action():
        ref = _load_instance(instance_file)
        return MailboxDelivery(MailboxStore(root)).flush_receipts(ref)

    _execute(json_output, action)


@mailbox_app.command("gc")
def mailbox_gc(
    root: Path | None = typer.Option(None, "--root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _execute(json_output, lambda: MailboxDelivery(MailboxStore(root)).gc())
