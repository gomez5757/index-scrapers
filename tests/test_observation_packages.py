from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import Path
import stat
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx
import pytest

from index_scrapers import observation_packages as packages


SHA = "a" * 40
CORPUS = "b" * 64
RELEASE = 71
ASSET = 72
RUN = 73
ATTEMPT = 2
ARTIFACT = 74
WORKFLOW = 75
BASE = f"https://api.github.com/repos/{packages.REPOSITORY}"
ACTOR = {"login": "operator", "id": 76}
CAPTURE = "2026-10-14T13:50:46.351Z"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def envelope(status="ready", operation="rent"):
    source = {"source": "integrated_browser_dom", "observed_at": CAPTURE,
              "source_card": {"id": "112658705", "url": "https://www.idealista.com/inmueble/112658705/",
                              "title": "Piso en Madrid", "price_text": "1.000€/mes", "details": [], "images": []},
              "origins": [{"observed_at": "2026-10-13T18:10:11.319Z",
                           "source_page": "https://www.idealista.com/alquiler-viviendas/madrid-madrid/"}]}
    return {"status": status, "candidate": {"portal": "idealista", "id": "idealista:112658705",
            "property": {"operation": operation}, "first_seen": CAPTURE}, "source": source,
            "evidence": {"gallery_complete": None}, "reasons": [] if status == "ready" else ["missing_price_text"]}


def jsonl(value):
    # Deliberately not canonical: packing must preserve input bytes, not merely values.
    return (json.dumps(value, ensure_ascii=False, indent=None) + "\n").encode("utf-8")


@pytest.fixture
def source_package(tmp_path, monkeypatch):
    monkeypatch.setattr(packages, "_adapter_commit", lambda: SHA)
    ready = tmp_path / "ready.jsonl"
    pending = tmp_path / "pending.jsonl"
    ready.write_bytes(jsonl(envelope()) + jsonl(envelope(operation="sale")))
    value = envelope("pending")
    value["source"]["observed_at"] = None
    value["source"]["origins"] = []
    pending.write_bytes(jsonl(value))
    output = tmp_path / "source.zip"
    result = packages.pack(ready=ready, pending=pending, corpus_sha256=CORPUS, output=output)
    return {"ready": ready.read_bytes(), "pending": pending.read_bytes(), "zip": output.read_bytes(),
            "result": result, "ready_path": ready, "pending_path": pending, "output": output}


@pytest.fixture
def producer_env():
    return {"GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": packages.REPOSITORY,
            "GITHUB_REPOSITORY_ID": str(packages.REPOSITORY_ID), "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": SHA, "GITHUB_RUN_ID": str(RUN),
            "GITHUB_RUN_ATTEMPT": str(ATTEMPT), "GITHUB_ACTOR": ACTOR["login"]}


def repo():
    return {"id": packages.REPOSITORY_ID, "full_name": packages.REPOSITORY, "fork": False,
            "private": False, "default_branch": "main"}


def asset(data):
    return {"id": ASSET, "name": packages.ASSET_NAME, "state": "uploaded", "size": len(data),
            "url": f"{BASE}/releases/assets/{ASSET}", "uploader": deepcopy(ACTOR)}


def freeze_routes(data):
    metadata = asset(data)
    return {BASE: repo(), f"{BASE}/releases/{RELEASE}": {"id": RELEASE, "draft": False,
            "url": f"{BASE}/releases/{RELEASE}", "author": deepcopy(ACTOR), "assets": [deepcopy(metadata)]},
            f"{BASE}/releases/assets/{ASSET}": metadata}


def client_for(routes, downloads, requests):
    def handle(request):
        requests.append(request)
        url = str(request.url)
        artifact_download = request.url.host == "api.github.com" and "/actions/artifacts/" in request.url.path
        download_accept = "application/vnd.github+json" if artifact_download else "application/octet-stream"
        if request.headers.get("accept") == download_accept and url in downloads:
            value = downloads[url]
        elif url in downloads and url not in routes:
            return httpx.Response(415)
        else:
            value = routes.get(url)
        if isinstance(value, httpx.Response):
            return value
        if isinstance(value, bytes):
            return httpx.Response(200, content=value)
        if value is None:
            return httpx.Response(404)
        return httpx.Response(200, json=value)
    return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True,
                        headers={"Authorization": "Bearer default-secret", "Cookie": "default-cookie"})


