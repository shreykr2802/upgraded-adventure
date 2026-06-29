"""
analysis/razor_constructs.py
────────────────────────────
Extracts every DISTINCT Razor construct used across the .NET codebase, so the
rule-derivation agent can map each unique construct to a React equivalent
exactly once — instead of re-deriving the same rule for thousands of repeats.

Example: a repo may contain @Html.TextBoxFor 400 times. That is ONE construct.
This module collapses all 400 into a single representative entry.

Pure static analysis — no LLM. Output feeds the A3 rule-derivation step.

Construct families detected:
  - HTML helpers:        @Html.TextBoxFor, @Html.DropDownListFor, @Html.CheckBoxFor, ...
  - Editor/display:      @Html.EditorFor, @Html.DisplayFor
  - Form helpers:        @using (Html.BeginForm(...)), @Html.AntiForgeryToken
  - Validation:          @Html.ValidationMessageFor, @Html.ValidationSummary
  - Links/actions:       @Html.ActionLink, @Url.Action
  - Control flow:        @foreach, @if, @for, @while, @switch
  - Auth/role:           @if (User.IsInRole(...)), @if (User.Identity...)
  - Partials:            @Html.Partial, @await Html.PartialAsync, <partial>
  - Raw expressions:     @Model.X, @ViewBag.X, @ViewData[...]
  - Tag helpers:         asp-for, asp-action, asp-controller, asp-validation-for
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from collections import defaultdict


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class RazorConstruct:
    family: str                 # e.g. "html_helper", "control_flow", "auth"
    kind: str                   # e.g. "TextBoxFor", "foreach", "IsInRole"
    representative: str         # one real example snippet from the code
    occurrences: int            # how many times seen across the repo
    example_files: list[str] = field(default_factory=list)  # up to 3 sample files

    def signature(self) -> str:
        return f"{self.family}:{self.kind}"


# ── Pattern definitions ───────────────────────────────────────────────────────
# Each entry: (family, kind, compiled regex). The regex should capture enough
# of the construct to be a useful example, but kind is what dedupes.

_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    # HTML form helpers (the *For variants)
    ("html_helper", "TextBoxFor",      re.compile(r"@Html\.TextBoxFor\([^)]*\)")),
    ("html_helper", "TextAreaFor",     re.compile(r"@Html\.TextAreaFor\([^)]*\)")),
    ("html_helper", "PasswordFor",     re.compile(r"@Html\.PasswordFor\([^)]*\)")),
    ("html_helper", "DropDownListFor", re.compile(r"@Html\.DropDownListFor\([^)]*\)")),
    ("html_helper", "ListBoxFor",      re.compile(r"@Html\.ListBoxFor\([^)]*\)")),
    ("html_helper", "CheckBoxFor",     re.compile(r"@Html\.CheckBoxFor\([^)]*\)")),
    ("html_helper", "RadioButtonFor",  re.compile(r"@Html\.RadioButtonFor\([^)]*\)")),
    ("html_helper", "HiddenFor",       re.compile(r"@Html\.HiddenFor\([^)]*\)")),
    ("html_helper", "LabelFor",        re.compile(r"@Html\.LabelFor\([^)]*\)")),
    ("html_helper", "DisplayNameFor",  re.compile(r"@Html\.DisplayNameFor\([^)]*\)")),

    # Editor / display templates
    ("editor", "EditorFor",   re.compile(r"@Html\.EditorFor\([^)]*\)")),
    ("editor", "DisplayFor",  re.compile(r"@Html\.DisplayFor\([^)]*\)")),

    # Forms
    ("form", "BeginForm",        re.compile(r"@using\s*\(\s*Html\.BeginForm\([^)]*\)\s*\)")),
    ("form", "AntiForgeryToken", re.compile(r"@Html\.AntiForgeryToken\(\)")),

    # Validation
    ("validation", "ValidationMessageFor", re.compile(r"@Html\.ValidationMessageFor\([^)]*\)")),
    ("validation", "ValidationSummary",    re.compile(r"@Html\.ValidationSummary\([^)]*\)")),

    # Links / URLs
    ("link", "ActionLink", re.compile(r"@Html\.ActionLink\([^)]*\)")),
    ("link", "UrlAction",  re.compile(r"@Url\.Action\([^)]*\)")),

    # Control flow
    ("control_flow", "foreach", re.compile(r"@foreach\s*\([^)]*\)")),
    ("control_flow", "for",     re.compile(r"@for\s*\([^)]*\)")),
    ("control_flow", "while",   re.compile(r"@while\s*\([^)]*\)")),
    ("control_flow", "switch",  re.compile(r"@switch\s*\([^)]*\)")),

    # Auth / role (checked before generic @if so these win)
    ("auth", "IsInRole",       re.compile(r"@if\s*\(\s*User\.IsInRole\([^)]*\)\s*\)")),
    ("auth", "IsAuthenticated", re.compile(r"User\.Identity\.IsAuthenticated")),

    # Generic @if (after auth)
    ("control_flow", "if", re.compile(r"@if\s*\([^)]*\)")),

    # Partials
    ("partial", "Partial",        re.compile(r"@Html\.Partial\([^)]*\)")),
    ("partial", "RenderPartial",  re.compile(r"@Html\.RenderPartial\([^)]*\)")),
    ("partial", "PartialAsync",   re.compile(r"@await\s+Html\.PartialAsync\([^)]*\)")),
    ("partial", "TagPartial",     re.compile(r"<partial\s+[^>]*>")),

    # Tag helpers (ASP.NET Core)
    ("tag_helper", "asp-for",            re.compile(r'asp-for=["\'][^"\']*["\']')),
    ("tag_helper", "asp-action",         re.compile(r'asp-action=["\'][^"\']*["\']')),
    ("tag_helper", "asp-controller",     re.compile(r'asp-controller=["\'][^"\']*["\']')),
    ("tag_helper", "asp-validation-for", re.compile(r'asp-validation-for=["\'][^"\']*["\']')),

    # Raw expressions (last — most generic)
    ("expression", "ViewBag",  re.compile(r"@ViewBag\.\w+")),
    ("expression", "ViewData", re.compile(r"@ViewData\[[^\]]*\]")),
    # @Model.X but NOT the @model directive (lowercase) which declares the type
    ("expression", "ModelProperty", re.compile(r"@Model\.\w+")),
]


# ── Extractor ─────────────────────────────────────────────────────────────────

class RazorConstructExtractor:
    """
    Walks a set of .cshtml files and collects every distinct Razor construct.

    Usage:
        ex = RazorConstructExtractor()
        ex.scan_files(["Views/User/Edit.cshtml", ...], repo_root="/path")
        constructs = ex.unique_constructs()   # deduped list
    """

    def __init__(self):
        # signature → aggregated RazorConstruct
        self._agg: dict[str, RazorConstruct] = {}

    def scan_text(self, text: str, source_file: str = ""):
        """Extract constructs from a single file's text and aggregate."""
        # Track which char ranges are already claimed by a more specific pattern,
        # so e.g. an auth @if isn't also counted as a generic @if.
        claimed: list[tuple[int, int]] = []

        def overlaps(start: int, end: int) -> bool:
            return any(s < end and start < e for s, e in claimed)

        for family, kind, pattern in _PATTERNS:
            for m in pattern.finditer(text):
                if overlaps(m.start(), m.end()):
                    continue
                claimed.append((m.start(), m.end()))
                sig = f"{family}:{kind}"
                snippet = m.group(0).strip()
                if sig not in self._agg:
                    self._agg[sig] = RazorConstruct(
                        family=family, kind=kind,
                        representative=snippet, occurrences=1,
                        example_files=[source_file] if source_file else [],
                    )
                else:
                    rc = self._agg[sig]
                    rc.occurrences += 1
                    if source_file and source_file not in rc.example_files and len(rc.example_files) < 3:
                        rc.example_files.append(source_file)

    def scan_files(self, rel_paths: list[str], repo_root: str):
        """Scan a list of .cshtml files (relative paths under repo_root)."""
        for rel in rel_paths:
            abs_path = os.path.join(repo_root, rel)
            if not os.path.exists(abs_path):
                continue
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                self.scan_text(f.read(), source_file=rel)

    def scan_page_map(self, page_map: dict, repo_root: str):
        """
        Scan every view file referenced in a page_map.json
        (entry views + partials + layouts across all pages).
        """
        seen: set[str] = set()
        for page in page_map.get("pages", []):
            views = [page.get("entry_view")] + page.get("partials", [])
            if page.get("layout"):
                views.append(page["layout"])
            for v in views:
                if v and v not in seen:
                    seen.add(v)
        self.scan_files(sorted(seen), repo_root)

    def unique_constructs(self) -> list[RazorConstruct]:
        """Return the deduplicated constructs, most frequent first."""
        return sorted(self._agg.values(), key=lambda c: c.occurrences, reverse=True)

    def summary(self) -> dict:
        constructs = self.unique_constructs()
        total_occurrences = sum(c.occurrences for c in constructs)
        by_family: dict[str, int] = defaultdict(int)
        for c in constructs:
            by_family[c.family] += 1
        return {
            "unique_constructs": len(constructs),
            "total_occurrences": total_occurrences,
            "by_family": dict(by_family),
        }
