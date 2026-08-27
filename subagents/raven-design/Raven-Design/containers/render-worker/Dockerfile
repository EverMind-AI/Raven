ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.61.0-noble@sha256:a9731514f24121d1dcd25d58d0a38146646d290a5998fd80d3e533e7b5e21c69
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c

FROM ${UV_IMAGE} AS uv
FROM ${PLAYWRIGHT_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG LIBREOFFICE_VERSION=4:24.2.7-0ubuntu0.24.04.6
ARG FFMPEG_VERSION=7:6.1.1-3ubuntu5
ARG QPDF_VERSION=11.9.0-1.1ubuntu0.1
ARG IMAGEMAGICK_VERSION=8:6.9.12.98+dfsg1-5.2build2

COPY --from=uv /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        "ffmpeg=${FFMPEG_VERSION}" \
        "imagemagick=${IMAGEMAGICK_VERSION}" \
        "libreoffice=${LIBREOFFICE_VERSION}" \
        "qpdf=${QPDF_VERSION}" \
    && command -v Xvfb \
    && command -v import \
    && fc-cache -f \
    && fc-list | LC_ALL=C sort | sha256sum > /opt/raven-renderer-fonts.sha256 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN uv pip install --system --no-cache \
    "greenlet==3.5.1" \
    "pillow==12.2.0" \
    "playwright==1.61.0" \
    "pyee==13.0.1" \
    "pymupdf==1.28.0" \
    "pypdfium2==4.30.0" \
    "typing-extensions==4.15.0"

WORKDIR /opt/raven
COPY raven/__init__.py /opt/raven/raven/__init__.py
COPY raven/rendering /opt/raven/raven/rendering

ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PYTHONPATH=/opt/raven
ENV TZ=UTC

RUN python -c "from raven.rendering.direct import DirectRenderEngine; from raven.rendering.models import RenderConfig; DirectRenderEngine(RenderConfig.discover())" \
    && /usr/bin/python3 -c "import uno" \
    && libreoffice --version \
    && ffmpeg -version \
    && qpdf --version
