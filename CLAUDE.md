# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Solverr is an ultra-fast, lightweight, next-generation replacement for FlareSolverr and TRAWL: it solves Cloudflare challenges (Turnstile, 5s Interstitial), Google reCAPTCHA v2, hCaptcha, GeeTest, and Imperva, and proxies HTTP requests for tools like Prowlarr/Jackett/Sonarr/Radarr. It exposes the FlareSolverr v1 & v2 JSON API (`POST /v1`, `POST /v2`), a native high-performance `POST /scrape` endpoint, a Prometheus `GET /metrics` exporter, and a rich interactive web dashboard.

## Running & developing

Running tests:
```
python -m unittest discover -s tests
```

Running a single test file or case:
```
python -m unittest tests.test_engine
python -m unittest tests.test_engine.EngineTests.test_specific_case
```

Local run:
```
pip install -r requirements.txt
playwright install-deps firefox
python -m camoufox fetch
python -m app.main
```

Docker (build + run locally from source):
```
docker compose up --build
```
Runs the `solverr` service only; the `redis` service is behind the `distributed` compose profile and does not start unless requested (see Horizontal Scaling below).

Docker (pull the published image, no build):
```
docker compose pull
docker compose up -d
```
`docker-compose.yml` sets `image: ghcr.io/rpeters1430/solverr:latest` alongside `build:`, so `docker compose up` without `--build` uses the local image if present or pulls `latest` from GHCR otherwise.

Quick manual checks:
- `GET /health` — liveness check & worker readiness
- `GET /metrics` — Prometheus exposition format metrics
- `GET /` — dashboard UI (`app/templates/index.html` + `app/static/`)
- `POST /v1` or `POST /v2` — FlareSolverr-compatible API
- `POST /scrape` — native advanced scraping API with tier overrides, selector waiting, extraction rules, and screenshots
- `GET/POST /proxy` — transparent HTTP proxy mode
- `GET/POST /api/*` — dashboard endpoints: `/api/stats`, `/api/cookies`, `/api/cookies/clear`, `/api/sessions`, `/api/test`

## Architecture: 4.5-Tier Adaptive Solving Pipeline

Camoufox is the only browser engine — there is no Chromium fallback (removed in v1.5; see below).

1. **Tier 1 — Fast TLS path** (`app/solver/fast_tls.py`): Uses `curl_cffi` with Firefox JA3/TLS impersonation to make direct requests in 30ms–150ms without launching a browser. Rotates between matched TLS-target/User-Agent profiles per-domain (`FAST_TLS_ROTATE`) — target and UA must always be from the same profile pair, since a mismatched pair is itself a bot signal.
2. **Tier 2 — Clearance Cookie Cache** (`app/solver/cache.py`): Reuses valid `cf_clearance` tokens and domain cookie jars. Supports zero-dependency local disk JSON storage (writes are debounced/throttled off the request hot path) or distributed Redis via `REDIS_URL`.
3. **Tier 3 — Multi-WAF Stealth Browser** (`app/solver/browser.py` + `app/solver/human_cursor.py`): Camoufox (Stealth Firefox) with cubic Bézier human mouse curves to defeat Turnstile, reCAPTCHA v2, hCaptcha, GeeTest, and Imperva. `BrowserPool.solve()` tries a warm pooled instance first (`CamoufoxPool` — a bounded pool of warm, no-proxy browser processes reused across solves, a fresh context per request rather than a fresh process, recycled after `CAMOUFOX_POOL_RECYCLE_USES`/`_SECONDS`), then always retries once on a fresh ephemeral Camoufox instance (new randomly generated fingerprint) before giving up — either as the pooled path's retry, or as the only attempt for requests carrying their own proxy or an explicit `userAgent` (which bypass the pool entirely, since Camoufox ties fingerprint/geo derivation to the proxy at launch time).
3.5. **Tier 3.5 — Paid Captcha-Solver Escalation** (`app/solver/captcha_solver.py`): Optional, off unless `CAPTCHA_SOLVER_API_KEY` is set. Only triggered after Tier 3's free click-based loop has exhausted its full timeout without clearing — extracts the widget's `data-sitekey`/`data-callback`, submits to a 2Captcha-protocol-compatible service, and injects the solved token back into the page. Covers interactive image challenges (hCaptcha puzzle grids, reCAPTCHA image selection) a checkbox click alone can't solve.
4. **Tier 4 — Fallback Proxy Escalation** (`app/solver/engine.py`): Escalates to residential or fallback proxies (`FALLBACK_PROXY_URL`) if direct solves fail — this re-enters `BrowserPool.solve()` with the fallback proxy set, so it's itself a fresh-Camoufox-with-alternate-proxy attempt, not a distinct engine.

