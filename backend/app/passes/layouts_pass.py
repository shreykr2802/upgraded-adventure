"""
passes/layouts_pass.py
──────────────────────
Pass 3 — Convert .NET layout views (_Layout.cshtml and friends) into React
layout components (the app shell: header, nav, footer, <Outlet/> slot).

  discover()      → distinct layout files referenced across the page map,
                    plus any *_Layout.cshtml in the repo
  dependencies()  → none between layouts in practice (flat); a layout may
                    reference a partial, handled inline
  migrate_one()   → read the layout cshtml, generate a React layout component
                    with a children/Outlet slot, review
"""

from __future__ import annotations

import os
import logging

from app.passes.base import WorkItem, PassResult, PassContext
from app.passes.artifact_store import artifact_store
from app.services import generate_component, review_component
from app.passes.models_pass import _parse_json, _merge_usage

logger = logging.getLogger(__name__)


GENERATE_SYSTEM = """
You convert a .NET Razor layout (_Layout.cshtml) into a React + TypeScript
layout component — the application shell.

Rules:
1. Export one component named after the file (e.g. _Layout → MainLayout,
   _AdminLayout → AdminLayout).
2. @RenderBody() becomes a content slot: render {children} (props: { children: React.ReactNode }).
3. @RenderSection(...) becomes optional named slots via props.
4. Convert the header/nav/footer markup to JSX using design-system components
   where REFERENCE COMPONENTS provides a match; otherwise plain semantic JSX.
5. Drop server tags (@ViewBag, @Html.*) — replace with props or // TODO.
6. Keep styling class names as className.

Reply ONLY with valid JSON, no markdown:
{
  "filename": "<Name>.tsx",
  "code": "<full TS component>",
  "components_used": ["..."],
  "todos": ["..."]
}
""".strip()


class LayoutsPass:
    layer = "layout"

    def discover(self, ctx: PassContext) -> list[WorkItem]:
        seen: set[str] = set()
        items: list[WorkItem] = []

        # Layouts referenced by the page map
        for page in ctx.page_map.get("pages", []):
            layout = page.get("layout")
            if layout and layout not in seen:
                seen.add(layout)

        # Plus any *_Layout.cshtml in the repo (some may not be referenced)
        for dirpath, _, filenames in os.walk(ctx.dotnet_repo):
            if any(s in dirpath for s in ("bin", "obj", ".git", "node_modules")):
                continue
            for fn in filenames:
                if fn.lower().endswith(".cshtml") and "layout" in fn.lower():
                    rel = os.path.relpath(os.path.join(dirpath, fn), ctx.dotnet_repo)
                    seen.add(rel)

        for rel in sorted(seen):
            abs_path = os.path.join(ctx.dotnet_repo, rel)
            if not os.path.exists(abs_path):
                continue
            base = os.path.basename(rel)[:-len(".cshtml")].lstrip("_")
            symbol = (base or "Main") + ("Layout" if "layout" not in base.lower() else "")
            items.append(WorkItem(
                origin=rel, symbol=symbol, source_path=abs_path, extra={},
            ))
        logger.info("Layouts pass discovered %d layouts", len(items))
        return items

    def dependencies(self, item: WorkItem, ctx: PassContext) -> list[str]:
        return []   # layouts are treated as independent shells

    def migrate_one(self, item: WorkItem, ctx: PassContext) -> PassResult:
        store = artifact_store()

        try:
            with open(item.source_path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError as e:
            return _fail_layout(item, f"read_error: {e}")

        # Retrieve a few design-system components for grounding
        comp_hits = store.retrieve(src[:1000], top_k=5, layer=None)
        comp_context = _hits_to_context(comp_hits) or "(none)"

        user_msg = (
            f"RAZOR LAYOUT ({item.origin}):\n{src}\n\n"
            f"REFERENCE COMPONENTS (design system, use where they fit):\n{comp_context}"
        )

        try:
            resp = generate_component(
                messages=[{"role": "user", "content": user_msg}],
                system=GENERATE_SYSTEM, max_tokens=3000, temperature=0.1,
            )
            parsed = _parse_json(resp.text)
            if not parsed or "code" not in parsed:
                return _fail_layout(item, "generation_parse_failed")

            filename = parsed.get("filename") or f"{item.symbol}.tsx"
            code = parsed["code"]
            review = review_component(generated_tsx=code)
            rparsed = _parse_json(review.text) or {}

            return PassResult(
                origin=item.origin, symbol=item.symbol, layer=self.layer,
                output_path=f"layouts/{filename}",
                files={filename: code},
                todos=parsed.get("todos", []),
                review_valid=bool(rparsed.get("valid", True)),
                review_issues=rparsed.get("issues", []),
                confidence=rparsed.get("confidence", "medium"),
                token_usage=_merge_usage(resp, review),
            )
        except Exception as e:
            logger.exception("Layout migration failed for %s", item.origin)
            return _fail_layout(item, str(e))


def _hits_to_context(hits) -> str:
    blocks = []
    for h in hits or []:
        payload = getattr(h, "payload", {}) or {}
        text = payload.get("text", "")
        if text:
            blocks.append(text[:400])
    return "\n\n".join(blocks)


def _fail_layout(item: WorkItem, error: str) -> PassResult:
    return PassResult(
        origin=item.origin, symbol=item.symbol, layer="layout",
        output_path=f"layouts/{item.symbol}.tsx", files={},
        review_valid=False, confidence="low", error=error,
    )
