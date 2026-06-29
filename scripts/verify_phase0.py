"""
verify_phase0.py
────────────────
Run this after filling in .env to confirm all 5 model roles
are reachable through your gateway.

Usage:
    cd backend
    python ../scripts/verify_phase0.py

Each check prints ✅ (pass) or ❌ (fail + specific reason).
Fix all failures before moving to Phase 1.
"""

import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from app.config import settings
from app.gateway import chat, embed, rerank, health, GatewayError

results: list[tuple[str, str, bool, str]] = []


def check(role: str, model_id: str, fn):
    try:
        result = fn()
        results.append((role, model_id, True, repr(result)))
    except GatewayError as e:
        results.append((role, model_id, False, f"HTTP {e.status} — {e.detail[:120]}"))
    except Exception as e:
        results.append((role, model_id, False, str(e)[:120]))


# ── Run ───────────────────────────────────────────────────────────────────────

print(f"\n🔍  UI Migration Agent — Phase 0 Verification")
print(f"   Gateway : {settings.llm_gateway_url}")
print(f"   Env     : {settings.app_env}\n")

print("Checking gateway connectivity...")
if not health():
    print(f"❌  Cannot reach gateway at {settings.llm_gateway_url}")
    print("    Check LLM_GATEWAY_URL and VPN/network access. Aborting.\n")
    sys.exit(1)
print("✅  Gateway reachable\n")

print("Checking Claude Sonnet 4.6 (generation)...")
check("Generation LLM", settings.model_sonnet, lambda: chat(
    model=settings.model_sonnet,
    messages=[{"role": "user", "content": "Reply with exactly: SONNET_OK"}],
    max_tokens=20, temperature=0.0,
))

print("Checking Claude Haiku 3.5 (classify + review)...")
check("Fast LLM", settings.model_haiku, lambda: chat(
    model=settings.model_haiku,
    messages=[{"role": "user", "content": "Reply with exactly: HAIKU_OK"}],
    max_tokens=20, temperature=0.0,
))

print("Checking BAAI/llm-embedder (code embedding)...")
check("Code Embedding", settings.model_code_embed, lambda: embed(
    model=settings.model_code_embed,
    texts=["@Html.TextBoxFor(m => m.FirstName)"],
))

print("Checking Amazon Titan embed-text-v2 (doc embedding)...")
check("Doc Embedding", settings.model_doc_embed, lambda: embed(
    model=settings.model_doc_embed,
    texts=["Use TextInput for single-line text entry."],
))

print("Checking BAAI/bge-reranker-large (reranker)...\n")
check("Reranker", settings.model_reranker, lambda: rerank(
    model=settings.model_reranker,
    query="convert input field to React",
    documents=[
        "TextInput component for single-line text",
        "Button component for actions",
        "DataTable for displaying rows",
    ],
    top_n=2,
))


# ── Report ────────────────────────────────────────────────────────────────────

passed = sum(1 for *_, ok, __ in results if ok)
total  = len(results)

if HAS_RICH:
    console = Console()
    table = Table(box=box.ROUNDED, header_style="bold white on dark_blue")
    table.add_column("Role",     style="bold", width=20)
    table.add_column("Model",    style="dim",  width=32)
    table.add_column("Status",   justify="center", width=8)
    table.add_column("Detail",   width=46)
    for role, model, ok, detail in results:
        status = "[green]✅ PASS[/green]" if ok else "[red]❌ FAIL[/red]"
        table.add_row(role, model, status, detail if ok else f"[red]{detail}[/red]")
    console.print(table)
    console.print(f"\n  [bold]{passed}/{total} checks passed[/bold]\n")
else:
    for role, model, ok, detail in results:
        icon = "✅" if ok else "❌"
        print(f"{icon}  {role:<22} {model}")
        if not ok:
            print(f"   Error: {detail}")
    print(f"\n{passed}/{total} checks passed\n")

# ── Troubleshooting ───────────────────────────────────────────────────────────

failed = [(role, model, detail) for role, model, ok, detail in results if not ok]
if failed:
    print("── Troubleshooting ─────────────────────────────────────────────────────\n")
    for role, model, detail in failed:
        print(f"  ❌ {role} ({model})")
        if "401" in detail or "403" in detail:
            print("  → Auth error. Check LLM_GATEWAY_KEY in your .env.\n")
        elif "404" in detail or "model not found" in detail.lower():
            print(f"  → Wrong model identifier. Confirm the exact string with your")
            print(f"    gateway admin. Current value in .env: '{model}'\n")
        elif "rerank" in role.lower():
            print("  → The /rerank route may not be enabled on your gateway,")
            print("    or it uses a different path/shape. Check with gateway admin.\n")
        elif "connect" in detail.lower() or "status=0" in detail:
            print("  → Cannot reach the gateway. Check LLM_GATEWAY_URL and VPN.\n")
        else:
            print(f"  → {detail}\n")
    sys.exit(1)

print("✅  Phase 0 complete — all model roles reachable.")
print("    Next: implement the Phase 1 /migrate endpoint in backend/app/main.py\n")
