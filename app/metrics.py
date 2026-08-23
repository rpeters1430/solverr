import os
import psutil
from app.solver.engine import metrics
from app.solver.cache import cookie_cache
from app.solver.sessions import session_manager
from app.solver.browser import browser_pool
from app.config import settings

def generate_prometheus_metrics() -> str:
    """
    Generates standard Prometheus exposition format text for /metrics endpoint.
    Compatible with Prometheus, VictoriaMetrics, Grafana Agent, and OpenTelemetry collector.
    """
    proc = psutil.Process(os.getpid())
    mem_bytes = proc.memory_info().rss
    cpu_pct = psutil.cpu_percent(interval=None)

    stats = metrics.to_dict()

    from app.solver.browser import CAMOUFOX_AVAILABLE
    stealth_engine = "camoufox" if CAMOUFOX_AVAILABLE else "unavailable"

    lines = [
        "# HELP solverr_info Build and version metadata about the Solverr engine instance",
        "# TYPE solverr_info gauge",
        f'solverr_info{{version="{settings.VERSION}",stealth_engine="{stealth_engine}",tls_target="{settings.FAST_TLS_TARGET}"}} 1',
        "",
        "# HELP solverr_requests_total Total number of processed requests partitioned by execution tier and outcome",
        "# TYPE solverr_requests_total counter",
        f'solverr_requests_total{{tier="tier1_fast_tls",status="success"}} {stats["tier1_fast_tls_hits"]}',
        f'solverr_requests_total{{tier="tier2_cache",status="success"}} {stats["tier2_cache_hits"]}',
        f'solverr_requests_total{{tier="tier3_browser",status="success"}} {stats["tier3_stealth_browser_solves"]}',
        f'solverr_requests_total{{tier="tier4_proxy_fallback",status="success"}} {stats["tier4_fallback_proxy_hits"]}',
        f'solverr_requests_total{{tier="all",status="failed"}} {stats["failed_requests"]}',
        "",
        "# HELP solverr_request_duration_avg_ms Average request latency in milliseconds by solver tier",
        "# TYPE solverr_request_duration_avg_ms gauge",
        f'solverr_request_duration_avg_ms{{tier="fast"}} {stats["avg_fast_ms"]}',
        f'solverr_request_duration_avg_ms{{tier="browser"}} {stats["avg_browser_ms"]}',
        "",
        "# HELP solverr_fast_hit_rate_percent Percentage of requests served without launching a full browser",
        "# TYPE solverr_fast_hit_rate_percent gauge",
        f'solverr_fast_hit_rate_percent {stats["fast_hit_rate_pct"]}',
        "",
        "# HELP solverr_active_workers Number of configured concurrent browser worker slots",
        "# TYPE solverr_active_workers gauge",
        f'solverr_active_workers {settings.MAX_BROWSER_WORKERS}',
        "",
        "# HELP solverr_cached_domains_count Number of unique domains with active cached cookies",
        "# TYPE solverr_cached_domains_count gauge",
        f'solverr_cached_domains_count {len(cookie_cache.get_all_entries())}',
        "",
        "# HELP solverr_active_sessions Number of active pinned proxy sessions",
        "# TYPE solverr_active_sessions gauge",
        f'solverr_active_sessions {len(session_manager.list_sessions())}',
        "",
        "# HELP solverr_memory_bytes Resident set memory size consumed by Solverr process in bytes",
        "# TYPE solverr_memory_bytes gauge",
        f'solverr_memory_bytes {mem_bytes}',
        "",
        "# HELP solverr_cpu_usage_percent CPU utilization percentage of Solverr host process",
        "# TYPE solverr_cpu_usage_percent gauge",
        f'solverr_cpu_usage_percent {cpu_pct}',
        "",
        "# HELP solverr_challenges_solved_total Count of specific bot challenges successfully solved",
        "# TYPE solverr_challenges_solved_total counter"
    ]

    for ctype, count in stats.get("challenges_solved", {}).items():
        lines.append(f'solverr_challenges_solved_total{{type="{ctype}"}} {count}')

    lines += [
        "",
        "# HELP solverr_cookie_cache_lookups_total Cookie cache lookups by outcome, independent of which tier ultimately served the request",
        "# TYPE solverr_cookie_cache_lookups_total counter",
        f'solverr_cookie_cache_lookups_total{{outcome="hit"}} {stats["cookie_cache_lookup_hits"]}',
        f'solverr_cookie_cache_lookups_total{{outcome="miss"}} {stats["cookie_cache_lookup_misses"]}',
        "",
        "# HELP solverr_timeouts_total Count of Tier 3 browser solves that exceeded their timeout budget",
        "# TYPE solverr_timeouts_total counter",
        f'solverr_timeouts_total {stats["timeouts_total"]}',
        "",
        "# HELP solverr_requests_inflight Currently in-flight deduplicated solve requests",
        "# TYPE solverr_requests_inflight gauge",
        f'solverr_requests_inflight {len(_inflight_snapshot())}',
    ]

    pool_stats = browser_pool.pool_stats()
    lines += [
        "",
        "# HELP solverr_browser_pool_size Configured Camoufox warm-pool capacity",
        "# TYPE solverr_browser_pool_size gauge",
        f'solverr_browser_pool_size {pool_stats["pool_size"]}',
        "",
        "# HELP solverr_browser_pool_busy Warm Camoufox instances currently checked out",
        "# TYPE solverr_browser_pool_busy gauge",
        f'solverr_browser_pool_busy {pool_stats["busy"]}',
        "",
        "# HELP solverr_browser_pool_idle Warm Camoufox instances currently idle and available",
        "# TYPE solverr_browser_pool_idle gauge",
        f'solverr_browser_pool_idle {pool_stats["idle"]}',
        "",
        "# HELP solverr_browser_pool_recycles_total Pooled Camoufox instances recycled (use/age budget exhausted)",
        "# TYPE solverr_browser_pool_recycles_total counter",
        f'solverr_browser_pool_recycles_total {pool_stats["recycles_total"]}',
        "",
        "# HELP solverr_browser_crashes_total Tier 3 solve attempts that exhausted both the pooled and ephemeral Camoufox paths",
        "# TYPE solverr_browser_crashes_total counter",
        f'solverr_browser_crashes_total {pool_stats["crashes_total"]}',
        "",
        "# HELP solverr_browser_queue_wait_seconds Average time a request waited for a free browser worker slot",
        "# TYPE solverr_browser_queue_wait_seconds gauge",
        f'solverr_browser_queue_wait_seconds {pool_stats["avg_queue_wait_seconds"]}',
    ]

    lines += [
        "",
        "# HELP solverr_request_duration_seconds Request latency in seconds, partitioned by execution tier",
        "# TYPE solverr_request_duration_seconds histogram",
    ]
    for tier_name, hist in metrics.duration_histograms.items():
        for bucket in hist.buckets:
            lines.append(f'solverr_request_duration_seconds_bucket{{tier="{tier_name}",le="{bucket}"}} {hist.bucket_counts[bucket]}')
        lines.append(f'solverr_request_duration_seconds_bucket{{tier="{tier_name}",le="+Inf"}} {hist.count}')
        lines.append(f'solverr_request_duration_seconds_sum{{tier="{tier_name}"}} {round(hist.sum, 4)}')
        lines.append(f'solverr_request_duration_seconds_count{{tier="{tier_name}"}} {hist.count}')

    lines.append("")
    return "\n".join(lines)


def _inflight_snapshot():
    from app.solver.engine import solver_engine
    return solver_engine._inflight
