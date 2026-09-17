# syntax=docker/dockerfile:1.27@sha256:bde3983e9c939224420ddaf6b784cc30e09b035a4dea01f581230c50809f372e
# Solverr - Ultra-fast & Lightweight FlareSolverr Alternative
# Optimized for high-efficiency container deployments (UGREEN NASync / Linux / Docker)

FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f AS base
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

# ---- Stage 1: Install Python dependencies + fetch browser engines ----
FROM base AS deps
WORKDIR /app

# Pin the Camoufox browser engine separately from the Python wrapper package.
# Keeping this explicit makes Docker rebuild the browser-fetch layer whenever
# the upstream engine is bumped instead of silently reusing an older cached
# browser binary. Camoufox 152.0.4-beta.30 is paired with pythonlib 0.5.6.
ARG CAMOUFOX_BROWSER_VERSION=152.0.4-beta.30

# uv resolves and installs far faster than pip; pinned to a specific PyPI
# release for reproducible builds. Installed into the base image's system
# site-packages (before the venv PATH switch below) so the uv binary itself
# never ends up copied into the venv that ships in the runtime image.
# (The pip cache mount here previously did nothing: PIP_NO_CACHE_DIR=1 plus
# --no-cache-dir told pip never to populate its cache dir in the first place.
# The mount targets /app/.cache/pip, not the usual /root/.cache/pip, because
# XDG_CACHE_HOME=/app/.cache is set above and both pip and uv follow the XDG
# base-directory spec on Linux - pointing the mount at /root/.cache would
# just recreate the same class of dead-cache bug against a path neither
# tool ever writes to.)
RUN --mount=type=cache,target=/app/.cache/pip \
    pip install uv==0.12.5

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN --mount=type=cache,target=/app/.cache/uv \
    uv pip install --python /opt/venv/bin/python -r requirements.txt

# Fetch and activate the exact Camoufox stealth Firefox browser binary, then
# trim it down. Passing the version directly prevents the image from drifting
# with the remote channel, while the ARG guarantees a version bump invalidates
# this Docker layer.
# - Fingerprint generation is pinned to os="linux" (app/solver/browser.py),
#   so the macos/windows font sets Camoufox also downloads by default are
#   dead weight (~890MB) - only ship the font set that's ever used.
# - Debug symbols in the Firefox binary/shared libs aren't needed at runtime.
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

# ---- Stage 2: Final minimal runtime image ----
FROM base AS runtime
WORKDIR /app

# Copy virtual environment
COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install OS libraries for Firefox/Camoufox and tini
# for PID 1 zombie reaping. This is a curated list (not `playwright
# install-deps firefox`, which pulls in a much larger transitive closure -
# Xvfb, X11 utilities, extra font packages - built to support any Playwright
# browser robustly, not just headless Camoufox/Firefox).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
      tini ca-certificates gosu \
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

# Copy the pre-fetched Camoufox browser engine
COPY --from=deps /app/.cache/camoufox /app/.cache/camoufox

# Copy application source
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
