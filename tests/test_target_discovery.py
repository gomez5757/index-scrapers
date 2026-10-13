import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from index_scrapers.target_discovery import (
    DEFAULT_SEEDS, MunicipalityIndex, parse_directory, run_discovery,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/targets"
FC = "https://www.fotocasa.es"
HA = "https://www.habitaclia.com"
GIRONA = FC + "/es/alquiler/viviendas/girona-capital/todas-las-zonas/l"
ALICANTE = HA + "/comprar/viviendas/alicante-provincia/alicante-alacant/s"


@pytest.fixture
def geography():
    return MunicipalityIndex.from_csv(
        ROOT / "data/municipalities.csv",
        provinces_html=FIXTURES / "ine-provinces.directory.html",
        capitals_html=FIXTURES / "boe-capitals.html",
        major_codes={"28079", "08019", "46250", "41091", "29067", "48020"},
    )


def test_official_geography_keeps_8132_municipalities_and_50_capitals(geography):
    assert len(geography.municipalities) == 8132
    assert len(geography.provinces) == 52
    assert len(geography.capitals) == 50
    assert "17079" in geography.capitals
    assert "03014" in geography.capitals
    assert "07040" in geography.capitals
    assert "51001" not in geography.capitals


def test_captured_source_hashes_are_reproducible():
    for source in json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))["pages"]:
        if "file" in source:
            assert hashlib.sha256((FIXTURES / source["file"]).read_bytes()).hexdigest() == source["sha256"]


def test_real_fotocasa_directory_discovers_girona_rent_and_province_frontier(geography):
    source = FC + "/es/viviendas-de-alquiler-en-espana"
    result = parse_directory("fotocasa", (FIXTURES / "fotocasa.directory.html").read_bytes(), source, geography)
    target = next(t for t in result["targets"] if t["url"] == GIRONA)
    assert (target["province"], target["municipality"], target["operation"], target["priority"]) == ("17", "17079", "rent", "capital")
    assert target["discovered_from"] == [source]
    assert target["provenance"][0]["href"] == GIRONA
    assert target["validation"] == "observed_link"
    assert any(f["url"] == FC + "/es/alquiler/viviendas/girona-provincia/todas-las-zonas/l" for f in result["frontier"])
    assert all(t["municipality"] != "07040" for t in result["targets"] if "palma-de-mallorca" in t["url"])


def test_real_habitaclia_directory_discovers_alicante_sale_and_official_aliases(geography):
    result = parse_directory("habitaclia", (FIXTURES / "habitaclia.directory.html").read_bytes(), HA + "/", geography)
    target = next(t for t in result["targets"] if t["url"] == ALICANTE)
    assert (target["province"], target["municipality"], target["operation"], target["priority"]) == ("03", "03014", "sale", "capital")
    assert any(t["municipality"] == "15030" for t in result["targets"])
    assert any(t["municipality"] == "20069" for t in result["targets"])
    assert any(t["municipality"] == "01059" for t in result["targets"])


def test_real_fotocasa_province_expands_to_municipalities(geography):
    source = FC + "/es/alquiler/viviendas/girona-provincia/todas-las-zonas/l"
    result = parse_directory("fotocasa", (FIXTURES / "fotocasa.province.directory.html").read_bytes(), source, geography)
    assert len(result["targets"]) == 19
    assert any(t["municipality"] == "17079" for t in result["targets"])
    assert all(t["province"] == "17" for t in result["targets"])


def test_real_habitaclia_province_traverses_comarcas_without_assigning_municipality(geography):
    source = HA + "/comprar/viviendas/alicante-provincia/s"
    result = parse_directory("habitaclia", (FIXTURES / "habitaclia.province.directory.html").read_bytes(), source, geography)
    assert result["targets"] == []
    areas = [f for f in result["frontier"] if f["scope"] == "area"]
    assert len(areas) == 9
    assert all("municipality" not in f and "comarcas" in f["provenance"][0]["directory_group"] for f in areas)
    assert any(f["url"] == HA + "/comprar/viviendas/alicante-provincia/vega-baja/s" for f in areas)


