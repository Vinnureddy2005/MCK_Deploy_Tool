"""The AidenOps API and its separation from the TX-PROJECTS one.

The two flows share a server and a log socket and nothing else. A release that
fails verification must not be reachable by the deploy endpoint at all - not
rejected there, but absent, so there is no path from a bad archive to a command.
"""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.services import aidenops_release

client = TestClient(app, raise_server_exceptions=False)

WHEEL = "aidenops_service-1.1.0+gd00222c-py3-none-any.whl"
REQS = "requirements-1.1.0+gd00222c.txt"
UI = "aidenops-ui-1.0.0+g635405c.tar.gz"

BODIES = {WHEEL: b"wheel", REQS: b"fastapi==0.115.0\n", UI: b"tarball"}


def _archive(directory, name="opsBinaries.zip", bodies=None):
    bodies = BODIES if bodies is None else bodies
    path = directory / name
    sums = "".join(
        f"{hashlib.sha256(body).hexdigest()}  {member}\n" for member, body in bodies.items()
    )
    with zipfile.ZipFile(path, "w") as zf:
        for member, body in bodies.items():
            zf.writestr(member, body)
        zf.writestr("SHA256SUMS.txt", sums)
        zf.writestr("MANIFEST.json", json.dumps({
            "archive": name, "built_by": "vineesh",
            "builds": [{"commit_short": "d00222c"}, {"commit_short": "635405c"}],
        }))
    return path


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def incoming(tmp_path, monkeypatch):
    """Point the incoming folder at a temp dir, and never leave state behind."""
    folder = tmp_path / "incoming"
    folder.mkdir()
    monkeypatch.setenv("AIDENOPS_INCOMING_DIR", str(folder))
    config.reload_settings()
    aidenops_release.clear()
    yield folder
    aidenops_release.clear()
    config.reload_settings()


# --- the bundle is reported, not chosen -----------------------------------


def test_the_bundle_is_reported_when_present(incoming):
    _archive(incoming)
    body = client.get("/api/aidenops/bundle").json()

    assert body["present"] is True
    assert body["name"] == "opsBinaries.zip"
    assert body["size"] > 0
    assert body["incoming_dir"] == str(incoming)


def test_a_missing_bundle_is_reported_not_an_error(incoming):
    """Nothing copied yet is a normal state to show, not a failure."""
    body = client.get("/api/aidenops/bundle").json()

    assert body["present"] is False
    assert body["name"] == "opsBinaries.zip"


def test_other_zips_in_the_folder_are_ignored(incoming):
    """There is one name. A leftover from some other purpose is not offered,
    because nothing is being offered."""
    _archive(incoming, name="something-else.zip")
    assert client.get("/api/aidenops/bundle").json()["present"] is False


def test_the_filename_is_not_a_request_parameter(incoming):
    """Accepting a name would only add a way to point this at the wrong file."""
    from app.routes.aidenops import VerifyRequest

    assert set(VerifyRequest.model_fields) == {"checksum"}


def test_verifying_without_the_bundle_says_where_to_put_it(incoming):
    response = client.post("/api/aidenops/verify", json={"checksum": "a" * 64})

    assert response.status_code == 400
    assert "Copy the release bundle there first" in response.json()["detail"]


# --- verification ----------------------------------------------------------


def test_a_matching_archive_verifies(incoming):
    path = _archive(incoming)
    response = client.post("/api/aidenops/verify",
                           json={"checksum": _sha256(path)})

    assert response.status_code == 200
    release = response.json()["release"]
    assert release["commits"] == ["d00222c", "635405c"]
    assert release["built_by"] == "vineesh"
    assert release["contents"]["has_backend"] and release["contents"]["has_ui"]
    assert len(release["members"]) == 3


def test_a_hash_pasted_in_blocks_is_accepted(incoming):
    """The Aiden tool shows it in groups of eight, so that is how it arrives."""
    path = _archive(incoming)
    digest = _sha256(path)
    spaced = " ".join(digest[i:i + 8] for i in range(0, 64, 8))

    assert client.post("/api/aidenops/verify",
                       json={"checksum": spaced}).status_code == 200


def test_a_mismatched_archive_is_refused_and_not_retained(incoming):
    """Not merely rejected here - absent afterwards, so no later endpoint can
    reach it."""
    path = _archive(incoming)
    response = client.post("/api/aidenops/verify",
                           json={"checksum": "b" * 64})

    assert response.status_code == 400
    assert "does not match the checksum" in response.json()["detail"]
    assert client.get("/api/aidenops/status").json()["release"] is None


def test_a_previously_verified_release_is_forgotten_on_a_failure(incoming):
    """A good release must not survive a subsequent bad one and get deployed."""
    path = _archive(incoming)
    client.post("/api/aidenops/verify", json={"checksum": _sha256(path)})
    assert client.get("/api/aidenops/status").json()["release"] is not None

    client.post("/api/aidenops/verify", json={"checksum": "c" * 64})
    assert client.get("/api/aidenops/status").json()["release"] is None


def test_a_missing_sums_file_is_refused(incoming):
    # Must carry the expected name, since that is the only file verify looks at.
    path = incoming / "opsBinaries.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(UI, b"tarball")

    response = client.post("/api/aidenops/verify",
                           json={"checksum": _sha256(path)})
    assert response.status_code == 400
    assert "cannot be verified" in response.json()["detail"]


