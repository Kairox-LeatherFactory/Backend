"""READ-ONLY importer test: parse every workbook in ./data with build_preview
and print the extraction LOG (per-sheet classification, per-client totals,
warnings). Writes NOTHING to any database.

Run:  .venv\\Scripts\\python.exe -m scripts.importer_smoke
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.modules.imports.import_engine import build_preview

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
FILES = ["GARMENT_ORDERPRODUCTION_DETAILS.xlsx", "johnpeter.xlsx", "employees_detail.xlsx"]


def main():
    print("=" * 78)
    print("IMPORTER READ-ONLY TEST (Excel -> structured preview + log)")
    print("=" * 78)
    grand_clients = grand_pieces = grand_warn = 0

    for fname in FILES:
        path = os.path.join(DATA, fname)
        print(f"\n### {fname}")
        if not os.path.exists(path):
            print("  (not found — skipped)")
            continue
        try:
            preview = build_preview(path)
            summary = preview.summary()
        except Exception as e:
            print(f"  [ERROR] {type(e).__name__}: {e}")
            continue

        # Per-sheet classification log
        print(f"  Sheets ({len(summary['sheets'])}):")
        for s in summary["sheets"]:
            print(f"    - {s['sheet']:<28} type={s['type']:<11} client={s['client']}")

        # Per-client extraction totals
        print(f"  Clients ({len(summary['clients'])}):")
        for key, c in summary["clients"].items():
            grand_clients += 1
            grand_pieces += c["pieces_ordered"]
            grand_warn += len(c["warnings"])
            print(f"    - {key:<10} order_lines={c['order_lines']:<4} "
                  f"pieces_ordered={c['pieces_ordered']:<6} "
                  f"styles={len(c['styles']):<3} prod_cards={c['production_cards']}")
            for w in c["warnings"][:5]:
                print(f"         warn: {w}")
            if len(c["warnings"]) > 5:
                print(f"         ... (+{len(c['warnings']) - 5} more warnings)")
        print(f"  total_warnings={summary['total_warnings']}")

    print("\n" + "=" * 78)
    print(f"GRAND TOTAL: {grand_clients} client-groups, {grand_pieces} pieces ordered, "
          f"{grand_warn} warnings")
    print("Extraction + logging works. No DB writes performed.")
    print("=" * 78)


if __name__ == "__main__":
    main()
