"""
api/routes_setup.py
───────────────────
Stage 2 — Setup the agent: scan React components, derive migration rules,
seed the vector stores. This is the API form of scripts/agent_setup.py.

  POST /api/setup            run A2 + A3 (SSE progress stream)
  GET  /api/components       discovered design-system components
  GET  /api/rules            derived migration rules + confidence summary
  GET  /api/rules/{kind}     one rule's detail

The SSE stream mirrors the agent_setup stages:
  A2a discover components → A2b semantic pass → index design store
  A2c scan React pages (usage) → A3a extract constructs → A3b derive rules
  → save migration_rules.json + index code store

Read endpoints follow "read JSON, fall back to recompute" for rules
(migration_rules.json). Components are re-discovered on demand since they
aren't separately persisted as a standalone file.
"""

from __future__ import annotations

import os
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import require_project, PAGE_MAP_PATH, RULES_PATH
from app.api.events import (
    SSE_HEADERS, SSE_MEDIA_TYPE,
    stage_event, progress_event, done_event, error_event,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["setup"])


# ── POST /api/setup (SSE) ─────────────────────────────────────────────────────

@router.post("/setup")
def setup(skip_semantics: bool = False, skip_pages: bool = False):
    """
    Run the full agent setup (A2 + A3), streaming progress as SSE.

    Query params:
      skip_semantics — skip the Haiku semantic enrichment pass (faster/cheaper)
      skip_pages     — skip indexing React pages as usage knowledge
    """
    cfg = require_project()

    if not os.path.exists(PAGE_MAP_PATH):
        raise HTTPException(
            status_code=409,
            detail="No page map found. Run POST /api/analyze first.",
        )

    def stream():
        try:
            from app.analysis.component_scanner import discover_components, build_catalogue_text
            from app.analysis.component_semantics import enrich_component_semantics, scan_react_pages
            from app.analysis.razor_constructs import RazorConstructExtractor
            from app.analysis.rule_deriver import RuleDeriver, save_rules, rules_to_code_patterns
            from app.rag.indexer import index_design_system, index_code_patterns

            # ── A2a: discover components ─────────────────────────────────────
            yield stage_event("components", "discovering React components")
            components = discover_components(
                react_repo=cfg.react_repo,
                components_dir=cfg.components_dir,
                import_base=cfg.import_base,
            )
            if not components:
                yield error_event(
                    "No components discovered. Check components_dir and that "
                    "@react-docgen/cli is available."
                )
                return
            yield progress_event("components", len(components), len(components),
                                 f"found {len(components)} components")

            # ── A2b: semantic enrichment (Haiku) ─────────────────────────────
            if not skip_semantics:
                yield stage_event("semantics", "enriching components (Haiku)")
                total = len(components)
                # enrich in place; emit progress via a callback bridge
                done = {"n": 0}
                def on_comp(i, t, name):
                    done["n"] = i
                # enrich_component_semantics takes a progress callback
                enrich_component_semantics(components, progress=on_comp)
                yield progress_event("semantics", total, total, "semantic pass complete")

            # ── index design store ───────────────────────────────────────────
            yield stage_event("index_design", "indexing components → design store")
            index_design_system(components)

            # ── A2c: scan React pages as usage knowledge ─────────────────────
            if not skip_pages:
                yield stage_event("usage", "scanning React pages for usage patterns")
                n_pages = scan_react_pages(
                    react_repo=cfg.react_repo,
                    pages_dir=cfg.pages_dir,
                )
                yield progress_event("usage", n_pages, n_pages,
                                     f"indexed {n_pages} pages as usage knowledge")

            # ── A3a: extract unique Razor constructs ─────────────────────────
            yield stage_event("constructs", "extracting unique Razor constructs")
            with open(PAGE_MAP_PATH, "r", encoding="utf-8") as f:
                page_map = json.load(f)
            extractor = RazorConstructExtractor()
            extractor.scan_page_map(page_map, cfg.dotnet_repo)
            constructs = extractor.unique_constructs()
            summary = extractor.summary()
            yield progress_event("constructs", summary["unique_constructs"],
                                 summary["unique_constructs"],
                                 f"{summary['unique_constructs']} unique constructs "
                                 f"from {summary['total_occurrences']} occurrences")

            if not constructs:
                yield error_event("No Razor constructs found in the page map.")
                return

            # ── A3b: derive rules (Sonnet) ───────────────────────────────────
            yield stage_event("rules", "deriving migration rules (Sonnet)")
            catalogue = build_catalogue_text(components)
            deriver = RuleDeriver(component_catalogue=catalogue)

            rules = []
            total_c = len(constructs)
            # derive_all supports a progress callback, but we want to stream each
            # event out — so iterate manually using derive_one.
            for i, c in enumerate(constructs):
                rules.append(deriver.derive_one(c))
                yield progress_event("rules", i + 1, total_c,
                                     f"{c.family}/{c.kind}")

            # ── save + index ─────────────────────────────────────────────────
            yield stage_event("saving", "saving rules + indexing code store")
            payload = save_rules(rules, RULES_PATH)
            patterns = rules_to_code_patterns(rules)
            index_code_patterns(patterns)

            summary_out = {
                "components": len(components),
                "constructs": len(constructs),
                "rules": len(rules),
                "confidence": payload["summary"],
            }
            yield done_event(summary_out, "setup complete")

        except Exception as e:
            logger.exception("setup failed")
            yield error_event(str(e))

    return StreamingResponse(stream(), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)


# ── GET /api/components ───────────────────────────────────────────────────────

@router.get("/components")
def get_components():
    """
    Return the discovered design-system components.
    Re-discovers from the React repo on demand (components aren't persisted
    to a standalone file — they live in the design store).
    """
    cfg = require_project()
    from app.analysis.component_scanner import discover_components

    components = discover_components(
        react_repo=cfg.react_repo,
        components_dir=cfg.components_dir,
        import_base=cfg.import_base,
    )
    by_tier: dict[str, int] = {}
    for c in components:
        by_tier[c.tier or "untiered"] = by_tier.get(c.tier or "untiered", 0) + 1

    return {
        "count": len(components),
        "by_tier": by_tier,
        "components": [
            {
                "name": c.name,
                "tier": c.tier,
                "import_path": c.import_path,
                "props": c.props,
                "usage": c.usage,
                "description": c.description,
            }
            for c in components
        ],
    }


# ── GET /api/rules ────────────────────────────────────────────────────────────

def _load_rules() -> dict:
    if not os.path.exists(RULES_PATH):
        raise HTTPException(
            status_code=409,
            detail="No rules found. Run POST /api/setup first.",
        )
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@router.get("/rules")
def get_rules():
    """Return all derived migration rules + the confidence summary."""
    return _load_rules()


@router.get("/rules/{kind}")
def get_rule(kind: str):
    """Return one rule by its construct_kind (e.g. 'TextBoxFor')."""
    data = _load_rules()
    for rule in data.get("rules", []):
        if rule.get("construct_kind") == kind:
            return rule
    raise HTTPException(status_code=404, detail=f"Rule not found: {kind}")
