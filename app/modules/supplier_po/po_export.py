"""
================================================================================
modules/supplier_po/po_export.py — Stage-5 supplier-PO PDF (§2)
================================================================================

Renders a supplier PO shaped like the real `suppler-po-form/*.pdf`: a fixed buyer
block (PAKKAR TANVEER EXPORTS, from config), a Bill-To supplier block, the PO number
+ date + buyer-ref tag, the line grid, the CGST/SGST (or IGST) tax footer, and the
fixed terms block.

ONE TEMPLATE PER SUPPLIER TYPE (§2a/§2b). All forms share ~90% boilerplate; the ONE
part that varies by type is the line-grid UOM column header + the default UOM. That
variation is config (`config/po_templates.yaml` → `template_cfg`), so onboarding a new
supplier type is a registry row + (optionally) a child HTML template — NO code branch.
The per-type HTML files in `templates/po/` mirror this renderer for a Jinja/WeasyPrint
path; this module is the offline-safe Python renderer the service calls.

ENGINE PRECEDENCE mirrors the Stage-3 BOM export: WeasyPrint → ReportLab → raw HTML,
so export always yields a durable artifact. Blocking → callers invoke from a threadpool.

FUNCTION GUIDE  (mirrors bom/export.py; cfg = the per-supplier-type template config)
  DEFAULT_TEMPLATE_CFG   the accessory fallback when a supplier_type has no registry row.
  _fmt(v) [private] money formatting.
  _line_rows_html(view, uom_header) [private] the line-grid <tr> rows.
  _build_html(view, meta, cfg) [private] the full PO HTML: buyer block + Bill-To + grid +
      CGST/SGST-or-IGST footer + terms. The ONE per-type variation is the UOM header (cfg).
  _reportlab_pdf(view, meta, cfg) [private] the pure-Python fallback render.
  render_po_pdf(view, meta, template_cfg?) -> (bytes, mime, ext)
      THE ENTRY POINT. WeasyPrint → ReportLab → raw HTML. CALLED FROM: PoService.send_po
      (in a threadpool); the bytes are sha256-deduped into a Document(kind=supplier_po_pdf).
================================================================================
"""
from __future__ import annotations

from html import escape

# Sensible default if a supplier_type has no registry row (the §2b fallback).
DEFAULT_TEMPLATE_CFG = {
    "template_name": "accessory", "uom_header": "UOM", "default_uom": "NOS",
}


def _fmt(v, dash: str = "-") -> str:
    return dash if v is None else (f"{v:,.2f}" if isinstance(v, (int, float)) else str(v))


def _line_rows_html(view: dict, uom_header: str) -> str:
    rows = []
    for i in view.get("items", []):
        rows.append(
            "<tr>"
            f"<td class='n'>{escape(str(i.get('item_no') or ''))}</td>"
            f"<td>{escape(str(i.get('description') or ''))}</td>"
            f"<td>{escape(str(i.get('color') or ''))}</td>"
            f"<td>{escape(str(i.get('uom') or ''))}</td>"
            f"<td class='n'>{_fmt(i.get('qty'))}</td>"
            f"<td class='n'>{_fmt(i.get('unit_price'))}</td>"
            f"<td class='n'>{_fmt(i.get('amount'))}</td>"
            "</tr>"
        )
    return "".join(rows)


