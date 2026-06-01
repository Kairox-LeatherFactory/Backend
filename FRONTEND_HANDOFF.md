# Frontend handoff — start building today, don't wait for the backend

1. Get the contract:  `python -m scripts.export_openapi`  -> `openapi.json`
2. Run a full fake API:  `npx @stoplight/prism-cli mock openapi.json`
   (or use MSW with handlers generated from openapi.json)
3. Build against these endpoints — shapes are final:

Screens to build (mapped to user roles):
- Login (Supabase Auth) -> token carries role in user_metadata.role
- Direct Manager: dashboard (GET /analytics/overview), alerts
  (GET /analytics/alerts/freight-risk, /stage-spread), wage runs.
- Cutting Manager: production entry form, CUTTING only.
  (GET /production/operations, POST /production/events)
- Stitching Manager: production entry, FUSING..LS.
- Orders browser grouped by client:
  GET /clients -> GET /clients/{id}/orders (nested PO->Style->SKU).
- Style progress card (the digital Carnaby card):
  GET /production/styles/{style_id}/progress -> {stages: {CUTTING:152,...}}

Auth header for every call:  Authorization: Bearer <supabase_jwt>
Role gates the production entry screen — UI should hide operations the role
can't enter (the API enforces it too, returning 403).
