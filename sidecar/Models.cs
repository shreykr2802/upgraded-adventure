// sidecar/Models.cs
// ──────────────────────────────────────────────────────────────────────────────
// Output data shapes. These serialise (via System.Text.Json) to the EXACT
// page_map.json contract the Python regex grapher produces, so the sidecar is
// a drop-in engine swap. JSON property names use snake_case to match.

using System.Text.Json.Serialization;

namespace DotNetAnalyzer;

public sealed class PageMap
{
    [JsonPropertyName("repo")] public string Repo { get; set; } = "";
    [JsonPropertyName("engine")] public string Engine { get; set; } = "roslyn";
    [JsonPropertyName("pages")] public List<PageCluster> Pages { get; set; } = new();

    // unresolved_count + unresolved_breakdown are (re)computed on the Python side
    // in engine._normalise(), so we don't duplicate that logic here. But we emit
    // unresolved_count for standalone use of the sidecar.
    [JsonPropertyName("unresolved_count")]
    public int UnresolvedCount => Pages.Sum(p => p.Unresolved.Count);
}

public sealed class PageCluster
{
    [JsonPropertyName("page_name")] public string PageName { get; set; } = "";
    [JsonPropertyName("entry_view")] public string EntryView { get; set; } = "";
    [JsonPropertyName("partials")] public List<string> Partials { get; set; } = new();
    [JsonPropertyName("layout")] public string? Layout { get; set; }
    [JsonPropertyName("controller")] public string? Controller { get; set; }
    [JsonPropertyName("controller_action")] public string? ControllerAction { get; set; }
    [JsonPropertyName("model")] public string? Model { get; set; }
    [JsonPropertyName("nested_models")] public List<string> NestedModels { get; set; } = new();
    [JsonPropertyName("unresolved")] public List<UnresolvedLink> Unresolved { get; set; } = new();
}

public sealed class UnresolvedLink
{
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
    [JsonPropertyName("reference")] public string Reference { get; set; } = "";
    [JsonPropertyName("source_file")] public string SourceFile { get; set; } = "";
    [JsonPropertyName("reason")] public string Reason { get; set; } = "";
}
