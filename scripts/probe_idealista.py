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
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

from index_scrapers.extractors import PortalBlocked, parse


TARGET_URL = "https://www.idealista.com/alquiler-viviendas/madrid-madrid/"
HOME_URL = "https://www.idealista.com/"
RENT_DIRECTORY_URL = "https://www.idealista.com/alquiler-viviendas/"
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


def classify_page(html, final_url, http_status, title, *, visible_challenge=False, visible_login=False, public_navigation=False):
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
    target_page = parts.path.rstrip("/") == urlsplit(TARGET_URL).path.rstrip("/")
    if parts.scheme != "https" or parts.netloc not in {"www.idealista.com", "idealista.com"} or not target_page and not public_navigation:
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
    if public_navigation and not target_page:
        if http_status is None or not 200 <= http_status < 300:
            result.update(status="error", reason="no_http_response" if http_status is None else f"http_{http_status}")
        else:
            result.update(status="observed", reason="public_page")
        return result
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


async def snapshot(page, http_status, *, public_navigation=False):
    html = await page.content()
    title = await page.title()
    return classify_page(
        html, page.url, http_status, title,
        visible_challenge=await any_visible(page, CHALLENGE_SELECTOR),
        visible_login=await any_visible(page, LOGIN_SELECTOR),
        public_navigation=public_navigation,
    )


def navigation_url(href, base_url):
    if not isinstance(href, str) or not href.strip():
        return None
    try:
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme != "https" or parts.hostname not in {"www.idealista.com", "idealista.com"} or parts.username or parts.password or parts.port not in {None, 443}:
            return None
        return public_url(absolute)
    except (TypeError, ValueError):
        return None


async def published_link(page, visited):
    anchors = page.locator("a[href]")
    candidate = None
    paths = []
    for index in range(min(await anchors.count(), 500)):
        anchor = anchors.nth(index)
        url = navigation_url(await anchor.get_attribute("href"), page.url)
        path = urlsplit(url or "").path.rstrip("/")
        if not url or path != "/alquiler-viviendas" and not path.startswith("/alquiler-viviendas/"):
            continue
        if url not in paths and len(paths) < 20:
            paths.append(url)
        if path in visited or not await anchor.is_visible():
            continue
        if path == urlsplit(TARGET_URL).path.rstrip("/"):
            return anchor, paths
        if candidate is None and path == urlsplit(RENT_DIRECTORY_URL).path.rstrip("/"):
            candidate = anchor
    return candidate, paths


async def reject_observed_optional_consent(page):
    dialogs = page.locator('[role="dialog"], [role="alertdialog"], dialog, [aria-modal="true"]')
    for index in range(min(await dialogs.count(), 10)):
        dialog = dialogs.nth(index)
        if not await dialog.is_visible():
            continue
        words = normalized(await dialog.inner_text())
        if "cookie" not in words and "consent" not in words:
            continue
        buttons = dialog.get_by_role("button")
        for button_index in range(min(await buttons.count(), 20)):
            button = buttons.nth(button_index)
            label = normalized(clean_text(await button.inner_text()))
            if label in {"rechazar", "rechazar todas", "reject", "reject all", "solo necesarias", "solo cookies necesarias", "reject optional cookies"} and await button.is_visible():
                await button.click(timeout=2000)
                return "rejected_optional"
        return "consent_required"
    return None


def trace_document(report, step, result):
    entry = {"step": step, "url": result["final_url"], "http_status": result["http_status"], "status": result["status"], "reason": result["reason"]}
    report.setdefault("trace", []).append(entry)