def freeze_source(source_package, tmp_path, producer_env, routes=None, downloads=None):
    routes = freeze_routes(source_package["zip"]) if routes is None else routes
    downloads = {f"{BASE}/releases/assets/{ASSET}": source_package["zip"]} if downloads is None else downloads
    requests = []
    with client_for(routes, downloads, requests) as client:
        manifest = packages.freeze(repository=packages.REPOSITORY, release_id=RELEASE, asset_id=ASSET,
            sha256=sha(source_package["zip"]), output_dir=tmp_path / "frozen", client=client, env=producer_env)
    return manifest, requests


def frozen_zip(path):
    result = io.BytesIO()
    with ZipFile(result, "w") as archive:
        for name in packages.MEMBERS:
            archive.writestr(name, (path / name).read_bytes())
    return result.getvalue()


def fetch_routes(data):
    artifact_url = f"{BASE}/actions/artifacts/{ARTIFACT}"
    origin = {"id": RUN, "repository_id": packages.REPOSITORY_ID, "head_repository_id": packages.REPOSITORY_ID,
              "head_branch": "main", "head_sha": SHA}
    run = {"id": RUN, "run_attempt": ATTEMPT, "head_sha": SHA, "head_branch": "main", "path": packages.WORKFLOW_PATH,
           "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "pull_requests": [],
           "repository": repo(), "head_repository": repo(), "workflow_id": WORKFLOW, "actor": deepcopy(ACTOR)}
    return {BASE: repo(), artifact_url: {"id": ARTIFACT, "name": f"idealista-observations-{RUN}-{ATTEMPT}",
            "size_in_bytes": len(data), "expired": False, "expires_at": "2099-01-01T00:00:00Z", "workflow_run": origin,
            "url": artifact_url, "archive_download_url": artifact_url + "/zip", "digest": "sha256:" + sha(data)},
            f"{BASE}/actions/runs/{RUN}/attempts/{ATTEMPT}": run,
            f"{BASE}/actions/workflows/{WORKFLOW}": {"id": WORKFLOW, "path": packages.WORKFLOW_PATH}}


def test_pack_preserves_every_source_byte_and_pending(source_package):
    manifest, compressed, data, _ = packages._unpack(source_package["zip"], attested=False)
    assert data == source_package["ready"] + source_package["pending"]
    assert gzip.decompress(compressed) == data
    assert manifest["observations"]["count"] == 3
    assert manifest["observations"]["ready"] == 2
    assert manifest["observations"]["pending"] == 1
    assert manifest["captured_at"] == {"first": "2026-10-13T18:10:11.319Z", "last": CAPTURE, "undated": 1}
    assert manifest["input_files"]["pending"]["sha256"] == sha(source_package["pending"])
    assert manifest["adapter_commit"] == SHA
    assert manifest["corpus_sha256"] == CORPUS


def test_pack_never_overwrites(source_package):
    before = source_package["output"].read_bytes()
    with pytest.raises(packages.PackageError, match="output_exists"):
        packages.pack(ready=source_package["ready_path"], pending=source_package["pending_path"],
                      corpus_sha256=CORPUS, output=source_package["output"])
    assert source_package["output"].read_bytes() == before


def test_field_origin_for_url_is_provenance_not_a_download_url():
    value = envelope()
    value["source"]["field_origins"] = {"url": {"observed_at": CAPTURE,
        "source_page": "https://www.idealista.com/alquiler-viviendas/madrid-madrid/"}}
    observations, capture, _ = packages._observations(jsonl(value))
    assert observations["count"] == 1 and capture["last"] == CAPTURE


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.5, "0", "01", "+1", " 1", "1/zip", "1?x=y",
                                 "https://api.github.com/1", "9223372036854775808"])
