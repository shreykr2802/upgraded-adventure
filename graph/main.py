#!/usr/bin/env python3
"""Razor dependency graph indexer — Layer 1.

Usage:
    python main.py index <project_root> [-o out_dir]
    python main.py closure <project_root> <Controller/Action> [-o out_dir]

`index` writes:
    graph.json      — all views, actions, routes, base-class writes
    closures.json   — per-route render closure (the context package the
                      migration agent consumes)
    report.txt      — human-readable summary + unresolved refs + gaps
"""
import argparse
import json
import sys
from pathlib import Path

from razor_parser import scan_views
from csharp_parser import scan_controllers
from graph_builder import Graph


def build(root: Path):
    print(f"Indexing {root} ...")
    views = scan_views(root)
    print(f"  {len(views)} .cshtml files")
    actions, base_writes = scan_controllers(root)
    print(f"  {len(actions)} controller actions "
          f"({len(base_writes)} classes with base-level ViewBag/ViewData writes)")
    g = Graph(views, actions, base_writes)
    routes = g.build_routes()
    print(f"  {len(routes)} routes")
    return g, routes


def cmd_index(root: Path, out: Path):
    g, routes = build(root)
    out.mkdir(parents=True, exist_ok=True)

    graph = {
        "views": {p: v.to_dict() for p, v in g.views.items()},
        "actions": [a.to_dict() for a in g.actions],
        "routes": [r.to_dict() for r in routes],
        "base_class_writes": g.base_writes,
    }
    (out / "graph.json").write_text(json.dumps(graph, indent=2))

    closures = {}
    for act in g.actions:
        route = next((r for r in routes
                      if r.controller == act.controller
                      and r.action == act.action), None)
        if route and route.view_path:
            key = f"{act.controller}/{act.action}"
            closures[key] = g.render_closure(act, route.view_path)
    (out / "closures.json").write_text(json.dumps(closures, indent=2))

    # human-readable report
    lines = [f"# Index report", "",
             f"Views: {len(g.views)}   Actions: {len(g.actions)}   "
             f"Routes with resolved views: {len(closures)}", ""]
    by_cx = {"simple": [], "medium": [], "complex": []}
    for key, c in closures.items():
        by_cx[c["complexity"]].append(key)
    for cx in ("simple", "medium", "complex"):
        lines.append(f"{cx.upper()} ({len(by_cx[cx])}): "
                     + ", ".join(sorted(by_cx[cx])))
    lines.append("")
    any_gap = False
    for key, c in closures.items():
        gaps = {k: v for k, v in c["gaps"].items() if v}
        if gaps:
            any_gap = True
            lines.append(f"[GAPS] {key}: {json.dumps(gaps)}")
    if not any_gap:
        lines.append("No ViewBag/ViewData/section gaps detected.")
    if g.unresolved:
        lines.append("")
        lines.append(f"UNRESOLVED refs ({len(set(g.unresolved))}): "
                     + ", ".join(sorted(set(g.unresolved))))
    (out / "report.txt").write_text("\n".join(lines))

    print(f"\nWrote {out}/graph.json, closures.json, report.txt")
    print("\n" + (out / "report.txt").read_text())


def cmd_closure(root: Path, key: str, out: Path):
    g, routes = build(root)
    ctrl, action = key.split("/", 1)
    act = next((a for a in g.actions
                if a.controller == ctrl and a.action == action), None)
    if act is None:
        sys.exit(f"No action found for {key}")
    route = next((r for r in routes
                  if r.controller == ctrl and r.action == action), None)
    if route is None or route.view_path is None:
        sys.exit(f"No resolved view for {key}")
    closure = g.render_closure(act, route.view_path)

    # assemble the full context package: closure metadata + file contents
    package = {"closure": closure, "sources": {}}
    package["sources"][act.controller_file] = \
        (root / act.controller_file).read_text(errors="replace")
    for f in closure["files"]:
        package["sources"][f["path"]] = \
            (root / f["path"]).read_text(errors="replace")

    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"closure_{ctrl}_{action}.json"
    dest.write_text(json.dumps(package, indent=2))
    print(f"\nWrote {dest}")
    print(json.dumps(closure, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("index")
    p1.add_argument("root", type=Path)
    p1.add_argument("-o", "--out", type=Path, default=Path("index_out"))
    p2 = sub.add_parser("closure")
    p2.add_argument("root", type=Path)
    p2.add_argument("key", help="Controller/Action, e.g. Orders/Details")
    p2.add_argument("-o", "--out", type=Path, default=Path("index_out"))
    args = ap.parse_args()
    if args.cmd == "index":
        cmd_index(args.root.resolve(), args.out)
    else:
        cmd_closure(args.root.resolve(), args.key, args.out)
