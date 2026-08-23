# ⚡ Solverr

**Solverr** is an ultra-fast, lightweight, next-generation replacement for **FlareSolverr**, **TRAWL**, and **Byparr**. Designed specifically for high-efficiency deployments on home servers, NAS hardware (**UGREEN NASync DXP4800 Pro / UGOS Pro**, Synology, TrueNAS, Unraid), and unthrottled desktop/Docker environments.

---

## 🔥 Key Features

- **⚡ 4.5-Tier Adaptive Solver Pipeline**:
  - **Tier 1 (Fast TLS)**: Level 1 & 2 JA3 / TLS Impersonation (`curl_cffi`) with **Adaptive Domain Scoring** solves requests in **30ms – 120ms** without browser overhead.
  - **Tier 2 (Clearance Cache)**: Instant `cf_clearance` & domain cookie jar reuse (**< 50ms**) with granular per-cookie TTL expiration and Netscape export.
  - **Tier 3 (Stealth Browser)**: Warm Camoufox (Stealth Firefox) pool with per-page isolated Bézier human mouse curves and **Deep Shadow DOM traversal**, escalating to a fresh Camoufox instance (new fingerprint) if the pooled attempt fails.
  - **Tier 3.5 (Paid Captcha Escalation)**: Optional 2Captcha-protocol escalation for interactive image puzzles (`CAPTCHA_SOLVER_API_KEY`).
  - **Tier 4 (Fallback Proxy)**: Automatic residential / fallback proxy escalation for rate-limited indexers.
- **🛡️ Multi-WAF & CAPTCHA Solver Suite**: Automated solving for **Cloudflare Turnstile**, **Cloudflare 5s Interstitial**, **Google reCAPTCHA v2 / Enterprise**, **hCaptcha**, **GeeTest**, **Imperva / Incapsula**, **DataDome**, and **Akamai**.
- **🌐 Deep Shadow DOM & Web Component Traversal**: In-page recursive DOM walker locates Turnstile and CAPTCHA checkboxes nested inside `#shadow-root` nodes across custom web components.
- **📈 Adaptive TLS Profile Learning**: Fast TLS automatically learns which browser TLS fingerprints (`firefox147`, `firefox144`, `firefox133`, `chrome146`, etc.) succeed per domain, penalizing failing fingerprints and picking optimal JA3 profiles.
- **🖱️ Isolated Humanized Bézier Curve Movement**: Emulates organic human mouse trajectories with micro-jitters, variable velocities, and natural pauses — fully isolated per page using weakref cursor tracking for multi-worker concurrency.
- **🍪 Netscape & JSON Cookie Export (`/api/cookies/export`)**: Single-click export of cached cookies in Netscape format (`curl -b cookies.txt`, `yt-dlp`, `wget`) or standard JSON.
- **📡 Real-time Live Event Streaming (SSE)**: Server-Sent Events stream (`/api/events`) broadcasts real-time solve feeds, tier transitions, and telemetry directly to the interactive dashboard.
- **🔌 100% FlareSolverr v1 & v2 Compatibility**: Standard `POST /v1` and `POST /v2` endpoints compatible out-of-the-box with **Prowlarr**, **Jackett**, **Sonarr**, **Radarr**, and **FlexGet**.
- **🚀 Native High-Performance `POST /scrape` API**: Full programmatic control with tier overrides, DOM selector waiting (`wait_selector`), data extraction (`extract_rules`), and debug screenshots.
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
| **Challenge Solvers** | Basic Cloudflare | Turnstile, reCAPTCHA, hCaptcha, GeeTest | **Turnstile, reCAPTCHA v2, hCaptcha, GeeTest, Imperva, Akamai** |
| **Caching Backend** | Memory only | Redis required (2 containers) | **Dual-Engine (Zero-dep Local + Optional Redis)** |
| **Prometheus Telemetry** | ❌ None | ⚠️ External exporter | **✅ Built-in Native `/metrics` endpoint** |
| **Cursor Emulation** | Direct click | Linear cursor | **Realistic Cubic Bézier Curves + Jitter** |
| **Web Dashboard** | Plain text | Basic health | **Modern Real-Time Interactive Test Bench** |

---

## 🟢 Quick Deployment: UGREEN NASync DXP4800 Pro (UGOS Pro)

