"""Request/response models and the `/scrape` extraction helper.

Every FlareSolverr-compatible call validates a `V1Request` on the way in and
serializes a `V1Response` (which embeds the full page body) on the way out, so
pydantic validation/serialization is a fixed per-request cost. `_extract_data`
is the CSS/regex extraction engine behind `/scrape`'s `extract_rules`.
"""

from app.api.flaresolverr import _extract_data
from app.models.flaresolverr import CookieModel, SolutionModel, V1Request, V1Response

from sample_data import CLEAN_PAGE_HTML, EXTRACT_RULES, V1_REQUEST_PAYLOAD, make_cookies


def test_v1_request_validation(benchmark):
    result = benchmark(V1Request.model_validate, V1_REQUEST_PAYLOAD)
    assert result.cmd == "request.get"


def test_v1_request_proxy_url(benchmark):
    req = V1Request.model_validate(V1_REQUEST_PAYLOAD)
    result = benchmark(req.get_proxy_url)
    assert result == "http://user:pass@proxy.example.com:8888"


def _solution() -> SolutionModel:
    return SolutionModel(
        url="https://indexer.example.com/api?t=search&q=ubuntu",
        status=200,
        headers={"content-type": "text/html; charset=utf-8", "server": "cloudflare", "cf-ray": "8d3f5a1b"},
        response=CLEAN_PAGE_HTML,
        cookies=make_cookies(20),
        userAgent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
        tier="tier1_fast_tls",
    )


def test_v1_response_serialization(benchmark):
    """Serializing a solved response, page body and cookie jar included."""
    response = V1Response(status="ok", message="Challenge solved!", solution=_solution())
    result = benchmark(response.model_dump_json)
    assert result.startswith('{"status":"ok"')


def test_solution_model_build(benchmark):
    """Constructing the solution Fast TLS returns for every request."""
    cookies = [
        CookieModel(name=f"c{i}", value="v" * 32, domain="indexer.example.com", path="/", expires=-1)
        for i in range(20)
    ]

    def build():
        return SolutionModel(
            url="https://indexer.example.com/api?t=search&q=ubuntu",
            status=200,
            headers={"content-type": "text/html", "server": "cloudflare"},
            response=CLEAN_PAGE_HTML,
            cookies=cookies,
            userAgent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
        )

    assert benchmark(build).status == 200


def test_extract_data_css_and_regex(benchmark):
    """CSS selectors, attribute lookups, list selectors and a regex rule over
    a full search-results page."""
    result = benchmark(_extract_data, CLEAN_PAGE_HTML, EXTRACT_RULES)
    assert result["title"] == "Search results"
    assert len(result["all_titles"]) == 150


def test_extract_data_single_selector(benchmark):
    result = benchmark(_extract_data, CLEAN_PAGE_HTML, {"download_link": "a.download@href"})
    assert result["download_link"] == "/download/0.torrent"
