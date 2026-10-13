"""Observe five published resources once; never import listings or solve challenges."""

import argparse
import asyncio
from datetime import datetime
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
from time import monotonic
from urllib.parse import urljoin, urlsplit

import httpx


ROBOTS_URL = "https://www.idealista.com/robots.txt"
OPENAPI_URL = "https://apidea.es/openapi.json"
LOCATIONS_URL = "https://apidea.es/locations-es-index.json"
AGENCY_URL = "https://delaordeninmobiliaria.es/inmuebles/"
IMAGE_URL = (
    "https://delaordeninmobiliaria.es/wp-content/uploads/2026/07/"
    "piso-venta-calle-oriana-19-madrid-768x512.webp"
)
IDEALISTA_AGENCY_LINK = (
    "https://www.idealista.com/pro/de-la-orden-gestion-inmobiliaria/inmueble/112062204/"
)
IDEALISTA_CANONICAL_LINK = "https://www.idealista.com/inmueble/112062204/"
RESOURCES = (
    (ROBOTS_URL, "robots"),
    (OPENAPI_URL, "openapi"),
    (LOCATIONS_URL, "locations"),
    (AGENCY_URL, "agency"),
    (IMAGE_URL, "image"),
)
REQUEST_TIMEOUT_SECONDS = 10.0
GLOBAL_TIMEOUT_SECONDS = 45.0
TEXT_BODY_LIMIT = 1_048_576
IMAGE_BODY_LIMIT = 1024


class _PublicHTMLFacts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.listing_link = False
        self.thumbnail = False
        self.visible_text = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden_depth += 1
        if self.hidden_depth:
            return
        attributes = dict(attrs)
        if tag == "a":
            self.listing_link |= urljoin(AGENCY_URL, attributes.get("href") or "") in {
                IDEALISTA_AGENCY_LINK, IDEALISTA_CANONICAL_LINK
            }
        if tag == "img":
            sources = [attributes.get(name) or "" for name in ("src", "data-src", "data-lazy-src")]
            for name in ("srcset", "data-srcset"):
                sources.extend(part.strip().split()[0] for part in (attributes.get(name) or "").split(",")
                               if part.strip())
            self.thumbnail |= any(urljoin(AGENCY_URL, source) == IMAGE_URL for source in sources)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.visible_text.append(data)


def _html_facts(body):
    facts = _PublicHTMLFacts()
    facts.feed(body.decode("utf-8", errors="replace"))
    return facts


def _has_challenge(facts):
    text = " ".join(facts.visible_text).lower()
    return bool(re.search(r"\bcaptcha\b", text)) or any(marker in text for marker in (
        "verify you are human", "checking your browser",
        "please enable js and disable any ad blocker",
    ))


def _content_type(response):
    # Only the observed media type is retained; arbitrary header parameters are excluded.
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return media_type if re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media_type) else None


def _metadata_date(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)?", value
    ):
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def _observations(kind, body, facts):
    if kind == "robots":
        directives = [line.split("#", 1)[0].strip().partition(":")
                      for line in body.decode("utf-8", errors="replace").splitlines()]
        return {
            "declares_sitemaps": any(key.lower() == "sitemap" and value.strip()
                                     for key, _, value in directives),
            "disallow_rss": any(key.lower() == "disallow" and value.strip() == "/rss/"
                                for key, _, value in directives),
        }
    if kind == "agency":
        return {"idealista_112062204_link": facts.listing_link, "published_thumbnail": facts.thumbnail}
    if kind == "image":
        image_format = "unknown"
        if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            image_format = "webp"
        elif body.startswith(b"\x89PNG\r\n\x1a\n"):
            image_format = "png"
        elif body.startswith(b"\xff\xd8\xff"):
            image_format = "jpeg"
        elif body[:6] in {b"GIF87a", b"GIF89a"}:
            image_format = "gif"
        return {"format_prefix": image_format}

    document = json.loads(body)
    if not isinstance(document, dict):
        return {}
    if kind == "openapi":
        components = document.get("components")
        schemes = components.get("securitySchemes") if isinstance(components, dict) else None
        scheme = schemes.get("RapidAPIKey") if isinstance(schemes, dict) else None
        declared = isinstance(scheme, dict) and (
            scheme.get("type"), scheme.get("in"), scheme.get("name")
        ) == ("apiKey", "header", "X-RapidAPI-Key")
        security = document.get("security")
        required = isinstance(security, list) and bool(security) and all(
            isinstance(requirement, dict) and "RapidAPIKey" in requirement for requirement in security
        )
        return {"rapidapi_key_scheme": declared, "rapidapi_key_required": bool(declared and required)}

    def segment_count(operation):
        metadata = document.get(operation)
        segments = metadata.get("segments") if isinstance(metadata, dict) else None
        return len(segments) if isinstance(segments, list) else None

    # generatedAt is source metadata, with no assertion about listing freshness.
    return {"generatedAt": _metadata_date(document.get("generatedAt")),
            "rent_segments": segment_count("rent"), "sale_segments": segment_count("sale")}


