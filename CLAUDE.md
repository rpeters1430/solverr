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
- `GET /health` — combined liveness+readiness check (503 only when the Camoufox import itself failed)
- `GET /health/live` — liveness only, never checks Camoufox/pool state (safe for an orchestrator's restart policy)
- `GET /health/ready` — readiness (same check as `/health`; split out so a load balancer can pull a degraded-but-alive instance without restarting it)
- `GET /metrics` — Prometheus exposition format metrics (open by default; gate behind `X-Api-Key` with `METRICS_REQUIRE_AUTH=true`)
- `GET /` — dashboard UI (`app/templates/index.html` + `app/static/`)
- `POST /v1` or `POST /v2` — FlareSolverr-compatible API
- `POST /scrape` — native advanced scraping API with tier overrides, selector waiting, extraction rules, and screenshots
- `GET/POST /proxy` — transparent HTTP proxy mode
- `GET/POST /api/*` — dashboard endpoints: `/api/stats`, `/api/cookies`, `/api/cookies/clear`, `/api/sessions`, `/api/test`, `/api/diagnostics/browser` (launches a real ephemeral Camoufox end-to-end - context, page, JS execution - unlike `/health` which only checks the import)

## Architecture: 4.5-Tier Adaptive Solving Pipeline

Camoufox is the only browser engine — there is no Chromium fallback (removed in v1.5; see below).

1. **Tier 1 — Fast TLS path** (`app/solver/fast_tls.py`): Uses `curl_cffi` with Firefox JA3/TLS impersonation to make direct requests in 30ms–150ms without launching a browser. Rotates between matched TLS-target/User-Agent profiles per-domain (`FAST_TLS_ROTATE`) — target and UA must always be from the same profile pair, since a mismatched pair is itself a bot signal.
2. **Tier 2 — Clearance Cookie Cache** (`app/solver/cache.py`): Reuses valid `cf_clearance` tokens and domain cookie jars. Supports zero-dependency local disk JSON storage (writes are debounced/throttled off the request hot path) or distributed Redis via `REDIS_URL`.
3. **Tier 3 — Multi-WAF Stealth Browser** (`app/solver/browser.py` + `app/solver/human_cursor.py`): Camoufox (Stealth Firefox) with cubic Bézier human mouse curves to defeat Turnstile, reCAPTCHA v2, hCaptcha, GeeTest, and Imperva. `BrowserPool.solve()` tries a warm pooled instance first (`CamoufoxPool` — a bounded pool of warm, no-proxy browser processes reused across solves, a fresh context per request rather than a fresh process, recycled after `CAMOUFOX_POOL_RECYCLE_USES`/`_SECONDS`), then always retries once on a fresh ephemeral Camoufox instance (new randomly generated fingerprint) before giving up — either as the pooled path's retry, or as the only attempt for requests carrying their own proxy or an explicit `userAgent` (which bypass the pool entirely, since Camoufox ties fingerprint/geo derivation to the proxy at launch time).
3.5. **Tier 3.5 — Paid Captcha-Solver Escalation** (`app/solver/captcha_solver.py`): Optional, off unless `CAPTCHA_SOLVER_API_KEY` is set. Only triggered after Tier 3's free click-based loop has exhausted its full timeout without clearing — extracts the widget's `data-sitekey`/`data-callback`, submits to a 2Captcha-protocol-compatible service, and injects the solved token back into the page. Covers interactive image challenges (hCaptcha puzzle grids, reCAPTCHA image selection) a checkbox click alone can't solve.
4. **Tier 4 — Fallback Proxy Escalation** (`app/solver/engine.py`): Escalates to residential or fallback proxies (`FALLBACK_PROXY_URL`) if direct solves fail — this re-enters `BrowserPool.solve()` with the fallback proxy set, so it's itself a fresh-Camoufox-with-alternate-proxy attempt, not a distinct engine.

In-flight request deduplication is handled in `app/solver/engine.py` via `HybridSolverEngine._inflight` futures, keyed by a SHA-256 fingerprint of every field that can change the outcome (method, url, postData, cookies, headers, proxy, session, userAgent, forceBrowser, fastTlsOnly, wait_selector, wait_delay_ms) — not just `method:url:forceBrowser`, so two concurrent requests that only differ in body/session/proxy never get coalesced into one answer. Sessions (`app/solver/sessions.py`) follow the same dual-mode pattern as the cookie cache — in-memory by default, Redis-backed (surviving restarts, shared across replicas) when `REDIS_URL` is set. See the README's "Horizontal Scaling" section for running multiple replicas behind a shared Redis.

Both the cookie cache (`app/solver/cache.py`) and sessions (`app/solver/sessions.py`) key cookie identity by `domain+path+name`, not name alone, so two same-name cookies on different paths of the same domain don't overwrite each other. The local (non-Redis) cookie cache and session store are bounded — `MAX_CACHE_DOMAINS`/`MAX_COOKIES_PER_DOMAIN`/`MAX_SESSIONS` evict the oldest entry once the cap is hit; Redis-backed storage relies on its own TTL expiry instead. Redis reads use `SCAN` (`scan_iter`), never `KEYS`, since `KEYS` blocks the single-threaded Redis server for the whole keyspace walk.

`SolutionModel.tier` (one of `tier1_fast_tls`/`tier2_cache`/`tier3_stealth_browser`/`tier4_fallback_proxy`) is set by `engine.py` at each of its four return points and is the source of truth for which tier actually handled a request — `/scrape`'s `tier_used` field reads it directly rather than guessing from duration/cookie presence.

**v1.5 removed Chromium entirely** (previously a Tier 3/4 fallback engine). Chromium's crashpad crash-reporter handler crashes outright when the process runs as a non-root `PUID`/`PGID` user — reproduced even under `--privileged` with every capability added, so it wasn't fixable via permissions. Camoufox has no such issue. `app/solver/stealth.py` (a Chrome-specific `navigator.webdriver`/`window.chrome` JS spoofing script only ever injected into the old Chromium context) was deleted along with it — repurposing it for a Firefox/Camoufox context would be counterproductive, since a Firefox page reporting a `window.chrome` object is itself a tell.

## Module layout

- `app/main.py` — FastAPI app, lifespan (periodic session cleanup; the Camoufox pool itself warms up lazily on first solve, not at startup), request-logging/auth middleware, `/health` and `/metrics`.
- `app/api/flaresolverr.py` — `/v1`, `/v2`, `/scrape`, `/proxy` route handlers.
- `app/api/dashboard.py` — `/api/*` dashboard endpoints (stats, cookies, sessions, test).
- `app/models/flaresolverr.py` — Pydantic request/response models shared by the API routes.
- `app/config.py` — single `Settings` object (`app.config.settings`) reading all env vars, including `MAX_BROWSER_WORKERS=auto` CPU/RAM-based auto-tuning; treat it as the source of truth for available configuration rather than the README's env var tables. `TOTAL_CPU_CORES`/`TOTAL_RAM_GB` read the container's cgroup v2 (`/sys/fs/cgroup/cpu.max`, `memory.max`) or v1 limits when present, falling back to host-level `os.cpu_count()`/`psutil` otherwise — `psutil.virtual_memory()` alone reads `/proc/meminfo`, which isn't namespaced and overstates what a `docker run --memory=`-limited container can actually use.
- `app/metrics.py` / `app/logging_config.py` — Prometheus exposition formatting (counters, gauges, and a per-tier request-duration histogram sourced from `app.solver.engine.metrics` and `app.solver.browser.browser_pool.pool_stats()`) and structured logging setup (request-id correlation via `set_request_id`).
- `app/security.py` — `check_target_url()`, called at the top of `HybridSolverEngine.process_request` (so it covers `/v1`, `/v2`, `/scrape`, and `/proxy` uniformly). Blocks loopback/RFC1918/link-local/cloud-metadata targets by default (`ALLOW_PRIVATE_NETWORKS=false`), with `ALLOWED_HOSTS`/`DENIED_HOSTS` overrides. Only the initial request target is checked — it does not re-validate redirects, since that would require hooking into `curl_cffi` (fast_tls.py), Camoufox navigation (browser.py), and the Tier 4 proxy path separately.

Optional `X-Api-Key` auth (`API_KEY` env var) is enforced in `app/main.py`'s middleware for every route except `/health`, `/metrics`, and `/static/*`.

## Docker image & deployment

`Dockerfile` is a two-stage build on `python:3.14-slim-bookworm`, `linux/amd64` only (matches the CI publish target and the UGREEN/NAS deployment targets called out in the README):

1. **`deps` stage** — creates a venv, `uv pip install -r requirements.txt` (uv, not pip, for faster/reproducible dependency resolution — see the Dockerfile's `deps` stage comments), then pre-fetches the Camoufox browser engine (`python -m camoufox fetch`) so it's baked into the image rather than downloaded at container start. Trims it afterward: deletes the `fonts/macos` and `fonts/windows` sets Camoufox also downloads by default (safe only because `app/solver/browser.py` pins `os="linux"` on every launch — Camoufox otherwise randomizes fingerprint OS per launch and needs all three font sets to render faithfully for whichever it picks), and `strip --strip-unneeded`s the Firefox binary/shared libs. Together with a curated (not `playwright install-deps firefox`) apt list in the runtime stage, this took the image from 3.23GB to 1.42GB.
2. **`runtime` stage** — copies the venv and pre-fetched Camoufox browser from `deps`, installs `tini`, `curl`, `ca-certificates`, `gosu`, and a curated list of the specific X11/GTK/NSS shared libs headless Camoufox/Firefox actually needs (not `playwright install-deps firefox`, which pulls in a much larger transitive closure — Xvfb, X11 utilities, extra fonts — built to support any Playwright browser), then copies in `app/` and `docker-entrypoint.sh`.

Container startup: `tini` is PID 1 (`ENTRYPOINT`), invoking `docker-entrypoint.sh`, which then `exec`s `python -m app.main` (the `CMD`). The container runs as **root by default** — set `PUID`/`PGID` together (both required, both must be numeric) to have the entrypoint `chown` `/app/data` and `/app/.cache` to that UID/GID and re-exec the process under it via `gosu`. Setting only one of the pair fails the container at startup by design. The entrypoint also registers a matching `/etc/passwd`/`/etc/group` entry for an unrecognized PUID/PGID before dropping privileges, since `gosu` derives `$HOME` from the target UID's passwd entry (ignoring any exported `HOME`) — an unregistered UID would otherwise resolve to `HOME=/`, which is read-only for a non-root user and breaks Camoufox's config/cache directories.

**Known issue, confirmed 2026-08-23 against a real build, investigated further 2026-08-24 (GitHub issue #17):** under `PUID`/`PGID` (non-root), a Camoufox launch hangs **every single time** (100% reproduction, not intermittent) instead of erroring — reproduced with `docker run -e PUID=1000 -e PGID=1000` alone against the published `ghcr.io/rpeters1430/solverr:latest` image, no extra hardening flags needed. Root execution (the default) reliably launches in ~3s.
  - Ruled out via direct testing: capabilities (`--cap-add=SYS_ADMIN --cap-add=SYS_PTRACE` — no effect), seccomp/AppArmor (`--security-opt seccomp=unconfined --security-opt apparmor=unconfined` — no effect), the content-process sandbox (`security.sandbox.*.level=0` Firefox prefs — no effect), and the compositor/WebRender path (`gfx.webrender.*`/`layers.acceleration.*` prefs disabled — no effect).
  - `DEBUG=pw:browser` tracing shows the Firefox parent process launches and logs identically for both root and non-root up through `RenderCompositorSWGL failed mapping default framebuffer, no dt` (itself benign — root hits the same line and proceeds). For root, the very next line is `Juggler listening to the pipe` ~140ms later and the solve completes. For non-root, no further output is ever produced — the process just sits `S` (sleeping), so Playwright's connection handshake to the juggler pipe never completes and the tier's `asyncio.wait_for` eventually times out. `Sandbox: CanCreateUserNamespace() clone() failure: EPERM` appears in both logs (confirmed benign, present on the successful root path too) — it is not the cause, despite looking like an obvious lead.
  - Net: the actual point of divergence is somewhere between the compositor init log line and the juggler pipe becoming ready, specific to running as a non-root UID, and not attributable to sandboxing, seccomp, or graphics acceleration settings tried so far. Still not fully root-caused.
  - Mitigations shipped in response: `CamoufoxPool._launch_instance()` now wraps `cm.__aenter__()` in its own `CAMOUFOX_LAUNCH_TIMEOUT_SECONDS` (30s) timeout so one hung launch no longer holds `CamoufoxPool._lock` for the full outer per-solve timeout (up to `BROWSER_TIMEOUT_MS + SOLVE_WALLCLOCK_GRACE_SECONDS`), which was otherwise serializing every other pooled `acquire()` behind it; and bare `TimeoutError`s from a hung tier (previously surfaced to API callers as an empty `"Error solving request: "` with zero information — `str(TimeoutError())` is `""`) now carry a descriptive message identifying which tier and timeout fired, via `browser.py`'s `_describe_solve_error()`.
  - **Practical recommendation until this is root-caused: don't use `PUID`/`PGID` on a deployment that needs Tier 3 browser solving to work — run as the default root user instead.** (Chown `./data` externally if you need it owned by a specific host user for other reasons; Tier 3 will not function at all under PUID/PGID as currently understood.) `BrowserPool.self_test()` (used by `GET /api/diagnostics/browser`) is the fastest way to check whether a given deployment is affected — it returns `503` with `"Browser self-test timed out..."` within ~45s if so.

`HEALTHCHECK` and the compose `healthcheck:` block both poll `curl -f http://localhost:8191/health`; `/health` in `app/main.py` reports `503` only when the Camoufox import itself failed (`CAMOUFOX_AVAILABLE` is False) — there's no other engine left to service Tier 3 in that case. An unused/lazy pool (nothing solved yet) is healthy.

Build and publish is automated by `.github/workflows/docker-publish.yml`: runs `python -m unittest discover -s tests` first, then (on push to `main`, version tags, or manual dispatch — not on pull requests) builds and pushes `linux/amd64` to `ghcr.io/rpeters1430/solverr` tagged `latest` (default branch only), the branch/ref name, semver, and short SHA. To reproduce that image locally: `docker build --platform linux/amd64 -t solverr .`

Horizontal scaling (multiple replicas behind a shared cache) needs `REDIS_URL` set on every replica and the compose `distributed` profile: `docker compose --profile distributed up -d --scale solverr=3`. Without a shared `REDIS_URL`, each replica's cookie cache and sessions are local to that process — see README "Horizontal Scaling" for the full rationale.
