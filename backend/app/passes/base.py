"""
passes/base.py
──────────────
The contract every migration pass implements, plus shared data types.

Each of the five passes (models, controllers, layouts, components, pages) is a
class implementing MigrationPass. The orchestrator runs them in order without
knowing their internals — it only relies on this interface.

The shape of every pass is the same:
  discover()      → what to convert in this pass (WorkItems)
  dependencies()  → deps of one item (origins), for the topological sort
  migrate_one()   → retrieve prior artifacts → generate → review → PassResult
  index_result()  → write the result into the artifact store

Keeping the interface uniform is what lets us add passes without touching the
orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.passes.artifact_store import MigratedArtifact


@dataclass
class WorkItem:
    """One unit of work in a pass (a model, a controller, a view, ...)."""
    origin: str                      # .NET source path — unique id
    symbol: str                      # the thing's name (UserModel, UserController)
    source_path: str                 # absolute path to read the source
    extra: dict = field(default_factory=dict)   # pass-specific data (e.g. cluster)


@dataclass
class PassResult:
    """The outcome of migrating one WorkItem."""
    origin: str
    symbol: str
    layer: str
    output_path: str
    files: dict                      # {filename: code} — usually one, sometimes more
    depends_on: list[str] = field(default_factory=list)
    todos: list[str] = field(default_factory=list)
    review_valid: bool = True
    review_issues: list[str] = field(default_factory=list)
    confidence: str = "medium"
    token_usage: dict = field(default_factory=dict)
    error: str | None = None

    def primary_code(self) -> str:
        """The main generated file's code (first file)."""
        return next(iter(self.files.values()), "")

    def to_artifact(self) -> MigratedArtifact:
        return MigratedArtifact(
            origin=self.origin,
            layer=self.layer,
            symbol=self.symbol,
            output_path=self.output_path,
            output_code=self.primary_code(),
            depends_on=self.depends_on,
            status="generated",
            notes="; ".join(self.todos[:3]),
        )


@runtime_checkable
class MigrationPass(Protocol):
    """Interface every pass implements. The orchestrator depends only on this."""

    layer: str                       # "model" | "controller" | "layout" | "component" | "page"

    def discover(self, context: "PassContext") -> list[WorkItem]:
        """Find everything this pass should convert."""
        ...

    def dependencies(self, item: WorkItem, context: "PassContext") -> list[str]:
        """Return the origins this item depends on (for the topological sort)."""
        ...

    def migrate_one(self, item: WorkItem, context: "PassContext") -> PassResult:
        """Convert one item: retrieve prior artifacts → generate → review."""
        ...


@dataclass
class PassContext:
    """Everything a pass needs to do its work, passed in by the orchestrator."""
    dotnet_repo: str
    react_repo: str
    page_map: dict                   # the analyzed .NET structure
    output_root: str                 # where generated TS files are written
    import_base: str | None = None
