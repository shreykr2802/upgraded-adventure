"""
tests/test_passes.py
────────────────────
Tests for the five migration passes + the C# parser. LLM calls mocked.

Run:
    cd backend
    pytest tests/test_passes.py -v
"""

import json
import pytest
from unittest.mock import patch, MagicMock


# ── C# parser ─────────────────────────────────────────────────────────────────

def test_unwrap_type():
    from app.passes.csharp_parser import unwrap_type
    assert unwrap_type("List<AddressModel>") == ("AddressModel", True)
    assert unwrap_type("IEnumerable<UserModel>") == ("UserModel", True)
    assert unwrap_type("Dictionary<string,Order>") == ("Order", True)
    assert unwrap_type("MyApp.Models.UserModel") == ("UserModel", False)
    assert unwrap_type("Foo[]") == ("Foo", True)
    assert unwrap_type("int?") == ("int", False)


def test_parse_class():
    from app.passes.csharp_parser import parse_class
    src = """public class UserModel : BaseEntity {
        public int Id { get; set; }
        public string Name { get; set; }
        public AddressModel Address { get; set; }
        public List<OrderModel> Orders { get; set; }
    }"""
    ci = parse_class(src)
    assert ci.name == "UserModel"
    assert "BaseEntity" in ci.base_types
    names = {p.name for p in ci.properties}
    assert names == {"Id", "Name", "Address", "Orders"}
    deps = ci.dependency_types()
    assert "AddressModel" in deps and "OrderModel" in deps


def test_parse_controller():
    from app.passes.csharp_parser import parse_controller
    src = """public class UserController : Controller {
        public ActionResult Edit(int id) { return View("Edit"); }
        public ActionResult Render(string v) { return View(v); }
    }"""
    co = parse_controller(src)
    assert co.name == "UserController"
    edit = next(a for a in co.actions if a.name == "Edit")
    assert "Edit" in edit.return_views
    render = next(a for a in co.actions if a.name == "Render")
    assert render.has_dynamic_view


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "Views" / "User").mkdir(parents=True)
    (root / "Views" / "Shared").mkdir(parents=True)
    (root / "Controllers").mkdir(parents=True)
    (root / "Models").mkdir(parents=True)
    (root / "Models" / "AddressModel.cs").write_text(
        "public class AddressModel { public string City { get; set; } }")
    (root / "Models" / "UserModel.cs").write_text(
        "public class UserModel { public int Id { get; set; } public AddressModel Address { get; set; } }")
    (root / "Controllers" / "UserController.cs").write_text(
        'public class UserController : Controller { public ActionResult Edit() { return View("Edit"); } }')
    (root / "Views" / "Shared" / "_Layout.cshtml").write_text("<body>@RenderBody()</body>")
    (root / "Views" / "User" / "_EditForm.cshtml").write_text("@model UserModel\n@Html.TextBoxFor(m=>m.Name)")
    (root / "Views" / "User" / "Edit.cshtml").write_text(
        '@model UserModel\n@{ Layout = "~/Views/Shared/_Layout.cshtml"; }\n@Html.Partial("_EditForm")')
    return str(root)


@pytest.fixture
def page_map(repo):
    from app.analysis.engine import analyze_repo
    return analyze_repo(repo, engine="regex")


@pytest.fixture
def ctx(repo, page_map):
    from app.passes.base import PassContext
    return PassContext(dotnet_repo=repo, react_repo=repo, page_map=page_map,
                       output_root="/tmp/passtest_out")


def _resp(text):
    from app.gateway import ChatResponse
    raw = MagicMock()
    raw.choices = [MagicMock()]
    raw.choices[0].message.content = text
    raw.usage.prompt_tokens = 1
    raw.usage.completion_tokens = 1
    raw.usage.total_tokens = 2
    return ChatResponse(raw)


def _review_ok():
    return _resp(json.dumps({"valid": True, "issues": [], "confidence": "high"}))


# ── Pass 1: models ────────────────────────────────────────────────────────────

def test_models_discover(ctx):
    from app.passes.models_pass import ModelsPass
    items = ModelsPass().discover(ctx)
    symbols = {i.symbol for i in items}
    assert "UserModel" in symbols
    assert "AddressModel" in symbols


def test_models_dependencies(ctx):
    from app.passes.models_pass import ModelsPass
    p = ModelsPass()
    items = {i.symbol: i for i in p.discover(ctx)}
    deps = p.dependencies(items["UserModel"], ctx)
    # UserModel depends on AddressModel's origin
    assert any("AddressModel" in d for d in deps)


def test_models_migrate_one(ctx):
    from app.passes.models_pass import ModelsPass
    p = ModelsPass()
    item = next(i for i in p.discover(ctx) if i.symbol == "UserModel")
    gen = _resp(json.dumps({"filename": "UserModel.ts",
                            "code": "export interface UserModel {}",
                            "imports": [], "todos": []}))
    with patch("app.passes.models_pass.generate_component", return_value=gen), \
         patch("app.passes.models_pass.review_component", return_value=_review_ok()):
        r = p.migrate_one(item, ctx)
    assert r.error is None
    assert r.output_path == "types/UserModel.ts"
    assert "UserModel.ts" in r.files


