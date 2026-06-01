"""
production — the operational heart of the system.

production_event is the finest grain (one worker, one operation, one SKU, one
day, a quantity). Pieces are NOT assumed to conserve across stages; the spread
is a metric analytics surfaces, never an error this module rejects. A config
table (operation_access) maps which manager role may log which operation.
"""
