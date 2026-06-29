"""
tests/test_razor_constructs.py
──────────────────────────────
Tests for the Razor construct extractor — dedup, overlap precedence,
and page_map scanning.

Run:
    cd backend
    pytest tests/test_razor_constructs.py -v
"""

import pytest


# ── Dedup ─────────────────────────────────────────────────────────────────────

def test_dedupes_repeated_construct():
    from app.analysis.razor_constructs import RazorConstructExtractor
    ex = RazorConstructExtractor()
    text = """
    @Html.TextBoxFor(m => m.A)
    @Html.TextBoxFor(m => m.B)
    @Html.TextBoxFor(m => m.C)
    """
    ex.scan_text(text, "Test.cshtml")
    constructs = ex.unique_constructs()
    textbox = [c for c in constructs if c.kind == "TextBoxFor"]
    assert len(textbox) == 1            # 3 occurrences → 1 construct
    assert textbox[0].occurrences == 3


# ── Overlap precedence: auth @if wins over generic @if ────────────────────────

def test_auth_if_not_double_counted_as_generic_if():
    from app.analysis.razor_constructs import RazorConstructExtractor
    ex = RazorConstructExtractor()
    ex.scan_text("@if (User.IsInRole(\"Admin\")) { }", "Test.cshtml")
    constructs = {c.kind for c in ex.unique_constructs()}
    assert "IsInRole" in constructs
    assert "if" not in constructs       # the generic @if must NOT also fire


def test_generic_if_still_detected_separately():
    from app.analysis.razor_constructs import RazorConstructExtractor
    ex = RazorConstructExtractor()
    ex.scan_text("@if (Model.IsActive) { }", "Test.cshtml")
    constructs = {c.kind for c in ex.unique_constructs()}
    assert "if" in constructs


# ── Multiple families ─────────────────────────────────────────────────────────

def test_detects_multiple_families():
    from app.analysis.razor_constructs import RazorConstructExtractor
    ex = RazorConstructExtractor()
    text = """
    @using (Html.BeginForm("Save", "User")) {
        @Html.AntiForgeryToken()
        @Html.TextBoxFor(m => m.Name)
        @Html.DropDownListFor(m => m.Country, Model.Countries)
        @Html.ValidationSummary(true)
        @foreach (var x in Model.Items) { <li>@x.Name</li> }
    }
    """
    ex.scan_text(text, "Test.cshtml")
    kinds = {c.kind for c in ex.unique_constructs()}
    assert "BeginForm" in kinds
    assert "AntiForgeryToken" in kinds
    assert "TextBoxFor" in kinds
    assert "DropDownListFor" in kinds
    assert "ValidationSummary" in kinds
    assert "foreach" in kinds


# ── Occurrence ordering ───────────────────────────────────────────────────────

def test_constructs_sorted_by_frequency():
    from app.analysis.razor_constructs import RazorConstructExtractor
    ex = RazorConstructExtractor()
    text = """
    @Html.TextBoxFor(m => m.A)
    @Html.TextBoxFor(m => m.B)
    @Html.CheckBoxFor(m => m.C)
    """
    ex.scan_text(text, "Test.cshtml")
    constructs = ex.unique_constructs()
    # TextBoxFor (2) should come before CheckBoxFor (1)
    assert constructs[0].kind == "TextBoxFor"
    assert constructs[0].occurrences == 2


# ── example_files capped at 3 ─────────────────────────────────────────────────

def test_example_files_capped():
    from app.analysis.razor_constructs import RazorConstructExtractor
    ex = RazorConstructExtractor()
    for i in range(5):
        ex.scan_text("@Html.TextBoxFor(m => m.X)", f"File{i}.cshtml")
    tb = [c for c in ex.unique_constructs() if c.kind == "TextBoxFor"][0]
    assert tb.occurrences == 5
    assert len(tb.example_files) == 3   # capped


# ── page_map scanning ─────────────────────────────────────────────────────────

def test_scan_page_map(tmp_path):
    from app.analysis.razor_constructs import RazorConstructExtractor

    # Build a tiny repo
    root = tmp_path / "repo"
    (root / "Views" / "User").mkdir(parents=True)
    (root / "Views" / "Shared").mkdir(parents=True)
    (root / "Views" / "User" / "Edit.cshtml").write_text("@Html.TextBoxFor(m => m.Name)\n")
    (root / "Views" / "User" / "_Form.cshtml").write_text("@Html.CheckBoxFor(m => m.Active)\n")
    (root / "Views" / "Shared" / "_Layout.cshtml").write_text("@RenderBody()\n")

    page_map = {
        "pages": [{
            "entry_view": "Views/User/Edit.cshtml",
            "partials": ["Views/User/_Form.cshtml"],
            "layout": "Views/Shared/_Layout.cshtml",
        }]
    }

    ex = RazorConstructExtractor()
    ex.scan_page_map(page_map, str(root))
    kinds = {c.kind for c in ex.unique_constructs()}
    assert "TextBoxFor" in kinds        # from entry view
    assert "CheckBoxFor" in kinds       # from partial


def test_summary_structure():
    from app.analysis.razor_constructs import RazorConstructExtractor
    ex = RazorConstructExtractor()
    ex.scan_text("@Html.TextBoxFor(m => m.A)\n@foreach (var x in y) {}", "T.cshtml")
    s = ex.summary()
    assert s["unique_constructs"] == 2
    assert s["total_occurrences"] == 2
    assert "html_helper" in s["by_family"]
    assert "control_flow" in s["by_family"]
