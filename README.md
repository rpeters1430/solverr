# ⚡ Solverr

**Solverr** is an ultra-fast, lightweight, next-generation replacement for **FlareSolverr**, **TRAWL**, and **Byparr**. Designed specifically for high-efficiency deployments on home servers, NAS hardware (**UGREEN NASync DXP4800 Pro / UGOS Pro**, Synology, TrueNAS, Unraid), and unthrottled desktop/Docker environments.

---

## 🔥 Key Features

- **⚡ 4-Tier Adaptive Solver Pipeline**:
  - **Tier 1 (Fast TLS)**: Level 1 & 2 JA3 / TLS Impersonation (`curl_cffi`) solves requests in **30ms – 120ms** without browser overhead.
  - **Tier 2 (Clearance Cache)**: Instant `cf_clearance` & domain cookie jar reuse (**< 50ms**).
  - **Tier 3 (Stealth Browser)**: Camoufox (Stealth Firefox) & Chromium pool with Bézier human mouse curves for hard challenges.
  - **Tier 4 (Fallback Proxy)**: Automatic residential / fallback proxy escalation for rate-limited indexers.
- **🛡️ Multi-WAF & CAPTCHA Solver Suite**: Automated solving for **Cloudflare Turnstile**, **Cloudflare 5s Interstitial**, **Google reCAPTCHA v2 / Enterprise**, **hCaptcha**, **GeeTest**, and **Imperva / Incapsula**.
- **🧩 Optional Paid Solver Escalation (Tier 3.5)**: When the free click-based solver can't clear an interactive image challenge, Solverr can escalate to a 2Captcha-compatible service (2Captcha, CapSolver, etc.) via `CAPTCHA_SOLVER_API_KEY` - fully optional, zero network calls unless configured.
- **🔥 Warm Browser Pool**: Camoufox instances are pooled and reused across requests instead of spawning a fresh Firefox process per solve, with periodic fingerprint recycling to avoid reuse fingerprinting.
- **🖱️ Humanized Bézier Curve Cursor Movement**: Emulates organic human mouse trajectories with micro-jitters, variable velocities, and natural pauses to defeat behavioral bot detection.
- **🔌 100% FlareSolverr v1 & v2 Compatibility**: Standard `POST /v1` and `POST /v2` endpoints compatible out-of-the-box with **Prowlarr**, **Jackett**, **Sonarr**, **Radarr**, and **FlexGet**.
- **🚀 Native High-Performance `POST /scrape` API**: Full programmatic control with tier overrides, DOM selector waiting (`wait_selector`), data extraction (`extract_rules`), and debug screenshots.
- **📊 Native Prometheus Metrics (`GET /metrics`)**: Standard Prometheus exposition format for 1-click scraping in Grafana, Prometheus, or VictoriaMetrics.
- **🧠 Dual-Mode Caching**: Zero-dependency local JSON file persistence by default, with automatic **Redis** cluster backend support via `REDIS_URL`.
- **📊 Real-time Web Control Center**: Live interactive challenge test bench with HTML viewer, screenshot preview, cookie explorer, and hardware monitors.

---

## 🚀 Comparison: FlareSolverr vs. TRAWL vs. Solverr

| Feature / Metric | Traditional FlareSolverr | TRAWL (`germondai/trawl`) | **Solverr** |
| :--- | :--- | :--- | :--- |
| **Engine** | Full Selenium Chrome | Camoufox | **Hybrid (Fast TLS + Camoufox + Playwright)** |
| **Response Latency** | 10s – 18s | ~500ms (cached) / 4–12s (solve) | **30ms – 100ms** (Fast) / **~1.8s** (Browser) |
| **RAM Usage** | ~600MB – 1.2GB | ~150MB – 300MB | **~75MB – 140MB** |
| **Challenge Solvers** | Basic Cloudflare | Turnstile, reCAPTCHA, hCaptcha, GeeTest | **Turnstile, reCAPTCHA v2, hCaptcha, GeeTest, Imperva, Akamai** |
| **Caching Backend** | Memory only | Redis required (2 containers) | **Dual-Engine (Zero-dep Local + Optional Redis)** |
| **Prometheus Telemetry** | ❌ None | ⚠️ External exporter | **✅ Built-in Native `/metrics` endpoint** |
| **Cursor Emulation** | Direct click | Linear cursor | **Realistic Cubic Bézier Curves + Jitter** |
| **Web Dashboard** | Plain text | Basic health | **Modern Real-Time Interactive Test Bench** |

---

## 🟢 Quick Deployment: UGREEN NASync DXP4800 Pro (UGOS Pro)

The **UGREEN NASync DXP4800 Pro** (Intel Core i5-1235U, 10-core / 12-thread CPU with Intel Iris Xe Graphics) runs Solverr with maximum hardware efficiency.

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
    shm_size: '2gb' # Recommended 1gb-2gb for Chromium/Camoufox multi-worker rendering
    environment:
      - PORT=8191
      - HOST=0.0.0.0
      - LOG_LEVEL=INFO
      - MAX_BROWSER_WORKERS=auto # Auto-scales across CPU cores (10-12 workers on DXP4800 Pro)
      - BROWSER_MAX_OLD_SPACE_SIZE=2048
      - HEADLESS=true
      - ENABLE_GPU=true
      - ENABLE_FAST_TLS=true
      - COOKIE_CACHE_TTL=7200
    volumes:
      - /volume1/docker/solverr/data:/app/data
    devices:
      - /dev/dri:/dev/dri # Intel Iris Xe / QuickSync hardware GPU acceleration
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
  --device /dev/dri:/dev/dri \
  -v /volume1/docker/solverr/data:/app/data \
  -e MAX_BROWSER_WORKERS=auto \
  -e ENABLE_GPU=true \
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
      - BROWSER_MAX_OLD_SPACE_SIZE=2048
      - ENABLE_GPU=true
      - ENABLE_FAST_TLS=true
      - COOKIE_CACHE_TTL=7200
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

Running Solverr natively directly on your host computer gives **100% native CPU & GPU performance**, zero Docker virtualization overhead, and unconstrained access to system RAM.

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

---

## 📊 Environment Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PORT` | `8191` | Server HTTP port |
| `HOST` | `0.0.0.0` | Server binding IP |
| `MAX_BROWSER_WORKERS` | `auto` | Max concurrent browser workers, and Camoufox pool size (`auto` matches CPU cores) |
| `ENABLE_GPU` | `true` | Enables GPU hardware acceleration & rasterization |
| `ENABLE_FAST_TLS` | `true` | Enables 50ms TLS impersonation fast path |
| `FAST_TLS_ROTATE` | `true` | Rotate the TLS/UA fingerprint per-domain across a matched profile pool instead of one fixed fingerprint |
| `USE_CAMOUFOX` | `true` | Use Camoufox stealth Firefox engine |
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