def test_real_habitaclia_comarca_expands_to_twelve_municipalities(geography):
    source = HA + "/comprar/viviendas/alicante-provincia/vega-baja/s"
    result = parse_directory("habitaclia", (FIXTURES / "habitaclia.area.directory.html").read_bytes(), source, geography)
    assert len(result["targets"]) == 12
    assert all(t["province"] == "03" and t["operation"] == "sale" for t in result["targets"])
    assert any(t["municipality"] == "03133" for t in result["targets"])
    assert all(t["municipality_name"] != "Vega Baja" for t in result["targets"])


def test_observed_area_group_is_not_assigned_even_if_its_slug_matches_a_municipality(geography):
    url = HA + "/comprar/viviendas/girona-provincia/girona/s"
    html = f'<div><h3>Casas y pisos en venta en comarcas</h3><div data-testid="internal-links-content-child_areas"><ul><li><a href="{url}">Girona</a></li></ul></div></div>'
    result = parse_directory("habitaclia", html, HA + "/", geography)
    assert result["targets"] == []
    assert result["frontier"][0]["scope"] == "area"


def test_ambiguous_fotocasa_name_is_excluded_even_from_province_source(geography):
    url = FC + "/es/alquiler/viviendas/cabanes/todas-las-zonas/l"
    result = parse_directory("fotocasa", f'<a href="{url}">Cabanes</a>', FC + "/es/alquiler/viviendas/girona-provincia/todas-las-zonas/l", geography)
    assert result["targets"] == []
    assert result["excluded"][0]["reason"] == "ambiguous_municipality"
    assert len(result["excluded"][0]["candidates"]) == 2


def test_explicit_habitaclia_province_disambiguates_same_name(geography):
    url = HA + "/comprar/viviendas/girona-provincia/cabanes/s"
    result = parse_directory("habitaclia", f'<a href="{url}">Cabanes</a>', HA + "/", geography)
    assert len(result["targets"]) == 1
    assert result["targets"][0]["province"] == "17"


def test_conflicting_label_is_not_assigned_to_slug_municipality(geography):
    result = parse_directory("fotocasa", f'<a href="{GIRONA}">Madrid</a>', FC + "/", geography)
    assert result["targets"] == []
    assert result["excluded"][0]["reason"] == "conflicting_label"


@pytest.mark.parametrize("href", [
    "http://www.fotocasa.es/es/alquiler/viviendas/girona-capital/todas-las-zonas/l",
    "https://www.fotocasa.es.evil.test/es/alquiler/viviendas/girona-capital/todas-las-zonas/l",
    "https://user:pass@www.fotocasa.es/es/alquiler/viviendas/girona-capital/todas-las-zonas/l",
    "https://www.fotocasa.es:444/es/alquiler/viviendas/girona-capital/todas-las-zonas/l",
    "//evil.test/es/alquiler/viviendas/girona-capital/todas-las-zonas/l",
])
def test_only_https_exact_allowlisted_hosts_without_credentials(geography, href):
    result = parse_directory("fotocasa", f'<a href="{href}">Girona</a>', FC + "/", geography)
    assert result["targets"] == []
    assert result["frontier"] == []


def test_filtered_submunicipal_and_provincial_urls_do_not_become_targets(geography):
    html = '<a href="/comprar/viviendas/girona-provincia/segunda-mano/s">Girona</a><a href="/comprar/viviendas/girona-provincia/girona-capital/amueblados/s">Girona</a><a href="/comprar/viviendas/girona-provincia/s">Girona</a>'
    result = parse_directory("habitaclia", html, HA + "/", geography)
    assert result["targets"] == []
    assert len(result["frontier"]) == 1
    assert result["frontier"][0]["scope"] == "province"


def test_duplicate_links_have_deterministic_order_and_full_provenance(geography):
    anchors = [f'<a href="{GIRONA}">Girona</a>', f'<a href="{GIRONA}#top">Ver anuncios</a>', '<a href="/es/comprar/viviendas/madrid-capital/todas-las-zonas/l">Madrid</a>']
    first = parse_directory("fotocasa", "".join(anchors), FC + "/", geography)
    second = parse_directory("fotocasa", "".join(reversed(anchors)), FC + "/", geography)
    # Reordering the source changes its hash, while target order and link evidence remain stable.
    comparable = [deepcopy(first), deepcopy(second)]
    for result in comparable:
        for row in result["targets"] + result["frontier"]:
            for evidence in row["provenance"]:
                evidence.pop("source_sha256")
    assert comparable[0] == comparable[1]
    assert len(first["targets"]) == 2
    assert first["targets"][0]["operation"] == "rent"
    assert len(first["targets"][0]["provenance"]) == 2


