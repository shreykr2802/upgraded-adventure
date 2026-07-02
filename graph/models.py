"""Graph node/edge models for the Razor dependency indexer."""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class ViewNode:
    """A parsed .cshtml file."""
    path: str                          # relative path from project root
    kind: str = "view"                 # view | partial | layout | viewstart
    model_type: Optional[str] = None   # @model directive
    explicit_layout: Optional[str] = None  # Layout = "..." (None = unset, "" = Layout=null)
    layout_is_null: bool = False
    partials: list = field(default_factory=list)       # partial names referenced
    sections_defined: list = field(default_factory=list)
    sections_rendered: list = field(default_factory=list)  # RenderSection calls (layouts)
    renders_body: bool = False
    viewbag_reads: list = field(default_factory=list)
    viewdata_reads: list = field(default_factory=list)
    tempdata_reads: list = field(default_factory=list)
    html_helpers: list = field(default_factory=list)   # Html.X(...) helper usage
    url_refs: list = field(default_factory=list)       # Url.Action / Html.ActionLink targets
    scripts: list = field(default_factory=list)        # <script src>, Scripts.Render bundles
    styles: list = field(default_factory=list)         # <link href>, Styles.Render bundles
    inline_script_lines: int = 0                       # LOC of inline <script> (jQuery risk signal)
    forms: list = field(default_factory=list)          # form posts (BeginForm / <form>)
    editor_display_templates: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class ActionNode:
    """A controller action discovered via C# parsing."""
    controller: str                    # e.g. "Orders" (suffix stripped)
    controller_class: str              # e.g. "OrdersController"
    controller_file: str
    base_classes: list = field(default_factory=list)
    action: str = ""
    http_methods: list = field(default_factory=list)
    route_attrs: list = field(default_factory=list)    # [Route("...")] values
    view_calls: list = field(default_factory=list)     # explicit View("Name") / PartialView("Name")
    returns_default_view: bool = False                 # bare return View(...)
    model_types_passed: list = field(default_factory=list)
    viewbag_writes: list = field(default_factory=list)
    viewdata_writes: list = field(default_factory=list)
    tempdata_writes: list = field(default_factory=list)
    redirects_to: list = field(default_factory=list)   # RedirectToAction targets

    def to_dict(self):
        return asdict(self)


@dataclass
class RouteEntry:
    """route → action → resolved view mapping."""
    route: str
    controller: str
    action: str
    http_methods: list
    view_path: Optional[str]

    def to_dict(self):
        return asdict(self)
