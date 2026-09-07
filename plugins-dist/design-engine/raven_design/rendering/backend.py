"""Execution backends for direct and BoxLite-isolated rendering."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from raven_design.rendering.bundle import validate_published_bundle
from raven_design.rendering.models import Detection, RenderConfig, RenderError, RenderOutcome, RenderRequest
from raven_design.rendering.paths import RenderPathPolicy

_MAX_WORKER_RESPONSE_BYTES = 16 * 1024 * 1024


class RenderExecutionBackend(Protocol):
    async def run(self, request: RenderRequest, *, preview_limit: int) -> RenderOutcome: ...


class DirectRenderBackend:
    def __init__(self, config: RenderConfig, path_policy: RenderPathPolicy) -> None:
        self.config = config
        self.path_policy = path_policy
        self._engine = None

    async def run(self, request: RenderRequest, *, preview_limit: int) -> RenderOutcome:
        try:
            return await asyncio.to_thread(self._run_sync, request, preview_limit)
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.split(".", 1)[0] in {"PIL", "fitz", "playwright"}:
                raise RenderError(
                    "renderer_unavailable",
                    "Install Raven's render dependencies to use the direct backend.",
                ) from exc
            raise

    def _run_sync(self, request: RenderRequest, preview_limit: int) -> RenderOutcome:
        if self._engine is None:
            from raven_design.rendering.direct import DirectRenderEngine

            self._engine = DirectRenderEngine(self.config, self.path_policy)
        return self._engine.run(request, preview_limit=preview_limit)


class BoxLiteRenderBackend:
    _REQUEST_VERSION = 1

    def __init__(
        self,
        config: RenderConfig,
        path_policy: RenderPathPolicy,
        *,
        image: str,
        cpus: int,
        memory_mib: int,
        create_timeout_seconds: int,
    ) -> None:
        self.config = config
        self.path_policy = path_policy
        self.image = image
        self.cpus = cpus
        self.memory_mib = memory_mib
        self.create_timeout_seconds = create_timeout_seconds

    async def run(self, request: RenderRequest, *, preview_limit: int) -> RenderOutcome:
        if not self.image:
            raise RenderError(
                "renderer_unavailable",
                "The isolated renderer image is not configured.",
            )
        request = self._resolve_request(request)
        self.path_policy.runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="raven-render-worker-",
            dir=self.path_policy.runtime_root,
        ) as temporary:
            job_root = Path(temporary)
            worker_request = self._stage_request(job_root, request, preview_limit)
            request_path = job_root / "request.json"
            request_path.write_text(
                json.dumps(worker_request, separators=(",", ":"), ensure_ascii=False),
                encoding="utf-8",
            )
            executor = None
            try:
                executor = self._executor(job_root)
                await executor.start()
                result = await executor.exec(
                    "python -m raven.rendering.worker /workspace/request.json",
                    cwd=str(job_root),
                    timeout=self.config.timeout_seconds,
                    env={
                        "LANG": _posix_locale(self.config.locale),
                        "LC_ALL": _posix_locale(self.config.locale),
                        "TZ": self.config.timezone,
                    },
                )
            except ImportError as exc:
                raise RenderError(
                    "renderer_unavailable",
                    "BoxLite is not installed.",
                ) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise RenderError(
                    "renderer_unavailable",
                    "The isolated renderer could not be started.",
                    retryable=True,
                ) from exc
            finally:
                if executor is not None:
                    try:
                        await asyncio.shield(executor.stop())
                    except Exception:
                        pass
            if result.exit_code != 0:
                raise RenderError(
                    "conversion_failed",
                    "The isolated renderer failed.",
                    retryable=result.exit_code == -1,
                    details={
                        "exit_code": result.exit_code,
                        "stderr": result.stderr[-1000:],
                    },
                )
            response_path = job_root / "response.json"
            try:
                if response_path.is_symlink() or response_path.stat().st_size > _MAX_WORKER_RESPONSE_BYTES:
                    raise OSError("unsafe response file")
                response = json.loads(response_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RenderError(
                    "invalid_output",
                    "The isolated renderer returned no valid response.",
                ) from exc
            if response.get("version") != self._REQUEST_VERSION or response.get("status") != "ok":
                error = response.get("error")
                if isinstance(error, dict):
                    raise RenderError(
                        str(error.get("code") or "conversion_failed"),
                        str(error.get("message") or "The isolated renderer failed."),
                        retryable=bool(error.get("retryable")),
                    )
                raise RenderError("invalid_output", "The isolated renderer response is invalid.")
            worker_bundle = self._worker_bundle(job_root, response)
            validate_published_bundle(worker_bundle, self.config.max_output_bytes)
            published = self._publish_bundle(worker_bundle, request.output_dir)
            return _outcome_from_wire(published, response)

    def _resolve_request(self, request: RenderRequest) -> RenderRequest:
        source = self.path_policy.resolve_source(request.path)
        if source.stat().st_size > self.config.max_input_bytes:
            raise RenderError("resource_limit_exceeded", "The source exceeds the input byte limit.")
        output_dir = self.path_policy.resolve_output_dir(
            request.output_dir,
            internal=request.internal_output,
        )
        asset_root = self.path_policy.resolve_asset_root(request.asset_root)
        return RenderRequest(
            path=source,
            output_dir=output_dir,
            motion_mode=request.motion_mode,
            capture_duration_seconds=request.capture_duration_seconds,
            capture_at_seconds=request.capture_at_seconds,
            page_range=request.page_range,
            viewport_width=request.viewport_width,
            viewport_height=request.viewport_height,
            asset_root=asset_root,
            internal_output=request.internal_output,
            actions=request.actions,
            scale=request.scale,
        )

    def _stage_request(
        self,
        job_root: Path,
        request: RenderRequest,
        preview_limit: int,
    ) -> dict[str, object]:
        input_dir = job_root / "input"
        input_dir.mkdir()
        stage_root = request.asset_root
        if stage_root is None and request.path.suffix.lower() in {".html", ".htm", ".svg", ".svgz"}:
            stage_root = request.path.parent
        if stage_root is None:
            source_target = input_dir / request.path.name
            shutil.copy2(request.path, source_target)
            asset_root = None
        else:
            source_target = _copy_asset_tree(
                stage_root,
                request.path,
                input_dir,
                self.config.max_asset_bytes,
            )
            asset_root = "/workspace/input"
        return {
            "version": self._REQUEST_VERSION,
            "source": f"/workspace/{source_target.relative_to(job_root).as_posix()}",
            "output_dir": "/workspace/output",
            "asset_root": asset_root,
            "preview_limit": preview_limit,
            "options": {
                "motion_mode": request.motion_mode,
                "capture_duration_seconds": request.capture_duration_seconds,
                "capture_at_seconds": request.capture_at_seconds,
                "page_range": request.page_range,
                "viewport_width": request.viewport_width,
                "viewport_height": request.viewport_height,
                "scale": request.scale,
                "actions": list(request.actions) if request.actions else None,
            },
            "config": asdict(self.config),
        }

    def _executor(self, job_root: Path):
        from raven.sandbox.boxlite_executor import BoxliteExecutor

        if self.config.allow_network:
            allow_net: bool | list[str] = list(self.config.network_allowlist) or True
        else:
            allow_net = False
        return BoxliteExecutor(
            image=self.image,
            workspace=job_root,
            cpus=self.cpus,
            memory_mib=self.memory_mib,
            allow_net=allow_net,
            default_timeout=self.config.timeout_seconds,
            create_timeout=self.create_timeout_seconds,
        )

    @staticmethod
    def _worker_bundle(job_root: Path, response: dict[str, object]) -> Path:
        bundle_name = response.get("bundle")
        if not isinstance(bundle_name, str) or Path(bundle_name).name != bundle_name:
            raise RenderError("invalid_output", "The isolated renderer returned an unsafe bundle path.")
        return job_root / "output" / bundle_name

    def _publish_bundle(self, source: Path, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        final_dir = output_dir / source.name
        stage_dir = output_dir / f".{source.name}.{uuid.uuid4().hex[:12]}.staging"
        if final_dir.exists():
            raise RenderError("publish_failed", "The generated artifact path already exists.")
        try:
            shutil.copytree(source, stage_dir)
            validate_published_bundle(stage_dir, self.config.max_output_bytes)
            os.replace(stage_dir, final_dir)
            return final_dir
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir)


def _copy_asset_tree(
    root: Path,
    source: Path,
    target: Path,
    max_bytes: int,
) -> Path:
    root = root.resolve(strict=True)
    source = source.resolve(strict=True)
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise RenderError("path_not_allowed", "The asset root is too broad.")
    try:
        relative_source = source.relative_to(root)
    except ValueError as exc:
        raise RenderError("path_not_allowed", "The source is outside asset_root.") from exc
    total = 0
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        total += candidate.stat().st_size
        if total > max_bytes:
            raise RenderError("resource_limit_exceeded", "Local assets exceed the configured byte limit.")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, destination)
    staged_source = target / relative_source
    if not staged_source.is_file():
        raise RenderError("unsafe_input", "The source could not be staged safely.")
    return staged_source


def _posix_locale(locale: str) -> str:
    language, separator, encoding = locale.partition(".")
    normalized = language.replace("-", "_")
    return f"{normalized}.{encoding}" if separator else f"{normalized}.UTF-8"


def _outcome_from_wire(bundle_dir: Path, response: dict[str, object]) -> RenderOutcome:
    detection_payload = response.get("detection")
    if not isinstance(detection_payload, dict):
        raise RenderError("invalid_output", "The renderer response has no detection metadata.")
    try:
        detection = Detection(**detection_payload)
        page_records = _wire_records(response["page_records"], bundle_dir, "pages")
        preview_records = _wire_records(response["preview_records"], bundle_dir, "preview")
        preview_candidate_count = response["preview_candidate_count"]
        warnings = response["warnings"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RenderError("invalid_output", "The renderer response metadata is invalid.") from exc
    if (
        not isinstance(detection.metadata, dict)
        or not isinstance(detection.motion_signals, list)
        or not all(isinstance(signal, str) for signal in detection.motion_signals)
        or not isinstance(warnings, list)
        or not all(isinstance(warning, dict) for warning in warnings)
        or not isinstance(preview_candidate_count, int)
        or isinstance(preview_candidate_count, bool)
        or preview_candidate_count < len(preview_records)
    ):
        raise RenderError("invalid_output", "The renderer response metadata is invalid.")
    return RenderOutcome(
        bundle_dir=bundle_dir,
        detection=detection,
        page_records=page_records,
        preview_records=preview_records,
        preview_candidate_count=preview_candidate_count,
        warnings=warnings,
    )


def _wire_records(
    payload: object,
    bundle_dir: Path,
    directory: str,
) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not all(isinstance(record, dict) for record in payload):
        raise ValueError("records must be a list of objects")
    records: list[dict[str, Any]] = []
    for record in payload:
        relative = PurePosixPath(str(record.get("path") or ""))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.parts[0] != directory
            or relative.suffix.lower() != ".png"
            or not bundle_dir.joinpath(*relative.parts).is_file()
        ):
            raise ValueError("record path is invalid")
        records.append(record)
    return records
