"""
api/routes_project.py
─────────────────────
Project configuration + health endpoints.

  POST /api/project   set/replace the project (repo paths + direction)
  GET  /api/project   current config + stage-completion status
  GET  /api/health    gateway + store reachability
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import (
    ProjectConfig, get_project, set_project, project_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["project"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ProjectRequest(BaseModel):
    dotnet_repo: str = Field(..., description="Absolute path to the .NET repo")
    react_repo: str = Field(..., description="Absolute path to the React repo")
    direction_from: str = "csharp/cshtml"
    direction_to: str = "react"
    components_dir: str = "src/components"
    pages_dir: str = "src/pages"
    import_base: str | None = None


class ProjectResponse(BaseModel):
    dotnet_repo: str
    react_repo: str
    direction_from: str
    direction_to: str
    components_dir: str
    pages_dir: str
    import_base: str | None
    status: dict


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/project", response_model=ProjectResponse)
def configure_project(req: ProjectRequest):
    """Set or replace the active project. Validates that both repos exist."""
    cfg = ProjectConfig(
        dotnet_repo=req.dotnet_repo,
        react_repo=req.react_repo,
        direction_from=req.direction_from,
        direction_to=req.direction_to,
        components_dir=req.components_dir,
        pages_dir=req.pages_dir,
        import_base=req.import_base,
    )
    problems = cfg.validate()
    if problems:
        raise HTTPException(status_code=400, detail={"problems": problems})

    set_project(cfg)
    return _to_response(cfg)


@router.get("/project", response_model=ProjectResponse)
def read_project():
    """Return the current project config + which stages are complete."""
    cfg = get_project()
    if cfg is None:
        raise HTTPException(status_code=404, detail="No project configured.")
    return _to_response(cfg)


@router.get("/health")
def health():
    """Gateway + vector store reachability."""
    from app.gateway import health as gateway_health
    gateway_ok = gateway_health()

    store_ok = True
    store_detail = "ok"
    try:
        from app.rag.stores import code_store, design_store
        _ = code_store().count() + design_store().count()
    except Exception as e:
        store_ok = False
        store_detail = str(e)[:160]

    overall = "ok" if (gateway_ok and store_ok) else "degraded"
    return {
        "status": overall,
        "gateway": "reachable" if gateway_ok else "unreachable",
        "store": store_detail if not store_ok else "reachable",
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_response(cfg: ProjectConfig) -> ProjectResponse:
    return ProjectResponse(
        dotnet_repo=cfg.dotnet_repo,
        react_repo=cfg.react_repo,
        direction_from=cfg.direction_from,
        direction_to=cfg.direction_to,
        components_dir=cfg.components_dir,
        pages_dir=cfg.pages_dir,
        import_base=cfg.import_base,
        status=project_status(),
    )
