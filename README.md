# ⚡ Solverr

[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/rpeters1430/solverr?utm_source=badge)

**Solverr** is an ultra-fast, lightweight, next-generation replacement for **FlareSolverr**, **TRAWL**, and **Byparr**. Designed specifically for high-efficiency deployments on home servers, NAS hardware (**UGREEN NASync DXP4800 Pro / UGOS Pro**, Synology, TrueNAS, Unraid), and unthrottled desktop/Docker environments.

---

## 🔥 Key Features

- **⚡ 4.5-Tier Adaptive Solver Pipeline**:
  - **Tier 1 (Fast TLS)**: Level 1 & 2 JA3 / TLS Impersonation (`curl_cffi`) with **Adaptive Domain Scoring** solves requests in **30ms – 120ms** without browser overhead.
  - **Tier 2 (Clearance Cache)**: Instant `cf_clearance` & domain cookie jar reuse (**< 50ms**) with granular per-cookie TTL expiration and Netscape export.
  - **Tier 3 (Stealth Browser)**: Warm Camoufox (Stealth Firefox) pool with per-page isolated Bézier human mouse curves and **Deep Shadow DOM traversal**, escalating to a fresh Camoufox instance (new fingerprint) if the pooled attempt fails.
  - **Tier 3.5 (Paid Captcha Escalation)**: Optional 2Captcha-protocol escalation for interactive image puzzles (`CAPTCHA_SOLVER_API_KEY`).
  - **Tier 4 (Fallback Proxy)**: Automatic residential / fallback proxy escalation for rate-limited indexers.
