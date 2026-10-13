"""Diagnose advertised public agency feeds once, without importing or following records."""

import argparse
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import tempfile
from time import monotonic
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import httpx


# Primary agency pages were checked with a web reader, not by probing feed paths.
AGENCIES = (
    {"id": "bafre", "source_url": "https://bafreinmobiliaria.es/",
     "primary_reference_url": "https://bafreinmobiliaria.es/"},
    {"id": "estate-one", "source_url": "https://blog.estateone.biz/",
     "primary_reference_url": "https://www.estateone.biz/blog/"},
    {"id": "buena-vida-spain", "source_url": "https://buenavidaspain.com/",
     "primary_reference_url": "https://buenavidaspain.com/"},
)
REQUEST_TIMEOUT_SECONDS = 10.0
GLOBAL_TIMEOUT_SECONDS = 60.0
BODY_LIMIT = 1_048_576
MAX_REQUESTS = 10
MAX_ORIGINS = 5
MAX_RECORDS = 500
MAX_SAVED_PROPERTY_LINKS = 10
FEED_TYPES = {"application/rss+xml": "rss", "application/atom+xml": "atom",
              "application/feed+json": "json_feed"}
FIELD_NAMES = ("title", "record_link", "price", "photo_links", "publication_date",
               "area", "rooms", "operation")
IDEALISTA_PROPERTY = re.compile(
    r"https://(?:www\.)?idealista\.com/(?:pro/[a-zA-Z0-9_-]+/)?inmueble/[0-9]+/"
)
PRIVATE_PATH = re.compile(
    r"(?:^|/)(?:users?|usuarios?|comments?|comentarios?|settings|metadata|private|"
    r"admin|wp-admin|wp-login\.php|login|signin|account|my-account|auth|tokens?)(?:/|$)", re.I
)


def public_url(value, base):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    try:
        url = urljoin(base, value)
        parts = urlsplit(url)
        if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or parts.port not in {None, 80, 443}
                or any(ord(character) < 33 or ord(character) == 127 for character in url)):
            return None
        try:
            if not ipaddress.ip_address(parts.hostname).is_global:
                return None
        except ValueError:
            if parts.hostname in {"localhost", "localhost.localdomain"} or parts.hostname.endswith(".local"):
                return None
        return url
    except ValueError:
        return None


def origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port


def candidate(attrs, source, method):
    rels = (attrs.get("rel") or "").lower().split()
    media_type = (attrs.get("type") or "").split(";", 1)[0].strip().lower()
    kind = FEED_TYPES.get(media_type) if "alternate" in rels else None
    if "https://api.w.org/" in rels and media_type in {"", "application/json"}:
        kind = "restapi"
    if kind is None:
        return None
    url = public_url(attrs.get("href"), source)
    if (url is None or origin(url) != origin(source) or urlsplit(url).scheme != "https"
            or PRIVATE_PATH.search(urlsplit(url).path)):
        return None
    return {"url": url, "kind": kind, "method": method,
            "rel": "https://api.w.org/" if kind == "restapi" else "alternate",
            "media_type": media_type or "application/json"}


