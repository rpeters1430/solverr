# syntax=docker/dockerfile:1.26
# Solverr - Ultra-fast & Lightweight FlareSolverr Alternative
# Optimized for high-efficiency container deployments (UGREEN NASync / Linux / Docker)

FROM python:3.14-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PUID= \
    PGID= \
    PORT=8191 \
    HOST=0.0.0.0 \
    LOG_LEVEL=INFO \
    MAX_BROWSER_WORKERS=auto \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    XDG_CACHE_HOME=/app/.cache

# ---- Stage 1: Install Python dependencies + fetch browser engines ----
FROM base AS deps
WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

# Download Playwright Chromium and Camoufox stealth browser binaries
RUN playwright install chromium && python -m camoufox fetch

# ---- Stage 2: Final minimal runtime image ----
FROM base AS runtime
WORKDIR /app

# Copy virtual environment
COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install OS libraries for Chromium & Firefox/Camoufox, curl for healthchecks, and tini for PID 1 zombie reaping
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends tini curl ca-certificates gosu \
    && playwright install-deps chromium firefox \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Copy pre-downloaded browser engines
COPY --from=deps /ms-playwright /ms-playwright
COPY --from=deps /app/.cache/camoufox /app/.cache/camoufox

# Copy application source
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN mkdir -p data /app/.cache \
    && chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chmod -R a+rX /ms-playwright /app/.cache/camoufox

EXPOSE 8191

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8191/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