def test_priority_requires_official_capital_table_and_keeps_pilot_major(geography):
    target = parse_directory("fotocasa", f'<a href="{GIRONA}">Girona</a>', FC + "/", geography)["targets"][0]
    assert target["capital_provenance"]["source"] == "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2025-25362"
    without = MunicipalityIndex.from_csv(ROOT / "data/municipalities.csv", provinces_html=FIXTURES / "ine-provinces.directory.html")
    assert parse_directory("fotocasa", f'<a href="{GIRONA}">Girona</a>', FC + "/", without)["targets"][0]["priority"] == "unknown"
    madrid = FC + "/es/alquiler/viviendas/madrid-capital/todas-las-zonas/l"
    assert parse_directory("fotocasa", f'<a href="{madrid}">Madrid</a>', FC + "/", geography)["targets"][0]["priority"] == "major"


def test_checkpoint_bounds_work_resumes_without_refetch_and_preserves_sources(tmp_path, geography):
    root = FC + "/es/viviendas-de-alquiler-en-espana"
    province = FC + "/es/alquiler/viviendas/girona-provincia/todas-las-zonas/l"
    requested = []
    def endpoint(request):
        requested.append(str(request.url))
        if str(request.url) == root:
            return httpx.Response(200, text=f'<a href="{province}">Girona</a><a href="{GIRONA}">Girona</a>')
        return httpx.Response(200, text=f'<a href="{GIRONA}">Girona</a>')
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        first = run_discovery(client, geography, checkpoint, output, seeds=[root], max_pages=1)
        assert first["pages_this_run"] == 1
        assert len(requested) == 1
        assert json.loads(checkpoint.read_text())["frontier"]
        second = run_discovery(client, geography, checkpoint, output, seeds=[root], max_pages=1)
    assert requested == [root, province]
    observed = json.loads(output.read_text())
    assert observed["targets"][0]["discovered_from"] == sorted([root, province])
    assert second["national_complete"] is False
    assert observed["coverage"]["expected_pairs_per_portal"] == 16264
    assert observed["coverage"]["portals"]["idealista"]["observed_pairs"] == 0


def test_blocked_responses_and_network_failures_are_durable_gaps(tmp_path, geography):
    def endpoint(request):
        if request.url.host == "www.idealista.com":
            return httpx.Response(403, text='<a href="https://www.fotocasa.es/es/alquiler/viviendas/girona-capital/todas-las-zonas/l">Girona</a>')
        raise httpx.ReadError("disconnected", request=request)
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        result = run_discovery(client, geography, tmp_path / "checkpoint.json", tmp_path / "observed.json", seeds=DEFAULT_SEEDS[:2], max_pages=2)
    assert result["targets_observed"] == 0
    state = json.loads((tmp_path / "checkpoint.json").read_text())
    assert {p["outcome"] for p in state["pages"]} == {"blocked", "network_error"}
    assert state["pages"][0]["status"] == 403
    assert state["frontier"] == []


def test_checkpoint_rejects_changed_geography_before_any_network_call(tmp_path, geography):
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as client:
        run_discovery(client, geography, checkpoint, output, seeds=[DEFAULT_SEEDS[0]], max_pages=1)
        geography.fingerprints["municipalities"] = "changed"
        with pytest.raises(ValueError, match="geography"):
            run_discovery(client, geography, checkpoint, output, max_pages=1)


