"""Bounded, immutable transport for saved public Idealista observations.

This module reads GitHub metadata and bytes only. It never opens State, delivers
listings, visits portal URLs, or downloads the images retained in the envelopes.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import zlib
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

import httpx


REPOSITORY = "trading-optimizer-lab-org/index-scrapers"
REPOSITORY_ID = 1415957925
WORKFLOW_PATH = ".github/workflows/freeze-observations.yml"
ASSET_NAME = "idealista-observations.zip"
MAX_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ENVELOPE_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MEMBERS = {"manifest.json", "observations.jsonl.gz"}
SCOPE = {"portal": "idealista", "country": "ES", "operations": ["rent", "sale"]}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
_BASE = f"https://api.github.com/repos/{REPOSITORY}"
_SECRET_KEYS = {"authorization", "cookie", "cookies", "password", "secret", "token", "access_token"}


class PackageError(ValueError):
    """A stable diagnostic code, without untrusted URLs or credentials."""


def _require(condition, code):
    if not condition:
        raise PackageError(code)


def _positive(value):
    _require(type(value) is int or isinstance(value, str), "invalid_id")
    text = str(value)
    _require(re.fullmatch(r"[1-9]\d{0,18}", text) is not None, "invalid_id")
    result = int(text)
    _require(result <= 9223372036854775807, "invalid_id")
    return result


def _hash(value):
    _require(isinstance(value, str) and _HASH.fullmatch(value) is not None, "invalid_sha256")
    return value


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(PackageError("invalid_json")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError("invalid_json") from None


def _date(value):
    _require(isinstance(value, str) and _DATE.fullmatch(value) is not None, "invalid_capture_date")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        _require(result.timestamp() >= 0, "invalid_capture_date")
        return result
    except (ValueError, OverflowError):
        raise PackageError("invalid_capture_date") from None


def _public_observation_url(value):
    _require(not re.search(r"[\x00-\x20\x7f\\]", value), "unsafe_observation_url")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        _require(parsed.scheme == "https" and (host in {"idealista.com", "www.idealista.com"}
                 or re.fullmatch(r"img\d+\.idealista\.com", host) is not None)
                 and parsed.username is None and parsed.password is None and parsed.port is None
                 and not parsed.query and not parsed.fragment
                 and not any(part in {".", ".."} for part in parsed.path.split("/"))
                 and not re.search(r"%(?:2e|2f|5c|25)", parsed.path, re.I), "unsafe_observation_url")
    except ValueError:
        raise PackageError("unsafe_observation_url") from None


def _scan(value, dates, depth=0, field_origins=False):
    _require(depth <= 40, "envelope_too_deep")
    if isinstance(value, dict):
        for key, child in value.items():
            _require(key.casefold() not in _SECRET_KEYS, "secret_field")
            if key in {"observed_at", "captured_at"} and child is not None:
                dates.append((_date(child), child))
            if (key in {"url", "source_page", "cover_image"} and child is not None
                    and not (field_origins and key == "url" and isinstance(child, dict))):
                _require(isinstance(child, str), "unsafe_observation_url")
                _public_observation_url(child)
            if key in {"images", "source_pages"} and isinstance(child, list):
                for url in child:
                    _require(isinstance(url, str), "unsafe_observation_url")
                    _public_observation_url(url)
            _scan(child, dates, depth + 1, field_origins=key == "field_origins")
    elif isinstance(value, list):
        for child in value:
            _scan(child, dates, depth + 1)
    elif isinstance(value, str):
        _require(not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value), "control_character")
        if re.match(r"(?i)[a-z][a-z0-9+.-]*://", value):
            _public_observation_url(value)
        _require(not re.search(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", value),
                 "secret_value")


def _observations(data):
    _require(0 < len(data) <= MAX_BYTES and data.endswith(b"\n"), "invalid_observation_size")
    counts, operations, source_types = Counter(), Counter(), Counter()
    dates = []
    undated = 0
    seen_pending = False
    ready_end = 0
    for line in io.BytesIO(data):
        _require(0 < len(line) <= MAX_ENVELOPE_BYTES and line.strip(), "invalid_envelope_size")
        envelope = _json(line)
        _require(isinstance(envelope, dict) and set(envelope) ==
                 {"status", "candidate", "reasons", "source", "evidence"}, "invalid_envelope")
        status = envelope["status"]
        _require(isinstance(status, str) and status in {"ready", "pending"} and isinstance(envelope["candidate"], dict)
                 and isinstance(envelope["source"], dict) and isinstance(envelope["evidence"], dict)
                 and isinstance(envelope["reasons"], list)
                 and all(isinstance(reason, str) for reason in envelope["reasons"]), "invalid_envelope")
        _require(status != "ready" or not seen_pending, "invalid_partition_order")
        seen_pending |= status == "pending"
        if status == "ready":
            ready_end += len(line)
        candidate = envelope["candidate"]
        _require(candidate.get("portal") == "idealista" and "property_id" not in candidate
                 and isinstance(candidate.get("property"), dict), "invalid_scope")
        operation = candidate["property"].get("operation")
        _require((operation is None or isinstance(operation, str) and operation in {"rent", "sale"})
                 and (status == "pending" or operation is not None),
                 "invalid_scope")
        source = envelope["source"]
        source_type = source.get("source")
        _require(isinstance(source_type, str) and 0 < len(source_type) <= 120
                 and isinstance(source.get("source_card"), dict), "missing_original_source")
        before = len(dates)
        _scan(envelope, dates)
        undated += len(dates) == before
        counts[status] += 1
        operations[operation or "unknown"] += 1
        source_types[source_type] += 1
    capture = {"first": min(dates)[1] if dates else None, "last": max(dates)[1] if dates else None,
               "undated": undated}
    return {"sha256": _sha(data), "bytes": len(data), "count": sum(counts.values()),
            "ready": counts["ready"], "pending": counts["pending"],
            "by_operation": dict(sorted(operations.items())), "by_source": dict(sorted(source_types.items()))}, capture, ready_end


def _input_manifest(data, count):
    return {"sha256": _sha(data), "bytes": len(data), "count": count}


def _bounded_file(path):
    path = Path(path)
    _require(not path.is_symlink() and path.is_file(), "invalid_input_file")
    before = path.stat()
    _require(before.st_size <= MAX_BYTES, "input_too_large")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    after = path.stat()
    _require(len(data) <= MAX_BYTES and (before.st_size, before.st_mtime_ns, before.st_ino) ==
             (after.st_size, after.st_mtime_ns, after.st_ino), "input_changed")
    return data


def _adapter_commit():
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, check=False)
    value = result.stdout.strip()
    _require(result.returncode == 0 and _COMMIT.fullmatch(value) is not None, "missing_adapter_commit")
    return value


def _archive(manifest, compressed):
    raw = _json_bytes(manifest)
    _require(len(raw) <= MAX_MANIFEST_BYTES, "manifest_too_large")
    result = io.BytesIO()
    with ZipFile(result, "w", compression=ZIP_STORED) as archive:
        archive.writestr("manifest.json", raw)
        archive.writestr("observations.jsonl.gz", compressed)
    data = result.getvalue()
    _require(len(data) <= MAX_BYTES, "archive_too_large")
    return data


def pack(*, ready, pending, corpus_sha256, output):
    """Create a new ZIP; input envelopes and their original JSONL bytes survive."""
    _hash(corpus_sha256)
    output = Path(output)
    _require(not output.exists(), "output_exists")
    ready_data, pending_data = _bounded_file(ready), _bounded_file(pending)
    data = ready_data + pending_data
    observations, capture, ready_end = _observations(data)
    _require(ready_end == len(ready_data), "invalid_partition")
    manifest = {"schema_version": 1, "scope": SCOPE, "adapter_commit": _adapter_commit(),
                "corpus_sha256": corpus_sha256, "observations": observations, "captured_at": capture,
                "input_files": {"ready": _input_manifest(ready_data, observations["ready"]),
                                "pending": _input_manifest(pending_data, observations["pending"])}}
    package = _archive(manifest, gzip.compress(data, mtime=0))
    with output.open("xb") as stream:
        stream.write(package)
    return {"sha256": _sha(package), "size_in_bytes": len(package), "manifest": manifest}


def _unpack(package, *, attested):
    _require(0 < len(package) <= MAX_BYTES, "archive_too_large")
    try:
        with ZipFile(io.BytesIO(package)) as archive:
            entries = archive.infolist()
            _require(len(entries) == 2 and {entry.filename for entry in entries} == MEMBERS, "invalid_zip_members")
            _require(sum(entry.file_size for entry in entries) <= MAX_BYTES, "zip_decompressed_too_large")
            for entry in entries:
                mode = entry.external_attr >> 16
                _require(not entry.is_dir() and not stat.S_ISLNK(mode) and not entry.flag_bits & 1
                         and (not stat.S_IFMT(mode) or stat.S_ISREG(mode))
                         and entry.compress_size <= MAX_BYTES and entry.compress_type in {ZIP_STORED, ZIP_DEFLATED},
                         "invalid_zip_entry")
            info = archive.getinfo("manifest.json")
            _require(info.file_size <= MAX_MANIFEST_BYTES, "manifest_too_large")
            with archive.open(info) as stream:
                raw_manifest = stream.read(MAX_MANIFEST_BYTES + 1)
            with archive.open("observations.jsonl.gz") as stream:
                compressed = stream.read(MAX_BYTES + 1)
    except (BadZipFile, RuntimeError, NotImplementedError, OSError):
        raise PackageError("invalid_zip") from None
    _require(len(raw_manifest) <= MAX_MANIFEST_BYTES and len(compressed) <= MAX_BYTES, "archive_too_large")
    manifest = _json(raw_manifest)
    required = {"schema_version", "scope", "adapter_commit", "corpus_sha256", "observations", "captured_at", "input_files"}
    _require(isinstance(manifest, dict) and set(manifest) == required | ({"attestation"} if attested else set()),
             "invalid_manifest")
    _require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
             and manifest["scope"] == SCOPE and isinstance(manifest["adapter_commit"], str)
             and _COMMIT.fullmatch(manifest["adapter_commit"]) is not None, "invalid_manifest")
    _hash(manifest["corpus_sha256"])
    _require(raw_manifest == _json_bytes(manifest), "noncanonical_manifest")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            data = stream.read(MAX_BYTES + 1)
    except (OSError, EOFError, zlib.error):
        raise PackageError("invalid_gzip") from None
    _require(len(data) <= MAX_BYTES, "gzip_decompressed_too_large")
    observations, capture, ready_end = _observations(data)
    _require(_json_bytes(manifest["observations"]) == _json_bytes(observations)
             and _json_bytes(manifest["captured_at"]) == _json_bytes(capture), "content_manifest_mismatch")
    _require(_json_bytes(manifest["input_files"]) == _json_bytes({
        "ready": _input_manifest(data[:ready_end], observations["ready"]),
        "pending": _input_manifest(data[ready_end:], observations["pending"])}), "input_manifest_mismatch")
    return manifest, compressed, data, raw_manifest


def _github_url(url, *, redirect=False):
    _require(isinstance(url, str) and len(url) <= 8192 and not re.search(r"[\x00-\x20\x7f\\]", url), "unsafe_download_url")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        allowed = host == "api.github.com"
        if redirect:
            # Actions downloads use GitHub's Azure artifact store; release assets
            # use the dedicated GitHub hosts. DNS suffix boundaries are explicit.
            allowed |= host in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
            allowed |= re.fullmatch(r"productionresultssa\d+\.blob\.core\.windows\.net", host) is not None
        _require(parts.scheme == "https" and parts.username is None and parts.password is None
                 and parts.port is None and not parts.fragment and allowed, "unsafe_download_url")
        return host
    except ValueError:
        raise PackageError("unsafe_download_url") from None


class _GitHub:
    def __init__(self, client, token):
        self.client, self.token = client, token
        _require(isinstance(token, str) and bool(token) and not re.search(r"[\x00-\x20\x7f]", token), "missing_github_token")

    def get(self, url, *, binary=False, limit=MAX_METADATA_BYTES, api_accept="application/vnd.github+json"):
        for hop in range(4):
            host = _github_url(url, redirect=binary and hop > 0)
            request = self.client.build_request("GET", url)
            # Explicitly remove client defaults before any cross-origin download.
            for header in ("authorization", "cookie", "proxy-authorization"):
                request.headers.pop(header, None)
            # Artifact ZIP endpoints negotiate JSON; release assets require octet-stream.
            request.headers["Accept"] = api_accept if host == "api.github.com" else "application/octet-stream"
            request.headers["X-GitHub-Api-Version"] = "2022-11-28"
            if host == "api.github.com":
                request.headers["Authorization"] = "Bearer " + self.token
            try:
                response = self.client.send(request, auth=None, stream=True, follow_redirects=False)
                try:
                    if binary and response.status_code == 302 and hop < 3:
                        url = response.headers.get("location")
                        _github_url(url, redirect=True)
                        continue
                    _require(response.status_code == 200, "github_download_failed" if binary else "github_metadata_failed")
                    length = response.headers.get("content-length")
                    _require(length is None or (length.isdigit() and int(length) <= limit), "download_too_large")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        _require(size <= limit, "download_too_large")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                finally:
                    response.close()
                return content if binary else _json(content)
            except httpx.HTTPError:
                raise PackageError("github_network_failed") from None
        raise PackageError("github_redirect_limit")

    def repository(self, repository):
        _require(repository == REPOSITORY, "unexpected_repository")
        result = self.get(_BASE)
        _require(isinstance(result, dict) and type(result.get("id")) is int and result["id"] == REPOSITORY_ID
                 and result.get("full_name") == REPOSITORY and result.get("private") is False
                 and result.get("fork") is False and result.get("default_branch") == "main", "untrusted_repository")
        return result


def _actor(value):
    _require(isinstance(value, str) and _LOGIN.fullmatch(value) is not None, "invalid_actor")
    return value


def _identity(value, actor):
    _require(isinstance(value, dict) and isinstance(value.get("login"), str)
             and value["login"].casefold() == actor.casefold(), "unauthorized_source_author")
    _positive(value.get("id"))


def _producer_environment(env):
    _require(env.get("GITHUB_REPOSITORY") == REPOSITORY and env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID)
             and env.get("GITHUB_REF") == "refs/heads/main" and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
             "untrusted_producer_environment")
    sha = env.get("GITHUB_SHA")
    _require(isinstance(sha, str) and _COMMIT.fullmatch(sha) is not None, "invalid_producer_sha")
    return {"repository": REPOSITORY, "repository_id": REPOSITORY_ID, "workflow": WORKFLOW_PATH,
            "event": "workflow_dispatch", "ref": "refs/heads/main", "producer_sha": sha,
            "run_id": _positive(env.get("GITHUB_RUN_ID")), "run_attempt": _positive(env.get("GITHUB_RUN_ATTEMPT")),
            "actor": _actor(env.get("GITHUB_ACTOR"))}


def _write_directory(output_dir, manifest, compressed, data=None):
    output = Path(output_dir)
    output.mkdir(parents=False, exist_ok=False)
    contents = {"manifest.json": _json_bytes(manifest), "observations.jsonl.gz": compressed}
    if data is not None:
        contents["observations.jsonl"] = data
    for name, content in contents.items():
        with (output / name).open("xb") as stream:
            stream.write(content)


def _freeze(github, *, repository, release_id, asset_id, sha256, output_dir, env):
    release_id, asset_id, sha256 = _positive(release_id), _positive(asset_id), _hash(sha256)
    _require(not Path(output_dir).exists(), "output_exists")
    attestation = _producer_environment(env)
    github.repository(repository)
    release = github.get(f"{_BASE}/releases/{release_id}")
    _require(isinstance(release, dict) and type(release.get("id")) is int and release["id"] == release_id and release.get("draft") is False
             and release.get("url") == f"{_BASE}/releases/{release_id}" and isinstance(release.get("assets"), list),
             "untrusted_release")
    _identity(release.get("author"), attestation["actor"])
    entries = [entry for entry in release["assets"] if isinstance(entry, dict) and entry.get("id") == asset_id]
    _require(len(entries) == 1, "asset_not_in_release")
    asset = github.get(f"{_BASE}/releases/assets/{asset_id}")
    _require(isinstance(asset, dict) and type(asset.get("id")) is int and asset["id"] == asset_id and asset.get("name") == ASSET_NAME
             and asset.get("state") == "uploaded" and asset.get("url") == f"{_BASE}/releases/assets/{asset_id}", "untrusted_asset")
    _identity(asset.get("uploader"), attestation["actor"])
    size = _positive(asset.get("size"))
    _require(size <= MAX_BYTES, "asset_too_large")
    _require(all(entries[0].get(key) == asset.get(key) for key in ("name", "state", "size", "uploader", "url")),
             "asset_metadata_mismatch")
    package = github.get(f"{_BASE}/releases/assets/{asset_id}", binary=True, limit=MAX_BYTES,
                         api_accept="application/octet-stream")
    _require(len(package) == size and _sha(package) == sha256, "source_sha256_mismatch")
    manifest, compressed, _, raw_manifest = _unpack(package, attested=False)
    attestation["source"] = {"release_id": release_id, "asset_id": asset_id, "name": ASSET_NAME,
                             "sha256": sha256, "size_in_bytes": size, "manifest_sha256": _sha(raw_manifest),
                             "release_author": release["author"]["login"], "asset_uploader": asset["uploader"]["login"]}
    manifest["attestation"] = attestation
    _require(len(_json_bytes(manifest)) <= MAX_MANIFEST_BYTES, "manifest_too_large")
    _write_directory(output_dir, manifest, compressed)
    return manifest


def _validate_run(run, run_id, attempt, head_sha):
    _require(isinstance(run, dict) and type(run.get("id")) is int and run["id"] == run_id
             and type(run.get("run_attempt")) is int and run["run_attempt"] == attempt
             and run.get("head_sha") == head_sha and run.get("head_branch") == "main"
             and run.get("event") == "workflow_dispatch" and run.get("status") == "completed"
             and run.get("conclusion") == "success" and run.get("pull_requests") == [], "untrusted_producer_run")
    for name in ("repository", "head_repository"):
        repository = run.get(name)
        _require(isinstance(repository, dict) and repository.get("id") == REPOSITORY_ID
                 and repository.get("full_name") == REPOSITORY and repository.get("fork") is False
                 and repository.get("private") is False, "untrusted_run_repository")
    _require(isinstance(run.get("path"), str)
             and run["path"] in {WORKFLOW_PATH, WORKFLOW_PATH + "@refs/heads/main", WORKFLOW_PATH + "@main"},
             "unexpected_producer_workflow")
    _positive(run.get("workflow_id"))
    _require(isinstance(run.get("actor"), dict), "invalid_actor")
    _actor(run["actor"].get("login"))


def _validate_attestation(manifest, run, artifact):
    attestation = manifest["attestation"]
    expected = {"repository": REPOSITORY, "repository_id": REPOSITORY_ID, "workflow": WORKFLOW_PATH,
                "event": "workflow_dispatch", "ref": "refs/heads/main", "producer_sha": run["head_sha"],
                "run_id": run["id"], "run_attempt": run["run_attempt"], "actor": run["actor"]["login"]}
    _require(isinstance(attestation, dict) and set(attestation) == set(expected) | {"source"}
             and _json_bytes({key: attestation[key] for key in expected}) == _json_bytes(expected),
             "attestation_run_mismatch")
    source = attestation["source"]
    _require(isinstance(source, dict) and set(source) == {"release_id", "asset_id", "name", "sha256", "size_in_bytes",
             "manifest_sha256", "release_author", "asset_uploader"} and source["name"] == ASSET_NAME, "invalid_source_attestation")
    _positive(source["release_id"])
    _positive(source["asset_id"])
    _require(_positive(source["size_in_bytes"]) <= MAX_BYTES, "invalid_source_attestation")
    _hash(source["sha256"])
    _hash(source["manifest_sha256"])
    _require(all(isinstance(source[key], str) and source[key].casefold() == expected["actor"].casefold()
                 for key in ("release_author", "asset_uploader")), "unauthorized_source_author")
    parent = {key: value for key, value in manifest.items() if key != "attestation"}
    _require(_sha(_json_bytes(parent)) == source["manifest_sha256"], "parent_manifest_mismatch")


def _fetch(github, *, repository, artifact_id, output_dir):
    artifact_id = _positive(artifact_id)
    _require(not Path(output_dir).exists(), "output_exists")
    github.repository(repository)
    artifact = github.get(f"{_BASE}/actions/artifacts/{artifact_id}")
    _require(isinstance(artifact, dict) and type(artifact.get("id")) is int and artifact["id"] == artifact_id and artifact.get("expired") is False,
             "untrusted_artifact")
    size = _positive(artifact.get("size_in_bytes"))
    _require(size <= MAX_BYTES, "artifact_too_large")
    _require(_date(artifact.get("expires_at")) > datetime.now(timezone.utc), "expired_artifact")
    origin = artifact.get("workflow_run")
    _require(isinstance(origin, dict) and origin.get("repository_id") == REPOSITORY_ID
             and origin.get("head_repository_id") == REPOSITORY_ID and origin.get("head_branch") == "main"
             and isinstance(origin.get("head_sha"), str) and _COMMIT.fullmatch(origin["head_sha"]) is not None,
             "untrusted_artifact_origin")
    run_id = _positive(origin.get("id"))
    _require(isinstance(artifact.get("name"), str), "unexpected_artifact_name")
    match = re.fullmatch(r"idealista-observations-([1-9]\d*)-([1-9]\d*)", artifact["name"])
    _require(match is not None and _positive(match[1]) == run_id, "unexpected_artifact_name")
    attempt = _positive(match[2])
    # An immutable artifact from an earlier successful attempt stays usable even
    # if that run is later rerun. Validate the exact producing attempt.
    run = github.get(f"{_BASE}/actions/runs/{run_id}/attempts/{attempt}")
    _validate_run(run, run_id, attempt, origin["head_sha"])
    workflow = github.get(f"{_BASE}/actions/workflows/{run['workflow_id']}")
    _require(isinstance(workflow, dict) and workflow.get("id") == run["workflow_id"]
             and workflow.get("path") == WORKFLOW_PATH, "unexpected_producer_workflow")
    expected_url = f"{_BASE}/actions/artifacts/{artifact_id}"
    _require(artifact.get("url") == expected_url and artifact.get("archive_download_url") == expected_url + "/zip",
             "untrusted_artifact_url")
    package = github.get(expected_url + "/zip", binary=True, limit=MAX_BYTES)
    digest = artifact.get("digest")
    _require(len(package) == size and isinstance(digest, str) and digest == "sha256:" + _sha(package),
             "artifact_sha256_mismatch")
    manifest, compressed, data, _ = _unpack(package, attested=True)
    _validate_attestation(manifest, run, artifact)
    _write_directory(output_dir, manifest, compressed, data)
    return manifest


def _remote(action, *, client=None, env=None, **kwargs):
    env = os.environ if env is None else env
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if client is not None:
        github = _GitHub(client, token)
        return action(github, **kwargs, **({"env": env} if action is _freeze else {}))
    with httpx.Client(timeout=60, verify=True, follow_redirects=False, trust_env=False) as owned:
        return _remote(action, client=owned, env=env, **kwargs)


def freeze(*, repository, release_id, asset_id, sha256, output_dir, client=None, env=None):
    return _remote(_freeze, client=client, env=env, repository=repository, release_id=release_id,
                   asset_id=asset_id, sha256=sha256, output_dir=output_dir)


def fetch(*, repository, artifact_id, output_dir, client=None, env=None):
    return _remote(_fetch, client=client, env=env, repository=repository, artifact_id=artifact_id, output_dir=output_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack_parser = commands.add_parser("pack")
    for option in ("ready", "pending", "corpus-sha256", "output"):
        pack_parser.add_argument("--" + option, required=True)
    freeze_parser = commands.add_parser("freeze")
    for option in ("repository", "release-id", "asset-id", "sha256", "output-dir"):
        freeze_parser.add_argument("--" + option, required=True)
    fetch_parser = commands.add_parser("fetch")
    for option in ("repository", "artifact-id", "output-dir"):
        fetch_parser.add_argument("--" + option, required=True)
    args = vars(parser.parse_args(argv))
    action = {"pack": pack, "freeze": freeze, "fetch": fetch}[args.pop("command")]
    try:
        result = action(**args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (PackageError, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, PackageError) else "local_io_failed"
        print(json.dumps({"error": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
