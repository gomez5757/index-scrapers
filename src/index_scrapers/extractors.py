"""HTML adapters; blocked pages never masquerade as an empty catalogue."""
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.parser import HTMLParser

DOMAINS = {"idealista": "www.idealista.com", "fotocasa": "www.fotocasa.es", "pisoscom": "www.pisos.com", "habitaclia": "www.habitaclia.com"}
IMAGE_DOMAINS = {"idealista.com", "idealista.net", "fotocasa.es", "habitaclia.com", "pisos.com", "imghs.net"}


def trusted_image_url(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        valid = url.scheme == "https" and not url.username and not url.password and url.port in {None, 443}
        valid = valid and any(host == domain or host.endswith("." + domain) for domain in IMAGE_DOMAINS)
        return value if valid and "placeholder" not in value.lower() else None
    except ValueError:
        return None


def initial_data(tree):
    for script in tree.css("script"):
        content = script.text()
        if "window.__INITIAL_PROPS__" in content and "JSON.parse(" in content:
            try:
                literal = json.JSONDecoder().raw_decode(content[content.index("JSON.parse(") + 11:])[0]
                return json.loads(literal)
            except (ValueError, TypeError):
                return None
    return None


def detail_price(portal, html, listing_id):
    tree = unblocked_tree(portal, html)
    if portal == "idealista":
        value = match_number(r"([\d.,]+)\s*€", text(tree.root, ".info-data-price"))
    elif portal == "pisoscom":
        value = match_number(r"por\s+([\d.,]+)\s*€", text(tree.root, "h1"))
    elif portal == "fotocasa":
        script = tree.css_first("#__initial_props__")
        try:
            entity = json.loads(script.text())["realEstate"]
            value = number(entity["price"]) if listing_id == f"fotocasa:{entity['id']}" else None
        except (AttributeError, ValueError, KeyError, TypeError):
            value = None
    else:
        value = None
        for script in tree.css("script"):
            content = script.text()
            if "window.__INITIAL_PROPS__" not in content or "JSON.parse(" not in content:
                continue
            try:
                raw = content[content.index("JSON.parse(") + 11:]
                literal = json.JSONDecoder().raw_decode(raw)[0]
                entity = json.loads(literal)["listing"]
                if listing_id == f"habitaclia:{entity['id']}":
                    value = number(entity["transaction"]["price"]["amount"])
            except (ValueError, KeyError, TypeError):
                continue
    return value if value is not None and value > 0 else None


class PortalBlocked(RuntimeError):
    pass


def number(value):
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    text = re.sub(r"[^\d.,]", "", str(value))
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        text = text.replace(".", "")
    try:
        value = Decimal(text)
        return int(value) if value.is_finite() and value >= 0 else None
    except InvalidOperation:
        return None


def trusted_url(portal, value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(urljoin(f"https://{DOMAINS[portal]}", value))
        valid = url.scheme == "https" and url.hostname in {DOMAINS[portal], DOMAINS[portal].removeprefix("www.")} and not url.username and not url.password and url.port in {None, 443}
    except ValueError:
        return None
    if not valid:
        return None
    return urlunsplit((url.scheme, url.netloc, url.path, "", ""))


def kind(title):
    text = (title or "").lower()
    for words, name in [(('estudio', 'estudi'), 'studio'), (('ático', 'atico', 'àtic'), 'penthouse'), (('dúplex', 'duplex'), 'duplex'), (('habitación', 'habitació'), 'room'), (('casa', 'chalet', 'adosado', 'torre'), 'house')]:
        if any(word in text for word in words):
            return name
    return "flat" if any(word in text for word in ('piso', 'pis ', 'apartamento', 'apartment')) else "other"


def row(portal, identifier, url, title, price, context, lang, **data):
    url = trusted_url(portal, url)
    price = number(price)
    if not identifier or not url or price is None or price <= 0:
        return None
    images = data.pop("images", [])
    if isinstance(images, str):
        images = [images]
    images = list(dict.fromkeys(value for value in images if trusted_image_url(value)))[:30]
    prop = {"operation": context["operation"], "kind": kind(title), "province": context["province"], "municipality": context["municipality"], "city_slug": context["city_slug"],
        "district": None, "lat": None, "lng": None, "area_m2": None, "rooms": None, "baths": None, "floor": None,
        "has_lift": None, "has_parking": None, "has_terrace": None, "furnished": None, "cover_image": images[0] if images else None}
    prop.update({key: value for key, value in data.items() if key in prop})
    if prop["district"]:
        prop["district"] = prop["district"][:120]
    return {"id": f"{portal}:{identifier}", "portal": portal, "url": url, "title": (title or "")[:300], "description": data.get("description", "")[:20000], "lang": lang,
        "price": price, "images": images, "advertiser": data.get("advertiser"), "status": "active", "property": prop}


def text(node, selector=None):
    child = node.css_first(selector) if selector else node
    return child.text(separator=" ", strip=True) if child else ""


def match_number(pattern, value):
    found = re.search(pattern, value, re.IGNORECASE)
    return number(found.group(1)) if found else None


def locality_matches(value, context):
    if not value:
        return False
    normalize = lambda v: re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", str(v).casefold()).encode("ascii", "ignore").decode()).removesuffix("capital")
    return normalize(value) == normalize(context.get("name") or context["city_slug"])


def operation_from_url(value):
    path = urlsplit(str(value)).path.lower()
    if any(part in path for part in ("/alquiler/", "/alquilar/", "/alquiler-")):
        return "rent"
    if any(part in path for part in ("/comprar/", "/venta/", "/comprar-", "/venta-")):
        return "sale"
    return None


def idealista(tree, context, lang):
    result = []
    for card in tree.css("article.item"):
        link = card.css_first("a.item-link")
        if not link:
            continue
        title = link.attributes.get("title") or text(link)
        if not locality_matches(title.rsplit(",", 1)[-1].strip(), context):
            continue
        price_text = text(card, ".item-price")
        monthly = "/mes" in price_text.replace(" ", "").lower()
        if monthly != (context["operation"] == "rent"):
            continue
        detail = " ".join(text(node) for node in card.css(".item-detail"))
        lift = False if "sin ascensor" in detail.lower() else True if "con ascensor" in detail.lower() else None
        item = row("idealista", card.attributes.get("data-element-id"), link.attributes.get("href"), title, price_text, context, lang,
            area_m2=match_number(r"([\d.,]+)\s*m[²2]", detail), rooms=match_number(r"(\d+)\s*hab", detail), has_lift=lift,
            district=text(card, ".item-location") or None, description=text(card, ".item-description"),
            images=[node.attributes.get("data-src") or node.attributes.get("src") for node in card.css("img")])
        if item:
            result.append(item)
    return result


def panot_cards(portal, tree, context, lang):
    """Fotocasa/Habitaclia server-rendered Panot cards, verified on saved captures."""
    result = []
    for card in tree.css("article"):
        link = card.css_first('a[data-panot-component="link-box-link"]')
        if not link:
            continue
        url = trusted_url(portal, link.attributes.get("href"))
        if not url:
            continue
        if operation_from_url(url) != context["operation"]:
            continue
        identifier = urlsplit(url).path.rstrip("/").split("/")[-2]
        title = link.attributes.get("title") or text(link)
        content = text(card)
        price_nodes = [text(node) for node in card.css("span") if "€" in text(node) and len(text(node)) < 60]
        price = match_number(r"([\d.,]+)\s*€", price_nodes[0]) if price_nodes else None
        location = text(card, "span.truncate") if portal == "habitaclia" else ""
        if not locality_matches(location.rsplit(",", 1)[-1].strip(), context):
            continue
        description = max((text(node) for node in card.css("p")), key=len, default="")
        item = row(portal, identifier, url, title, price, context, lang, area_m2=match_number(r"([\d.,]+)\s*m[²2]", content),
            rooms=match_number(r"(\d+)\s*hab", content), baths=match_number(r"(\d+)\s*ba[ñn]", content),
            district=location.split(",")[0].strip() or None, description=description,
            images=[node.attributes.get("src") for node in card.css("picture img")])
        if item:
            result.append(item)
    return result


def fotocasa_initial(tree, context, lang):
    script = tree.css_first("#__initial_props__")
    if not script:
        return []
    try:
        data = json.loads(script.text())["initialSearch"]["result"]["realEstates"]
    except (ValueError, KeyError, TypeError):
        return []
    result = []
    for estate in data:
        if estate.get("transactionTypeId") is not None and estate["transactionTypeId"] != (3 if context["operation"] == "rent" else 1):
            continue
        features = {feature["key"]: feature["value"] for feature in estate.get("features", []) if "key" in feature and "value" in feature}
        address = estate.get("address") or {}
        locality = address.get("municipality") or address.get("city")
        if not locality_matches(locality, context):
            continue
        coordinates = estate.get("coordinates") or {}
        detail = estate.get("detail") or {}
        url = detail.get("es-ES") or next(iter(detail.values()), None)
        if url and ("/comprar/" in url and context["operation"] != "sale" or "/alquiler/" in url and context["operation"] != "rent"):
            continue
        item = row("fotocasa", estate.get("id"), url, estate.get("title") or estate.get("location") or "", estate.get("rawPrice"), context, lang,
            description=estate.get("description") or "", advertiser=estate.get("clientAlias"), district=address.get("neighborhood") or address.get("district"),
            lat=coordinates.get("latitude"), lng=coordinates.get("longitude"), area_m2=number(features.get("surface")), rooms=number(features.get("rooms")), baths=number(features.get("bathrooms")),
            floor=str(features["floor"]) if "floor" in features else None,
            has_lift=bool(features["lift"]) if "lift" in features else None, has_parking=bool(features["parking"]) if "parking" in features else None,
            has_terrace=bool(features["terrace"]) if "terrace" in features else None, furnished=bool(features["furnished"]) if "furnished" in features else None,
            images=[media.get("src") for media in estate.get("multimedia", []) if media.get("type") == "image"])
        if item:
            item["property"]["kind"] = {"Flat": "flat", "House": "house", "Studio": "studio", "Penthouse": "penthouse", "Duplex": "duplex", "Room": "room"}.get(estate.get("buildingSubtype"), item["property"]["kind"])
            result.append(item)
    return result


def habitaclia_initial(tree, context, lang):
    data = initial_data(tree)
    if not data or "initialSearchResultsPage" not in data:
        return None
    try:
        search = data["initialSearchResultsPage"]["initialSearchContext"]
        if str(search["geography"]["code"]).zfill(5) != context["municipality"] or {"rent": "rent", "buy": "sale", "sale": "sale"}.get(search["filters"]["transactionType"]) != context["operation"]:
            return []
        entities = search["results"]["items"]
    except (KeyError, TypeError):
        return []
    titles = {item["id"]: item["title"] for item in panot_cards("habitaclia", tree, context, lang)}
    result = []
    for entity in entities:
        summary = entity.get("summary") or {}
        location = summary.get("location") or {}
        geography = location.get("geography") or []
        municipal = next((str(item["code"]).zfill(5) for item in geography if item.get("level") == "municipality"), None)
        transaction = entity.get("transaction") or {}
        if municipal != context["municipality"] or {"rent": "rent", "buy": "sale", "sale": "sale"}.get(transaction.get("type")) != context["operation"]:
            continue
        prop = entity.get("property") or {}
        features = prop.get("features") or {}
        feature = lambda name: True if name in features.get("has", []) else False if name in features.get("hasNot", []) else None
        coordinates = location.get("coordinates") or {}
        neighborhood = next((item.get("value") for item in location.get("layers", []) if item.get("type") == "neighbourhood"), None)
        item = row("habitaclia", entity.get("id"), entity.get("navigationUrl"), titles.get(f"habitaclia:{entity.get('id')}") or summary.get("title") or "", (transaction.get("price") or {}).get("amount"), context, lang,
            description=summary.get("description") or "", advertiser=(summary.get("publisher") or {}).get("name"), district=neighborhood or location.get("district"),
            lat=coordinates.get("latitude"), lng=coordinates.get("longitude"), area_m2=number(prop.get("builtSurface")), rooms=number(prop.get("rooms")), baths=number(prop.get("bathrooms")), floor=prop.get("floor"),
            has_lift=feature("ELEVATOR"), has_parking=feature("PRIVATE_PARKING"), has_terrace=feature("TERRACE"), furnished=feature("FURNISHED"),
            images=[media.get("url") for media in (summary.get("multimedia") or {}).get("images", [])])
        if item:
            item["property"]["kind"] = prop.get("propertySubtype") if prop.get("propertySubtype") in {"flat", "house", "studio", "penthouse", "duplex", "room"} else item["property"]["kind"]
            result.append(item)
    return result


def pisoscom(tree, context, lang):
    result = []
    for card in tree.css(".ad-preview"):
        link = card.css_first("a.ad-preview__title")
        if not link:
            continue
        if operation_from_url(link.attributes.get("href")) != context["operation"]:
            continue
        details = " ".join(text(node) for node in card.css(".ad-preview__char"))
        geo = {}
        script = card.css_first('script[type="application/ld+json"]')
        if script:
            try:
                entity = json.loads(script.text())
                if not locality_matches((entity.get("address") or {}).get("addressLocality"), context):
                    continue
                geo = entity.get("geo") or {}
            except (ValueError, TypeError):
                continue
        else:
            continue
        coordinates = {}
        for output, source in (("lat", "latitude"), ("lng", "longitude")):
            try:
                coordinates[output] = float(geo[source])
            except (KeyError, ValueError, TypeError):
                coordinates[output] = None
        item = row("pisoscom", card.attributes.get("id"), link.attributes.get("href"), text(link), text(card, ".ad-preview__price"), context, lang,
            area_m2=match_number(r"([\d.,]+)\s*m[²2]", details), rooms=match_number(r"(\d+)\s*hab", details), baths=match_number(r"(\d+)\s*ba[ñn]", details),
            district=text(card, ".ad-preview__subtitle").split(" (")[0] or None, description=text(card, ".ad-preview__description"),
            images=[node.attributes.get("data-src") or node.attributes.get("src") for node in card.css(".carousel img")], **coordinates)
        if item:
            result.append(item)
    return result


def structured(portal, tree, context, lang):
    result = []

    def walk(value):
        if isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, dict):
            offers = value.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict) and offers.get("priceCurrency", "EUR") == "EUR" and value.get("url"):
                entity = value.get("mainEntity") or value
                if not isinstance(entity, dict):
                    return
                locality = (entity.get("address") or {}).get("addressLocality")
                if not locality_matches(locality, context):
                    return
                if operation_from_url(value["url"]) != context["operation"]:
                    return
                seller = offers.get("seller") or {}
                geo = entity.get("geo") or {}
                identifier = value.get("identifier")
                if isinstance(identifier, dict):
                    identifier = identifier.get("value")
                if identifier is None:
                    path = urlsplit(str(value["url"])).path
                    match = re.search(r"(?:/|[-i])(\d{4,})(?:/d|/|\.htm|$)", path)
                    identifier = match.group(1) if match else None
                item = row(portal, identifier, value["url"], value.get("name"), offers.get("price"), context, lang,
                    description=value.get("description") or entity.get("description") or "", images=entity.get("image", value.get("image", [])),
                    area_m2=number(entity.get("floorSize")), rooms=number(entity.get("numberOfRooms")), baths=number(entity.get("numberOfBathroomsTotal")),
                    lat=geo.get("latitude"), lng=geo.get("longitude"), advertiser=seller.get("name"))
                if item:
                    result.append(item)
            else:
                for key in ("@graph", "itemListElement", "item"):
                    if key in value:
                        walk(value[key])

    for script in tree.css('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.text()))
        except (ValueError, TypeError):
            continue
    return result


def unblocked_tree(portal, html):
    if portal not in DOMAINS:
        raise ValueError("unknown portal")
    tree = HTMLParser(html)
    title = text(tree.root, "title").lower()
    body = text(tree.root, "body").lower()
    if "access denied" in title or "captcha" in body and len(body) < 3000 or "enable javascript" in body and len(body) < 3000 or "captcha-delivery.com" in html and len(html) < 3000:
        raise PortalBlocked(f"{portal}: HTTP challenge")
    return tree


def parse(portal, html, context):
    tree = unblocked_tree(portal, html)
    root = tree.css_first("html")
    lang = (root.attributes.get("lang", "es") if root else "es").split("-")[0]
    if portal == "idealista":
        rows = idealista(tree, context, lang)
    elif portal in {"fotocasa", "habitaclia"}:
        if portal == "fotocasa" and tree.css_first("#__initial_props__"):
            return fotocasa_initial(tree, context, lang)
        if portal == "habitaclia":
            authoritative = habitaclia_initial(tree, context, lang)
            if authoritative is not None:
                return authoritative
        rows = panot_cards(portal, tree, context, lang)
    else:
        rows = pisoscom(tree, context, lang)
    rows.extend(structured(portal, tree, context, lang))
    return list({item["id"]: item for item in rows}.values())
