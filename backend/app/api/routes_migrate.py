"""
api/routes_migrate.py
─────────────────────
Stage 3 — the layered migration, exposed over HTTP for the UI.

  POST /api/migrate/layer        run the current layer (SSE progress stream)
  POST /api/migrate/approve      approve the current layer's review gate → advance
  GET  /api/migrate/status       per-layer status for the stepper
  GET  /api/migrate/artifacts    list generated artifacts (optional ?layer=)
  GET  /api/migrate/artifact/{origin:path}   one artifact's detail + code

The migration runs layer by layer with a hard review gate between layers, so
the UI drives it as: run layer → review → approve → run next layer.
"""

from __future__ import annotations

import os
import json
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.deps import require_project, PAGE_MAP_PATH, MIGRATED_DIR
from app.api.events import (
    SSE_HEADERS, SSE_MEDIA_TYPE,
    stage_event, progress_event, done_event, error_event,
)
from app.passes.base import PassContext
from app.passes.registry import register_all_passes
from app.passes.orchestrator import Orchestrator
from app.passes.artifact_store import LAYERS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/migrate", tags=["migrate"])

_MANIFEST = os.path.join(MIGRATED_DIR, "manifest.json")
_RECORDS = os.path.join(MIGRATED_DIR, "artifact_records.json")


def _build_orchestrator() -> Orchestrator:
    cfg = require_project()
    if not os.path.exists(PAGE_MAP_PATH):
        raise HTTPException(status_code=409, detail="No page map. Run /api/analyze first.")
    with open(PAGE_MAP_PATH, "r", encoding="utf-8") as f:
        page_map = json.load(f)
    os.makedirs(MIGRATED_DIR, exist_ok=True)
    ctx = PassContext(
        dotnet_repo=cfg.dotnet_repo,
        react_repo=cfg.react_repo,
        page_map=page_map,
        output_root=MIGRATED_DIR,
        import_base=cfg.import_base,
    )
    register_all_passes()
    return Orchestrator(ctx, _MANIFEST, _RECORDS)


# ── POST /api/migrate/layer (SSE) ─────────────────────────────────────────────

@router.post("/layer")
def run_layer():
    """Run the current layer, streaming per-item progress. Stops at review gate."""
    orch = _build_orchestrator()
    layer = orch.current_layer()
    if layer is None:
        raise HTTPException(status_code=409, detail="Migration already complete.")

    def stream():
        events: list = []
        try:
            yield stage_event(layer, f"running {layer} pass")

            def progress(stage, current, total, message):
                events.append((stage, current, total, message))

            # run_layer is synchronous; we collect progress then emit. For true
            # streaming we re-implement the loop here would duplicate logic, so
            # we run and then flush events, plus a final done.
            st = orch.run_layer(progress=progress)

            for (stage, current, total, message) in events:
                if stage == "migrating" and total:
                    yield progress_event(layer, current, total, message)
                else:
                    yield stage_event(layer, message)

            yield done_event({
                "layer": layer,
                "total": st.total,
                "completed": st.completed,
                "failed": st.failed,
                "cycles": st.cycles,
                "status": st.status,
                "next_action": "approve",
            }, f"{layer} complete — awaiting review")
        except Exception as e:
            logger.exception("migrate layer failed")
            yield error_event(str(e))

    return StreamingResponse(stream(), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)


# ── POST /api/migrate/approve ─────────────────────────────────────────────────

@router.post("/approve")
def approve_layer():
    """Approve the current layer's review gate and advance to the next."""
    orch = _build_orchestrator()
    try:
        nxt = orch.approve_layer()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"approved": True, "next_layer": nxt, "complete": nxt is None}


# ── GET /api/migrate/status ───────────────────────────────────────────────────

@router.get("/status")
def status():
    """Per-layer status for the stepper."""
    orch = _build_orchestrator()
    return orch.status()


# ── GET /api/migrate/artifacts ────────────────────────────────────────────────

@router.get("/artifacts")
def list_artifacts(layer: str | None = Query(None)):
    """List generated artifacts from the records file (optional ?layer=)."""
    if not os.path.exists(_RECORDS):
        return {"count": 0, "artifacts": []}
    with open(_RECORDS, "r", encoding="utf-8") as f:
        data = json.load(f)
    arts = data.get("artifacts", [])
    if layer:
        arts = [a for a in arts if a.get("layer") == layer]
    return {
        "count": len(arts),
        "artifacts": [
            {"origin": a["origin"], "symbol": a["symbol"], "layer": a["layer"],
             "output_path": a["output_path"], "status": a.get("status")}
            for a in arts
        ],
    }


@router.get("/artifact/{origin:path}")
def get_artifact(origin: str):
    """One artifact's full detail including generated code."""
    if not os.path.exists(_RECORDS):
        raise HTTPException(status_code=404, detail="No artifacts yet.")
    with open(_RECORDS, "r", encoding="utf-8") as f:
        data = json.load(f)
    for a in data.get("artifacts", []):
        if a["origin"] == origin:
            return a
    raise HTTPException(status_code=404, detail=f"Artifact not found: {origin}")
