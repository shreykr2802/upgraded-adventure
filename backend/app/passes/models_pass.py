"""
passes/models_pass.py
─────────────────────
Pass 1 — Convert C# model classes into TypeScript interfaces.

  discover()      → every model .cs class in the .NET repo
  dependencies()  → the other model classes this one references (nested types,
                    base classes) so they convert first (topological order)
  migrate_one()   → parse the C# class, retrieve any already-converted
                    dependency interfaces, ask Sonnet to emit a clean TS
                    interface, review it

Output lands in  types/<Name>.ts  and is indexed in the artifact store so the
controller/component/page passes can import the real interfaces later.
"""

from __future__ import annotations

import os
import json
import logging

from app.passes.base import WorkItem, PassResult, PassContext
from app.passes.csharp_parser import parse_class, ClassInfo
from app.passes.artifact_store import artifact_store
from app.services import generate_component, review_component

logger = logging.getLogger(__name__)


GENERATE_SYSTEM = """
You convert a single C# model/DTO class into a clean TypeScript interface for a
React + TypeScript codebase.

Rules:
1. Emit ONE exported interface named exactly after the class.
2. Map C# types to idiomatic TS:
   string→string, int/long/decimal/double/float→number, bool→boolean,
   DateTime/DateTimeOffset→string (ISO), Guid→string,
   List<T>/IEnumerable<T>/T[]→T[], Dictionary<K,V>→Record<K, V>,
   nullable (T?) → optional property (name?: T) .
3. For referenced model types, use the interface name directly and add an
   import from its sibling file: import type { X } from './X';
   Only import types that appear in REFERENCE INTERFACES.
4. Preserve property names but convert to camelCase (C# PascalCase → camelCase).
5. No classes, no decorators, no logic — a pure interface plus imports.

Reply ONLY with valid JSON, no markdown:
{
  "filename": "<Name>.ts",
  "code": "<the full TypeScript file content>",
  "imports": ["<referenced interface names you imported>"],
  "todos": ["<anything uncertain that needs review>"]
}
""".strip()

REVIEW_SYSTEM = """
You review a generated TypeScript interface produced from a C# model.
Check: valid TS syntax, camelCase properties, correct type mapping, imports
resolve to real referenced interfaces, no leftover C# syntax.

Reply ONLY with valid JSON:
{"valid": true|false, "issues": ["..."], "confidence": "high"|"medium"|"low"}
""".strip()


