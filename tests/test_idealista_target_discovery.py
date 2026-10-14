import hashlib
import json
from html import escape
from pathlib import Path

import httpx
import pytest

from index_scrapers.target_discovery import DEFAULT_SEEDS, MunicipalityIndex, parse_directory, run_discovery


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/targets"
IDEALISTA = "https://www.idealista.com"
DIRECTORY = IDEALISTA + "/long-term-rentals-in-spain"
SALE_DIRECTORY = IDEALISTA + "/properties-for-sale-in-spain"


@pytest.fixture(scope="module")
def geography():
    return MunicipalityIndex.from_csv(
        ROOT / "data/municipalities.csv",
        provinces_html=FIXTURES / "ine-provinces.directory.html",
        capitals_html=FIXTURES / "boe-capitals.html",
        major_codes={"28079"},
    )


@pytest.fixture(scope="module")
def observed_subset():
    return json.loads((FIXTURES / "idealista-directory-observed-subset.json").read_text(encoding="utf-8"))


def _reconstructed_html(subset):
    anchors = "".join(_anchor(link["url"], link["text"]) for link in subset["links"])
    return '<html lang="es"><h1>' + escape(subset["heading"]) + "</h1>" + anchors + "</html>"


def _anchor(href, label):
    return '<a href="' + escape(href, quote=True) + '">' + escape(label) + "</a>"


def test_subset_explicitly_distinguishes_dom_observation_from_reconstructed_html(observed_subset):
    assert observed_subset["source"] == "integrated_browser_dom"
    assert observed_subset["source_url"] == DIRECTORY
    assert observed_subset["heading"] == "Viviendas en alquiler en España"
    assert observed_subset["subset"] is True
    assert observed_subset["http_observed"] is False
    assert "HTML sintético" in observed_subset["html_reconstruction"]
    assert observed_subset["observed_source_sha256"] == "4e61686975b8f109eaa4fa08bed6bb17171bb9886e534f517ac3841ea4bb90e8"
    assert len(observed_subset["links"]) == 14
    assert len({link["url"] for link in observed_subset["links"]}) == 14


def test_observed_rent_subset_resolves_cities_and_preserves_provinces_and_island(geography, observed_subset):
    result = parse_directory("idealista", _reconstructed_html(observed_subset), DIRECTORY, geography)
    assert {(row["municipality"], row["province"]) for row in result["targets"]} == {
        ("46250", "46"), ("29067", "29"), ("41091", "41"),
        ("33024", "33"), ("30030", "30"), ("28079", "28"),
    }
    provinces = {row["url"].removeprefix(IDEALISTA): row["province"] for row in result["frontier"] if row["scope"] == "province"}
    assert provinces == {
        "/alquiler-viviendas/alicante/": "03",
        "/alquiler-viviendas/madrid-provincia/": "28",
        "/alquiler-viviendas/murcia-provincia/": "30",
        "/alquiler-viviendas/las-palmas/": "35",
        "/alquiler-viviendas/balears-illes/": "07",
        "/alquiler-viviendas/valencia-provincia/": "46",
        "/alquiler-viviendas/vizcaya/": "48",
    }
    areas = [row for row in result["frontier"] if row["scope"] == "area"]
    assert len(areas) == 1
    assert areas[0]["url"] == IDEALISTA + "/alquiler-viviendas/santa-cruz-de-tenerife/el-hierro/"
    assert areas[0]["province"] == "38"
    assert all("municipality" not in row for row in result["frontier"])
    assert len(result["frontier"]) == 14
    assert result["excluded"] == []
    assert all(row["operation"] == "rent" for row in result["targets"] + result["frontier"])


