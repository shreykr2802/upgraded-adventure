"""
analysis/engine.py
──────────────────
Engine abstraction for .NET analysis.

Two engines produce the SAME page_map.json contract:
  - "regex"  → DotNetGrapher (pure Python, no dependencies, approximate)
  - "roslyn" → the .NET sidecar (Roslyn + Razor.Language, accurate, needs SDK)

Everything downstream (assembler, rule_deriver, migrator, the API) reads
page_map.json and is unaware of which engine produced it. This module is the
single seam where the choice is made.

Contract (page_map.json):
  {
    "repo": "<abs path>",
    "pages": [ { page_name, entry_view, partials[], layout, controller,
                 controller_action, model, nested_models[], unresolved[] }, ... ],
    "unresolved_count": <int>,
    "unresolved_breakdown": { total, by_kind, by_reason, ... }
  }
"""

from __future__ import annotations

import os
import json
import shutil
import logging
import subprocess
from collections import defaultdict

logger = logging.getLogger(__name__)

# Path to the sidecar project (set via env or default to repo-relative location)
SIDECAR_DIR = os.environ.get(
    "ROSLYN_SIDECAR_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "sidecar"),
)


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_repo(
    dotnet_repo: str,
    engine: str = "regex",
    out_path: str | None = None,
    sln_path: str | None = None,
) -> dict:
    """
    Analyse a .NET repo and return a page_map dict (also written to out_path).

    Args:
        dotnet_repo: Path to the .NET repo root.
        engine:      "regex" (Python grapher) or "roslyn" (.NET sidecar).
        out_path:    Where to write page_map.json (optional).
        sln_path:    Path to the .sln (roslyn engine only; auto-discovered if None).

    Returns:
        The page_map dict.
    """
    engine = (engine or "regex").lower()

    if engine == "roslyn":
        page_map = _run_roslyn(dotnet_repo, sln_path)
    elif engine == "regex":
        page_map = _run_regex(dotnet_repo)
    else:
        raise ValueError(f"Unknown engine: {engine!r} (use 'regex' or 'roslyn')")

    # Validate + normalise the contract regardless of engine
    page_map = _normalise(page_map, dotnet_repo)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(page_map, f, indent=2)
        logger.info("Wrote page map (%s engine) → %s", engine, out_path)

    return page_map


def available_engines() -> dict:
    """Report which engines are usable in this environment."""
    return {
        "regex": True,  # always available
        "roslyn": _dotnet_available() and os.path.isdir(SIDECAR_DIR),
    }


# ── Regex engine (existing Python grapher) ────────────────────────────────────

def _run_regex(dotnet_repo: str) -> dict:
    from app.analysis.dotnet_grapher import DotNetGrapher
    g = DotNetGrapher(dotnet_repo)
    clusters = g.map_repo()
    pages = []
    for c in clusters:
        pages.append({
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
        })
    return {
        "repo": os.path.abspath(dotnet_repo),
        "pages": pages,
        "unresolved_breakdown": g.unresolved_breakdown(clusters),
    }


# ── Roslyn engine (.NET sidecar) ──────────────────────────────────────────────

def _dotnet_available() -> bool:
    return shutil.which("dotnet") is not None


def _discover_sln(dotnet_repo: str) -> str | None:
    """Find a .sln file in the repo (prefer the shallowest one)."""
    candidates = []
    for dirpath, _, filenames in os.walk(dotnet_repo):
        if any(skip in dirpath for skip in ("bin", "obj", ".git", "node_modules")):
            continue
        for fn in filenames:
            if fn.endswith(".sln"):
                full = os.path.join(dirpath, fn)
                depth = full.count(os.sep)
                candidates.append((depth, full))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _run_roslyn(dotnet_repo: str, sln_path: str | None) -> dict:
    if not _dotnet_available():
        raise RuntimeError(
            "The 'roslyn' engine needs the .NET SDK (dotnet) on PATH. "
            "Install it or use --engine regex."
        )
    if not os.path.isdir(SIDECAR_DIR):
        raise RuntimeError(
            f"Roslyn sidecar not found at {SIDECAR_DIR}. "
            f"Set ROSLYN_SIDECAR_DIR or build the sidecar project."
        )

    sln = sln_path or _discover_sln(dotnet_repo)
    if not sln:
        raise RuntimeError(
            f"No .sln found under {dotnet_repo}. Pass --sln explicitly."
        )

    import tempfile
    out_file = os.path.join(tempfile.gettempdir(), "roslyn_page_map.json")

    cmd = [
        "dotnet", "run", "--project", SIDECAR_DIR, "-c", "Release", "--",
        "--sln", sln,
        "--repo", os.path.abspath(dotnet_repo),
        "--out", out_file,
    ]
    logger.info("Running Roslyn sidecar: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError(
            f"Roslyn sidecar failed (exit {result.returncode}):\n{result.stderr[:1000]}"
        )
    if not os.path.exists(out_file):
        raise RuntimeError("Roslyn sidecar did not produce output file.")

    with open(out_file, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Normalisation (guarantee the contract) ────────────────────────────────────

_PAGE_FIELDS = {
    "page_name": "", "entry_view": "", "partials": list,
    "layout": None, "controller": None, "controller_action": None,
    "model": None, "nested_models": list, "unresolved": list,
}


def _normalise(page_map: dict, dotnet_repo: str) -> dict:
    """
    Ensure the page_map matches the contract exactly, filling any missing
    fields and (re)computing unresolved_count + breakdown so both engines
    yield identical structure.
    """
    pages = page_map.get("pages", [])
    norm_pages = []
    for p in pages:
        np = {}
        for field, default in _PAGE_FIELDS.items():
            val = p.get(field)
            if val is None and default is list:
                val = []
            elif val is None and default == "":
                val = ""
            np[field] = val
        # normalise unresolved entries
        np["unresolved"] = [
            {
                "kind": u.get("kind", "unknown"),
                "reference": u.get("reference", ""),
                "source_file": u.get("source_file", ""),
                "reason": u.get("reason", ""),
            }
            for u in (np.get("unresolved") or [])
        ]
        norm_pages.append(np)

    # (re)compute breakdown so it's consistent across engines
    by_kind: dict[str, int] = defaultdict(int)
    by_reason: dict[str, int] = defaultdict(int)
    pages_with = 0
    total = 0
    for p in norm_pages:
        if p["unresolved"]:
            pages_with += 1
        for u in p["unresolved"]:
            by_kind[u["kind"]] += 1
            by_reason[u["reason"]] += 1
            total += 1

    return {
        "repo": page_map.get("repo", os.path.abspath(dotnet_repo)),
        "engine": page_map.get("engine"),
        "pages": norm_pages,
        "unresolved_count": total,
        "unresolved_breakdown": {
            "total": total,
            "by_kind": dict(sorted(by_kind.items(), key=lambda x: -x[1])),
            "by_reason": dict(sorted(by_reason.items(), key=lambda x: -x[1])),
            "pages_with_unresolved": pages_with,
            "pages_total": len(norm_pages),
        },
    }
