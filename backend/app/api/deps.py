"""
api/deps.py
───────────
Single-project state for the API (v1 manages one repo pair at a time).

Holds the project configuration in memory and persists it to a small JSON
file so it survives restarts. Also centralises the on-disk artifact paths
(page_map.json, migration_rules.json, migration_report.json) used by the
read-with-fallback endpoints.

This is deliberately simple — a module-level singleton, no database.
"""

from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# Where API artifacts live. Override with API_WORKDIR env var.
WORKDIR = os.environ.get("API_WORKDIR", os.path.abspath("./.agent_workdir"))
os.makedirs(WORKDIR, exist_ok=True)

_PROJECT_FILE      = os.path.join(WORKDIR, "project.json")
PAGE_MAP_PATH      = os.path.join(WORKDIR, "page_map.json")
RULES_PATH         = os.path.join(WORKDIR, "migration_rules.json")
MIGRATED_DIR       = os.path.join(WORKDIR, "migrated")
MIGRATION_REPORT   = os.path.join(MIGRATED_DIR, "migration_report.json")


@dataclass
class ProjectConfig:
    dotnet_repo: str
    react_repo: str
    direction_from: str = "csharp/cshtml"
    direction_to: str = "react"
    components_dir: str = "src/components"
    pages_dir: str = "src/pages"
    import_base: str | None = None

    def validate(self) -> list[str]:
        """Return a list of problems; empty means valid."""
        problems = []
        if not os.path.isdir(self.dotnet_repo):
            problems.append(f".NET repo not found: {self.dotnet_repo}")
        if not os.path.isdir(self.react_repo):
            problems.append(f"React repo not found: {self.react_repo}")
        return problems


# ── In-memory singleton ───────────────────────────────────────────────────────

_project: ProjectConfig | None = None


def _load_from_disk() -> ProjectConfig | None:
    if os.path.exists(_PROJECT_FILE):
        try:
            with open(_PROJECT_FILE, "r", encoding="utf-8") as f:
                return ProjectConfig(**json.load(f))
        except Exception as e:
            logger.warning("Could not load project file: %s", e)
    return None


def get_project() -> ProjectConfig | None:
    global _project
    if _project is None:
        _project = _load_from_disk()
    return _project


def set_project(cfg: ProjectConfig) -> ProjectConfig:
    global _project
    _project = cfg
    with open(_PROJECT_FILE, "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, indent=2)
    logger.info("Project set: %s → %s", cfg.dotnet_repo, cfg.react_repo)
    return cfg


def require_project() -> ProjectConfig:
    """Raise if no project is configured (used by stage endpoints)."""
    cfg = get_project()
    if cfg is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="No project configured. POST /api/project first.")
    return cfg


# ── Stage-completion status (derived from which artifacts exist) ──────────────

def project_status() -> dict:
    """Report which stages have produced output, for the dashboard stepper."""
    return {
        "analyzed": os.path.exists(PAGE_MAP_PATH),
        "setup_done": os.path.exists(RULES_PATH),
        "has_migrations": os.path.exists(MIGRATION_REPORT),
    }