- **🛡️ Multi-WAF & CAPTCHA Solver Suite**: Automated solving for **Cloudflare Turnstile**, **Cloudflare 5s Interstitial**, **Google reCAPTCHA v2 / Enterprise**, **hCaptcha**, **GeeTest**, **Imperva / Incapsula**, **DataDome**, **Akamai**, and **AWS WAF**.
- **🌐 Deep Shadow DOM & Web Component Traversal**: In-page recursive DOM walker locates Turnstile and CAPTCHA checkboxes nested inside `#shadow-root` nodes across custom web components.
- **📈 Adaptive TLS Profile Learning**: Fast TLS automatically learns which browser TLS fingerprints (`firefox147`, `firefox144`, `firefox133`, `chrome146`, etc.) succeed per domain, penalizing failing fingerprints and picking optimal JA3 profiles.
- **🖱️ Isolated Humanized Bézier Curve Movement**: Emulates organic human mouse trajectories with micro-jitters, variable velocities, and natural pauses — fully isolated per page using weakref cursor tracking for multi-worker concurrency.
- **🍪 Netscape & JSON Cookie Export (`/api/cookies/export`)**: Single-click export of cached cookies in Netscape format (`curl -b cookies.txt`, `yt-dlp`, `wget`) or standard JSON.
- **📡 Real-time Live Event Streaming (SSE)**: Server-Sent Events stream (`/api/events`) broadcasts real-time solve feeds, tier transitions, and telemetry directly to the interactive dashboard.
- **🔌 100% FlareSolverr v1 & v2 Compatibility**: Standard `POST /v1` and `POST /v2` endpoints compatible out-of-the-box with **Prowlarr**, **Jackett**, **Sonarr**, **Radarr**, and **FlexGet**.
- **🚀 Native High-Performance `POST /scrape` API**: Full programmatic control with tier overrides, DOM selector waiting (`wait_selector`), data extraction (`extract_rules`), and debug screenshots.
- **🤖 MCP Server for AI Agents (`/mcp`)**: First-class [Model Context Protocol](https://modelcontextprotocol.io) tools (`solverr_scrape`, `solverr_screenshot`, `solverr_get_cookies`, `solverr_get_stats`) so an agent can drive Solverr directly, gated by the same `X-Api-Key` as the rest of the API. On by default; disable with `ENABLE_MCP=false`.
- **📊 Native Prometheus Metrics (`GET /metrics`)**: Standard Prometheus exposition format for 1-click scraping in Grafana, Prometheus, or VictoriaMetrics.
- **🧠 Dual-Mode Caching**: Zero-dependency local JSON file persistence by default, with automatic **Redis** cluster backend support via `REDIS_URL`.
- **📊 Real-time Web Control Center**: Live interactive challenge test bench with HTML viewer, screenshot preview, live SSE event feed, cookie explorer, and hardware monitors.

---

## 🚀 Comparison: FlareSolverr vs. TRAWL vs. Solverr

| Feature / Metric | Traditional FlareSolverr | TRAWL (`germondai/trawl`) | **Solverr** |
| :--- | :--- | :--- | :--- |
| **Engine** | Full Selenium Chrome | Camoufox | **Hybrid (Fast TLS + Camoufox)** |
| **Response Latency** | 10s – 18s | ~500ms (cached) / 4–12s (solve) | **30ms – 100ms** (Fast) / **~1.8s** (Browser) |
| **RAM Usage** | ~600MB – 1.2GB | ~150MB – 300MB | **~75MB – 140MB** |
| **Challenge Solvers** | Basic Cloudflare | Turnstile, reCAPTCHA, hCaptcha, GeeTest | **Turnstile, reCAPTCHA v2, hCaptcha, GeeTest, Imperva, DataDome, Akamai, AWS WAF** |
| **Caching Backend** | Memory only | Redis required (2 containers) | **Dual-Engine (Zero-dep Local + Optional Redis)** |
| **Prometheus Telemetry** | ❌ None | ⚠️ External exporter | **✅ Built-in Native `/metrics` endpoint** |
| **Cursor Emulation** | Direct click | Linear cursor | **Realistic Cubic Bézier Curves + Jitter** |
| **Web Dashboard** | Plain text | Basic health | **Modern Real-Time Interactive Test Bench** |
| **AI Agent Support (MCP)** | ❌ None | ✅ `read`/`scrape`/`screenshot`/`inspect` tools | **✅ `/mcp` Streamable HTTP server** (`solverr_scrape`/`solverr_screenshot`/`solverr_get_cookies`/`solverr_get_stats`) |

---

## 🟢 Quick Deployment: UGREEN NASync DXP4800 Pro (UGOS Pro)

The included `compose.ugreen.yml` is tuned for the DXP4800 Pro while it is
also running Jellyfin and the Arr stack. It fixes Solverr at two browser
workers, two CPU cores, 3GB RAM, 1GB shared memory, bounded caches, rotated
logs, and your common UGOS ownership values (`PUID=1000`, `PGID=10`).
Camoufox is tested under that exact non-root identity before an image can be
published.

Do not map `/dev/dri` into Solverr. Media requests are blocked during solves,
so the Intel GPU provides little benefit here and is better left available to
Jellyfin transcoding.

### Using UGOS Pro Docker Compose

1. Open **Docker** in UGOS Pro and create a project named `solverr`.
2. Copy `compose.ugreen.yml` into the project, or paste its contents into the
   UGOS Compose editor.
3. Create `/volume1/docker/solverr/data` if it does not already exist.
4. Deploy the project and open `http://YOUR-NAS-IP:8191`.

If the NAS has only 8GB RAM and regularly performs Jellyfin transcodes, reduce
`MAX_BROWSER_WORKERS` to `1` and `mem_limit` to `2g`. Raising the worker
count is rarely useful for a single Prowlarr instance; watch the browser queue
metric before increasing it.

### Command-line equivalent

```bash
docker run -d \
  --name solverr \
  --restart unless-stopped \
  --cpus=2 \
  --memory=3g \
  --pids-limit=512 \
  --shm-size=1g \
  -p 8191:8191 \
  -v /volume1/docker/solverr/data:/app/data \
  -e NAS_MODE=true \
  -e MAX_BROWSER_WORKERS=2 \
  -e ENABLE_MCP=false \
  -e PUID=1000 \
  -e PGID=10 \
  ghcr.io/rpeters1430/solverr:latest
```

---

## 🐳 Generic Docker Deployment

```yaml
services:
  solverr:
    image: ghcr.io/rpeters1430/solverr:latest
    container_name: solverr
    restart: unless-stopped
    ports:
      - "8191:8191"
    shm_size: '2gb'
    environment:
      - PORT=8191
      - HOST=0.0.0.0
      - LOG_LEVEL=INFO
      - MAX_BROWSER_WORKERS=auto
      - ENABLE_FAST_TLS=true
      - COOKIE_CACHE_TTL=7200
      # - PUID=1000 # Optional: run container process as this UID (must be set with PGID)
      # - PGID=1000 # Optional: run container process as this GID (must be set with PUID)
    volumes:
      - ./data:/app/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8191/health', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

---

## 💻 Native Host Execution (Windows / Linux / macOS)

Running Solverr natively directly on your host computer gives **100% native CPU performance**, zero Docker virtualization overhead, and unconstrained access to system RAM.

### Quick Start:

```powershell
# Windows PowerShell launcher:
.\run_native.ps1
```
Or double-click **`run_native.bat`** in Windows Explorer.

---

## 📈 Horizontal Scaling

Every request Solverr handles is a self-contained request/response - there's no job queue or shared in-process state that needs coordinating, so scaling out is just running more replicas behind a load balancer:

1. **Share the cookie cache & sessions.** Point every replica at the same Redis instance (`REDIS_URL=redis://redis:6379/0`) so a `cf_clearance` cookie earned by one replica is immediately reusable by the others, and FlareSolverr `session`s work no matter which replica handles a given request. Without Redis, each replica's cache/sessions are local to that process. The bundled `docker-compose.yml` has a `redis` service behind the `distributed` profile: `docker compose --profile distributed up --scale solverr=3`.
2. **Put a load balancer in front.** Any round-robin proxy works (nginx, Traefik, an internal DNS record, or your orchestrator's own Service object) - requests are stateless from the balancer's point of view once Redis is shared.
3. **Each replica keeps its own local browser pool.** `MAX_BROWSER_WORKERS` and the Camoufox pool are per-process by design (a warm Firefox process can't be shared across containers), so total capacity is `replicas × MAX_BROWSER_WORKERS`. Scale by adding replicas rather than raising one replica's worker count past what its CPU/RAM can actually support.
4. **`/metrics` is per-replica.** Prometheus scrapes each container as its own target and tags it with an `instance` label automatically - aggregate across replicas with a `sum by (...)` query in Grafana rather than expecting one `/metrics` endpoint to report a cluster-wide total.

---

## ⚙️ Prowlarr / Jackett Configuration

1. In **Prowlarr** / **Jackett**, navigate to **Settings** &rarr; **Indexers** (or FlareSolverr setting).
2. Add FlareSolverr proxy:
   - **Tags**: `flaresolverr`
   - **FlareSolverr Host**: `http://localhost:8191` (or `http://YOUR-NAS-IP:8191`)
   - **Max Timeout**: `60000`
3. Click **Test** and **Save**.

---

## 🛠️ API Reference

### 1. FlareSolverr Compatible Endpoint (`POST /v1` & `POST /v2`)
```json
{
  "cmd": "request.get",
  "url": "https://nowsecure.nl",
  "maxTimeout": 60000
}
```

### 2. Native Scrape API (`POST /scrape`)
```json
{
  "url": "https://nowsecure.nl",
  "method": "GET",
  "tier": "auto",
  "wait_selector": "body",
  "screenshot": true,
  "extract_rules": {
    "title": "title",
    "links": "a@href"
  }
}
```

### 3. Prometheus Metrics (`GET /metrics`)
Scrape endpoint for Grafana, Prometheus, or VictoriaMetrics:
```
http://localhost:8191/metrics
```

### 4. Netscape Cookie Export (`GET /api/cookies/export`)
Export cached cookies in Netscape format (`curl -b cookies.txt`, `yt-dlp`) or JSON:
```bash
# Export all cached cookies in Netscape format
curl http://localhost:8191/api/cookies/export?format=netscape -o cookies.txt

# Export cookies for a specific domain
curl http://localhost:8191/api/cookies/export?domain=example.com -o cookies_example.txt
```

### 5. Real-Time Event Stream (`GET /api/events`)
Server-Sent Events (SSE) stream for live solve monitoring:
```bash
curl -N http://localhost:8191/api/events
```

### 6. MCP Server (`POST /mcp`)
Streamable HTTP [MCP](https://modelcontextprotocol.io) endpoint for AI agents - point an MCP-compatible client at `http://localhost:8191/mcp` (an `X-Api-Key` header is required if `API_KEY` is set, exactly like every other endpoint). Exposes:
- `solverr_scrape` - fetch a URL through the tiered solver, with optional `extract_rules`
- `solverr_screenshot` - solve and return a JPEG screenshot of the resulting page
- `solverr_get_cookies` - read cached clearance cookies for a domain without a new request
- `solverr_get_stats` - engine/browser-pool health

On by default; set `ENABLE_MCP=false` to disable.

**Security note:** if `API_KEY` is set, that shared secret gates `/mcp` (like every other endpoint) and no further configuration is needed. If `API_KEY` is **not** set, `/mcp` still enforces a Host/Origin allowlist restricted to `localhost`/`127.0.0.1` by default - this stops a malicious webpage from reaching an unauthenticated MCP server via DNS rebinding. To use MCP from a real (non-localhost) client without an `API_KEY`, set `MCP_ALLOWED_HOSTS` (comma-separated `host:port` or `host:*`, e.g. `my-nas.local:8191`) and `MCP_ALLOWED_ORIGINS` (full origins, e.g. `http://my-nas.local:8191`) - though setting `API_KEY` instead is the safer option.

---

## 📊 Environment Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PORT` | `8191` | Server HTTP port |
| `HOST` | `0.0.0.0` | Server binding IP |
| `PUID` | `None` | Optional runtime UID override for the main process (must be paired with `PGID`) |
| `PGID` | `None` | Optional runtime GID override for the main process (must be paired with `PUID`) |
| `MAX_BROWSER_WORKERS` | `auto` | Max concurrent browser workers and Camoufox pool size. Auto mode respects cgroup CPU/RAM limits and caps NAS mode at two workers; an explicit value is clamped to 1–16. |
| `RAM_PER_WORKER_GB` | `1.0` | RAM budgeted per browser worker when auto-tuning `MAX_BROWSER_WORKERS` |
| `RAM_RESERVED_GB` | `2.0` | RAM reserved for the OS/other containers and excluded from auto-tuning's worker budget |
| `ENABLE_FAST_TLS` | `true` | Enables 50ms TLS impersonation fast path |
| `FAST_TLS_ROTATE` | `true` | Rotate the TLS/UA fingerprint per-domain across a matched profile pool instead of one fixed fingerprint |
| `CAMOUFOX_POOL_ENABLED` | `true` | Reuse warm Camoufox processes across no-proxy solves instead of spawning one per request |
| `CAMOUFOX_POOL_RECYCLE_USES` | `40` | Recycle a pooled browser instance after this many solves |
| `CAMOUFOX_POOL_RECYCLE_SECONDS` | `1800` | Recycle a pooled browser instance after this many seconds, whichever comes first |
| `REDIS_URL` | `None` | Optional Redis URL for distributed cookie cache & sessions - required when running multiple replicas, see [Horizontal Scaling](#-horizontal-scaling) |
| `COOKIE_CACHE_TTL` | `7200` | Clearance cookie cache TTL in seconds |
| `MAX_CACHE_DOMAINS` | `1000` | Local (non-Redis) cookie cache: max distinct domains before the oldest is evicted |
| `MAX_COOKIES_PER_DOMAIN` | `100` | Local (non-Redis) cookie cache: max cookies per domain before the oldest are evicted |
| `MAX_SESSIONS` | `500` | Max in-memory sessions before the oldest (by last access) is evicted |
| `FALLBACK_PROXY_URL` | `None` | Optional Tier 4 fallback proxy URL |
| `API_KEY` | `None` | When set, requires a matching `X-Api-Key` header (header only - never a query param) on every endpoint except `/health`, and `/metrics` unless `METRICS_REQUIRE_AUTH=true` |
| `METRICS_REQUIRE_AUTH` | `false` | Require `X-Api-Key` on `/metrics` too, instead of leaving it open for Prometheus scrapers |
| `ALLOW_PRIVATE_NETWORKS` | `false` | Allow initial targets, redirects, and browser subresources that resolve to loopback/RFC1918/link-local/cloud-metadata addresses. |
| `ALLOWED_HOSTS` | (empty) | Comma-separated hostnames exempted from the private-network block above |
| `DENIED_HOSTS` | (empty) | Comma-separated hostnames always rejected, regardless of `ALLOW_PRIVATE_NETWORKS` |
| `MAX_REQUEST_BODY_MB` | `10` | Reject incoming requests whose `Content-Length` exceeds this (`0` disables) |
| `MAX_RESPONSE_BODY_MB` | `50` | Truncate an oversized solved response body before returning it |
| `MAX_SCREENSHOT_MB` | `8` | Drop a captured screenshot instead of returning it if it exceeds this size |
| `CAPTCHA_SOLVER_API_KEY` | `None` | Optional 2Captcha-compatible API key for the Tier 3.5 paid-solver escalation on interactive image challenges |
| `CAPTCHA_SOLVER_BASE_URL` | `https://2captcha.com` | API base URL - point at another provider's 2captcha-compatible endpoint (e.g. CapSolver) here |
| `ENABLE_MCP` | `true` | Mount the MCP (Model Context Protocol) server at `/mcp` for AI agents - see [MCP Server](#6-mcp-server-post-mcp) |
| `MCP_ALLOWED_HOSTS` | (empty) | Comma-separated Host header values (`host:port` or `host:*`) `/mcp` accepts beyond `localhost`/`127.0.0.1`. Only consulted when `API_KEY` is unset |
| `MCP_ALLOWED_ORIGINS` | (empty) | Comma-separated Origin header values `/mcp` accepts beyond `localhost`/`127.0.0.1`. Only consulted when `API_KEY` is unset |
| `HEADLESS` | `true` | Run browser in headless mode |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## 🏎️ Performance Benchmarks

Solverr's CPU-bound hot paths are continuously benchmarked with [CodSpeed](https://app.codspeed.io/rpeters1430/solverr) on every push and pull request, so a change that makes challenge detection, the cookie cache, or request/response handling slower shows up as a regression in the PR instead of as latency on your NAS.

```bash
pip install -r requirements.txt -r requirements-bench.txt
python -m pytest benchmarks/ --codspeed
```

The suite lives in `benchmarks/` and covers multi-WAF challenge detection, the Tier 2 cookie cache and session store, Pydantic request/response handling, `/scrape` HTML extraction, Tier 1 TLS-profile selection, the SSRF guard, Prometheus metrics rendering, and Bézier cursor generation. It runs separately from the unit tests (`python -m unittest discover -s tests`).

---

## 📜 License

MIT License
