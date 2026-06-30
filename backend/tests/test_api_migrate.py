"""
tests/test_api_migrate.py
─────────────────────────
Tests for the layered migration API (routes_migrate). The passes' LLM calls
are mocked at the gateway level so the orchestrator runs for real.

Run:
    cd backend
    pytest tests/test_api_migrate.py -v
"""

import os
import json
import shutil
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "Views" / "User").mkdir(parents=True)
    (root / "Views" / "Shared").mkdir(parents=True)
    (root / "Controllers").mkdir(parents=True)
    (root / "Models").mkdir(parents=True)
    (root / "Models" / "UserModel.cs").write_text(
        "public class UserModel { public int Id { get; set; } }")
    (root / "Controllers" / "UserController.cs").write_text(
        'public class UserController : Controller { public ActionResult Edit() { return View("Edit"); } }')
    (root / "Views" / "Shared" / "_Layout.cshtml").write_text("<body>@RenderBody()</body>")
    (root / "Views" / "User" / "Edit.cshtml").write_text(
        '@model UserModel\n@{ Layout = "~/Views/Shared/_Layout.cshtml"; }\n<p>Edit</p>')
    return str(root)


@pytest.fixture
def workdir(tmp_path, repo, monkeypatch):
    wd = str(tmp_path / "work")
    os.makedirs(os.path.join(wd, "migrated"), exist_ok=True)
    monkeypatch.setenv("API_WORKDIR", wd)
    import importlib
    from app.api import deps as deps_mod
    importlib.reload(deps_mod)
    from app.api import routes_migrate, routes_project, routes_analyze, routes_setup
    importlib.reload(routes_project)
    importlib.reload(routes_analyze)
    importlib.reload(routes_setup)
    importlib.reload(routes_migrate)
    # write a page map into the workdir
    from app.analysis.engine import analyze_repo
    pm = analyze_repo(repo, engine="regex")
    with open(os.path.join(wd, "page_map.json"), "w") as f:
        json.dump(pm, f)
    return wd


@pytest.fixture
def client(workdir, repo):
    from app.main import app
    c = TestClient(app)
    c.post("/api/project", json={"dotnet_repo": repo, "react_repo": repo})
    return c


def _fake_chat(model, messages, system, max_tokens=4000, temperature=0.1, **kw):
    from app.gateway import ChatResponse
    s = (system or "").lower()
    c = messages[0]["content"] if messages else ""
    raw = MagicMock()
    raw.choices = [MagicMock()]
    raw.usage.prompt_tokens = 1
    raw.usage.completion_tokens = 1
    raw.usage.total_tokens = 2
    if "typescript interface" in s:
        body = json.dumps({"filename": "UserModel.ts", "code": "export interface UserModel {}", "todos": []})
    elif "data hook" in s:
        body = json.dumps({"filename": "useUser.ts", "code": "export function useUser(){}", "todos": []})
    elif "layout component" in s:
        body = json.dumps({"filename": "MainLayout.tsx", "code": "export const MainLayout=()=>null", "todos": []})
    elif "shared react" in s:
        body = json.dumps({"filename": "C.tsx", "code": "export const C=()=>null", "todos": []})
    elif "final react" in s:
        body = json.dumps({"structure": "single", "files": {"UserEditPage.tsx": "export const UserEditPage=()=>null"}, "todos": []})
    else:
        body = json.dumps({"valid": True, "issues": [], "confidence": "high", "code": "export {}", "todos": []})
    raw.choices[0].message.content = body
    return ChatResponse(raw)


def _fake_embed(texts):
    from app.gateway import EmbedResponse
    n = len(texts) if isinstance(texts, list) else 1
    raw = MagicMock()
    raw.data = [MagicMock(embedding=[0.1] * 768) for _ in range(n)]
    return EmbedResponse(raw)


# ── status / preconditions ────────────────────────────────────────────────────

def test_status_starts_at_model(client):
    r = client.get("/api/migrate/status")
    assert r.status_code == 200
    assert r.json()["current_layer"] == "model"


def test_approve_before_run_409(client):
    r = client.post("/api/migrate/approve")
    assert r.status_code == 409


def test_artifacts_empty_initially(client):
    r = client.get("/api/migrate/artifacts")
    assert r.status_code == 200
    assert r.json()["count"] == 0


# ── run a layer (SSE) ─────────────────────────────────────────────────────────

def test_run_model_layer(client):
    with patch("app.gateway.chat", side_effect=_fake_chat), \
         patch("app.services.chat", side_effect=_fake_chat), \
         patch("app.services.embed_code", side_effect=_fake_embed), \
         patch("app.passes.artifact_store.ArtifactStore.retrieve", return_value=[]):
        r = client.post("/api/migrate/layer")
    assert r.status_code == 200
    events = [json.loads(l[5:]) for l in r.text.splitlines() if l.startswith("data:")]
    done = [e for e in events if e["type"] == "done"]
    assert done
    assert done[0]["payload"]["layer"] == "model"
    assert done[0]["payload"]["completed"] >= 1


def test_run_then_approve_advances(client):
    with patch("app.gateway.chat", side_effect=_fake_chat), \
         patch("app.services.chat", side_effect=_fake_chat), \
         patch("app.services.embed_code", side_effect=_fake_embed), \
         patch("app.passes.artifact_store.ArtifactStore.retrieve", return_value=[]):
        client.post("/api/migrate/layer")
    r = client.post("/api/migrate/approve")
    assert r.status_code == 200
    assert r.json()["next_layer"] == "controller"


def test_artifacts_listed_after_run(client):
    with patch("app.gateway.chat", side_effect=_fake_chat), \
         patch("app.services.chat", side_effect=_fake_chat), \
         patch("app.services.embed_code", side_effect=_fake_embed), \
         patch("app.passes.artifact_store.ArtifactStore.retrieve", return_value=[]):
        client.post("/api/migrate/layer")
    r = client.get("/api/migrate/artifacts?layer=model")
    assert r.status_code == 200
    assert r.json()["count"] >= 1
    art = r.json()["artifacts"][0]
    assert art["layer"] == "model"
    assert art["output_path"].startswith("types/")
