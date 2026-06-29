"""
tests/test_api_setup.py
───────────────────────
Tests for the Stage 2 API: setup (SSE) + components/rules reads.

The setup stream does heavy work (component discovery via npx, LLM calls,
store indexing), so those are mocked at their source modules. We verify the
stage sequence, the read endpoints, and the error paths.

Run:
    cd backend
    pytest tests/test_api_setup.py -v
"""

import os
import json
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def repos(tmp_path):
    dotnet = tmp_path / "dotnet"
    (dotnet / "Views" / "User").mkdir(parents=True)
    (dotnet / "Views" / "User" / "Edit.cshtml").write_text(
        "@Html.TextBoxFor(m => m.Name)\n<table></table>\n"
    )
    react = tmp_path / "react" / "src" / "components"
    react.mkdir(parents=True)
    return str(dotnet), str(tmp_path / "react")


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    wd = str(tmp_path / "work")
    os.makedirs(wd, exist_ok=True)
    monkeypatch.setenv("API_WORKDIR", wd)
    import importlib
    from app.api import deps as deps_mod
    importlib.reload(deps_mod)
    from app.api import routes_project, routes_analyze, routes_setup
    importlib.reload(routes_project)
    importlib.reload(routes_analyze)
    importlib.reload(routes_setup)
    return wd


@pytest.fixture
def client(workdir):
    from app.main import app
    return TestClient(app)


def _configure(client, dotnet, react):
    return client.post("/api/project", json={"dotnet_repo": dotnet, "react_repo": react})


def _write_page_map(workdir, dotnet):
    page_map = {
        "pages": [{
            "entry_view": "Views/User/Edit.cshtml",
            "partials": [], "layout": None,
        }],
        "unresolved_count": 0,
    }
    with open(os.path.join(workdir, "page_map.json"), "w") as f:
        json.dump(page_map, f)


def _collect_sse(text: str) -> list[dict]:
    return [json.loads(l[5:]) for l in text.splitlines() if l.startswith("data:")]


# ── Setup preconditions ───────────────────────────────────────────────────────

def test_setup_requires_project(client):
    r = client.post("/api/setup")
    assert r.status_code == 409


def test_setup_requires_page_map(client, repos):
    dotnet, react = repos
    _configure(client, dotnet, react)
    # no page_map.json written
    r = client.post("/api/setup")
    assert r.status_code == 409


# ── Setup SSE happy path (everything mocked) ──────────────────────────────────

def test_setup_streams_all_stages(client, repos, workdir):
    dotnet, react = repos
    _configure(client, dotnet, react)
    _write_page_map(workdir, dotnet)

    from app.rag.indexer import ComponentDoc
    from app.analysis.rule_deriver import DerivedRule
    from app.analysis.razor_constructs import RazorConstruct

    fake_components = [ComponentDoc(
        name="TextInput", import_path="@x/ui/atoms", props="value: string",
        usage="<TextInput/>", description="text", tier="atom",
    )]
    fake_rule = DerivedRule(
        construct_family="html_helper", construct_kind="TextBoxFor",
        razor_example="@Html.TextBoxFor(m=>m.Name)", react_mapping="<TextInput/>",
        target_component="TextInput", notes="ok", confidence="high",
    )
    fake_construct = RazorConstruct(
        family="html_helper", kind="TextBoxFor",
        representative="@Html.TextBoxFor(m=>m.Name)", occurrences=4,
    )

    with patch("app.analysis.component_scanner.discover_components", return_value=fake_components), \
         patch("app.analysis.component_semantics.enrich_component_semantics", return_value=fake_components), \
         patch("app.analysis.component_semantics.scan_react_pages", return_value=3), \
         patch("app.rag.indexer.index_design_system"), \
         patch("app.rag.indexer.index_code_patterns"), \
         patch("app.analysis.razor_constructs.RazorConstructExtractor.scan_page_map"), \
         patch("app.analysis.razor_constructs.RazorConstructExtractor.unique_constructs", return_value=[fake_construct]), \
         patch("app.analysis.razor_constructs.RazorConstructExtractor.summary",
               return_value={"unique_constructs": 1, "total_occurrences": 4, "by_family": {"html_helper": 1}}), \
         patch("app.analysis.rule_deriver.RuleDeriver.derive_one", return_value=fake_rule):

        r = client.post("/api/setup")

    assert r.status_code == 200
    events = _collect_sse(r.text)
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    # Expected stage sequence
    assert "components" in stages
    assert "semantics" in stages
    assert "index_design" in stages
    assert "usage" in stages
    assert "constructs" in stages
    assert "rules" in stages
    assert "saving" in stages
    # Terminal done event with a summary
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["payload"]["rules"] == 1
    assert done[0]["payload"]["components"] == 1


