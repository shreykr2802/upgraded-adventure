// sidecar/RazorAnalyzer.cs
// ──────────────────────────────────────────────────────────────────────────────
// Parses .cshtml files using the real Razor language engine (not regex), and
// resolves partials, layout (incl. _ViewStart chain), and the @model directive
// from the parsed syntax tree.

using Microsoft.AspNetCore.Razor.Language;
using Microsoft.AspNetCore.Razor.Language.Intermediate;

namespace DotNetAnalyzer;

public sealed class ViewInfo
{
    public string Rel = "";
    public string? ModelTypeName;            // from @model directive (full type string)
    public string? ExplicitLayout;           // from Layout = "..." if present
    public List<string> PartialRefs = new(); // names referenced via Partial/RenderPartial/PartialAsync
    public List<string> EditorForProps = new(); // property names from EditorFor/DisplayFor
}

public sealed class RazorAnalyzer
{
    private readonly string _repoRoot;
    private readonly Dictionary<string, string> _viewsByRel; // rel → abs
    private readonly RazorProjectEngine _engine;

    public RazorAnalyzer(string repoRoot, Dictionary<string, string> viewsByRel)
    {
        _repoRoot = repoRoot;
        _viewsByRel = viewsByRel;
        var fs = RazorProjectFileSystem.Create(repoRoot);
        _engine = RazorProjectEngine.Create(RazorConfiguration.Default, fs);
    }

    // Parse one view into a ViewInfo by walking the Razor syntax tree.
    public ViewInfo Parse(string rel, string abs)
    {
        var info = new ViewInfo { Rel = rel };
        string text;
        try { text = File.ReadAllText(abs); }
        catch { return info; }

        var sourceDoc = RazorSourceDocument.Create(text, abs);
        var codeDoc = RazorCodeDocument.Create(sourceDoc, Array.Empty<RazorSourceDocument>());
        var syntaxTree = RazorSyntaxTree.Parse(sourceDoc);

        // Walk tokens/nodes for directives + helper calls.
        // Razor's syntax tree exposes directives (@model, @{ Layout }) and
        // C# expression spans. We scan the rendered C# for known helper calls.
        var walker = new RazorWalker(info);
        walker.Visit(syntaxTree.Root);

        return info;
    }

    // Recursively collect partials referenced by a view.
    public void CollectPartials(
        string viewRel,
        Dictionary<string, ViewInfo> allViews,
        List<string> partials,
        List<UnresolvedLink> unresolved,
        HashSet<string> seen)
    {
        if (!seen.Add(viewRel)) return;
        if (!allViews.TryGetValue(viewRel, out var info)) return;

        foreach (var refName in info.PartialRefs)
        {
            var resolved = FindView(refName, viewRel);
            if (resolved is not null)
            {
                if (!partials.Contains(resolved)) partials.Add(resolved);
                CollectPartials(resolved, allViews, partials, unresolved, seen);
            }
            else
            {
                unresolved.Add(new UnresolvedLink
                {
                    Kind = "partial", Reference = refName, SourceFile = viewRel,
                    Reason = "partial file not found in repo",
                });
            }
        }

        // EditorFor/DisplayFor → resolve by convention (EditorTemplates folder)
        foreach (var prop in info.EditorForProps)
        {
            var template = FindEditorTemplate(prop);
            if (template is not null)
            {
                if (!partials.Contains(template)) partials.Add(template);
            }
            else
            {
                unresolved.Add(new UnresolvedLink
                {
                    Kind = "partial", Reference = $"EditorFor: {prop}", SourceFile = viewRel,
                    Reason = "editor/display template not found by convention — review manually",
                });
            }
        }
    }

    // Resolve layout: explicit Layout=, else walk _ViewStart chain to repo root.
    public string? ResolveLayout(
        string viewRel, ViewInfo info,
        Dictionary<string, ViewInfo> allViews,
        List<UnresolvedLink> unresolved)
    {
        if (!string.IsNullOrWhiteSpace(info.ExplicitLayout))
        {
            if (info.ExplicitLayout!.Trim().Equals("null", StringComparison.OrdinalIgnoreCase))
                return null;
            var resolved = FindView(info.ExplicitLayout!, viewRel);
            if (resolved is not null) return resolved;
            unresolved.Add(new UnresolvedLink
            {
                Kind = "layout", Reference = info.ExplicitLayout!, SourceFile = viewRel,
                Reason = "layout file not found",
            });
            return null;
        }

        // Walk _ViewStart.cshtml from the view's folder up to the root.
        var dir = Path.GetDirectoryName(viewRel)?.Replace('\\', '/') ?? "";
        var parts = dir.Length > 0 ? dir.Split('/') : Array.Empty<string>();
        for (int i = parts.Length; i >= 0; i--)
        {
            var folder = string.Join('/', parts.Take(i));
            var vs = folder.Length > 0 ? $"{folder}/_ViewStart.cshtml" : "_ViewStart.cshtml";
            if (allViews.TryGetValue(vs, out var vsInfo) &&
                !string.IsNullOrWhiteSpace(vsInfo.ExplicitLayout))
            {
                var resolved = FindView(vsInfo.ExplicitLayout!, vs);
                if (resolved is not null) return resolved;
            }
        }
        return null; // no layout discoverable — not an error
    }

    // ── View name resolution ──────────────────────────────────────────────────

    private string? FindView(string name, string fromView)
    {
        var clean = name.Replace("~/", "").Replace('\\', '/').TrimStart('/');
        var target = clean.EndsWith(".cshtml") ? clean : clean + ".cshtml";

        // exact path
        foreach (var rel in _viewsByRel.Keys)
            if (rel.Equals(target, StringComparison.OrdinalIgnoreCase)) return rel;
        // with Views/ prefix
        if (!target.StartsWith("views/", StringComparison.OrdinalIgnoreCase))
        {
            var withViews = "Views/" + target;
            foreach (var rel in _viewsByRel.Keys)
                if (rel.Equals(withViews, StringComparison.OrdinalIgnoreCase)) return rel;
        }
        // suffix match
        foreach (var rel in _viewsByRel.Keys)
            if (rel.EndsWith("/" + target, StringComparison.OrdinalIgnoreCase)) return rel;
        // basename, prefer same folder then Shared
        var baseName = Path.GetFileName(target);
        var matches = _viewsByRel.Keys
            .Where(r => Path.GetFileName(r).Equals(baseName, StringComparison.OrdinalIgnoreCase))
            .ToList();
        if (matches.Count == 1) return matches[0];
        if (matches.Count > 1)
        {
            var callerDir = Path.GetDirectoryName(fromView)?.Replace('\\', '/') ?? "";
            var sameFolder = matches.FirstOrDefault(m =>
                (Path.GetDirectoryName(m)?.Replace('\\', '/') ?? "")
                    .Equals(callerDir, StringComparison.OrdinalIgnoreCase));
            if (sameFolder is not null) return sameFolder;
            var shared = matches.FirstOrDefault(m => m.Contains("shared", StringComparison.OrdinalIgnoreCase));
            return shared ?? matches[0];
        }
        return null;
    }

    private string? FindEditorTemplate(string prop)
    {
        var targets = new[]
        {
            $"editortemplates/{prop}.cshtml".ToLowerInvariant(),
            $"displaytemplates/{prop}.cshtml".ToLowerInvariant(),
        };
        foreach (var rel in _viewsByRel.Keys)
        {
            var low = rel.Replace('\\', '/').ToLowerInvariant();
            if (targets.Any(t => low.EndsWith(t))) return rel;
        }
        return null;
    }
}
