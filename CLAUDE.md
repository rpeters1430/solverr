# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Solverr is an ultra-fast, lightweight, next-generation replacement for FlareSolverr and TRAWL: it solves Cloudflare challenges (Turnstile, 5s Interstitial), Google reCAPTCHA v2, hCaptcha, GeeTest, and Imperva, and proxies HTTP requests for tools like Prowlarr/Jackett/Sonarr/Radarr. It exposes the FlareSolverr v1 & v2 JSON API (`POST /v1`, `POST /v2`), a native high-performance `POST /scrape` endpoint, a Prometheus `GET /metrics` exporter, and a rich interactive web dashboard.

## Running & developing

Running tests:
```
python -m unittest discover -s tests
```

Local run:
```
pip install -r requirements.txt
playwright install chromium
python -m app.main
```

Docker:
```
docker compose up --build
```

Quick manual checks:
- `GET /health` — liveness check & worker readiness
- `GET /metrics` — Prometheus exposition format metrics
- `GET /` — dashboard UI (`app/templates/index.html` + `app/static/`)
- `POST /v1` or `POST /v2` — FlareSolverr-compatible API
- `POST /scrape` — native advanced scraping API with tier overrides, selector waiting, extraction rules, and screenshots
- `GET/POST /proxy` — transparent HTTP proxy mode
- `GET/POST /api/*` — dashboard endpoints: `/api/stats`, `/api/cookies`, `/api/cookies/clear`, `/api/sessions`, `/api/test`

## Architecture: 4.5-Tier Adaptive Solving Pipeline

1. **Tier 1 — Fast TLS path** (`app/solver/fast_tls.py`): Uses `curl_cffi` with Chrome/Firefox JA3/TLS impersonation to make direct requests in 30ms–150ms without launching a browser. Rotates between matched TLS-target/User-Agent profiles per-domain (`FAST_TLS_ROTATE`) — target and UA must always be from the same profile pair, since a mismatched pair (e.g. Firefox JA3 with a Chrome UA header) is itself a bot signal.
2. **Tier 2 — Clearance Cookie Cache** (`app/solver/cache.py`): Reuses valid `cf_clearance` tokens and domain cookie jars. Supports zero-dependency local disk JSON storage (writes are debounced/throttled off the request hot path) or distributed Redis via `REDIS_URL`.
3. **Tier 3 — Multi-WAF Stealth Browser** (`app/solver/browser.py` + `app/solver/stealth.py` + `app/solver/human_cursor.py`): Camoufox (Stealth Firefox) & Chromium pool with cubic Bézier human mouse curves to defeat Turnstile, reCAPTCHA v2, hCaptcha, GeeTest, and Imperva. Camoufox instances run through `CamoufoxPool` — a bounded pool of warm, no-proxy browser processes reused across solves (a fresh context per request, not a fresh process) and recycled after `CAMOUFOX_POOL_RECYCLE_USES`/`_SECONDS`. A request carrying its own proxy or an explicit `userAgent` bypasses the pool and gets an ephemeral instance instead, since Camoufox ties its fingerprint/geo derivation to the proxy at launch time.
3.5. **Tier 3.5 — Paid Captcha-Solver Escalation** (`app/solver/captcha_solver.py`): Optional, off unless `CAPTCHA_SOLVER_API_KEY` is set. Only triggered after Tier 3's free click-based loop has exhausted its full timeout without clearing — extracts the widget's `data-sitekey`/`data-callback`, submits to a 2Captcha-protocol-compatible service, and injects the solved token back into the page. Covers interactive image challenges (hCaptcha puzzle grids, reCAPTCHA image selection) a checkbox click alone can't solve.
4. **Tier 4 — Fallback Proxy Escalation** (`app/solver/engine.py`): Escalates to residential or fallback proxies (`FALLBACK_PROXY_URL`) if direct solves fail.

In-flight request deduplication is handled in `app/solver/engine.py` via `HybridSolverEngine._inflight` futures. Sessions (`app/solver/sessions.py`) follow the same dual-mode pattern as the cookie cache — in-memory by default, Redis-backed (surviving restarts, shared across replicas) when `REDIS_URL` is set. See the README's "Horizontal Scaling" section for running multiple replicas behind a shared Redis.
