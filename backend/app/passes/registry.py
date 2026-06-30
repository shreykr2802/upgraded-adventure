"""
passes/registry.py
───────────────────
Registers all five migration passes with the orchestrator, in order.

Call register_all_passes() once before constructing/using the Orchestrator.
The orchestrator then drives them via the manifest's layer sequence
(model → controller → layout → component → page).
"""

from __future__ import annotations

import logging

from app.passes.orchestrator import register_pass, PASS_REGISTRY
from app.passes.models_pass import ModelsPass
from app.passes.controllers_pass import ControllersPass
from app.passes.layouts_pass import LayoutsPass
from app.passes.components_pass import ComponentsPass
from app.passes.pages_pass import PagesPass

logger = logging.getLogger(__name__)

ALL_PASSES = [ModelsPass, ControllersPass, LayoutsPass, ComponentsPass, PagesPass]


def register_all_passes():
    """Register every pass. Idempotent — safe to call more than once."""
    PASS_REGISTRY.clear()
    for pass_cls in ALL_PASSES:
        register_pass(pass_cls())
    logger.info("Registered %d passes: %s", len(ALL_PASSES), list(PASS_REGISTRY))
    return PASS_REGISTRY
