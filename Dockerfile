# syntax=docker/dockerfile:1.27@sha256:bde3983e9c939224420ddaf6b784cc30e09b035a4dea01f581230c50809f372e
# Solverr - Ultra-fast & Lightweight FlareSolverr Alternative
# Optimized for high-efficiency container deployments (UGREEN NASync / Linux / Docker)

FROM python:3.14-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PUID= \
    PGID= \
    PORT=8191 \
    HOST=0.0.0.0 \
    LOG_LEVEL=INFO \
    MAX_BROWSER_WORKERS=auto \
    XDG_CACHE_HOME=/app/.cache

# Stage 1: Python dependencies and the Camoufox browser.
FROM base AS deps
WORKDIR /app

# Pinned here so an engine bump invalidates the fetch layer. 152.0.4-beta.30 pairs with pythonlib 0.5.6.
ARG CAMOUFOX_BROWSER_VERSION=152.0.4-beta.30

# Installed before the venv exists so uv never ships in the runtime image.
# Cache mounts live under /app/.cache because XDG_CACHE_HOME points there.
RUN --mount=type=cache,target=/app/.cache/pip \
    pip install uv==0.12.5

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN --mount=type=cache,target=/app/.cache/uv \
    uv pip install --python /opt/venv/bin/python -r requirements.txt

# Every launch pins os="linux", so the macOS/Windows font sets (~890MB) are never used.
RUN python -m camoufox fetch "official/stable/${CAMOUFOX_BROWSER_VERSION}" \
    && python -m camoufox set "official/stable/${CAMOUFOX_BROWSER_VERSION}" \
    && python -m camoufox version \
    && rm -rf /app/.cache/camoufox/browsers/official/*/fonts/macos \
              /app/.cache/camoufox/browsers/official/*/fonts/windows
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends binutils \
    && find /app/.cache/camoufox -type f \( -name 'camoufox' -o -name 'camoufox-bin' -o -name '*.so*' \) \
         -exec strip --strip-unneeded {} + 2>/dev/null || true

# Stage 2: runtime image.
FROM base AS runtime
WORKDIR /app

COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Curated libs for headless Firefox; `playwright install-deps` pulls in far more (Xvfb, fonts).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
      tini curl ca-certificates gosu \
      libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
      libcairo2 libcairo-gobject2 \
      libdbus-1-3 libdbus-glib-1-2 \
      libfontconfig1 \
      libgdk-pixbuf-2.0-0 \
      libglib2.0-0 \
      libgtk-3-0 \
      libnspr4 libnss3 \
      libpango-1.0-0 libpangocairo-1.0-0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 \
      libxcomposite1 libxcursor1 libxdamage1 \
      libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 libxss1 libxtst6 \
      libdrm2 libgbm1 \
      libasound2 \
      fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

COPY --from=deps /app/.cache/camoufox /app/.cache/camoufox

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN mkdir -p data /app/home /app/.cache/camoufox/tmp /app/.cache/camoufox/fontconfig \
    && chmod 1777 /app/.cache/camoufox/tmp /app/.cache/camoufox/fontconfig \
    && chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chmod -R a+rX /app/.cache/camoufox \
    && rm -rf /usr/local/lib/python*/site-packages/setuptools* \
              /usr/local/lib/python*/site-packages/pip* \
              /opt/venv/lib/python*/site-packages/setuptools* \
              /opt/venv/lib/python*/site-packages/pip*

EXPOSE 8191

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8191/health', timeout=3)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
