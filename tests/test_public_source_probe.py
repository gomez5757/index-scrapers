import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "probe_published_sources", ROOT / "scripts/probe_published_sources.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, body, chunk_size=256):
        self.body = body
        self.chunk_size = chunk_size
        self.bytes_yielded = 0
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.body), self.chunk_size):
            chunk = self.body[offset:offset + self.chunk_size]
            self.bytes_yielded += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


def public_documents():
    return {
        probe.ROBOTS_URL: (
            "text/plain", b"User-agent: *\nDisallow: /rss/\nSitemap: https://www.idealista.com/sitemap.xml\n"
        ),
        probe.OPENAPI_URL: (
            "application/json", json.dumps({
                "components": {"securitySchemes": {"RapidAPIKey": {
                    "type": "apiKey", "in": "header", "name": "X-RapidAPI-Key"
                }}},
                "security": [{"RapidAPIKey": []}],
            }).encode()
        ),
        probe.LOCATIONS_URL: (
            "application/json", json.dumps({
                "generatedAt": "2026-07-22",
                "rent": {"totalListings": 87444, "segments": [{"name": "synthetic"}]},
                "sale": {"segments": [{}, {}]},
            }).encode()
        ),
        probe.AGENCY_URL: (
            "text/html", (
                '<a href="' + probe.IDEALISTA_AGENCY_LINK + '">Public link</a>'
                '<img src="' + probe.IMAGE_URL + '" title="Private title 999999 EUR">'
            ).encode()
        ),
        probe.IMAGE_URL: ("image/webp", b"RIFF\x10\x00\x00\x00WEBP" + b"x" * 2048),
    }


def run_documents(documents=None, overrides=None):
    documents = public_documents() if documents is None else documents
    overrides = overrides or {}
    requests = []
    streams = {}

    def handler(request):
        url = str(request.url)
        requests.append(request)
        content_type, body = documents[url]
        status, body = overrides.get(url, (200, body))
        stream = streams[url] = CountingStream(body)
        return httpx.Response(status, headers={
            "Content-Type": content_type + "; private-token=do-not-record",
            "Set-Cookie": "session=do-not-send; Path=/",
            "Location": "https://private.example/?key=do-not-record",
        }, stream=stream)

    report = asyncio.run(probe.run_probe(transport=httpx.MockTransport(handler)))
    return report, requests, streams


def by_url(report):
    return {result["url"]: result for result in report["results"]}


