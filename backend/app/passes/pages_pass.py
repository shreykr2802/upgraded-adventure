"""
passes/pages_pass.py
────────────────────
Pass 5 — Convert entry views into final React page components, COMPOSING the
already-converted artifacts from every earlier pass:
  - model interfaces (Pass 1)
  - hooks (Pass 2)
  - layout (Pass 3)
  - shared components (Pass 4)

This is the culmination: a page is assembled from real, already-migrated
TypeScript, not derived from scratch.

  discover()      → entry (non-partial) views from the page map
  dependencies()  → none between pages (each is independent)
  migrate_one()   → assemble the cluster source, retrieve the converted hook,
                    layout, components and model interface, generate the page,
                    review; merge grapher-unresolved items into TODOs
"""

from __future__ import annotations

import os
import logging

from app.passes.base import WorkItem, PassResult, PassContext
from app.passes.artifact_store import artifact_store
from app.services import generate_component, review_component
from app.passes.models_pass import _parse_json, _merge_usage, _find_artifact_by_symbol
from app.passes.layouts_pass import _hits_to_context

logger = logging.getLogger(__name__)


GENERATE_SYSTEM = """
You convert a complete .NET page (entry view + its partials, with its layout,
controller and model already migrated) into a final React + TypeScript page
component that COMPOSES the already-converted pieces.

You are given:
- the original Razor cluster (for intent),
- the converted MODEL interface (import its type),
- the converted HOOK (use its data functions),
- the converted LAYOUT (wrap the page in it),
- converted shared COMPONENTS (compose them instead of re-implementing).

Rules:
1. Export one page component (e.g. Views/User/Edit → UserEditPage).
2. Import and use the converted hook for data (e.g. const { ... } = useUser()).
3. Import and compose the converted shared components for the parts of the page
   that map to them (don't re-implement a partial that already became a component).
4. Wrap content in the converted layout if the page had one.
5. Use the model interface for all typed state/props.
6. Use design-system components for any remaining raw form/table markup.
7. Flag dropped server logic as // TODO; never silently lose behavior.

Reply ONLY with valid JSON, no markdown:
{
  "structure": "single"|"multi",
  "files": {"<File>.tsx": "<code>", "...": "..."},
  "components_used": ["..."],
  "imports": ["..."],
  "todos": ["..."]
}
""".strip()


