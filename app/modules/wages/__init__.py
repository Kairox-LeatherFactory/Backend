"""
wages — payroll.

Effective-dated Rates (per style x operation) and FROZEN wage runs. A closed run
is a snapshot: editing an old production event can never silently rewrite past
payroll. Forks on wage_type — piece_rate sums qty*rate; monthly is a fixed line.
"""