async def observe_via_home(page, report, timeout_error):
    document_status = {"value": None}

    def record_response(response):
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            document_status["value"] = response.status

    page.on("response", record_response)
    try:
        response = await diagnostic_phase(report, "navigation", page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000), timeout_seconds=30, timeout_error=timeout_error)
    except timeout_error:
        result = await diagnostic_phase(report, "render", snapshot(page, document_status["value"], public_navigation=True), timeout_error=timeout_error)
        report.update(result)
        if result["status"] != "blocked":
            report.update(status="error", reason="navigation_timeout")
        trace_document(report, "home", report)
        return
    document_status["value"] = response.status if response else document_status["value"]
    visited = set()
    for document_index in range(3):
        result = await diagnostic_phase(report, "render", snapshot(page, document_status["value"], public_navigation=True), timeout_error=timeout_error)
        report.update(result)
        step = "home" if document_index == 0 else "target" if result["final_url"] and urlsplit(result["final_url"]).path.rstrip("/") == urlsplit(TARGET_URL).path.rstrip("/") else "intermediate"
        trace_document(report, step, result)
        visited.add(urlsplit(result["final_url"] or "").path.rstrip("/"))
        if result["status"] in {"blocked", "error"}:
            return
        if step == "target":
            if result["status"] == "inconclusive":
                try:
                    await diagnostic_phase(report, "render", page.wait_for_selector(READY_SELECTOR, state="visible", timeout=10000), timeout_seconds=10, timeout_error=timeout_error)
                except timeout_error:
                    pass
                result = await diagnostic_phase(report, "render", snapshot(page, document_status["value"], public_navigation=True), timeout_error=timeout_error)
                report.update(result)
                report["trace"][-1].update(url=result["final_url"], http_status=result["http_status"], status=result["status"], reason=result["reason"])
            return
        try:
            consent = await reject_observed_optional_consent(page)
        except timeout_error:
            consent = "consent_required"
        if consent == "consent_required":
            report.update(status="blocked", reason="consent_required")
            report["trace"][-1].update(status="blocked", reason="consent_required")
            return
        if consent:
            report.setdefault("consent_actions", []).append(consent)
            result = await diagnostic_phase(report, "render", snapshot(page, document_status["value"], public_navigation=True), timeout_error=timeout_error)
            report.update(result)
            report["trace"][-1].update(url=result["final_url"], http_status=result["http_status"], status=result["status"], reason=result["reason"])
            if result["status"] in {"blocked", "error"}:
                return
        anchor, paths = await published_link(page, visited)
        report["observed_rental_paths"] = list(dict.fromkeys(report.get("observed_rental_paths", []) + paths))[:20]
        if anchor is None or document_index == 2:
            reason = "link_missing" if anchor is None else "document_limit"
            report.update(status="inconclusive", reason=reason)
            report["trace"][-1].update(status="inconclusive", reason=reason)
            return
        previous_url = public_url(page.url)
        document_status["value"] = None
        try:
            await diagnostic_phase(report, "navigation", anchor.click(timeout=10000), timeout_seconds=10, timeout_error=timeout_error)
            await diagnostic_phase(report, "navigation", page.wait_for_url(lambda url: public_url(url) != previous_url, wait_until="domcontentloaded", timeout=15000), timeout_seconds=15, timeout_error=timeout_error)
        except timeout_error:
            result = await diagnostic_phase(report, "render", snapshot(page, document_status["value"], public_navigation=True), timeout_error=timeout_error)
            report.update(result)
            if result["status"] != "blocked":
                report.update(status="error", reason="navigation_timeout")
            if result["final_url"] != previous_url:
                trace_document(report, "target" if urlsplit(result["final_url"] or "").path.rstrip("/") == urlsplit(TARGET_URL).path.rstrip("/") else "intermediate", report)
            else:
                report["trace"][-1].update(status=report["status"], reason=report["reason"])
            return


async def diagnostic_phase(report, name, operation, *, timeout_seconds=None, timeout_source="operation", timeout_error=()):
    started = time.monotonic()
    if timeout_seconds is None:
        timeout_seconds = report.get("timeout_seconds", TOTAL_TIMEOUT_SECONDS)
        timeout_source = "total"
    entry = {"phase": name, "status": "running", "timeout_seconds": timeout_seconds, "timeout_source": timeout_source}
    report.setdefault("phases", []).append(entry)
    report["phase"] = name
    try:
        value = await operation
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
        report["failed_phase"] = name
        if not report.get("cancelled_phase"):
            report["cancelled_phase"] = name
        report["timeout_phase"] = report["cancelled_phase"]
        raise
    except Exception as error:
        timed_out = isinstance(error, TimeoutError) or bool(timeout_error) and isinstance(error, timeout_error)
        entry.update(status="timeout" if timed_out else "failed", error_type=type(error).__name__)
        report["failed_phase"] = name
        if timed_out:
            report["timeout_phase"] = name
        raise
    else:
        entry["status"] = "completed"
        return value
    finally:
        entry["elapsed_seconds"] = round(time.monotonic() - started, 3)


