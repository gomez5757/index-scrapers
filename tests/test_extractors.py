from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures"
CONTEXT = {"operation": "rent", "province": "28", "municipality": "28079", "city_slug": "madrid"}


def extract(portal, fixture):
    from index_scrapers.extractors import parse
    return parse(portal, (FIXTURES / fixture).read_text(encoding="utf-8"), CONTEXT)


def test_idealista_extracts_euros_area_rooms_and_skips_unknown_price():
    rows = extract("idealista", "idealista.synthetic.html")
    assert len(rows) == 1
    assert rows[0]["id"] == "idealista:10001"
    assert rows[0]["price"] == 1250
    assert rows[0]["url"] == "https://www.idealista.com/inmueble/10001/"
    assert rows[0]["property"]["area_m2"] == 80
    assert rows[0]["property"]["rooms"] == 2
    assert rows[0]["property"]["has_lift"] is True
    assert rows[0]["property"]["district"] == "Centro"


@pytest.mark.parametrize("portal,identifier,price,area,rooms,lang", [
    ("fotocasa", "20001", 1200, 75, 2, "es"),
    ("pisoscom", "30001", 900, 40, 0, "es"),
    ("habitaclia", "40001", 1100, 65, 2, "ca"),
])
def test_structured_portals_normalize_saved_html(portal, identifier, price, area, rooms, lang):
    row, = extract(portal, f"{portal}.synthetic.html")
    assert row["id"] == f"{portal}:{identifier}"
    assert row["price"] == price
    assert row["property"]["area_m2"] == area
    assert row["property"]["rooms"] == rooms
    assert row["lang"] == lang
    assert row["property"]["municipality"] == "28079"


@pytest.mark.parametrize("portal", ["idealista", "fotocasa", "pisoscom", "habitaclia"])
def test_captcha_is_a_blocker_not_an_empty_catalogue(portal):
    from index_scrapers.extractors import PortalBlocked
    with pytest.raises(PortalBlocked):
        extract(portal, "blocked.synthetic.html")


def test_external_listing_urls_are_not_trusted():
    from index_scrapers.extractors import parse
    html = (FIXTURES / "fotocasa.synthetic.html").read_text().replace("www.fotocasa.es/es/alquiler", "evil.example/es/alquiler")
    assert parse("fotocasa", html, CONTEXT) == []


@pytest.mark.parametrize("portal,identifier,price,area,rooms", [
    ("fotocasa", "191088451", 1495, 52, 2),
    ("habitaclia", "78370e0d-4a95-4823-882c-69ec4a07c080", 2500, 94, 2),
])
def test_real_http_capture_extracts_known_first_card(portal, identifier, price, area, rooms):
    rows = extract(portal, f"captures/{portal}.http-200.html")
    assert len(rows) >= 20
    row = next(row for row in rows if row["id"] == f"{portal}:{identifier}")
    assert row["price"] == price
    assert row["property"]["area_m2"] == area
    assert row["property"]["rooms"] == rooms
    assert row["images"]


def test_real_idealista_challenge_is_reported():
    from index_scrapers.extractors import PortalBlocked
    with pytest.raises(PortalBlocked):
        extract("idealista", "captures/idealista.http-403.html")


def test_real_idealista_browser_card_has_photos_without_map_placeholder():
    row, = extract("idealista", "captures/idealista.browser-20261012.html")
    assert row["id"] == "idealista:101920558"
    assert row["price"] == 1000
    assert row["property"]["area_m2"] == 48
    assert row["property"]["rooms"] == 1
    assert row["property"]["has_lift"] is True
    assert len(row["images"]) == 2
    assert all("placeholder" not in value for value in row["images"])


def test_real_pisoscom_browser_card_uses_portal_id_and_excludes_agency_logo():
    row, = extract("pisoscom", "captures/pisoscom.browser-20261012.html")
    assert row["id"] == "pisoscom:67590000637.101000"
    assert row["price"] == 2200
    assert row["property"]["area_m2"] == 170
    assert row["property"]["rooms"] == 3
    assert row["property"]["baths"] == 2
    assert row["property"]["lat"] == pytest.approx(40.4023888)
    assert len(row["images"]) == 2
    assert all("logos" not in value for value in row["images"])


def test_fotocasa_neighbouring_city_is_not_assigned_target_municipality():
    from index_scrapers.extractors import parse
    import json
    estate = {"id": 9, "rawPrice": 900, "location": "Centro", "address": {"municipality": "Toledo", "city": "Toledo"},
        "detail": {"es-ES": "/es/alquiler/vivienda/toledo/9/d"}, "features": [], "multimedia": []}
    data = json.dumps({"initialSearch": {"result": {"realEstates": [estate]}}})
    assert parse("fotocasa", f'<script id="__initial_props__">{data}</script>', CONTEXT) == []


@pytest.mark.parametrize("portal,fixture,identifier,price", [
    ("fotocasa", "fotocasa.detail.http-200.html", "191088451", 1495),
    ("habitaclia", "habitaclia.detail.http-200.html", "78370e0d-4a95-4823-882c-69ec4a07c080", 2500),
    ("idealista", "idealista.detail-browser-20261012.html", "101920558", 1000),
])
def test_real_detail_page_supplies_price_for_reviews(portal, fixture, identifier, price):
    from index_scrapers.extractors import detail_price
    html = (FIXTURES / "captures" / fixture).read_text(encoding="utf-8")
    assert detail_price(portal, html, f"{portal}:{identifier}") == price


def test_pisoscom_detail_title_price_is_explicitly_synthetic():
    from index_scrapers.extractors import detail_price
    html = '<h1>Piso en alquiler en Calle de Téllez en Pacífico por 2.200 €/mes</h1>'
    assert detail_price("pisoscom", html, "pisoscom:67590000637.101000") == 2200