def test_redirect_is_not_followed_and_large_pages_are_not_parsed(tmp_path, geography):
    requested = []
    def endpoint(request):
        requested.append(str(request.url))
        if request.url.host == "www.idealista.com":
            return httpx.Response(302, headers={"Location": "https://evil.test/"})
        return httpx.Response(200, text="x" * 200)
    with httpx.Client(transport=httpx.MockTransport(endpoint), follow_redirects=True) as client:
        run_discovery(client, geography, tmp_path / "checkpoint.json", tmp_path / "observed.json", seeds=DEFAULT_SEEDS[:2], max_pages=2, max_page_bytes=100)
    assert len(requested) == 2
    assert {p["outcome"] for p in json.loads((tmp_path / "checkpoint.json").read_text())["pages"]} == {"redirect_requires_review", "page_too_large"}


def test_http_discovery_does_not_reuse_response_cookies(tmp_path, geography):
    cookies = []
    def endpoint(request):
        cookies.append(request.headers.get("Cookie"))
        return httpx.Response(200, text="<html></html>", headers={"Set-Cookie": "session=anonymous; Path=/"})
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        run_discovery(client, geography, tmp_path / "checkpoint.json", tmp_path / "observed.json", seeds=[FC + "/", FC + "/es/viviendas-de-alquiler-en-espana"], max_pages=2)
    assert cookies == [None, None]


def test_script_refuses_production_config_as_output_without_writing_it(tmp_path):
    spec = importlib.util.spec_from_file_location("discover_targets_script", ROOT / "scripts/discover_targets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before = (ROOT / "config/targets.json").read_bytes()
    with pytest.raises(SystemExit):
        module.main(["--checkpoint", str(tmp_path / "checkpoint.json"), "--output", str(ROOT / "config/targets.json")])
    assert (ROOT / "config/targets.json").read_bytes() == before
    assert not (tmp_path / "checkpoint.json").exists()


def test_depth_limit_keeps_observed_frontier_for_later_expansion(tmp_path, geography):
    root = FC + "/es/viviendas-de-alquiler-en-espana"
    province = FC + "/es/alquiler/viviendas/girona-provincia/todas-las-zonas/l"
    calls = []
    def endpoint(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=f'<a href="{province}">Girona</a>' if len(calls) == 1 else f'<a href="{GIRONA}">Girona</a>')
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        result = run_discovery(client, geography, checkpoint, output, seeds=[root], max_pages=5, max_depth=0)
        assert result["pages_this_run"] == 1
        assert result["frontier_pending"] == 1
        assert run_discovery(client, geography, checkpoint, output, seeds=[root], max_pages=1, max_depth=1)["targets_observed"] == 1
    assert calls == [root, province]


def test_checkpoint_exclusive_lock_preserves_other_writer(tmp_path, geography):
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    lock = tmp_path / "checkpoint.json.lock"
    lock.write_text("another-writer")
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No network expected"))) as client:
        with pytest.raises(FileExistsError):
            run_discovery(client, geography, checkpoint, output)
    assert lock.read_text() == "another-writer"
    assert not output.exists()


def test_checkpoint_output_cannot_replace_and_then_delete_its_lock(tmp_path, geography):
    checkpoint = tmp_path / "checkpoint.json"
    output = tmp_path / "checkpoint.json.lock"
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No network expected"))) as client:
        with pytest.raises(ValueError, match="lock"):
            run_discovery(client, geography, checkpoint, output, seeds=[], max_pages=1)
    assert not checkpoint.exists()
    assert not output.exists()


def test_script_rejects_output_colliding_with_checkpoint_lock(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("discover_targets_script", ROOT / "scripts/discover_targets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html></html>"))
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    checkpoint = tmp_path / "checkpoint.json"
    output = tmp_path / "checkpoint.json.lock"
    with pytest.raises(SystemExit):
        module.main(["--checkpoint", str(checkpoint), "--output", str(output)])
    assert not checkpoint.exists()
    assert not output.exists()


def test_observed_output_recovers_from_authoritative_checkpoint(tmp_path, geography, monkeypatch):
    from index_scrapers import target_discovery
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    original_write = target_discovery._atomic_json
    def failing_output(path, value):
        if path == output:
            raise OSError("output disk failure")
        original_write(path, value)
    monkeypatch.setattr(target_discovery, "_atomic_json", failing_output)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=f'<a href="{GIRONA}">Girona</a>'))) as client:
        with pytest.raises(OSError, match="output disk failure"):
            run_discovery(client, geography, checkpoint, output, seeds=[FC + "/"], max_pages=1, max_depth=0)
    assert len(json.loads(checkpoint.read_text())["pages"]) == 1
    monkeypatch.setattr(target_discovery, "_atomic_json", original_write)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("Completed pages must not be fetched again"))) as client:
        result = run_discovery(client, geography, checkpoint, output, seeds=[FC + "/"], max_pages=1, max_depth=0)
    assert result["pages_this_run"] == 0
    assert json.loads(output.read_text())["targets"][0]["municipality"] == "17079"


