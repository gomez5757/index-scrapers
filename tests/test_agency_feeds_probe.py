import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "probe_agency_feeds", ROOT / "scripts/probe_agency_feeds.py"
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


def agencies(count=2):
    return tuple({"id": f"agency-{number}", "source_url": f"https://agency{number}.example/",
                  "primary_reference_url": f"https://agency{number}.example/"}
                 for number in range(count))


def documents(targets):
    content = {}
    for agency in targets:
        source = agency["source_url"]
        content[source] = (200, "text/html", b'<link rel="alternate" type="application/rss+xml" href="/feed/">')
        content[source + "feed/"] = (200, "application/rss+xml", b'<rss><channel><item><title>Private author and address</title><link>https://agency0.example/news/</link><pubDate>Mon, 12 Oct 2026 10:00:00 +0000</pubDate><description>&lt;img src="https://agency0.example/photo.jpg"&gt;</description></item></channel></rss>')
    return content


def run_documents(targets=None, content=None, header_overrides=None):
    targets = agencies() if targets is None else targets
    content = documents(targets) if content is None else content
    requests, streams = [], {}

    def handler(request):
        url = str(request.url)
        requests.append(request)
        status, media_type, body = content[url]
        streams[url] = CountingStream(body)
        headers = {"Content-Type": media_type + "; private-token=never-save",
                   "Set-Cookie": "identity=never-send; Path=/"}
        headers.update((header_overrides or {}).get(url, {}))
        return httpx.Response(status, headers=headers, stream=streams[url])

    report = asyncio.run(probe.run_probe(agencies=targets, transport=httpx.MockTransport(handler)))
    return report, requests, streams


def test_only_once_gets_the_advertised_resource_without_cookies_identity_or_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://secret:password@private.example")
    options = []
    client_type = httpx.AsyncClient

    def client(**kwargs):
        options.append(kwargs)
        return client_type(**kwargs)

    monkeypatch.setattr(probe.httpx, "AsyncClient", client)
    report, requests, _ = run_documents()
    assert report["diagnostic_complete"] is True
    assert report["ingested"] is False
    assert len(requests) == len({str(request.url) for request in requests}) == 4
    for request in requests:
        assert request.method == "GET"
        assert request.headers["accept-encoding"] == "identity"
        assert set(request.headers) <= {"host", "accept", "accept-encoding", "connection", "user-agent"}
        assert all(0 < timeout <= 10 for timeout in request.extensions["timeout"].values())
    assert all(option["trust_env"] is False and option["follow_redirects"] is False for option in options)
    saved = json.dumps(report)
    for private in ("Private author", "never-save", "never-send", "photo.jpg", "password"):
        assert private not in saved


@pytest.mark.parametrize("status", [401, 403, 429])
def test_blocked_origin_is_not_requested_again(status):
    targets = agencies(1)
    content = documents(targets)
    content[targets[0]["source_url"]] = (status, "text/html", b"private challenge")
    report, requests, streams = run_documents(targets, content)
    result = report["results"][0]
    assert len(requests) == 1
    assert streams[targets[0]["source_url"]].bytes_yielded == 0
    assert result["discovery"]["status"] == "blocked"
    assert result["resource"] is None


@pytest.mark.parametrize("body,reason", [
    (b"<p>Please complete the CAPTCHA</p>", "captcha"),
    (b'<form action="/wp-login.php"><input type="password"></form>', "login"),
])
def test_http_200_challenge_or_login_stops_before_feed(body, reason):
    targets = agencies(1)
    content = documents(targets)
    content[targets[0]["source_url"]] = (200, "text/html", body)
    report, requests, _ = run_documents(targets, content)
    assert len(requests) == 1
    assert report["results"][0]["discovery"]["reason"] == reason