# --- deploying -------------------------------------------------------------


def test_deploying_without_a_verified_release_is_refused(incoming):
    response = client.post("/api/aidenops/deploy", json={"target": "ui"})
    assert response.status_code == 409
    assert "Verify an archive first" in response.json()["detail"]


def test_the_backend_target_is_accepted(incoming):
    """It is implemented now, so it must not be refused as unavailable. Whatever
    happens next is a server outcome, not a missing feature."""
    path = _archive(incoming)
    client.post("/api/aidenops/verify", json={"checksum": _sha256(path)})

    response = client.post("/api/aidenops/deploy", json={"target": "backend"})
    assert response.status_code != 501


def test_a_release_without_a_wheel_cannot_deploy_the_backend(incoming):
    path = _archive(incoming, bodies={UI: BODIES[UI]})
    client.post("/api/aidenops/verify", json={"checksum": _sha256(path)})

    response = client.post("/api/aidenops/deploy", json={"target": "backend"})
    assert response.status_code == 400
    assert "no backend wheel" in response.json()["detail"]


def test_a_release_without_a_ui_cannot_deploy_one(incoming):
    path = _archive(incoming, bodies={WHEEL: BODIES[WHEEL], REQS: BODIES[REQS]})
    client.post("/api/aidenops/verify", json={"checksum": _sha256(path)})

    response = client.post("/api/aidenops/deploy", json={"target": "ui"})
    assert response.status_code == 400
    assert "no UI bundle" in response.json()["detail"]


def test_an_unknown_target_is_refused(incoming):
    path = _archive(incoming)
    client.post("/api/aidenops/verify", json={"checksum": _sha256(path)})

    assert client.post("/api/aidenops/deploy",
                       json={"target": "database"}).status_code == 400


def test_clearing_forgets_the_release(incoming):
    path = _archive(incoming)
    client.post("/api/aidenops/verify", json={"checksum": _sha256(path)})

    client.post("/api/aidenops/clear")
    assert client.get("/api/aidenops/status").json()["release"] is None


# --- the two flows stay apart ---------------------------------------------


def test_both_pages_are_served_and_cross_link():
    assert "/aidenops" in client.get("/").text
    aidenops = client.get("/aidenops")
    assert aidenops.status_code == 200
    assert 'href="/"' in aidenops.text
    assert "TX-PROJECTS" in aidenops.text


def test_the_routers_share_no_prefix():
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/aidenops/status" in paths
    assert "/api/services" in paths or any(p.startswith("/api/") and "aidenops" not in p
                                           for p in paths)


def test_no_hash_appears_in_the_page_before_anything_is_verified():
    """A code on screen before verification would be a value for a release
    nobody has checked."""
    import re

    assert not re.search(r"[0-9a-f]{32,}", client.get("/aidenops").text)


# --- fetching from the hub -------------------------------------------------


def test_the_hub_request_uses_the_bundle_name(incoming, monkeypatch):
    """Same request the JAR path makes; only the filename differs."""
    monkeypatch.setenv("INSTALLATION_HUB_URL", "http://hub.invalid/api/path")
    monkeypatch.setenv("INSTALLATION_CODE", "test-code")
    config.reload_settings()

    from app.services.download_service import DownloadService

    url = DownloadService(config.settings).build_url(config.settings.aidenops_bundle_name)
    assert "filename=opsBinaries.zip" in url
    assert "code=test-code" in url


def test_the_code_is_never_in_a_redacted_url(incoming, monkeypatch):
    """It is a credential, and URLs get logged."""
    monkeypatch.setenv("INSTALLATION_HUB_URL", "http://hub.invalid/api/path")
    monkeypatch.setenv("INSTALLATION_CODE", "super-secret-code")
    config.reload_settings()

    from app.services.download_service import DownloadService

    d = DownloadService(config.settings)
    assert "super-secret-code" not in DownloadService.redact(
        d.build_url(config.settings.aidenops_bundle_name)
    )


def test_fetching_without_a_hub_configured_is_a_client_error(incoming, monkeypatch):
    monkeypatch.setenv("INSTALLATION_HUB_URL", "")
    config.reload_settings()

    response = client.post("/api/aidenops/fetch")
    assert response.status_code == 400
    assert "INSTALLATION_HUB_URL" in response.json()["detail"]


def test_a_dry_run_does_not_write_the_bundle(incoming, monkeypatch):
    """A rehearsal must not leave a zero-byte file where the verifier looks."""
    monkeypatch.setenv("INSTALLATION_HUB_URL", "http://hub.invalid/api/path")
    monkeypatch.setenv("INSTALLATION_CODE", "test-code")
    monkeypatch.setenv("DRY_RUN", "true")
    config.reload_settings()

    body = client.post("/api/aidenops/fetch").json()
    assert body["simulated"] is True
    assert not (incoming / "opsBinaries.zip").exists()


def test_an_error_page_is_not_accepted_as_an_archive(tmp_path):
    """The hub answers with 200 and HTML when something is wrong, which would
    otherwise be handed to the verifier as a zip."""
    from app.services.download_service import DownloadService

    page = tmp_path / "not-a-zip"
    page.write_bytes(b"<html><body>Unauthorized</body></html>")
    assert DownloadService._looks_like_jar(page) is False

    real = tmp_path / "looks-like-one"
    real.write_bytes(b"PK\x03\x04rest")
    assert DownloadService._looks_like_jar(real) is True