def test_ids_are_positive_integers_only(value):
    with pytest.raises(packages.PackageError, match="invalid_id"):
        packages._positive(value)


@pytest.mark.parametrize("change,code", [
    (lambda value: value["candidate"].update(portal="fotocasa"), "invalid_scope"),
    (lambda value: value["candidate"].update(property_id="temporary"), "invalid_scope"),
    (lambda value: value["candidate"]["property"].update(operation="commercial"), "invalid_scope"),
    (lambda value: value["candidate"]["property"].update(operation=[]), "invalid_scope"),
    (lambda value: value.update(status=[]), "invalid_envelope"),
    (lambda value: value["source"].update(token="secret"), "secret_field"),
    (lambda value: value["source"]["source_card"].update(url="https://user:password@www.idealista.com/inmueble/1/"), "unsafe_observation_url"),
    (lambda value: value["source"]["source_card"].update(url="https://www.idealista.com/inmueble/1/?token=secret"), "unsafe_observation_url"),
    (lambda value: value["source"]["source_card"].update(url="https://www.idealista.com/\ninmueble/1/"), "unsafe_observation_url"),
    (lambda value: value["source"]["source_card"].update(url="ftp://www.idealista.com/inmueble/1/"), "unsafe_observation_url"),
    (lambda value: value["source"]["source_card"].update(images=["data:image/png;base64,secret"]), "unsafe_observation_url"),
    (lambda value: value["source"].update(observed_at="yesterday"), "invalid_capture_date"),
])
def test_rejects_out_of_scope_and_secret_envelopes(change, code):
    value = envelope()
    change(value)
    with pytest.raises(packages.PackageError, match=code):
        packages._observations(jsonl(value))


def altered_package(source_package, *, change=None, members=None, duplicate=False, symlink=False):
    with ZipFile(io.BytesIO(source_package["zip"])) as archive:
        data = {name: archive.read(name) for name in archive.namelist()}
    if change:
        change(data)
    result = io.BytesIO()
    with ZipFile(result, "w", compression=ZIP_DEFLATED) as archive:
        for name in members or data:
            info = ZipInfo(name)
            if symlink and name == "observations.jsonl.gz":
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, data.get(name, b"unexpected"))
        if duplicate:
            archive.writestr("manifest.json", data["manifest.json"])
    return result.getvalue()


@pytest.mark.parametrize("members", [["manifest.json", "../observations.jsonl.gz"],
    ["manifest.json", "observations.jsonl.gz", "extra.txt"], ["manifest.json"],
    ["manifest.json", "/observations.jsonl.gz"]])
def test_zip_members_are_exact(source_package, members):
    with pytest.raises(packages.PackageError, match="invalid_zip_members"):
        packages._unpack(altered_package(source_package, members=members), attested=False)


def test_zip_duplicate_and_symlink_are_rejected(source_package):
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate = altered_package(source_package, duplicate=True)
    with pytest.raises(packages.PackageError, match="invalid_zip_members"):
        packages._unpack(duplicate, attested=False)
    with pytest.raises(packages.PackageError, match="invalid_zip_entry"):
        packages._unpack(altered_package(source_package, symlink=True), attested=False)


@pytest.mark.parametrize("field,value", [("sha256", "c" * 64), ("count", 99), ("pending", 0), ("ready", 3),
                                        ("pending", True), ("count", 3.0)])
def test_manifest_sha_counts_cannot_be_relabelled(source_package, field, value):
    def change(data):
        manifest = json.loads(data["manifest.json"])
        manifest["observations"][field] = value
        data["manifest.json"] = packages._json_bytes(manifest)
    with pytest.raises(packages.PackageError, match="content_manifest_mismatch"):
        packages._unpack(altered_package(source_package, change=change), attested=False)


def test_gzip_bomb_is_bounded_before_json_parsing(source_package, monkeypatch):
    monkeypatch.setattr(packages, "MAX_BYTES", 4096)
    def change(data):
        data["observations.jsonl.gz"] = gzip.compress(b"x" * 100_000)
    with pytest.raises(packages.PackageError, match="gzip_decompressed_too_large"):
        packages._unpack(altered_package(source_package, change=change), attested=False)