def test_script_reads_existing_slugs_and_runs_with_separate_output(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("discover_targets_script", ROOT / "scripts/discover_targets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=(FIXTURES / "fotocasa.directory.html").read_bytes()))
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    output = tmp_path / "observed.json"
    before = (ROOT / "config/targets.json").read_bytes()
    assert module.main(["--portal", "fotocasa", "--max-pages", "1", "--checkpoint", str(tmp_path / "checkpoint.json"), "--output", str(output)]) == 0
    observed = json.loads(output.read_text())
    assert any(t["municipality"] == "28079" and t["city_slug"] == "madrid" for t in observed["targets"])
    assert observed["coverage"]["national_complete"] is False
    assert (ROOT / "config/targets.json").read_bytes() == before


def _saved_checkpoint(tmp_path, geography):
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    html = f'<a href="{GIRONA}">Girona</a><a href="/es/alquiler/viviendas/girona-provincia/todas-las-zonas/l">Girona</a><a href="/es/alquiler/viviendas/cabanes/todas-las-zonas/l">Cabanes</a><a href="https://evil.test/es/alquiler/viviendas/girona-capital/todas-las-zonas/l">Girona</a>'
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html))) as client:
        run_discovery(client, geography, checkpoint, output, seeds=[FC + "/"], max_pages=1, max_depth=0)
    return checkpoint, output, json.loads(checkpoint.read_text())


def test_checkpoint_declares_version_mode_and_parser_fingerprint(tmp_path, geography):
    checkpoint, output, state = _saved_checkpoint(tmp_path, geography)
    assert state["schema_version"] == 2
    assert state["execution_mode"] == "http"
    assert state["parser_sha256"] == hashlib.sha256((ROOT / "src/index_scrapers/target_discovery.py").read_bytes()).hexdigest()
    assert json.loads(output.read_text())["execution_mode"] == "http"


def test_fixture_replay_cannot_resume_as_http_and_keeps_both_files(tmp_path, geography):
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=f'<a href="{GIRONA}">Girona</a>'))) as client:
        run_discovery(client, geography, checkpoint, output, seeds=[FC + "/"], max_pages=1, max_depth=0, execution_mode="fixture_replay")
    before = checkpoint.read_bytes(), output.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No fetch before mode validation"))) as client:
        with pytest.raises(ValueError, match="mode"):
            run_discovery(client, geography, checkpoint, output, seeds=[FC + "/"], max_pages=1, max_depth=0, execution_mode="http")
    assert (checkpoint.read_bytes(), output.read_bytes()) == before
    assert not checkpoint.with_suffix(".json.lock").exists()
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No fetch at this depth"))) as client:
        assert run_discovery(client, geography, checkpoint, output, seeds=[FC + "/"], max_pages=1, max_depth=0, execution_mode="fixture_replay")["pages_this_run"] == 0
    assert json.loads(output.read_text(encoding="utf-8"))["execution_mode"] == "fixture_replay"


