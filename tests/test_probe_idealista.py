import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("probe_idealista", ROOT / "scripts/probe_idealista.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_public_listing_page_uses_existing_parser():
    html = (ROOT / "tests/fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
    result = probe.classify_page(html, probe.TARGET_URL, 200, "Alquiler en Madrid")
    assert result["status"] == "accessible"
    assert result["listing_count"] == 1


def test_real_http_challenge_is_blocked_not_an_empty_catalogue():
    html = (ROOT / "tests/fixtures/captures/idealista.http-403.html").read_text(encoding="utf-8")
    result = probe.classify_page(html, probe.TARGET_URL, 403, "idealista")
    assert result["status"] == "blocked"
    assert result["reason"] == "http_403"
    assert result["listing_count"] is None


@pytest.mark.parametrize("http_status", [401, 429])
def test_authentication_and_rate_limit_are_blocked(http_status):
    result = probe.classify_page("<body>Unavailable</body>", probe.TARGET_URL, http_status, "idealista")
    assert result["status"] == "blocked"
    assert result["reason"] == f"http_{http_status}"


@pytest.mark.parametrize("flag,reason", [("visible_challenge", "visible_captcha"), ("visible_login", "authentication_required")])
def test_visible_intervention_stops_even_if_listings_are_present(flag, reason):
    html = (ROOT / "tests/fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
    result = probe.classify_page(html, probe.TARGET_URL, 200, "idealista", **{flag: True})
    assert result["status"] == "blocked"
    assert result["reason"] == reason


def test_a_normal_login_link_does_not_block_a_public_listing_page():
    html = (ROOT / "tests/fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
    html = html.replace("<body>", '<body><a href="/login">Iniciar sesión</a>')
    assert "Iniciar sesión" in html
    result = probe.classify_page(html, probe.TARGET_URL, 200, "Alquiler en Madrid")
    assert result["status"] == "accessible"


def test_challenge_with_http_200_is_blocked():
    result = probe.classify_page("<body>Please enable JS and disable any ad blocker</body>", probe.TARGET_URL, 200, "idealista")
    assert result["status"] == "blocked"
    assert result["listing_count"] is None


@pytest.mark.parametrize("url,reason", [
    ("https://www.idealista.com/login?token=temporary", "authentication_redirect"),
    ("https://other.example/", "unexpected_redirect"),
    ("https://www.idealista.com/venta-viviendas/madrid-madrid/", "unexpected_redirect"),
])
def test_redirect_does_not_prove_the_requested_public_channel(url, reason):
    result = probe.classify_page("<body>Other page</body>", url, 200, "Other")
    assert result["status"] == "blocked"
    assert result["reason"] == reason
    assert "token=" not in result["final_url"]


def test_zero_parsed_listings_is_inconclusive():
    result = probe.classify_page("<body>Madrid</body>", probe.TARGET_URL, 200, "Alquiler en Madrid")
    assert result["status"] == "inconclusive"
    assert result["listing_count"] == 0


def test_report_url_drops_credentials_and_query_tokens():
    assert probe.public_url("https://user:secret@www.idealista.com/login?token=secret#fragment") == "https://www.idealista.com/login"


def test_driver_failure_still_produces_a_report_without_raw_error(monkeypatch):
    async def fail(playwright, report):
        raise RuntimeError("secret?token=do-not-log")

    class FakeManager:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    fake_api = ModuleType("playwright.async_api")
    fake_api.async_playwright = FakeManager
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)
    monkeypatch.setattr(probe, "observe", fail)
    result = asyncio.run(probe.run_probe())
    assert result["status"] == "error"
    assert result["reason"] == "runtime_error"
    assert result["error_type"] == "RuntimeError"
    assert "do-not-log" not in str(result)


def test_browser_uses_a_clean_context_one_navigation_and_stops_on_block(monkeypatch):
    fake_api = ModuleType("playwright.async_api")
    fake_api.TimeoutError = type("FakeTimeoutError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)
    calls = []

    class EmptyLocator:
        async def count(self):
            return 0

    class Page:
        url = probe.TARGET_URL

        async def goto(self, url, **kwargs):
            calls.append(("goto", url, kwargs))
            return type("Response", (), {"status": 403})()

        async def content(self):
            return "<body>Please enable JS and disable any ad blocker</body>"

        async def title(self):
            return "idealista"

        def locator(self, selector):
            return EmptyLocator()

        async def wait_for_selector(self, *args, **kwargs):
            raise AssertionError("A blocked page must not wait or try again")

    class Context:
        async def new_page(self):
            return Page()

    class Browser:
        async def new_context(self, *args, **kwargs):
            assert args == () and kwargs == {}
            calls.append(("new_context",))
            return Context()

        async def close(self):
            calls.append(("close",))

    class Chromium:
        async def launch(self, **kwargs):
            assert kwargs == {"headless": True, "timeout": 10000}
            return Browser()

    playwright = type("Playwright", (), {"chromium": Chromium()})()
    report = {"http_status": None}
    asyncio.run(probe.observe(playwright, report))
    assert report["status"] == "blocked"
    assert [call[0] for call in calls] == ["new_context", "goto", "close"]
    assert calls[1][1] == probe.TARGET_URL