def test_manifest_size_bound(source_package, monkeypatch):
    monkeypatch.setattr(packages, "MAX_MANIFEST_BYTES", 20)
    with pytest.raises(packages.PackageError, match="manifest_too_large"):
        packages._unpack(source_package["zip"], attested=False)


def test_freeze_reads_only_exact_asset_and_attests_source(source_package, tmp_path, producer_env):
    manifest, requests = freeze_source(source_package, tmp_path, producer_env)
    assert manifest["attestation"]["source"]["sha256"] == sha(source_package["zip"])
    assert manifest["attestation"]["run_id"] == RUN
    assert manifest["attestation"]["run_attempt"] == ATTEMPT
    assert {path.name for path in (tmp_path / "frozen").iterdir()} == packages.MEMBERS
    assert gzip.decompress((tmp_path / "frozen/observations.jsonl.gz").read_bytes()) == source_package["ready"] + source_package["pending"]
    assert all(request.method == "GET" for request in requests)
    assert all(request.headers["authorization"] == "Bearer test-token" for request in requests)
    assert all("cookie" not in request.headers for request in requests)
    asset_requests = [request for request in requests if request.url.path.endswith(f"/releases/assets/{ASSET}")]
    assert [request.headers["accept"] for request in asset_requests] == [
        "application/vnd.github+json", "application/octet-stream"]


@pytest.mark.parametrize("mutate,code", [
    (lambda routes: routes[BASE].update(fork=True), "untrusted_repository"),
    (lambda routes: routes[BASE].update(private=True), "untrusted_repository"),
    (lambda routes: routes[f"{BASE}/releases/{RELEASE}"].update(draft=True), "untrusted_release"),
    (lambda routes: routes[f"{BASE}/releases/{RELEASE}"]["author"].update(login="someone-else"), "unauthorized_source_author"),
    (lambda routes: routes[f"{BASE}/releases/{RELEASE}"].update(assets=[]), "asset_not_in_release"),
    (lambda routes: routes[f"{BASE}/releases/assets/{ASSET}"].update(state="new"), "untrusted_asset"),
    (lambda routes: routes[f"{BASE}/releases/assets/{ASSET}"].update(name="other.zip"), "untrusted_asset"),
    (lambda routes: routes[f"{BASE}/releases/assets/{ASSET}"]["uploader"].update(login="someone-else"), "unauthorized_source_author"),
    (lambda routes: routes[f"{BASE}/releases/assets/{ASSET}"].update(size=packages.MAX_BYTES + 1), "asset_too_large"),
])
def test_freeze_rejects_untrusted_metadata_before_download(source_package, tmp_path, producer_env, mutate, code):
    routes = freeze_routes(source_package["zip"])
    mutate(routes)
    with pytest.raises(packages.PackageError, match=code):
        freeze_source(source_package, tmp_path, producer_env, routes=routes)
    assert not (tmp_path / "frozen").exists()


@pytest.mark.parametrize("key,value", [("GITHUB_REF", "refs/heads/feature"), ("GITHUB_EVENT_NAME", "pull_request"),
    ("GITHUB_REPOSITORY_ID", "1"), ("GITHUB_SHA", "bad"), ("GITHUB_RUN_ATTEMPT", "0")])
def test_freeze_requires_main_manual_environment(source_package, tmp_path, producer_env, key, value):
    producer_env[key] = value
    with pytest.raises(packages.PackageError):
        freeze_source(source_package, tmp_path, producer_env)


def test_source_sha_is_verified(source_package, tmp_path, producer_env):
    with client_for(freeze_routes(source_package["zip"]),
                    {f"{BASE}/releases/assets/{ASSET}": source_package["zip"]}, []) as client:
        with pytest.raises(packages.PackageError, match="source_sha256_mismatch"):
            packages.freeze(repository=packages.REPOSITORY, release_id=RELEASE, asset_id=ASSET,
                sha256="c" * 64, output_dir=tmp_path / "frozen", client=client, env=producer_env)