@pytest.mark.parametrize("collection,path,value", [
    (None, ["execution_mode"], "fixture_replay"),
    (None, ["parser_sha256"], "0" * 64),
    (None, ["schema_version"], 1),
    (None, ["targets"], {}),
    ("targets", [0, "url"], "https://evil.test/alquiler/"),
    ("targets", [0, "operation"], "sale"),
    ("targets", [0, "municipality"], "28079"),
    ("targets", [0, "province"], "28"),
    ("targets", [0, "priority"], "major"),
    ("targets", [0, "city_slug"], "madrid"),
    ("targets", [0, "provenance", 0, "href"], "/es/alquiler/viviendas/madrid-capital/todas-las-zonas/l"),
    ("targets", [0, "provenance", 0, "source_sha256"], "0" * 64),
    ("targets", [0, "provenance", 0, "discovered_from"], FC + "/not-visited"),
    ("frontier", [0, "scope"], "unknown"),
    ("frontier", [0, "operation"], "sale"),
    ("frontier", [0, "depth"], -1),
    ("pages", [0, "status"], 403),
    ("pages", [0, "url"], "https://evil.test/"),
    ("pages", [0, "retrieved_at"], "invalid-date"),
    ("excluded", [0, "reason"], "invented-reason"),
    ("excluded", [0, "source_sha256"], "0" * 64),
])
def test_checkpoint_rejects_incoherent_collections_without_writing(tmp_path, geography, collection, path, value):
    checkpoint, output, state = _saved_checkpoint(tmp_path, geography)
    node = state if collection is None else state[collection]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    checkpoint.write_text(json.dumps(state), encoding="utf-8")
    output.write_bytes(b"previous observed output\n")
    before = checkpoint.read_bytes(), output.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No request from rejected checkpoint"))) as client:
        with pytest.raises(ValueError, match="[Cc]heckpoint"):
            run_discovery(client, geography, checkpoint, output, max_pages=1, max_depth=0)
    assert (checkpoint.read_bytes(), output.read_bytes()) == before
    assert not checkpoint.with_suffix(".json.lock").exists()


@pytest.mark.parametrize("collection", ["frontier", "targets", "pages", "excluded"])
def test_checkpoint_rejects_duplicate_records_in_every_collection(tmp_path, geography, collection):
    checkpoint, output, state = _saved_checkpoint(tmp_path, geography)
    state[collection].append(deepcopy(state[collection][0]))
    checkpoint.write_text(json.dumps(state), encoding="utf-8")
    before = checkpoint.read_bytes(), output.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No request from rejected checkpoint"))) as client:
        with pytest.raises(ValueError, match="[Cc]heckpoint"):
            run_discovery(client, geography, checkpoint, output, max_pages=1, max_depth=0)
    assert (checkpoint.read_bytes(), output.read_bytes()) == before


