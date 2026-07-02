#!/usr/bin/env python3
"""Phase 0: Codebase Profiler.

Run this BEFORE writing/adapting any migration parser. It answers:
  1. What kind of .NET codebase is this? (MVC5 / Core / WebForms mix,
     frameworks, packages, JS-side frameworks)
  2. How is it structured? (class taxonomy, base-class hierarchies,
     custom conventions, Areas, custom view engines / HtmlHelpers)
  3. How does data reach the UI? (EF entities vs DTOs vs ViewModels,
     stored procs vs LINQ, AutoMapper vs manual mapping, ViewBag vs
     strongly-typed models, AJAX/JSON endpoints)

Outputs:
  profile.json            — machine-readable evidence
  PROFILE.md              — human-readable report + parser recommendations
  llm_characterization/   — prompt + sampled source files for an LLM pass
                            (wire to your internal gateway)

Usage:
  python profiler.py /path/to/solution [-o profile_out]
"""
import argparse
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import tree_sitter_c_sharp as tscs
from tree_sitter import Language, Parser

CSHARP = Language(tscs.language())
_parser = Parser(CSHARP)

SKIP_DIRS = {"bin", "obj", "node_modules", "packages", ".git", ".vs",
             "TestResults", "wwwroot/lib"}

# ---------- package → technology mapping ----------
PACKAGE_SIGNALS = {
    "EntityFramework": "EF6 (classic Entity Framework)",
    "Microsoft.EntityFrameworkCore": "EF Core",
    "Dapper": "Dapper micro-ORM",
    "System.Data.SqlClient": "raw ADO.NET SQL",
    "Microsoft.Data.SqlClient": "raw ADO.NET SQL (modern)",
    "AutoMapper": "AutoMapper object mapping",
    "Unity": "Unity DI container", "Autofac": "Autofac DI",
    "Ninject": "Ninject DI", "SimpleInjector": "SimpleInjector DI",
    "Microsoft.AspNet.Mvc": "ASP.NET MVC 5 (classic)",
    "Microsoft.AspNetCore": "ASP.NET Core",
    "Microsoft.AspNet.WebPages": "classic Razor WebPages",
    "Microsoft.AspNet.SignalR": "SignalR (classic)",
    "Newtonsoft.Json": "JSON.NET serialization",
    "jQuery": "jQuery (NuGet-managed)",
    "knockoutjs": "Knockout.js", "AngularJS": "AngularJS 1.x",
    "Telerik": "Telerik/Kendo UI controls",
    "Kendo": "Kendo UI controls",
    "DevExpress": "DevExpress controls",
    "Moq": "(test) Moq", "xunit": "(test) xUnit", "NUnit": "(test) NUnit",
}

JS_FRAMEWORK_HINTS = {
    "knockout": "Knockout.js", "angular": "AngularJS/Angular",
    "react": "React", "vue": "Vue", "backbone": "Backbone",
    "kendo": "Kendo UI", "bootstrap": "Bootstrap",
    "jquery.validate": "jQuery unobtrusive validation",
    "signalr": "SignalR client",
}

# ---------- data-access regexes ----------
RE_STORED_PROC = re.compile(
    r'CommandType\.StoredProcedure|Database\.SqlQuery|ExecuteSqlCommand'
    r'|FromSqlRaw|FromSqlInterpolated|"\s*EXEC(?:UTE)?\s+', re.IGNORECASE)
RE_PROC_NAME = re.compile(
    r'(?:CommandText\s*=\s*|SqlQuery[^"]*|Query[^"]*\(\s*)"'
    r'(?:EXEC(?:UTE)?\s+)?(\[?\w+\]?\.\[?\w+\]?|\w+_\w+)"', re.IGNORECASE)
RE_RAW_SQL = re.compile(r'"\s*SELECT\s+.+?\s+FROM\s+', re.IGNORECASE | re.DOTALL)
RE_DAPPER = re.compile(r'\.(Query|QueryAsync|QueryFirst\w*|Execute)\s*<')
RE_AUTOMAPPER = re.compile(r'CreateMap\s*<\s*([\w\.]+)\s*,\s*([\w\.]+)\s*>')
RE_HTTPCLIENT = re.compile(r'\b(HttpClient|WebClient|RestClient)\b')
RE_LINQ_EF = re.compile(r'\.(Where|Select|Include|FirstOrDefault|ToList)\s*\(')
RE_VIEW_ENGINE = re.compile(r'ViewEngines\.Engines|IViewLocationExpander|RazorViewEngine')
RE_JSON_RETURN = re.compile(r'return\s+Json\s*\(')
RE_VIEWBAG_WRITE = re.compile(r'ViewBag\.(\w+)\s*=')
RE_SESSION = re.compile(r'\bSession\[\s*"([^"]+)"')


