"""
tests/test_grapher.py
─────────────────────
Tests for the .NET page grapher. Builds a synthetic .NET repo in a tmp dir
(via pytest's tmp_path) and verifies cluster resolution across folders.

Run:
    cd backend
    pytest tests/test_grapher.py -v
"""

import os
import pytest


# ── Synthetic repo fixture ────────────────────────────────────────────────────

@pytest.fixture
def dotnet_repo(tmp_path):
    """Build a realistic mixed .NET repo with partials, layout, nested models."""
    root = tmp_path / "repo"
    (root / "Views" / "User").mkdir(parents=True)
    (root / "Views" / "Shared").mkdir(parents=True)
    (root / "Controllers").mkdir(parents=True)
    (root / "Models").mkdir(parents=True)

    (root / "Views" / "User" / "Edit.cshtml").write_text(
        '@model MyApp.Models.UserEditModel\n'
        '@{ Layout = "~/Views/Shared/_Layout.cshtml"; }\n'
        '<h2>Edit</h2>\n'
        '@Html.Partial("_EditForm")\n'
        '@Html.EditorFor(m => m.Address)\n'
    )
    (root / "Views" / "User" / "_EditForm.cshtml").write_text(
        '@model MyApp.Models.UserEditModel\n'
        '@Html.TextBoxFor(m => m.FirstName)\n'
        '@await Html.PartialAsync("_ContactBlock")\n'
    )
    (root / "Views" / "Shared" / "_ContactBlock.cshtml").write_text(
        '@model MyApp.Models.UserEditModel\n'
        '@Html.TextBoxFor(m => m.Email)\n'
    )
    (root / "Views" / "Shared" / "_Layout.cshtml").write_text(
        '<!DOCTYPE html><html><body>@RenderBody()</body></html>\n'
    )
    (root / "Controllers" / "UserController.cs").write_text(
        'namespace MyApp.Controllers {\n'
        '  public class UserController : Controller {\n'
        '    [HttpGet]\n'
        '    public ActionResult Edit(int id) {\n'
        '      var model = _svc.GetUser(id);\n'
        '      return View("Edit", model);\n'
        '    }\n'
        '  }\n'
        '}\n'
    )
    (root / "Models" / "UserEditModel.cs").write_text(
        'namespace MyApp.Models {\n'
        '  public class UserEditModel {\n'
        '    public int Id { get; set; }\n'
        '    public string FirstName { get; set; }\n'
        '    public string Email { get; set; }\n'
        '    public AddressModel Address { get; set; }\n'
        '  }\n'
        '}\n'
    )
    (root / "Models" / "AddressModel.cs").write_text(
        'namespace MyApp.Models {\n'
        '  public class AddressModel {\n'
        '    public string Street { get; set; }\n'
        '    public string City { get; set; }\n'
        '  }\n'
        '}\n'
    )
    return str(root)


# ── Scan ──────────────────────────────────────────────────────────────────────

def test_scan_indexes_files(dotnet_repo):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(dotnet_repo)
    g.scan()
    assert len(g._cshtml) == 4   # Edit, _EditForm, _ContactBlock, _Layout
    assert len(g._cs) == 3       # controller + 2 models


# ── Partial resolution (recursive) ────────────────────────────────────────────

def test_resolves_nested_partials(dotnet_repo):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(dotnet_repo)
    cluster = g.resolve_page("Views/User/Edit.cshtml")
    # _EditForm is direct, _ContactBlock is nested inside _EditForm
    partial_names = [os.path.basename(p) for p in cluster.partials]
    assert "_EditForm.cshtml" in partial_names
    assert "_ContactBlock.cshtml" in partial_names


# ── Layout resolution ─────────────────────────────────────────────────────────

def test_resolves_layout(dotnet_repo):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(dotnet_repo)
    cluster = g.resolve_page("Views/User/Edit.cshtml")
    assert cluster.layout is not None
    assert "_Layout.cshtml" in cluster.layout


# ── Model + nested model resolution ───────────────────────────────────────────

def test_resolves_model_and_nested(dotnet_repo):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(dotnet_repo)
    cluster = g.resolve_page("Views/User/Edit.cshtml")
    assert cluster.model is not None
    assert "UserEditModel.cs" in cluster.model
    nested_names = [os.path.basename(n) for n in cluster.nested_models]
    assert "AddressModel.cs" in nested_names


# ── Controller + action resolution (explicit return View) ─────────────────────

def test_resolves_controller_and_action(dotnet_repo):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(dotnet_repo)
    cluster = g.resolve_page("Views/User/Edit.cshtml")
    assert cluster.controller is not None
    assert "UserController.cs" in cluster.controller
    assert cluster.controller_action == "Edit"


# ── EditorFor flagged as unresolved ───────────────────────────────────────────

def test_editorfor_flagged(dotnet_repo):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(dotnet_repo)
    cluster = g.resolve_page("Views/User/Edit.cshtml")
    editor_flags = [u for u in cluster.unresolved if "EditorFor" in u.reference]
    assert len(editor_flags) == 1


# ── Missing partial flagged ───────────────────────────────────────────────────

