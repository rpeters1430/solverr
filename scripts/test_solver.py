#!/usr/bin/env python3
"""
Solverr Verification & Benchmark Suite
Test multiple sites across Fast TLS, Cloudflare, and real-world indexers.
Supports concurrent or sequential execution, per-tier breakdown, and cookie verification.
"""

import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# Ensure stdout supports UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ANSI color codes
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

# Default curated test sites
DEFAULT_SITES = [
    # Tier 1: Fast TLS verification
    {
        "url": "https://httpbin.org/get",
        "category": "fast_tls",
        "description": "Standard HTTP GET (HTTPBin - Baseline TLS)",
    },
    {
        "url": "https://tls.peet.ws/api/all",
        "category": "fast_tls",
        "description": "TLS JA3 / Fingerprint Inspection",
    },
    # Tier 3: Cloudflare Turnstile & WAF Challenges
    {
        "url": "https://nowsecure.nl",
        "category": "cloudflare",
        "description": "Cloudflare Under Attack / Turnstile (nowsecure.nl)",
    },
    {
        "url": "https://cloudflare.manfredi.io/",
        "category": "cloudflare",
        "description": "Cloudflare Challenge Test Page",
    },
    {
        "url": "https://2captcha.com/demo/cloudflare-turnstile",
        "category": "cloudflare",
        "description": "2Captcha Turnstile Demo",
    },
    # Indexers & Trackers (Common Prowlarr/Jackett targets)
    {
        "url": "https://1337x.to",
        "category": "indexers",
        "description": "1337x Torrent Indexer (Cloudflare / Turnstile)",
    },
    {
        "url": "https://torrentgalaxy.to",
        "category": "indexers",
        "description": "TorrentGalaxy (Cloudflare Protected)",
    },
    {
        "url": "https://nyaa.si",
        "category": "indexers",
        "description": "Nyaa Anime Tracker",
    },
    {
        "url": "https://yts.mx",
        "category": "indexers",
        "description": "YTS Movie Indexer",
    },
    {
        "url": "https://ext.to",
        "category": "indexers",
        "description": "EXT Torrent Indexer",
    },
]


def check_health(server_url: str) -> Optional[Dict[str, Any]]:
    """Check if the Solverr server is online and report health status."""
    health_url = urllib.parse.urljoin(server_url.rstrip("/") + "/", "health")
    try:
        req = urllib.request.Request(health_url, headers={"User-Agent": "Solverr-Test/1.0"})
        with urllib.request.urlopen(req, timeout=5) as res:
            if res.status == 200:
                data = json.loads(res.read().decode("utf-8"))
                return data
    except Exception as e:
        print(f"{RED}Health check failed connecting to {health_url}: {e}{RESET}")
    return None