def test_cross_host_redirect_drops_all_auth_even_client_defaults(source_package, tmp_path, producer_env):
    target = "https://release-assets.githubusercontent.com/asset?signature=test"
    downloads = {f"{BASE}/releases/assets/{ASSET}": httpx.Response(302, headers={"location": target}),
                 target: source_package["zip"]}
    _, requests = freeze_source(source_package, tmp_path, producer_env, downloads=downloads)
    last = requests[-1]
    assert last.url.host == "release-assets.githubusercontent.com"
    assert "authorization" not in last.headers and "cookie" not in last.headers


@pytest.mark.parametrize("target", [
    "https://release-assets.githubusercontent.com/asset?signature=dummy-signature",
    "https://productionresultssa4.blob.core.windows.net/actions-artifacts/test?sig=dummy-signature",
])
def test_client_basic_auth_cannot_override_api_bearer_or_reach_signed_redirect(target):
    requests = []
    source = f"{BASE}/releases/assets/{ASSET}"

    def handle(request):
        requests.append(request)
        if str(request.url) == source:
            return httpx.Response(302, headers={"location": target})
        assert str(request.url) == target
        return httpx.Response(200, content=b"mock-package")

    with httpx.Client(transport=httpx.MockTransport(handle),
                      auth=httpx.BasicAuth("dummy-user", "dummy-password"),
                      headers={"Cookie": "dummy-cookie"}) as client:
        assert packages._GitHub(client, "test-token").get(
            source, binary=True, api_accept="application/octet-stream") == b"mock-package"
    assert len(requests) == 2
    assert "authorization" not in requests[1].headers
    assert "cookie" not in requests[1].headers
    assert "proxy-authorization" not in requests[1].headers
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert requests[0].headers["accept"] == "application/octet-stream"
    assert requests[0].headers["x-github-api-version"] == "2022-11-28"


@pytest.mark.parametrize("target", ["http://release-assets.githubusercontent.com/asset", "https://evil.example/asset",
    "https://release-assets.githubusercontent.com.evil.example/asset", "https://user:secret@objects.githubusercontent.com/a",
    "https://objects.githubusercontent.com:443/a", "https://bad.blob.core.windows.net.evil.example/a",
    "https://attackeraccount.blob.core.windows.net/a",
    "https://objects.githubusercontent.com/a\nsecret"])
def test_redirect_origins_are_strict(source_package, tmp_path, producer_env, target):
    downloads = {f"{BASE}/releases/assets/{ASSET}": httpx.Response(302, headers={"location": target})}
    with pytest.raises(packages.PackageError, match="unsafe_download_url|github_network_failed"):
        freeze_source(source_package, tmp_path, producer_env, downloads=downloads)


def test_fetch_validates_attempt_and_keeps_pending(source_package, tmp_path, producer_env):
    freeze_source(source_package, tmp_path, producer_env)
    data = frozen_zip(tmp_path / "frozen")
    requests = []
    with client_for(fetch_routes(data), {f"{BASE}/actions/artifacts/{ARTIFACT}/zip": data}, requests) as client:
        manifest = packages.fetch(repository=packages.REPOSITORY, artifact_id=str(ARTIFACT),
            output_dir=tmp_path / "fetched", client=client, env={"GH_TOKEN": "test-token"})
    assert (tmp_path / "fetched/observations.jsonl").read_bytes() == source_package["ready"] + source_package["pending"]
    assert manifest["observations"]["pending"] == 1
    assert manifest["observations"]["count"] == 3
    assert all(request.method == "GET" for request in requests)
    assert any(str(request.url).endswith(f"/attempts/{ATTEMPT}") for request in requests)