def test_feed_must_be_explicit_and_safe_not_in_scripts_comments_or_cross_origin():
    source = agencies(1)[0]["source_url"]
    content = {source: (200, "text/html", b'<script>"/feed/"</script><!-- <link rel="alternate" type="application/rss+xml" href="/feed/"> --><link rel="alternate" type="application/rss+xml" href="https://private.example/feed/"><link rel="alternate" type="application/rss+xml" href="/comments/feed/"><link rel="https://api.w.org/" href="/wp-json/wp/v2/users"><a href="/feed/">Ordinary page</a><link rel="alternate" type="application/rss+xml" href="/feed/?token=secret">')}
    report, requests, _ = run_documents(agencies(1), content)
    assert len(requests) == 1
    assert report["results"][0]["outcome"] == "no_advertised_resource"
    assert "private.example" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_html_and_http_link_header_advertisements_use_the_exact_url():
    source = agencies(1)[0]["source_url"]
    content = {source: (200, "text/html", b'<p>Agency</p>'),
               source + "catalogue.xml": (200, "application/rss+xml", b"<rss><channel/></rss>")}
    report, requests, _ = run_documents(agencies(1), content, {
        source: {"Link": '<' + source + 'catalogue.xml>; rel="alternate"; type="application/rss+xml"'}
    })
    assert [str(request.url) for request in requests] == [source, source + "catalogue.xml"]
    assert report["results"][0]["resource"]["advertisement"]["method"] == "http_link"


def test_rss_entries_are_not_claimed_as_property_listings_and_dates_are_metadata():
    report, _, _ = run_documents()
    summary = report["results"][0]["resource"]["observations"]
    assert summary["format"] == "rss"
    assert summary["record_count"] == 1
    assert summary["listing_candidates"] == 0
    assert summary["field_counts"]["title"] == 1
    assert summary["field_counts"]["price"] == 0
    assert summary["field_counts"]["photo_links"] == 1
    assert summary["publication_date_range"] == ["2026-10-12", "2026-10-12"]
    assert summary["dates_are_listing_freshness"] is False


def test_json_feed_counts_fields_but_saves_only_exact_idealista_property_links():
    source = agencies(1)[0]["source_url"]
    exact = "https://www.idealista.com/pro/example-agency/inmueble/112062204/"
    feed = {"version": "https://jsonfeed.org/version/1.1", "items": [{
        "title": "Private name and address", "url": source + "news/", "date_published": "2026-10-12T10:00:00Z",
        "image": source + "private-photo.jpg", "content_html": f'<a href="{exact}">link</a><a href="{exact}extra">false</a><script>"https://www.idealista.com/inmueble/999999/"</script>'
    }]}
    content = {source: (200, "text/html", b'<link rel="alternate" type="application/feed+json" href="/public.json">'),
               source + "public.json": (200, "application/feed+json", json.dumps(feed).encode())}
    report, requests, _ = run_documents(agencies(1), content)
    summary = report["results"][0]["resource"]["observations"]
    assert len(requests) == 2
    assert summary["format"] == "json_feed"
    assert summary["idealista_property_links"] == [exact]
    assert summary["listing_candidates"] == 0
    assert "999999" not in json.dumps(report)
    assert "Private name" not in json.dumps(report)


def test_structured_property_xml_counts_numeric_price_without_saving_address_or_photo_urls():
    body = b'<properties><property><title>Private address</title><price>579000</price><currency>EUR</currency><area>85</area><rooms>2</rooms><photos><photo>https://agency0.example/private.webp</photo></photos><url>https://agency0.example/property/1/</url><updated>2026-10-12</updated></property><property><price>NaN</price></property></properties>'
    summary = probe.summarize_resource(body, "application/xml", "https://agency0.example/catalogue.xml")
    assert summary["record_count"] == 2
    assert summary["listing_candidates"] == 2
    assert summary["field_counts"]["price"] == 1
    assert summary["field_counts"]["area"] == 1
    assert summary["field_counts"]["rooms"] == 1
    assert summary["field_counts"]["photo_links"] == 1
    assert "Private address" not in json.dumps(summary)
    assert "private.webp" not in json.dumps(summary)


def test_restapi_index_is_documentation_only_and_no_child_routes_are_requested():
    source = agencies(1)[0]["source_url"]
    content = {source: (200, "text/html", b'<link rel="https://api.w.org/" href="/wp-json/">'),
               source + "wp-json/": (200, "application/json", b'{"name":"Private name","namespaces":["wp/v2"],"routes":{"/wp/v2/users":{},"/wp/v2/properties":{}},"authentication":{"private":"secret"}}')}
    report, requests, _ = run_documents(agencies(1), content)
    summary = report["results"][0]["resource"]["observations"]
    assert len(requests) == 2
    assert summary["format"] == "public_api_index"
    assert summary["route_count"] == 2
    assert summary["record_count"] == 0
    assert report["results"][0]["outcome"] == "documentation_only"
    assert "users" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_redirects_are_not_followed_and_sensitive_location_is_not_saved():
    source = agencies(1)[0]["source_url"]
    report, requests, _ = run_documents(agencies(1), {source: (302, "text/html", b"")}, {
        source: {"Location": "https://private.example/?token=secret"}
    })
    assert len(requests) == 1
    assert report["results"][0]["discovery"]["status"] == "redirect"
    assert "secret" not in json.dumps(report)