def test_one_ordinary_get_per_public_resource_without_identity_or_credentials(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://secret:password@proxy.invalid")
    monkeypatch.setenv("X_RAPIDAPI_KEY", "never-send-this")
    original_client = httpx.AsyncClient
    client_options = []

    def client(**kwargs):
        client_options.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(probe.httpx, "AsyncClient", client)
    report, requests, _ = run_documents()
    assert report["diagnostic_complete"] is True
    assert [str(request.url) for request in requests] == list(public_documents())
    assert len(set(str(request.url) for request in requests)) == 5
    for request in requests:
        assert request.method == "GET"
        assert request.headers["user-agent"].startswith("python-httpx/")
        assert request.headers["accept-encoding"] == "identity"
        assert set(request.headers) <= {
            "host", "accept", "accept-encoding", "connection", "user-agent"
        }
        assert all(0 < timeout <= 10 for timeout in request.extensions["timeout"].values())
    assert all(option["trust_env"] is False for option in client_options)
    assert all(option["follow_redirects"] is False for option in client_options)
    saved = json.dumps(report)
    for private in ("do-not-record", "do-not-send", "never-send-this", "999999", "synthetic", "87444"):
        assert private not in saved
    assert all(result["content_type"] in {"text/plain", "application/json", "text/html", "image/webp"}
               for result in report["results"])


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("blocked_url,skipped_url", [
    ("https://apidea.es/openapi.json", "https://apidea.es/locations-es-index.json"),
    ("https://delaordeninmobiliaria.es/inmuebles/",
     "https://delaordeninmobiliaria.es/wp-content/uploads/2026/07/piso-venta-calle-oriana-19-madrid-768x512.webp"),
])
def test_a_blocked_origin_is_not_requested_again(status, blocked_url, skipped_url):
    report, requests, streams = run_documents(overrides={blocked_url: (status, b"private challenge")})
    results = by_url(report)
    assert results[blocked_url]["status"] == "blocked"
    assert results[blocked_url]["http_status"] == status
    assert streams[blocked_url].bytes_yielded == 0
    assert results[skipped_url]["status"] == "skipped"
    assert results[skipped_url]["reason"] == "origin_blocked"
    assert results[skipped_url]["http_status"] is None
    assert skipped_url not in [str(request.url) for request in requests]
    assert report["diagnostic_complete"] is True


@pytest.mark.parametrize("blocked_url,skipped_url", [
    ("https://apidea.es/openapi.json", "https://apidea.es/locations-es-index.json"),
    ("https://delaordeninmobiliaria.es/inmuebles/",
     "https://delaordeninmobiliaria.es/wp-content/uploads/2026/07/piso-venta-calle-oriana-19-madrid-768x512.webp"),
])
def test_captcha_in_an_http_200_stops_the_origin(blocked_url, skipped_url):
    report, requests, _ = run_documents(overrides={
        blocked_url: (200, b"<html><body>Please complete the CAPTCHA</body></html>")
    })
    results = by_url(report)
    assert results[blocked_url]["status"] == "blocked"
    assert results[blocked_url]["reason"] == "captcha"
    assert results[skipped_url]["status"] == "skipped"
    assert skipped_url not in [str(request.url) for request in requests]


def test_body_bounds_and_hash_cover_only_the_read_prefix():
    documents = public_documents()
    robots = b"a" * (probe.TEXT_BODY_LIMIT + 4096)
    documents[probe.ROBOTS_URL] = ("text/plain", robots)
    report, _, streams = run_documents(documents)
    results = by_url(report)
    for url, limit in ((probe.ROBOTS_URL, 1_048_576), (probe.IMAGE_URL, 1024)):
        result = results[url]
        assert result["status"] == "partial"
        assert result["bytes_read"] == limit
        assert result["sha256"] == hashlib.sha256(documents[url][1][:limit]).hexdigest()
        assert result["body_complete"] is False
        assert streams[url].bytes_yielded == limit
        assert streams[url].closed is True
    assert results[probe.IMAGE_URL]["observations"] == {"format_prefix": "webp"}


def test_invalid_json_http_200_is_distinct_from_a_transport_failure():
    report, _, _ = run_documents(overrides={probe.OPENAPI_URL: (200, b"not JSON")})
    result = by_url(report)[probe.OPENAPI_URL]
    assert result["status"] == "invalid_JSON"
    assert result["http_status"] == 200
    assert result["error_class"] == "JSONDecodeError"
    assert result["observations"] == {}
    assert by_url(report)[probe.LOCATIONS_URL]["http_status"] == 200


def test_valid_json_documentation_mentioning_captcha_is_not_a_challenge():
    documents = public_documents()
    metadata = json.loads(documents[probe.OPENAPI_URL][1])
    metadata["info"] = {"description": "This documentation mentions CAPTCHA."}
    documents[probe.OPENAPI_URL] = ("application/json", json.dumps(metadata).encode())
    report, requests, _ = run_documents(documents)
    assert by_url(report)[probe.OPENAPI_URL]["status"] == "observed"
    assert by_url(report)[probe.LOCATIONS_URL]["status"] == "observed"
    assert len(requests) == 5


def test_metadata_date_and_counts_do_not_assert_listing_currency():
    report, _, _ = run_documents()
    results = by_url(report)
    assert results[probe.ROBOTS_URL]["observations"] == {
        "declares_sitemaps": True, "disallow_rss": True
    }
    assert results[probe.OPENAPI_URL]["observations"] == {
        "rapidapi_key_scheme": True, "rapidapi_key_required": True
    }
    assert results[probe.LOCATIONS_URL]["observations"] == {
        "generatedAt": "2026-07-22", "rent_segments": 1, "sale_segments": 2
    }


def test_robots_without_sitemap_and_openapi_without_required_security():
    documents = public_documents()
    documents[probe.ROBOTS_URL] = ("text/plain", b"# Sitemap: ignored\nDisallow: /rss/\nDisallow: /rss/\n")
    documents[probe.OPENAPI_URL] = ("application/json", b'{"components":{"securitySchemes":{"RapidAPIKey":{"type":"apiKey","in":"header","name":"X-RapidAPI-Key"}}}}')
    report, _, _ = run_documents(documents)
    assert by_url(report)[probe.ROBOTS_URL]["observations"] == {
        "declares_sitemaps": False, "disallow_rss": True
    }
    assert by_url(report)[probe.OPENAPI_URL]["observations"] == {
        "rapidapi_key_scheme": True, "rapidapi_key_required": False
    }


@pytest.mark.parametrize("link", [
    "https://www.idealista.com/pro/de-la-orden-gestion-inmobiliaria/inmueble/112062204/",
    "https://www.idealista.com/inmueble/112062204/",
])
def test_agency_only_observes_exact_published_links_and_thumbnail(link):
    documents = public_documents()
    documents[probe.AGENCY_URL] = ("text/html", (
        f'<a href="{link}">123 EUR</a><img srcset="{probe.IMAGE_URL} 768w">'
    ).encode())
    report, _, _ = run_documents(documents)
    assert by_url(report)[probe.AGENCY_URL]["observations"] == {
        "idealista_112062204_link": True, "published_thumbnail": True
    }


def test_link_substrings_titles_and_script_literals_do_not_count_as_links():
    documents = public_documents()
    documents[probe.AGENCY_URL] = ("text/html", (
        f'<a href="{probe.IDEALISTA_AGENCY_LINK}extra">112062204</a>'
        f'<script>"{probe.IDEALISTA_AGENCY_LINK}"; captcha_library = true</script>'
        f'<p>{probe.IMAGE_URL}</p><img title="{probe.IMAGE_URL}">'
    ).encode())
    report, _, _ = run_documents(documents)
    result = by_url(report)[probe.AGENCY_URL]
    assert result["status"] == "observed"
    assert result["observations"] == {
        "idealista_112062204_link": False, "published_thumbnail": False
    }


def test_redirect_is_reported_without_following_or_saving_location():
    report, requests, _ = run_documents(overrides={probe.OPENAPI_URL: (302, b"")})
    result = by_url(report)[probe.OPENAPI_URL]
    assert result["status"] == "redirect"
    assert result["http_status"] == 302
    assert len(requests) == 5
    assert "private.example" not in json.dumps(report)


def test_transport_error_does_not_save_the_sensitive_exception_message():
    def handler(request):
        raise httpx.ConnectError("https://user:key@private.example/?token=secret", request=request)

    report = asyncio.run(probe.run_probe(transport=httpx.MockTransport(handler)))
    assert report["diagnostic_complete"] is True
    assert all(result["status"] == "transport_error" for result in report["results"])
    assert all(result["error_class"] == "ConnectError" for result in report["results"])
    assert all(result["http_status"] is None for result in report["results"])
    assert "secret" not in json.dumps(report)


def test_request_deadline_covers_a_slow_response_body(monkeypatch):
    monkeypatch.setattr(probe, "REQUEST_TIMEOUT_SECONDS", 0.01)

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first bytes"
            await asyncio.sleep(0.1)
            yield b"late bytes"

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, stream=SlowStream())

    report = asyncio.run(probe.run_probe(transport=httpx.MockTransport(handler)))
    assert len(requests) == 5
    assert all(result["status"] == "transport_error" for result in report["results"])
    assert all(result["error_class"] == "TimeoutError" for result in report["results"])
    assert all(result["http_status"] == 200 for result in report["results"])


