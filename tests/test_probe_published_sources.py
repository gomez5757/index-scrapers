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
    "directory_source_probe", ROOT / "scripts/probe_published_sources.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

DIRECTORY_URLS = [
    "https://www.idealista.com/long-term-rentals-in-spain",
    "https://www.idealista.com/properties-for-sale-in-spain",
]
DEFAULT_URLS = [
    "https://www.idealista.com/robots.txt",
    "https://apidea.es/openapi.json",
    "https://apidea.es/locations-es-index.json",
    "https://delaordeninmobiliaria.es/inmuebles/",
    "https://delaordeninmobiliaria.es/wp-content/uploads/2026/07/"
    "piso-venta-calle-oriana-19-madrid-768x512.webp",
]


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body
        self.bytes_yielded = 0
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.body), 256):
            chunk = self.body[offset:offset + 256]
            self.bytes_yielded += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


def directory_probe(handler):
    return asyncio.run(probe.run_probe(
        source_set="idealista_directories", transport=httpx.MockTransport(handler)
    ))


def test_directory_selection_gets_only_the_two_published_urls_once(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://secret:password@proxy.invalid")
    monkeypatch.setenv("X_RAPIDAPI_KEY", "never-send-this")
    original_client = httpx.AsyncClient
    options = []
    requests = []
    body = b'<html><body><a href="/inmueble/999999/">Private listing 777 EUR</a></body></html>'

    def client(**kwargs):
        options.append(kwargs)
        return original_client(**kwargs)

    def handler(request):
        requests.append(request)
        assert str(request.url) in DIRECTORY_URLS
        return httpx.Response(200, headers={
            "Content-Type": "text/html; private-token=do-not-record",
            "Set-Cookie": "session=do-not-send; Path=/",
        }, stream=CountingStream(body))

    monkeypatch.setattr(probe.httpx, "AsyncClient", client)
    report = directory_probe(handler)
    assert [str(request.url) for request in requests] == DIRECTORY_URLS
    assert [result["url"] for result in report["results"]] == DIRECTORY_URLS
    assert report["diagnostic_complete"] is True
    assert all(result["status"] == "observed" for result in report["results"])
    assert all(result["observations"] == {} for result in report["results"])
    assert all(result["sha256"] == hashlib.sha256(body).hexdigest() for result in report["results"])
    assert all(option["trust_env"] is False and option["follow_redirects"] is False for option in options)
    for request in requests:
        assert request.method == "GET"
        assert request.headers["user-agent"].startswith("python-httpx/")
        assert request.headers["accept-encoding"] == "identity"
        assert set(request.headers) <= {"host", "accept", "accept-encoding", "connection", "user-agent"}
        assert all(0 < timeout <= 10 for timeout in request.extensions["timeout"].values())
    saved = json.dumps(report)
    for private in ("999999", "777", "Private listing", "do-not-record", "do-not-send", "never-send-this"):
        assert private not in saved


@pytest.mark.parametrize("source_set,expected_urls", [(None, DEFAULT_URLS), ("idealista_directories", DIRECTORY_URLS)])
def test_cli_selection_and_unchanged_default_write_only_the_selected_report(monkeypatch, tmp_path,
                                                                         source_set, expected_urls):
    requests = []
    real_probe = probe.run_probe

    def handler(request):
        requests.append(str(request.url))
        body = b"RIFF\x10\x00\x00\x00WEBP" if str(request.url) == DEFAULT_URLS[-1] else b"{}"
        return httpx.Response(200, stream=CountingStream(body))

    async def mock_probe(**kwargs):
        return await real_probe(transport=httpx.MockTransport(handler), **kwargs)

    output = tmp_path / "report.json"
    argv = ["probe", "--output", str(output)]
    if source_set is not None:
        argv.extend(["--source-set", source_set])
    monkeypatch.setattr(probe, "run_probe", mock_probe)
    monkeypatch.setattr(sys, "argv", argv)
    assert probe.main() == 0
    assert requests == expected_urls
    assert [result["url"] for result in json.loads(output.read_text(encoding="utf-8"))["results"]] == expected_urls


def test_cli_rejects_arbitrary_url_without_running_the_probe(monkeypatch, tmp_path):
    def unexpected_probe(**kwargs):
        pytest.fail("An invalid selection must not start any requests")

    monkeypatch.setattr(probe, "run_probe", unexpected_probe)
    monkeypatch.setattr(sys, "argv", ["probe", "--output", str(tmp_path / "report.json"),
                                     "--source-set", "https://private.example/"])
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 2
    assert not (tmp_path / "report.json").exists()


def test_programmatic_unknown_selection_rejects_before_any_request():
    def handler(request):
        pytest.fail("An unknown selection must not issue a request")

    with pytest.raises(ValueError, match="Unknown published source set"):
        asyncio.run(probe.run_probe(source_set="https://private.example/",
                                    transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("status,body,reason", [
    (403, b"private denied response", "http_403"),
    (429, b"private rate limit response", "http_429"),
    (200, b"<html><body>Please complete the CAPTCHA</body></html>", "captcha"),
])
def test_blocked_first_directory_stops_the_origin_without_reading_denied_body(status, body, reason):
    requests = []
    stream = CountingStream(body)

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(status, headers={"Content-Type": "text/html"}, stream=stream)

    report = directory_probe(handler)
    assert requests == DIRECTORY_URLS[:1]
    first, second = report["results"]
    assert first["status"] == "blocked"
    assert first["reason"] == reason
    assert first["http_status"] == status
    assert first["observations"] == {}
    if status in {403, 429}:
        assert stream.bytes_yielded == first["bytes_read"] == 0
    assert stream.closed is True
    assert second["status"] == "skipped"
    assert second["reason"] == "origin_blocked"
    assert second["http_status"] is None
    assert report["diagnostic_complete"] is True
    assert "private" not in json.dumps(report)


def test_directory_body_limit_keeps_only_a_one_mib_hash_without_html():
    body = b"<html><body>Private listing " + b"x" * 1_048_576
    streams = []

    def handler(request):
        stream = CountingStream(body)
        streams.append(stream)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, stream=stream)

    report = directory_probe(handler)
    assert len(streams) == 2
    for result, stream in zip(report["results"], streams):
        assert result["status"] == "partial"
        assert result["reason"] == "body_limit"
        assert result["bytes_read"] == stream.bytes_yielded == 1_048_576
        assert result["sha256"] == hashlib.sha256(body[:1_048_576]).hexdigest()
        assert result["body_complete"] is False
        assert result["observations"] == {}
        assert stream.closed is True
    assert "Private listing" not in json.dumps(report)


def test_global_45_second_deadline_skips_the_second_directory(monkeypatch):
    ticks = iter([100.0, 100.0, 146.0])
    monkeypatch.setattr(probe, "monotonic", lambda: next(ticks))
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={"Content-Type": "text/html"}, stream=CountingStream(b"<html/>"))

    report = directory_probe(handler)
    assert requests == DIRECTORY_URLS[:1]
    assert report["status"] == "partial"
    assert report["diagnostic_complete"] is False
    assert report["results"][1]["reason"] == "global_deadline"
    assert report["results"][1]["http_status"] is None


def test_directory_request_deadline_bounds_a_slow_body_without_retry(monkeypatch):
    monkeypatch.setattr(probe, "REQUEST_TIMEOUT_SECONDS", 0.01)
    requests = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.1)
            yield b"late private HTML"

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={"Content-Type": "text/html"}, stream=SlowStream())

    report = directory_probe(handler)
    assert requests == DIRECTORY_URLS
    assert all(result["status"] == "transport_error" for result in report["results"])
    assert all(result["reason"] == "request_timeout" for result in report["results"])
    assert all(result["bytes_read"] == 0 for result in report["results"])
