// sidecar/Program.cs
// ──────────────────────────────────────────────────────────────────────────────
// .NET analysis sidecar for the UI Migration Agent.
//
// Uses Roslyn (MSBuildWorkspace) to load the whole solution with full semantic
// resolution, and the Razor language engine to parse .cshtml files into real
// syntax trees. Emits the SAME page_map.json contract the Python regex grapher
// produces, so it's a drop-in engine swap.
//
// Why this beats regex:
//   - return View(variable)  → Roslyn traces the variable's value (data-flow)
//   - model types/generics   → resolved from the semantic model, not string-stripped
//   - partials/EditorFor     → from the Razor syntax tree, not pattern matching
//
// Usage:
//   dotnet run -c Release -- --sln App.sln --repo /path/to/repo --out page_map.json
//
// Output JSON shape (per page):
//   { page_name, entry_view, partials[], layout, controller, controller_action,
//     model, nested_models[], unresolved[] }
// Top level: { repo, engine:"roslyn", pages[], unresolved_count, unresolved_breakdown }

using System.Text.Json;
using Microsoft.Build.Locator;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.MSBuild;
using Microsoft.AspNetCore.Razor.Language;

namespace DotNetAnalyzer;

public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        var opts = ParseArgs(args);
        if (opts.Sln is null || opts.Repo is null || opts.Out is null)
        {
            Console.Error.WriteLine("Usage: --sln <path> --repo <path> --out <path>");
            return 2;
        }

        // MSBuildLocator must run before any Roslyn workspace API is touched.
        if (!MSBuildLocator.IsRegistered)
            MSBuildLocator.RegisterDefaults();

        try
        {
            var result = await Analyze(opts.Sln, opts.Repo);
            var json = JsonSerializer.Serialize(result, new JsonSerializerOptions
            {
                WriteIndented = true,
                DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.Never,
            });
            await File.WriteAllTextAsync(opts.Out, json);
            Console.Error.WriteLine($"Wrote {result.Pages.Count} pages → {opts.Out}");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"Analyzer failed: {ex.Message}\n{ex.StackTrace}");
            return 1;
        }
    }

    // ── Core analysis ─────────────────────────────────────────────────────────

    private static async Task<PageMap> Analyze(string slnPath, string repoRoot)
    {
        using var workspace = MSBuildWorkspace.Create();
        workspace.WorkspaceFailed += (_, e) =>
            Console.Error.WriteLine($"[workspace] {e.Diagnostic.Message}");

        var solution = await workspace.OpenSolutionAsync(slnPath);

        // 1. Index all .cshtml on disk (the Razor side).
        var cshtmlFiles = Directory
            .EnumerateFiles(repoRoot, "*.cshtml", SearchOption.AllDirectories)
            .Where(p => !p.Contains($"{Path.DirectorySeparatorChar}bin{Path.DirectorySeparatorChar}")
                     && !p.Contains($"{Path.DirectorySeparatorChar}obj{Path.DirectorySeparatorChar}"))
            .ToList();

        var viewsByRel = cshtmlFiles.ToDictionary(
            p => Rel(repoRoot, p),
            p => p,
            StringComparer.OrdinalIgnoreCase);

        // 2. Parse every Razor view once into a small descriptor.
        var razor = new RazorAnalyzer(repoRoot, viewsByRel);
        var viewInfos = new Dictionary<string, ViewInfo>(StringComparer.OrdinalIgnoreCase);
        foreach (var (rel, abs) in viewsByRel)
            viewInfos[rel] = razor.Parse(rel, abs);

        // 3. Roslyn pass over controllers: map actions → views, resolve models.
        var controllerIndex = await BuildControllerIndex(solution);

        // 4. Assemble page clusters for entry views (non-partials).
        var pages = new List<PageCluster>();
        foreach (var (rel, info) in viewInfos)
        {
            if (IsPartialName(Path.GetFileName(rel)))
                continue; // partials are included by other views, not entry pages

            var cluster = BuildCluster(rel, info, viewInfos, controllerIndex, razor);
            pages.Add(cluster);
        }

        return new PageMap
        {
            Repo = Path.GetFullPath(repoRoot),
            Engine = "roslyn",
            Pages = pages,
        };
    }

    // ── Cluster assembly ──────────────────────────────────────────────────────

    private static PageCluster BuildCluster(
        string entryRel,
        ViewInfo info,
        Dictionary<string, ViewInfo> allViews,
        ControllerIndex controllers,
        RazorAnalyzer razor)
    {
        var unresolved = new List<UnresolvedLink>();

        // Partials (recursive, from the Razor tree)
        var partials = new List<string>();
        razor.CollectPartials(entryRel, allViews, partials, unresolved, new HashSet<string>(StringComparer.OrdinalIgnoreCase));

        // Layout (explicit, _ViewStart chain handled in RazorAnalyzer)
        var layout = razor.ResolveLayout(entryRel, info, allViews, unresolved);

        // Controller + action + model, resolved semantically by Roslyn
        controllers.ResolveForView(entryRel, info, out var controller, out var action,
                                   out var model, out var nestedModels, unresolved);

        return new PageCluster
        {
            PageName = ToPageName(entryRel),
            EntryView = entryRel,
            Partials = partials,
            Layout = layout,
            Controller = controller,
            ControllerAction = action,
            Model = model,
            NestedModels = nestedModels,
            Unresolved = unresolved,
        };
    }

    // ── Roslyn: controller index ──────────────────────────────────────────────

    private static async Task<ControllerIndex> BuildControllerIndex(Solution solution)
    {
        var index = new ControllerIndex();

        foreach (var project in solution.Projects)
        {
            var compilation = await project.GetCompilationAsync();
            if (compilation is null) continue;

            foreach (var tree in compilation.SyntaxTrees)
            {
                var model = compilation.GetSemanticModel(tree);
                var root = await tree.GetRootAsync();

                foreach (var classDecl in root.DescendantNodes().OfType<ClassDeclarationSyntax>())
                {
                    if (!classDecl.Identifier.Text.EndsWith("Controller")) continue;

                    foreach (var methodDecl in classDecl.Members.OfType<MethodDeclarationSyntax>())
                    {
                        index.AddAction(classDecl, methodDecl, model, tree.FilePath);
                    }
                }

                // Also index model classes for type resolution
                foreach (var classDecl in root.DescendantNodes().OfType<ClassDeclarationSyntax>())
                {
                    var symbol = model.GetDeclaredSymbol(classDecl) as INamedTypeSymbol;
                    if (symbol != null)
                        index.AddType(symbol, tree.FilePath);
                }
            }
        }
        return index;
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private static string Rel(string root, string path) =>
        Path.GetRelativePath(root, path).Replace('\\', '/');

    private static bool IsPartialName(string fileName)
    {
        var baseName = Path.GetFileNameWithoutExtension(fileName);
        return baseName.StartsWith("_") ||
               baseName.EndsWith("Partial", StringComparison.OrdinalIgnoreCase);
    }

    private static string ToPageName(string rel)
    {
        var noExt = rel.EndsWith(".cshtml") ? rel[..^7] : rel;
        return noExt.Replace("Views/", "", StringComparison.OrdinalIgnoreCase);
    }

    private static Options ParseArgs(string[] args)
    {
        var o = new Options();
        for (int i = 0; i < args.Length - 1; i++)
        {
            switch (args[i])
            {
                case "--sln": o.Sln = args[++i]; break;
                case "--repo": o.Repo = args[++i]; break;
                case "--out": o.Out = args[++i]; break;
            }
        }
        return o;
    }

    private sealed class Options
    {
        public string? Sln, Repo, Out;
    }
}
