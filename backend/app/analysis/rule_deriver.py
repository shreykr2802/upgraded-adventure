"""
analysis/rule_deriver.py
────────────────────────
The rule-derivation agent (A3). Given:
  - the unique Razor constructs found across the .NET repo, and
  - the React components discovered in the target repo,

it reasons (via Sonnet) about how each construct should map to the React
side, and emits structured migration rules.

This is the step that makes the system an *agent* rather than a pipeline:
the rules are produced by the model reasoning over both codebases, not
hand-written by the developer.

Output: a list of DerivedRule objects → saved to migration_rules.json
(for human review) and indexed into the Code Pattern store.

Uses Sonnet (stronger reasoning) per the chosen configuration.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict

from app.services import generate_component  # Sonnet-backed
from app.analysis.razor_constructs import RazorConstruct

logger = logging.getLogger(__name__)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class DerivedRule:
    construct_family: str          # e.g. "html_helper"
    construct_kind: str            # e.g. "TextBoxFor"
    razor_example: str             # the representative snippet
    react_mapping: str             # how to convert it (component + usage)
    target_component: str | None   # which design-system component to use (if any)
    notes: str                     # caveats, manual-review flags
    confidence: str                # "high" | "medium" | "low"

    def to_dict(self) -> dict:
        return asdict(self)


# ── Prompt ────────────────────────────────────────────────────────────────────

_RULE_SYSTEM = """
You are a migration architect. You are given:
  1. A single Razor (.NET MVC) construct used in a legacy codebase.
  2. The catalogue of available React/TypeScript design-system components
     (names, props, descriptions) in the target codebase.

Your job: decide how this Razor construct should be converted to React using
the AVAILABLE components. Do not invent components that aren't in the catalogue.
If no suitable component exists, say so and propose a plain-React fallback,
and flag it for manual review.

Reply ONLY with valid JSON, no markdown:
{
  "target_component": "<exact component name from catalogue, or null>",
  "react_mapping": "<concise description of how to convert, with a short code example>",
  "notes": "<caveats, edge cases, or manual-review flags>",
  "confidence": "high" | "medium" | "low"
}

Confidence guide:
  high   — a clear, direct component match exists.
  medium — a reasonable match but with caveats (props differ, partial fit).
  low    — no good match; fallback proposed, needs human review.
""".strip()


def _build_user_message(construct: RazorConstruct, component_catalogue: str) -> str:
    return f"""
RAZOR CONSTRUCT TO MAP:
  Family: {construct.family}
  Kind:   {construct.kind}
  Example: {construct.representative}
  (used {construct.occurrences} times across the repo)

AVAILABLE REACT COMPONENTS (catalogue):
{component_catalogue}
""".strip()


# ── Deriver ───────────────────────────────────────────────────────────────────

class RuleDeriver:
    """
    Derives migration rules for a set of unique Razor constructs.

    Usage:
        deriver = RuleDeriver(component_catalogue_text)
        rules = deriver.derive_all(unique_constructs)
    """

    def __init__(self, component_catalogue: str):
        """
        Args:
            component_catalogue: A text catalogue of available React components,
                                 e.g. assembled from the Design System store
                                 (name, props, description per component).
        """
        self.component_catalogue = component_catalogue

    def derive_one(self, construct: RazorConstruct) -> DerivedRule:
        """Derive a single rule for one construct via Sonnet."""
        user_msg = _build_user_message(construct, self.component_catalogue)
        resp = generate_component(
            messages=[{"role": "user", "content": user_msg}],
            system=_RULE_SYSTEM,
            max_tokens=800,
            temperature=0.0,
        )

        raw = resp.text.strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Rule derivation JSON parse failed for %s. Raw: %s",
                           construct.kind, raw[:200])
            return DerivedRule(
                construct_family=construct.family,
                construct_kind=construct.kind,
                razor_example=construct.representative,
                react_mapping="(could not parse model output — review manually)",
                target_component=None,
                notes=f"Parse error. Raw output: {raw[:300]}",
                confidence="low",
            )

        return DerivedRule(
            construct_family=construct.family,
            construct_kind=construct.kind,
            razor_example=construct.representative,
            react_mapping=parsed.get("react_mapping", ""),
            target_component=parsed.get("target_component"),
            notes=parsed.get("notes", ""),
            confidence=parsed.get("confidence", "low"),
        )

    def derive_all(self, constructs: list[RazorConstruct], progress=None) -> list[DerivedRule]:
        """
        Derive rules for all unique constructs.

        Args:
            constructs: Deduplicated list from RazorConstructExtractor.
            progress:   Optional callback(i, total, construct) for UI feedback.
        """
        rules: list[DerivedRule] = []
        total = len(constructs)
        for i, c in enumerate(constructs):
            if progress:
                progress(i + 1, total, c)
            logger.info("Deriving rule %d/%d: %s", i + 1, total, c.kind)
            rules.append(self.derive_one(c))
        return rules


# ── Helpers ───────────────────────────────────────────────────────────────────

def save_rules(rules: list[DerivedRule], path: str):
    """Write derived rules to a JSON file for human review."""
    payload = {
        "rule_count": len(rules),
        "rules": [r.to_dict() for r in rules],
        "summary": {
            "high_confidence":   sum(1 for r in rules if r.confidence == "high"),
            "medium_confidence": sum(1 for r in rules if r.confidence == "medium"),
            "low_confidence":    sum(1 for r in rules if r.confidence == "low"),
            "needs_review":      [r.construct_kind for r in rules if r.confidence == "low"],
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    logger.info("Saved %d rules to %s", len(rules), path)
    return payload


def rules_to_code_patterns(rules: list[DerivedRule]):
    """
    Convert derived rules into CodePattern objects for the Code Pattern store.
    Imported lazily to avoid a hard dependency when only deriving.
    """
    from app.rag.indexer import CodePattern
    patterns = []
    for r in rules:
        patterns.append(CodePattern(
            cshtml_pattern=r.razor_example,
            react_equivalent=r.react_mapping,
            notes=f"[{r.confidence}] {r.notes}".strip(),
            tags=[r.construct_family, r.construct_kind]
                 + ([r.target_component] if r.target_component else []),
        ))
    return patterns
