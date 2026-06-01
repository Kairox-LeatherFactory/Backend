"""
clients — the order hierarchy: Client -> PurchaseOrder -> Style -> SKU.

The SKU (style + colour + size) is the atomic unit every production event and
wage calculation joins to. A CLIENT-role user is scoped to only their own orders.
"""
