"""
attendance — punch-in/out, shift policy, piece-rate floor hours.

LOCATION TRACKING IS REMOVED: there is no geofence, no factory coordinate
and no device coordinate on any write path. A punch is authorised by the
operator's login plus the employee's card. See geofence.py for the disabled
implementation and how to switch it back on.

Three flows from the spec:
  A. Self-service (managers / HR / permanent workers)
  B. Proxy by supervisor (piece-rate workers)
  C. Supervisor onboards a new piece-rate worker on the floor

Cross-module integration: production uses is_present_today() to refuse logging
output for workers not on the floor; wages uses total_hours() / days_present()
to compute pay for the PIECE_RATE wage type.
"""
