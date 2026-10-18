# Changelog

Solverr follows [semantic versioning](https://semver.org). Releases are published as Docker images on
`ghcr.io/rpeters1430/solverr`; pushing a `vX.Y.Z` tag also produces the matching semver image tags.

## 1.8.0 — 2026-10-18

### Added

- **Fallback proxy pool.** `FALLBACK_PROXY_URLS` (comma-separated) adds Tier 4 proxies alongside
  `FALLBACK_PROXY_URL`. They are rotated round-robin; a proxy that fails cools down for
  `FALLBACK_PROXY_COOLDOWN_SECONDS` (doubling per consecutive failure, up to 8x), and a request tries up to
  `FALLBACK_PROXY_MAX_ATTEMPTS` proxies while its budget lasts. Pool health is in `/api/stats`
  (`fallback_proxies`) and `/metrics` (`solverr_fallback_proxies{state=...}`).
- **Debug capture.** `capture: ["console", "network", "redirects"]` on `/v1`, `/v2` and `/scrape` returns the
  browser's console messages and page errors, every network request with its status, and the main-frame
  redirect chain in `solution.debug` / `debug`. Bounded, with a `truncated` count.
- **MCP `solverr_inspect` tool** built on debug capture, optionally returning the page as Markdown.
- **Markdown and plain-text output.** `format: "markdown" | "text"` on `/scrape` and on MCP `solverr_scrape`.
  HTML pages only; extraction still runs on the original HTML.
- **FlareSolverr 3.x request options** on `/v1`/`/v2`: `returnScreenshot`, `waitInSeconds`, `disableMedia`,
  `session_ttl_minutes`, and `tabs_till_verify` (accepted, not needed).

### Changed

- `/scrape` `tier: "tier4_proxy"` now goes straight to the fallback proxy pool (it used to behave exactly like
  `tier3_browser`), and returns 400 when no fallback proxy is configured or the request has its own `proxy`.
- `/scrape` `tier: "tier2_cache"` now means Fast TLS with cached cookies and no browser (it used to be accepted
  and ignored). Unknown `tier` values are rejected with 422 instead of being treated as `auto`.
- `/v1` requests with `screenshot` (or `returnScreenshot`) now always use the browser, so a screenshot is never
  silently missing because Tier 1 answered first.
- Turnstile, reCAPTCHA and hCaptcha widgets are located with one page evaluation instead of up to 14
  sequential browser round trips per click attempt.
- `/metrics` and `/api/stats` count cached domains from a Redis sorted set (`solverr:cookie_domains`) instead of
  `SCAN`ning every cookie key on each refresh. Cookies cached by an older version are indexed by one `SCAN` per
  process.

### Fixed

- Proxy credentials could appear in logs through raw exception text and tracebacks. Every log line now has
  `user:password@` in URLs masked, and the masking no longer swallows ordinary text that follows a URL.
- A request body that failed a model validator (for example an invalid field value) returned 500 instead of 422,
  because the validation error couldn't be serialized.

### Documentation

- README: the new options, a `/scrape` tier table, and a note that session proxy URLs (with credentials) are
  stored in Redis as plain text, so Redis should stay private and password-protected.
