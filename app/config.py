import os
import json
import math
from typing import List, Optional, Tuple
import psutil


def _cgroup_memory_limit_bytes() -> Optional[int]:
    """Container memory limit (cgroup v2, then v1), or None if unlimited.
    psutil reads /proc/meminfo, which reports host RAM inside a container."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as f:
                val = f.read().strip()
            if val and val != "max":
                n = int(val)
                if n < (1 << 62):  # cgroup v1's "unlimited" sentinel is a huge number, not a real limit
                    return n
        except (OSError, ValueError):
            continue
    return None


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path) as f:
            val = f.read().strip()
        return None if val in ("", "max") else int(val)
    except (OSError, ValueError):
        return None


def cgroup_memory_usage() -> Optional[Tuple[int, int]]:
    """(working_set_bytes, limit_bytes) for this container, or None without a memory limit.

    The working set leaves out inactive page cache, which the kernel reclaims before it OOM-kills,
    the same way `docker stats` and the kubelet count it."""
    limit = _cgroup_memory_limit_bytes()
    if not limit:
        return None
    for usage_path, stat_path, inactive_key in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.stat", "inactive_file"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.stat", "total_inactive_file"),
    ):
        usage = _read_int(usage_path)
        if usage is None:
            continue
        inactive = 0
        try:
            with open(stat_path) as f:
                for line in f:
                    key, _, value = line.partition(" ")
                    if key == inactive_key:
                        inactive = int(value)
                        break
        except (OSError, ValueError):
            pass
        return max(0, usage - inactive), limit
    return None


def _cgroup_cpu_limit() -> Optional[float]:
    """Container CPU quota in cores (cgroup v2, then v1), or None.
    os.cpu_count() reports host cores even under `--cpus=`."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota_str, period_str = f.read().split()
        if quota_str != "max":
            return int(quota_str) / int(period_str)
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as f:
            quota = int(f.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
            period = int(f.read().strip())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def parse_user_prefs(value: Optional[str]) -> dict:
    """USER_PREFS: a JSON object of extra Firefox prefs. A bad value fails startup rather than
    silently launching browsers without the prefs the operator asked for."""
    if not value or not value.strip():
        return {}
    try:
        prefs = json.loads(value)
    except ValueError as e:
        raise ValueError("USER_PREFS must contain valid JSON") from e
    if not isinstance(prefs, dict):
        raise ValueError("USER_PREFS must be a JSON object of pref name to value")
    for name, pref in prefs.items():
        if isinstance(pref, (str, bool)):
            continue
        if isinstance(pref, int) and -2**31 <= pref < 2**31:
            continue
        raise ValueError(f"USER_PREFS['{name}'] must be a string, boolean or signed 32-bit integer")
    return prefs


class Settings:
    PORT: int = int(os.getenv("PORT", "8191"))
    HOST: str = os.getenv("HOST", "0.0.0.0")  # nosec B104
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    _cgroup_cpus: Optional[float] = _cgroup_cpu_limit()
    _cgroup_mem_bytes: Optional[int] = _cgroup_memory_limit_bytes()

    TOTAL_CPU_CORES: int = max(1, math.floor(_cgroup_cpus)) if _cgroup_cpus else (os.cpu_count() or 4)
    TOTAL_RAM_GB: float = (
        round(_cgroup_mem_bytes / (1024**3), 1) if _cgroup_mem_bytes
        else (round(psutil.virtual_memory().total / (1024**3), 1) if hasattr(psutil, "virtual_memory") else 8.0)
    )

    # Caps auto workers low so Solverr doesn't starve Plex/Jellyfin transcodes on a NAS.
    NAS_MODE: bool = os.getenv("NAS_MODE", "false").lower() in ("true", "1", "yes")

    # "auto"/0 sizes workers by CPU cores (1-16), then clamps to the RAM left after the reserve.
    RAM_PER_WORKER_GB: float = float(os.getenv("RAM_PER_WORKER_GB", "2.0" if NAS_MODE else "1.0"))
    RAM_RESERVED_GB: float = float(os.getenv("RAM_RESERVED_GB", "2.0"))

    _max_nas_workers: int = 2 if NAS_MODE else 16
    _cpu_based_workers: int = min(_max_nas_workers, max(1, TOTAL_CPU_CORES))
    _usable_ram_gb: float = TOTAL_RAM_GB - RAM_RESERVED_GB
    _ram_based_workers: int = (
        max(1, int(_usable_ram_gb // RAM_PER_WORKER_GB))
        if _usable_ram_gb > 0 and RAM_PER_WORKER_GB > 0 else 1
    )
    _auto_worker_count: int = min(_cpu_based_workers, _ram_based_workers)

    _raw_workers: str = os.getenv("MAX_BROWSER_WORKERS", "auto").strip()
    if _raw_workers.lower() == "auto" or _raw_workers == "0":
        MAX_BROWSER_WORKERS: int = _auto_worker_count
        WORKER_AUTO_TUNED: bool = True
    else:
        try:
            MAX_BROWSER_WORKERS: int = min(16, max(1, int(_raw_workers)))
            WORKER_AUTO_TUNED: bool = False
        except ValueError:
            MAX_BROWSER_WORKERS: int = _auto_worker_count
            WORKER_AUTO_TUNED: bool = True

    HEADLESS: bool = os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")
    BROWSER_TIMEOUT_MS: int = int(os.getenv("BROWSER_TIMEOUT", "30000"))
    ENABLE_FAST_TLS: bool = os.getenv("ENABLE_FAST_TLS", "true").lower() in ("true", "1", "yes")
    FALLBACK_PROXY_URL: Optional[str] = os.getenv("FALLBACK_PROXY_URL", None)
    
    REDIS_URL: Optional[str] = os.getenv("REDIS_URL", None)
    COOKIE_CACHE_PERSISTENT: bool = os.getenv("COOKIE_CACHE_PERSISTENT", "true").lower() in ("true", "1", "yes")
    COOKIE_CACHE_TTL: int = int(os.getenv("COOKIE_CACHE_TTL", "7200"))
    CACHE_FILE: str = os.getenv("CACHE_FILE", "data/cookies_cache.json")
    HISTORY_DB_PATH: str = os.getenv("HISTORY_DB_PATH", "data/request_history.sqlite")
    HISTORY_MAX_RECORDS: int = int(os.getenv("HISTORY_MAX_RECORDS", "5000"))
    MAX_SCREENSHOT_HEIGHT_PX: int = int(os.getenv("MAX_SCREENSHOT_HEIGHT_PX", "8000"))

    # Local-backend bounds only; Redis relies on TTL expiry instead.
    MAX_CACHE_DOMAINS: int = int(os.getenv("MAX_CACHE_DOMAINS", "1000"))
    MAX_COOKIES_PER_DOMAIN: int = int(os.getenv("MAX_COOKIES_PER_DOMAIN", "100"))
    MAX_SESSIONS: int = int(os.getenv("MAX_SESSIONS", "500"))
    # Cap on one session's saved localStorage/sessionStorage (0 disables); it is stored in Redis per session.
    SESSION_STORAGE_MAX_KB: int = int(os.getenv("SESSION_STORAGE_MAX_KB", "512"))
    
    # Target and UA must name the same Firefox version; a TLS/UA mismatch is a WAF signal.
    FAST_TLS_TARGET: str = os.getenv("FAST_TLS_TARGET", "firefox147")
    DEFAULT_USER_AGENT: str = os.getenv(
        "DEFAULT_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"
    )
    # Sticky per-domain choice among matched TLS/UA profiles, instead of one fixed fingerprint.
    FAST_TLS_ROTATE: bool = os.getenv("FAST_TLS_ROTATE", "true").lower() in ("true", "1", "yes")

    # Warm Camoufox processes, recycled to bound fingerprint reuse and memory growth.
    # Requests with their own proxy always bypass the pool.
    CAMOUFOX_POOL_ENABLED: bool = os.getenv("CAMOUFOX_POOL_ENABLED", "true").lower() in ("true", "1", "yes")
    CAMOUFOX_POOL_RECYCLE_USES: int = int(os.getenv("CAMOUFOX_POOL_RECYCLE_USES", "40"))
    CAMOUFOX_POOL_RECYCLE_SECONDS: int = int(os.getenv("CAMOUFOX_POOL_RECYCLE_SECONDS", "1800"))
    # Close warm instances left unused this long (0 keeps them until recycled); the pool relaunches
    # lazily on the next solve. Frees RAM on a NAS between bursts of indexer traffic.
    CAMOUFOX_POOL_IDLE_TIMEOUT_SECONDS: int = int(os.getenv("CAMOUFOX_POOL_IDLE_TIMEOUT_SECONDS", "0"))
    # Under a container memory limit: retire an instance on release once the working set passes this
    # share of the limit (0 disables), and skip warm replacements below MIN_REPLACE_HEADROOM_MB.
    CAMOUFOX_MEMORY_RECYCLE_PERCENT: float = float(os.getenv("CAMOUFOX_MEMORY_RECYCLE_PERCENT", "85"))
    CAMOUFOX_MIN_REPLACE_HEADROOM_MB: int = int(os.getenv("CAMOUFOX_MIN_REPLACE_HEADROOM_MB", "512"))

    # Extra Firefox prefs for every Camoufox launch, as JSON (e.g. {"network.dns.blockDotOnion": false}).
    # The proxy fail-closed prefs in browser/pool.py always win.
    USER_PREFS: dict = parse_user_prefs(os.getenv("USER_PREFS"))

    # Match timezone/locale/geolocation to the proxy's exit IP; costs one extra request per launch.
    CAMOUFOX_GEOIP_ON_PROXY: bool = os.getenv("CAMOUFOX_GEOIP_ON_PROXY", "true").lower() in ("true", "1", "yes")

# When set, all routes except health checks, /static/*, /favicon.ico, and /metrics unless METRICS_REQUIRE_AUTH=true require X-Api-Key or Authorization: Bearer.
    API_KEY: Optional[str] = os.getenv("API_KEY", None)

    # Off by default because typical Prometheus scrapers send no auth headers.
    METRICS_REQUIRE_AUTH: bool = os.getenv("METRICS_REQUIRE_AUTH", "false").lower() in ("true", "1", "yes")

    # SSRF protection covers initial targets, redirects, and browser subresources.
    ALLOW_PRIVATE_NETWORKS: bool = os.getenv("ALLOW_PRIVATE_NETWORKS", "false").lower() in ("true", "1", "yes")
    ALLOWED_HOSTS: set = {h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()}
    DENIED_HOSTS: set = {h.strip().lower() for h in os.getenv("DENIED_HOSTS", "").split(",") if h.strip()}

    # Size limits in MB; 0 disables the check.
    MAX_REQUEST_BODY_MB: float = float(os.getenv("MAX_REQUEST_BODY_MB", "10"))
    MAX_RESPONSE_BODY_MB: float = float(os.getenv("MAX_RESPONSE_BODY_MB", "50"))
    MAX_SCREENSHOT_MB: float = float(os.getenv("MAX_SCREENSHOT_MB", "8"))

    # Paid 2Captcha-compatible fallback for image challenges clicks can't clear. Off without a key.
    CAPTCHA_SOLVER_API_KEY: Optional[str] = os.getenv("CAPTCHA_SOLVER_API_KEY", None)
    CAPTCHA_SOLVER_BASE_URL: str = os.getenv("CAPTCHA_SOLVER_BASE_URL", "https://2captcha.com")
    CAPTCHA_SOLVER_TIMEOUT: int = int(os.getenv("CAPTCHA_SOLVER_TIMEOUT", "120"))
    CAPTCHA_SOLVER_POLL_INTERVAL: int = int(os.getenv("CAPTCHA_SOLVER_POLL_INTERVAL", "5"))

    # Reuse curl_cffi sessions per (domain, target, proxy) to skip repeat TLS handshakes.
    FAST_TLS_POOL_ENABLED: bool = os.getenv("FAST_TLS_POOL_ENABLED", "true").lower() in ("true", "1", "yes")
    FAST_TLS_POOL_SIZE: int = int(os.getenv("FAST_TLS_POOL_SIZE", "50"))
    # Bounds the per-domain TLS profile score dict, which otherwise grows forever.
    MAX_FAST_TLS_DOMAIN_SCORES: int = int(os.getenv("MAX_FAST_TLS_DOMAIN_SCORES", "2000"))

    # Default for requests that don't set followMetaRefresh: follow short-delay
    # <meta http-equiv="refresh"> redirects (at most 3 hops, delay <= 10s).
    FOLLOW_META_REFRESH: bool = os.getenv("FOLLOW_META_REFRESH", "false").lower() in ("true", "1", "yes")

    ENABLE_MCP: bool = os.getenv("ENABLE_MCP", "true").lower() in ("true", "1", "yes")
    # Without API_KEY, the Host/Origin check is MCP's only DNS-rebinding guard, so it defaults to localhost.
    # Hosts are "host:port" or "host:*"; origins are full URLs. Both are ignored once API_KEY is set.
    MCP_ALLOWED_HOSTS: List[str] = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    MCP_ALLOWED_ORIGINS: List[str] = [o.strip() for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]

    # Plain semver, no "v" prefix or edition suffix, so callers can add their own.
    VERSION: str = "1.7.0"
    EDITION: str = "ultra"
    DISPLAY_VERSION: str = f"{VERSION}-{EDITION}"

settings = Settings()