def test_reconstructed_subset_provenance_preserves_only_published_urls_and_its_own_hash(geography, observed_subset):
    html = _reconstructed_html(observed_subset)
    result = parse_directory("idealista", html, DIRECTORY, geography)
    published = {link["url"]: link["text"] for link in observed_subset["links"]}
    digest = hashlib.sha256(html.encode()).hexdigest()
    assert digest != observed_subset["observed_source_sha256"]
    assert {row["url"] for row in result["frontier"]} == published.keys()
    for row in result["targets"] + result["frontier"]:
        assert row["discovered_from"] == [DIRECTORY]
        assert row["provenance"] == [{
            "discovered_from": DIRECTORY, "source_sha256": digest,
            "href": row["url"], "label": published[row["url"]],
        }]
    assert all(row["validation"] == "observed_link" for row in result["targets"])
    madrid = next(row for row in result["targets"] if row["municipality"] == "28079")
    assert madrid["priority"] == "major"


@pytest.mark.parametrize("path,scope,operation", [
    ("/long-term-rentals-in-spain", "country", "rent"),
    ("/long-term-rentals-in-spain/", "country", "rent"),
    ("/properties-for-sale-in-spain", "country", "sale"),
    ("/properties-for-sale-in-spain/", "country", "sale"),
    ("/alquiler-viviendas/", "country", "rent"),
    ("/venta-viviendas/", "country", "sale"),
])
def test_directory_links_are_frontier_only(geography, path, scope, operation):
    result = parse_directory("idealista", _anchor(path, "España"), DIRECTORY, geography)
    assert result["targets"] == []
    assert len(result["frontier"]) == 1
    assert (result["frontier"][0]["scope"], result["frontier"][0]["operation"]) == (scope, operation)


def test_default_seeds_request_both_observed_idealista_directories_without_generated_municipal_urls(tmp_path, geography):
    expected = [
        DIRECTORY,
        SALE_DIRECTORY,
        "https://www.pisos.com/",
        "https://www.fotocasa.es/es/viviendas-de-alquiler-en-espana",
        "https://www.habitaclia.com/",
    ]
    requested = []

    def endpoint(request):
        requested.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    checkpoint = tmp_path / "checkpoint.json"
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        result = run_discovery(client, geography, checkpoint, tmp_path / "observed.json",
                               max_pages=len(expected), max_depth=0, execution_mode="fixture_replay")
    assert list(DEFAULT_SEEDS) == requested == expected
    assert result["pages_this_run"] == len(expected)
    assert result["targets_observed"] == 0
    assert result["national_complete"] is False
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert state["seed_urls"] == expected
    assert state["targets"] == state["frontier"] == []


@pytest.mark.parametrize("directory", [DIRECTORY, SALE_DIRECTORY])
def test_country_directory_does_not_guess_nested_municipal_urls(geography, directory):
    result = parse_directory("idealista", _anchor(directory + "/madrid/", "Madrid"), directory, geography)
    assert result["targets"] == result["frontier"] == []
    assert result["excluded"][0]["reason"] == "unsupported_or_filtered_route"


@pytest.mark.parametrize("slug,label,municipality", [
    ("alicante-alacant-alicante", "Alicante/Alacant", "03014"),
    ("a-coruna-a-coruna", "A Coruña", "15030"),
    ("donostia-san-sebastian-gipuzkoa", "Donostia/San Sebastián", "20069"),
    ("bilbao-vizcaya", "Bilbao", "48020"),
])
def test_synthetic_anchors_match_official_aliases_with_explicit_province(geography, slug, label, municipality):
    result = parse_directory("idealista", _anchor("/alquiler-viviendas/" + slug + "/", label), DIRECTORY, geography)
    assert len(result["targets"]) == 1
    assert result["targets"][0]["municipality"] == municipality


def test_sale_requires_its_own_observed_anchor_without_copying_rent(geography):
    rent = _anchor("/alquiler-viviendas/madrid-madrid/", "Madrid")
    rent_only = parse_directory("idealista", rent, DIRECTORY, geography)
    assert [row["operation"] for row in rent_only["targets"]] == ["rent"]
    sale = _anchor("/venta-viviendas/madrid-madrid/", "Madrid")
    combined = parse_directory("idealista", rent + sale, DIRECTORY, geography)
    assert [row["operation"] for row in combined["targets"]] == ["rent", "sale"]
    assert combined["targets"][1]["provenance"][0]["href"] == "/venta-viviendas/madrid-madrid/"