def solve_site(
    server_url: str,
    target_url: str,
    timeout_ms: int = 60000,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch a request.get command to Solverr /v1 endpoint."""
    v1_url = urllib.parse.urljoin(server_url.rstrip("/") + "/", "v1")
    payload = {
        "cmd": "request.get",
        "url": target_url,
        "maxTimeout": timeout_ms,
    }

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Solverr-Test-Suite/1.0",
    }
    if api_key:
        headers["X-Api-Key"] = api_key

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(v1_url, data=data, headers=headers)

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0 + 10) as response:
            elapsed_sec = time.perf_counter() - t0
            body = response.read().decode("utf-8")
            res_json = json.loads(body)
            res_json["elapsed_sec"] = elapsed_sec
            return res_json
    except urllib.error.HTTPError as e:
        elapsed_sec = time.perf_counter() - t0
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body)
            err_json["elapsed_sec"] = elapsed_sec
            return err_json
        except Exception:
            return {
                "status": "error",
                "message": f"HTTP {e.code}: {e.reason}",
                "elapsed_sec": elapsed_sec,
            }
    except Exception as e:
        elapsed_sec = time.perf_counter() - t0
        return {
            "status": "error",
            "message": str(e),
            "elapsed_sec": elapsed_sec,
        }


def format_tier(tier: Optional[str]) -> str:
    """Format solving tier with color."""
    if not tier:
        return f"{DIM}unknown{RESET}"
    if "fast_tls" in tier:
        return f"{CYAN}[Tier 1: Fast TLS]{RESET}"
    if "cache" in tier:
        return f"{GREEN}[Tier 2: Cached]{RESET}"
    if "browser" in tier or "camoufox" in tier:
        return f"{MAGENTA}[Tier 3: Browser]{RESET}"
    if "proxy" in tier:
        return f"{YELLOW}[Tier 4: Proxy]{RESET}"
    return f"{BOLD}[{tier}]{RESET}"


def run_single_test(
    idx: int,
    total: int,
    site: Dict[str, Any],
    server_url: str,
    timeout_ms: int,
    verbose: bool,
    api_key: Optional[str],
) -> Dict[str, Any]:
    """Run test for one site and print real-time result."""
    url = site["url"]
    desc = site.get("description", url)

    print(f"\n{BOLD}[{idx}/{total}]{RESET} Testing: {CYAN}{url}{RESET}")
    if desc != url:
        print(f"       Description: {DIM}{desc}{RESET}")

    res = solve_site(server_url, url, timeout_ms=timeout_ms, api_key=api_key)
    status = res.get("status")
    solution = res.get("solution") or {}
    elapsed = res.get("elapsed_sec", 0.0)
    tier = solution.get("tier")
    http_status = solution.get("status")
    cookies = solution.get("cookies", [])
    cookie_names = [c.get("name") for c in cookies if isinstance(c, dict)]

    is_success = status == "ok" and http_status in (200, 201, 204, 301, 302, 304)

    if is_success:
        symbol = f"{GREEN}✓ PASS{RESET}"
        tier_str = format_tier(tier)
        print(
            f"       Result: {symbol} in {BOLD}{elapsed:.2f}s{RESET} | "
            f"HTTP {GREEN}{http_status}{RESET} | {tier_str}"
        )
        if cookie_names:
            important = [c for c in cookie_names if "cf" in c.lower() or "clearance" in c.lower() or "session" in c.lower()]
            display_cookies = important if important else cookie_names[:3]
            extra = f" (+{len(cookie_names) - len(display_cookies)} more)" if len(cookie_names) > len(display_cookies) else ""
            print(f"       Cookies ({len(cookie_names)}): {DIM}{', '.join(display_cookies)}{extra}{RESET}")
        if verbose:
            print(f"       User-Agent: {DIM}{solution.get('userAgent', 'N/A')}{RESET}")
    else:
        symbol = f"{RED}✗ FAIL{RESET}"
        msg = res.get("message") or f"HTTP {http_status}"
        print(
            f"       Result: {symbol} in {BOLD}{elapsed:.2f}s{RESET} | "
            f"HTTP {RED}{http_status or 'N/A'}{RESET} | Message: {RED}{msg}{RESET}"
        )

    return {
        "url": url,
        "description": desc,
        "success": is_success,
        "http_status": http_status,
        "tier": tier,
        "elapsed_sec": elapsed,
        "cookie_count": len(cookies),
        "message": res.get("message"),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Solverr Challenge Solver Verification & Test Suite",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        "-H",
        default=os.environ.get("SOLVERR_URL", "http://192.168.50.114:8191"),
        help="Solverr server base URL",
    )
    parser.add_argument(
        "--category",
        "-c",
        choices=["all", "fast_tls", "cloudflare", "indexers"],
        default="all",
        help="Category of sites to test",
    )
    parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=None,
        help="Limit number of sites to test (e.g. -n 3)",
    )
    parser.add_argument(
        "--urls",
        "-u",
        nargs="+",
        help="Custom site URLs to test (e.g. -u https://example.com https://nowsecure.nl)",
    )
    parser.add_argument(
        "--file",
        "-f",
        type=str,
        help="File path containing custom URLs (one per line)",
    )
    parser.add_argument(
        "--timeout",
        "-t",
        type=int,
        default=60,
        help="Per-site solve timeout in seconds",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=1,
        help="Concurrent test workers (1 for sequential, >1 for parallel)",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("SOLVERR_API_KEY"),
        help="Optional API key (if API_KEY is configured on Solverr)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print verbose details (User-Agent, headers, etc.)",
    )

    args = parser.parse_args()
    server_url = args.host.rstrip("/")

    print(f"\n{BOLD}{CYAN}======================================================{RESET}")
    print(f"{BOLD}{CYAN}      Solverr Verification & Benchmark Suite{RESET}")
    print(f"{BOLD}{CYAN}======================================================{RESET}")
    print(f"Target Server: {BOLD}{server_url}{RESET}")

    # 1. Health check pre-flight
    health = check_health(server_url)
    if not health:
        print(f"{RED}Cannot connect to Solverr at {server_url}. Is the container running?{RESET}")
        sys.exit(1)

    print(f"Server Health: {GREEN}ONLINE{RESET}")
    print(
        f"Engine: {BOLD}{health.get('stealth_engine', 'N/A')}{RESET} | "
        f"Version: {health.get('version', 'N/A')} | "
        f"Workers: {health.get('workers', 'N/A')} | "
        f"Camoufox: {GREEN if health.get('camoufox_available') else RED}{health.get('camoufox_available')}{RESET}"
    )

    # 2. Assemble test list
    test_sites: List[Dict[str, Any]] = []

    if args.urls:
        for u in args.urls:
            test_sites.append({"url": u, "category": "custom", "description": u})
    elif args.file:
        if not os.path.exists(args.file):
            print(f"{RED}File not found: {args.file}{RESET}")
            sys.exit(1)
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    test_sites.append({"url": line, "category": "custom", "description": line})
    else:
        if args.category == "all":
            test_sites = list(DEFAULT_SITES)
        else:
            test_sites = [s for s in DEFAULT_SITES if s.get("category") == args.category]

    if args.count and args.count > 0:
        test_sites = test_sites[:args.count]

    total = len(test_sites)
    print(f"Test Sites:    {BOLD}{total} sites{RESET} (category: {args.category}, concurrency: {args.workers})")
    print(f"{DIM}------------------------------------------------------{RESET}")

    results: List[Dict[str, Any]] = []
    timeout_ms = args.timeout * 1000

    suite_t0 = time.perf_counter()

    if args.workers > 1:
        print(f"{YELLOW}Running in parallel with {args.workers} workers...{RESET}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    run_single_test,
                    idx + 1,
                    total,
                    site,
                    server_url,
                    timeout_ms,
                    args.verbose,
                    args.api_key,
                ): site
                for idx, site in enumerate(test_sites)
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    print(f"{RED}Worker exception: {e}{RESET}")
    else:
        for idx, site in enumerate(test_sites):
            r = run_single_test(
                idx + 1,
                total,
                site,
                server_url,
                timeout_ms,
                args.verbose,
                args.api_key,
            )
            results.append(r)

    total_suite_time = time.perf_counter() - suite_t0

    # 3. Print Summary
    passed = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    tier1_solves = [r for r in passed if r.get("tier") and "fast_tls" in r["tier"]]
    tier3_solves = [r for r in passed if r.get("tier") and ("browser" in r["tier"] or "camoufox" in r["tier"])]

    print(f"\n{BOLD}{CYAN}======================================================{RESET}")
    print(f"{BOLD}{CYAN}                 TEST SUITE SUMMARY{RESET}")
    print(f"{BOLD}{CYAN}======================================================{RESET}")
    print(f"Total Tested:   {BOLD}{len(results)}{RESET}")
    print(f"Passed:         {GREEN}{len(passed)}{RESET} / {len(results)} ({len(passed)/len(results)*100:.1f}%)" if results else "0")
    print(f"Failed:         {RED}{len(failed)}{RESET} / {len(results)}")
    print(f"Total Runtime:  {BOLD}{total_suite_time:.2f}s{RESET}")

    if tier1_solves:
        avg_t1 = sum(r["elapsed_sec"] for r in tier1_solves) / len(tier1_solves)
        print(f"Tier 1 (Fast TLS): {CYAN}{len(tier1_solves)} solves{RESET} (Avg: {BOLD}{avg_t1*1000:.0f}ms{RESET})")
    if tier3_solves:
        avg_t3 = sum(r["elapsed_sec"] for r in tier3_solves) / len(tier3_solves)
        print(f"Tier 3 (Browser):  {MAGENTA}{len(tier3_solves)} solves{RESET} (Avg: {BOLD}{avg_t3:.2f}s{RESET})")

    if failed:
        print(f"\n{RED}{BOLD}Failed Sites:{RESET}")
        for f in failed:
            print(f" - {f['url']}: {f.get('message') or ('HTTP ' + str(f.get('http_status')))}")

    print(f"{BOLD}{CYAN}======================================================{RESET}\n")

    sys.exit(0 if len(failed) == 0 else 1)


if __name__ == "__main__":
    main()
