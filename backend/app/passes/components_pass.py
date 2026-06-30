"""
passes/components_pass.py
─────────────────────────
Pass 4 — Convert reusable partial views (_*.cshtml / *Partial.cshtml, and the
EditorTemplates) into shared React components, mapped to the design system.

These are the pieces multiple pages share. Converting them before pages means
Pass 5 composes already-built components instead of re-deriving them.

  discover()      → distinct partials referenced across the page map clusters
  dependencies()  → partials that include other partials (nested) → ordered
  migrate_one()   → read the partial cshtml, retrieve design-system components +
                    any already-converted model interfaces, generate, review
"""

from __future__ import annotations

import os
import re
import logging

from app.passes.base import WorkItem, PassResult, PassContext
from app.passes.artifact_store import artifact_store
from app.services import generate_component, review_component
from app.passes.models_pass import _parse_json, _merge_usage
from app.passes.layouts_pass import _hits_to_context

logger = logging.getLogger(__name__)


GENERATE_SYSTEM = """
You convert a reusable .NET Razor partial view into a shared React + TypeScript
component, using the project's design system.

Rules:
1. Export one component named in PascalCase from the partial's name
   (_EditForm → EditForm, _ContactBlockPartial → ContactBlock).
2. Use design-system components from REFERENCE COMPONENTS wherever a form field,
   button, table, etc. is present. Map HTML/Razor inputs to the matching
   component (text input, select, checkbox, table→grid, etc.). Never emit raw
   <input>/<table> when a design-system component exists.
3. Type the props from the @model directive using the matching interface from
   REFERENCE INTERFACES; import it from '../types/<Name>'.
4. @Html.*For(m => m.Field) → the corresponding controlled component bound to a
   prop value + onChange.
5. Flag any server-side logic (validation, auth) as // TODO — never drop silently.

Reply ONLY with valid JSON, no markdown:
{
  "filename": "<Name>.tsx",
  "code": "<full TS component>",
  "components_used": ["..."],
  "imports": ["..."],
  "todos": ["..."]
}
""".strip()


def _is_partial(filename: str) -> bool:
    base = filename[:-len(".cshtml")] if filename.endswith(".cshtml") else filename
    return base.startswith("_") or base.lower().endswith("partial")


class ComponentsPass:
    layer = "component"

    def discover(self, ctx: PassContext) -> list[WorkItem]:
        # Collect every partial referenced as a cluster partial in the page map.
        seen: dict[str, str] = {}    # rel → abs
        for page in ctx.page_map.get("pages", []):
            for partial in page.get("partials", []):
                abs_path = os.path.join(ctx.dotnet_repo, partial)
                if os.path.exists(abs_path):
                    seen.setdefault(partial, abs_path)

        items: list[WorkItem] = []
        for rel, abs_path in sorted(seen.items()):
            base = os.path.basename(rel)[:-len(".cshtml")].lstrip("_")
            # strip a trailing "Partial"
            if base.lower().endswith("partial"):
                base = base[: -len("partial")]
            symbol = _pascal(base) or "Component"
            items.append(WorkItem(
                origin=rel, symbol=symbol, source_path=abs_path,
                extra={"rel": rel},
            ))
        logger.info("Components pass discovered %d partials", len(items))
        return items

    def dependencies(self, item: WorkItem, ctx: PassContext) -> list[str]:
        # A partial that includes other partials depends on them. Re-read and
        # look for Partial("_X") references that are also in our discovered set.
        deps: list[str] = []
        try:
            with open(item.source_path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            return deps
        import re
        refs = re.findall(r'Partial(?:Async)?\(\s*["\']([^"\']+)["\']', src)
        known = {os.path.basename(p.origin)[:-len(".cshtml")].lower(): p.origin
                 for p in self.discover(ctx)}
        for r in refs:
            key = os.path.basename(r).lstrip("_").lower()
            if key in known and known[key] != item.origin:
                deps.append(known[key])
        return deps

    def migrate_one(self, item: WorkItem, ctx: PassContext) -> PassResult:
        store = artifact_store()

        try:
            with open(item.source_path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError as e:
            return _fail_comp(item, f"read_error: {e}")

        comp_hits = store.retrieve(src[:1200], top_k=6)
        comp_context = _hits_to_context(comp_hits) or "(none)"

        # model interface, if @model present
        import re
        model_ctx = "(none)"
        mm = re.search(r"@model\s+([\w\.\<\>]+)", src)
        if mm:
            from app.passes.csharp_parser import unwrap_type
            core, _ = unwrap_type(mm.group(1).split(".")[-1])
            from app.passes.models_pass import _find_artifact_by_symbol
            art = _find_artifact_by_symbol(store, core)
            if art:
                model_ctx = f"// {art.output_path}\n{art.output_code}"

        user_msg = (
            f"RAZOR PARTIAL ({item.origin}):\n{src}\n\n"
            f"REFERENCE COMPONENTS (design system):\n{comp_context}\n\n"
            f"REFERENCE INTERFACE (model type):\n{model_ctx}"
        )

        try:
            resp = generate_component(
                messages=[{"role": "user", "content": user_msg}],
                system=GENERATE_SYSTEM, max_tokens=3500, temperature=0.1,
            )
            parsed = _parse_json(resp.text)
            if not parsed or "code" not in parsed:
                return _fail_comp(item, "generation_parse_failed")

            filename = parsed.get("filename") or f"{item.symbol}.tsx"
            code = parsed["code"]
            review = review_component(generated_tsx=code)
            rparsed = _parse_json(review.text) or {}

            return PassResult(
                origin=item.origin, symbol=item.symbol, layer=self.layer,
                output_path=f"components/{filename}",
                files={filename: code},
                depends_on=self.dependencies(item, ctx),
                todos=parsed.get("todos", []),
                review_valid=bool(rparsed.get("valid", True)),
                review_issues=rparsed.get("issues", []),
                confidence=rparsed.get("confidence", "medium"),
                token_usage=_merge_usage(resp, review),
            )
        except Exception as e:
            logger.exception("Component migration failed for %s", item.origin)
            return _fail_comp(item, str(e))


def _pascal(s: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]", s)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _fail_comp(item: WorkItem, error: str) -> PassResult:
    return PassResult(
        origin=item.origin, symbol=item.symbol, layer="component",
        output_path=f"components/{item.symbol}.tsx", files={},
        review_valid=False, confidence="low", error=error,
    )
