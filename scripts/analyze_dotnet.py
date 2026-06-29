"""
scripts/analyze_dotnet.py
─────────────────────────
Runs the .NET grapher over your real C#/Razor repo and produces a reviewable
page map — showing, for each page, the full cluster of files that make it up
(view + partials + layout + controller + model), plus everything it couldn't
resolve statically (flagged for you).

This is the FIRST step of Phase A. It does no LLM work and no migration —
it just answers "what are the pages, and what files make up each one?"

USAGE:
    # Map the whole repo
    python scripts/analyze_dotnet.py --repo /path/to/dotnet-repo

    # Resolve a single page on demand
    python scripts/analyze_dotnet.py --repo /path/to/dotnet-repo --page Views/User/Edit.cshtml

    # Save the map to JSON (used by later Phase A steps)
    python scripts/analyze_dotnet.py --repo /path/to/dotnet-repo --out page_map.json
"""

import sys, os, json, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.analysis.dotnet_grapher import DotNetGrapher, PageCluster

try:
    from rich.console import Console
    from rich.table import Table
    from rich.tree import Tree
    from rich import box
    console = Console()
    HAS_RICH = True
except ImportError:
    console = None
    HAS_RICH = False


def log(msg=""):
    if console:
        console.print(msg)
    else:
        import re
        print(re.sub(r"\[.*?\]", "", str(msg)))


def cluster_to_dict(c: PageCluster) -> dict:
    return {
        "page_name": c.page_name,
        "entry_view": c.entry_view,
        "partials": c.partials,
        "layout": c.layout,
        "controller": c.controller,
        "controller_action": c.controller_action,
        "model": c.model,
        "nested_models": c.nested_models,
        "unresolved": [
            {"kind": u.kind, "reference": u.reference, "source_file": u.source_file, "reason": u.reason}
            for u in c.unresolved
        ],
    }


def print_cluster_tree(c: PageCluster):
    if not HAS_RICH:
        log(f"\nPage: {c.page_name}")
        log(f"  entry:      {c.entry_view}")
        for p in c.partials:
            log(f"  partial:    {p}")
        log(f"  layout:     {c.layout}")
        log(f"  controller: {c.controller} (action: {c.controller_action})")
        log(f"  model:      {c.model}")
        for n in c.nested_models:
            log(f"  nested:     {n}")
        for u in c.unresolved:
            log(f"  ⚠ unresolved [{u.kind}]: {u.reference} — {u.reason}")
        return

    tree = Tree(f"[bold cyan]{c.page_name}[/bold cyan]")
    tree.add(f"[white]entry view[/white]  {c.entry_view}")
    if c.partials:
        pnode = tree.add(f"[white]partials ({len(c.partials)})[/white]")
        for p in c.partials:
            pnode.add(f"[dim]{p}[/dim]")
    if c.layout:
        tree.add(f"[white]layout[/white]  {c.layout}")
    if c.controller:
        tree.add(f"[white]controller[/white]  {c.controller} [dim](action: {c.controller_action})[/dim]")
    if c.model:
        mnode = tree.add(f"[white]model[/white]  {c.model}")
        for n in c.nested_models:
            mnode.add(f"[dim]nested: {n}[/dim]")
    if c.unresolved:
        unode = tree.add(f"[yellow]⚠ unresolved ({len(c.unresolved)})[/yellow]")
        for u in c.unresolved:
            unode.add(f"[yellow][{u.kind}] {u.reference}[/yellow] [dim]— {u.reason}[/dim]")
    console.print(tree)


