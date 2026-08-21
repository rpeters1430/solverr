import os
from typing import Optional
import psutil

class Settings:
    PORT: int = int(os.getenv("PORT", "8191"))
    HOST: str = os.getenv("HOST", "0.0.0.0")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    # Hardware & CPU Info
    TOTAL_CPU_CORES: int = os.cpu_count() or 4
    TOTAL_RAM_GB: float = round(psutil.virtual_memory().total / (1024**3), 1) if hasattr(psutil, "virtual_memory") else 8.0
    
    # Worker Auto-Tuning: "auto" or 0 calculates based on host hardware cores
    # (min 4, max 16), then clamps to what host RAM can actually support.
    # Each worker is a warm Camoufox (Firefox) process - budget ~1GB/worker
    # and always leave ~2GB of host RAM for the OS, the container runtime,
    # and any other services (Sonarr/Radarr/Prowlarr, etc.) sharing the box.
    RAM_PER_WORKER_GB: float = float(os.getenv("RAM_PER_WORKER_GB", "1.0"))
    RAM_RESERVED_GB: float = float(os.getenv("RAM_RESERVED_GB", "2.0"))

    _cpu_based_workers: int = min(16, max(4, TOTAL_CPU_CORES))
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
            MAX_BROWSER_WORKERS: int = int(_raw_workers)
            WORKER_AUTO_TUNED: bool = False
        except ValueError:
            MAX_BROWSER_WORKERS: int = _auto_worker_count
            WORKER_AUTO_TUNED: bool = True

    HEADLESS: bool = os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")
    BROWSER_TIMEOUT_MS: int = int(os.getenv("BROWSER_TIMEOUT", "30000"))
    ENABLE_FAST_TLS: bool = os.getenv("ENABLE_FAST_TLS", "true").lower() in ("true", "1", "yes")
    FALLBACK_PROXY_URL: Optional[str] = os.getenv("FALLBACK_PROXY_URL", None)
    
    # Caching: Dual-Mode (Disk JSON + Optional Redis)
    REDIS_URL: Optional[str] = os.getenv("REDIS_URL", None)
    COOKIE_CACHE_PERSISTENT: bool = os.getenv("COOKIE_CACHE_PERSISTENT", "true").lower() in ("true", "1", "yes")
    COOKIE_CACHE_TTL: int = int(os.getenv("COOKIE_CACHE_TTL", "7200"))
    CACHE_FILE: str = os.getenv("CACHE_FILE", "data/cookies_cache.json")
    
    # Impersonation & User-Agent - Firefox profile, matching Camoufox's
    # Firefox-based fingerprint (the only browser engine Solverr launches).
    # NOTE: target and UA must be for the *same* browser version - a JA3/JA4
    # fingerprint claiming Firefox 147 next to a "Firefox/135.0" UA header is
    # itself a mismatch signal that WAFs can key off. Keep these in sync, or
    # rely on FAST_TLS_ROTATE (app/solver/fast_tls.py) to pick a matched pair.
    FAST_TLS_TARGET: str = os.getenv("FAST_TLS_TARGET", "firefox147")
    DEFAULT_USER_AGENT: str = os.getenv(
        "DEFAULT_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"
    )
    # Rotate the TLS/UA pair per-domain (sticky) across a small pool of
    # matched profiles in the configured target's browser family, instead of
    # presenting one fixed fingerprint to every site this instance touches.
    FAST_TLS_ROTATE: bool = os.getenv("FAST_TLS_ROTATE", "true").lower() in ("true", "1", "yes")

    # Camoufox stealth-browser pool tuning: keep warm browser processes
    # around instead of spawning a fresh one per solve. Instances are
    # recycled after N uses or N seconds to bound fingerprint reuse and
    # memory growth. A request carrying its own proxy always gets a
    # dedicated ephemeral instance (proxy/geo/fingerprint must line up).
    CAMOUFOX_POOL_ENABLED: bool = os.getenv("CAMOUFOX_POOL_ENABLED", "true").lower() in ("true", "1", "yes")
    CAMOUFOX_POOL_RECYCLE_USES: int = int(os.getenv("CAMOUFOX_POOL_RECYCLE_USES", "40"))
    CAMOUFOX_POOL_RECYCLE_SECONDS: int = int(os.getenv("CAMOUFOX_POOL_RECYCLE_SECONDS", "1800"))

    # Optional API key auth. When set, all endpoints except /health and
    # /metrics require an `X-Api-Key` header matching this value.
    API_KEY: Optional[str] = os.getenv("API_KEY", None)

    # Optional paid captcha-solving service (2Captcha-protocol compatible)
    # used as a last-resort escalation for interactive image challenges
    # (hCaptcha puzzle grids, reCAPTCHA image selection) that the free
    # click-based solver in app/solver/browser.py can't clear on its own.
    # Unset by default - no network calls happen unless an API key is set.
    CAPTCHA_SOLVER_API_KEY: Optional[str] = os.getenv("CAPTCHA_SOLVER_API_KEY", None)
    CAPTCHA_SOLVER_BASE_URL: str = os.getenv("CAPTCHA_SOLVER_BASE_URL", "https://2captcha.com")
    CAPTCHA_SOLVER_TIMEOUT: int = int(os.getenv("CAPTCHA_SOLVER_TIMEOUT", "120"))
    CAPTCHA_SOLVER_POLL_INTERVAL: int = int(os.getenv("CAPTCHA_SOLVER_POLL_INTERVAL", "5"))

    # Telemetry
    PROMETHEUS_ENABLED: bool = os.getenv("PROMETHEUS_ENABLED", "true").lower() in ("true", "1", "yes")

    # API Version
    VERSION: str = "v1.5.0-ultra"

settings = Settings()
