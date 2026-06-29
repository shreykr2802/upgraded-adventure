# .NET Analysis Sidecar (Roslyn + Razor)

An optional, more accurate analysis engine for the UI Migration Agent.

The Python `dotnet_grapher.py` uses regex — fast, no dependencies, but
approximate. This sidecar uses **Roslyn** (the C# compiler API) and the
**Razor language engine** to analyse the .NET solution semantically. It emits
the exact same `page_map.json` contract, so it's a drop-in swap — nothing
downstream changes.

## When to use it

Use the sidecar (`--engine roslyn`) when the regex engine leaves too many
unresolved items, especially:

| Problem | Regex | Roslyn sidecar |
|---|---|---|
| `return View("Edit")` | ✅ | ✅ |
| `return View(model)` model type | approximate | ✅ exact (semantic model) |
| `return View(someVar)` dynamic | ❌ flagged | ✅ traced via data-flow |
| `List<UserModel>` model unwrap | string-stripped | ✅ real generic resolution |
| inherited / partial model classes | regex match | ✅ symbol resolution |
| nested model types | shallow | ✅ via property symbols |

## Requirements

- .NET SDK 8.0+ (`dotnet --version`)
- The solution must build (or at least load) — Roslyn opens the `.sln` via
  MSBuildWorkspace.

## Build

```bash
cd sidecar
dotnet build -c Release
```

## Run standalone

```bash
dotnet run -c Release -- \
    --sln /path/to/YourApp.sln \
    --repo /path/to/repo \
    --out page_map.json
```

## Run through the agent (recommended)

```bash
cd backend
python ../scripts/analyze_dotnet.py \
    --repo /path/to/repo \
    --engine roslyn \
    --out ../page_map.json
# --sln is auto-discovered; pass --sln explicitly if there are several
```

The Python side (`app/analysis/engine.py`) shells out to this sidecar, then
normalises the output to guarantee the `page_map.json` contract (recomputing
`unresolved_count` and `unresolved_breakdown` so both engines are identical).

## Files

- `Program.cs` — entry point; loads the solution, orchestrates the passes
- `ControllerIndex.cs` — **the Roslyn core**: resolves actions → views → models,
  including data-flow tracing for dynamic view names
- `RazorAnalyzer.cs` — resolves partials, layout (incl. `_ViewStart` chain),
  and EditorFor templates from the parsed Razor view
- `RazorWalker.cs` — extracts directives/helper calls from the Razor tree
- `Models.cs` — output shapes that serialise to the `page_map.json` contract

## Honest limitations

- **Razor helper extraction** still scans the parsed view's text for
  `Html.Partial(...)` / `EditorFor(...)` calls. Razor's public syntax API makes
  true node-by-node walking of these calls awkward and version-sensitive, so
  this part is not yet fully semantic. The **C# side (ControllerIndex) is fully
  semantic** via Roslyn — that's where the big accuracy win is.
- Data-flow tracing for dynamic view names handles the common cases (local
  `var x = "..."`, simple reassignment). Views chosen by complex logic
  (switch statements, method return values) are still flagged as dynamic —
  correctly, since they can't be resolved to a single view statically.
- Requires the solution to load in MSBuildWorkspace; projects with unusual
  custom MSBuild targets may emit workspace warnings (logged to stderr, not
  fatal).
