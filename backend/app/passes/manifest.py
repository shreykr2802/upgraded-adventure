"""
passes/manifest.py
──────────────────
The control-flow manifest for the layered migration.

Division of responsibility (confirmed design):
  - manifest.json   → CONTROL FLOW: which layer we're on, what's completed,
                      whether we're paused for a review gate, per-item status.
  - artifact store  → SOURCE OF TRUTH: the actual converted code.

If the manifest is lost, it can be rebuilt from the artifact store records
(every artifact knows its layer + status). The manifest exists so the
orchestrator can resume and enforce review gates without re-querying the store
each time.
"""

from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass, field, asdict

from app.passes.artifact_store import LAYERS

logger = logging.getLogger(__name__)


@dataclass
class LayerState:
    layer: str
    status: str = "pending"          # pending | running | awaiting_review | done
    total: int = 0
    completed: int = 0
    failed: list[str] = field(default_factory=list)      # origins that errored
    cycles: list[list[str]] = field(default_factory=list)  # detected dep cycles


@dataclass
class Manifest:
    output_root: str
    layers: dict = field(default_factory=dict)   # layer → LayerState (as dict)
    current_layer: str | None = None

    @classmethod
    def fresh(cls, output_root: str) -> "Manifest":
        m = cls(output_root=output_root)
        for layer in LAYERS:
            m.layers[layer] = asdict(LayerState(layer=layer))
        m.current_layer = LAYERS[0]
        return m

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: str) -> "Manifest | None":
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)

    # ── Layer state helpers ───────────────────────────────────────────────────

    def layer_state(self, layer: str) -> LayerState:
        return LayerState(**self.layers[layer])

    def set_layer_state(self, state: LayerState):
        self.layers[state.layer] = asdict(state)

    def mark_layer(self, layer: str, status: str):
        st = self.layer_state(layer)
        st.status = status
        self.set_layer_state(st)

    def next_layer(self, layer: str) -> str | None:
        idx = LAYERS.index(layer)
        return LAYERS[idx + 1] if idx + 1 < len(LAYERS) else None

    def is_complete(self) -> bool:
        return all(self.layers[l]["status"] == "done" for l in LAYERS)

    # ── Rebuild from the artifact store (store = source of truth) ─────────────

    @classmethod
    def rebuild_from_store(cls, output_root: str, store) -> "Manifest":
        """
        Reconstruct manifest state purely from what's in the artifact store.
        Used if the manifest file is lost but artifacts exist.
        """
        m = cls.fresh(output_root)
        for layer in LAYERS:
            arts = store.by_layer(layer)
            st = m.layer_state(layer)
            st.completed = len(arts)
            st.total = len(arts)
            st.status = "done" if arts else "pending"
            m.set_layer_state(st)
        # current layer = first non-done
        for layer in LAYERS:
            if m.layers[layer]["status"] != "done":
                m.current_layer = layer
                break
        else:
            m.current_layer = None
        logger.info("Rebuilt manifest from store: current=%s", m.current_layer)
        return m
