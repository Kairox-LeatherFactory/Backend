"""
clients — the order hierarchy: Client -> ClientOrder -> Style -> SKU.

The SKU (style + colour + size) is the atomic unit every production event and
wage calculation joins to. A CLIENT-role user is scoped to only their own orders.

NAMING: the buyer order is `client_order` (was `purchase_order`); the name
`purchase_order` now belongs to the supplier PO in the procurement module.
"""
