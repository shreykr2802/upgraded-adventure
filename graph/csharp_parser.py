"""Scan C# controllers with tree-sitter.

Extracts per action: HTTP verbs, route attributes, View()/PartialView()
calls (with view-name and model inference), ViewBag/ViewData/TempData
writes, and RedirectToAction targets. Also resolves base-class chains so
ViewBag values set in a BaseController are attributed to every action
that inherits them (a top source of "missing data" in file-by-file
conversion).
"""
import re
from pathlib import Path
import tree_sitter_c_sharp as tscs
from tree_sitter import Language, Parser
from models import ActionNode

CSHARP = Language(tscs.language())
_parser = Parser(CSHARP)

ACTION_RESULT_TYPES = {
    "ActionResult", "IActionResult", "ViewResult", "PartialViewResult",
    "JsonResult", "RedirectResult", "RedirectToRouteResult", "FileResult",
    "ContentResult", "Task",  # Task<ActionResult> handled via generic check
}
HTTP_ATTRS = {"HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT",
              "HttpDelete": "DELETE", "HttpPatch": "PATCH"}

RE_VIEW_CALL = re.compile(
    r'\b(View|PartialView)\s*\(\s*(?:"([^"]+)"\s*)?(?:,?\s*([^)]+?))?\)')
RE_VIEWBAG_WRITE = re.compile(r'ViewBag\.(\w+)\s*=')
RE_VIEWDATA_WRITE = re.compile(r'ViewData\[\s*"([^"]+)"\s*\]\s*=')
RE_TEMPDATA_WRITE = re.compile(r'TempData\[\s*"([^"]+)"\s*\]\s*=')
RE_REDIRECT = re.compile(
    r'RedirectToAction\s*\(\s*(?:nameof\s*\(\s*(\w+)\s*\)|"(\w+)")'
    r'(?:\s*,\s*"(\w+)")?')
RE_NEW_MODEL = re.compile(r'new\s+([\w\.]+(?:<[^>]*>)?)\s*[({]')


def _node_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk(node, kind):
    """Yield all descendants of a given kind."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            yield n
        stack.extend(reversed(n.children))


def _attr_list(node, src):
    """Return attribute names + string args for a class/method node."""
    attrs = []
    for child in node.children:
        if child.type == "attribute_list":
            for attr in _walk(child, "attribute"):
                name_node = attr.child_by_field_name("name")
                name = _node_text(name_node, src) if name_node else ""
                arg = None
                m = re.search(r'\(\s*"([^"]*)"', _node_text(attr, src))
                if m:
                    arg = m.group(1)
                attrs.append((name.split(".")[-1], arg))
    return attrs


def _returns_action_result(method_node, src) -> bool:
    rt = method_node.child_by_field_name("returns") or \
         method_node.child_by_field_name("type")
    if rt is None:
        return False
    text = _node_text(rt, src)
    base = re.split(r'[<\s]', text.strip())[0]
    if base == "Task":
        inner = re.search(r'Task\s*<\s*([\w]+)', text)
        return bool(inner and inner.group(1) in ACTION_RESULT_TYPES)
    return base in ACTION_RESULT_TYPES


def parse_controller_file(abs_path: Path, root: Path) -> list:
    src = abs_path.read_bytes()
    tree = _parser.parse(src)
    rel = abs_path.relative_to(root).as_posix()
    actions = []

    for cls in _walk(tree.root_node, "class_declaration"):
        cls_name_node = cls.child_by_field_name("name")
        if cls_name_node is None:
            continue
        cls_name = _node_text(cls_name_node, src)
        if not cls_name.endswith("Controller"):
            continue

        bases = []
        for b in cls.children:
            if b.type == "base_list":
                bases = [t.strip() for t in
                         _node_text(b, src).lstrip(":").split(",")]
        cls_attrs = _attr_list(cls, src)
        cls_routes = [a for n, a in cls_attrs if n == "Route" and a]

        body = cls.child_by_field_name("body")
        if body is None:
            continue

        for method in _walk(body, "method_declaration"):
            # only public methods that plausibly return an action result
            mods = [ _node_text(c, src) for c in method.children
                     if c.type == "modifier" ]
            if "public" not in mods:
                continue
            if not _returns_action_result(method, src):
                continue

            name = _node_text(method.child_by_field_name("name"), src)
            m_attrs = _attr_list(method, src)
            if any(n == "NonAction" for n, _ in m_attrs):
                continue

            act = ActionNode(
                controller=cls_name[:-len("Controller")],
                controller_class=cls_name,
                controller_file=rel,
                base_classes=bases,
                action=name,
            )
            act.http_methods = sorted({HTTP_ATTRS[n] for n, _ in m_attrs
                                       if n in HTTP_ATTRS}) or ["GET"]
            m_routes = [a for n, a in m_attrs if n == "Route" and a is not None]
            act.route_attrs = [f"{cr}/{mr}".strip("/") if cr else mr
                               for cr in (cls_routes or [None])
                               for mr in (m_routes or [None]) if mr] \
                              or cls_routes

            body_node = method.child_by_field_name("body")
            body_text = _node_text(body_node, src) if body_node else ""

            for kind, view_name, args in RE_VIEW_CALL.findall(body_text):
                if view_name:
                    act.view_calls.append(
                        {"kind": kind, "view": view_name})
                else:
                    act.returns_default_view = True
                if args:
                    for mt in RE_NEW_MODEL.findall(args):
                        act.model_types_passed.append(mt)
            act.viewbag_writes = sorted(set(RE_VIEWBAG_WRITE.findall(body_text)))
            act.viewdata_writes = sorted(set(RE_VIEWDATA_WRITE.findall(body_text)))
            act.tempdata_writes = sorted(set(RE_TEMPDATA_WRITE.findall(body_text)))
            act.redirects_to = [
                {"action": a or b, "controller": c or None}
                for a, b, c in RE_REDIRECT.findall(body_text)]
            actions.append(act)

    return actions


def collect_base_writes(abs_path: Path, root: Path) -> dict:
    """For non-suffix 'Controller' base classes (e.g. BaseController):
    capture ViewBag/ViewData writes anywhere in the class so they can be
    inherited by derived controllers' actions."""
    src = abs_path.read_bytes()
    tree = _parser.parse(src)
    out = {}
    for cls in _walk(tree.root_node, "class_declaration"):
        name_node = cls.child_by_field_name("name")
        if name_node is None:
            continue
        name = _node_text(name_node, src)
        text = _node_text(cls, src)
        vb = sorted(set(RE_VIEWBAG_WRITE.findall(text)))
        vd = sorted(set(RE_VIEWDATA_WRITE.findall(text)))
        if vb or vd:
            out[name] = {"viewbag": vb, "viewdata": vd}
    return out


def scan_controllers(root: Path):
    """Return (actions, base_class_writes) for all .cs files under root."""
    actions, base_writes = [], {}
    for p in sorted(root.rglob("*.cs")):
        if any(part in ("bin", "obj", "node_modules", "Migrations")
               for part in p.parts):
            continue
        try:
            actions.extend(parse_controller_file(p, root))
            base_writes.update(collect_base_writes(p, root))
        except Exception as e:  # never let one bad file kill the index
            print(f"  [warn] failed to parse {p}: {e}")
    return actions, base_writes