In-flight request deduplication is handled in `app/solver/engine.py` via `HybridSolverEngine._inflight` futures. Sessions (`app/solver/sessions.py`) follow the same dual-mode pattern as the cookie cache — in-memory by default, Redis-backed (surviving restarts, shared across replicas) when `REDIS_URL` is set. See the README's "Horizontal Scaling" section for running multiple replicas behind a shared Redis.

**v1.5 removed Chromium entirely** (previously a Tier 3/4 fallback engine). Chromium's crashpad crash-reporter handler crashes outright when the process runs as a non-root `PUID`/`PGID` user — reproduced even under `--privileged` with every capability added, so it wasn't fixable via permissions. Camoufox has no such issue. `app/solver/stealth.py` (a Chrome-specific `navigator.webdriver`/`window.chrome` JS spoofing script only ever injected into the old Chromium context) was deleted along with it — repurposing it for a Firefox/Camoufox context would be counterproductive, since a Firefox page reporting a `window.chrome` object is itself a tell.

## Module layout

- `app/main.py` — FastAPI app, lifespan (periodic session cleanup; the Camoufox pool itself warms up lazily on first solve, not at startup), request-logging/auth middleware, `/health` and `/metrics`.
- `app/api/flaresolverr.py` — `/v1`, `/v2`, `/scrape`, `/proxy` route handlers.
- `app/api/dashboard.py` — `/api/*` dashboard endpoints (stats, cookies, sessions, test).
- `app/models/flaresolverr.py` — Pydantic request/response models shared by the API routes.
- `app/config.py` — single `Settings` object (`app.config.settings`) reading all env vars, including `MAX_BROWSER_WORKERS=auto` CPU/RAM-based auto-tuning; treat it as the source of truth for available configuration rather than the README's env var tables.
- `app/metrics.py` / `app/logging_config.py` — Prometheus exposition formatting and structured logging setup (request-id correlation via `set_request_id`).

Optional `X-Api-Key` auth (`API_KEY` env var) is enforced in `app/main.py`'s middleware for every route except `/health`, `/metrics`, and `/static/*`.

## Docker image & deployment

`Dockerfile` is a two-stage build on `python:3.14-slim-bookworm`, `linux/amd64` only (matches the CI publish target and the UGREEN/NAS deployment targets called out in the README):

1. **`deps` stage** — creates a venv, `pip install -r requirements.txt`, then pre-fetches the Camoufox browser engine (`python -m camoufox fetch`) so it's baked into the image rather than downloaded at container start.
2. **`runtime` stage** — copies the venv and pre-fetched Camoufox browser from `deps`, installs `tini`, `curl`, `ca-certificates`, `gosu`, and Playwright's OS-level Firefox deps (`playwright install-deps firefox` — the shared libs Camoufox's Firefox binary also needs), then copies in `app/` and `docker-entrypoint.sh`.

Container startup: `tini` is PID 1 (`ENTRYPOINT`), invoking `docker-entrypoint.sh`, which then `exec`s `python -m app.main` (the `CMD`). The container runs as **root by default** — set `PUID`/`PGID` together (both required, both must be numeric) to have the entrypoint `chown` `/app/data` and `/app/.cache` to that UID/GID and re-exec the process under it via `gosu`. Setting only one of the pair fails the container at startup by design. The entrypoint also registers a matching `/etc/passwd`/`/etc/group` entry for an unrecognized PUID/PGID before dropping privileges, since `gosu` derives `$HOME` from the target UID's passwd entry (ignoring any exported `HOME`) — an unregistered UID would otherwise resolve to `HOME=/`, which is read-only for a non-root user and breaks Camoufox's config/cache directories.

`HEALTHCHECK` and the compose `healthcheck:` block both poll `curl -f http://localhost:8191/health`; `/health` in `app/main.py` reports `503` only when the Camoufox import itself failed (`CAMOUFOX_AVAILABLE` is False) — there's no other engine left to service Tier 3 in that case. An unused/lazy pool (nothing solved yet) is healthy.

Build and publish is automated by `.github/workflows/docker-publish.yml`: runs `python -m unittest discover -s tests` first, then (on push to `main`, version tags, or manual dispatch — not on pull requests) builds and pushes `linux/amd64` to `ghcr.io/rpeters1430/solverr` tagged `latest` (default branch only), the branch/ref name, semver, and short SHA. To reproduce that image locally: `docker build --platform linux/amd64 -t solverr .`

Horizontal scaling (multiple replicas behind a shared cache) needs `REDIS_URL` set on every replica and the compose `distributed` profile: `docker compose --profile distributed up -d --scale solverr=3`. Without a shared `REDIS_URL`, each replica's cookie cache and sessions are local to that process — see README "Horizontal Scaling" for the full rationale.