def test_expired_global_deadline_skips_remaining_resources_without_http(monkeypatch):
    ticks = iter([100.0, 100.0, 146.0, 146.0, 146.0, 146.0])
    monkeypatch.setattr(probe, "monotonic", lambda: next(ticks))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(403, stream=CountingStream(b""))

    report = asyncio.run(probe.run_probe(transport=httpx.MockTransport(handler)))
    assert len(requests) == 1
    assert report["status"] == "partial"
    assert report["diagnostic_complete"] is False
    assert all(result["status"] == "skipped" for result in report["results"][1:])
    assert all(result["reason"] == "global_deadline" for result in report["results"][1:])
    assert all(result["http_status"] is None for result in report["results"][1:])


def test_report_write_replaces_atomically_and_cli_zero_means_diagnostic_complete(monkeypatch, tmp_path):
    output = tmp_path / "report.json"
    output.write_text('{"old":true}', encoding="utf-8")
    report = {"status": "complete", "diagnostic_complete": True, "results": [{"status": "blocked"}]}
    original_replace = probe.os.replace
    replacements = []

    def replace(source, destination):
        assert json.loads(Path(source).read_text(encoding="utf-8")) == report
        assert json.loads(output.read_text(encoding="utf-8")) == {"old": True}
        replacements.append((source, destination))
        original_replace(source, destination)

    async def fake_probe():
        return report

    monkeypatch.setattr(probe.os, "replace", replace)
    monkeypatch.setattr(probe, "run_probe", fake_probe)
    monkeypatch.setattr(sys, "argv", ["probe", "--output", str(output)])
    assert probe.main() == 0
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert len(replacements) == 1
    assert list(tmp_path.iterdir()) == [output]


def test_incomplete_cli_report_uses_nonzero_exit(monkeypatch, tmp_path):
    async def fake_probe():
        return {"status": "partial", "diagnostic_complete": False, "results": []}

    monkeypatch.setattr(probe, "run_probe", fake_probe)
    monkeypatch.setattr(sys, "argv", ["probe", "--output", str(tmp_path / "report.json")])
    assert probe.main() == 1