@pytest.mark.parametrize("digest_matches", [True, False], ids=["valid-digest", "invalid-digest"])
def test_fetch_negotiates_json_for_artifacts_and_verifies_digest(source_package, tmp_path, producer_env, digest_matches):
    freeze_source(source_package, tmp_path, producer_env)
    data = frozen_zip(tmp_path / "frozen")
    routes = fetch_routes(data)
    if not digest_matches:
        routes[f"{BASE}/actions/artifacts/{ARTIFACT}"]["digest"] = "sha256:" + "c" * 64
    target = "https://productionresultssa4.blob.core.windows.net/actions-artifacts/test?sig=dummy-signature"
    downloads = {f"{BASE}/actions/artifacts/{ARTIFACT}/zip": httpx.Response(302, headers={"location": target}),
                 target: data}
    requests = []
    with client_for(routes, downloads, requests) as client:
        if digest_matches:
            manifest = packages.fetch(repository=packages.REPOSITORY, artifact_id=ARTIFACT,
                output_dir=tmp_path / "fetched", client=client, env=producer_env)
            assert manifest["observations"]["pending"] == 1
            assert (tmp_path / "fetched/observations.jsonl").read_bytes() == source_package["ready"] + source_package["pending"]
        else:
            with pytest.raises(packages.PackageError, match="artifact_sha256_mismatch"):
                packages.fetch(repository=packages.REPOSITORY, artifact_id=ARTIFACT,
                    output_dir=tmp_path / "fetched", client=client, env=producer_env)
            assert not (tmp_path / "fetched").exists()
    api_requests = [request for request in requests if request.url.host == "api.github.com"]
    assert all(request.headers["accept"] == "application/vnd.github+json" for request in api_requests)
    assert all(request.headers["authorization"] == "Bearer test-token" for request in api_requests)
    assert all("cookie" not in request.headers for request in api_requests)
    assert requests[-1].url.host == "productionresultssa4.blob.core.windows.net"
    assert requests[-1].headers["accept"] == "application/octet-stream"
    assert all(header not in requests[-1].headers for header in ("authorization", "cookie", "proxy-authorization"))


def test_consumer_azure_download_has_no_credentials(source_package, tmp_path, producer_env):
    freeze_source(source_package, tmp_path, producer_env)
    data = frozen_zip(tmp_path / "frozen")
    target = "https://productionresultssa4.blob.core.windows.net/actions-artifacts/test?sig=signed"
    downloads = {f"{BASE}/actions/artifacts/{ARTIFACT}/zip": httpx.Response(302, headers={"location": target}), target: data}
    requests = []
    with client_for(fetch_routes(data), downloads, requests) as client:
        packages.fetch(repository=packages.REPOSITORY, artifact_id=ARTIFACT, output_dir=tmp_path / "fetched",
                       client=client, env=producer_env)
    assert requests[-1].url.host == "productionresultssa4.blob.core.windows.net"
    assert requests[-2].headers["accept"] == "application/vnd.github+json"
    assert requests[-1].headers["accept"] == "application/octet-stream"
    assert "authorization" not in requests[-1].headers and "cookie" not in requests[-1].headers