def main():
    parser = argparse.ArgumentParser(description="Analyse a .NET repo into page clusters")
    parser.add_argument("--repo", required=True, help="Path to the .NET repo root")
    parser.add_argument("--page", help="Resolve a single page (path to entry .cshtml)")
    parser.add_argument("--out", help="Write the full page map to this JSON file")
    parser.add_argument("--engine", default="regex", choices=["regex", "roslyn"],
                        help="Analysis engine: 'regex' (Python, no deps) or "
                             "'roslyn' (.NET sidecar, accurate, needs SDK)")
    parser.add_argument("--sln", help="Path to the .sln (roslyn engine; auto-discovered if omitted)")
    args = parser.parse_args()

    # ── Roslyn engine: delegate to the sidecar via the engine abstraction ─────
    if args.engine == "roslyn":
        from app.analysis.engine import analyze_repo, available_engines
        avail = available_engines()
        if not avail["roslyn"]:
            log("[red]Roslyn engine unavailable.[/red] Needs the .NET SDK on PATH "
                "and the sidecar project built. Falling back is not automatic — "
                "re-run with [cyan]--engine regex[/cyan] to use the Python grapher.")
            sys.exit(1)
        out = args.out or "page_map.json"
        log(f"\n[bold cyan]Analyzing with Roslyn sidecar[/bold cyan]")
        page_map = analyze_repo(args.repo, engine="roslyn", out_path=out, sln_path=args.sln)
        bd = page_map["unresolved_breakdown"]
        log(f"[green]✅ {len(page_map['pages'])} pages, "
            f"{bd['total']} unresolved[/green]")
        log(f"   Saved to {out}\n")
        return

    g = DotNetGrapher(args.repo)
    g.scan()

    log(f"\n[bold cyan].NET Repo Analysis[/bold cyan]")
    log(f"   Repo : {os.path.abspath(args.repo)}")
    log(f"   Files: {len(g._cshtml)} .cshtml, {len(g._cs)} .cs\n")

    # ── Single page mode ──────────────────────────────────────────────────────
    if args.page:
        cluster = g.resolve_page(args.page)
        print_cluster_tree(cluster)
        if args.out:
            with open(args.out, "w") as f:
                json.dump(cluster_to_dict(cluster), f, indent=2)
            log(f"\n[green]Saved to {args.out}[/green]")
        return

    # ── Whole-repo mode ───────────────────────────────────────────────────────
    clusters = g.map_repo()
    log(f"[bold]Found {len(clusters)} pages[/bold]\n")

    for c in clusters:
        print_cluster_tree(c)
        log("")

    # ── Unresolved summary ────────────────────────────────────────────────────
    unresolved = g.collect_unresolved(clusters)
    breakdown = g.unresolved_breakdown(clusters)

    if unresolved:
        # Breakdown first — shows which category dominates
        log(f"\n[bold yellow]⚠ {breakdown['total']} unresolved links across "
            f"{breakdown['pages_with_unresolved']}/{breakdown['pages_total']} pages[/bold yellow]")
        if HAS_RICH:
            bt = Table(box=box.ROUNDED, header_style="bold white on dark_red", title="By kind")
            bt.add_column("Kind", width=14)
            bt.add_column("Count", justify="right", width=8)
            for kind, count in breakdown["by_kind"].items():
                bt.add_row(kind, str(count))
            console.print(bt)

            rt = Table(box=box.ROUNDED, header_style="bold white on dark_red", title="By reason")
            rt.add_column("Reason", width=60)
            rt.add_column("Count", justify="right", width=8)
            for reason, count in breakdown["by_reason"].items():
                rt.add_row(reason, str(count))
            console.print(rt)
        else:
            log("  By kind:")
            for kind, count in breakdown["by_kind"].items():
                log(f"    {kind}: {count}")
            log("  By reason:")
            for reason, count in breakdown["by_reason"].items():
                log(f"    {reason}: {count}")

        # Then the detailed list
        log(f"\n[bold]Detail:[/bold]")
        if HAS_RICH:
            table = Table(box=box.ROUNDED, header_style="bold white on dark_red")
            table.add_column("Kind", width=10)
            table.add_column("Reference", width=35)
            table.add_column("In file", width=35)
            table.add_column("Reason", width=40)
            for u in unresolved:
                table.add_row(u.kind, u.reference, u.source_file, u.reason)
            console.print(table)
        else:
            for u in unresolved:
                log(f"  [{u.kind}] {u.reference} (in {u.source_file}) — {u.reason}")
    else:
        log("\n[green]✅ No unresolved links.[/green]")

    # ── Save ──────────────────────────────────────────────────────────────────
    if args.out:
        page_map = {
            "repo": os.path.abspath(args.repo),
            "pages": [cluster_to_dict(c) for c in clusters],
            "unresolved_count": len(unresolved),
            "unresolved_breakdown": breakdown,
        }
        with open(args.out, "w") as f:
            json.dump(page_map, f, indent=2)
        log(f"\n[green]Saved page map to {args.out}[/green]")
        log("[dim]This map feeds the next Phase A steps (rule derivation + migration).[/dim]\n")


if __name__ == "__main__":
    main()