class ModelsPass:
    layer = "model"

    # ── discover ──────────────────────────────────────────────────────────────

    def discover(self, ctx: PassContext) -> list[WorkItem]:
        items: list[WorkItem] = []
        for cs_path in _iter_cs_files(ctx.dotnet_repo):
            try:
                with open(cs_path, "r", encoding="utf-8", errors="replace") as f:
                    src = f.read()
            except OSError:
                continue
            # A model file is one that declares a class and is NOT a controller.
            if "Controller" in os.path.basename(cs_path):
                continue
            ci = parse_class(src)
            if ci is None or _looks_like_controller(ci):
                continue
            # Heuristic: skip classes with no properties (often interfaces/enums/services)
            if not ci.properties:
                continue
            rel = os.path.relpath(cs_path, ctx.dotnet_repo)
            items.append(WorkItem(
                origin=rel,
                symbol=ci.name,
                source_path=cs_path,
                extra={"class_info": ci},
            ))
        logger.info("Models pass discovered %d model classes", len(items))
        return items

    # ── dependencies ──────────────────────────────────────────────────────────

    def dependencies(self, item: WorkItem, ctx: PassContext) -> list[str]:
        ci: ClassInfo = item.extra["class_info"]
        # Map dependency type names → their origin (rel path) if we discovered them.
        dep_origins: list[str] = []
        name_to_origin = _build_name_index(ctx)
        for dep_type in ci.dependency_types():
            origin = name_to_origin.get(dep_type)
            if origin and origin != item.origin:
                dep_origins.append(origin)
        return dep_origins

    # ── migrate one ───────────────────────────────────────────────────────────

    def migrate_one(self, item: WorkItem, ctx: PassContext) -> PassResult:
        ci: ClassInfo = item.extra["class_info"]
        store = artifact_store()

        # Gather already-converted dependency interfaces as grounding context.
        ref_blocks = []
        for dep_type in ci.dependency_types():
            art = _find_artifact_by_symbol(store, dep_type)
            if art:
                ref_blocks.append(f"// {art.output_path}\n{art.output_code}")
        ref_context = "\n\n".join(ref_blocks) if ref_blocks else "(none)"

        user_msg = (
            f"C# CLASS ({item.origin}):\n{ci.raw}\n\n"
            f"REFERENCE INTERFACES (already converted, import from these):\n{ref_context}"
        )

        try:
            resp = generate_component(
                messages=[{"role": "user", "content": user_msg}],
                system=GENERATE_SYSTEM,
                max_tokens=1500,
                temperature=0.0,
            )
            parsed = _parse_json(resp.text)
            if not parsed or "code" not in parsed:
                return _fail(item, "generation_parse_failed",
                             resp.usage if hasattr(resp, "usage") else {})

            filename = parsed.get("filename") or f"{item.symbol}.ts"
            code = parsed["code"]
            todos = parsed.get("todos", [])
            imports = parsed.get("imports", [])

            review = review_component(generated_tsx=code)
            rparsed = _parse_json(review.text) or {}

            return PassResult(
                origin=item.origin,
                symbol=item.symbol,
                layer=self.layer,
                output_path=f"types/{filename}",
                files={filename: code},
                depends_on=self.dependencies(item, ctx),
                todos=todos,
                review_valid=bool(rparsed.get("valid", True)),
                review_issues=rparsed.get("issues", []),
                confidence=rparsed.get("confidence", "medium"),
                token_usage=_merge_usage(resp, review),
            )
        except Exception as e:
            logger.exception("Model migration failed for %s", item.origin)
            return _fail(item, str(e), {})


# ── module-level helpers (shared by passes) ───────────────────────────────────

def _iter_cs_files(repo: str):
    for dirpath, _, filenames in os.walk(repo):
        if any(s in dirpath for s in ("bin", "obj", ".git", "node_modules")):
            continue
        for fn in filenames:
            if fn.endswith(".cs"):
                yield os.path.join(dirpath, fn)


_NAME_INDEX_CACHE: dict[str, dict[str, str]] = {}


def _build_name_index(ctx: PassContext) -> dict[str, str]:
    """Map every model class name → its origin rel path (cached per repo)."""
    if ctx.dotnet_repo in _NAME_INDEX_CACHE:
        return _NAME_INDEX_CACHE[ctx.dotnet_repo]
    index: dict[str, str] = {}
    for cs_path in _iter_cs_files(ctx.dotnet_repo):
        if "Controller" in os.path.basename(cs_path):
            continue
        try:
            with open(cs_path, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        ci = parse_class(src)
        if ci and ci.properties:
            index[ci.name] = os.path.relpath(cs_path, ctx.dotnet_repo)
    _NAME_INDEX_CACHE[ctx.dotnet_repo] = index
    return index


def _looks_like_controller(ci: ClassInfo) -> bool:
    return ci.name.endswith("Controller") or "Controller" in ci.base_types


def _find_artifact_by_symbol(store, symbol: str):
    for art in store.all():
        if art.symbol == symbol:
            return art
    return None


def _parse_json(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e == -1:
        return None
    try:
        return json.loads(raw[s : e + 1])
    except json.JSONDecodeError:
        return None


def _merge_usage(*responses) -> dict:
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for r in responses:
        u = getattr(r, "usage", {}) or {}
        for k in usage:
            usage[k] += u.get(k, 0)
    return usage


def _fail(item: WorkItem, error: str, usage) -> PassResult:
    return PassResult(
        origin=item.origin, symbol=item.symbol, layer="model",
        output_path=f"types/{item.symbol}.ts", files={},
        review_valid=False, confidence="low", error=error,
        token_usage=usage if isinstance(usage, dict) else {},
    )