def test_detail_challenge_does_not_read_price_from_recommended_cards():
    from index_scrapers.extractors import detail_price, PortalBlocked
    with pytest.raises(PortalBlocked):
        detail_price("idealista", '<title>Access denied</title><body>CAPTCHA<span class="info-data-price">1000</span></body>', "idealista:1")


@pytest.mark.parametrize("url", ["https://evil.example/photo.jpg", "https://secret@img.idealista.com/a.jpg", "https://img.idealista.com:444/a.jpg"])
def test_image_urls_reject_external_hosts_credentials_and_nonstandard_ports(url):
    from index_scrapers.extractors import parse
    html = (FIXTURES / "idealista.synthetic.html").read_text(encoding="utf-8").replace("https://img.idealista.com/fixture.jpg", url)
    row, = parse("idealista", html, CONTEXT)
    assert row["images"] == []
    assert row["property"]["cover_image"] is None


def test_fotocasa_card_cannot_bypass_authoritative_hydration_scope():
    from index_scrapers.extractors import parse
    import json
    estate = {"id": 9, "rawPrice": 900, "location": "Centro", "address": {"municipality": "Toledo"}, "detail": {"es-ES": "/es/alquiler/vivienda/toledo/9/d"}, "features": [], "multimedia": []}
    data = json.dumps({"initialSearch": {"result": {"realEstates": [estate]}}})
    card = '<article><a data-panot-component="link-box-link" href="/es/alquiler/vivienda/toledo/9/d">Piso</a><span>900 €</span></article>'
    assert parse("fotocasa", card + f'<script id="__initial_props__">{data}</script>', CONTEXT) == []


def test_fotocasa_sale_recommendation_cannot_become_rental():
    from index_scrapers.extractors import parse
    import json
    estate = {"id": 9, "rawPrice": 90000, "transactionTypeId": 1, "address": {"municipality": "Madrid"}, "detail": {"es-ES": "/es/comprar/vivienda/madrid/9/d"}, "features": [], "multimedia": []}
    data = json.dumps({"initialSearch": {"result": {"realEstates": [estate]}}})
    assert parse("fotocasa", f'<script id="__initial_props__">{data}</script>', CONTEXT) == []


def test_habitaclia_real_hydration_supplies_coordinates_and_features_for_dedup():
    rows = extract("habitaclia", "captures/habitaclia.http-200.html")
    row = next(row for row in rows if row["id"] == "habitaclia:78370e0d-4a95-4823-882c-69ec4a07c080")
    assert row["property"]["lat"] == pytest.approx(40.4043377)
    assert row["property"]["lng"] == pytest.approx(-3.7069029)
    assert row["property"]["has_lift"] is True
    assert row["property"]["furnished"] is True
    assert row["property"]["district"] == "Acacias"
    assert len(row["description"]) > 100


@pytest.mark.parametrize("address", ["", '"address":{"addressLocality":"Barcelona"},'])
def test_structured_fallback_without_verified_municipality_is_rejected(address):
    from index_scrapers.extractors import parse
    html = '<script type="application/ld+json">{"@type":"Product","identifier":"1","url":"https://www.habitaclia.com/alquiler-piso-barcelona-i1.htm",' + address + '"offers":{"price":1000}}</script>'
    assert parse("habitaclia", html, CONTEXT) == []


def test_panot_card_without_verified_municipality_is_rejected():
    from index_scrapers.extractors import parse
    html = '<article><a data-panot-component="link-box-link" href="/alquiler/planta-intermedia/barcelona/123/d">Piso</a><span>900 €</span></article>'
    assert parse("habitaclia", html, CONTEXT) == []


def test_habitaclia_buy_source_operation_is_normalized_to_sale():
    from index_scrapers.extractors import parse
    import json
    entity = {"id": "sale-1", "navigationUrl": "/comprar/piso/madrid/sale-1/d", "summary": {"title": "Piso", "location": {"geography": [{"level": "municipality", "code": "28079"}]}},
        "property": {"builtSurface": 80, "rooms": 2}, "transaction": {"type": "buy", "price": {"amount": 125000}}}
    data = {"initialSearchResultsPage": {"initialSearchContext": {"geography": {"code": "28079"}, "filters": {"transactionType": "buy"}, "results": {"items": [entity]}}}}
    literal = json.dumps(json.dumps(data))
    rows = parse("habitaclia", f'<script>window.__INITIAL_PROPS__ = JSON.parse({literal});</script>', {**CONTEXT, "operation": "sale"})
    assert len(rows) == 1
    assert rows[0]["property"]["operation"] == "sale"
    assert rows[0]["price"] == 125000


def test_idealista_sale_price_cannot_become_monthly_rental():
    from index_scrapers.extractors import parse
    html = (FIXTURES / "idealista.synthetic.html").read_text(encoding="utf-8").replace("<span>/mes</span>", "")
    assert parse("idealista", html, CONTEXT) == []


def test_panot_purchase_card_cannot_become_rental():
    from index_scrapers.extractors import parse
    html = '<article><a data-panot-component="link-box-link" href="/comprar/planta-intermedia/madrid/123/d">Piso</a><span>90000 €</span><span class="truncate">Centro, Madrid Capital</span></article>'
    assert parse("habitaclia", html, CONTEXT) == []


def test_pisoscom_other_city_card_cannot_copy_target_municipality():
    from index_scrapers.extractors import parse
    html = (FIXTURES / "captures/pisoscom.browser-20261012.html").read_text(encoding="utf-8").replace('"addressLocality":"Madrid Capital"', '"addressLocality":"Barcelona Capital"')
    assert parse("pisoscom", html, CONTEXT) == []
