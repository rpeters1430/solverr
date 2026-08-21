# syntax=docker/dockerfile:1.7
# Solverr - Ultra-fast & Lightweight FlareSolverr Alternative
# Optimized for high-efficiency container deployments (UGREEN NASync / Linux / Docker)

FROM python:3.14-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PORT=8191 \
    HOST=0.0.0.0 \
    LOG_LEVEL=INFO \
    MAX_BROWSER_WORKERS=auto \
    PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

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
    && apt-get install -y --no-install-recommends tini curl ca-certificates \
    && playwright install-deps chromium firefox \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Copy pre-downloaded browser engines
COPY --from=deps /root/.cache/ms-playwright /root/.cache/ms-playwright
COPY --from=deps /root/.cache/camoufox /root/.cache/camoufox

# Copy application source
COPY app ./app
RUN mkdir -p data

EXPOSE 8191

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8191/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
