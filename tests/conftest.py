import pytest


@pytest.fixture
def listing():
    return {
        "id": "idealista:10001", "property_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "portal": "idealista",
        "url": "https://www.idealista.com/inmueble/10001/", "title": "Piso", "description": "Luminoso",
        "lang": "es", "price": 1250, "images": [], "advertiser": None, "status": "active",
        "first_seen": "2026-10-12T12:00:00Z", "last_seen": "2026-10-12T12:00:00Z", "last_checked": "2026-10-12T12:00:00Z",
        "property": {"operation": "rent", "kind": "flat", "province": "28", "municipality": "28079", "city_slug": "madrid",
            "district": "Centro", "lat": None, "lng": None, "area_m2": 80, "rooms": 2, "baths": 1,
            "floor": None, "has_lift": None, "has_parking": None, "has_terrace": None, "furnished": None, "cover_image": None},
    }
