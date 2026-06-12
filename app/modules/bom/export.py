"""
================================================================================
modules/procurement/export.py — Stage-3 BOM PDF export (§4)
================================================================================

Renders the approved BOM as a "Yardage & Price Quotation" PDF shaped like
`data/BMO-1.pdf`. The input is the SERVER's current persisted BOM view (the
post-edit source of truth) — never the original extraction — and export is only
called from a locked BOM, so the numbers are frozen.

ENGINE PRECEDENCE (stage-3 spec §4a — WeasyPrint primary, ReportLab fallback)
    1. WeasyPrint (HTML/CSS) — the recommended primary; best fit for the invoice-
       like quotation layout. Needs native libs (cairo/pango), present in the
       Docker/Linux runtime. Used when importable.
    2. ReportLab (pure-Python) — the fallback that runs everywhere incl. Windows
       dev + the SQLite test suite, with no native deps. Pinned in requirements.
    3. Raw HTML bytes — last resort if neither lib is installed; still a durable,
       self-verifying artifact (mime text/html) so export never hard-fails.

Rendering is blocking → callers invoke `render_bom_pdf` from a threadpool
(bom_service does). The revision + approver identity + timestamp are stamped onto
the artifact (the workflow doc's "locked, full revision history" requirement).
================================================================================
"""
from __future__ import annotations

from html import escape


def _fmt(v, dash: str = "-") -> str:
    return dash if v is None else (f"{v:,.2f}" if isinstance(v, (int, float)) else str(v))


def _build_html(view: dict, meta: dict) -> str:
    cur = view.get("currency") or ""
    rows = []
    for i in view.get("items", []):
        rows.append(
            "<tr>"
            f"<td>{escape(str(i.get('category') or ''))}</td>"
            f"<td>{escape(str(i.get('name') or ''))}</td>"
            f"<td>{escape(str(i.get('material_color') or ''))}</td>"
            f"<td class='n'>{_fmt(i.get('qty_per_garment'))}</td>"
            f"<td>{escape(str(i.get('uom') or ''))}</td>"
            f"<td class='n'>{_fmt(i.get('unit_price'))}</td>"
            f"<td class='n'>{_fmt(i.get('bulk_qty'))}</td>"
            f"<td class='n'>{_fmt(i.get('total_cost'))}</td>"
            f"<td>{escape(str(i.get('dcm_source') or ''))}</td>"
            "</tr>"
        )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      body {{ font-family: Helvetica, Arial, sans-serif; font-size: 11px; color:#111; }}
      h1 {{ font-size: 18px; margin: 0 0 2px; }}
      .meta {{ margin-bottom: 10px; color:#333; }}
      .meta b {{ display:inline-block; min-width:120px; }}
      table {{ width:100%; border-collapse: collapse; }}
      th, td {{ border:1px solid #999; padding:4px 6px; text-align:left; }}
      th {{ background:#eee; }}
      td.n, th.n {{ text-align:right; }}
      tfoot td {{ font-weight:bold; }}
      .stamp {{ margin-top:14px; font-size:10px; color:#555; }}
    </style></head><body>
      <h1>Yardage &amp; Price Quotation</h1>
      <div class="meta">
        <div><b>Style</b> {escape(str(meta.get('style_name') or '-'))}</div>
        <div><b>Order</b> {escape(str(meta.get('order_number') or '-'))}</div>
        <div><b>BOM</b> {escape(str(view.get('id') or '-'))}</div>
        <div><b>Status</b> {escape(str(view.get('status') or '-'))} (rev {view.get('revision')})</div>
        <div><b>Order qty</b> {view.get('order_qty')}</div>
      </div>
      <table>
        <thead><tr>
          <th>Category</th><th>Material</th><th>Colour</th><th class="n">DCM/garment</th>
          <th>UOM</th><th class="n">Unit price</th><th class="n">Bulk qty</th>
          <th class="n">Total</th><th>DCM source</th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody>
        <tfoot><tr>
          <td colspan="7">FOB / garment: {cur} {_fmt(view.get('garment_fob_price'))}
              &nbsp;|&nbsp; Bulk total:</td>
          <td class="n">{cur} {_fmt(view.get('bulk_total'))}</td><td></td>
        </tr></tfoot>
      </table>
      <div class="stamp">
        Approved by {escape(str(meta.get('approved_by') or '-'))}
        on {escape(str(meta.get('approved_at') or '-'))} ·
        revision {view.get('revision')} · generated {escape(str(meta.get('generated_at') or '-'))}
      </div>
    </body></html>"""


def _reportlab_pdf(view: dict, meta: dict) -> bytes:
    """Pure-Python fallback render (no native deps)."""
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), title="Yardage & Price Quotation")
    styles = getSampleStyleSheet()
    cur = view.get("currency") or ""
    flow = [
        Paragraph("Yardage &amp; Price Quotation", styles["Title"]),
        Paragraph(
            f"Style: {meta.get('style_name') or '-'} &nbsp; | &nbsp; "
            f"Order: {meta.get('order_number') or '-'} &nbsp; | &nbsp; "
            f"BOM: {view.get('id')} &nbsp; | &nbsp; "
            f"Status: {view.get('status')} (rev {view.get('revision')})",
            styles["Normal"],
        ),
        Spacer(1, 10),
    ]
    header = ["Category", "Material", "Colour", "DCM/grmt", "UOM",
              "Unit price", "Bulk qty", "Total", "DCM source"]
    data = [header]
    for i in view.get("items", []):
        data.append([
            i.get("category") or "", i.get("name") or "", i.get("material_color") or "",
            _fmt(i.get("qty_per_garment")), i.get("uom") or "",
            _fmt(i.get("unit_price")), _fmt(i.get("bulk_qty")),
            _fmt(i.get("total_cost")), i.get("dcm_source") or "",
        ])
    data.append(["", "", "", "", "", "", f"FOB {cur}", _fmt(view.get("garment_fob_price")), ""])
    data.append(["", "", "", "", "", "", f"Bulk {cur}", _fmt(view.get("bulk_total")), ""])
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("FONTNAME", (0, -2), (-1, -1), "Helvetica-Bold"),
    ]))
    flow.append(table)
    flow.append(Spacer(1, 12))
    flow.append(Paragraph(
        f"Approved by {meta.get('approved_by') or '-'} on {meta.get('approved_at') or '-'} · "
        f"revision {view.get('revision')} · generated {meta.get('generated_at') or '-'}",
        styles["Italic"],
    ))
    doc.build(flow)
    return buf.getvalue()


def render_bom_pdf(view: dict, meta: dict) -> tuple[bytes, str, str]:
    """Render the BOM. Returns (bytes, mime, ext). Tries WeasyPrint → ReportLab →
    raw HTML so export always yields a durable artifact."""
    html = _build_html(view, meta)
    try:
        from weasyprint import HTML                       # primary (Docker/Linux)
        return HTML(string=html).write_pdf(), "application/pdf", ".pdf"
    except Exception:
        pass
    try:
        return _reportlab_pdf(view, meta), "application/pdf", ".pdf"   # fallback
    except Exception:
        pass
    return html.encode("utf-8"), "text/html", ".html"     # last resort
