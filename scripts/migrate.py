"""
scripts/migrate.py
──────────────────
Run the full layered migration: five ordered passes
(models → controllers → layouts → components → pages), each topologically
sorted, each indexing its output before the next, with review gates between
passes.

PREREQUISITES:
  - page_map.json produced by analyze_dotnet.py
  - .env configured (gateway, models, CHROMA_PATH)
  - design system seeded via agent_setup.py (improves component/page quality)

USAGE:
  cd backend

  # Run every layer straight through (auto-approve each gate):
  python ../scripts/migrate.py \
      --dotnet-repo /path/to/dotnet \
      --react-repo /path/to/react \
      --page-map ../page_map.json \
      --out ../migrated \
      --auto-approve

  # Run one layer at a time (stop at each review gate):
  python ../scripts/migrate.py ... --layer model
  #   review, then:
  python ../scripts/migrate.py ... --approve         # advance to next layer
  python ../scripts/migrate.py ... --layer controller
  ...

  # Resume wherever you left off:
  python ../scripts/migrate.py ... --resume

LAYERS: model → controller → layout → component → page
OUTPUT: <out>/types, /hooks, /layouts, /components, /pages + manifest + records
"""

import sys, os, json, argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))

from app.passes.base import PassContext
from app.passes.registry import register_all_passes
from app.passes.orchestrator import Orchestrator
from app.passes.artifact_store import LAYERS

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


def build_orchestrator(args) -> Orchestrator:
    with open(args.page_map, "r", encoding="utf-8") as f:
        page_map = json.load(f)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    ctx = PassContext(
        dotnet_repo=os.path.abspath(args.dotnet_repo),
        react_repo=os.path.abspath(args.react_repo),
        page_map=page_map,
        output_root=out,
        import_base=args.import_base,
    )
    register_all_passes()
    return Orchestrator(
        ctx,
        manifest_path=os.path.join(out, "manifest.json"),
        records_path=os.path.join(out, "artifact_records.json"),
    )


def run_one_layer(orch: Orchestrator):
    layer = orch.current_layer()
    if layer is None:
        log("[green]Migration already complete.[/green]")
        return
    log(f"\n[bold cyan]━━ Pass: {layer} ━━[/bold cyan]")

    def progress(stage, current, total, message):
        if stage == "migrating" and total:
            log(f"   [{current}/{total}] {message}")
        elif stage == "awaiting_review":
            log(f"   [yellow]{message}[/yellow]")
        else:
            log(f"   {message}")

    st = orch.run_layer(progress=progress)
    ok = st.completed - len(st.failed)
    log(f"[bold]{layer}[/bold]: {ok} ok, {len(st.failed)} failed of {st.total}")
    if st.cycles:
        log(f"   [yellow]dependency cycles: {st.cycles}[/yellow]")
    if st.failed:
        log(f"   [red]failed: {st.failed}[/red]")
    log(f"   [dim]review output under {orch.ctx.output_root}, then --approve[/dim]")


def main():
    p = argparse.ArgumentParser(description="Layered .NET → React migration")
    p.add_argument("--dotnet-repo", required=True)
    p.add_argument("--react-repo", required=True)
    p.add_argument("--page-map", required=True)
    p.add_argument("--out", default="migrated")
    p.add_argument("--import-base", default=None)
    p.add_argument("--layer", choices=LAYERS, help="Run a specific layer (must be the current one)")
    p.add_argument("--approve", action="store_true", help="Approve the current layer's review gate and advance")
    p.add_argument("--resume", action="store_true", help="Run the current layer (wherever the manifest left off)")
    p.add_argument("--auto-approve", action="store_true", help="Run all remaining layers, auto-approving each gate")
    p.add_argument("--status", action="store_true", help="Show current migration status and exit")
    args = p.parse_args()

    orch = build_orchestrator(args)

    # ── status ────────────────────────────────────────────────────────────────
    if args.status:
        s = orch.status()
        log("\n[bold]Migration status[/bold]")
        if HAS_RICH:
            t = Table(box=box.ROUNDED)
            t.add_column("Layer"); t.add_column("Status"); t.add_column("Done")
            for l in LAYERS:
                ls = s["layers"][l]
                t.add_row(l, ls["status"], f"{ls.get('completed',0)}/{ls.get('total',0)}")
            console.print(t)
        else:
            for l in LAYERS:
                ls = s["layers"][l]
                log(f"  {l}: {ls['status']} ({ls.get('completed',0)}/{ls.get('total',0)})")
        log(f"current: {s['current_layer']} | complete: {s['complete']}")
        return

    # ── approve a gate ────────────────────────────────────────────────────────
    if args.approve:
        nxt = orch.approve_layer()
        log(f"[green]Approved.[/green] Next layer: {nxt or '(complete)'}")
        return

    # ── auto-approve: run everything ──────────────────────────────────────────
    if args.auto_approve:
        log("\n[bold]Running all layers (auto-approve)[/bold]")
        while orch.current_layer() is not None:
            run_one_layer(orch)
            orch.approve_layer()
        log("\n[bold green]✅ Migration complete.[/bold green]")
        _final_summary(orch)
        return

    # ── single layer (explicit or resume) ─────────────────────────────────────
    if args.layer and args.layer != orch.current_layer():
        log(f"[red]Current layer is '{orch.current_layer()}', not '{args.layer}'.[/red] "
            f"Use --approve to advance, or --resume to run the current one.")
        sys.exit(1)

    run_one_layer(orch)


def _final_summary(orch: Orchestrator):
    import glob
    out = orch.ctx.output_root
    counts = {}
    for layer_dir in ["types", "hooks", "layouts", "components", "pages"]:
        files = glob.glob(os.path.join(out, layer_dir, "**", "*.ts*"), recursive=True)
        counts[layer_dir] = len(files)
    log("\n[bold]Generated:[/bold]")
    for d, n in counts.items():
        log(f"   {d}: {n} files")
    log(f"\n   Output: {out}")


if __name__ == "__main__":
    main()
