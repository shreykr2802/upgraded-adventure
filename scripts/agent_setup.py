"""
scripts/agent_setup.py
──────────────────────
PHASE A — one-command agent setup. Combines A2 (analyse React repo) and
A3 (derive migration rules) into a single autonomous step.

What it does, end to end:
  A2a  Discover React components (react-docgen-typescript)      [structural]
  A2b  Enrich each with a semantic usage sentence (Haiku)        [meaning]
       └─► index all into the Design System store
  A2c  Scan existing React pages for usage/API patterns          [usage knowledge]
       └─► index into the Code Pattern store (no pairing)
  A3a  Extract unique Razor constructs from page_map.json        [deduped]
  A3b  Derive migration rules with Sonnet                        [the agent move]
       └─► save migration_rules.json (for review) + index into Code Pattern store

After this runs you review migration_rules.json, then Phase B migrates pages.

PREREQUISITES:
  - page_map.json produced by analyze_dotnet.py
  - react-docgen-typescript available (npx fallback works)
  - .env configured, vector store reachable (Chroma per your stores.py)

USAGE:
    cd backend
    python ../scripts/agent_setup.py \
        --react-repo /path/to/react \
        --dotnet-repo /path/to/dotnet \
        --page-map ../page_map.json \
        --import-base "@your-org/ui" \
        --rules-out ../migration_rules.json

    # Useful flags:
    #   --components-dir src/components     (default)
    #   --pages-dir src/pages               (default)
    #   --skip-pages                        (skip A2c usage scan)
    #   --skip-semantics                    (skip A2b Haiku enrichment — faster/cheaper)
    #   --dry-run                           (analyse + print, index nothing, derive nothing)
"""

import sys, os, json, argparse, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))

from app.analysis.component_scanner import discover_components, build_catalogue_text
from app.analysis.component_semantics import enrich_component_semantics, scan_react_pages
from app.analysis.razor_constructs import RazorConstructExtractor
from app.analysis.rule_deriver import RuleDeriver, save_rules, rules_to_code_patterns

try:
    from rich.console import Console
    from rich.table import Table
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


def step(n: str, title: str):
    log(f"\n[bold cyan]━━ {n}  {title}[/bold cyan]")