@pytest.mark.parametrize("target,key,value,code", [
    ("artifact", "expired", True, "untrusted_artifact"),
    ("artifact", "expires_at", "2020-01-01T00:00:00Z", "expired_artifact"),
    ("artifact", "size_in_bytes", packages.MAX_BYTES + 1, "artifact_too_large"),
    ("artifact", "name", "idealista-observations-999-2", "unexpected_artifact_name"),
    ("artifact", "digest", "sha256:" + "c" * 64, "artifact_sha256_mismatch"),
    ("origin", "repository_id", 1, "untrusted_artifact_origin"),
    ("origin", "head_repository_id", 1, "untrusted_artifact_origin"),
    ("origin", "head_branch", "feature", "untrusted_artifact_origin"),
    ("run", "event", "pull_request", "untrusted_producer_run"),
    ("run", "conclusion", "failure", "untrusted_producer_run"),
    ("run", "status", "in_progress", "untrusted_producer_run"),
    ("run", "run_attempt", 1, "untrusted_producer_run"),
    ("run", "head_sha", "d" * 40, "untrusted_producer_run"),
    ("run", "path", ".github/workflows/scrape.yml", "unexpected_producer_workflow"),
    ("run", "path", [], "unexpected_producer_workflow"),
    ("run", "pull_requests", [{"number": 1}], "untrusted_producer_run"),
    ("workflow", "path", ".github/workflows/scrape.yml", "unexpected_producer_workflow"),
])
def test_consumer_rejects_metadata_mismatch(source_package, tmp_path, producer_env, target, key, value, code):
    freeze_source(source_package, tmp_path, producer_env)
    data = frozen_zip(tmp_path / "frozen")
    routes = fetch_routes(data)
    artifact = routes[f"{BASE}/actions/artifacts/{ARTIFACT}"]
    targets = {"artifact": artifact, "origin": artifact["workflow_run"],
               "run": routes[f"{BASE}/actions/runs/{RUN}/attempts/{ATTEMPT}"],
               "workflow": routes[f"{BASE}/actions/workflows/{WORKFLOW}"]}
    targets[target][key] = value
    with client_for(routes, {f"{BASE}/actions/artifacts/{ARTIFACT}/zip": data}, []) as client:
        with pytest.raises(packages.PackageError, match=code):
            packages.fetch(repository=packages.REPOSITORY, artifact_id=ARTIFACT, output_dir=tmp_path / "fetched",
                           client=client, env=producer_env)
    assert not (tmp_path / "fetched").exists()


def test_consumer_rejects_fork_head(source_package, tmp_path, producer_env):
    freeze_source(source_package, tmp_path, producer_env)
    data = frozen_zip(tmp_path / "frozen")
    routes = fetch_routes(data)
    routes[f"{BASE}/actions/runs/{RUN}/attempts/{ATTEMPT}"]["head_repository"]["fork"] = True
    with client_for(routes, {}, []) as client:
        with pytest.raises(packages.PackageError, match="untrusted_run_repository"):
            packages.fetch(repository=packages.REPOSITORY, artifact_id=ARTIFACT, output_dir=tmp_path / "fetched",
                           client=client, env=producer_env)


def test_attestation_is_bound_to_source_manifest_and_run(source_package, tmp_path, producer_env):
    manifest, _ = freeze_source(source_package, tmp_path, producer_env)
    run = fetch_routes(b"test")[f"{BASE}/actions/runs/{RUN}/attempts/{ATTEMPT}"]
    manifest["corpus_sha256"] = "c" * 64
    with pytest.raises(packages.PackageError, match="parent_manifest_mismatch"):
        packages._validate_attestation(manifest, run, {})
    manifest["attestation"]["producer_sha"] = "d" * 40
    with pytest.raises(packages.PackageError, match="attestation_run_mismatch"):
        packages._validate_attestation(manifest, run, {})


def test_download_stream_limit_even_without_content_length():
    with client_for({}, {f"{BASE}/actions/artifacts/1/zip": b"x" * 101}, []) as client:
        with pytest.raises(packages.PackageError, match="download_too_large"):
            packages._GitHub(client, "test-token").get(f"{BASE}/actions/artifacts/1/zip", binary=True, limit=100)


def test_cli_reports_only_safe_diagnostic(tmp_path, capsys):
    assert packages.main(["fetch", "--repository", packages.REPOSITORY, "--artifact-id", "https://secret.example/",
                          "--output-dir", str(tmp_path / "output")]) == 1
    output = capsys.readouterr().out
    assert "https://secret.example" not in output
    assert "Traceback" not in output


def test_producer_workflow_is_main_manual_and_read_only():
    text = (Path(__file__).parents[1] / packages.WORKFLOW_PATH).read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text and "refs/heads/main" in text
    assert "contents: read" in text and "runs-on: ubuntu-latest" in text
    assert "actions/upload-artifact@v4" in text
    assert "idealista-observations-${{ github.run_id }}-${{ github.run_attempt }}" in text
    assert '${{ inputs.' not in text.split("        run: |", 1)[1]
    assert "INGEST_TOKEN" not in text and "index-durable-state" not in text and "restore" not in text
