"""Manager Dashboards — a READ-ONLY module (no models, no writes).

Mirrors the analytics module's posture: it owns no tables and never mutates
state. It is a query + shaping surface over production_event / piece / sku /
style / client_order / material_lot / drawer / employee / audit_log, exposing
four manager dashboards defined in the requirements docs:

    Cutting   — leather cut side (LEATHER_CUTTING + leather lots).
    Lining    — lining cut side (LINING_CUTTING + lining lots); mirrors Cutting.
    Stitching — pre-store (Pasting/Fusing) → STORE → post-store (Line/Shell/Final)
                funnel. STORE is a derived DRAWER state, never a ProductionEvent.
    Store     — drawer movement: contents, hold, empty, employee traceability.

Every damage / expected-consumption / waste / employee-target / photo field is
part of the contract but returns 0/null with a reason in `meta.unsupported`,
because the current schema stores no damage state, no BOM baseline, and no
employee target/photo column. When those land, the fields populate with no shape
change on the client.
"""
