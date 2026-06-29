"""
app/main.py
───────────
FastAPI application for the UI Migration Agent.

This is the clean, layered-architecture entry point. It mounts the UI-facing
API for the stages that are stable:

  /api/project   — configure the repo pair + direction
  /api/analyze   — Stage 1: analyze the .NET repo → page_map.json (SSE)
  /api/setup     — Stage 2: scan React + derive rules + seed stores (SSE)
  /api/health    — gateway + store reachability

The migration stage is being (re)built on the layered passes architecture
(app/passes/*) and its routes will be mounted here once the passes land.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("UI Migration Agent API starting up")
    yield
    logger.info("UI Migration Agent API shutting down")


app = FastAPI(
    title="UI Migration Agent",
    description="Migrates a .NET/CSHTML codebase into React/TypeScript, layer by layer.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],  # CRA + Vite dev servers
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Mount the UI-facing API ───────────────────────────────────────────────────
from app.api.routes_project import router as project_router
from app.api.routes_analyze import router as analyze_router
from app.api.routes_setup import router as setup_router

app.include_router(project_router)
app.include_router(analyze_router)
app.include_router(setup_router)


@app.get("/", tags=["infra"])
def root():
    return {
        "service": "ui-migration-agent",
        "version": "2.0.0",
        "stages": ["project", "analyze", "setup", "migrate (passes — in progress)"],
    }
