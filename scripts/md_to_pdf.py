"""
Render a Markdown document to PDF the same way the existing ones were rendered.

WHY HEADLESS CHROME AND NOT A PYTHON PDF LIBRARY
    The PDFs already in `docs/` were produced by Chrome (`Skia/PDF` in their
    metadata). Matching that matters more than picking the "nicer" tool: these
    documents are handed to a client and to two frontend developers, and a
    re-render in a different engine would change every page break, every table
    width and the page count on the cover — which reads as a different document
    rather than an updated one.

    It also means the layout rules below are ordinary CSS, so anyone can change
    the look without learning a PDF API.

USAGE
    python scripts/md_to_pdf.py docs/FOO.md [docs/BAR.md ...]
    -> writes docs/FOO.pdf beside each input
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]

_BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# A4 at a readable size. `pre` wraps rather than clipping, because a code block
# that runs off the right edge of a PDF is unrecoverable — there is no scrollbar.
CSS = """
@page { size: A4; margin: 16mm 14mm 18mm 14mm; }
* { box-sizing: border-box; }
body {
  font-family: "Segoe UI", -apple-system, "Helvetica Neue", Arial, sans-serif;
  font-size: 9.6pt; line-height: 1.5; color: #1a1a1a; margin: 0;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 1.1em 0 0.45em; page-break-after: avoid; }
h1 { font-size: 20pt; color: #111; border-bottom: 2.5px solid #1a1a1a; padding-bottom: 6px; }
h2 { font-size: 15pt; color: #111; border-bottom: 1px solid #ccc; padding-bottom: 4px; page-break-before: auto; }
h3 { font-size: 12pt; color: #222; }
h4 { font-size: 10.5pt; color: #333; }
h5, h6 { font-size: 9.8pt; color: #444; }
p { margin: 0.5em 0; orphans: 3; widows: 3; }
ul, ol { margin: 0.5em 0 0.5em 0; padding-left: 1.5em; }
li { margin: 0.18em 0; }
a { color: #0b5cad; text-decoration: none; }
code {
  font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
  font-size: 0.87em; background: #f2f3f5; padding: 1px 4px;
  border-radius: 3px; word-break: break-word;
}
pre {
  background: #f7f8fa; border: 1px solid #e1e4e8; border-left: 3px solid #999;
  border-radius: 4px; padding: 9px 11px; margin: 0.6em 0;
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
  page-break-inside: avoid; font-size: 8.4pt; line-height: 1.42;
}
pre code { background: none; padding: 0; font-size: inherit; }
blockquote {
  margin: 0.65em 0; padding: 7px 12px; background: #fbf9f2;
  border-left: 3px solid #d2a72c; color: #3a3a3a; page-break-inside: avoid;
}
blockquote p:first-child { margin-top: 0; }
blockquote p:last-child { margin-bottom: 0; }
table {
  border-collapse: collapse; width: 100%; margin: 0.65em 0;
  font-size: 8.6pt; page-break-inside: auto;
}
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th, td {
  border: 1px solid #d6d9dd; padding: 4px 7px; text-align: left;
  vertical-align: top; word-wrap: break-word; overflow-wrap: anywhere;
}
th { background: #eef1f4; font-weight: 600; }
tbody tr:nth-child(even) { background: #fafbfc; }
hr { border: none; border-top: 1px solid #d6d9dd; margin: 1.3em 0; }
img { max-width: 100%; }
strong { font-weight: 650; }
"""

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>{css}</style></head><body>
{body}
</body></html>
"""


def _browser() -> str:
    for candidate in _BROWSERS:
        if pathlib.Path(candidate).exists():
            return candidate
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return found
    raise SystemExit(
        "No Chrome or Edge found. The existing PDFs were rendered by Chrome; "
        "install one, or render these two .md files to PDF by hand.")


def render(md_path: pathlib.Path) -> pathlib.Path:
    import markdown

    text = md_path.read_text(encoding="utf-8")
    html_body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
        output_format="html5",
    )
    title = md_path.stem.replace("_", " ").title()
    html = _HTML.format(title=title, css=CSS, body=html_body)

    pdf_path = md_path.with_suffix(".pdf")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        src = tmp / "doc.html"
        src.write_text(html, encoding="utf-8")
        out = tmp / "doc.pdf"
        # A fresh --user-data-dir keeps this from attaching to the user's own
        # running browser, which would ignore the headless flags entirely.
        subprocess.run(
            [_browser(), "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={tmp / 'profile'}",
             "--no-pdf-header-footer", "--print-to-pdf-no-header",
             f"--print-to-pdf={out}", src.as_uri()],
            check=True, capture_output=True, timeout=600)
        if not out.exists():
            raise SystemExit(f"{md_path.name}: the browser wrote no PDF")
        shutil.copyfile(out, pdf_path)
    return pdf_path


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    for arg in args:
        p = pathlib.Path(arg)
        if not p.is_absolute():
            p = ROOT / p
        pdf = render(p)
        try:
            import fitz
            pages = fitz.open(pdf).page_count
            print(f"  {pdf.relative_to(ROOT)}  —  {pages} pages, "
                  f"{pdf.stat().st_size // 1024} KB")
        except Exception:
            print(f"  {pdf.relative_to(ROOT)}  —  "
                  f"{pdf.stat().st_size // 1024} KB")
