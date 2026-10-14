from pathlib import Path

import httpx
import pytest
from selectolax.parser import HTMLParser

from index_scrapers.runner import _next_link, _pagination, discover
from index_scrapers.state import State


NOW = "2026-10-14T14:00:00Z"
TARGET = {
    "portal": "idealista", "operation": "rent", "province": "28",
    "municipality": "28079", "city_slug": "madrid", "priority": "major",
    "url": "https://www.idealista.com/alquiler-viviendas/madrid-madrid/",
}
LISTING_HTML = (Path(__file__).parent / "fixtures/idealista.synthetic.html").read_text(encoding="utf-8")
# Public pagination observed on Madrid rental page 34 on 2026-10-14.
OBSERVED_PAGINATION_HTML = '''<div class="pagination"><ul>
<li class="prev"><a rel="nofollow" class="icon-arrow-left" href="/alquiler-viviendas/madrid-madrid/pagina-33.htm"><span>Anterior</span></a></li>
<li><a rel="nofollow" class="" href="/alquiler-viviendas/madrid-madrid/">1</a></li>
<li>...</li><li class="selected"><span>34</span></li>
<li><a rel="nofollow" class="" href="/alquiler-viviendas/madrid-madrid/pagina-35.htm">35</a></li>
<li><a rel="nofollow" class="" href="/alquiler-viviendas/madrid-madrid/pagina-36.htm">36</a></li>
<li><a rel="nofollow" class="" href="/alquiler-viviendas/madrid-madrid/pagina-37.htm">37</a></li>
<li><a rel="nofollow" class="" href="/alquiler-viviendas/madrid-madrid/pagina-38.htm">38</a></li>
<li class="next"><a rel="nofollow" class="icon-arrow-right-after" href="/alquiler-viviendas/madrid-madrid/pagina-35.htm"><span>Siguiente</span></a></li>
</ul></div>'''


@pytest.mark.parametrize("number,total,location", [
    ("8.573", 8573, "Madrid"),
    ("10.342", 10342, "Madrid provincia"),
    ("8\u00a0573", 8573, "Madrid"),
    ("10\u202f342", 10342, "Madrid provincia"),
])
def test_idealista_advertised_total_uses_spanish_number_format(number, total, location):
    tree = HTMLParser(f'<h1><span>{number}</span> casas y pisos en alquiler en {location}</h1>')
    assert _pagination(tree, "idealista") == {"total_results": total, "has_more": True}


def test_idealista_card_count_does_not_establish_page_capacity_or_total_pages():
    cards = "".join(f'<article class="item" data-element-id="{identifier}"></article>' for identifier in range(30))
    tree = HTMLParser('<h1>8.573 casas y pisos en alquiler en Madrid</h1>' + cards)
    assert _pagination(tree, "idealista") == {"total_results": 8573, "has_more": True}


def test_idealista_observed_current_page_and_next_link_do_not_imply_total_pages():
    tree = HTMLParser('<h1>8.576 casas y pisos en alquiler en Madrid</h1>' + OBSERVED_PAGINATION_HTML)
    assert _pagination(tree, "idealista") == {"page": 34, "total_results": 8576, "has_more": True}
    assert _next_link(tree).attributes["href"] == "/alquiler-viviendas/madrid-madrid/pagina-35.htm"


def test_idealista_observed_navigation_preserves_unknown_total():
    assert _pagination(HTMLParser(OBSERVED_PAGINATION_HTML), "idealista") == {"page": 34, "has_more": True}


@pytest.mark.parametrize("marker", [
    '<a rel="next" href="/original">Siguiente</a>',
    '<link rel="next" href="/original">',
    '<a aria-label="Siguiente" href="/original">Next</a>',
    '<a title="Siguiente" href="/original">Next</a>',
])
def test_observed_next_selector_preserves_existing_next_marker_priority(marker):
    tree = HTMLParser(OBSERVED_PAGINATION_HTML + marker)
    assert _next_link(tree).attributes["href"] == "/original"


@pytest.mark.parametrize("number", ["0", "", "34 de 286"])
def test_idealista_current_page_remains_unknown_without_a_positive_integer(number):
    tree = HTMLParser(f'<div class="pagination"><ul><li class="selected"><span>{number}</span></li></ul></div>')
    assert _pagination(tree, "idealista") == {}


@pytest.mark.parametrize("html", [
    "<html></html>",
    "<h1>Casas y pisos en alquiler en Madrid</h1>",
    "<h1>Madrid 2026</h1>",
    "<h1>8.57 casas y pisos en alquiler en Madrid</h1>",
    "<p>8.573 casas y pisos en alquiler en Madrid</p>",
])
def test_idealista_total_remains_unknown_without_an_unambiguous_heading(html):
    assert _pagination(HTMLParser(html), "idealista") == {}


def test_idealista_advertised_total_without_next_link_blocks_initial_completion(tmp_path):
    html = LISTING_HTML + '<h1>8.573 casas y pisos en alquiler en Madrid</h1>'
    path = tmp_path / "state.sqlite"
    key = "initial:idealista:28079:rent"
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))) as client:
        report = discover(state, client, [TARGET], NOW, max_pages=1, hash_images=False, resume=True)
        assert report["initial"]["status"] == "blocked"
        assert state.pending_count() == 1
    with State(path) as state:
        cursor = state.progress(key)
        assert cursor["done"] is False
        assert cursor["status"] == "blocked"
        assert cursor["reason"] == "pagination_missing"
        assert cursor["pagination"] == {"total_results": 8573, "has_more": True}


def test_idealista_initial_cursor_follows_observed_next_then_blocks_unproven_exhaustion(tmp_path):
    first_url = TARGET["url"] + "pagina-34.htm"
    next_url = TARGET["url"] + "pagina-35.htm"
    target = {**TARGET, "url": first_url}
    heading = '<h1>8.576 casas y pisos en alquiler en Madrid</h1>'
    first_html = LISTING_HTML + heading + OBSERVED_PAGINATION_HTML
    last_html = LISTING_HTML.replace("10001", "10003") + heading + '<div class="pagination"><ul><li class="selected"><span>35</span></li></ul></div>'
    requested = []

    def endpoint(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=first_html if str(request.url) == first_url else last_html)

    path = tmp_path / "state.sqlite"
    key = "initial:idealista:28079:rent"
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        report = discover(state, client, [target], NOW, max_pages=1, hash_images=False, resume=True)
        assert report["initial"]["status"] == "pending"
        cursor = state.progress(key)
        assert cursor["url"] == next_url
        assert cursor["pagination"] == {"page": 34, "total_results": 8576, "has_more": True}
    with State(path) as state, httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        report = discover(state, client, [target], NOW, max_pages=1, hash_images=False, resume=True)
        assert requested == [first_url, next_url]
        assert report["initial"]["status"] == "blocked"
        cursor = state.progress(key)
        assert cursor["reason"] == "pagination_missing"
        assert cursor["done"] is False
        assert cursor["pages"] == 2
        assert cursor["pagination"] == {"page": 35, "total_results": 8576, "has_more": True}
