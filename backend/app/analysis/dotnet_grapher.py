"""
analysis/dotnet_grapher.py
──────────────────────────
Builds a dependency graph of a .NET MVC / Razor codebase so the agent can
migrate a *whole page* (view + partials + layout + controller + model) as one
unit, instead of treating each .cshtml file in isolation.

This is needed because one logical page is spread across many files in
different folders:
    Views/User/Edit.cshtml            ← entry view
    Views/User/_EditForm.cshtml       ← partial pulled in via @Html.Partial
    Views/Shared/_Address.cshtml      ← shared partial / EditorFor template
    Views/Shared/_Layout.cshtml       ← layout wrapper
    Controllers/UserController.cs     ← action with `return View("Edit")`
    Models/UserEditModel.cs           ← bound model (+ nested models)

DESIGN CHOICES (per project requirements):
  - Regex-based resolution (not a full C#/Razor parser). Fast, good enough,
    flags anything it cannot resolve.
  - Two modes: map_repo() for the whole repo, resolve_page() on demand.
  - Unresolved links (dynamic view names, missing files) are flagged and
    skipped, collected into `unresolved` for the user to review.

This produces PageCluster objects. It does NOT call any LLM — pure static
analysis. The agent consumes these clusters downstream.
"""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class UnresolvedLink:
    kind: str          # "partial" | "view" | "model" | "layout"
    reference: str     # the raw reference that couldn't be resolved
    source_file: str   # where the reference appeared
    reason: str        # why it couldn't be resolved


@dataclass
class PageCluster:
    """A fully-resolved logical page, assembled across folders."""
    page_name: str                         # logical name e.g. "User/Edit"
    entry_view: str                        # path to the main .cshtml
    partials: list[str] = field(default_factory=list)
    layout: str | None = None
    controller: str | None = None
    controller_action: str | None = None
    model: str | None = None
    nested_models: list[str] = field(default_factory=list)
    unresolved: list[UnresolvedLink] = field(default_factory=list)

    def all_view_files(self) -> list[str]:
        files = [self.entry_view] + self.partials
        if self.layout:
            files.append(self.layout)
        return files

    def all_files(self) -> list[str]:
        files = self.all_view_files()
        if self.controller:
            files.append(self.controller)
        if self.model:
            files.append(self.model)
        files.extend(self.nested_models)
        return files

    def summary(self) -> str:
        return (
            f"{self.page_name}: view + {len(self.partials)} partials, "
            f"layout={'yes' if self.layout else 'no'}, "
            f"controller={'yes' if self.controller else 'no'}, "
            f"model={'yes' if self.model else 'no'}, "
            f"unresolved={len(self.unresolved)}"
        )


# ── Regex patterns ────────────────────────────────────────────────────────────

# Partial includes in .cshtml
_PARTIAL_PATTERNS = [
    re.compile(r"""@Html\.Partial(?:Async)?\(\s*["']([^"']+)["']""", re.IGNORECASE),
    re.compile(r"""@Html\.RenderPartial(?:Async)?\(\s*["']([^"']+)["']""", re.IGNORECASE),
    re.compile(r"""@await\s+Html\.PartialAsync\(\s*["']([^"']+)["']""", re.IGNORECASE),
    re.compile(r"""<partial\s+name=["']([^"']+)["']""", re.IGNORECASE),   # tag helper
]
# EditorFor / DisplayFor — template resolved by type/name convention
_EDITOR_PATTERN = re.compile(r"""@Html\.(?:Editor|Display)For\(""", re.IGNORECASE)
# Captures the lambda/property expression inside EditorFor/DisplayFor, e.g.
#   @Html.EditorFor(m => m.Address)  → "m => m.Address"
#   @Html.DisplayFor(model => model.User.Name) → "model => model.User.Name"
_EDITOR_FOR_PROP = re.compile(
    r"""@Html\.(?:Editor|Display)For\(\s*([^,)]+?)\s*(?:,[^)]*)?\)""",
    re.IGNORECASE,
)