def test_nested_geography_cannot_be_promoted_even_if_area_matches_municipality(geography):
    href = "/alquiler-viviendas/madrid-provincia/madrid/"
    result = parse_directory("idealista", _anchor(href, "Madrid"), DIRECTORY, geography)
    assert result["targets"] == []
    assert (result["frontier"][0]["scope"], result["frontier"][0]["province"]) == ("area", "28")


@pytest.mark.parametrize("operation", ["alquiler", "venta"])
@pytest.mark.parametrize("province", ["madrid-provincia", "alicante"])
@pytest.mark.parametrize("facet", [
    "con-garaje", "con-terraza", "con-piscina", "con-lujo",
    "con-alquiler-temporal", "con-ascensor", "con-pisos",
])
def test_published_facets_below_a_province_cannot_enter_the_area_frontier(geography, operation, province, facet):
    href = f"/{operation}-viviendas/{province}/{facet}/"
    result = parse_directory("idealista", _anchor(href, facet.replace("-", " ")), DIRECTORY, geography)
    assert result["targets"] == result["frontier"] == []
    assert result["excluded"][0]["reason"] == "unsupported_or_filtered_route"


@pytest.mark.parametrize("operation,expected", [("alquiler", "rent"), ("venta", "sale")])
@pytest.mark.parametrize("location,area,label,province", [
    ("santa-cruz-de-tenerife", "el-hierro", "El Hierro", "38"),
    ("madrid-provincia", "zona-norte", "Zona norte", "28"),
])
def test_synthetic_area_anchor_is_preserved_after_facet_exclusion(geography, operation, expected, location, area, label, province):
    href = f"/{operation}-viviendas/{location}/{area}/"
    result = parse_directory("idealista", _anchor(href, label), DIRECTORY, geography)
    assert result["targets"] == result["excluded"] == []
    assert len(result["frontier"]) == 1
    assert (result["frontier"][0]["scope"], result["frontier"][0]["province"], result["frontier"][0]["operation"]) == ("area", province, expected)


@pytest.mark.parametrize("slug,province", [
    ("alava", "01"),
    ("a-coruna-provincia", "15"),
    ("guipuzcoa", "20"),
    ("vizcaya", "48"),
    ("balears-illes", "07"),
])
@pytest.mark.parametrize("route,operation", [("alquiler", "rent"), ("venta", "sale")])
def test_synthetic_province_anchors_preserve_observed_idealista_aliases(geography, slug, province, route, operation):
    result = parse_directory("idealista", _anchor(f"/{route}-viviendas/{slug}/", "Provincia"), DIRECTORY, geography)
    assert result["targets"] == result["excluded"] == []
    assert len(result["frontier"]) == 1
    assert (result["frontier"][0]["scope"], result["frontier"][0]["province"], result["frontier"][0]["operation"]) == ("province", province, operation)


@pytest.mark.parametrize("slug,label", [
    ("andorra", "Andorra"),
    ("gibraltar", "Gibraltar"),
    ("cerdanya-francesa", "Puigcerdà"),
    ("pais-vasco-frances", "Bilbao"),
])
def test_foreign_directory_roots_cannot_inherit_a_spanish_municipality_from_label(geography, slug, label):
    result = parse_directory("idealista", _anchor("/alquiler-viviendas/" + slug + "/", label), DIRECTORY, geography)
    assert result["targets"] == result["frontier"] == []
    assert result["excluded"][0]["reason"] == "unknown_province"


