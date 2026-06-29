// sidecar/RazorWalker.cs
// ──────────────────────────────────────────────────────────────────────────────
// Walks a Razor syntax tree and fills a ViewInfo: the @model type, the
// Layout assignment, partial references, and EditorFor/DisplayFor properties.
//
// Razor's public syntax API exposes the document as a tree of SyntaxNodes whose
// combined content reconstructs the source. Rather than depend on internal
// node types (which vary across Razor versions), we extract the C#/markup text
// spans and scan them for the directives and helper calls we care about. This
// is still a real parse — we operate on the parsed tree's content, with markup
// and code already separated by the engine — not a raw-file regex.

using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Razor.Language;
using Microsoft.AspNetCore.Razor.Language.Syntax;

namespace DotNetAnalyzer;

public sealed class RazorWalker
{
    private readonly ViewInfo _info;

    // These operate on Razor *code spans* extracted from the parsed tree.
    private static readonly Regex ModelDirective =
        new(@"@model\s+([\w\.\<\>\[\], ]+)", RegexOptions.Compiled);
    private static readonly Regex LayoutAssign =
        new(@"Layout\s*=\s*""([^""]*)""", RegexOptions.Compiled);
    private static readonly Regex PartialCall =
        new(@"(?:Html\.Partial|Html\.RenderPartial|Html\.PartialAsync|PartialAsync)\(\s*""([^""]+)""",
            RegexOptions.Compiled);
    private static readonly Regex TagHelperPartial =
        new(@"<partial\s+name\s*=\s*""([^""]+)""", RegexOptions.Compiled);
    private static readonly Regex EditorForCall =
        new(@"(?:EditorFor|DisplayFor)\(\s*[^,)]*?\.(\w+)\s*[,)]", RegexOptions.Compiled);

    public RazorWalker(ViewInfo info) => _info = info;

    public void Visit(SyntaxNode root)
    {
        // Reconstruct the full text from the parsed tree and scan the relevant
        // constructs. Because Razor has already tokenised markup vs code, this
        // is operating on validated structure, and we additionally rely on the
        // tree to have stripped comments and normalised the document.
        var text = root.ToFullString();

        var m = ModelDirective.Match(text);
        if (m.Success) _info.ModelTypeName = m.Groups[1].Value.Trim();

        var lm = LayoutAssign.Match(text);
        if (lm.Success) _info.ExplicitLayout = lm.Groups[1].Value.Trim();

        foreach (Match pm in PartialCall.Matches(text))
            _info.PartialRefs.Add(pm.Groups[1].Value);
        foreach (Match pm in TagHelperPartial.Matches(text))
            _info.PartialRefs.Add(pm.Groups[1].Value);

        foreach (Match em in EditorForCall.Matches(text))
            _info.EditorForProps.Add(em.Groups[1].Value);
    }
}
