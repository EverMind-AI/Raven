"""Compose local adapters into the direct rendering engine."""

from __future__ import annotations

from typing import Any

from raven.rendering.models import RenderConfig, RenderOutcome, RenderRequest
from raven.rendering.paths import RenderPathPolicy


class DirectRenderEngine:
    def __init__(
        self,
        config: RenderConfig,
        path_policy: RenderPathPolicy | None = None,
    ) -> None:
        from raven.rendering.animated_image import AnimatedImageAdapter
        from raven.rendering.browser import BrowserAdapter
        from raven.rendering.office import LibreOfficeBackend, OfficeRouter
        from raven.rendering.pdf import PyMuPdfBackend
        from raven.rendering.pipeline import RenderPipeline
        from raven.rendering.preview import PreviewBuilder

        self.config = config
        self.previews = PreviewBuilder(config)
        self.pdf = PyMuPdfBackend()
        self.office = LibreOfficeBackend(config)
        self.pipeline = RenderPipeline(
            config,
            {
                "browser": BrowserAdapter(config, self.pdf),
                "office": OfficeRouter(
                    config.office_backend_order,
                    {"libreoffice": self.office},
                    allow_cloud=config.allow_cloud_office,
                ),
                "animated_image": AnimatedImageAdapter(config, self.pdf),
            },
            self.previews,
            path_policy=path_policy,
            pdf_backend=self.pdf,
        )

    def run(self, request: RenderRequest, *, preview_limit: int) -> RenderOutcome:
        return self.pipeline.run(request, preview_limit=preview_limit)

    def capabilities(self) -> dict[str, Any]:
        return {
            "browser": bool(self.config.chrome_path),
            "office": self.office.available(),
            "animated_image": True,
            "pdf": True,
        }