class PublicFacts(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.candidates = []
        self.visible_text = []
        self.hidden_depth = 0
        self.password_input = False
        self.photo_count = 0
        self.idealista_links = set()

    def handle_starttag(self, tag, attributes):
        if tag in {"script", "style"}:
            self.hidden_depth += 1
        if self.hidden_depth:
            return
        attrs = dict(attributes)
        if tag == "link":
            advertised = candidate(attrs, self.source, "html_link")
            if advertised:
                self.candidates.append(advertised)
        if tag == "input" and (attrs.get("type") or "").lower() == "password":
            self.password_input = True
        if tag == "img" and public_url(attrs.get("src"), self.source):
            self.photo_count += 1
        if tag == "a":
            link = public_url(attrs.get("href"), self.source)
            if link and IDEALISTA_PROPERTY.fullmatch(link):
                self.idealista_links.add(link)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.visible_text.append(data)


def html_facts(body, source):
    facts = PublicFacts(source)
    facts.feed(body.decode("utf-8", errors="replace"))
    return facts


def challenge(facts):
    text = " ".join(facts.visible_text).lower()
    if re.search(r"\bcaptcha\b", text) or any(marker in text for marker in (
        "verify you are human", "checking your browser", "please enable js and disable any ad blocker",
    )):
        return "captcha"
    return "login" if facts.password_input else None


def content_type(response):
    value = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return value if re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", value) else None


def empty_observation(url):
    return {"url": url, "status": "skipped", "reason": None, "http_status": None,
            "content_type": None, "bytes_read": 0, "sha256": None, "body_complete": False,
            "error_class": None, "observations": {}}


async def fetch_once(result, timeout, transport):
    body = bytearray()
    header_candidates = []
    try:
        # A fresh client per GET discards Set-Cookie even for a subsequent same-origin feed.
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=timeout, headers={"Accept-Encoding": "identity"}) as client:
            async with client.stream("GET", result["url"]) as response:
                result["http_status"] = response.status_code
                result["content_type"] = content_type(response)
                if response.status_code in {401, 403, 407, 429}:
                    result.update(status="blocked", reason=f"http_{response.status_code}")
                    return b"", []
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location", "")
                    login = bool(PRIVATE_PATH.search(urlsplit(urljoin(result["url"], location)).path))
                    result.update(status="blocked" if login else "redirect",
                                  reason="login" if login else "redirect_not_followed")
                    return b"", []
                if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                    result.update(status="unsupported", reason="encoded_body_not_read")
                    return b"", []
                for link in response.links.values():
                    advertised = candidate({"href": link.get("url"), "rel": link.get("rel"),
                                            "type": link.get("type")}, result["url"], "http_link")
                    if advertised:
                        header_candidates.append(advertised)
                async for chunk in response.aiter_raw(chunk_size=256):
                    body.extend(chunk[:BODY_LIMIT - len(body)])
                    if len(body) >= BODY_LIMIT:
                        break
                else:
                    result["body_complete"] = True
        is_html = result["content_type"] == "text/html" or bool(
            re.match(br"\s*<(?:!doctype\s+html|html|head|body)(?:\s|>)", body, re.I)
        )
        reason = challenge(html_facts(body, result["url"])) if is_html else None
        if reason:
            result.update(status="blocked", reason=reason)
        elif not result["body_complete"]:
            result.update(status="partial", reason="body_limit")
        elif not 200 <= result["http_status"] < 300:
            result.update(status="http_error", reason="http_status")
        else:
            result.update(status="observed", reason=None)
        return bytes(body), header_candidates
    finally:
        result["bytes_read"] = len(body)
        result["sha256"] = hashlib.sha256(body).hexdigest()


def positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value.strip()):
        return False
    try:
        number = float(value)
        return math.isfinite(number) and number >= 0
    except (ValueError, OverflowError):
        return False


def metadata_day(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)?", value):
            return datetime.fromisoformat(value).date().isoformat()
        parsed = parsedate_to_datetime(value)
        return parsed.date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def blank_summary(format_name, records):
    return {"format": format_name, "record_count": len(records), "records_examined": min(len(records), MAX_RECORDS),
            "listing_candidates": 0, "field_counts": dict.fromkeys(FIELD_NAMES, 0),
            "photo_link_count": 0, "publication_date_range": None,
            "dates_are_listing_freshness": False, "idealista_property_links": [],
            "idealista_property_link_count": 0, "idealista_links_truncated": False}


def summarize_records(records, format_name, source):
    summary = blank_summary(format_name, records)
    dates, property_links = [], set()
    for record in records[:MAX_RECORDS]:
        values = record["values"]
        fields = summary["field_counts"]
        for name in FIELD_NAMES:
            if name == "photo_links":
                present = bool(record["photo_count"])
            elif name == "publication_date":
                date = next((day for value in values.get(name, []) if (day := metadata_day(value))), None)
                if date:
                    dates.append(date)
                present = bool(date)
            elif name in {"price", "area", "rooms"}:
                present = any(positive_number(value) for value in values.get(name, []))
            elif name == "record_link":
                present = any(public_url(value, source) for value in values.get(name, []))
            else:
                present = any(isinstance(value, str) and value.strip() for value in values.get(name, []))
            fields[name] += int(present)
        summary["photo_link_count"] += record["photo_count"]
        has_price = any(positive_number(value) for value in values.get("price", []))
        has_property_field = any(values.get(field) for field in ("area", "rooms", "operation"))
        summary["listing_candidates"] += int(record["explicit_property"] or (has_price and has_property_field))
        property_links.update(record["idealista_links"])
    if dates:
        summary["publication_date_range"] = [min(dates), max(dates)]
    summary["idealista_property_link_count"] = len(property_links)
    summary["idealista_links_truncated"] = len(property_links) > MAX_SAVED_PROPERTY_LINKS
    summary["idealista_property_links"] = sorted(property_links)[:MAX_SAVED_PROPERTY_LINKS]
    return summary