def _text(node, src):
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk(node, kind):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            yield n
        stack.extend(reversed(n.children))


def iter_files(root: Path, suffix: str):
    for p in root.rglob(f"*{suffix}"):
        if not any(part in SKIP_DIRS for part in p.parts):
            yield p


# ==================== 1. PROJECT INVENTORY ====================

def profile_projects(root: Path):
    projects = []
    for csproj in iter_files(root, ".csproj"):
        info = {"path": str(csproj.relative_to(root)), "framework": None,
                "sdk_style": False, "packages": [], "project_type": "unknown"}
        try:
            tree = ET.parse(csproj)
            r = tree.getroot()
            ns = {"m": "http://schemas.microsoft.com/developer/msbuild/2003"} \
                if r.tag.startswith("{") else {}
            info["sdk_style"] = "Sdk" in r.attrib
            for tag in ("TargetFramework", "TargetFrameworks",
                        "TargetFrameworkVersion"):
                el = r.find(f".//{{*}}{tag}") if ns else r.find(f".//{tag}")
                if el is not None and el.text:
                    info["framework"] = el.text
                    break
            for pr in (r.findall(".//{*}PackageReference") if ns
                       else r.findall(".//PackageReference")):
                name = pr.get("Include")
                if name:
                    info["packages"].append(name)
            guids = r.find(".//{*}ProjectTypeGuids") if ns \
                else r.find(".//ProjectTypeGuids")
            if guids is not None and guids.text:
                if "349C5851" in guids.text.upper():
                    info["project_type"] = "web"
        except Exception as e:
            info["parse_error"] = str(e)
        # packages.config sibling (classic NuGet)
        pkg_cfg = csproj.parent / "packages.config"
        if pkg_cfg.exists():
            info["packages"] += re.findall(r'id="([^"]+)"',
                                           pkg_cfg.read_text(errors="replace"))
        projects.append(info)
    return projects


def detect_technologies(projects):
    found = {}
    for proj in projects:
        for pkg in proj["packages"]:
            for prefix, label in PACKAGE_SIGNALS.items():
                if pkg.startswith(prefix):
                    found.setdefault(label, []).append(proj["path"])
    return {k: sorted(set(v)) for k, v in found.items()}


# ==================== 2. FILE CENSUS ====================

def file_census(root: Path):
    ext_count = Counter()
    js_frameworks = Counter()
    webforms = []
    for p in root.rglob("*"):
        if p.is_dir() or any(part in SKIP_DIRS for part in p.parts):
            continue
        ext_count[p.suffix.lower()] += 1
        if p.suffix.lower() in (".aspx", ".ascx", ".master", ".asmx"):
            webforms.append(str(p.relative_to(root)))
        if p.suffix.lower() == ".js":
            low = p.name.lower()
            for hint, label in JS_FRAMEWORK_HINTS.items():
                if hint in low:
                    js_frameworks[label] += 1
    return {"extensions": dict(ext_count.most_common(25)),
            "webforms_files": webforms[:50],
            "webforms_count": len(webforms),
            "js_frameworks_by_filename": dict(js_frameworks)}


# ==================== 3. CLASS TAXONOMY ====================

def classify_class(name, bases, props_only, has_dbsets, attrs, namespace):
    ns = namespace.lower()
    if has_dbsets or any("DbContext" in b for b in bases):
        return "dbcontext"
    if name.endswith("Controller") or any("Controller" in b for b in bases):
        return "controller"
    if name.endswith(("Repository", "Repo")) or "repositor" in ns:
        return "repository"
    if name.endswith(("Service", "Manager", "Provider", "Handler")):
        return "service"
    if name.endswith(("ViewModel", "Vm")) or "viewmodel" in ns:
        return "viewmodel"
    if name.endswith(("Dto", "DTO", "Request", "Response")) or "dto" in ns:
        return "dto"
    if any(a in ("Table",) for a in attrs) or "entit" in ns or "domain" in ns:
        return "entity"
    if props_only:
        return "poco_unclassified"
    return "other"