def test_setup_skip_flags(client, repos, workdir):
    dotnet, react = repos
    _configure(client, dotnet, react)
    _write_page_map(workdir, dotnet)

    from app.rag.indexer import ComponentDoc
    from app.analysis.rule_deriver import DerivedRule
    from app.analysis.razor_constructs import RazorConstruct

    fake_components = [ComponentDoc(name="TextInput", import_path="@x", props="", usage="", description="", tier="atom")]
    fake_rule = DerivedRule("html_helper", "TextBoxFor", "x", "y", "TextInput", "", "high")
    fake_construct = RazorConstruct("html_helper", "TextBoxFor", "x", 1)

    with patch("app.analysis.component_scanner.discover_components", return_value=fake_components), \
         patch("app.analysis.component_semantics.enrich_component_semantics", return_value=fake_components) as mock_enrich, \
         patch("app.analysis.component_semantics.scan_react_pages", return_value=0) as mock_pages, \
         patch("app.rag.indexer.index_design_system"), \
         patch("app.rag.indexer.index_code_patterns"), \
         patch("app.analysis.razor_constructs.RazorConstructExtractor.scan_page_map"), \
         patch("app.analysis.razor_constructs.RazorConstructExtractor.unique_constructs", return_value=[fake_construct]), \
         patch("app.analysis.razor_constructs.RazorConstructExtractor.summary",
               return_value={"unique_constructs": 1, "total_occurrences": 1, "by_family": {}}), \
         patch("app.analysis.rule_deriver.RuleDeriver.derive_one", return_value=fake_rule):

        r = client.post("/api/setup?skip_semantics=true&skip_pages=true")

    events = _collect_sse(r.text)
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert "semantics" not in stages   # skipped
    assert "usage" not in stages       # skipped
    assert "rules" in stages           # still runs
    mock_enrich.assert_not_called()
    mock_pages.assert_not_called()


def test_setup_errors_on_no_components(client, repos, workdir):
    dotnet, react = repos
    _configure(client, dotnet, react)
    _write_page_map(workdir, dotnet)

    with patch("app.analysis.component_scanner.discover_components", return_value=[]):
        r = client.post("/api/setup")

    events = _collect_sse(r.text)
    assert any(e["type"] == "error" for e in events)


# ── Rules read endpoints ──────────────────────────────────────────────────────

def _write_rules(workdir):
    payload = {
        "rule_count": 2,
        "rules": [
            {"construct_family": "html_helper", "construct_kind": "TextBoxFor",
             "razor_example": "@Html.TextBoxFor(m=>m.Name)", "react_mapping": "<TextInput/>",
             "target_component": "TextInput", "notes": "direct", "confidence": "high"},
            {"construct_family": "html_tag", "construct_kind": "table",
             "razor_example": "<table>", "react_mapping": "<GridSystem/>",
             "target_component": "GridSystem", "notes": "grid", "confidence": "medium"},
        ],
        "summary": {"high_confidence": 1, "medium_confidence": 1, "low_confidence": 0, "needs_review": []},
    }
    with open(os.path.join(workdir, "migration_rules.json"), "w") as f:
        json.dump(payload, f)


def test_get_rules(client, repos, workdir):
    dotnet, react = repos
    _configure(client, dotnet, react)
    _write_rules(workdir)
    r = client.get("/api/rules")
    assert r.status_code == 200
    assert r.json()["rule_count"] == 2


def test_get_rules_409_before_setup(client, repos, workdir):
    dotnet, react = repos
    _configure(client, dotnet, react)
    r = client.get("/api/rules")
    assert r.status_code == 409


def test_get_rule_by_kind(client, repos, workdir):
    dotnet, react = repos
    _configure(client, dotnet, react)
    _write_rules(workdir)
    r = client.get("/api/rules/table")
    assert r.status_code == 200
    assert r.json()["target_component"] == "GridSystem"


def test_get_rule_404(client, repos, workdir):
    dotnet, react = repos
    _configure(client, dotnet, react)
    _write_rules(workdir)
    r = client.get("/api/rules/Nonexistent")
    assert r.status_code == 404


# ── Components read endpoint ───────────────────────────────────────────────────

def test_get_components(client, repos, workdir):
    dotnet, react = repos
    _configure(client, dotnet, react)

    from app.rag.indexer import ComponentDoc
    fake = [
        ComponentDoc(name="TextInput", import_path="@x/atoms", props="value: string",
                     usage="<TextInput/>", description="text input", tier="atom"),
        ComponentDoc(name="UserCard", import_path="@x/molecules", props="user: User",
                     usage="<UserCard/>", description="card", tier="molecule"),
    ]
    with patch("app.analysis.component_scanner.discover_components", return_value=fake):
        r = client.get("/api/components")

    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["by_tier"]["atom"] == 1
    assert body["by_tier"]["molecule"] == 1
