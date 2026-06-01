"""
attendance — geofenced punch-in/out, shift policy, daily-wage hours.

Three flows from the spec:
  A. Self-service (managers / HR / permanent workers)
  B. Proxy by supervisor (daily-wage workers)
  C. Supervisor onboards a new daily-wage worker on the floor

Cross-module integration: production uses is_present_today() to refuse logging
output for workers not on the floor; wages uses total_hours() / days_present()
to compute pay for the DAILY_WAGE wage type.
"""