def test_read_prefix_body_limit_and_hash(monkeypatch):
    targets = agencies(1)
    source = targets[0]["source_url"]
    body = b"a" * (probe.BODY_LIMIT + 1024)
    report, requests, streams = run_documents(targets, {source: (200, "text/html", body)})
    observation = report["results"][0]["discovery"]
    assert len(requests) == 1
    assert observation["status"] == "partial"
    assert observation["bytes_read"] == probe.BODY_LIMIT
    assert streams[source].bytes_yielded == probe.BODY_LIMIT
    assert streams[source].closed is True
    assert observation["sha256"] == hashlib.sha256(body[:probe.BODY_LIMIT]).hexdigest()


def test_request_timeout_covers_slow_body_and_does_not_retry(monkeypatch):
    monkeypatch.setattr(probe, "REQUEST_TIMEOUT_SECONDS", 0.01)

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"prefix"
            await asyncio.sleep(0.1)
            yield b"late"

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, stream=SlowStream())

    report = asyncio.run(probe.run_probe(agencies=agencies(1), transport=httpx.MockTransport(handler)))
    assert len(requests) == 1
    assert report["results"][0]["discovery"]["reason"] == "request_timeout"
    assert report["results"][0]["discovery"]["error_class"] == "TimeoutError"


def test_global_deadline_request_budget_and_origin_cap(monkeypatch):
    ticks = iter([100.0, 100.0, 161.0, 161.0, 161.0])
    monkeypatch.setattr(probe, "monotonic", lambda: next(ticks))
    targets = agencies(2)
    report, requests, _ = run_documents(targets)
    assert len(requests) == 1
    assert report["diagnostic_complete"] is False
    assert report["status"] == "partial"
    monkeypatch.setattr(probe, "monotonic", lambda: 100.0)
    monkeypatch.setattr(probe, "MAX_REQUESTS", 1)
    report, requests, _ = run_documents(agencies(2))
    assert len(requests) == 1
    assert report["diagnostic_complete"] is False
    monkeypatch.setattr(probe, "MAX_REQUESTS", 10)
    report, requests, _ = run_documents(agencies(6))
    assert len({request.url.host for request in requests}) == 5
    assert len(requests) == 10
    assert report["results"][5]["outcome"] == "origin_limit"


def test_transport_errors_are_sanitized_and_xml_entities_rejected():
    def handler(request):
        raise httpx.ConnectError("https://user:secret@private.example", request=request)

    report = asyncio.run(probe.run_probe(agencies=agencies(1), transport=httpx.MockTransport(handler)))
    assert report["results"][0]["discovery"]["error_class"] == "ConnectError"
    assert "secret" not in json.dumps(report)
    with pytest.raises(ValueError, match="xml_declaration"):
        probe.summarize_resource(b'<!DOCTYPE rss [<!ENTITY x "secret">]><rss/>', "application/xml", "https://agency0.example/feed/")


def test_only_one_feed_is_selected_and_rss_is_preferred_to_api():
    source = agencies(1)[0]["source_url"]
    content = documents(agencies(1))
    content[source] = (200, "text/html", b'<link rel="https://api.w.org/" href="/wp-json/"><link rel="alternate" type="application/rss+xml" href="/feed/"><link rel="alternate" type="application/atom+xml" href="/atom/">')
    report, requests, _ = run_documents(agencies(1), content)
    assert [str(request.url) for request in requests] == [source, source + "feed/"]
    assert report["results"][0]["resource"]["advertisement"]["rel"] == "alternate"


def test_atomic_report_and_zero_exit_mean_completed_diagnosis_even_without_catalogue(monkeypatch, tmp_path):
    report = {"diagnostic_complete": True, "status": "complete", "results": [{"outcome": "blocked"}]}

    async def fake_probe():
        return report

    monkeypatch.setattr(probe, "run_probe", fake_probe)
    output = tmp_path / "report.json"
    assert probe.main(["--output", str(output)]) == 0
    assert json.loads(output.read_text()) == report
    assert list(tmp_path.iterdir()) == [output]