ALIASES = {"title": "title", "titulo": "title", "url": "record_link", "link": "record_link",
           "external_url": "record_link", "price": "price", "precio": "price", "amount": "price",
           "pubdate": "publication_date", "published": "publication_date", "updated": "publication_date",
           "date_published": "publication_date", "date_modified": "publication_date", "fecha": "publication_date",
           "area": "area", "surface": "area", "superficie": "area", "rooms": "rooms",
           "bedrooms": "rooms", "habitaciones": "rooms", "operation": "operation", "operacion": "operation"}
PHOTO_FIELDS = {"image", "images", "photo", "photos", "picture", "pictures", "foto", "fotos", "thumbnail", "content"}
CONTENT_FIELDS = {"description", "encoded", "content", "summary", "content_html"}
PROPERTY_TYPES = {"property", "inmueble", "listing", "house", "apartment", "residence", "singlefamilyresidence"}


def record_facts(pairs, source, explicit_property=False):
    record = {"values": {}, "photo_count": 0, "idealista_links": set(),
              "explicit_property": explicit_property}
    for name, value, attributes in pairs:
        name = name.lower()
        field = ALIASES.get(name)
        if field:
            record["values"].setdefault(field, []).append(attributes.get("href") or value)
        if name in PHOTO_FIELDS or name == "enclosure" and (attributes.get("type") or "").startswith("image/"):
            link = attributes.get("url") or attributes.get("href") or value
            record["photo_count"] += int(bool(public_url(link, source)))
        if name in CONTENT_FIELDS and isinstance(value, str):
            facts = html_facts(value.encode(), source)
            record["photo_count"] += facts.photo_count
            record["idealista_links"].update(facts.idealista_links)
        if field == "record_link":
            link = public_url(attributes.get("href") or value, source)
            if link and IDEALISTA_PROPERTY.fullmatch(link):
                record["idealista_links"].add(link)
    return record


def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def summarize_resource(body, media_type, source):
    prefix = body.lstrip()
    if prefix.startswith((b"{", b"[")):
        document = json.loads(body)
        if isinstance(document, dict) and isinstance(document.get("routes"), dict):
            summary = blank_summary("public_api_index", [])
            summary["route_count"] = len(document["routes"])
            return summary
        format_name, explicit = "unrecognized_json", False
        entries = []
        if isinstance(document, dict):
            if str(document.get("version", "")).startswith("https://jsonfeed.org/version/"):
                format_name, entries = "json_feed", document.get("items", [])
            else:
                for collection in ("properties", "listings", "inmuebles"):
                    if isinstance(document.get(collection), list):
                        format_name, entries, explicit = "property_json", document[collection], True
                        break
        if not isinstance(entries, list):
            raise ValueError("invalid_collection")
        records = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            pairs = []

            def fields(values, depth=0):
                if depth > 4:
                    return
                for name, value in values.items():
                    if name.lower() in set(ALIASES) | PHOTO_FIELDS | CONTENT_FIELDS:
                        for entry in value if isinstance(value, list) else [value]:
                            if isinstance(entry, (str, int, float)):
                                pairs.append((name, entry, {}))
                    if name in {"offers", "property", "image", "images", "photos"}:
                        for nested in value if isinstance(value, list) else [value]:
                            if isinstance(nested, dict):
                                fields(nested, depth + 1)

            fields(item)
            declared_type = item.get("@type")
            is_property = isinstance(declared_type, str) and declared_type.lower() in PROPERTY_TYPES
            records.append(record_facts(pairs, source, explicit or is_property))
        return summarize_records(records, format_name, source)
    if prefix.startswith(b"<"):
        if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
            raise ValueError("xml_declaration")
        document = ElementTree.fromstring(body)
        root_name = local_name(document.tag)
        if root_name == "rss":
            format_name, entries = "rss", document.findall("./channel/item")
        elif root_name == "feed":
            format_name = "atom"
            entries = [child for child in document if local_name(child.tag) == "entry"]
        elif root_name in {"properties", "listings", "inmuebles"}:
            format_name = "property_xml"
            entries = [child for child in document if local_name(child.tag) in PROPERTY_TYPES]
        else:
            format_name, entries = "unrecognized_xml", []
        records = [record_facts([(local_name(node.tag), node.text or "", node.attrib)
                                 for node in entry.iter()], source, format_name == "property_xml")
                   for entry in entries]
        return summarize_records(records, format_name, source)
    raise ValueError("unsupported_document")


