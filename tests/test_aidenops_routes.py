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


def _archive(directory, name="aidenops-d00222c-635405c.zip", bodies=None):
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


# --- listing ---------------------------------------------------------------


def test_the_incoming_folder_is_listed(incoming):
    _archive(incoming)
    body = client.get("/api/aidenops/archives").json()

    assert [a["name"] for a in body["archives"]] == ["aidenops-d00222c-635405c.zip"]
    assert body["incoming_dir"] == str(incoming)


def test_an_empty_folder_is_not_an_error(incoming):
    body = client.get("/api/aidenops/archives").json()
    assert body["archives"] == []


def test_only_zips_are_offered(incoming):
    _archive(incoming)
    (incoming / "notes.txt").write_text("x", encoding="utf-8")
    (incoming / "other.tar.gz").write_bytes(b"x")

    names = [a["name"] for a in client.get("/api/aidenops/archives").json()["archives"]]
    assert names == ["aidenops-d00222c-635405c.zip"]


# --- verification ----------------------------------------------------------


def test_a_matching_archive_verifies(incoming):
    path = _archive(incoming)
    response = client.post("/api/aidenops/verify",
                           json={"archive": path.name, "checksum": _sha256(path)})

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
                       json={"archive": path.name, "checksum": spaced}).status_code == 200


def test_a_mismatched_archive_is_refused_and_not_retained(incoming):
    """Not merely rejected here - absent afterwards, so no later endpoint can
    reach it."""
    path = _archive(incoming)
    response = client.post("/api/aidenops/verify",
                           json={"archive": path.name, "checksum": "b" * 64})

    assert response.status_code == 400
    assert "does not match the checksum" in response.json()["detail"]
    assert client.get("/api/aidenops/status").json()["release"] is None


def test_a_previously_verified_release_is_forgotten_on_a_failure(incoming):
    """A good release must not survive a subsequent bad one and get deployed."""
    path = _archive(incoming)
    client.post("/api/aidenops/verify", json={"archive": path.name, "checksum": _sha256(path)})
    assert client.get("/api/aidenops/status").json()["release"] is not None

    client.post("/api/aidenops/verify", json={"archive": path.name, "checksum": "c" * 64})
    assert client.get("/api/aidenops/status").json()["release"] is None


@pytest.mark.parametrize(
    "name",
    ["../../../etc/passwd", "..\\\\windows\\\\x.zip", "a.zip; id", "-rf.zip", "x.whl", ""],
)
def test_unsafe_archive_names_are_refused(incoming, name):
    response = client.post("/api/aidenops/verify", json={"archive": name, "checksum": "a" * 64})
    assert response.status_code == 400


def test_an_archive_outside_the_folder_is_refused(incoming, tmp_path):
    """Resolving and re-checking the parent is what closes this, not the name
    pattern alone."""
    outside = _archive(tmp_path, name="elsewhere.zip")
    response = client.post("/api/aidenops/verify",
                           json={"archive": outside.name, "checksum": _sha256(outside)})

    assert response.status_code == 400
    assert "not in" in response.json()["detail"]


def test_a_missing_sums_file_is_refused(incoming):
    path = incoming / "aidenops-broken.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(UI, b"tarball")

    response = client.post("/api/aidenops/verify",
                           json={"archive": path.name, "checksum": _sha256(path)})
    assert response.status_code == 400
    assert "cannot be verified" in response.json()["detail"]


# --- deploying -------------------------------------------------------------


def test_deploying_without_a_verified_release_is_refused(incoming):
    response = client.post("/api/aidenops/deploy", json={"target": "ui"})
    assert response.status_code == 409
    assert "Verify an archive first" in response.json()["detail"]


def test_the_backend_target_says_it_is_not_built_yet(incoming):
    """An honest 501 rather than a stub that appears to work."""
    path = _archive(incoming)
    client.post("/api/aidenops/verify", json={"archive": path.name, "checksum": _sha256(path)})

    response = client.post("/api/aidenops/deploy", json={"target": "backend"})
    assert response.status_code == 501
    assert "not implemented yet" in response.json()["detail"]


def test_a_release_without_a_ui_cannot_deploy_one(incoming):
    path = _archive(incoming, bodies={WHEEL: BODIES[WHEEL], REQS: BODIES[REQS]})
    client.post("/api/aidenops/verify", json={"archive": path.name, "checksum": _sha256(path)})

    response = client.post("/api/aidenops/deploy", json={"target": "ui"})
    assert response.status_code == 400
    assert "no UI bundle" in response.json()["detail"]


def test_an_unknown_target_is_refused(incoming):
    path = _archive(incoming)
    client.post("/api/aidenops/verify", json={"archive": path.name, "checksum": _sha256(path)})

    assert client.post("/api/aidenops/deploy",
                       json={"target": "database"}).status_code == 400


def test_clearing_forgets_the_release(incoming):
    path = _archive(incoming)
    client.post("/api/aidenops/verify", json={"archive": path.name, "checksum": _sha256(path)})

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