The **UGREEN NASync DXP4800 Pro** ships with an Intel Core i3-1315U - 6 cores (2P+4E) / 8 threads, up to 4.5GHz, integrated UHD Graphics (no Iris Xe) - and 8GB DDR5-5600 stock, expandable to 96GB across 2 SODIMM slots. `MAX_BROWSER_WORKERS=auto` sizes off both CPU thread count *and* available RAM (see [Environment Configuration](#-environment-configuration)), so on the 8GB stock config it lands well under 8 concurrent browser workers by default rather than assuming a 10-12 thread machine. If you're running Sonarr/Radarr/Prowlarr on the same box, budget worker count against the RAM they need too - set `MAX_BROWSER_WORKERS` explicitly rather than relying on `auto` once other containers are sharing the NAS.

### Method 1: Using UGOS Pro Docker Compose (Recommended)

1. Open **Docker** in the UGOS Pro desktop.
2. Go to **Projects** &rarr; **Create Project**.
3. Name the project `solverr` and paste the compose configuration below:

```yaml
services:
  solverr:
    image: ghcr.io/rpeters1430/solverr:latest
    container_name: solverr
    restart: unless-stopped
    ports:
      - "8191:8191"
    shm_size: '2gb' # Recommended 1gb-2gb for Camoufox multi-worker rendering
    environment:
      - PORT=8191
      - HOST=0.0.0.0
      - LOG_LEVEL=INFO
      - MAX_BROWSER_WORKERS=auto # Auto-scales across CPU cores, clamped to available RAM (see Environment Configuration below)
      - HEADLESS=true
      - ENABLE_FAST_TLS=true
      - COOKIE_CACHE_TTL=7200
      # - PUID=1000 # Optional: run container process as this UID (must be set with PGID)
      # - PGID=1000 # Optional: run container process as this GID (must be set with PUID)
    volumes:
      - /volume1/docker/solverr/data:/app/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8191/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

4. Click **Deploy / Done**. Solverr dashboard is now available at `http://YOUR-NAS-IP:8191`.

### Method 2: Command Line / SSH on UGREEN NAS

```bash
# Pull and run directly
docker run -d \
  --name solverr \
  --restart unless-stopped \
  -p 8191:8191 \
  --shm-size=2g \
  -v /volume1/docker/solverr/data:/app/data \
  -e MAX_BROWSER_WORKERS=auto \
  -e PUID=1000 \
  -e PGID=1000 \
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
      test: ["CMD", "curl", "-f", "http://localhost:8191/health"]
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

---

## 📊 Environment Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PORT` | `8191` | Server HTTP port |
| `HOST` | `0.0.0.0` | Server binding IP |
| `PUID` | `None` | Optional runtime UID override for the main process (must be paired with `PGID`) |
| `PGID` | `None` | Optional runtime GID override for the main process (must be paired with `PUID`) |
| `MAX_BROWSER_WORKERS` | `auto` | Max concurrent browser workers, and Camoufox pool size (`auto` matches CPU cores, then clamps to a RAM-based cap - see `RAM_PER_WORKER_GB`/`RAM_RESERVED_GB`) |
| `RAM_PER_WORKER_GB` | `1.0` | RAM budgeted per browser worker when auto-tuning `MAX_BROWSER_WORKERS` |
| `RAM_RESERVED_GB` | `2.0` | RAM reserved for the OS/other containers and excluded from auto-tuning's worker budget |
| `ENABLE_FAST_TLS` | `true` | Enables 50ms TLS impersonation fast path |
| `FAST_TLS_ROTATE` | `true` | Rotate the TLS/UA fingerprint per-domain across a matched profile pool instead of one fixed fingerprint |
| `CAMOUFOX_POOL_ENABLED` | `true` | Reuse warm Camoufox processes across no-proxy solves instead of spawning one per request |
| `CAMOUFOX_POOL_RECYCLE_USES` | `40` | Recycle a pooled browser instance after this many solves |
| `CAMOUFOX_POOL_RECYCLE_SECONDS` | `1800` | Recycle a pooled browser instance after this many seconds, whichever comes first |
| `REDIS_URL` | `None` | Optional Redis URL for distributed cookie cache & sessions - required when running multiple replicas, see [Horizontal Scaling](#-horizontal-scaling) |
| `COOKIE_CACHE_TTL` | `7200` | Clearance cookie cache TTL in seconds |
| `FALLBACK_PROXY_URL` | `None` | Optional Tier 4 fallback proxy URL |
| `API_KEY` | `None` | When set, requires a matching `X-Api-Key` header on every endpoint except `/health` and `/metrics` |
| `CAPTCHA_SOLVER_API_KEY` | `None` | Optional 2Captcha-compatible API key for the Tier 3.5 paid-solver escalation on interactive image challenges |
| `CAPTCHA_SOLVER_BASE_URL` | `https://2captcha.com` | API base URL - point at another provider's 2captcha-compatible endpoint (e.g. CapSolver) here |
| `HEADLESS` | `true` | Run browser in headless mode |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## 📜 License

MIT License
