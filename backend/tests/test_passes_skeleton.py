"""
tests/test_passes_skeleton.py
─────────────────────────────
Tests for the layered-migration skeleton: toposort, manifest, orchestrator.
No real LLM/store needed — the artifact store's vector put is mocked.

Run:
    cd backend
    pytest tests/test_passes_skeleton.py -v
"""

import os
import pytest
from unittest.mock import patch

from app.passes.base import WorkItem, PassResult, PassContext


# ── toposort ──────────────────────────────────────────────────────────────────

def test_toposort_chain():
    from app.passes.toposort import toposort
    r = toposort({"UserModel": ["AddressModel"], "AddressModel": []})
    assert r.ordered == ["AddressModel", "UserModel"]
    assert not r.has_cycles


def test_toposort_diamond():
    from app.passes.toposort import toposort
    r = toposort({"Page": ["Form", "Header"], "Form": ["Input"],
                  "Header": ["Input"], "Input": []})
    assert r.ordered.index("Input") < r.ordered.index("Form")
    assert r.ordered.index("Form") < r.ordered.index("Page")


def test_toposort_cycle_detected_not_blocking():
    from app.passes.toposort import toposort
    r = toposort({"A": ["B"], "B": ["A"]})
    assert r.has_cycles
    # cyclic nodes still appear in the order (not dropped)
    assert set(r.ordered) == {"A", "B"}


def test_toposort_missing_deps():
    from app.passes.toposort import toposort
    r = toposort({"A": ["External"]})
    assert "External" in r.missing
    assert r.ordered == ["A"]


# ── manifest ──────────────────────────────────────────────────────────────────

def test_manifest_fresh_starts_at_first_layer():
    from app.passes.manifest import Manifest
    from app.passes.artifact_store import LAYERS
    m = Manifest.fresh("/out")
    assert m.current_layer == LAYERS[0]
    assert all(m.layers[l]["status"] == "pending" for l in LAYERS)


def test_manifest_save_load_roundtrip(tmp_path):
    from app.passes.manifest import Manifest
    m = Manifest.fresh("/out")
    m.current_layer = "controller"
    path = str(tmp_path / "manifest.json")
    m.save(path)
    loaded = Manifest.load(path)
    assert loaded.current_layer == "controller"


def test_manifest_next_layer():
    from app.passes.manifest import Manifest
    m = Manifest.fresh("/out")
    assert m.next_layer("model") == "controller"
    assert m.next_layer("page") is None


# ── orchestrator skeleton ─────────────────────────────────────────────────────

class _FakePass:
    layer = "model"

    def discover(self, ctx):
        return [
            WorkItem(origin="Models/UserModel.cs", symbol="UserModel",
                     source_path="x", extra={"deps": ["Models/AddressModel.cs"]}),
            WorkItem(origin="Models/AddressModel.cs", symbol="AddressModel",
                     source_path="x", extra={"deps": []}),
        ]

    def dependencies(self, item, ctx):
        return item.extra.get("deps", [])

    def migrate_one(self, item, ctx):
        return PassResult(
            origin=item.origin, symbol=item.symbol, layer="model",
            output_path=f"types/{item.symbol}.ts",
            files={f"{item.symbol}.ts": f"export interface {item.symbol} {{}}"},
            depends_on=item.extra.get("deps", []), confidence="high",
        )


@pytest.fixture
def orchestrator(tmp_path):
    from app.passes.orchestrator import Orchestrator, register_pass, PASS_REGISTRY
    PASS_REGISTRY.clear()
    register_pass(_FakePass())
    ctx = PassContext(dotnet_repo="/x", react_repo="/y", page_map={},
                      output_root=str(tmp_path / "out"))
    orch = Orchestrator(
        ctx,
        str(tmp_path / "manifest.json"),
        str(tmp_path / "records.json"),
    )
    return orch


def test_orchestrator_starts_at_model(orchestrator):
    assert orchestrator.current_layer() == "model"


def test_orchestrator_runs_in_topo_order(orchestrator):
    with patch("app.passes.artifact_store.ArtifactStore.put"):
        events = []
        orchestrator.run_layer(progress=lambda *a: events.append(a))
    order = [e[3] for e in events if e[0] == "migrating"]
    assert order == ["AddressModel", "UserModel"]


def test_orchestrator_writes_files(orchestrator, tmp_path):
    with patch("app.passes.artifact_store.ArtifactStore.put"):
        orchestrator.run_layer()
    out = tmp_path / "out" / "types"
    assert (out / "UserModel.ts").exists()
    assert (out / "AddressModel.ts").exists()


def test_orchestrator_stops_at_review_gate(orchestrator):
    with patch("app.passes.artifact_store.ArtifactStore.put"):
        st = orchestrator.run_layer()
    assert st.status == "awaiting_review"
    # gate holds — current layer hasn't advanced
    assert orchestrator.current_layer() == "model"


def test_orchestrator_approve_advances(orchestrator):
    with patch("app.passes.artifact_store.ArtifactStore.put"):
        orchestrator.run_layer()
    nxt = orchestrator.approve_layer()
    assert nxt == "controller"
    assert orchestrator.current_layer() == "controller"


def test_orchestrator_approve_before_review_raises(orchestrator):
    # haven't run the layer yet → not awaiting review
    with pytest.raises(RuntimeError, match="not awaiting review"):
        orchestrator.approve_layer()


def test_orchestrator_no_pass_registered_raises(tmp_path):
    from app.passes.orchestrator import Orchestrator, PASS_REGISTRY
    PASS_REGISTRY.clear()
    ctx = PassContext(dotnet_repo="/x", react_repo="/y", page_map={},
                      output_root=str(tmp_path / "out"))
    orch = Orchestrator(ctx, str(tmp_path / "m.json"), str(tmp_path / "r.json"))
    with pytest.raises(RuntimeError, match="No pass registered"):
        orch.run_layer()
