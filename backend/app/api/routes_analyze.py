"""
api/routes_analyze.py
─────────────────────
Stage 1 — Analyze the .NET repo into a page map.

  POST /api/analyze                 run the grapher (SSE progress stream)
  GET  /api/pagemap                 the resolved page clusters
  GET  /api/pagemap/unresolved      unresolved items + breakdown (?reason= filter)
  GET  /api/pagemap/page/{name}     one page's full cluster detail

Read endpoints follow "read JSON, fall back to recompute":
  - first try the saved page_map.json
  - if missing, recompute from the grapher on the fly
"""

from __future__ import annotations

import os
import json
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.deps import require_project, PAGE_MAP_PATH
from app.api.events import (
    SSE_HEADERS, SSE_MEDIA_TYPE,
    stage_event, progress_event, done_event, error_event,
)
from app.analysis.dotnet_grapher import DotNetGrapher, PageCluster

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["analyze"])


# ── Serialisation ─────────────────────────────────────────────────────────────

def _cluster_to_dict(c: PageCluster) -> dict:
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
            {"kind": u.kind, "reference": u.reference,
             "source_file": u.source_file, "reason": u.reason}
            for u in c.unresolved
        ],
    }


def _build_page_map(grapher: DotNetGrapher, clusters: list[PageCluster]) -> dict:
    return {
        "repo": grapher.repo_root,
        "pages": [_cluster_to_dict(c) for c in clusters],
        "unresolved_count": len(grapher.collect_unresolved(clusters)),
        "unresolved_breakdown": grapher.unresolved_breakdown(clusters),
    }


# ── POST /api/analyze (SSE) ───────────────────────────────────────────────────

@router.post("/analyze")
def analyze():
    """
    Run the .NET grapher over the configured repo, streaming progress as SSE.
    Writes page_map.json on completion; the final `done` event carries a summary.
    """
    cfg = require_project()

    def stream():
        try:
            yield stage_event("scanning", "scanning .NET repository")
            grapher = DotNetGrapher(cfg.dotnet_repo)
            grapher.scan()
            total_views = len(grapher._cshtml)
            yield progress_event("scanning", total_views, total_views,
                                 f"indexed {total_views} views, {len(grapher._cs)} code files")

            yield stage_event("resolving", "resolving page clusters")
            # Resolve pages one at a time so we can emit progress.
            entry_views = [
                rel for rel in sorted(grapher._cshtml)
                if not grapher._is_partial_filename(os.path.basename(rel))
            ] if hasattr(grapher, "_is_partial_filename") else [
                rel for rel in sorted(grapher._cshtml)
                if not os.path.basename(rel).startswith("_")
            ]

            clusters: list[PageCluster] = []
            total = len(entry_views)
            for i, rel in enumerate(entry_views):
                try:
                    clusters.append(grapher.resolve_page(rel))
                except Exception as e:
                    logger.warning("resolve failed for %s: %s", rel, e)
                if (i + 1) % 5 == 0 or (i + 1) == total:
                    yield progress_event("resolving", i + 1, total,
                                         f"resolved {i + 1}/{total} pages")

            yield stage_event("saving", "writing page map")
            page_map = _build_page_map(grapher, clusters)
            with open(PAGE_MAP_PATH, "w", encoding="utf-8") as f:
                json.dump(page_map, f, indent=2)

            summary = {
                "pages": len(clusters),
                "unresolved_count": page_map["unresolved_count"],
                "unresolved_breakdown": page_map["unresolved_breakdown"],
            }
            yield done_event(summary, "analysis complete")

        except Exception as e:
            logger.exception("analyze failed")
            yield error_event(str(e))

    return StreamingResponse(stream(), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)


# ── Read endpoints (JSON with recompute fallback) ─────────────────────────────

def _load_or_compute_page_map() -> dict:
    if os.path.exists(PAGE_MAP_PATH):
        with open(PAGE_MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    # Fallback: recompute (no streaming, blocking — only hit if file absent)
    cfg = require_project()
    grapher = DotNetGrapher(cfg.dotnet_repo)
    clusters = grapher.map_repo()
    page_map = _build_page_map(grapher, clusters)
    with open(PAGE_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(page_map, f, indent=2)
    return page_map


@router.get("/pagemap")
def get_pagemap():
    """Return the full resolved page map (pages + unresolved summary)."""
    return _load_or_compute_page_map()


@router.get("/pagemap/unresolved")
def get_unresolved(reason: str | None = Query(None, description="Filter by reason substring")):
    """
    Return only the unresolved items + the breakdown.
    Optional ?reason= filters to items whose reason contains that substring.
    """
    page_map = _load_or_compute_page_map()
    items = []
    for page in page_map.get("pages", []):
        for u in page.get("unresolved", []):
            if reason and reason.lower() not in u.get("reason", "").lower():
                continue
            items.append({**u, "page_name": page.get("page_name")})
    return {
        "breakdown": page_map.get("unresolved_breakdown", {}),
        "count": len(items),
        "items": items,
    }


@router.get("/pagemap/page/{page_name:path}")
def get_page(page_name: str):
    """Return one page's full cluster detail by page_name."""
    page_map = _load_or_compute_page_map()
    for page in page_map.get("pages", []):
        if page.get("page_name") == page_name:
            return page
    raise HTTPException(status_code=404, detail=f"Page not found: {page_name}")