# Layout assignment in .cshtml
_LAYOUT_PATTERN = re.compile(r"""Layout\s*=\s*["']([^"']+)["']""")

# @model directive
_MODEL_DIRECTIVE = re.compile(r"""@model\s+([\w\.<>]+)""")

# return View(...) in controllers
_RETURN_VIEW_STR   = re.compile(r"""return\s+View\(\s*["']([^"']+)["']""")
_RETURN_VIEW_MODEL = re.compile(r"""return\s+View\(\s*([a-zA-Z_]\w*)\s*\)""")
_RETURN_VIEW_BARE  = re.compile(r"""return\s+View\(\s*\)""")
_RETURN_VIEW_DYNAMIC = re.compile(r"""return\s+View\(\s*[^"')]*\b(?:variable|viewName|name)\b""", re.IGNORECASE)

# Action method signature (public ActionResult Edit(...))
_ACTION_METHOD = re.compile(
    r"""public\s+(?:async\s+)?(?:virtual\s+)?[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)""",
)


# ── Grapher ───────────────────────────────────────────────────────────────────

_COLLECTION_WRAPPERS = {
    "list", "ilist", "ienumerable", "icollection", "collection",
    "iqueryable", "array", "hashset", "iset",
}


def _extract_core_type(raw_type: str) -> str | None:
    """
    Extract the meaningful model class name from an @model directive value.

    Handles:
      MyApp.Models.UserModel        → UserModel
      List<MyApp.Models.UserModel>  → UserModel   (unwrap collection)
      IEnumerable<UserModel>        → UserModel
      Dictionary<string, UserModel> → UserModel   (last type arg)
      UserModel                     → UserModel
    """
    t = raw_type.strip()

    # If generic, recurse into the innermost/last type argument.
    if "<" in t and t.endswith(">"):
        outer = t[: t.index("<")].split(".")[-1].lower()
        inner = t[t.index("<") + 1 : -1]
        # For Dictionary<K,V> take the last arg; for List<T> there's just one.
        last_arg = inner.split(",")[-1].strip()
        core = _extract_core_type(last_arg)
        # If the wrapper itself isn't a known collection, still prefer the inner type.
        return core

    # Strip namespace.
    name = t.split(".")[-1].strip()
    # Ignore primitives — a primitive model is unusual and not a class file.
    if name.lower() in {"string", "int", "long", "bool", "object", "decimal"}:
        return None
    return name or None

