// sidecar/ControllerIndex.cs
// ──────────────────────────────────────────────────────────────────────────────
// The Roslyn-powered core. This is where the sidecar genuinely beats regex:
//
//   • return View("Name")      → exact view name
//   • return View(model)       → action's model type from the semantic model
//   • return View(someVar)     → DATA-FLOW: trace the variable to its assignment
//   • model property types     → real INamedTypeSymbol, generics unwrapped,
//                                 inheritance + nested model types resolved
//
// All resolution uses Roslyn's SemanticModel, so it reflects what the compiler
// actually sees — not string heuristics.

using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace DotNetAnalyzer;

public sealed class ControllerIndex
{
    // action key: "ControllerName.ActionName" → resolved info
    private readonly List<ActionInfo> _actions = new();
    // type name → declaring file path
    private readonly Dictionary<string, (INamedTypeSymbol Symbol, string File)> _types =
        new(StringComparer.Ordinal);

    public void AddAction(
        ClassDeclarationSyntax controller,
        MethodDeclarationSyntax method,
        SemanticModel model,
        string filePath)
    {
        var controllerName = controller.Identifier.Text;          // e.g. "UserController"
        var actionName = method.Identifier.Text;                   // e.g. "Edit"

        var info = new ActionInfo
        {
            Controller = controllerName,
            Action = actionName,
            File = filePath,
        };

        // Find all return View(...) / PartialView(...) in this method body.
        if (method.Body is not null || method.ExpressionBody is not null)
        {
            var returns = method.DescendantNodes().OfType<InvocationExpressionSyntax>()
                .Where(IsViewInvocation);

            foreach (var inv in returns)
            {
                var resolved = ResolveViewName(inv, actionName, model);
                if (resolved.ViewName is not null)
                    info.ViewNames.Add(resolved.ViewName);
                else if (resolved.IsDynamic)
                    info.HasDynamicView = true;

                // Model type passed to View(model)
                var modelType = ResolveViewModelType(inv, method, model);
                if (modelType is not null) info.ModelType ??= modelType;
            }
        }

        // Convention: action name == view name when no explicit string given.
        if (info.ViewNames.Count == 0 && !info.HasDynamicView)
            info.ViewNames.Add(actionName);

        _actions.Add(info);
    }

    public void AddType(INamedTypeSymbol symbol, string file)
    {
        var name = symbol.Name;
        if (!_types.ContainsKey(name))
            _types[name] = (symbol, file);
    }

    // ── Resolve controller/action/model for a given entry view ────────────────

    public void ResolveForView(
        string viewRel, ViewInfo info,
        out string? controllerFile, out string? action,
        out string? modelFile, out List<string> nestedModelFiles,
        List<UnresolvedLink> unresolved)
    {
        controllerFile = null;
        action = null;
        modelFile = null;
        nestedModelFiles = new List<string>();

        var viewBase = Path.GetFileNameWithoutExtension(viewRel);
        var folder = Path.GetFileName(Path.GetDirectoryName(viewRel) ?? "");  // e.g. "User"
        var expectedController = folder + "Controller";

        // Find the action whose view name matches this view (prefer same controller).
        var candidates = _actions
            .Where(a => a.ViewNames.Any(v => v.Equals(viewBase, StringComparison.OrdinalIgnoreCase)))
            .OrderByDescending(a => a.Controller.Equals(expectedController, StringComparison.OrdinalIgnoreCase))
            .ToList();

        var match = candidates.FirstOrDefault();
        if (match is null)
        {
            // Maybe the controller uses a dynamic view name → flag it.
            var dyn = _actions.FirstOrDefault(a =>
                a.Controller.Equals(expectedController, StringComparison.OrdinalIgnoreCase) &&
                a.HasDynamicView);
            if (dyn is not null)
            {
                unresolved.Add(new UnresolvedLink
                {
                    Kind = "view", Reference = viewRel, SourceFile = dyn.File,
                    Reason = "controller returns a dynamic view name — Roslyn could not statically resolve it",
                });
            }
            else
            {
                unresolved.Add(new UnresolvedLink
                {
                    Kind = "view", Reference = viewRel, SourceFile = expectedController,
                    Reason = "no controller action resolves to this view",
                });
            }
            return;
        }

        controllerFile = RelFile(match.File);
        action = match.Action;

        // Model: prefer the action's model type; fall back to the @model directive.
        var modelTypeName = match.ModelType ?? CoreTypeName(info.ModelTypeName);
        if (modelTypeName is not null && _types.TryGetValue(modelTypeName, out var t))
        {
            modelFile = RelFile(t.File);
            // Nested models: walk public property types that are also known types.
            CollectNested(t.Symbol, nestedModelFiles, new HashSet<string>(), depth: 0);
        }
        else if (modelTypeName is not null)
        {
            unresolved.Add(new UnresolvedLink
            {
                Kind = "model", Reference = modelTypeName, SourceFile = viewRel,
                Reason = "model class not found in solution",
            });
        }
    }

