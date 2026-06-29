"""
passes/orchestrator.py
──────────────────────
Runs the layered migration: five ordered passes, each one topologically
sorted, each indexing its output before the next pass begins, with a HARD
review gate between passes.

The orchestrator is pass-agnostic — it drives anything implementing the
MigrationPass protocol. Passes register themselves in PASS_REGISTRY.

Control flow:
  for layer in LAYERS:
    items   = pass.discover(ctx)
    ordered = toposort(items by pass.dependencies)
    for item in ordered:
        result = pass.migrate_one(item, ctx)     # retrieve→generate→review
        write files; artifact_store.put(result)  # INDEX before next pass
    → mark layer awaiting_review; STOP            # hard review gate

The caller (CLI or API) calls run_layer() to process the current layer, then
approve_layer() to release the gate and advance. This makes the gate explicit
and the whole thing resumable.
"""

from __future__ import annotations

import os
import logging

from app.passes.base import MigrationPass, PassContext, PassResult, WorkItem
from app.passes.toposort import toposort
from app.passes.artifact_store import artifact_store, LAYERS, MigratedArtifact
from app.passes.manifest import Manifest, LayerState

logger = logging.getLogger(__name__)


# Passes register here. Empty for now — the skeleton runs with no passes and is
# filled in as each pass (models, controllers, ...) is built.
PASS_REGISTRY: dict[str, MigrationPass] = {}


def register_pass(pass_impl: MigrationPass):
    """Register a pass implementation under its layer name."""
    PASS_REGISTRY[pass_impl.layer] = pass_impl
    logger.info("Registered pass: %s", pass_impl.layer)


class Orchestrator:
    def __init__(self, ctx: PassContext, manifest_path: str, records_path: str):
        self.ctx = ctx
        self.manifest_path = manifest_path
        self.records_path = records_path
        self.store = artifact_store()

        # Load or init manifest + records (store = source of truth).
        self.store.load_records(records_path)
        self.manifest = (
            Manifest.load(manifest_path)
            or Manifest.fresh(ctx.output_root)
        )

    # ── Public control surface ────────────────────────────────────────────────

    def current_layer(self) -> str | None:
        return self.manifest.current_layer

    def status(self) -> dict:
        return {
            "current_layer": self.manifest.current_layer,
            "complete": self.manifest.is_complete(),
            "layers": {l: self.manifest.layers[l] for l in LAYERS},
        }

    def run_layer(self, progress=None) -> LayerState:
        """
        Run the current layer to completion (all items), then pause at the
        review gate. Returns the LayerState. Yields no events itself; pass a
        `progress(stage, current, total, message)` callback for streaming.
        """
        layer = self.manifest.current_layer
        if layer is None:
            raise RuntimeError("Migration already complete.")

        pass_impl = PASS_REGISTRY.get(layer)
        if pass_impl is None:
            raise RuntimeError(
                f"No pass registered for layer '{layer}'. "
                f"Registered: {list(PASS_REGISTRY)}"
            )

        st = self.manifest.layer_state(layer)
        st.status = "running"
        self.manifest.set_layer_state(st)
        self.manifest.save(self.manifest_path)

        if progress:
            progress("discover", 0, 0, f"discovering {layer} work items")

        items = pass_impl.discover(self.ctx)

        # Build dependency graph and topologically sort.
        graph = {it.origin: pass_impl.dependencies(it, self.ctx) for it in items}
        sort_result = toposort(graph)
        by_origin = {it.origin: it for it in items}
        ordered_items = [by_origin[o] for o in sort_result.ordered if o in by_origin]

        st.total = len(ordered_items)
        st.cycles = sort_result.cycles
        self.manifest.set_layer_state(st)
        self.manifest.save(self.manifest_path)

        if sort_result.has_cycles:
            logger.warning("Dependency cycles in %s layer: %s", layer, sort_result.cycles)

        # Process each item in dependency order, indexing as we go.
        os.makedirs(self.ctx.output_root, exist_ok=True)
        for i, item in enumerate(ordered_items):
            if progress:
                progress("migrating", i, st.total, item.symbol)
            try:
                result = pass_impl.migrate_one(item, self.ctx)
                self._write_and_index(result)
                if result.error:
                    st.failed.append(item.origin)
            except Exception as e:
                logger.exception("Pass %s failed on %s", layer, item.origin)
                st.failed.append(item.origin)

            st.completed = i + 1
            self.manifest.set_layer_state(st)
            self.manifest.save(self.manifest_path)
            self.store.dump_records(self.records_path)

        # Hard review gate: stop here, await approval.
        st.status = "awaiting_review"
        self.manifest.set_layer_state(st)
        self.manifest.save(self.manifest_path)
        if progress:
            progress("awaiting_review", st.total, st.total,
                     f"{layer} complete — {st.completed - len(st.failed)} ok, "
                     f"{len(st.failed)} failed — awaiting review")
        return st

    def approve_layer(self) -> str | None:
        """
        Release the review gate for the current layer and advance to the next.
        Returns the next layer name, or None if migration is complete.
        """
        layer = self.manifest.current_layer
        if layer is None:
            return None
        st = self.manifest.layer_state(layer)
        if st.status != "awaiting_review":
            raise RuntimeError(
                f"Layer '{layer}' is '{st.status}', not awaiting review."
            )
        st.status = "done"
        self.manifest.set_layer_state(st)

        nxt = self.manifest.next_layer(layer)
        self.manifest.current_layer = nxt
        self.manifest.save(self.manifest_path)
        logger.info("Approved %s → advancing to %s", layer, nxt)
        return nxt

    # ── Internals ──────────────────────────────────────────────────────────────

    def _write_and_index(self, result: PassResult):
        """Write generated files to disk and index the artifact (before next pass)."""
        # Write files under output_root, preserving the pass's output_path folder.
        for fname, code in result.files.items():
            # output_path is the primary file's relative path; others share its dir.
            rel_dir = os.path.dirname(result.output_path)
            target_dir = os.path.join(self.ctx.output_root, rel_dir)
            os.makedirs(target_dir, exist_ok=True)
            with open(os.path.join(target_dir, os.path.basename(fname)), "w",
                      encoding="utf-8") as f:
                f.write(code)

        # Index into the artifact store (this is what later passes retrieve).
        if not result.error:
            self.store.put(result.to_artifact())
