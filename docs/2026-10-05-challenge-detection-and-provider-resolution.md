# Challenge detection and per-provider resolution (2026-10-05)

Branch: `feat/challenge-wall-detection`

This work came out of a comparison of Solverr with [TRAWL](https://github.com/germondai/trawl) and
[Byparr](https://github.com/ThePhaseless/Byparr). It covers the first three items of the improvement
list that comparison produced. The remaining items are at the end.

**Status: unit-tested only.** All 291 unit tests pass. Nothing here has been run against a real
browser or a live protected site yet. See "Before merging" below.

## Why

Reading the three codebases side by side turned up three problems in Solverr:

1. **Real pages were mistaken for challenges.** Detection matched bare brand names anywhere in the
   HTML. A page carrying DataDome's telemetry script, a login form with a reCAPTCHA, or an article
   that mentioned GeeTest was treated as a challenge. Tier 1 escalated it to a browser, and the
   browser loop then waited out most of the request's timeout.
2. **Unsolved challenges were reported as solved.** If a challenge page was served with HTTP 200 and
   never cleared, Solverr returned it with "Challenge solved!".
3. **Several listed providers had no handling.** The README lists Imperva, DataDome, Akamai and
   GeeTest as supported, but beyond a marker list there was no code for them. Only `cf_clearance` and
   `aws-waf-token` counted as proof of a pass.

## What changed

### 1. Detection: type, wall, IP block

`app/solver/browser/challenges.py` now answers three separate questions.

- **Which provider is it?** `detect_challenge()` uses structural markers (element classes, script
  paths, provider-owned hosts) and no bare brand names. It also reads the response headers
  `cf-mitigated`, `x-amzn-waf-action` and `x-dd-b`.
- **Is the page blocked, or is it the real page?** `is_challenge_wall()` separates a provider's own
  interstitial from a real page that embeds a captcha widget or carries a telemetry script. A wall
  needs a challenge title, a 403/429/503, a provider header, interstitial-only markup, or (Imperva
  only) a near-empty body.
- **Is the IP refused outright?** `ip_block_provider()` recognises Cloudflare errors 1005-1009, 1015
  and 1020, and DataDome's hard block. Cloudflare 1010 is left out because a fresh browser
  fingerprint can clear it.

Tier 1 (`fast_tls.py`) no longer escalates a real page that only carries Imperva, DataDome or AWS
WAF scripts.

### 2. Honest failure

In the browser solve loop (`app/solver/browser/browser.py`):

- A wall still present when the attempt ends raises `ChallengeNotSolvedError` instead of being
  returned as a solution.
- A captcha widget embedded in a real page is tried for up to 15 seconds
  (`WIDGET_SOLVE_WINDOW_SECONDS`), then the page is returned as it is.
- An IP block fails on the first content read. The same-IP retry is skipped so the remaining
  timeout goes to the fallback proxy (Tier 4).
- `/v1`, `/v2`, `/scrape` and MCP return the reason to the client, for example
  `cloudflare_turnstile challenge not solved: still present after 38s`. Other failures still return
  only a request id, because their text can contain proxy credentials.

### 3. Per-provider resolution

New module `app/solver/browser/clearance.py` maps each provider to the cookie that proves its check
passed:

| Provider | Cookie |
|---|---|
| Cloudflare | `cf_clearance` |
| Imperva | `reese84`, `___utmvc` |
| Akamai | `_abck` (only once validated) |
| DataDome | `datadome` |
| AWS WAF | `aws-waf-token` |
| DDoS-Guard | `__ddg2_`, `__ddg5_` |

A cookie only counts if it was earned during the current solve. Cached cookies replayed into the
browser are ignored, and so is the cookie DataDome and AWS WAF set on the wall's own response.

The solve loop uses this as follows:

- **Cookie issued but no redirect.** After 5 seconds the loop reloads the original URL itself, once.
- **Wall survives the reload.** If it is still up 10 seconds later, the attempt fails early so the
  fresh-fingerprint retry and Tier 4 get the remaining timeout.
- **Akamai.** Its press-and-hold button is held once for 5.5 seconds and the mouse keeps moving. The
  generic click logic is skipped for it.
- **DataDome slider and AWS WAF image captcha.** Neither the click loop nor the paid solver can
  handle these, so the attempt fails after two readings instead of waiting out the timeout.

## Behaviour changes to be aware of

- **A wall served with HTTP 200 is now an error.** Prowlarr will show a failure where it previously
  received challenge HTML as a "success".
- **A clearance cookie alone no longer clears a wall.** Previously any `cf_clearance` or
  `aws-waf-token` in the cookie jar marked the challenge as cleared, which could return the
  interstitial's HTML. That shortcut now applies only to a widget embedded in a real page.
- **A reload re-submits a POST.** If the original request was a POST and the reload in section 3
  triggers, the body is sent again.
- **One existing test was rewritten.** `test_loop_stops_at_attempt_deadline` asserted the old
  behaviour (a 503 solution for an uncleared wall). It now expects the error.

## Before merging

- **Test against real trackers.** Run `docker compose up --build`, open the dashboard at
  `http://localhost:8191`, and put three or four Cloudflare-protected trackers through the test
  bench, including any that have been slow or failing.
- **Unverified numbers.** These are judgement calls or values ported from TRAWL, not measurements:
  the 1,500-character Imperva stub cut-off (TRAWL uses 5,000), the 5-second and 10-second cookie
  graces, the 15-second widget window, and the Akamai button selectors.
- **Imperva, Akamai, DataDome and AWS WAF paths are untested live.** For torrent-tracker use they
  rarely come up; Cloudflare and DDoS-Guard are the ones that matter.
- **Benchmarks were not run.** `pytest-codspeed` is not installed locally. The benchmark assertions
  were checked by hand, and detection cost on a clean page is unchanged (about 0.45 ms).
- **The test suite is slower.** It went from about 10 seconds to about 22, because the new flow
  tests wait on real timers.

## Not done

- Per-tier timings in the `/scrape` response (part of item 2).
- GeeTest still has detection only; a slider solver would be part of item 4.

## Remaining ideas from the comparison

Ordered for torrent-tracker use.

| Item | Recommendation |
|---|---|
| 7. Pool hardening: match timezone and language to the exit IP, cap Firefox content processes, warm the browser at startup | Do next. Small, and aimed at Cloudflare pass rate and NAS memory use. |
| 5. Forward HTTPS proxy for Prowlarr | Only if a tracker passes in Solverr's test bench but still fails in Prowlarr. That happens when a site ties its clearance to the solving browser's connection, so the cookie does not work from Prowlarr's own client. Largest build on the list. |
| 4. Free captcha solving (reCAPTCHA audio, GeeTest slider) | Skip for tracker use. |
| 6. Markdown/text output for AI agents | Skip for tracker use. |