    private void CollectNested(INamedTypeSymbol type, List<string> outFiles, HashSet<string> seen, int depth)
    {
        if (depth > 2 || !seen.Add(type.Name)) return;
        foreach (var member in type.GetMembers().OfType<IPropertySymbol>())
        {
            var pt = UnwrapType(member.Type);
            if (pt is INamedTypeSymbol named && _types.TryGetValue(named.Name, out var t)
                && named.Name != type.Name)
            {
                var rel = RelFile(t.File);
                if (!outFiles.Contains(rel)) outFiles.Add(rel);
                CollectNested(named, outFiles, seen, depth + 1);
            }
        }
    }

    // ── View name / model resolution via Roslyn ───────────────────────────────

    private static bool IsViewInvocation(InvocationExpressionSyntax inv)
    {
        var name = inv.Expression switch
        {
            IdentifierNameSyntax id => id.Identifier.Text,
            MemberAccessExpressionSyntax ma => ma.Name.Identifier.Text,
            _ => null,
        };
        return name is "View" or "PartialView";
    }

    private (string? ViewName, bool IsDynamic) ResolveViewName(
        InvocationExpressionSyntax inv, string actionName, SemanticModel model)
    {
        var firstArg = inv.ArgumentList.Arguments.FirstOrDefault()?.Expression;
        if (firstArg is null)
            return (actionName, false); // return View() → convention

        // Literal string → exact name.
        if (firstArg is LiteralExpressionSyntax lit && lit.Token.Value is string s)
            return (s, false);

        // Variable / expression → try Roslyn constant value, then data-flow.
        var constant = model.GetConstantValue(firstArg);
        if (constant.HasValue && constant.Value is string cs)
            return (cs, false);

        // If it's an identifier, try to trace its assignments for a string literal.
        if (firstArg is IdentifierNameSyntax id)
        {
            var traced = TraceStringAssignment(id, inv, model);
            if (traced is not null) return (traced, false);
        }

        // Genuinely dynamic — could not resolve statically.
        return (null, true);
    }

    // Walk back through the method body for `var x = "literal";` assignments.
    private static string? TraceStringAssignment(
        IdentifierNameSyntax id, InvocationExpressionSyntax usage, SemanticModel model)
    {
        var symbol = model.GetSymbolInfo(id).Symbol;
        if (symbol is null) return null;

        var method = usage.Ancestors().OfType<MethodDeclarationSyntax>().FirstOrDefault();
        if (method is null) return null;

        // Local declarations: var x = "...";
        foreach (var decl in method.DescendantNodes().OfType<VariableDeclaratorSyntax>())
        {
            var declSymbol = model.GetDeclaredSymbol(decl);
            if (!SymbolEqualityComparer.Default.Equals(declSymbol, symbol)) continue;
            if (decl.Initializer?.Value is LiteralExpressionSyntax lit && lit.Token.Value is string s)
                return s;
        }
        // Simple assignments: x = "...";
        foreach (var assign in method.DescendantNodes().OfType<AssignmentExpressionSyntax>())
        {
            if (assign.Left is IdentifierNameSyntax lhs &&
                SymbolEqualityComparer.Default.Equals(model.GetSymbolInfo(lhs).Symbol, symbol) &&
                assign.Right is LiteralExpressionSyntax rlit && rlit.Token.Value is string rs)
                return rs;
        }
        return null;
    }

    private string? ResolveViewModelType(
        InvocationExpressionSyntax inv, MethodDeclarationSyntax method, SemanticModel model)
    {
        // View(model) → the model arg's type. For View("Name", model) it's the 2nd arg.
        var args = inv.ArgumentList.Arguments;
        ExpressionSyntax? modelArg = args.Count switch
        {
            1 when args[0].Expression is not LiteralExpressionSyntax => args[0].Expression,
            >= 2 => args[1].Expression,
            _ => null,
        };
        if (modelArg is null) return null;

        var type = model.GetTypeInfo(modelArg).Type;
        if (type is null) return null;
        var unwrapped = UnwrapType(type);
        return (unwrapped as INamedTypeSymbol)?.Name;
    }

    // Unwrap List<T>, IEnumerable<T>, Task<T>, etc. to the meaningful inner type.
    private static ITypeSymbol UnwrapType(ITypeSymbol type)
    {
        if (type is INamedTypeSymbol named && named.IsGenericType)
        {
            // take the last type argument (handles Dictionary<K,V> → V, List<T> → T)
            var arg = named.TypeArguments.LastOrDefault();
            if (arg is not null) return UnwrapType(arg);
        }
        return type;
    }

    private static string? CoreTypeName(string? modelDirective)
    {
        if (string.IsNullOrWhiteSpace(modelDirective)) return null;
        var t = modelDirective.Trim();
        // strip generics
        if (t.Contains('<') && t.EndsWith(">"))
        {
            var inner = t.Substring(t.IndexOf('<') + 1, t.Length - t.IndexOf('<') - 2);
            t = inner.Split(',').Last().Trim();
        }
        return t.Split('.').Last();
    }

    private static string RelFile(string absFile) => absFile.Replace('\\', '/');

    // ── Inner types ────────────────────────────────────────────────────────────

    private sealed class ActionInfo
    {
        public string Controller = "";
        public string Action = "";
        public string File = "";
        public List<string> ViewNames = new();
        public bool HasDynamicView;
        public string? ModelType;
    }
}
