"""
analysis/component_scanner.py
─────────────────────────────
Importable component discovery — wraps react-docgen-typescript to extract
React component structure (name, props, types) into ComponentDoc objects.

This is the structural half of A2. The semantic half (Haiku usage sentences)
lives in component_semantics.py. Keeping discovery importable lets both the
CLI (extract_components.py) and the orchestrator (agent_setup.py) use it.

Atomic tier (atom/molecule/organism) is inferred from the folder path.
"""

from __future__ import annotations

import os
import glob
import json
import subprocess
import logging

from app.rag.indexer import ComponentDoc

logger = logging.getLogger(__name__)


# ── Tier + import path inference ──────────────────────────────────────────────

def infer_tier(file_path: str) -> str | None:
    p = file_path.lower()
    if "/atoms/" in p or "\\atoms\\" in p:
        return "atom"
    if "/molecules/" in p or "\\molecules\\" in p:
        return "molecule"
    if "/organisms/" in p or "\\organisms\\" in p:
        return "organism"
    return None


def infer_import_path(file_path: str, repo_root: str, import_base: str | None) -> str:
    tier = infer_tier(file_path)
    if import_base:
        return f"{import_base}/{tier}s" if tier else import_base
    rel = os.path.relpath(file_path, repo_root)
    return os.path.splitext(rel)[0].replace(os.sep, "/")


# ── docgen invocation ─────────────────────────────────────────────────────────

def run_docgen(component_glob: str, tsconfig: str | None = None) -> list[dict]:
    """
    Run react-docgen-typescript over component files (tries npx, then global).
    Returns parsed JSON array, or [] on failure.

    The CLI requires a tsconfig.json. If `tsconfig` isn't given, it looks for
    one in the repo (the tool defaults to ./tsconfig.json in the cwd).
    """
    files = [
        f for f in glob.glob(component_glob, recursive=True)
        if not any(skip in f for skip in [".test.", ".spec.", ".stories.", ".d.ts"])
    ]
    if not files:
        logger.warning("No component files matched: %s", component_glob)
        return []

    logger.info("Parsing %d component files with react-docgen-typescript-cli", len(files))

    config_args = ["--config", tsconfig] if tsconfig else []

    # Correct CLI binary name is `react-docgen-typescript-cli`.
    cmds = [
        ["react-docgen-typescript-cli", *files, *config_args],
        ["npx", "--yes", "react-docgen-typescript-cli", *files, *config_args],
    ]
    last_err = None
    for cmd in cmds:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0 and result.stdout.strip():
                # The CLI prints "Processing file: ..." lines before the JSON.
                # Slice from the first '{' to the last '}' to isolate the JSON.
                out = result.stdout
                start = out.find("{")
                end = out.rfind("}")
                if start != -1 and end != -1:
                    return json.loads(out[start:end + 1])
                last_err = "no JSON object found in docgen output"
                continue
            last_err = result.stderr[:300]
        except FileNotFoundError:
            last_err = f"{cmd[0]} not found"
            continue
        except subprocess.TimeoutExpired:
            last_err = "docgen timed out"
            break
        except json.JSONDecodeError as e:
            last_err = f"could not parse docgen JSON: {e}"
            break
    logger.error("react-docgen-typescript-cli failed: %s", last_err)
    logger.error("Install with: npm install -g react-docgen-typescript-cli")
    return []


# ── Mapping to ComponentDoc ───────────────────────────────────────────────────

def props_to_string(props: dict) -> str:
    parts = []
    for name, meta in (props or {}).items():
        type_name = meta.get("type", {}).get("name", "any")
        optional = "" if meta.get("required", False) else "?"
        parts.append(f"{name}{optional}: {type_name}")
    return "; ".join(parts)


def build_usage(name: str, props: dict) -> str:
    required = [n for n, m in (props or {}).items() if m.get("required", False)][:3]
    attrs = " ".join(f"{p}={{...}}" for p in required)
    return f"<{name} {attrs} />" if attrs else f"<{name} />"


def to_component_doc(name: str, entry: dict, file_path: str, repo_root: str, import_base: str | None) -> ComponentDoc | None:
    """
    Map one react-docgen-typescript-cli component entry to a ComponentDoc.

    The CLI emits an object keyed by component name:
        { "TextInput": { "description": "...", "props": {...} }, ... }
    The source file path is found inside each prop's `declarations[].fileName`.
    """
    if not name:
        return None
    props = entry.get("props", {})
    description = (entry.get("description") or "").strip() or f"{name} component."
    return ComponentDoc(
        name=name,
        import_path=infer_import_path(file_path, repo_root, import_base),
        props=props_to_string(props),
        usage=build_usage(name, props),
        description=description,
        tier=infer_tier(file_path),
        tags=None,
    )


def _entry_file_path(entry: dict) -> str:
    """Pull the source file path out of a docgen entry's prop declarations."""
    for meta in (entry.get("props") or {}).values():
        decls = meta.get("declarations") or []
        if decls and decls[0].get("fileName"):
            return decls[0]["fileName"]
    return ""


# ── Public entry point ────────────────────────────────────────────────────────

def discover_components(
    react_repo: str,
    components_dir: str = "src/components",
    import_base: str | None = None,
    tsconfig: str | None = None,
) -> list[ComponentDoc]:
    """
    Discover all React components and return ComponentDoc objects.
    Structural only — call enrich_component_semantics() afterwards for
    the Haiku usage sentences.

    Args:
        tsconfig: Path to tsconfig.json. Defaults to <react_repo>/tsconfig.json
                  if present (react-docgen-typescript-cli requires one).
    """
    repo_root = os.path.abspath(react_repo)
    comp_dir = os.path.join(repo_root, components_dir)
    component_glob = os.path.join(comp_dir, "**", "*.tsx")

    if tsconfig is None:
        default_tsconfig = os.path.join(repo_root, "tsconfig.json")
        tsconfig = default_tsconfig if os.path.exists(default_tsconfig) else None

    raw = run_docgen(component_glob, tsconfig=tsconfig)

    # docgen-cli returns an object keyed by component name (not an array).
    # Some versions may wrap per-file; normalise both shapes to {name: entry}.
    docs: list[ComponentDoc] = []
    if isinstance(raw, dict):
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            file_path = _entry_file_path(entry)
            doc = to_component_doc(name, entry, file_path, repo_root, import_base)
            if doc:
                docs.append(doc)
    elif isinstance(raw, list):
        # Fallback for array-style output
        for entry in raw:
            name = entry.get("displayName") or entry.get("name", "")
            file_path = entry.get("filePath") or _entry_file_path(entry)
            doc = to_component_doc(name, entry, file_path, repo_root, import_base)
            if doc:
                docs.append(doc)

    logger.info("Discovered %d components", len(docs))
    return docs


def build_catalogue_text(components: list[ComponentDoc]) -> str:
    """
    Build a compact text catalogue of components for the rule-derivation agent.
    One block per component: name, tier, props, description.
    """
    blocks = []
    for c in components:
        blocks.append(
            f"- {c.name} (tier: {c.tier or 'n/a'})\n"
            f"  props: {c.props}\n"
            f"  use: {c.description}"
        )
    return "\n".join(blocks)
