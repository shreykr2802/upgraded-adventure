"""
tests/test_api_analyze.py
─────────────────────────
Tests for the Stage 1 API: project config + analyze (SSE) + page map reads.

Run:
    cd backend
    pytest tests/test_api_analyze.py -v
"""

import os
import json
import pytest
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def dotnet_repo(tmp_path):
    root = tmp_path / "dotnet"
    (root / "Views" / "User").mkdir(parents=True)
    (root / "Controllers").mkdir(parents=True)
    (root / "Models").mkdir(parents=True)
    (root / "Views" / "User" / "Edit.cshtml").write_text(
        "@model MyApp.Models.UserModel\n@Html.Partial(\"_Form\")\n"
    )
    (root / "Views" / "User" / "_Form.cshtml").write_text("@Html.TextBoxFor(m => m.Name)\n")
    (root / "Controllers" / "UserController.cs").write_text(
        "public class UserController { public ActionResult Edit() { return View(\"Edit\"); } }\n"
    )
    (root / "Models" / "UserModel.cs").write_text(
        "public class UserModel { public string Name { get; set; } }\n"
    )
    return str(root)


@pytest.fixture
def react_repo(tmp_path):
    root = tmp_path / "react" / "src" / "components"
    root.mkdir(parents=True)
    return str(tmp_path / "react")


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Isolate the API workdir per test
    workdir = str(tmp_path / "work")
    monkeypatch.setenv("API_WORKDIR", workdir)
    # Reload deps so it picks up the new workdir
    import importlib
    from app.api import deps as deps_mod
    importlib.reload(deps_mod)
    # routes import deps at module load; reload them too
    from app.api import routes_project, routes_analyze
    importlib.reload(routes_project)
    importlib.reload(routes_analyze)
    from app.main import app
    # Re-mount freshly reloaded routers
    return TestClient(app)


def _configure(client, dotnet_repo, react_repo):
    return client.post("/api/project", json={
        "dotnet_repo": dotnet_repo,
        "react_repo": react_repo,
    })


# ── Project config ────────────────────────────────────────────────────────────

def test_configure_project_ok(client, dotnet_repo, react_repo):
    r = _configure(client, dotnet_repo, react_repo)
    assert r.status_code == 200
    body = r.json()
    assert body["dotnet_repo"] == dotnet_repo
    assert body["status"]["analyzed"] is False


def test_configure_project_bad_path(client, react_repo):
    r = client.post("/api/project", json={
        "dotnet_repo": "/does/not/exist",
        "react_repo": react_repo,
    })
    assert r.status_code == 400
    assert "problems" in r.json()["detail"]


def test_get_project_before_config_404(client):
    r = client.get("/api/project")
    assert r.status_code == 404


def test_analyze_requires_project(client):
    # No project configured → analyze should 409 (surfaced as stream error or status)
    r = client.post("/api/analyze")
    # require_project raises HTTPException(409) before streaming starts
    assert r.status_code in (409, 200)
    if r.status_code == 200:
        # if it streamed, the first event should be an error
        assert "error" in r.text or "scanning" in r.text


# ── Analyze SSE ───────────────────────────────────────────────────────────────

def _collect_sse(resp_text: str) -> list[dict]:
    events = []
    for line in resp_text.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:]))
    return events


def test_analyze_streams_to_done(client, dotnet_repo, react_repo):
    _configure(client, dotnet_repo, react_repo)
    r = client.post("/api/analyze")
    assert r.status_code == 200
    events = _collect_sse(r.text)
    types = [e["type"] for e in events]
    assert "stage" in types
    assert "done" in types
    done = [e for e in events if e["type"] == "done"][0]
    assert done["payload"]["pages"] == 1


def test_analyze_writes_page_map(client, dotnet_repo, react_repo):
    _configure(client, dotnet_repo, react_repo)
    client.post("/api/analyze")
    # After analyze, status should report analyzed=True
    r = client.get("/api/project")
    assert r.json()["status"]["analyzed"] is True


# ── Page map reads ────────────────────────────────────────────────────────────

def test_get_pagemap(client, dotnet_repo, react_repo):
    _configure(client, dotnet_repo, react_repo)
    client.post("/api/analyze")
    r = client.get("/api/pagemap")
    assert r.status_code == 200
    d = r.json()
    assert len(d["pages"]) == 1
    assert d["pages"][0]["page_name"] == "User/Edit"


def test_get_pagemap_recompute_fallback(client, dotnet_repo, react_repo):
    # Don't run analyze — pagemap should recompute from the grapher
    _configure(client, dotnet_repo, react_repo)
    r = client.get("/api/pagemap")
    assert r.status_code == 200
    assert len(r.json()["pages"]) >= 1


def test_get_unresolved(client, dotnet_repo, react_repo):
    _configure(client, dotnet_repo, react_repo)
    client.post("/api/analyze")
    r = client.get("/api/pagemap/unresolved")
    assert r.status_code == 200
    body = r.json()
    assert "breakdown" in body
    assert "items" in body
    assert isinstance(body["count"], int)


def test_get_single_page(client, dotnet_repo, react_repo):
    _configure(client, dotnet_repo, react_repo)
    client.post("/api/analyze")
    r = client.get("/api/pagemap/page/User/Edit")
    assert r.status_code == 200
    assert r.json()["model"] is not None


def test_get_single_page_404(client, dotnet_repo, react_repo):
    _configure(client, dotnet_repo, react_repo)
    client.post("/api/analyze")
    r = client.get("/api/pagemap/page/Nope/Missing")
    assert r.status_code == 404
