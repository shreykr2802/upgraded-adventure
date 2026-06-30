"""
passes/controllers_pass.py
──────────────────────────
Pass 2 — Convert C# controllers into React hooks with typed fetch STUBS.

Per the project decision: convert to typed fetch stubs, NOT real endpoints.
Each controller action becomes a typed async function inside a use<Name>()
hook, returning the (already-converted) TS interface, with the actual HTTP
call left as a clearly-marked TODO stub.

  discover()      → every *Controller.cs
  dependencies()  → the model interfaces (Pass 1 output) the controller uses,
                    so hooks import real types
  migrate_one()   → parse controller, retrieve referenced interfaces, generate
                    a hook file, review
"""

from __future__ import annotations

import os
import logging

from app.passes.base import WorkItem, PassResult, PassContext
from app.passes.csharp_parser import parse_controller, ControllerInfo
from app.passes.artifact_store import artifact_store
from app.services import generate_component, review_component
from app.passes.models_pass import (
    _iter_cs_files, _find_artifact_by_symbol, _parse_json, _merge_usage,
)

logger = logging.getLogger(__name__)


GENERATE_SYSTEM = """
You convert a C# MVC controller into a React + TypeScript data hook.

IMPORTANT: do NOT implement real HTTP endpoints. Produce typed fetch STUBS —
each action becomes a typed async function whose body is a // TODO stub that
returns a typed placeholder. The point is correct TypeScript shape and naming,
not working network calls.

Rules:
1. Export one hook: use<ControllerNameWithoutController>()  (e.g. UserController → useUser).
2. For each GET-like action that returns a view/model, add an async function
   that returns the corresponding TS interface (imported from ../types).
3. For each POST-like action, add an async mutation function with a typed
   parameter and a Promise<void> or typed result.
4. Use the model interfaces from REFERENCE INTERFACES for all types; import them
   with: import type { X } from '../types/X';
5. Each function body is a stub:
      // TODO: wire endpoint — original action: <Action>
      throw new Error('not implemented');
   (or return a typed mock for GETs if trivial).
6. No real fetch URLs, no axios, no business logic from the controller body.

Reply ONLY with valid JSON, no markdown:
{
  "filename": "use<Name>.ts",
  "code": "<full TS hook file>",
  "imports": ["<interface names imported>"],
  "todos": ["<each stubbed endpoint>"]
}
""".strip()

REVIEW_SYSTEM = """
Review a generated React hook (typed fetch stubs) from a C# controller.
Check: valid TS, exports a use<Name> hook, imports resolve to referenced
interfaces, each endpoint is a clearly-marked TODO stub (no invented URLs).

Reply ONLY with valid JSON:
{"valid": true|false, "issues": ["..."], "confidence": "high"|"medium"|"low"}
""".strip()


class ControllersPass:
    layer = "controller"

    def discover(self, ctx: PassContext) -> list[WorkItem]:
        items: list[WorkItem] = []
        for cs_path in _iter_cs_files(ctx.dotnet_repo):
            if "Controller" not in os.path.basename(cs_path):
                continue
            try:
                with open(cs_path, "r", encoding="utf-8", errors="replace") as f:
                    src = f.read()
            except OSError:
                continue
            co = parse_controller(src)
            if co is None:
                continue
            rel = os.path.relpath(cs_path, ctx.dotnet_repo)
            items.append(WorkItem(
                origin=rel, symbol=co.name, source_path=cs_path,
                extra={"controller_info": co},
            ))
        logger.info("Controllers pass discovered %d controllers", len(items))
        return items

    def dependencies(self, item: WorkItem, ctx: PassContext) -> list[str]:
        # Controllers depend on model origins (so interfaces exist first).
        # We map referenced model type names → model origins via the store +
        # the model name index. Returning model origins keeps the topo sort
        # correct across layers (models already done in an earlier pass, so
        # these resolve to completed work — used here mainly for context).
        co: ControllerInfo = item.extra["controller_info"]
        store = artifact_store()
        deps: list[str] = []
        for t in co.referenced_types:
            art = _find_artifact_by_symbol(store, t)
            if art and art.origin not in deps:
                deps.append(art.origin)
        return deps

    def migrate_one(self, item: WorkItem, ctx: PassContext) -> PassResult:
        co: ControllerInfo = item.extra["controller_info"]
        store = artifact_store()

        ref_blocks = []
        for t in co.referenced_types:
            art = _find_artifact_by_symbol(store, t)
            if art:
                ref_blocks.append(f"// {art.output_path}\n{art.output_code}")
        ref_context = "\n\n".join(ref_blocks) if ref_blocks else "(none)"

        actions_summary = "\n".join(
            f"- {a.name}: views={a.return_views} models={a.model_types}"
            f"{' [dynamic view]' if a.has_dynamic_view else ''}"
            for a in co.actions
        )

        user_msg = (
            f"C# CONTROLLER ({item.origin}):\n{co.raw}\n\n"
            f"ACTIONS:\n{actions_summary}\n\n"
            f"REFERENCE INTERFACES (import these for types):\n{ref_context}"
        )

        try:
            resp = generate_component(
                messages=[{"role": "user", "content": user_msg}],
                system=GENERATE_SYSTEM,
                max_tokens=2500,
                temperature=0.0,
            )
            parsed = _parse_json(resp.text)
            if not parsed or "code" not in parsed:
                return _fail_ctrl(item, "generation_parse_failed")

            base = item.symbol.replace("Controller", "")
            filename = parsed.get("filename") or f"use{base}.ts"
            code = parsed["code"]

            review = review_component(generated_tsx=code)
            rparsed = _parse_json(review.text) or {}

            return PassResult(
                origin=item.origin, symbol=f"use{base}", layer=self.layer,
                output_path=f"hooks/{filename}",
                files={filename: code},
                depends_on=self.dependencies(item, ctx),
                todos=parsed.get("todos", []),
                review_valid=bool(rparsed.get("valid", True)),
                review_issues=rparsed.get("issues", []),
                confidence=rparsed.get("confidence", "medium"),
                token_usage=_merge_usage(resp, review),
            )
        except Exception as e:
            logger.exception("Controller migration failed for %s", item.origin)
            return _fail_ctrl(item, str(e))


def _fail_ctrl(item: WorkItem, error: str) -> PassResult:
    base = item.symbol.replace("Controller", "")
    return PassResult(
        origin=item.origin, symbol=f"use{base}", layer="controller",
        output_path=f"hooks/use{base}.ts", files={},
        review_valid=False, confidence="low", error=error,
    )
