# Prowlarr and request reliability implementation plan

Goal: adapt Trawl's relevant correctness fixes to Solverr's Python engine and improve Prowlarr cookie handoff.

Architecture: retain Fast TLS/Camoufox and existing APIs. Use conservative POST handling, explicit private-request isolation, one cancellable deadline, precise active Cloudflare markers, reserved fallback time, and proxy-generated error metadata. Add opt-in FLARESOLVERR_COOKIE_FALLBACK for stateless v1/v2 GETs; successful ping checks must remain successful. No additional runtime dependencies or CAPTCHA services.

1. Add regression tests for ambiguous POST failures, accepted error responses, browser retries, private cache reads/writes and TLS cookie jars. Implement guards without changing safe GET retries.
2. Add deadline tests for session loading and blocked browser queues. Enforce the same deadline around session setup, execution and persistence; cap browser attempts to remaining time and reserve up to 30%/10s for a configured fallback.
3. Add false-positive tests for verification prose, comments and inert examples. Preserve real challenge titles, widgets and provider headers.
4. Add Prowlarr cookie-handoff tests for browser fallback, ping success, disabled mode, unusable cookies and session exclusion. Document and forward the optional setting in Compose.
5. Add proxy tests for generated Proxy-Status, origin status preservation, HEAD/204/304, and incoming Cookie parsing. Keep private upstream cookies isolated.
6. Run the complete unittest suite, ruff and JS renderer tests, review the diff and open a PR.

Review focus: accepted POSTs must never be retried; private credentials must not enter shared clearance state; cancellation must release browser slots; normal Prowlarr ping checks must remain successful; origin errors must not be labeled proxy failures.

## Completion evidence

- Implemented tasks 1–5 and documented the opt-in cookie handoff setting in README and both Compose files.
- Final independent review found forced-fallback POST replay, redirect provenance and Tier 4 cookie validation gaps. Each was reproduced with a failing regression, then fixed and verified.
- Full Python suite: 514 tests passed. Ruff and git diff whitespace checks passed. Release notes UI: 2 Node tests passed.
- Validation used installed pinned dependencies in an isolated Python 3.12 environment. Live Prowlarr UI and real WAF sites were not exercised; Docker/CI validation remains a separate release gate.