def _empty_result(url):
    return {"url": url, "status": "skipped", "reason": None, "http_status": None,
            "content_type": None, "bytes_read": 0, "sha256": None,
            "body_complete": False, "error_class": None, "observations": {}}


async def _observe_one(result, kind, timeout, transport):
    body = bytearray()
    result["sha256"] = hashlib.sha256(body).hexdigest()
    limit = IMAGE_BODY_LIMIT if kind == "image" else TEXT_BODY_LIMIT
    try:
        # Each resource gets a new client: Set-Cookie is never reused, including on the same origin.
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(timeout), headers={"Accept-Encoding": "identity"},
        ) as client:
            async with client.stream("GET", result["url"]) as response:
                result["http_status"] = response.status_code
                result["content_type"] = _content_type(response)
                if response.status_code in {403, 429}:
                    result.update(status="blocked", reason=f"http_{response.status_code}")
                    return
                if 300 <= response.status_code < 400:
                    result.update(status="redirect", reason="redirect_not_followed")
                    return
                async for chunk in response.aiter_raw(chunk_size=256):
                    body.extend(chunk[:limit - len(body)])
                    if len(body) >= limit:
                        break
                else:
                    result["body_complete"] = True

        prefix = body.lstrip()
        is_html = prefix.startswith(b"<") or (
            result["content_type"] == "text/html" and not prefix.startswith((b"{", b"["))
        )
        facts = _html_facts(body) if kind == "agency" or is_html else None
        if facts is not None and _has_challenge(facts):
            result.update(status="blocked", reason="captcha")
        elif not result["body_complete"]:
            result.update(status="partial", reason="body_limit")
            if kind not in {"openapi", "locations"}:
                result["observations"] = _observations(kind, body, facts)
        elif not 200 <= result["http_status"] < 300:
            result.update(status="http_error", reason="http_status")
        else:
            try:
                result["observations"] = _observations(kind, body, facts)
                result.update(status="observed", reason=None)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                result.update(status="invalid_JSON", reason="json_decode", error_class=type(error).__name__)
    finally:
        result["bytes_read"] = len(body)
        result["sha256"] = hashlib.sha256(body).hexdigest()


async def run_probe(*, transport=None):
    deadline = monotonic() + GLOBAL_TIMEOUT_SECONDS
    blocked_origins = set()
    results = []
    diagnostic_complete = True
    for url, kind in RESOURCES:
        result = _empty_result(url)
        results.append(result)
        remaining = deadline - monotonic()
        origin = urlsplit(url).netloc
        if remaining <= 0:
            result["reason"] = "global_deadline"
            diagnostic_complete = False
            continue
        if origin in blocked_origins:
            result["reason"] = "origin_blocked"
            continue
        timeout = min(REQUEST_TIMEOUT_SECONDS, remaining)
        try:
            # httpx timeouts alone apply per phase. This bounds the complete GET and streamed read.
            await asyncio.wait_for(_observe_one(result, kind, timeout, transport), timeout=timeout)
        except TimeoutError as error:
            global_expired = remaining <= REQUEST_TIMEOUT_SECONDS
            result.update(status="partial" if global_expired else "transport_error",
                          reason="global_deadline" if global_expired else "request_timeout",
                          error_class=type(error).__name__)
            diagnostic_complete &= not global_expired
        except httpx.HTTPError as error:
            result.update(status="transport_error", reason="httpx_error", error_class=type(error).__name__)
        if result["status"] == "blocked":
            blocked_origins.add(origin)
    return {"status": "complete" if diagnostic_complete else "partial",
            "diagnostic_complete": diagnostic_complete, "results": results}


def write_report(output, report):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=".report-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run_probe())
    write_report(args.output, report)
    # A completed diagnosis can contain blocked, invalid JSON or transport error results.
    return 0 if report["diagnostic_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
