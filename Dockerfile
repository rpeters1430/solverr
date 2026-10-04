# syntax=docker/dockerfile:1.27@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# Solverr - Ultra-fast & Lightweight FlareSolverr Alternative
# Optimized for high-efficiency container deployments (UGREEN NASync / Linux / Docker)

FROM python:3.14-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151 AS base
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

# Pinned here so an engine bump invalidates the fetch layer. Must match the browser
# pythonlib pairs with (camoufox/browser-pin.json): 0.5.7 pairs with 156.0.1-beta.34.
ARG CAMOUFOX_BROWSER_VERSION=156.0.1-beta.34

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
# A bare `fetch` installs pythonlib's paired browser (no prerelease prompt) and seeds the
# pinned fpgen model; it exits 0 on failure, so assert the pinned build actually landed.
RUN python -m camoufox fetch \
    && python -m camoufox set "official/prerelease/${CAMOUFOX_BROWSER_VERSION}" \
    && ls -d /app/.cache/camoufox/browsers/official/${CAMOUFOX_BROWSER_VERSION}* \
    && python -m camoufox version
# Newer bundles store each font once under an OS-set group (L, LM, LMW, ..., per
# fonts/groups.json); older ones ship fonts/<os>/. Keep only what Linux reads, and fail on
# an unrecognised layout so a future change can't silently ship the other OSes' fonts again.
RUN python <<'EOF'
import json, pathlib, shutil
dirs = list(pathlib.Path("/app/.cache/camoufox/browsers").glob("*/*/fonts"))
assert dirs, "no Camoufox fonts directory found"
for fonts in dirs:
    groups = fonts / "groups.json"
    if groups.exists():
        meta = json.loads(groups.read_text())
        drop = set(meta["groups"]) - set(meta["readBy"]["lin"])
    elif (fonts / "linux").is_dir():
        drop = {"macos", "windows"}
    else:
        raise SystemExit(f"unrecognised Camoufox font layout in {fonts}")
    for name in sorted(drop):
        if (fonts / name).is_dir():
            shutil.rmtree(fonts / name)
            print("pruned", fonts / name)
EOF
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
# fpgen's model lives in the root-owned venv and is refetched once its mtime is five weeks
# old, which a PUID/PGID user can't do. Re-stamp it as root; a no-op when COPY kept mtimes.
RUN python -c "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()"

# Curated libs for headless Firefox; `playwright install-deps` pulls in far more (Xvfb, fonts).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
      tini curl ca-certificates gosu \
      libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 \
      libcairo2 libcairo-gobject2 \
      libdbus-1-3 libdbus-glib-1-2 \
      libfontconfig1 \
      libgdk-pixbuf-2.0-0 \
      libglib2.0-0t64 \
      libgtk-3-0t64 \
      libnspr4 libnss3 \
      libpango-1.0-0 libpangocairo-1.0-0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 \
      libxcomposite1 libxcursor1 libxdamage1 \
      libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 libxss1 libxtst6 \
      libdrm2 libgbm1 \
      libasound2t64 \
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