class PagesPass:
    layer = "page"

    def discover(self, ctx: PassContext) -> list[WorkItem]:
        items: list[WorkItem] = []
        for page in ctx.page_map.get("pages", []):
            name = page.get("page_name")
            entry = page.get("entry_view")
            if not name or not entry:
                continue
            symbol = _page_symbol(name)
            items.append(WorkItem(
                origin=entry, symbol=symbol, source_path=os.path.join(ctx.dotnet_repo, entry),
                extra={"page": page},
            ))
        logger.info("Pages pass discovered %d pages", len(items))
        return items

    def dependencies(self, item: WorkItem, ctx: PassContext) -> list[str]:
        return []   # pages are independent of each other

    def migrate_one(self, item: WorkItem, ctx: PassContext) -> PassResult:
        page = item.extra["page"]
        store = artifact_store()

        # ── assemble the cluster source ──
        cluster_src = _assemble_cluster(page, ctx.dotnet_repo)

        # ── retrieve converted artifacts that this page composes ──
        ref_sections: list[str] = []

        # model interface
        model_name = _model_symbol_from_page(page, ctx.dotnet_repo)
        if model_name:
            art = _find_artifact_by_symbol(store, model_name)
            if art:
                ref_sections.append(f"CONVERTED MODEL INTERFACE:\n// {art.output_path}\n{art.output_code}")

        # hook (from the controller of this page)
        ctrl = page.get("controller")
        if ctrl:
            ctrl_base = os.path.basename(ctrl)[:-len(".cs")].replace("Controller", "")
            art = _find_artifact_by_symbol(store, f"use{ctrl_base}")
            if art:
                ref_sections.append(f"CONVERTED HOOK:\n// {art.output_path}\n{art.output_code}")

        # layout
        layout = page.get("layout")
        if layout:
            for a in store.by_layer("layout"):
                if a.origin == layout:
                    ref_sections.append(f"CONVERTED LAYOUT:\n// {a.output_path}\n{a.output_code}")
                    break

        # shared components (the page's partials → converted components)
        for partial in page.get("partials", []):
            for a in store.by_layer("component"):
                if a.origin == partial:
                    ref_sections.append(f"CONVERTED COMPONENT:\n// {a.output_path}\n{a.output_code}")
                    break

        ref_context = "\n\n".join(ref_sections) if ref_sections else "(none converted yet)"

        user_msg = (
            f"ORIGINAL RAZOR CLUSTER ({item.origin}):\n{cluster_src}\n\n"
            f"{ref_context}"
        )

        try:
            resp = generate_component(
                messages=[{"role": "user", "content": user_msg}],
                system=GENERATE_SYSTEM, max_tokens=6000, temperature=0.1,
            )
            parsed = _parse_json(resp.text)
            if not parsed or "files" not in parsed:
                return _fail_page(item, "generation_parse_failed")

            files = parsed["files"]
            primary = next(iter(files.keys()), f"{item.symbol}.tsx")
            review = review_component(
                generated_tsx="\n\n".join(f"// {n}\n{c}" for n, c in files.items())
            )
            rparsed = _parse_json(review.text) or {}

            # merge grapher-unresolved into todos
            todos = list(parsed.get("todos", []))
            for u in page.get("unresolved", []):
                todos.append(f"[grapher/{u.get('kind')}] {u.get('reference')} — {u.get('reason')}")

            return PassResult(
                origin=item.origin, symbol=item.symbol, layer=self.layer,
                output_path=f"pages/{primary}",
                files=files,
                todos=todos,
                review_valid=bool(rparsed.get("valid", True)),
                review_issues=rparsed.get("issues", []),
                confidence=rparsed.get("confidence", "medium"),
                token_usage=_merge_usage(resp, review),
            )
        except Exception as e:
            logger.exception("Page migration failed for %s", item.origin)
            return _fail_page(item, str(e))


# ── helpers ───────────────────────────────────────────────────────────────────

def _page_symbol(page_name: str) -> str:
    parts = [p for p in page_name.replace("\\", "/").split("/") if p]
    pascal = "".join(p[:1].upper() + p[1:] for p in parts)
    return f"{pascal}Page"


def _assemble_cluster(page: dict, repo: str, max_chars: int = 6000) -> str:
    parts = []
    for label, rel in [("ENTRY VIEW", page.get("entry_view"))]:
        if rel:
            parts.append(_read_section(label, rel, repo, max_chars))
    for partial in page.get("partials", []):
        parts.append(_read_section("PARTIAL", partial, repo, max_chars))
    return "".join(p for p in parts if p)


def _read_section(label: str, rel: str, repo: str, max_chars: int) -> str:
    path = os.path.join(repo, rel)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    if len(content) > max_chars:
        content = content[:max_chars] + "\n/* ...truncated... */"
    return f"\n===== {label}: {rel} =====\n{content}\n"


def _model_symbol_from_page(page: dict, repo: str) -> str | None:
    entry = page.get("entry_view")
    if not entry:
        return None
    path = os.path.join(repo, entry)
    if not os.path.exists(path):
        return None
    import re
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()
    m = re.search(r"@model\s+([\w\.\<\>]+)", src)
    if not m:
        return None
    from app.passes.csharp_parser import unwrap_type
    core, _ = unwrap_type(m.group(1).split(".")[-1])
    return core


def _fail_page(item: WorkItem, error: str) -> PassResult:
    return PassResult(
        origin=item.origin, symbol=item.symbol, layer="page",
        output_path=f"pages/{item.symbol}.tsx", files={},
        review_valid=False, confidence="low", error=error,
    )
