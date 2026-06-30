"""
passes/csharp_parser.py
───────────────────────
A lightweight, regex-based C# source parser — Python-only, no Roslyn.

It extracts just enough structure from a .cs file for the model and controller
passes to do their work and to compute dependencies for the topological sort:

  parse_class()   → ClassInfo: name, base types, properties (name + type)
  parse_controller() → ControllerInfo: name, actions, referenced model types

This is intentionally NOT a full C# parser. It handles the common shapes seen
in MVC model and controller files (auto-properties, generics, attributes,
inheritance). Anything it can't parse degrades gracefully — the LLM pass still
gets the raw source as a fallback, so parsing gaps reduce dependency accuracy
but never block migration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# C# primitive / framework types that are never "model dependencies".
BUILTINS = {
    "string", "int", "long", "short", "byte", "bool", "boolean", "decimal",
    "double", "float", "char", "object", "void", "var", "dynamic",
    "datetime", "datetimeoffset", "timespan", "guid", "uri",
    "int32", "int64", "uint", "ulong", "sbyte", "nint", "nuint",
}

# Collection wrappers we unwrap to find the inner element type.
_COLLECTIONS = {
    "list", "ilist", "ienumerable", "icollection", "collection",
    "iquerable", "iqueryable", "array", "hashset", "iset", "ireadonlylist",
    "ireadonlycollection", "observablecollection", "task",
}


@dataclass
class PropertyInfo:
    name: str
    type_raw: str            # as written, e.g. "List<AddressModel>"
    type_core: str           # unwrapped meaningful type, e.g. "AddressModel"
    is_collection: bool
    nullable: bool


@dataclass
class ClassInfo:
    name: str
    base_types: list[str] = field(default_factory=list)
    properties: list[PropertyInfo] = field(default_factory=list)
    raw: str = ""

    def dependency_types(self) -> list[str]:
        """Non-builtin types this class references (for the topo sort)."""
        deps: list[str] = []
        for p in self.properties:
            t = p.type_core
            if t and t.lower() not in BUILTINS and t not in deps:
                deps.append(t)
        for b in self.base_types:
            if b and b.lower() not in BUILTINS and b not in deps:
                deps.append(b)
        return deps


@dataclass
class ActionInfo:
    name: str
    return_views: list[str] = field(default_factory=list)   # explicit View("X")
    model_types: list[str] = field(default_factory=list)     # referenced types
    has_dynamic_view: bool = False


@dataclass
class ControllerInfo:
    name: str
    actions: list[ActionInfo] = field(default_factory=list)
    referenced_types: list[str] = field(default_factory=list)
    raw: str = ""


# ── Type helpers ──────────────────────────────────────────────────────────────

def unwrap_type(type_raw: str) -> tuple[str, bool]:
    """
    Reduce a written type to its meaningful core type + whether it's a collection.
      List<AddressModel>        → ("AddressModel", True)
      IEnumerable<UserModel>    → ("UserModel", True)
      Dictionary<string,Order>  → ("Order", True)
      MyApp.Models.UserModel    → ("UserModel", False)
      string                    → ("string", False)
    """
    t = type_raw.strip().rstrip("?").strip()
    is_collection = False

    # Arrays: Foo[]
    if t.endswith("[]"):
        is_collection = True
        t = t[:-2].strip()

    # Generic wrappers
    while "<" in t and t.endswith(">"):
        outer = t[: t.index("<")].split(".")[-1].strip().lower()
        inner = t[t.index("<") + 1 : -1]
        if outer in _COLLECTIONS or outer.startswith("dictionary") or outer.startswith("idictionary"):
            is_collection = True
        # take the last type argument (Dictionary<K,V> → V; List<T> → T)
        t = inner.split(",")[-1].strip()

    core = t.split(".")[-1].strip().rstrip("?")
    return core, is_collection


# ── Class parsing ─────────────────────────────────────────────────────────────

_CLASS_RE = re.compile(
    r"\b(?:public\s+|internal\s+|abstract\s+|sealed\s+|partial\s+)*class\s+"
    r"(\w+)\s*(?:<[^>]+>)?\s*(?::\s*([^{]+))?\{",
    re.MULTILINE,
)

# auto-property: public Type Name { get; set; }
_PROP_RE = re.compile(
    r"public\s+(?:virtual\s+|override\s+|required\s+)*"
    r"([\w\.\<\>\[\],\s\?]+?)\s+(\w+)\s*\{\s*get;",
    re.MULTILINE,
)


def parse_class(source: str, want_class: str | None = None) -> ClassInfo | None:
    """
    Parse the first class (or the named class) from C# source.
    Returns ClassInfo or None if no class found.
    """
    for m in _CLASS_RE.finditer(source):
        name = m.group(1)
        if want_class and name != want_class:
            continue

        bases = []
        if m.group(2):
            bases = [b.strip().split(".")[-1].split("<")[0]
                     for b in m.group(2).split(",") if b.strip()]

        # find the class body bounds (from the opening brace) to scope properties
        body = _extract_body(source, m.end() - 1)
        props = []
        for pm in _PROP_RE.finditer(body):
            type_raw = pm.group(1).strip()
            pname = pm.group(2)
            core, is_coll = unwrap_type(type_raw)
            props.append(PropertyInfo(
                name=pname,
                type_raw=type_raw,
                type_core=core,
                is_collection=is_coll,
                nullable=type_raw.rstrip().endswith("?"),
            ))

        return ClassInfo(name=name, base_types=bases, properties=props, raw=source)
    return None


def _extract_body(source: str, brace_pos: int) -> str:
    """Return the substring of the balanced { } block starting at brace_pos."""
    depth = 0
    for i in range(brace_pos, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[brace_pos : i + 1]
    return source[brace_pos:]


# ── Controller parsing ────────────────────────────────────────────────────────

_ACTION_RE = re.compile(
    r"public\s+(?:async\s+)?(?:virtual\s+)?[\w<>\[\]\.\?]+\s+(\w+)\s*\([^)]*\)",
    re.MULTILINE,
)
_RETURN_VIEW_STR = re.compile(r"""return\s+(?:View|PartialView)\(\s*["']([^"']+)["']""")
_RETURN_VIEW_DYNAMIC = re.compile(r"""return\s+(?:View|PartialView)\(\s*[A-Za-z_]\w*\s*[,)]""")
_TYPE_REF = re.compile(r"\b([A-Z]\w+(?:Model|Dto|ViewModel|Entity|Vm))\b")


def parse_controller(source: str, want_class: str | None = None) -> ControllerInfo | None:
    """Parse a controller .cs file into actions + referenced model types."""
    cls = None
    for m in _CLASS_RE.finditer(source):
        if m.group(1).endswith("Controller"):
            if want_class and m.group(1) != want_class:
                continue
            cls = m
            break
    if not cls:
        return None

    name = cls.group(1)
    body = _extract_body(source, cls.end() - 1)

    actions: list[ActionInfo] = []
    # Split into rough method chunks by action signatures, then scan each.
    matches = list(_ACTION_RE.finditer(body))
    for i, am in enumerate(matches):
        action_name = am.group(1)
        start = am.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[start:end]

        views = _RETURN_VIEW_STR.findall(chunk)
        dynamic = bool(_RETURN_VIEW_DYNAMIC.search(chunk)) and not views
        model_types = sorted(set(_TYPE_REF.findall(chunk)))

        actions.append(ActionInfo(
            name=action_name,
            return_views=views or ([action_name] if not dynamic else []),
            model_types=model_types,
            has_dynamic_view=dynamic,
        ))

    referenced = sorted(set(_TYPE_REF.findall(body)))
    return ControllerInfo(name=name, actions=actions,
                          referenced_types=referenced, raw=source)
