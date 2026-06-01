"""
imports — idempotent Excel ingestion.

New purchase orders arrive as spreadsheets forever, so this is not a one-time
migration. A two-step preview/commit flow lets a human see exactly what will be
written before anything is stored. Re-importing the same file does not duplicate
rows. The parsing layer is defensive about the real files' messiness (trailing
spaces, numbers-as-text, stacked multi-size blocks).
"""
