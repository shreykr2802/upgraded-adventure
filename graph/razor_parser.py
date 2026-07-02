"""Parse .cshtml files into ViewNode graph nodes.

Regex-based on purpose: Razor is a mixed C#/HTML dialect with no great
off-the-shelf Python parser, and the constructs we need (directives,
helper calls, tag helpers) are syntactically regular enough for this
to be reliable. Anything exotic gets flagged, not silently dropped.
"""
import re
from pathlib import Path
from models import ViewNode

RE_MODEL = re.compile(r'^\s*@model\s+([\w\.\<\>\?\[\],\s]+?)\s*$', re.MULTILINE)
RE_LAYOUT = re.compile(r'Layout\s*=\s*(null|"([^"]*)")', re.MULTILINE)
# Html.Partial / Html.RenderPartial / await Html.PartialAsync / RenderPartialAsync
RE_PARTIAL_HELPER = re.compile(
    r'Html\.(?:RenderPartialAsync|PartialAsync|RenderPartial|Partial)\s*\(\s*"([^"]+)"')
# <partial name="..."/> tag helper (Core) — kept for hybrid codebases
RE_PARTIAL_TAG = re.compile(r'<partial\s+[^>]*name\s*=\s*"([^"]+)"', re.IGNORECASE)
RE_SECTION_DEF = re.compile(r'@section\s+(\w+)')
RE_SECTION_RENDER = re.compile(r'RenderSection(?:Async)?\s*\(\s*"(\w+)"(?:\s*,\s*(?:required\s*:\s*)?(true|false))?')
RE_RENDER_BODY = re.compile(r'@RenderBody\s*\(\s*\)')
RE_VIEWBAG = re.compile(r'ViewBag\.(\w+)')
RE_VIEWDATA = re.compile(r'ViewData\[\s*"([^"]+)"\s*\]')
RE_TEMPDATA = re.compile(r'TempData\[\s*"([^"]+)"\s*\]')
RE_HTML_HELPER = re.compile(r'Html\.(\w+)\s*\(')
RE_URL_ACTION = re.compile(
    r'(?:Url\.Action|Html\.ActionLink)\s*\(\s*"([^"]+)"(?:\s*,\s*"([^"]+)")?')
RE_SCRIPT_SRC = re.compile(r'<script[^>]+src\s*=\s*"([^"]+)"', re.IGNORECASE)
RE_LINK_HREF = re.compile(r'<link[^>]+href\s*=\s*"([^"]+)"', re.IGNORECASE)
RE_BUNDLE_SCRIPTS = re.compile(r'Scripts\.Render\s*\(\s*"([^"]+)"')
RE_BUNDLE_STYLES = re.compile(r'Styles\.Render\s*\(\s*"([^"]+)"')
RE_INLINE_SCRIPT = re.compile(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>',
                              re.IGNORECASE | re.DOTALL)
RE_BEGINFORM = re.compile(
    r'Html\.BeginForm\s*\(\s*(?:"([^"]+)"\s*(?:,\s*"([^"]+)")?)?')
RE_HTML_FORM = re.compile(r'<form[^>]*action\s*=\s*"([^"]+)"', re.IGNORECASE)
RE_TEMPLATE_HELPER = re.compile(r'Html\.(?:EditorFor|DisplayFor|EditorForModel|DisplayForModel)\b')

# Helpers that are structural noise vs. ones worth surfacing for migration
_IGNORED_HELPERS = {"Raw", "Partial", "RenderPartial", "PartialAsync",
                    "RenderPartialAsync", "ActionLink", "BeginForm", "EndForm",
                    "AntiForgeryToken"}


def classify_view(rel_path: str, text: str) -> str:
    name = Path(rel_path).name.lower()
    if name == "_viewstart.cshtml":
        return "viewstart"
    if RE_RENDER_BODY.search(text):
        return "layout"
    if name.startswith("_"):
        return "partial"
    return "view"


def parse_cshtml(abs_path: Path, root: Path) -> ViewNode:
    text = abs_path.read_text(encoding="utf-8", errors="replace")
    rel = abs_path.relative_to(root).as_posix()
    node = ViewNode(path=rel, kind=classify_view(rel, text))

    m = RE_MODEL.search(text)
    if m:
        node.model_type = m.group(1).strip()

    lm = RE_LAYOUT.search(text)
    if lm:
        if lm.group(1) == "null":
            node.layout_is_null = True
        else:
            node.explicit_layout = lm.group(2)

    node.partials = sorted(set(RE_PARTIAL_HELPER.findall(text))
                           | set(RE_PARTIAL_TAG.findall(text)))
    node.sections_defined = sorted(set(RE_SECTION_DEF.findall(text)))
    node.sections_rendered = sorted(
        {m[0] for m in RE_SECTION_RENDER.findall(text)
         if m[1] != "false"})  # only required sections count as obligations
    node.renders_body = bool(RE_RENDER_BODY.search(text))
    node.viewbag_reads = sorted(set(RE_VIEWBAG.findall(text)))
    node.viewdata_reads = sorted(set(RE_VIEWDATA.findall(text)))
    node.tempdata_reads = sorted(set(RE_TEMPDATA.findall(text)))
    node.html_helpers = sorted({h for h in RE_HTML_HELPER.findall(text)}
                               - _IGNORED_HELPERS)
    node.url_refs = [{"action": a, "controller": c or None}
                     for a, c in RE_URL_ACTION.findall(text)]
    node.scripts = sorted(set(RE_SCRIPT_SRC.findall(text))
                          | set(RE_BUNDLE_SCRIPTS.findall(text)))
    node.styles = sorted(set(RE_LINK_HREF.findall(text))
                         | set(RE_BUNDLE_STYLES.findall(text)))
    node.inline_script_lines = sum(
        len([l for l in blk.splitlines() if l.strip()])
        for blk in RE_INLINE_SCRIPT.findall(text))

    forms = []
    for action, controller in RE_BEGINFORM.findall(text):
        forms.append({"type": "BeginForm",
                      "action": action or None,
                      "controller": controller or None})
    for action in RE_HTML_FORM.findall(text):
        forms.append({"type": "html_form", "action": action, "controller": None})
    node.forms = forms

    if RE_TEMPLATE_HELPER.search(text):
        node.editor_display_templates.append("uses Editor/Display templates")

    return node


def scan_views(root: Path) -> dict:
    """Return {rel_path: ViewNode} for every .cshtml under root."""
    out = {}
    for p in sorted(root.rglob("*.cshtml")):
        if any(part in ("bin", "obj", "node_modules") for part in p.parts):
            continue
        node = parse_cshtml(p, root)
        out[node.path] = node
    return out