def profile_classes(root: Path):
    taxonomy = defaultdict(list)
    base_edges = Counter()            # (class, base) frequency by base
    attr_census = Counter()
    html_helper_exts = []
    dbcontexts = {}                   # context → [entity types from DbSet<T>]
    suffix_census = Counter()

    for p in iter_files(root, ".cs"):
        try:
            src = p.read_bytes()
            tree = _parser.parse(src)
        except Exception:
            continue
        rel = str(p.relative_to(root))

        ns_nodes = list(_walk(tree.root_node, "namespace_declaration")) + \
                   list(_walk(tree.root_node, "file_scoped_namespace_declaration"))
        namespace = _text(ns_nodes[0].child_by_field_name("name"), src) \
            if ns_nodes and ns_nodes[0].child_by_field_name("name") else ""

        for cls in _walk(tree.root_node, "class_declaration"):
            nm = cls.child_by_field_name("name")
            if nm is None:
                continue
            name = _text(nm, src)
            m = re.search(r'[A-Z][a-z]+$', name)
            if m:
                suffix_census[m.group(0)] += 1

            bases = []
            for c in cls.children:
                if c.type == "base_list":
                    bases = [t.strip().split("<")[0] for t in
                             _text(c, src).lstrip(":").split(",")]
            for b in bases:
                base_edges[b] += 1

            attrs = []
            for c in cls.children:
                if c.type == "attribute_list":
                    attrs += [ _text(a.child_by_field_name("name"), src)
                               for a in _walk(c, "attribute")
                               if a.child_by_field_name("name") ]
            for a in attrs:
                attr_census[a] += 1

            body = cls.child_by_field_name("body")
            body_text = _text(body, src) if body else ""
            methods = list(_walk(body, "method_declaration")) if body else []
            props = list(_walk(body, "property_declaration")) if body else []
            props_only = bool(props) and not methods

            dbsets = re.findall(r'DbSet<\s*([\w\.]+)\s*>', body_text)
            if dbsets:
                dbcontexts[name] = sorted(set(dbsets))

            # HtmlHelper extension methods (custom helpers = custom parser rules)
            for meth in methods:
                params = meth.child_by_field_name("parameters")
                if params is not None and "this HtmlHelper" in _text(params, src):
                    html_helper_exts.append(
                        {"file": rel, "class": name,
                         "method": _text(meth.child_by_field_name("name"), src)})

            kind = classify_class(name, bases, props_only, bool(dbsets),
                                  attrs, namespace)
            taxonomy[kind].append({"name": name, "file": rel,
                                   "namespace": namespace, "bases": bases})

    return {
        "taxonomy_counts": {k: len(v) for k, v in taxonomy.items()},
        "taxonomy": {k: v[:200] for k, v in taxonomy.items()},
        "common_base_classes": dict(base_edges.most_common(20)),
        "attribute_census": dict(attr_census.most_common(25)),
        "name_suffix_census": dict(suffix_census.most_common(20)),
        "custom_html_helpers": html_helper_exts,
        "dbcontexts": dbcontexts,
    }


# ==================== 4. DATA-ACCESS & UI-DATA-FLOW ====================

