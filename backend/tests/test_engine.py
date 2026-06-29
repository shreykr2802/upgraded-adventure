"""
tests/test_engine.py
────────────────────
Tests for the analysis engine abstraction (engine.py).

The regex engine is exercised end-to-end. The roslyn engine is tested for its
unavailable-environment behaviour and sln discovery (we can't run the .NET
sidecar in CI without the SDK, so the actual roslyn run is not exercised here).

Run:
    cd backend
    pytest tests/test_engine.py -v
"""

import os
import json
import pytest


@pytest.fixture
def dotnet_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "Views" / "User").mkdir(parents=True)
    (root / "Controllers").mkdir(parents=True)
    (root / "Models").mkdir(parents=True)
    (root / "Views" / "User" / "Edit.cshtml").write_text(
        "@model M\n@Html.Partial(\"_Form\")\n"
    )
    (root / "Views" / "User" / "_Form.cshtml").write_text("@Html.TextBoxFor(m => m.Name)\n")
    (root / "Controllers" / "UserController.cs").write_text(
        "public class UserController { public ActionResult Edit() { return View(\"Edit\"); } }\n"
    )
    (root / "Models" / "M.cs").write_text("public class M { public string Name {get;set;} }\n")
    return str(root)


# ── Engine availability ───────────────────────────────────────────────────────

def test_regex_always_available():
    from app.analysis.engine import available_engines
    assert available_engines()["regex"] is True


# ── Regex engine via the abstraction ──────────────────────────────────────────

def test_analyze_regex_contract(dotnet_repo, tmp_path):
    from app.analysis.engine import analyze_repo
    out = str(tmp_path / "page_map.json")
    pm = analyze_repo(dotnet_repo, engine="regex", out_path=out)

    # contract: top-level keys
    assert "repo" in pm
    assert "pages" in pm
    assert "unresolved_count" in pm
    assert "unresolved_breakdown" in pm

    # contract: page fields
    page = pm["pages"][0]
    for field in ["page_name", "entry_view", "partials", "layout",
                  "controller", "controller_action", "model",
                  "nested_models", "unresolved"]:
        assert field in page

    # file was written
    assert os.path.exists(out)
    with open(out) as f:
        assert json.load(f)["pages"]


def test_normalise_fills_missing_fields():
    from app.analysis.engine import _normalise
    # a deliberately sparse page_map (as a sidecar might omit nulls)
    raw = {"repo": "/x", "pages": [{"page_name": "A/B", "entry_view": "Views/A/B.cshtml"}]}
    norm = _normalise(raw, "/x")
    page = norm["pages"][0]
    assert page["partials"] == []
    assert page["nested_models"] == []
    assert page["unresolved"] == []
    assert page["layout"] is None
    assert "unresolved_count" in norm
    assert norm["unresolved_breakdown"]["pages_total"] == 1


def test_normalise_recomputes_breakdown():
    from app.analysis.engine import _normalise
    raw = {
        "repo": "/x",
        "pages": [
            {"page_name": "A", "entry_view": "v", "unresolved": [
                {"kind": "view", "reference": "x", "source_file": "c", "reason": "dynamic"},
            ]},
            {"page_name": "B", "entry_view": "v", "unresolved": [
                {"kind": "view", "reference": "y", "source_file": "c", "reason": "dynamic"},
                {"kind": "model", "reference": "z", "source_file": "c", "reason": "not found"},
            ]},
        ],
    }
    norm = _normalise(raw, "/x")
    bd = norm["unresolved_breakdown"]
    assert bd["total"] == 3
    assert bd["by_kind"]["view"] == 2
    assert bd["by_kind"]["model"] == 1
    assert bd["by_reason"]["dynamic"] == 2
    assert bd["pages_with_unresolved"] == 2
    assert norm["unresolved_count"] == 3


# ── Roslyn engine behaviour without the SDK ───────────────────────────────────

def test_roslyn_unavailable_raises(dotnet_repo, monkeypatch):
    from app.analysis import engine
    monkeypatch.setattr(engine, "_dotnet_available", lambda: False)
    with pytest.raises(RuntimeError, match="needs the .NET SDK"):
        engine.analyze_repo(dotnet_repo, engine="roslyn")


def test_unknown_engine_raises(dotnet_repo):
    from app.analysis.engine import analyze_repo
    with pytest.raises(ValueError, match="Unknown engine"):
        analyze_repo(dotnet_repo, engine="banana")


# ── .sln discovery ────────────────────────────────────────────────────────────

def test_discover_sln_prefers_shallowest(tmp_path):
    from app.analysis.engine import _discover_sln
    root = tmp_path / "repo"
    (root / "deep" / "nested").mkdir(parents=True)
    (root / "App.sln").write_text("sln")
    (root / "deep" / "nested" / "Other.sln").write_text("sln")
    found = _discover_sln(str(root))
    assert found.endswith("App.sln")   # shallowest wins


def test_discover_sln_none_when_absent(tmp_path):
    from app.analysis.engine import _discover_sln
    (tmp_path / "empty").mkdir()
    assert _discover_sln(str(tmp_path / "empty")) is None