def _build_html(view: dict, meta: dict, cfg: dict) -> str:
    cur = view.get("currency") or "INR"
    sup = view.get("supplier") or {}
    buyer = meta.get("buyer") or {}
    uom_header = cfg.get("uom_header", "UOM")
    gst_mode = view.get("gst_mode") or "INTRA"
    if gst_mode == "INTER":
        tax_rows = (f"<tr><td colspan='6'>IGST</td><td class='n'>{cur} {_fmt(view.get('igst'))}</td></tr>")
    else:
        tax_rows = (
            f"<tr><td colspan='6'>CGST</td><td class='n'>{cur} {_fmt(view.get('cgst'))}</td></tr>"
            f"<tr><td colspan='6'>SGST</td><td class='n'>{cur} {_fmt(view.get('sgst'))}</td></tr>"
        )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      body {{ font-family: Helvetica, Arial, sans-serif; font-size: 11px; color:#111; }}
      h1 {{ font-size: 16px; margin: 0 0 2px; }}
      .blk {{ margin-bottom:8px; }}
      .blk b {{ display:inline-block; min-width:80px; }}
      .cols {{ display:flex; justify-content:space-between; }}
      table {{ width:100%; border-collapse: collapse; margin-top:6px; }}
      th, td {{ border:1px solid #999; padding:4px 6px; text-align:left; }}
      th {{ background:#eee; }}
      td.n, th.n {{ text-align:right; }}
      tfoot td {{ font-weight:bold; }}
      .terms {{ margin-top:12px; font-size:10px; color:#444; }}
    </style></head><body>
      <h1>PURCHASE ORDER</h1>
      <div class="cols">
        <div class="blk">
          <div><b>{escape(str(buyer.get('name') or ''))}</b></div>
          <div>{escape(str(buyer.get('address') or ''))}</div>
          <div>GSTIN: {escape(str(buyer.get('gstin') or ''))}</div>
          <div>{escape(str(buyer.get('email') or ''))} {escape(str(buyer.get('phone') or ''))}</div>
        </div>
        <div class="blk">
          <div><b>P/O #</b> {escape(str(view.get('po_number') or 'DRAFT'))}</div>
          <div><b>Date</b> {escape(str(view.get('issue_date') or meta.get('generated_at') or '-'))}</div>
          <div><b>Ref</b> {escape(str(view.get('buyer_ref') or '-'))}</div>
        </div>
      </div>
      <div class="blk">
        <b>Bill To:</b> {escape(str(sup.get('name') or '-'))}
        &nbsp; {escape(str(sup.get('address') or ''))}
        &nbsp; GSTIN: {escape(str(sup.get('gstin') or '-'))}
        &nbsp; {escape(str(sup.get('email') or sup.get('phone') or ''))}
      </div>
      <table>
        <thead><tr>
          <th class="n">#</th><th>Description</th><th>Colour</th>
          <th>{escape(uom_header)}</th><th class="n">Qty</th>
          <th class="n">Rate</th><th class="n">Amount</th>
        </tr></thead>
        <tbody>{_line_rows_html(view, uom_header)}</tbody>
        <tfoot>
          <tr><td colspan="6">Invoice Subtotal</td>
              <td class="n">{cur} {_fmt(view.get('subtotal'))}</td></tr>
          {tax_rows}
          <tr><td colspan="6">Round Off</td>
              <td class="n">{cur} {_fmt(view.get('round_off'))}</td></tr>
          <tr><td colspan="6">TOTAL</td>
              <td class="n">{cur} {_fmt(view.get('total'))}</td></tr>
        </tfoot>
      </table>
      <div class="terms">
        Please send 2 copies of your invoice. Delivery: {view.get('delivery_days') or '-'} days.
        Payment terms: {view.get('payment_terms_days') or '-'} days. For {escape(str(buyer.get('name') or ''))}.
      </div>
    </body></html>"""


def _reportlab_pdf(view: dict, meta: dict, cfg: dict) -> bytes:
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Purchase Order")
    styles = getSampleStyleSheet()
    cur = view.get("currency") or "INR"
    sup = view.get("supplier") or {}
    buyer = meta.get("buyer") or {}
    flow = [
        Paragraph("Purchase Order", styles["Title"]),
        Paragraph(f"{buyer.get('name') or ''} — GSTIN {buyer.get('gstin') or ''}", styles["Normal"]),
        Paragraph(f"P/O #: {view.get('po_number') or 'DRAFT'} &nbsp; "
                  f"Date: {view.get('issue_date') or meta.get('generated_at') or '-'} &nbsp; "
                  f"Ref: {view.get('buyer_ref') or '-'}", styles["Normal"]),
        Paragraph(f"Bill To: {sup.get('name') or '-'} &nbsp; GSTIN: {sup.get('gstin') or '-'}",
                  styles["Normal"]),
        Spacer(1, 8),
    ]
    header = ["#", "Description", "Colour", cfg.get("uom_header", "UOM"),
              "Qty", "Rate", "Amount"]
    data = [header]
    for i in view.get("items", []):
        data.append([
            i.get("item_no") or "", i.get("description") or "", i.get("color") or "",
            i.get("uom") or "", _fmt(i.get("qty")), _fmt(i.get("unit_price")),
            _fmt(i.get("amount")),
        ])
    data.append(["", "", "", "", "", "Subtotal", _fmt(view.get("subtotal"))])
    if (view.get("gst_mode") or "INTRA") == "INTER":
        data.append(["", "", "", "", "", "IGST", _fmt(view.get("igst"))])
    else:
        data.append(["", "", "", "", "", "CGST", _fmt(view.get("cgst"))])
        data.append(["", "", "", "", "", "SGST", _fmt(view.get("sgst"))])
    data.append(["", "", "", "", "", "TOTAL", _fmt(view.get("total"))])
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    flow.append(table)
    flow.append(Spacer(1, 10))
    flow.append(Paragraph(
        f"Delivery {view.get('delivery_days') or '-'} days · payment "
        f"{view.get('payment_terms_days') or '-'} days · send 2 invoice copies.",
        styles["Italic"],
    ))
    doc.build(flow)
    return buf.getvalue()


def render_po_pdf(view: dict, meta: dict, template_cfg: dict | None = None) -> tuple[bytes, str, str]:
    """Render the PO. Returns (bytes, mime, ext). WeasyPrint → ReportLab → raw HTML."""
    cfg = template_cfg or DEFAULT_TEMPLATE_CFG
    html = _build_html(view, meta, cfg)
    try:
        from weasyprint import HTML
        return HTML(string=html).write_pdf(), "application/pdf", ".pdf"
    except Exception:
        pass
    try:
        return _reportlab_pdf(view, meta, cfg), "application/pdf", ".pdf"
    except Exception:
        pass
    return html.encode("utf-8"), "text/html", ".html"
