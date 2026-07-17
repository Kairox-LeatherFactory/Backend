"""
wages — payroll.

Effective-dated Rates (per style x operation) and FROZEN wage runs. A closed run
is a snapshot: editing an old production event can never silently rewrite past
payroll.

THE FORK — on Employee.wage_type, and ONLY on Employee.wage_type:
    PIECE_RATE  wage = sum(qty * rate effective on the work_date). Nothing else.
                No daily floor, no attendance guarantee, no minimum. The pieces
                they cut ARE the wage.
    MONTHLY     wage = monthly_salary, prorated across the calendar months the
                run window touches. Completely independent of production — a
                monthly tailor who logs 400 pieces still earns exactly salary.

An employee gets EXACTLY ONE line per run. Never both. This matters because the
source file has TAILOR and CUTTER in both the monthly and the piece-rate blocks,
so "a salaried worker who also logs production" is the normal case, not an edge.

PERIODS ARE TYPED BY A HUMAN. The manager enters period_start and period_end.
The system does not derive them — but it does refuse windows that overlap an
already-closed run (409), because the same pieces must never be paid twice.

NO UUIDs CROSS THE WIRE. The rate API speaks style_code and operation_code, and
resolves them to ids server-side — same contract as /production/scan, which takes
sku_code. A style code is 'JP-CLERMONT_VEST': make_sku_code's first two segments,
so it is the literal prefix of every SKU code under that style. The manager reads
it straight off a printed traveler.

    style code   JP-CLERMONT_VEST                  <- what the rate API uses
    sku codes    JP-CLERMONT_VEST-DARK_BROWN-46    <- 12 of these per style
                 JP-CLERMONT_VEST-DARK_BROWN-48
                 JP-CLERMONT_VEST-BLACK-46  ...

Rates are per style x operation, so the picker is style-level. A SKU-level picker
would list all 12 codes above as separate rows that every one of them writes to
the same Rate row.
"""
