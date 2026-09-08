"""Realistic sample payloads shared by the CodSpeed benchmark suite.

The HTML documents mirror what the solver actually sees: a Cloudflare
interstitial and a full indexer search-results page.
"""

from app.models.flaresolverr import CookieModel


CHALLENGE_PAGE_TITLE = "Just a moment..."

CHALLENGE_PAGE_HTML = """<!DOCTYPE html>
<html lang="en-US"><head><meta charset="UTF-8"><title>Just a moment...</title>
<meta http-equiv="X-UA-Compatible" content="IE=Edge"><meta name="robots" content="noindex,nofollow">
<link href="/cdn-cgi/styles/challenges.css" rel="stylesheet">
</head><body class="no-js"><div class="main-wrapper" role="main"><div class="main-content">
<h1 class="zone-name-title h1"><img class="heading-favicon" src="/favicon.ico"/>indexer.example.com</h1>
<h2 class="h2" id="challenge-running">Checking if the site connection is secure</h2>
<div id="challenge-stage"><div class="cf-turnstile" data-sitekey="0x4AAAAAAADnPIDROrmt1Wwj"
 data-callback="onTurnstileSuccess"></div></div>
<div id="challenge-body-text" class="core-msg spacer">indexer.example.com needs to review the security of your
 connection before proceeding.</div></div></div>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" defer></script>
<script>window._cf_chl_opt={cvId:'2',cType:'managed',cRay:'8d3f5a1b2c3d4e5f'};</script>
</body></html>"""

CLEAN_PAGE_TITLE = "Search results - Example Indexer"


def _build_listing_rows(count: int) -> str:
    rows = []
    for i in range(count):
        rows.append(
            f'<tr class="release" data-id="{i}">'
            f'<td class="title"><a href="/details/{i}" title="Release {i}">Ubuntu.Release.{i}.iso</a></td>'
            f'<td class="size">{(i % 40) + 1}.4 GB</td>'
            f'<td class="seeders">{i * 3 % 511}</td>'
            f'<td class="leechers">{i % 97}</td>'
            f'<td class="added"><span class="dt">2026-0{(i % 9) + 1}-1{i % 10}</span></td>'
            f'<td class="dl"><a class="download" href="/download/{i}.torrent" '
            f'data-hash="e3b0c44298fc1c149afbf4c8996fb924{i:08d}">grab</a></td>'
            "</tr>"
        )
    return "".join(rows)


CLEAN_PAGE_HTML = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    f"<title>{CLEAN_PAGE_TITLE}</title>"
    '<link rel="stylesheet" href="/static/app.css"><meta name="description" content="Torrent indexer search">'
    "</head><body><div id=\"wrapper\"><header><nav><a href=\"/\">Home</a><a href=\"/browse\">Browse</a></nav></header>"
    '<main><h1 class="page-title">Search results</h1>'
    '<table id="results" class="table"><thead><tr><th>Title</th><th>Size</th><th>S</th><th>L</th>'
    "<th>Added</th><th>DL</th></tr></thead><tbody>"
    + _build_listing_rows(150)
    + "</tbody></table></main>"
    '<footer><p class="copyright">Example Indexer</p></footer></div></body></html>'
)

# Lower-cased copies: the solve loop hands `detect_challenge` an already
# lower-cased body (see fast_tls.request / BrowserPool._execute_solve_flow).
CHALLENGE_PAGE_HTML_LOWER = CHALLENGE_PAGE_HTML.lower()
CLEAN_PAGE_HTML_LOWER = CLEAN_PAGE_HTML.lower()

EXTRACT_RULES = {
    "title": "h1.page-title",
    "first_release": "table#results td.title a",
    "download_link": "a.download@href",
    "all_titles": "td.title a[]",
    "all_sizes": "td.size[]",
    "result_count": r"regex:<tr class=\"release\" data-id=\"(\d+)\"",
}


def make_cookies(count: int, domain: str = "indexer.example.com") -> list[CookieModel]:
    """A realistic post-solve cookie jar: a cf_clearance plus session/tracking
    cookies of the kind a Cloudflare-fronted indexer sets."""
    cookies = [
        CookieModel(
            name="cf_clearance",
            value="j9Xk2lP" + "a1b2c3d4e5" * 8,
            domain=f".{domain}",
            path="/",
            expires=4102444800.0,
            size=96,
            httpOnly=True,
            secure=True,
            sameSite="None",
        )
    ]
    for i in range(count - 1):
        cookies.append(
            CookieModel(
                name=f"sess_{i}",
                value=f"value-{i}-" + "x" * 24,
                domain=domain,
                path="/" if i % 3 else f"/sub{i % 5}",
                expires=-1 if i % 2 else 4102444800.0,
                size=40,
                httpOnly=bool(i % 2),
                secure=True,
                session=not bool(i % 2),
            )
        )
    return cookies


V1_REQUEST_PAYLOAD = {
    "cmd": "request.get",
    "url": "https://indexer.example.com/api?t=search&q=ubuntu&cat=5070",
    "maxTimeout": 60000,
    "session": "prowlarr-session-1",
    "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
    "headers": {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Api-Key": "indexer-key",
    },
    "proxy": {"url": "http://proxy.example.com:8888", "username": "user", "password": "pass"},
    "cookies": [
        {"name": "cf_clearance", "value": "abc123" * 10, "domain": ".indexer.example.com", "path": "/"},
        {"name": "PHPSESSID", "value": "0123456789abcdef", "domain": "indexer.example.com", "path": "/"},
    ],
    "returnOnlyCookies": False,
    "fastTlsOnly": False,
    "forceBrowser": False,
}