def profile_data_flow(root: Path):
    signals = Counter()
    stored_procs = set()
    automapper_maps = []
    files_with = defaultdict(list)
    viewbag_write_files = Counter()
    session_keys = Counter()

    for p in iter_files(root, ".cs"):
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        rel = str(p.relative_to(root))
        checks = [
            ("stored_procedures", RE_STORED_PROC), ("raw_sql", RE_RAW_SQL),
            ("dapper", RE_DAPPER), ("ef_linq", RE_LINQ_EF),
            ("http_calls_to_other_services", RE_HTTPCLIENT),
            ("custom_view_engine", RE_VIEW_ENGINE),
            ("json_ajax_endpoints", RE_JSON_RETURN),
        ]
        for label, rx in checks:
            n = len(rx.findall(text))
            if n:
                signals[label] += n
                if len(files_with[label]) < 25:
                    files_with[label].append(rel)
        stored_procs |= set(RE_PROC_NAME.findall(text))
        automapper_maps += [{"from": a, "to": b, "file": rel}
                            for a, b in RE_AUTOMAPPER.findall(text)]
        n_vb = len(RE_VIEWBAG_WRITE.findall(text))
        if n_vb:
            viewbag_write_files[rel] = n_vb
        for k in RE_SESSION.findall(text):
            session_keys[k] += 1

    # Razor-side: strongly typed vs ViewBag-driven views
    typed, untyped, total = 0, 0, 0
    for p in iter_files(root, ".cshtml"):
        total += 1
        try:
            t = p.read_text(errors="replace")
        except Exception:
            continue
        if re.search(r'^\s*@model\s+', t, re.MULTILINE):
            typed += 1
        else:
            untyped += 1

    return {
        "signal_counts": dict(signals),
        "example_files": {k: v for k, v in files_with.items()},
        "stored_proc_names": sorted(stored_procs)[:100],
        "automapper_maps": automapper_maps[:200],
        "viewbag_heaviest_files": dict(
            Counter(viewbag_write_files).most_common(15)),
        "session_keys": dict(session_keys.most_common(25)),
        "views": {"total": total, "strongly_typed": typed,
                  "viewbag_or_dynamic": untyped},
    }


# ==================== 5. LLM CHARACTERIZATION PACKAGE ====================

def sample_for_llm(root: Path, classes, out_dir: Path, max_bytes=12000):
    """Pick representative files per category for the LLM narrative pass."""
    out_dir.mkdir(parents=True, exist_ok=True)
    picks = []
    for kind in ("controller", "service", "repository", "viewmodel",
                 "dto", "entity", "dbcontext"):
        items = classes["taxonomy"].get(kind, [])
        for item in items[:2]:  # 2 samples per category
            picks.append((kind, item["file"]))
    # plus the 2 heaviest views and a layout
    views = sorted(iter_files(root, ".cshtml"),
                   key=lambda p: p.stat().st_size, reverse=True)[:2]
    picks += [("view", str(v.relative_to(root))) for v in views]
    layouts = [p for p in iter_files(root, ".cshtml")
               if "_Layout" in p.name]
    picks += [("layout", str(l.relative_to(root))) for l in layouts[:1]]

    sources = {}
    for kind, rel in picks:
        try:
            txt = (root / rel).read_text(errors="replace")[:max_bytes]
            sources[rel] = {"kind": kind, "content": txt}
        except Exception:
            pass
    (out_dir / "samples.json").write_text(json.dumps(sources, indent=2))
    return list(sources.keys())


PROMPT_TEMPLATE = """You are analyzing a legacy .NET codebase to plan a \
UI migration (Razor -> React; backend APIs stay .NET). You are given \
(1) a machine-generated evidence profile and (2) representative source \
files. Produce a CODEBASE CHARACTERIZATION with these sections:

1. ARCHITECTURE: what pattern is this actually (textbook MVC, MVC + \
service layer, N-tier, WebForms hybrid, etc.)? Cite evidence.
2. DATA PATH TO UI: trace how data flows DB -> entity -> \
mapping -> viewmodel/ViewBag -> view. Name the concrete classes involved.
3. CUSTOM CONVENTIONS: base controllers, custom HtmlHelpers, custom view \
engines, naming conventions, Areas, anything nonstandard a parser must handle.
4. UI STATE & BEHAVIOR: how much rendering depends on inline JS/jQuery, \
Session, TempData, ViewBag vs strongly-typed models.
5. PARSER SPEC: given all of the above, specify exactly what a dependency \
indexer must extract for THIS codebase (list of constructs + resolution \
rules), and which constructs static analysis will MISS (needing runtime \
capture).
6. RISK REGISTER: top 10 things most likely to be silently dropped during \
migration.

Respond in markdown. Be specific; name files and classes from the evidence.

=== EVIDENCE PROFILE ===
{profile}

=== SAMPLE FILES ===
{samples}
"""


# ==================== MAIN ====================

