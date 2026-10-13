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


@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize("via_home", [True, False])
@pytest.mark.parametrize("browser", ["chromium", "firefox"])
def test_driver_failure_still_produces_a_report_without_raw_error(monkeypatch, headless, via_home, browser):
    async def fail(playwright, report, *, headless, via_home, browser):
        assert browser == report["browser"]
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
    result = asyncio.run(probe.run_probe(headless=headless, via_home=via_home, browser=browser))
    assert result["status"] == "error"
    assert result["reason"] == "runtime_error"
    assert result["error_type"] == "RuntimeError"
    assert result["headless"] is headless
    assert result["browser"] == browser
    assert result["navigation_mode"] == ("via_home" if via_home else "direct")
    assert "do-not-log" not in str(result)


@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize("browser", ["chromium", "firefox"])
def test_browser_uses_a_clean_context_one_navigation_and_stops_on_block(monkeypatch, headless, browser):
    fake_api = ModuleType("playwright.async_api")
    fake_api.TimeoutError = type("FakeTimeoutError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)
    calls = []
    selected_engines = []

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
        def __init__(self, name):
            self.name = name

        async def launch(self, **kwargs):
            assert kwargs == {"headless": headless, "timeout": 10000}
            selected_engines.append(self.name)
            return Browser()

    playwright = type("Playwright", (), {"chromium": Chromium("chromium"), "firefox": Chromium("firefox")})()
    report = {"http_status": None}
    asyncio.run(probe.observe(playwright, report, headless=headless, browser=browser))
    assert report["status"] == "blocked"
    assert [call[0] for call in calls] == ["new_context", "goto", "close"]
    assert calls[1][1] == probe.TARGET_URL
    assert selected_engines == [browser]


@pytest.mark.parametrize("browser", [None, "firefox"])
def test_cli_propagates_browser_engine_headed_and_navigation_mode(monkeypatch, tmp_path, browser):
    observed = []

    async def fake_probe(**kwargs):
        observed.append(kwargs)
        return {"status": "blocked", "browser": kwargs["browser"]}

    output = tmp_path / "report.json"
    argv = ["probe", "--headed", "--via-home", "--output", str(output)]
    if browser:
        argv.extend(["--browser", browser])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(probe, "run_probe", fake_probe)
    assert probe.main() == 0
    assert observed == [{"headless": False, "via_home": True, "browser": browser or "chromium"}]


def test_cli_rejects_an_unavailable_browser_engine_before_starting_runtime(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["probe", "--browser", "webkit"])
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 2


@pytest.mark.parametrize("href,expected", [
    ("/alquiler-viviendas/?campaign=public#section", probe.RENT_DIRECTORY_URL),
    ("madrid-madrid/?campaign=public", probe.TARGET_URL),
    ("https://other.example/alquiler-viviendas/", None),
    ("https://user:secret@www.idealista.com/alquiler-viviendas/", None),
    ("javascript:alert(1)", None),
    (None, None),
])
def test_published_link_normalization_rejects_external_or_authenticated_urls(href, expected):
    assert probe.navigation_url(href, probe.RENT_DIRECTORY_URL) == expected


def fake_route(monkeypatch, documents):
    fake_api = ModuleType("playwright.async_api")
    fake_api.TimeoutError = type("FakeTimeoutError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)
    calls = []

    class Collection:
        def __init__(self, items=()):
            self.items = list(items)

        async def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class ConsentButton:
        async def inner_text(self):
            return page.document["consent_button"]

        async def is_visible(self):
            return True

        async def click(self, **kwargs):
            calls.append(("consent_click", page.document["consent_button"]))
            page.document["consent"] = False

    class ConsentDialog:
        async def is_visible(self):
            return True

        async def inner_text(self):
            return "Consentimiento para cookies"

        def get_by_role(self, role):
            assert role == "button"
            return Collection([ConsentButton()] if page.document.get("consent_button") else [])

    class Anchor:
        def __init__(self, href):
            self.href = href

        async def get_attribute(self, name):
            assert name == "href"
            return self.href

        async def is_visible(self):
            return True

        async def click(self, **kwargs):
            calls.append(("click", self.href))
            expected = probe.navigation_url(self.href, page.url)
            page.index += 1
            assert expected.rstrip("/") == probe.public_url(page.url).rstrip("/")
            page.emit_response()

    class Page:
        index = 0
        main_frame = object()
        response_callback = None

        @property
        def document(self):
            return documents[self.index]

        @property
        def url(self):
            return self.document["url"]

        def on(self, event, callback):
            assert event == "response"
            self.response_callback = callback

        def emit_response(self):
            request = type("Request", (), {"is_navigation_request": lambda self: True})()
            response = type("Response", (), {"status": self.document.get("http_status", 200), "frame": self.main_frame, "request": request})()
            self.response_callback(response)
            return response

        async def goto(self, url, **kwargs):
            assert url == probe.HOME_URL
            calls.append(("goto", url))
            return self.emit_response()

        async def content(self):
            return self.document.get("html", "<body>Public rental directory</body>")

        async def title(self):
            return "idealista"

        def locator(self, selector):
            if selector == "a[href]":
                return Collection(Anchor(href) for href in self.document.get("links", []))
            if selector.startswith('[role="dialog"]') and self.document.get("consent"):
                return Collection([ConsentDialog()])
            return Collection()

        async def wait_for_url(self, predicate, **kwargs):
            assert kwargs == {"wait_until": "domcontentloaded", "timeout": 15000}
            assert predicate(self.url)

        async def wait_for_selector(self, selector, **kwargs):
            assert selector == probe.READY_SELECTOR
            assert kwargs == {"state": "visible", "timeout": 10000}
            calls.append(("readiness",))
            self.document["html"] = self.document["ready_html"]

    page = Page()

    class Context:
        async def new_page(self):
            return page

    class Browser:
        async def new_context(self, *args, **kwargs):
            assert args == () and kwargs == {}
            return Context()

        async def close(self):
            calls.append(("close",))

    class Chromium:
        async def launch(self, **kwargs):
            assert kwargs == {"headless": False, "timeout": 10000}
            return Browser()

    playwright = type("Playwright", (), {"chromium": Chromium()})()
    report = {"http_status": None, "trace": []}
    return playwright, report, calls


@pytest.mark.parametrize("directory", [True, False])
def test_via_home_follows_two_or_three_published_documents_and_only_parses_target(monkeypatch, directory):
    target_html = (ROOT / "tests/fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
    documents = [{"url": probe.HOME_URL, "links": ["/alquiler-viviendas/" if directory else "/alquiler-viviendas/madrid-madrid/?campaign=public"]}]
    if directory:
        documents.append({"url": probe.RENT_DIRECTORY_URL, "links": ["madrid-madrid/?campaign=public"]})
    documents.append({"url": probe.TARGET_URL + "?campaign=public", "html": target_html})
    parser = probe.parse
    parsed = []

    def parse_target(portal, html, context):
        assert html == target_html
        parsed.append(html)
        return parser(portal, html, context)

    monkeypatch.setattr(probe, "parse", parse_target)
    playwright, report, calls = fake_route(monkeypatch, documents)
    asyncio.run(probe.observe(playwright, report, headless=False, via_home=True))
    assert report["status"] == "accessible" and report["listing_count"] == 1
    assert len(parsed) == 1
    assert [entry["step"] for entry in report["trace"]] == (["home", "intermediate", "target"] if directory else ["home", "target"])
    assert all(entry["http_status"] == 200 and "?" not in entry["url"] for entry in report["trace"])
    assert [call for call in calls if call[0] == "goto"] == [("goto", probe.HOME_URL)]
    assert len([call for call in calls if call[0] == "click"]) == len(documents) - 1


def test_via_home_waits_for_target_readiness_and_updates_trace(monkeypatch):
    target_html = (ROOT / "tests/fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
    documents = [
        {"url": probe.HOME_URL, "links": ["/alquiler-viviendas/madrid-madrid/"]},
        {"url": probe.TARGET_URL, "html": "<body>Loading listings</body>", "ready_html": target_html},
    ]
    playwright, report, calls = fake_route(monkeypatch, documents)
    asyncio.run(probe.observe(playwright, report, headless=False, via_home=True))
    assert report["status"] == "accessible" and report["listing_count"] == 1
    assert [call for call in calls if call[0] == "readiness"] == [("readiness",)]
    assert report["trace"][-1] == {"step": "target", "url": probe.TARGET_URL, "http_status": 200, "status": "accessible", "reason": "parsed_listings"}


@pytest.mark.parametrize("blocked_document", [0, 1])
@pytest.mark.parametrize("http_status", [200, 403])
def test_via_home_never_clicks_after_a_challenge(monkeypatch, blocked_document, http_status):
    documents = [
        {"url": probe.HOME_URL, "links": ["/alquiler-viviendas/"]},
        {"url": probe.RENT_DIRECTORY_URL, "links": ["madrid-madrid/"]},
    ]
    documents[blocked_document].update(http_status=http_status, html="<body>Please enable JS and disable any ad blocker</body>")
    monkeypatch.setattr(probe, "parse", lambda *args: pytest.fail("A challenge or directory must not parse listings"))
    playwright, report, calls = fake_route(monkeypatch, documents)
    asyncio.run(probe.observe(playwright, report, headless=False, via_home=True))
    assert report["status"] == "blocked"
    assert len(report["trace"]) == blocked_document + 1
    assert len([call for call in calls if call[0] == "click"]) == blocked_document
    assert calls[-1] == ("close",)


def test_via_home_reports_missing_link_and_sanitized_observed_rental_paths(monkeypatch):
    documents = [{"url": probe.HOME_URL, "links": ["/alquiler-viviendas/madrid-provincia/?token=transient", "https://other.example/alquiler-viviendas/"]}]
    playwright, report, calls = fake_route(monkeypatch, documents)
    asyncio.run(probe.observe(playwright, report, headless=False, via_home=True))
    assert report["status"] == "inconclusive" and report["reason"] == "link_missing"
    assert report["listing_count"] is None
    assert report["observed_rental_paths"] == ["https://www.idealista.com/alquiler-viviendas/madrid-provincia/"]
    assert not [call for call in calls if call[0] == "click"]


def test_unknown_consent_requires_intervention_without_forcing_buttons(monkeypatch):
    documents = [{"url": probe.HOME_URL, "links": ["/alquiler-viviendas/"], "consent": True, "consent_button": "Aceptar todas"}]
    playwright, report, calls = fake_route(monkeypatch, documents)
    asyncio.run(probe.observe(playwright, report, headless=False, via_home=True))
    assert report["status"] == "blocked" and report["reason"] == "consent_required"
    assert not [call for call in calls if call[0] in {"click", "consent_click"}]


def test_optional_consent_is_rejected_only_by_an_observed_public_button(monkeypatch):
    target_html = (ROOT / "tests/fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
    documents = [
        {"url": probe.HOME_URL, "links": ["/alquiler-viviendas/madrid-madrid/"], "consent": True, "consent_button": "Rechazar todas"},
        {"url": probe.TARGET_URL, "html": target_html},
    ]
    playwright, report, calls = fake_route(monkeypatch, documents)
    asyncio.run(probe.observe(playwright, report, headless=False, via_home=True))
    assert report["status"] == "accessible"
    assert report["consent_actions"] == ["rejected_optional"]
    assert [call for call in calls if call[0] == "consent_click"] == [("consent_click", "Rechazar todas")]