# ── Pass 2: controllers ───────────────────────────────────────────────────────

def test_controllers_discover(ctx):
    from app.passes.controllers_pass import ControllersPass
    items = ControllersPass().discover(ctx)
    assert any(i.symbol == "UserController" for i in items)


def test_controllers_migrate_one(ctx):
    from app.passes.controllers_pass import ControllersPass
    p = ControllersPass()
    item = next(i for i in p.discover(ctx) if i.symbol == "UserController")
    gen = _resp(json.dumps({"filename": "useUser.ts",
                            "code": "export function useUser(){}",
                            "imports": [], "todos": ["wire Edit"]}))
    with patch("app.passes.controllers_pass.generate_component", return_value=gen), \
         patch("app.passes.controllers_pass.review_component", return_value=_review_ok()):
        r = p.migrate_one(item, ctx)
    assert r.error is None
    assert r.symbol == "useUser"
    assert r.output_path == "hooks/useUser.ts"


# ── Pass 3: layouts ───────────────────────────────────────────────────────────

def test_layouts_discover(ctx):
    from app.passes.layouts_pass import LayoutsPass
    items = LayoutsPass().discover(ctx)
    assert any("Layout" in i.origin for i in items)


def test_layouts_migrate_one(ctx):
    from app.passes.layouts_pass import LayoutsPass
    p = LayoutsPass()
    item = p.discover(ctx)[0]
    gen = _resp(json.dumps({"filename": "MainLayout.tsx",
                            "code": "export const MainLayout=()=>null",
                            "components_used": [], "todos": []}))
    with patch("app.passes.layouts_pass.generate_component", return_value=gen), \
         patch("app.passes.layouts_pass.review_component", return_value=_review_ok()), \
         patch("app.passes.artifact_store.ArtifactStore.retrieve", return_value=[]):
        r = p.migrate_one(item, ctx)
    assert r.error is None
    assert r.output_path.startswith("layouts/")


# ── Pass 4: components ────────────────────────────────────────────────────────

def test_components_discover(ctx):
    from app.passes.components_pass import ComponentsPass
    items = ComponentsPass().discover(ctx)
    assert any(i.symbol == "EditForm" for i in items)


def test_components_migrate_one(ctx):
    from app.passes.components_pass import ComponentsPass
    p = ComponentsPass()
    item = next(i for i in p.discover(ctx) if i.symbol == "EditForm")
    gen = _resp(json.dumps({"filename": "EditForm.tsx",
                            "code": "export const EditForm=()=>null",
                            "components_used": [], "imports": [], "todos": []}))
    with patch("app.passes.components_pass.generate_component", return_value=gen), \
         patch("app.passes.components_pass.review_component", return_value=_review_ok()), \
         patch("app.passes.artifact_store.ArtifactStore.retrieve", return_value=[]):
        r = p.migrate_one(item, ctx)
    assert r.error is None
    assert r.output_path.startswith("components/")


# ── Pass 5: pages ─────────────────────────────────────────────────────────────

def test_pages_discover(ctx):
    from app.passes.pages_pass import PagesPass
    items = PagesPass().discover(ctx)
    assert any(i.symbol == "UserEditPage" for i in items)


def test_pages_migrate_one(ctx):
    from app.passes.pages_pass import PagesPass
    p = PagesPass()
    item = next(i for i in p.discover(ctx) if i.symbol == "UserEditPage")
    gen = _resp(json.dumps({"structure": "single",
                            "files": {"UserEditPage.tsx": "export const UserEditPage=()=>null"},
                            "components_used": [], "imports": [], "todos": []}))
    with patch("app.passes.pages_pass.generate_component", return_value=gen), \
         patch("app.passes.pages_pass.review_component", return_value=_review_ok()), \
         patch("app.passes.artifact_store.ArtifactStore.by_layer", return_value=[]):
        r = p.migrate_one(item, ctx)
    assert r.error is None
    assert "UserEditPage.tsx" in r.files


def test_pages_merges_grapher_unresolved(ctx):
    from app.passes.pages_pass import PagesPass
    p = PagesPass()
    item = next(i for i in p.discover(ctx) if i.symbol == "UserEditPage")
    # inject a fake unresolved item into the page
    item.extra["page"]["unresolved"] = [
        {"kind": "partial", "reference": "EditorFor X", "reason": "runtime"}]
    gen = _resp(json.dumps({"structure": "single",
                            "files": {"UserEditPage.tsx": "x"}, "todos": []}))
    with patch("app.passes.pages_pass.generate_component", return_value=gen), \
         patch("app.passes.pages_pass.review_component", return_value=_review_ok()), \
         patch("app.passes.artifact_store.ArtifactStore.by_layer", return_value=[]):
        r = p.migrate_one(item, ctx)
    assert any("grapher" in t for t in r.todos)


# ── Registry ──────────────────────────────────────────────────────────────────

def test_registry_registers_all_in_order():
    from app.passes.registry import register_all_passes
    reg = register_all_passes()
    assert list(reg.keys()) == ["model", "controller", "layout", "component", "page"]