async def run_probe(*, agencies=AGENCIES, transport=None):
    deadline = monotonic() + GLOBAL_TIMEOUT_SECONDS
    origins, requested, blocked = set(), set(), set()
    report = {"schema_version": 1, "status": "complete", "diagnostic_complete": True, "ingested": False,
              "observed_at_utc": datetime.now(timezone.utc).isoformat(),
              "limits": {"requests": MAX_REQUESTS, "origins": MAX_ORIGINS, "body_bytes": BODY_LIMIT,
                         "request_seconds": REQUEST_TIMEOUT_SECONDS, "total_seconds": GLOBAL_TIMEOUT_SECONDS,
                         "resources_per_agency": 1, "records_examined_per_resource": MAX_RECORDS},
              "requests_performed": 0, "origins_requested": 0, "results": []}

    async def observe(result):
        remaining = deadline - monotonic()
        url = result["url"]
        request_origin = origin(url)
        if remaining <= 0:
            reason = "global_deadline"
        elif url in requested:
            reason = "already_requested"
        elif request_origin in blocked:
            reason = "origin_blocked"
        elif request_origin not in origins and len(origins) >= MAX_ORIGINS:
            reason = "origin_limit"
        elif len(requested) >= MAX_REQUESTS:
            reason = "request_limit"
        else:
            reason = None
        if reason:
            result["reason"] = reason
            if reason in {"global_deadline", "request_limit", "origin_limit"}:
                report["diagnostic_complete"] = False
            return b"", []
        origins.add(request_origin)
        requested.add(url)
        timeout = min(REQUEST_TIMEOUT_SECONDS, remaining)
        try:
            body, advertised = await asyncio.wait_for(fetch_once(result, timeout, transport), timeout=timeout)
        except TimeoutError as error:
            global_expired = remaining <= REQUEST_TIMEOUT_SECONDS
            result.update(status="transport_error", reason="global_deadline" if global_expired else "request_timeout",
                          error_class=type(error).__name__)
            report["diagnostic_complete"] &= not global_expired
            body, advertised = b"", []
        except (httpx.HTTPError, ValueError) as error:
            result.update(status="transport_error", reason="request_error", error_class=type(error).__name__)
            body, advertised = b"", []
        if result["status"] == "blocked":
            blocked.add(request_origin)
        return body, advertised

    for agency in agencies:
        source = agency["source_url"]
        result = {"agency_id": agency["id"], "agency_source_url": source,
                  "primary_reference_url": agency["primary_reference_url"], "outcome": None,
                  "discovery": empty_observation(source), "resource": None}
        report["results"].append(result)
        if not public_url(source, source) or urlsplit(source).scheme != "https":
            result["outcome"] = "invalid_source"
            report["diagnostic_complete"] = False
            continue
        body, advertised = await observe(result["discovery"])
        discovery = result["discovery"]
        if discovery["status"] != "observed":
            result["outcome"] = discovery["reason"] or discovery["status"]
            continue
        if discovery["content_type"] != "text/html":
            result["outcome"] = "discovery_not_html"
            continue
        facts = html_facts(body, source)
        advertised.extend(facts.candidates)
        unique = {entry["url"]: entry for entry in advertised if entry["url"] != source}
        discovery["observations"] = {"advertised_feed_count": sum(entry["kind"] != "restapi" for entry in unique.values()),
                                     "advertised_api_count": sum(entry["kind"] == "restapi" for entry in unique.values()),
                                     "idealista_property_link_count": len(facts.idealista_links),
                                     "idealista_property_links": sorted(facts.idealista_links)[:MAX_SAVED_PROPERTY_LINKS]}
        if not unique:
            result["outcome"] = "no_advertised_resource"
            continue
        selected = min(unique.values(), key=lambda entry: (entry["kind"] == "restapi", entry["kind"] != "rss"))
        resource = result["resource"] = empty_observation(selected["url"])
        resource["advertised_by"] = source
        resource["advertisement"] = {key: selected[key] for key in ("method", "rel", "media_type")}
        body, _ = await observe(resource)
        if resource["status"] != "observed":
            result["outcome"] = resource["reason"] or resource["status"]
            continue
        try:
            summary = resource["observations"] = summarize_resource(body, resource["content_type"], selected["url"])
            result["outcome"] = ("documentation_only" if summary["format"] == "public_api_index" else
                                 "listing_candidates" if summary["listing_candidates"] else
                                 "syndicated_entries_only" if summary["record_count"] else "no_records")
        except (ValueError, ElementTree.ParseError, UnicodeDecodeError) as error:
            resource.update(status="invalid_document", reason="parse_error", error_class=type(error).__name__)
            result["outcome"] = "invalid_document"
    report["requests_performed"] = len(requested)
    report["origins_requested"] = len(origins)
    report["status"] = "complete" if report["diagnostic_complete"] else "partial"
    return report


def write_report(output, report):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=".agency-report-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = asyncio.run(run_probe())
    write_report(args.output, report)
    return 0 if report["diagnostic_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