def render_report(profile):
    p = profile
    lines = ["# Codebase Profile", ""]
    lines.append("## Projects")
    for pr in p["projects"]:
        lines.append(f"- `{pr['path']}` — {pr['framework'] or '?'} "
                     f"({'SDK-style' if pr['sdk_style'] else 'legacy csproj'}), "
                     f"{len(pr['packages'])} packages")
    lines.append("")
    lines.append("## Detected technologies")
    for tech, projs in sorted(p["technologies"].items()):
        lines.append(f"- {tech}")
    if p["files"]["webforms_count"]:
        lines.append(f"- WARNING: {p['files']['webforms_count']} WebForms "
                     "files (.aspx/.ascx) — Razor parser alone is insufficient")
    lines.append("")
    lines.append("## Class taxonomy")
    for k, n in sorted(p["classes"]["taxonomy_counts"].items(),
                       key=lambda kv: -kv[1]):
        lines.append(f"- {k}: {n}")
    lines.append("")
    lines.append("## Views: how data arrives")
    v = p["data_flow"]["views"]
    lines.append(f"- {v['strongly_typed']}/{v['total']} views strongly typed "
                 f"(@model); {v['viewbag_or_dynamic']} ViewBag/dynamic-driven")
    lines.append(f"- Data-access signals: "
                 f"{json.dumps(p['data_flow']['signal_counts'])}")
    if p["data_flow"]["stored_proc_names"]:
        lines.append(f"- Stored procs referenced: "
                     f"{len(p['data_flow']['stored_proc_names'])}")
    if p["classes"]["custom_html_helpers"]:
        lines.append(f"- Custom HtmlHelper extensions: "
                     f"{len(p['classes']['custom_html_helpers'])} "
                     "(parser must expand these)")
    lines.append("")
    lines.append("## Parser recommendations (heuristic)")
    recs = []
    sc = p["data_flow"]["signal_counts"]
    if p["files"]["webforms_count"]:
        recs.append("Add a WebForms (.aspx/.ascx) parser path — Razor-only "
                    "indexing will skip these pages entirely.")
    if v["viewbag_or_dynamic"] > v["strongly_typed"]:
        recs.append("Codebase is ViewBag-heavy: prioritize base-controller/"
                    "filter ViewBag tracing over @model type extraction.")
    else:
        recs.append("Codebase is strongly-typed: prioritize ViewModel -> "
                    "TypeScript interface generation from @model types.")
    if sc.get("stored_procedures") or sc.get("raw_sql"):
        recs.append("Data shapes come from SQL/stored procs, not just C# "
                    "types — DTO property lists may not reflect actual "
                    "payloads; verify with runtime capture (Layer 2).")
    if sc.get("json_ajax_endpoints"):
        recs.append(f"{sc['json_ajax_endpoints']} Json(...) returns found: "
                    "index AJAX endpoints as first-class routes; the React "
                    "app will call these directly.")
    if p["classes"]["custom_html_helpers"]:
        recs.append("Custom HtmlHelpers detected: parser needs an expansion "
                    "table (helper -> emitted HTML) or their React "
                    "component equivalents.")
    if sc.get("custom_view_engine"):
        recs.append("Custom view engine/location expander present: default "
                    "Views/{Controller}/{View} resolution WILL be wrong; "
                    "extract custom search paths first.")
    for r in recs:
        lines.append(f"- {r}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("profile_out"))
    args = ap.parse_args()
    root = args.root.resolve()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    print(f"Profiling {root} ...")
    projects = profile_projects(root)
    print(f"  {len(projects)} projects")
    profile = {
        "projects": projects,
        "technologies": detect_technologies(projects),
        "files": file_census(root),
        "classes": profile_classes(root),
        "data_flow": profile_data_flow(root),
    }
    (out / "profile.json").write_text(json.dumps(profile, indent=2))

    report = render_report(profile)
    (out / "PROFILE.md").write_text(report)

    llm_dir = out / "llm_characterization"
    sampled = sample_for_llm(root, profile["classes"], llm_dir)
    slim = dict(profile)
    slim["classes"] = {k: v for k, v in profile["classes"].items()
                       if k != "taxonomy"}
    prompt = PROMPT_TEMPLATE.format(
        profile=json.dumps(slim, indent=2)[:30000],
        samples=json.dumps(json.loads(
            (llm_dir / "samples.json").read_text()), indent=2)[:60000])
    (llm_dir / "prompt.md").write_text(prompt)

    print(f"\nWrote {out}/profile.json, PROFILE.md, "
          f"llm_characterization/ ({len(sampled)} sample files)")
    print("\n" + report)


if __name__ == "__main__":
    main()