def test_script_rejects_replay_checkpoint_without_http_or_output(tmp_path, geography, monkeypatch, capsys):
    checkpoint, output, state = _saved_checkpoint(tmp_path, geography)
    state["execution_mode"] = "fixture_replay"
    checkpoint.write_text(json.dumps(state), encoding="utf-8")
    before = checkpoint.read_bytes(), output.read_bytes()
    spec = importlib.util.spec_from_file_location("discover_targets_script", ROOT / "scripts/discover_targets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda r: pytest.fail("CLI must not fetch from replay checkpoint"))
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    with pytest.raises(SystemExit):
        module.main(["--checkpoint", str(checkpoint), "--output", str(output)])
    assert "mode" in capsys.readouterr().err
    assert (checkpoint.read_bytes(), output.read_bytes()) == before


@pytest.mark.parametrize("collection,field,value", [(None, "schema_version", 2.0), ("frontier", "scope", []), ("pages", "outcome", [])])
def test_checkpoint_rejects_wrong_schema_or_non_scalar_enums(tmp_path, geography, collection, field, value):
    checkpoint, output, state = _saved_checkpoint(tmp_path, geography)
    record = state if collection is None else state[collection][0]
    record[field] = value
    checkpoint.write_text(json.dumps(state), encoding="utf-8")
    before = checkpoint.read_bytes(), output.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No request before scalar/type validation"))) as client:
        with pytest.raises(ValueError, match="[Cc]heckpoint"):
            run_discovery(client, geography, checkpoint, output, max_pages=1, max_depth=0)
    assert (checkpoint.read_bytes(), output.read_bytes()) == before


@pytest.mark.parametrize("case", ["blocked", "redirect", "http_error", "large", "network", "parsed", "unsupported"])
def test_valid_terminal_page_outcomes_resume_without_refetch(tmp_path, geography, case):
    def endpoint(request):
        if case == "network":
            raise httpx.ReadError("offline test", request=request)
        return httpx.Response({"blocked": 403, "redirect": 302, "http_error": 503}.get(case, 200), text="<html></html>" if case != "large" else "x" * 200)
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    seed = DEFAULT_SEEDS[0] if case == "unsupported" else FC + "/"
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        run_discovery(client, geography, checkpoint, output, seeds=[seed], max_pages=1, max_page_bytes=100)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No refetch of validated terminal page"))) as client:
        assert run_discovery(client, geography, checkpoint, output, seeds=[seed], max_pages=1)["pages_this_run"] == 0


def test_checkpoint_rejects_invented_network_error_type(tmp_path, geography):
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    def endpoint(request):
        raise httpx.ReadError("offline test", request=request)
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        run_discovery(client, geography, checkpoint, output, seeds=[FC + "/"], max_pages=1)
    state = json.loads(checkpoint.read_text())
    state["pages"][0]["error_type"] = "InventedError"
    checkpoint.write_text(json.dumps(state), encoding="utf-8")
    before = checkpoint.read_bytes(), output.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No request from invalid checkpoint"))) as client:
        with pytest.raises(ValueError, match="checkpoint pages network error"):
            run_discovery(client, geography, checkpoint, output, max_pages=1)
    assert (checkpoint.read_bytes(), output.read_bytes()) == before


def test_version_two_replay_of_real_captures_resumes_all_179_routes(tmp_path, geography):
    sources = {s["requested_url"]: s for s in json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))["pages"] if s.get("portal") in {"idealista", "pisoscom", "fotocasa", "habitaclia"}}
    def endpoint(request):
        source = sources[str(request.url)]
        if "error" in source:
            raise httpx.ReadError("captured disconnect", request=request)
        return httpx.Response(source["status"], content=(FIXTURES / source["file"]).read_bytes())
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        result = run_discovery(client, geography, checkpoint, output, seeds=list(sources), max_pages=len(sources), max_depth=0, execution_mode="fixture_replay")
    assert result["targets_observed"] == 179
    before = checkpoint.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No network/refetch on replay resume"))) as client:
        result = run_discovery(client, geography, checkpoint, output, seeds=list(sources), max_pages=1, max_depth=0, execution_mode="fixture_replay")
    assert result["pages_this_run"] == 0
    assert result["targets_observed"] == 179
    assert checkpoint.read_bytes() == before
    assert json.loads(output.read_text(encoding="utf-8"))["execution_mode"] == "fixture_replay"


@pytest.mark.parametrize("changed_seeds", [[FC + "/"], [HA + "/", FC + "/"], [FC + "/", HA + "/", DEFAULT_SEEDS[0]], []])
def test_checkpoint_rejects_changed_seed_inputs_before_lock_or_writes(tmp_path, geography, monkeypatch, changed_seeds):
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html></html>"))) as client:
        run_discovery(client, geography, checkpoint, output, seeds=[FC + "/", HA + "/"], max_pages=1)
    before = checkpoint.read_bytes(), output.read_bytes()
    original_open = Path.open
    lock = checkpoint.with_name(checkpoint.name + ".lock")
    def guarded_open(path, *args, **kwargs):
        if path.resolve() == lock.resolve():
            pytest.fail("Changed seed inputs must be rejected before lock acquisition")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No request from changed seed inputs"))) as client:
        with pytest.raises(ValueError, match="checkpoint seed"):
            run_discovery(client, geography, checkpoint, output, seeds=changed_seeds, max_pages=1)
    assert (checkpoint.read_bytes(), output.read_bytes()) == before
    assert not lock.exists()


def test_checkpoint_accepts_equivalent_normalized_unique_seeds(tmp_path, geography):
    checkpoint, output = tmp_path / "checkpoint.json", tmp_path / "observed.json"
    calls = []
    def endpoint(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="<html></html>")
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        run_discovery(client, geography, checkpoint, output, seeds=[FC + "/#a", FC + "/", HA + "/#b"], max_pages=1)
        result = run_discovery(client, geography, checkpoint, output, seeds=[FC + "/", HA + "/#changed", HA + "/"], max_pages=1)
    assert calls == [FC + "/", HA + "/"]
    assert result["pages_total"] == 2
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["seed_urls"] == [FC + "/", HA + "/"]