def main():
    p = argparse.ArgumentParser(description="Phase A — combined agent setup (A2 + A3)")
    p.add_argument("--react-repo", required=True)
    p.add_argument("--dotnet-repo", required=True)
    p.add_argument("--page-map", required=True, help="page_map.json from analyze_dotnet.py")
    p.add_argument("--components-dir", default="src/components")
    p.add_argument("--pages-dir", default="src/pages")
    p.add_argument("--import-base", default=None)
    p.add_argument("--rules-out", default="migration_rules.json")
    p.add_argument("--skip-pages", action="store_true")
    p.add_argument("--skip-semantics", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    t0 = time.perf_counter()
    log("\n[bold]UI Migration Agent — Phase A Setup[/bold]")
    log(f"   React repo  : {os.path.abspath(args.react_repo)}")
    log(f"   .NET repo   : {os.path.abspath(args.dotnet_repo)}")
    log(f"   Page map    : {args.page_map}")
    if args.dry_run:
        log("   [yellow]DRY RUN — nothing will be indexed or derived[/yellow]")

    # ─────────────────────────────────────────────────────────────────────────
    # A2a — Discover components (structural)
    # ─────────────────────────────────────────────────────────────────────────
    step("A2a", "Discovering React components")
    components = discover_components(
        react_repo=args.react_repo,
        components_dir=args.components_dir,
        import_base=args.import_base,
    )
    if not components:
        log("[red]No components discovered — check --components-dir and that "
            "react-docgen-typescript is available.[/red]")
        sys.exit(1)

    tiers: dict[str, int] = {}
    for c in components:
        tiers[c.tier or "untiered"] = tiers.get(c.tier or "untiered", 0) + 1
    log(f"   Found [bold]{len(components)}[/bold] components: " +
        ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())))

    # ─────────────────────────────────────────────────────────────────────────
    # A2b — Semantic enrichment (Haiku)
    # ─────────────────────────────────────────────────────────────────────────
    if args.skip_semantics:
        log("\n[yellow]Skipping A2b semantic enrichment (--skip-semantics)[/yellow]")
    else:
        step("A2b", "Enriching components with semantics (Haiku)")
        def comp_progress(i, total, name):
            log(f"   [{i}/{total}] {name}")
        if not args.dry_run:
            enrich_component_semantics(components, progress=comp_progress)
        else:
            log("   [dim](dry run — skipped Haiku calls)[/dim]")

    # ─────────────────────────────────────────────────────────────────────────
    # A2b (index) — Design System store
    # ─────────────────────────────────────────────────────────────────────────
    if not args.dry_run:
        from app.rag.indexer import index_design_system
        log("\n   Indexing components → Design System store...")
        index_design_system(components)
        log(f"   [green]✅ Indexed {len(components)} components[/green]")

    # ─────────────────────────────────────────────────────────────────────────
    # A2c — React page usage scan
    # ─────────────────────────────────────────────────────────────────────────
    if args.skip_pages:
        log("\n[yellow]Skipping A2c page usage scan (--skip-pages)[/yellow]")
    else:
        step("A2c", "Scanning React pages for usage patterns")
        if not args.dry_run:
            def page_progress(i, total, name):
                log(f"   [{i}/{total}] {name}")
            n = scan_react_pages(
                react_repo=args.react_repo,
                pages_dir=args.pages_dir,
                progress=page_progress,
            )
            log(f"   [green]✅ Indexed {n} pages as usage knowledge[/green]")
        else:
            log("   [dim](dry run — skipped)[/dim]")

    # ─────────────────────────────────────────────────────────────────────────
    # A3a — Extract unique Razor constructs
    # ─────────────────────────────────────────────────────────────────────────
    step("A3a", "Extracting unique Razor constructs")
    with open(args.page_map, "r", encoding="utf-8") as f:
        page_map = json.load(f)

    extractor = RazorConstructExtractor()
    extractor.scan_page_map(page_map, args.dotnet_repo)
    constructs = extractor.unique_constructs()
    summary = extractor.summary()
    log(f"   [bold]{summary['unique_constructs']}[/bold] unique constructs "
        f"from [bold]{summary['total_occurrences']}[/bold] total occurrences")
    log(f"   By family: " + ", ".join(f"{k}={v}" for k, v in summary["by_family"].items()))

    if not constructs:
        log("[yellow]No Razor constructs found — is page_map.json correct?[/yellow]")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────────────────
    # A3b — Derive migration rules (Sonnet)
    # ─────────────────────────────────────────────────────────────────────────
    step("A3b", "Deriving migration rules (Sonnet)")
    catalogue = build_catalogue_text(components)

    if args.dry_run:
        log("   [dim](dry run — skipping rule derivation)[/dim]")
        log(f"\n[bold]Would derive rules for {len(constructs)} constructs:[/bold]")
        for c in constructs:
            log(f"   [{c.family}/{c.kind}] x{c.occurrences}")
        return

    deriver = RuleDeriver(component_catalogue=catalogue)
    def rule_progress(i, total, c):
        log(f"   [{i}/{total}] {c.family}/{c.kind}")
    rules = deriver.derive_all(constructs, progress=rule_progress)

    # Save for review
    payload = save_rules(rules, args.rules_out)

    # Index into the Code Pattern store
    from app.rag.indexer import index_code_patterns
    patterns = rules_to_code_patterns(rules)
    index_code_patterns(patterns)
    log(f"   [green]✅ Derived {len(rules)} rules, indexed into Code Pattern store[/green]")

    # ─────────────────────────────────────────────────────────────────────────
    # Report
    # ─────────────────────────────────────────────────────────────────────────
    elapsed = round(time.perf_counter() - t0, 1)
    log(f"\n[bold green]━━ Phase A complete in {elapsed}s[/bold green]")

    s = payload["summary"]
    if HAS_RICH:
        table = Table(box=box.ROUNDED, header_style="bold white on dark_blue")
        table.add_column("Confidence", width=14)
        table.add_column("Count", justify="right", width=8)
        table.add_row("[green]high[/green]", str(s["high_confidence"]))
        table.add_row("[yellow]medium[/yellow]", str(s["medium_confidence"]))
        table.add_row("[red]low[/red]", str(s["low_confidence"]))
        console.print(table)

    if s["needs_review"]:
        log(f"\n[bold yellow]⚠ {len(s['needs_review'])} rules need review "
            f"(no clear component match):[/bold yellow]")
        for kind in s["needs_review"]:
            log(f"   • {kind}")

    log(f"\n[bold]Next steps:[/bold]")
    log(f"   1. Review [cyan]{args.rules_out}[/cyan] — especially the low-confidence rules")
    log(f"   2. Run Phase B to migrate pages from your page_map.json\n")


if __name__ == "__main__":
    main()