async def observe(playwright, report, *, headless=True, via_home=False, browser="chromium", wait_for_render=False, startup_timeout_seconds=10):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    engine = getattr(playwright, browser)
    browser = await diagnostic_phase(report, "launch", engine.launch(headless=headless, timeout=startup_timeout_seconds * 1000), timeout_seconds=startup_timeout_seconds, timeout_error=PlaywrightTimeoutError)
    try:
        total_budget = report.get("timeout_seconds", TOTAL_TIMEOUT_SECONDS)
        context = await diagnostic_phase(report, "new_context", browser.new_context(), timeout_seconds=total_budget, timeout_source="total", timeout_error=PlaywrightTimeoutError)
        page = await diagnostic_phase(report, "new_page", context.new_page(), timeout_seconds=total_budget, timeout_source="total", timeout_error=PlaywrightTimeoutError)
        if via_home:
            await observe_via_home(page, report, PlaywrightTimeoutError)
            return
        navigation_status = {"latest": None}
        if wait_for_render:
            report["public_render_observed"] = False

            def record_response(response):
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    navigation_status["latest"] = response.status
                    report["last_navigation_http_status"] = response.status

            page.on("response", record_response)
        navigation_timed_out = False
        try:
            response = await diagnostic_phase(report, "navigation", page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000), timeout_seconds=30, timeout_error=PlaywrightTimeoutError)
            report["http_status"] = response.status if response else None
        except PlaywrightTimeoutError:
            navigation_timed_out = True
        if wait_for_render:
            if report["http_status"] is None:
                report["http_status"] = navigation_status["latest"]
            report["initial_http_status"] = report["http_status"]
        report.update(await diagnostic_phase(report, "render", snapshot(page, report["http_status"]), timeout_error=PlaywrightTimeoutError))
        trace_document(report, "target", report)
        if wait_for_render and not navigation_timed_out:
            if report["http_status"] in {401, 429}:
                return
            path = urlsplit(report["final_url"] or "").path.lower().split("/")
            manual_auth = any(segment in {"login", "signin", "identificacion", "access"} for segment in path) or await any_visible(page, LOGIN_SELECTOR)
            if manual_auth:
                report.update(status="blocked", reason="authentication_required")
                report["trace"][-1].update(status="blocked", reason="authentication_required")
                return
            try:
                await diagnostic_phase(report, "render", page.wait_for_selector("article.item", state="visible", timeout=15000), timeout_seconds=15, timeout_error=PlaywrightTimeoutError)
            except PlaywrightTimeoutError:
                report.update(await diagnostic_phase(report, "render", snapshot(page, report["initial_http_status"]), timeout_error=PlaywrightTimeoutError))
            else:
                report["public_render_observed"] = True
                latest_status = navigation_status["latest"]
                report.update(await diagnostic_phase(report, "render", snapshot(page, latest_status if latest_status is not None else report["initial_http_status"]), timeout_error=PlaywrightTimeoutError))
            report["trace"][-1].update(url=report["final_url"], http_status=report["http_status"], status=report["status"], reason=report["reason"])
            return
        if report["status"] in {"blocked", "accessible"}:
            return
        if navigation_timed_out:
            report.update(status="error", reason="navigation_timeout")
            report["trace"][-1].update(status="error", reason="navigation_timeout")
            return
        if report["status"] == "inconclusive":
            try:
                await diagnostic_phase(report, "render", page.wait_for_selector(READY_SELECTOR, state="visible", timeout=10000), timeout_seconds=10, timeout_error=PlaywrightTimeoutError)
            except PlaywrightTimeoutError:
                pass
            report.update(await diagnostic_phase(report, "render", snapshot(page, report["http_status"]), timeout_error=PlaywrightTimeoutError))
            report["trace"][-1].update(status=report["status"], reason=report["reason"])
    finally:
        await diagnostic_phase(report, "close", asyncio.wait_for(browser.close(), timeout=3), timeout_seconds=3, timeout_error=PlaywrightTimeoutError)


async def run_probe(*, headless=True, via_home=False, browser="chromium", wait_for_render=False, startup_timeout_seconds=10):
    started = time.monotonic()
    startup_timeout_seconds = 60 if browser == "firefox" and startup_timeout_seconds == 60 else 10
    total_budget = TOTAL_TIMEOUT_SECONDS + startup_timeout_seconds - 10
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
        "browser": browser,
        "headless": headless,
        "navigation_mode": "via_home" if via_home else "direct",
        "wait_for_render": wait_for_render,
        "trace": [],
        "startup_timeout_seconds": startup_timeout_seconds,
        "timeout_seconds": total_budget,
        "phase": "launch",
        "failed_phase": None,
        "timeout_phase": None,
        "phases": [],
    }
    try:
        from playwright.async_api import async_playwright

        async with asyncio.timeout(total_budget):
            async with async_playwright() as playwright:
                await observe(playwright, report, headless=headless, via_home=via_home, browser=browser, wait_for_render=wait_for_render, startup_timeout_seconds=startup_timeout_seconds)
    except TimeoutError:
        report.update(status="error", reason="total_timeout", timeout_phase=report.get("cancelled_phase") or report.get("timeout_phase") or report["phase"])
    except Exception as error:
        # Error messages may contain transient URL tokens; retain only the type.
        report.update(status="error", reason="runtime_error", error_type=type(error).__name__)
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--output", type=Path, default=Path("probe-artifacts/report.json"))
    arguments.add_argument("--headed", action="store_true", help="Use a headed browser; Linux runners require a display such as Xvfb")
    arguments.add_argument("--via-home", action="store_true", help="Start at the public home page and follow published rental links, at most three documents")
    arguments.add_argument("--browser", choices=["chromium", "firefox"], default="chromium", help="Standard Playwright browser engine")
    arguments.add_argument("--wait-for-render", action="store_true", help="Direct navigation only: passively wait up to 15 seconds for public listing cards")
    arguments.add_argument("--startup-timeout-seconds", type=int, choices=[10, 60], default=10, help="Firefox only: optional 60-second startup margin; the default is 10 seconds")
    args = arguments.parse_args()
    if args.wait_for_render and args.via_home:
        arguments.error("--wait-for-render is supported only with direct navigation")
    if args.startup_timeout_seconds != 10 and args.browser != "firefox":
        arguments.error("The extended startup margin is supported only with Firefox")
    report = asyncio.run(run_probe(headless=not args.headed, via_home=args.via_home, browser=args.browser, wait_for_render=args.wait_for_render, startup_timeout_seconds=args.startup_timeout_seconds))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