def test_missing_partial_flagged(dotnet_repo, tmp_path):
    from app.analysis.dotnet_grapher import DotNetGrapher
    # Add a view referencing a non-existent partial
    bad_view = tmp_path / "repo" / "Views" / "User" / "Broken.cshtml"
    bad_view.write_text('@Html.Partial("_DoesNotExist")\n')
    g = DotNetGrapher(dotnet_repo)
    cluster = g.resolve_page("Views/User/Broken.cshtml")
    missing = [u for u in cluster.unresolved if u.reference == "_DoesNotExist"]
    assert len(missing) == 1
    assert missing[0].kind == "partial"


# ── map_repo excludes partials as entry points ────────────────────────────────

def test_map_repo_excludes_partials(dotnet_repo):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(dotnet_repo)
    clusters = g.map_repo()
    # Only Edit.cshtml is a page; the 3 underscore-prefixed files are excluded
    assert len(clusters) == 1
    assert clusters[0].page_name == "User/Edit"


# ── all_files aggregation ─────────────────────────────────────────────────────

def test_all_files_includes_everything(dotnet_repo):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(dotnet_repo)
    cluster = g.resolve_page("Views/User/Edit.cshtml")
    files = cluster.all_files()
    # entry + 2 partials + layout + controller + model + nested model = 7
    assert len(files) == 7


# ── Unresolved collection across repo ─────────────────────────────────────────

def test_collect_unresolved(dotnet_repo):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(dotnet_repo)
    clusters = g.map_repo()
    unresolved = g.collect_unresolved(clusters)
    # At least the EditorFor flag should be present
    assert any("EditorFor" in u.reference for u in unresolved)


# ── Hardened resolution (added in refinement pass) ────────────────────────────

@pytest.fixture
def repo_viewstart(tmp_path):
    """Repo where layout comes from _ViewStart, with generic model + EditorFor."""
    root = tmp_path / "repo"
    (root / "Views" / "User").mkdir(parents=True)
    (root / "Views" / "Shared" / "EditorTemplates").mkdir(parents=True)
    (root / "Models").mkdir(parents=True)
    (root / "Controllers").mkdir(parents=True)

    (root / "Views" / "_ViewStart.cshtml").write_text(
        '@{ Layout = "~/Views/Shared/_main.cshtml"; }\n'
    )
    (root / "Views" / "Shared" / "_main.cshtml").write_text("<body>@RenderBody()</body>\n")
    (root / "Views" / "User" / "List.cshtml").write_text(
        "@model List<MyApp.Models.UserModel>\n"
        "@Html.EditorFor(m => m.FirstUser)\n"
        "@foreach (var u in Model) { <div>@u.Name</div> }\n"
    )
    (root / "Views" / "Shared" / "EditorTemplates" / "FirstUser.cshtml").write_text(
        "@model MyApp.Models.UserModel\n@Html.TextBoxFor(m => m.Name)\n"
    )
    (root / "Models" / "UserModel.cs").write_text(
        "namespace MyApp.Models { public class UserModel : BaseEntity {\n"
        "  public string Name { get; set; }\n"
        "  public AddressModel Address { get; set; }\n} }\n"
    )
    (root / "Models" / "AddressModel.cs").write_text(
        "namespace MyApp.Models { public class AddressModel { public string City { get; set; } } }\n"
    )
    (root / "Controllers" / "UserController.cs").write_text(
        "public class UserController { public ActionResult List() { return View(\"List\"); } }\n"
    )
    return str(root)


def test_layout_resolved_from_viewstart(repo_viewstart):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(repo_viewstart)
    c = g.resolve_page("Views/User/List.cshtml")
    assert c.layout is not None
    assert "_main.cshtml" in c.layout


def test_generic_model_unwrapped(repo_viewstart):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(repo_viewstart)
    c = g.resolve_page("Views/User/List.cshtml")
    # List<UserModel> should resolve to UserModel.cs, not fail on "List"
    assert c.model is not None
    assert "UserModel.cs" in c.model


def test_nested_model_through_inheritance(repo_viewstart):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(repo_viewstart)
    c = g.resolve_page("Views/User/List.cshtml")
    nested = [__import__("os").path.basename(n) for n in c.nested_models]
    assert "AddressModel.cs" in nested


def test_editorfor_template_resolved_by_convention(repo_viewstart):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(repo_viewstart)
    c = g.resolve_page("Views/User/List.cshtml")
    partial_names = [__import__("os").path.basename(p) for p in c.partials]
    assert "FirstUser.cshtml" in partial_names
    # and it should NOT be flagged as unresolved anymore
    editor_unresolved = [u for u in c.unresolved if "FirstUser" in u.reference]
    assert len(editor_unresolved) == 0


def test_extract_core_type():
    from app.analysis.dotnet_grapher import _extract_core_type
    assert _extract_core_type("MyApp.Models.UserModel") == "UserModel"
    assert _extract_core_type("List<MyApp.Models.UserModel>") == "UserModel"
    assert _extract_core_type("IEnumerable<UserModel>") == "UserModel"
    assert _extract_core_type("Dictionary<string, UserModel>") == "UserModel"
    assert _extract_core_type("UserModel") == "UserModel"


def test_unresolved_breakdown(repo_viewstart):
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(repo_viewstart)
    clusters = g.map_repo()
    bd = g.unresolved_breakdown(clusters)
    assert "total" in bd
    assert "by_kind" in bd
    assert "by_reason" in bd
    assert bd["pages_total"] >= 1