def test_duplicate_anchors_keep_normalized_url_and_original_evidence(geography):
    href = "/alquiler-viviendas/madrid-madrid/"
    alias = "https://WWW.IDEALISTA.COM/alquiler-viviendas/madrid-madrid/#top"
    html = _anchor(href, "Madrid") + _anchor(alias, "Ver anuncios")
    result = parse_directory("idealista", html, DIRECTORY, geography)
    assert len(result["targets"]) == len(result["frontier"]) == 1
    assert result["targets"][0]["url"] == IDEALISTA + href
    assert {row["href"] for row in result["targets"][0]["provenance"]} == {href, alias}


@pytest.mark.parametrize("href", [
    "http://www.idealista.com/alquiler-viviendas/madrid-madrid/",
    "https://www.idealista.com.evil.test/alquiler-viviendas/madrid-madrid/",
    "https://user:pass@www.idealista.com/alquiler-viviendas/madrid-madrid/",
    "https://www.idealista.com:444/alquiler-viviendas/madrid-madrid/",
    "//evil.test/alquiler-viviendas/madrid-madrid/",
    "/alquiler-viviendas/madrid-madrid/?ordenado-por=precios-asc",
    "/alquiler-viviendas/x/../madrid-madrid/",
    "/alquiler-viviendas/./madrid-madrid/",
    "/alquiler-viviendas/%2e%2e/madrid-madrid/",
    "/alquiler-viviendas/madrid%2fmadrid/",
    "/alquiler-viviendas/madrid%252fmadrid/",
    "/alquiler-viviendas/madrid%5cmadrid/",
    "/alquiler-viviendas/madrid-madrid\\/",
    "/alquiler-viviendas/madrid-madrid/ ",
    "/long-term-rentals-in-spain?ordenado-por=precios-asc",
    "/properties-for-sale-in-spain?ordenado-por=precios-asc",
])
def test_unsafe_urls_are_rejected_before_path_normalization(geography, href):
    result = parse_directory("idealista", _anchor(href, "Madrid"), DIRECTORY, geography)
    assert result["targets"] == result["frontier"] == []
    assert result["excluded"][0]["reason"] == "unsafe_url"
    assert result["excluded"][0]["href"] == href


@pytest.mark.parametrize("href", [
    "/alquiler-viviendas/madrid-madrid/con-garaje/",
    "/alquiler-viviendas/madrid-madrid/pagina-2.htm",
    "/alquiler-viviendas/madrid-madrid/centro/",
    "/alquiler-viviendas/valencia-valencia/centro/",
    "/alquiler-viviendas//madrid-madrid/",
    "/comprar-viviendas/madrid-madrid/",
    "/alquiler-viviendas/cabanes/",
    "/alquiler-viviendas/provincia-inventada/",
])
def test_filtered_and_unknown_geography_do_not_become_targets(geography, href):
    result = parse_directory("idealista", _anchor(href, "Madrid"), DIRECTORY, geography)
    assert result["targets"] == result["frontier"] == []
    assert result["excluded"]


@pytest.mark.parametrize("href,label,reason", [
    ("/alquiler-viviendas/madrid-madrid/", "Murcia", "conflicting_label"),
    ("/alquiler-viviendas/cabanes-girona/", "Cabanes", None),
    ("/alquiler-viviendas/cabanes-madrid/", "Cabanes", "unknown_municipality"),
    ("/alquiler-viviendas/provincia-inventada-provincia/", "Madrid", "unknown_province"),
])
def test_route_geography_and_label_must_agree(geography, href, label, reason):
    result = parse_directory("idealista", _anchor(href, label), DIRECTORY, geography)
    if reason is None:
        assert len(result["targets"]) == 1
        assert result["targets"][0]["province"] == "17"
    else:
        assert result["targets"] == result["frontier"] == []
        assert result["excluded"][0]["reason"] == reason


def test_pisoscom_directory_remains_unsupported_and_unknown_portal_raises(geography):
    result = parse_directory("pisoscom", '<a href="/">España</a>', "https://www.pisos.com/", geography)
    assert result["excluded"][0]["reason"] == "unsupported_portal_directory"
    with pytest.raises(ValueError, match="Unknown portal"):
        parse_directory("unknown", "", DIRECTORY, geography)