class DotNetGrapher:
    """
    Indexes a .NET repo's file tree, then resolves page clusters.

    Usage:
        g = DotNetGrapher("/path/to/dotnet-repo")
        g.scan()                       # build the file index (fast)
        cluster = g.resolve_page("Views/User/Edit.cshtml")   # on-demand
        clusters = g.map_repo()        # whole-repo
    """

    def __init__(self, repo_root: str):
        self.repo_root = os.path.abspath(repo_root)
        self._cshtml: dict[str, str] = {}      # rel_path → abs_path
        self._cs: dict[str, str] = {}          # rel_path → abs_path
        self._cshtml_by_name: dict[str, list[str]] = {}   # basename → [rel_paths]
        self._scanned = False

    # ── Scanning ────────────────────────────────────────────────────────────

    def scan(self):
        """Walk the repo, index every .cshtml and .cs file."""
        for dirpath, _, filenames in os.walk(self.repo_root):
            # Skip common noise
            if any(skip in dirpath for skip in ("bin", "obj", "node_modules", ".git")):
                continue
            for fn in filenames:
                abs_path = os.path.join(dirpath, fn)
                rel_path = os.path.relpath(abs_path, self.repo_root)
                if fn.endswith(".cshtml"):
                    self._cshtml[rel_path] = abs_path
                    base = fn[:-len(".cshtml")].lower()
                    self._cshtml_by_name.setdefault(base, []).append(rel_path)
                elif fn.endswith(".cs"):
                    self._cs[rel_path] = abs_path
        self._scanned = True
        logger.info(
            "Scanned %s: %d .cshtml, %d .cs files",
            self.repo_root, len(self._cshtml), len(self._cs),
        )

    def _read(self, rel_path: str) -> str:
        abs_path = self._cshtml.get(rel_path) or self._cs.get(rel_path)
        if not abs_path:
            return ""
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    # ── View file resolution ──────────────────────────────────────────────────

    def _find_view_file(self, name: str, from_view: str) -> str | None:
        """
        Resolve a partial/view name to an actual file.
        Checks (in order): explicit path, same folder as caller, Shared folders.
        """
        name_clean = name.replace("~/", "").lstrip("/")

        # 1. Explicit relative path that exists as-is
        candidate = name_clean if name_clean.endswith(".cshtml") else f"{name_clean}.cshtml"
        for rel in self._cshtml:
            if rel.replace("\\", "/").lower() == candidate.replace("\\", "/").lower():
                return rel

        # 2. By basename, prefer same folder as the calling view
        base = os.path.basename(name_clean).lower()
        matches = self._cshtml_by_name.get(base, [])
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]

        # Multiple matches — prefer one in the same folder, then Shared/
        caller_dir = os.path.dirname(from_view).lower()
        same_folder = [m for m in matches if os.path.dirname(m).lower() == caller_dir]
        if same_folder:
            return same_folder[0]
        shared = [m for m in matches if "shared" in m.lower()]
        if shared:
            return shared[0]
        return matches[0]   # fall back to first

    # ── Partial resolution (recursive) ──────────────────────────────────────────

    def _resolve_partials(self, view_rel: str, seen: set[str]) -> tuple[list[str], list[UnresolvedLink]]:
        """Recursively resolve all partials referenced by a view."""
        if view_rel in seen:
            return [], []
        seen.add(view_rel)

        content = self._read(view_rel)
        partials: list[str] = []
        unresolved: list[UnresolvedLink] = []

        # Named partials
        refs: set[str] = set()
        for pat in _PARTIAL_PATTERNS:
            refs.update(pat.findall(content))

        for ref in refs:
            resolved = self._find_view_file(ref, view_rel)
            if resolved:
                if resolved not in partials:
                    partials.append(resolved)
                # Recurse — partials can include partials
                child_partials, child_unresolved = self._resolve_partials(resolved, seen)
                for cp in child_partials:
                    if cp not in partials:
                        partials.append(cp)
                unresolved.extend(child_unresolved)
            else:
                unresolved.append(UnresolvedLink(
                    kind="partial", reference=ref, source_file=view_rel,
                    reason="partial file not found in repo",
                ))

        # EditorFor / DisplayFor — try to resolve the template by convention
        # before flagging. ASP.NET looks in EditorTemplates/DisplayTemplates
        # folders for a .cshtml named after the property's type or the property.
        for em in _EDITOR_FOR_PROP.finditer(content):
            prop_expr = em.group(1)            # e.g. "m => m.Address" or "Model.Address"
            prop_name = prop_expr.split(".")[-1].strip()
            template = self._find_editor_template(prop_name, view_rel)
            if template:
                if template not in partials:
                    partials.append(template)
                    # recurse into the template's own partials
                    child_p, child_u = self._resolve_partials(template, seen)
                    for cp in child_p:
                        if cp not in partials:
                            partials.append(cp)
                    unresolved.extend(child_u)
            else:
                unresolved.append(UnresolvedLink(
                    kind="partial",
                    reference=f"EditorFor/DisplayFor: {prop_name}",
                    source_file=view_rel,
                    reason="editor/display template not found by convention — review manually",
                ))

        return partials, unresolved

    def _find_editor_template(self, prop_name: str, from_view: str) -> str | None:
        """
        Resolve an EditorFor/DisplayFor template by ASP.NET convention.
        Looks for <prop_name>.cshtml in EditorTemplates/DisplayTemplates folders
        (in the view's area and in Shared).
        """
        targets = [
            f"editortemplates/{prop_name}.cshtml".lower(),
            f"displaytemplates/{prop_name}.cshtml".lower(),
        ]
        for rel in self._cshtml:
            low = rel.replace("\\", "/").lower()
            if any(low.endswith(t) for t in targets):
                return rel
        return None

    # ── Layout resolution ──────────────────────────────────────────────────────

    def _resolve_layout(self, view_rel: str) -> tuple[str | None, UnresolvedLink | None]:
        content = self._read(view_rel)
        m = _LAYOUT_PATTERN.search(content)

        # If the view sets Layout explicitly, use that.
        if m:
            layout_ref = m.group(1)
            if layout_ref.strip().lower() in ("null", ""):
                return None, None   # explicitly no layout
            resolved = self._find_view_file(layout_ref, view_rel)
            if resolved:
                return resolved, None
            return None, UnresolvedLink(
                kind="layout", reference=layout_ref, source_file=view_rel,
                reason="layout file not found",
            )

        # No explicit Layout in the view — walk up the _ViewStart.cshtml chain.
        # .NET applies _ViewStart from the view's folder up to the repo root,
        # with the closest one winning. We search nearest-first.
        layout_ref, viewstart_file = self._layout_from_viewstart(view_rel)
        if layout_ref:
            resolved = self._find_view_file(layout_ref, viewstart_file or view_rel)
            if resolved:
                return resolved, None
            return None, UnresolvedLink(
                kind="layout", reference=layout_ref,
                source_file=viewstart_file or view_rel,
                reason="layout from _ViewStart not found",
            )

        # No explicit layout and no _ViewStart found — genuinely no layout (or a
        # global default we can't see). Not flagged as an error.
        return None, None

    def _layout_from_viewstart(self, view_rel: str) -> tuple[str | None, str | None]:
        """
        Walk from the view's folder up to the repo root looking for
        _ViewStart.cshtml files, and return the Layout assignment from the
        nearest one that sets it. Returns (layout_ref, viewstart_rel_path).
        """
        view_dir = os.path.dirname(view_rel)
        # Build the list of folders from the view's dir up to root
        parts = view_dir.replace("\\", "/").split("/") if view_dir else []
        candidates: list[str] = []
        for i in range(len(parts), -1, -1):
            folder = "/".join(parts[:i])
            vs = f"{folder}/_ViewStart.cshtml" if folder else "_ViewStart.cshtml"
            candidates.append(vs)

        for vs_rel in candidates:
            # match against the index case-insensitively
            actual = None
            for rel in self._cshtml:
                if rel.replace("\\", "/").lower() == vs_rel.lower():
                    actual = rel
                    break
            if not actual:
                continue
            content = self._read(actual)
            m = _LAYOUT_PATTERN.search(content)
            if m:
                ref = m.group(1)
                if ref.strip().lower() not in ("null", ""):
                    return ref, actual
        return None, None

    # ── Model resolution ────────────────────────────────────────────────────────

    def _resolve_model(self, view_rel: str) -> tuple[str | None, list[str], UnresolvedLink | None]:
        """Find the @model type and locate its .cs file + nested model files."""
        content = self._read(view_rel)
        m = _MODEL_DIRECTIVE.search(content)
        if not m:
            return None, [], None

        raw_type = m.group(1).strip()
        model_type = _extract_core_type(raw_type)
        if not model_type:
            return None, [], None

        model_file = self._find_cs_class(model_type)
        if not model_file:
            return None, [], UnresolvedLink(
                kind="model", reference=raw_type, source_file=view_rel,
                reason="model class file not found",
            )

        nested = self._find_nested_models(model_file, root_type=model_type)
        return model_file, nested, None

    def _find_cs_class(self, class_name: str) -> str | None:
        """
        Find the .cs file declaring `class <class_name>`.
        Matches plain, inherited (`: Base`), generic (`<T>`), and partial classes.
        """
        pattern = re.compile(
            rf"""\b(?:partial\s+)?class\s+{re.escape(class_name)}\b"""
        )
        for rel in self._cs:
            if pattern.search(self._read(rel)):
                return rel
        return None

    def _find_nested_models(self, model_file: str, root_type: str, depth: int = 0) -> list[str]:
        """Follow property types inside a model to other model files (shallow)."""
        if depth > 2:
            return []
        content = self._read(model_file)
        # public SomeType Prop { get; set; }
        prop_types = re.findall(
            r"""public\s+(?:virtual\s+)?(?:I?List<|IEnumerable<|ICollection<)?(\w+)>?\s+\w+\s*\{\s*get;""",
            content,
        )
        builtins = {
            "string", "int", "long", "bool", "decimal", "double", "float",
            "DateTime", "Guid", "byte", "short", "object", "var", "string",
        }
        nested: list[str] = []
        for t in set(prop_types):
            if t in builtins or t == root_type:
                continue
            f = self._find_cs_class(t)
            if f and f != model_file and f not in nested:
                nested.append(f)
                nested.extend(self._find_nested_models(f, root_type, depth + 1))
        return list(dict.fromkeys(nested))

    # ── Controller resolution ────────────────────────────────────────────────────

    def _find_controller_for_view(self, view_rel: str) -> tuple[str | None, str | None, UnresolvedLink | None]:
        """
        Find which controller action renders this view.
        Handles explicit return View("Name"), return View(model), return View().
        """
        view_base = os.path.basename(view_rel)[:-len(".cshtml")]      # "Edit"
        # Controller name often matches the view's parent folder: Views/User/ → UserController
        folder = os.path.basename(os.path.dirname(view_rel))          # "User"
        expected_controller = f"{folder}Controller"

        # Find candidate controller files
        candidates = [rel for rel in self._cs if os.path.basename(rel).lower().startswith(expected_controller.lower())]
        if not candidates:
            # broaden: any controller that references this view name
            candidates = [rel for rel in self._cs if "controller" in os.path.basename(rel).lower()]

        for ctrl_rel in candidates:
            content = self._read(ctrl_rel)

            # Dynamic view name → flag
            if _RETURN_VIEW_DYNAMIC.search(content):
                # still try explicit matches below, but note the risk
                pass

            # Explicit return View("ViewName") — with or without extra args
            for vn in _RETURN_VIEW_STR.findall(content):
                if vn.lower() == view_base.lower():
                    action = self._action_containing_viewname(content, vn)
                    return ctrl_rel, action, None

            # Convention: action name == view name, with bare return View() or return View(model)
            action = self._find_action_named(content, view_base)
            if action:
                return ctrl_rel, action, None

        return None, None, UnresolvedLink(
            kind="view", reference=view_rel, source_file=expected_controller,
            reason="no controller action resolves to this view (may use dynamic view name)",
        )

    def _find_action_named(self, controller_content: str, action_name: str) -> str | None:
        for m in _ACTION_METHOD.finditer(controller_content):
            if m.group(1).lower() == action_name.lower():
                return m.group(1)
        return None

    def _action_containing(self, content: str, snippet: str) -> str | None:
        idx = content.find(snippet)
        if idx == -1:
            return None
        # Walk backwards to the nearest method signature
        before = content[:idx]
        matches = list(_ACTION_METHOD.finditer(before))
        return matches[-1].group(1) if matches else None

    def _action_containing_viewname(self, content: str, view_name: str) -> str | None:
        """
        Find the action method whose body contains return View("<view_name>"...),
        tolerating extra arguments like View("Edit", model).
        """
        # Match return View("Edit" with optional further args
        pat = re.compile(rf"""return\s+View\(\s*["']{re.escape(view_name)}["']""")
        m = pat.search(content)
        if not m:
            return None
        before = content[:m.start()]
        matches = list(_ACTION_METHOD.finditer(before))
        return matches[-1].group(1) if matches else None

    # ── Public: resolve one page ──────────────────────────────────────────────

    def resolve_page(self, entry_view_rel: str) -> PageCluster:
        """Resolve a full page cluster starting from an entry .cshtml view."""
        if not self._scanned:
            self.scan()

        # Normalise the path to match the index
        entry_view_rel = entry_view_rel.replace("\\", "/")
        if entry_view_rel not in self._cshtml:
            # try to locate by basename
            match = self._find_view_file(entry_view_rel, "")
            if match:
                entry_view_rel = match
            else:
                raise FileNotFoundError(f"Entry view not found in repo: {entry_view_rel}")

        page_name = os.path.splitext(entry_view_rel)[0].replace("Views/", "").replace("/", "/")

        partials, p_unresolved = self._resolve_partials(entry_view_rel, seen=set())
        layout, l_unresolved = self._resolve_layout(entry_view_rel)
        model, nested, m_unresolved = self._resolve_model(entry_view_rel)
        controller, action, c_unresolved = self._find_controller_for_view(entry_view_rel)

        unresolved = list(p_unresolved)
        for u in (l_unresolved, m_unresolved, c_unresolved):
            if u:
                unresolved.append(u)

        cluster = PageCluster(
            page_name=page_name,
            entry_view=entry_view_rel,
            partials=partials,
            layout=layout,
            controller=controller,
            controller_action=action,
            model=model,
            nested_models=nested,
            unresolved=unresolved,
        )
        logger.info("Resolved page: %s", cluster.summary())
        return cluster

    # ── Public: map the whole repo ─────────────────────────────────────────────

    def map_repo(self, skip_partials_as_entry: bool = True) -> list[PageCluster]:
        """
        Resolve every page in the repo.
        Treats top-level views (not starting with '_') as entry points;
        partials (starting with '_') are skipped as entry points since they're
        included by other views.
        """
        if not self._scanned:
            self.scan()

        clusters: list[PageCluster] = []
        for rel in sorted(self._cshtml):
            base = os.path.basename(rel)
            if skip_partials_as_entry and base.startswith("_"):
                continue   # partial / layout — not a page on its own
            try:
                clusters.append(self.resolve_page(rel))
            except Exception as e:
                logger.warning("Failed to resolve %s: %s", rel, e)
        logger.info("Mapped %d pages from repo", len(clusters))
        return clusters

    # ── Reporting ──────────────────────────────────────────────────────────────

    def collect_unresolved(self, clusters: list[PageCluster]) -> list[UnresolvedLink]:
        """Flatten all unresolved links across clusters for review."""
        out: list[UnresolvedLink] = []
        for c in clusters:
            out.extend(c.unresolved)
        return out

    def unresolved_breakdown(self, clusters: list[PageCluster]) -> dict:
        """
        Summarise unresolved items by kind and by reason, so you can see which
        category dominates and target the biggest bucket.

        Returns:
            {
              "total": int,
              "by_kind":   {kind: count, ...},
              "by_reason": {reason: count, ...},
              "pages_with_unresolved": int,
              "pages_total": int,
            }
        """
        from collections import defaultdict
        by_kind: dict[str, int] = defaultdict(int)
        by_reason: dict[str, int] = defaultdict(int)
        pages_with = 0
        total = 0

        for c in clusters:
            if c.unresolved:
                pages_with += 1
            for u in c.unresolved:
                by_kind[u.kind] += 1
                by_reason[u.reason] += 1
                total += 1

        return {
            "total": total,
            "by_kind": dict(sorted(by_kind.items(), key=lambda x: -x[1])),
            "by_reason": dict(sorted(by_reason.items(), key=lambda x: -x[1])),
            "pages_with_unresolved": pages_with,
            "pages_total": len(clusters),
        }
