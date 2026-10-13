"""One public-page browser observation; never collect or deliver a catalogue."""
import argparse
import asyncio
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from index_scrapers.extractors import PortalBlocked, parse


TARGET_URL = "https://www.idealista.com/alquiler-viviendas/madrid-madrid/"
CONTEXT = {"operation": "rent", "province": "28", "municipality": "28079", "city_slug": "madrid"}
TOTAL_TIMEOUT_SECONDS = 60
CHALLENGE_SELECTOR = (
    'iframe[src*="captcha-delivery.com"], iframe[src*="recaptcha"], '
    'iframe[src*="hcaptcha"]'
)
LOGIN_SELECTOR = 'input[type="password"]'
READY_SELECTOR = f"article.item, {CHALLENGE_SELECTOR}, {LOGIN_SELECTOR}"


def public_url(value):
    """Drop credentials, query tokens and fragments from the reported URL."""
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            return None
        port = parts.port
        host = parts.hostname
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            host = f"{host}:{port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))[:1000]
    except ValueError:
        return None


def clean_text(value):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value)).strip()[:300]


def normalized(value):
    return unicodedata.normalize("NFKD", value.casefold()).encode("ascii", "ignore").decode()


def classify_page(html, final_url, http_status, title, *, visible_challenge=False, visible_login=False):
    """Only a successful target page with parsed listings proves this channel."""
    result = {
        "status": "inconclusive",
        "final_url": public_url(final_url),
        "title": clean_text(title),
        "http_status": http_status,
        "reason": "no_parsed_listings",
        "listing_count": None,
    }

    def blocked(reason):
        result.update(status="blocked", reason=reason)
        return result

    if http_status in {401, 403, 429}:
        return blocked(f"http_{http_status}")
    parts = urlsplit(result["final_url"] or "")
    if any(segment in {"login", "signin", "identificacion", "access"} for segment in parts.path.lower().split("/")):
        return blocked("authentication_redirect")
    if parts.scheme != "https" or parts.netloc not in {"www.idealista.com", "idealista.com"} or parts.path.rstrip("/") != urlsplit(TARGET_URL).path.rstrip("/"):
        return blocked("unexpected_redirect")
    if visible_login:
        return blocked("authentication_required")
    if visible_challenge:
        return blocked("visible_captcha")

    tree = HTMLParser(html)
    body_node = tree.css_first("body") or tree.root
    body = normalized(body_node.text(separator=" ", strip=True) if body_node else "")
    heading = normalized(title)
    challenge_terms = (
        "captcha", "verify you are human", "verifica que eres humano",
        "confirma que eres humano", "enable javascript", "enable js",
        "disable any ad blocker", "access denied", "acceso denegado",
        "checking your browser", "unusual traffic", "trafico inusual",
    )
    if any(term in heading for term in challenge_terms) or len(body) < 5000 and any(term in body for term in challenge_terms):
        return blocked("page_challenge")
    try:
        result["listing_count"] = len(parse("idealista", html, CONTEXT))
    except PortalBlocked:
        return blocked("parser_challenge")
    if http_status is None or not 200 <= http_status < 300:
        result.update(status="error", reason="no_http_response" if http_status is None else f"http_{http_status}")
    elif result["listing_count"]:
        result.update(status="accessible", reason="parsed_listings")
    return result


async def any_visible(page, selector):
    matches = page.locator(selector)
    for index in range(min(await matches.count(), 20)):
        if await matches.nth(index).is_visible():
            return True
    return False


async def snapshot(page, http_status):
    html = await page.content()
    title = await page.title()
    return classify_page(
        html, page.url, http_status, title,
        visible_challenge=await any_visible(page, CHALLENGE_SELECTOR),
        visible_login=await any_visible(page, LOGIN_SELECTOR),
    )


async def observe(playwright, report, *, headless=True):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    browser = await playwright.chromium.launch(headless=headless, timeout=10000)
    try:
        context = await browser.new_context()
        page = await context.new_page()
        navigation_timed_out = False
        try:
            response = await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            report["http_status"] = response.status if response else None
        except PlaywrightTimeoutError:
            navigation_timed_out = True
        report.update(await snapshot(page, report["http_status"]))
        if report["status"] in {"blocked", "accessible"}:
            return
        if navigation_timed_out:
            report.update(status="error", reason="navigation_timeout")
            return
        if report["status"] == "inconclusive":
            try:
                await page.wait_for_selector(READY_SELECTOR, state="visible", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            report.update(await snapshot(page, report["http_status"]))
    finally:
        await asyncio.wait_for(browser.close(), timeout=3)


async def run_probe(*, headless=True):
    started = time.monotonic()
    try:
        playwright_version = version("playwright")
    except PackageNotFoundError:
        playwright_version = None
    report = {
        "requested_url": TARGET_URL,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "error",
        "final_url": None,
        "title": "",
        "http_status": None,
        "reason": "runtime_error",
        "listing_count": None,
        "playwright_version": playwright_version,
        "browser": "chromium",
        "headless": headless,
        "timeout_seconds": TOTAL_TIMEOUT_SECONDS,
    }
    try:
        from playwright.async_api import async_playwright

        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            async with async_playwright() as playwright:
                await observe(playwright, report, headless=headless)
    except TimeoutError:
        report.update(status="error", reason="total_timeout")
    except Exception as error:
        # Error messages may contain transient URL tokens; retain only the type.
        report.update(status="error", reason="runtime_error", error_type=type(error).__name__)
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--output", type=Path, default=Path("probe-artifacts/report.json"))
    arguments.add_argument("--headed", action="store_true", help="Use a headed Chromium browser; Linux runners require a display such as Xvfb")
    args = arguments.parse_args()
    report = asyncio.run(run_probe(headless=not args.headed))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
