#!/usr/bin/env python3
"""Re-verify the security control that D5 rests on.

The ArcticBase/artifacts spec decides that model-authored reports are published as
`md` and never `html`. The reason is NOT that `md` is rendered somewhere safer --
`MdViewer.svelte:32` puts the rendered output in an iframe with no `sandbox`
attribute, from a same-origin URL, exactly as `HtmlViewer` does. Both kinds share
that container. The only thing between a model-authored report and script execution
with full ArcticBase API access is `{"html": False}` in ArcticBase's markdown-it
configuration.

ArcticBase is consumed, not modified, so that line belongs to a dependency we do not
control. Run this after any ArcticBase upgrade, or any change to its render path.

    python scripts/check_arcticbase_md_escaping.py [--config path/to/render.py]

Exits non-zero if any payload produces live script-capable markup.

Two things this script does deliberately, both learned the hard way:

  * It PARSES the rendered HTML and inspects real tags and attributes. An earlier
    version grepped the output for "onerror", "javascript:" and friends, and
    reported eleven leaks -- every one of which was correctly escaped text that
    merely contained the substring. Grepping this is worse than not checking,
    because it cries wolf and trains you to ignore it.

  * It FAILS if the tasklists plugin is missing rather than testing without it.
    ArcticBase's `render.py` swallows the ImportError and falls back, so a run
    without the plugin silently exercises a different renderer than production.
"""
from __future__ import annotations

import sys
from html.parser import HTMLParser

try:
    from markdown_it import MarkdownIt
except ImportError:
    sys.exit("markdown-it-py is not installed; cannot check ArcticBase's md renderer")


def build_renderer() -> MarkdownIt:
    """Mirror ArcticBase backend/src/arctic_base/api/render.py:21-33.

    Keep this in step with that function. If it drifts, this script is testing a
    renderer ArcticBase does not use, which is the failure mode it exists to prevent.
    """
    md = MarkdownIt(
        "commonmark", {"html": False, "linkify": True, "typographer": True}
    ).enable(["table", "strikethrough"])
    try:
        from mdit_py_plugins.tasklists import tasklists_plugin
    except ImportError:
        sys.exit(
            "mdit-py-plugins is not installed. ArcticBase declares it "
            "(backend/pyproject.toml) and render.py loads it, so testing without it "
            "would exercise a renderer production does not use. Install it and rerun."
        )
    md.use(tasklists_plugin)
    return md


DANGEROUS_TAGS = {"script", "iframe", "object", "embed", "link", "meta", "base", "form"}
DANGEROUS_SCHEMES = ("javascript:", "vbscript:", "data:text/html")


class LiveMarkupAuditor(HTMLParser):
    """Collects genuinely script-capable constructs, not strings that look like them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.findings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DANGEROUS_TAGS:
            self.findings.append(f"<{tag}> element emitted")
        for key, value in attrs:
            if key.lower().startswith("on"):
                self.findings.append(f"event handler {key}={value!r} on <{tag}>")
            if value and value.strip().lower().replace("\t", "").replace(
                "\n", ""
            ).startswith(DANGEROUS_SCHEMES):
                self.findings.append(f"{key}={value!r} on <{tag}> uses a script scheme")


PAYLOADS: dict[str, str] = {
    "raw script tag": "<script>alert(1)</script>",
    "img onerror": '<img src=x onerror="alert(1)">',
    "svg onload": "<svg onload=alert(1)>",
    "iframe": '<iframe src="//evil"></iframe>',
    "javascript: link": "[click](javascript:alert(1))",
    "case-varied js": "[click](JaVaScRiPt:alert(1))",
    "entity-encoded js": "[click](java&#115;cript:alert(1))",
    "tab-split scheme": "[click](java\tscript:alert(1))",
    "newline-split scheme": "[click](java\nscript:alert(1))",
    "data html link": "[click](data:text/html,<script>alert(1)</script>)",
    "vbscript link": "[click](vbscript:msgbox(1))",
    "attr break in href": '[x](//e/"/onmouseover="alert`1`)',
    "attr break in src": '![x](//e/"/onerror="alert`1`)',
    "attr break in title": '[x](//e "\\" onmouseover=alert`1`")',
    "autolink js": "<javascript:alert(1)>",
    "linkify js": "javascript:alert(1)",
    "html comment split": "<!-- --><script>alert(1)</script>",
    "backslash quote": '[x](//e/\\"onmouseover=alert`1`)',
    "ref-style js link": "[x][r]\n\n[r]: javascript:alert(1)",
    "img ref js": "![x][r]\n\n[r]: javascript:alert(1)",
    "fenced block escape": "```\n</code></pre><script>alert(1)</script>\n```",
    "table cell html": "| a |\n|---|\n| <script>alert(1)</script> |",
    "tasklist raw html": "- [x] <script>alert(1)</script>",
    "tasklist img onerror": "- [ ] <img src=x onerror=alert(1)>",
    "tasklist attr break": '- [x] [y](//e/"/onmouseover="alert`1`)',
    "tasklist nested": "- [x] a\n  - [ ] <script>alert(1)</script>",
}


def main() -> int:
    md = build_renderer()
    leaks: list[tuple[str, str, str, list[str]]] = []

    for name, source in PAYLOADS.items():
        rendered = md.render(source)
        auditor = LiveMarkupAuditor()
        auditor.feed(rendered)
        if auditor.findings:
            leaks.append((name, source, rendered, auditor.findings))
            print(f"LIVE MARKUP | {name}")
        else:
            print(f"neutralised | {name}")

    print()
    if leaks:
        print(f"FAIL: {len(leaks)} of {len(PAYLOADS)} payloads produced live markup.")
        print("D5 assumes this cannot happen. Model-authored md must not be published")
        print("to ArcticBase until this is resolved -- the viewer's iframe is not")
        print("sandboxed and will execute what the renderer emits.")
        for name, source, rendered, findings in leaks:
            print(f"\n!! {name}\n   in : {source!r}\n   out: {rendered.strip()}")
            for finding in findings:
                print(f"   >>  {finding}")
        return 1

    print(f"PASS: {len(PAYLOADS)} payloads, none produced script-capable markup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
