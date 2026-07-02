"""Build the dependency graph and compute per-route render closures.

Resolution rules implemented (classic ASP.NET MVC + Core conventions):
- View lookup: Views/{Controller}/{View}.cshtml then Views/Shared/{View}.cshtml
- Partial lookup: same-controller folder, then Shared, honoring "~/" and
  explicit .cshtml paths
- Layout: explicit `Layout = "..."` wins; otherwise nearest _ViewStart.cshtml
  walking up from the view's folder; `Layout = null` stops the chain
- Layouts can themselves have layouts (nested layout chains)
- ViewBag writes from base controllers are inherited into the closure
"""
from pathlib import PurePosixPath
from models import RouteEntry


class Graph:
    def __init__(self, views: dict, actions: list, base_writes: dict):
        self.views = views              # {rel_path: ViewNode}
        self.actions = actions          # [ActionNode]
        self.base_writes = base_writes  # {ClassName: {viewbag, viewdata}}
        self.viewstarts = {p: v for p, v in views.items()
                           if v.kind == "viewstart"}
        self.unresolved = []            # things we could not resolve → report

    # ---------- path resolution ----------

    def _exists(self, path: str):
        return path if path in self.views else None

    def resolve_view(self, controller: str, name: str):
        """Resolve a view/partial name to a rel path, MVC-convention style."""
        if name.startswith("~/"):
            return self._exists(name[2:]) or self._mark_unresolved(name)
        if name.endswith(".cshtml"):
            return self._exists(name) or self._mark_unresolved(name)
        candidates = [
            f"Views/{controller}/{name}.cshtml",
            f"Views/Shared/{name}.cshtml",
        ]
        for c in candidates:
            if c in self.views:
                return c
        return self._mark_unresolved(f"{controller}/{name}")

    def _mark_unresolved(self, ref: str):
        self.unresolved.append(ref)
        return None

    def layout_for(self, view_path: str):
        """Explicit layout, else nearest _ViewStart walking up the tree."""
        node = self.views[view_path]
        if node.layout_is_null:
            return None
        if node.explicit_layout:
            return self._resolve_layout_name(view_path, node.explicit_layout)
        folder = PurePosixPath(view_path).parent
        while True:
            vs = (folder / "_ViewStart.cshtml").as_posix()
            if vs in self.viewstarts:
                vs_node = self.viewstarts[vs]
                if vs_node.layout_is_null:
                    return None
                if vs_node.explicit_layout:
                    return self._resolve_layout_name(vs, vs_node.explicit_layout)
            if folder == PurePosixPath("."):
                return None
            folder = folder.parent

    def _resolve_layout_name(self, from_path: str, layout: str):
        if layout.startswith("~/"):
            return self._exists(layout[2:]) or self._mark_unresolved(layout)
        if layout.endswith(".cshtml"):
            # bare filename → resolve relative to Shared, then same folder
            name = PurePosixPath(layout).name
            same = (PurePosixPath(from_path).parent / name).as_posix()
            shared = f"Views/Shared/{name}"
            return self._exists(shared) or self._exists(same) \
                or self._mark_unresolved(layout)
        return self._mark_unresolved(layout)

    # ---------- routes ----------

    def build_routes(self):
        routes = []
        for act in self.actions:
            view_path = None
            explicit = [vc for vc in act.view_calls if vc["kind"] == "View"]
            if explicit:
                view_path = self.resolve_view(act.controller,
                                              explicit[0]["view"])
            elif act.returns_default_view:
                view_path = self.resolve_view(act.controller, act.action)

            if act.route_attrs:
                for r in act.route_attrs:
                    routes.append(RouteEntry(
                        route="/" + r.strip("/"), controller=act.controller,
                        action=act.action, http_methods=act.http_methods,
                        view_path=view_path))
            else:  # conventional routing
                routes.append(RouteEntry(
                    route=f"/{act.controller}/{act.action}",
                    controller=act.controller, action=act.action,
                    http_methods=act.http_methods, view_path=view_path))
        return routes

    # ---------- closure ----------

    def render_closure(self, action, view_path: str):
        """Everything that participates in rendering this action's page."""
        files, order = set(), []
        vb_provided = set(action.viewbag_writes)
        vd_provided = set(action.viewdata_writes)
        for base in action.base_classes:
            bw = self.base_writes.get(base)
            if bw:
                vb_provided |= set(bw["viewbag"])
                vd_provided |= set(bw["viewdata"])

        def add(path, role):
            if path is None or path in files:
                return
            files.add(path)
            node = self.views[path]
            order.append({"path": path, "role": role})
            # partials referenced by this file (also from PartialView calls)
            ctrl = action.controller
            for p in node.partials:
                add(self.resolve_view(ctrl, p), "partial")

        add(view_path, "view")
        # explicit PartialView(...) returned by the action itself
        for vc in action.view_calls:
            if vc["kind"] == "PartialView":
                add(self.resolve_view(action.controller, vc["view"]), "partial")

        # layout chain (layouts can nest)
        current, guard = view_path, 0
        while current is not None and guard < 10:
            layout = self.layout_for(current)
            if layout is None or layout in files:
                break
            add(layout, "layout")
            current, guard = layout, guard + 1

        # aggregate signals across the closure
        vb_read, vd_read, td_read = set(), set(), set()
        scripts, styles, helpers, forms = set(), set(), set(), []
        sections_def, sections_needed = set(), set()
        inline_js = 0
        model_types = set()
        for f in order:
            n = self.views[f["path"]]
            vb_read |= set(n.viewbag_reads)
            vd_read |= set(n.viewdata_reads)
            td_read |= set(n.tempdata_reads)
            scripts |= set(n.scripts)
            styles |= set(n.styles)
            helpers |= set(n.html_helpers)
            forms.extend(n.forms)
            sections_def |= set(n.sections_defined)
            sections_needed |= set(n.sections_rendered)
            inline_js += n.inline_script_lines
            if n.model_type:
                model_types.add(n.model_type)
        model_types |= set(action.model_types_passed)

        gaps = {
            "viewbag_read_but_never_written": sorted(vb_read - vb_provided),
            "viewdata_read_but_never_written": sorted(vd_read - vd_provided),
            "sections_rendered_but_undefined": sorted(
                sections_needed - sections_def),
        }
        complexity = ("complex" if inline_js > 40 or len(order) > 6
                      else "medium" if inline_js > 0 or len(order) > 3
                      else "simple")

        return {
            "controller": action.controller,
            "action": action.action,
            "http_methods": action.http_methods,
            "files": order,
            "model_types": sorted(model_types),
            "viewbag": {"provided": sorted(vb_provided),
                        "read": sorted(vb_read)},
            "viewdata": {"provided": sorted(vd_provided),
                         "read": sorted(vd_read)},
            "tempdata_read": sorted(td_read),
            "scripts": sorted(scripts),
            "styles": sorted(styles),
            "html_helpers": sorted(helpers),
            "forms": forms,
            "inline_script_lines": inline_js,
            "complexity": complexity,
            "gaps": gaps,
        }
