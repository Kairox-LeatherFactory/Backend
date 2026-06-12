"""
================================================================================
modules/procurement/supplier_service.py — Stage-5 supplier import, CRUD, matching
================================================================================

Owns the supplier side of Stage 5:
  - the idempotent importer (§9a): preview (dry-run) + commit (upsert `supplier` keyed
    on name; rebuild `supplier_supply_history` from the provision ledger; absent rows
    soft-deactivate, never hard-delete), mirroring the Stage-4 inventory sync;
  - supplier CRUD (§9b/§9c): list/get/add/edit/soft-delete/reactivate, each audited;
  - the article→supplier matcher (§1): deterministic ledger → alias → category → advisory
    fuzzy → unresolved, with recency/frequency/contactability ranking (supplier_match.py).

LAYERING. Reads only procurement-owned tables. Blocking openpyxl work runs in a
threadpool (house async rule).
================================================================================
"""
from __future__ import annotations

import uuid
from collections import Counter
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.modules.supplier_po import presenters as present
from app.modules.supplier_po.enums import SupplierEmailStatus
from app.modules.inventory.inventory_match import Alias
from app.modules.inventory.inventory_normalize import bom_line_key, normalize_key, tokens
from app.core.models import AuditLog
from app.modules.supplier_po.models import Supplier, SupplierSupplyHistory
from app.modules.supplier_po.repository import SupplierPoRepository
from app.modules.supplier_po.supplier_import import parse_suppliers
from app.modules.supplier_po.supplier_match import (
    HistoryCandidate,
    MatchResult,
    match_shortfall,
)
from app.modules.supplier_po.supplier_normalize import (
    mode_for_bom_category,
    state_code_from_gstin,
    supplier_type_for_mode,
)


class SupplierService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = SupplierPoRepository(db)

    # ══════════════════════════════════════════════════════════════════════
    # Import (§9a) — preview + commit
    # ══════════════════════════════════════════════════════════════════════
    async def preview(self, data: bytes) -> dict:
        prev = await run_in_threadpool(parse_suppliers, data)
        return self._preview_block(prev)

    async def commit(self, user, data: bytes) -> dict:
        prev = await run_in_threadpool(parse_suppliers, data)

        # dominant history mode per supplier → supplier_type default (§10)
        modes_by_supplier: dict[str, Counter] = {}
        for h in prev.history:
            modes_by_supplier.setdefault(h.supplier_normalized_name, Counter())
            if h.mode:
                modes_by_supplier[h.supplier_normalized_name][h.mode] += h.txn_count

        # 1. upsert suppliers (sheet-wins-if-present, else keep) + map name→id
        id_by_norm: dict[str, uuid.UUID] = {}
        upserted = 0
        for s in prev.suppliers:
            existing = await self.repo.get_supplier_by_name(s.name)
            counter = modes_by_supplier.get(s.normalized_name)
            dominant = counter.most_common(1)[0][0] if counter else None
            stype = supplier_type_for_mode(dominant)
            if existing is None:
                existing = Supplier(
                    name=s.name, phone=s.phone, email=s.email, service=s.service,
                    supplier_type=stype,
                    email_status=(SupplierEmailStatus.UNKNOWN.value if s.email
                                  else SupplierEmailStatus.UNKNOWN.value),
                    is_active=True,
                )
                self.db.add(existing)
                await self.db.flush()
            else:
                existing.phone = existing.phone or s.phone        # never blank a known value
                existing.email = existing.email or s.email
                existing.service = existing.service or s.service
                existing.supplier_type = existing.supplier_type or stype
                existing.state_code = existing.state_code or state_code_from_gstin(existing.gstin)
                existing.is_active = True
            id_by_norm[s.normalized_name] = existing.id
            upserted += 1

        # 2. rebuild supply history wholesale (§1d 'refreshed on re-import')
        await self.repo.clear_supply_history()
        for h in prev.history:
            sid = id_by_norm.get(h.supplier_normalized_name)
            if sid is None:
                continue
            self.db.add(SupplierSupplyHistory(
                supplier_id=sid, normalized_description=h.normalized_description,
                raw_description=h.raw_description, mode=h.mode, uom=h.uom,
                txn_count=h.txn_count, first_purchased_at=h.first_purchased_at,
                last_purchased_at=h.last_purchased_at, last_rate=h.last_rate,
                min_rate=h.min_rate, max_rate=h.max_rate,
            ))

        # 3. soft-deactivate suppliers absent from this import (never hard-delete §9c)
        keep_ids = set(id_by_norm.values())
        deactivated = 0
        for sup in await self.repo.all_active_suppliers():
            if sup.id not in keep_ids:
                sup.is_active = False
                deactivated += 1

        self.db.add(AuditLog(
            actor_user_id=getattr(user, "id", None), action="SUPPLIER_IMPORT",
            entity_type="supplier", at=datetime.now(timezone.utc),
            after={"suppliers": upserted, "history_rows": len(prev.history),
                   "deactivated": deactivated},
        ))
        await self.db.commit()
        return {**self._preview_block(prev), "committed": upserted,
                "history_rows": len(prev.history), "deactivated": deactivated}

    @staticmethod
    def _preview_block(prev) -> dict:
        return {
            "contact_rows": prev.contact_rows,
            "provision_rows": prev.provision_rows,
            "suppliers": len(prev.suppliers),
            "suppliers_with_phone": prev.suppliers_with_phone,
            "suppliers_with_email": prev.suppliers_with_email,
            "history_rows": len(prev.history),
            "warnings": prev.warnings,
            "sample_history": [{
                "supplier": h.supplier_name,
                "article": h.normalized_description, "mode": h.mode,
                "txn_count": h.txn_count, "last_rate": float(h.last_rate) if h.last_rate else None,
            } for h in prev.history[:25]],
        }

    # ══════════════════════════════════════════════════════════════════════
    # CRUD (§9b/§9c)
    # ══════════════════════════════════════════════════════════════════════
    async def list_suppliers(self, *, search=None, service=None, active=None,
                             limit=100, offset=0) -> dict:
        rows = await self.repo.list_suppliers(search=search, service=service,
                                              active=active, limit=limit, offset=offset)
        return {"suppliers": [present.supplier_block(s) for s in rows], "count": len(rows)}

    async def get_supplier(self, supplier_id: uuid.UUID) -> dict:
        s = await self.repo.get_supplier(supplier_id)
        if s is None:
            raise HTTPException(404, "Supplier not found.")
        history = await self.repo.supply_history_for_supplier(supplier_id)
        open_pos = await self.repo.supplier_open_pos(supplier_id)
        return present.supplier_block(s, history=history, open_pos=open_pos)

    async def create_supplier(self, user, data: dict) -> dict:
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(422, detail={"error": "name_required"})
        if await self.repo.get_supplier_by_name(name):
            raise HTTPException(409, detail={"error": "duplicate_name", "name": name})
        s = Supplier(
            name=name, phone=data.get("phone"), email=data.get("email"),
            service=data.get("service"), gstin=data.get("gstin"),
            address=data.get("address"),
            currency=data.get("currency") or "INR",
            payment_terms_days=data.get("payment_terms_days") or 60,
            lead_time_days=data.get("lead_time_days") or 10,
            supplier_type=data.get("supplier_type"),
            whatsapp_phone=data.get("whatsapp_phone") or data.get("phone"),
            state_code=state_code_from_gstin(data.get("gstin")),
            email_status=(SupplierEmailStatus.VALID.value if data.get("email")
                          else SupplierEmailStatus.UNKNOWN.value),
            is_active=True,
        )
        self.db.add(s)
        await self.db.flush()
        await self._audit(user, "SUPPLIER_CREATE", s.id, after=self._snap(s))
        await self.db.commit()
        return await self.get_supplier(s.id)

    async def update_supplier(self, user, supplier_id: uuid.UUID, data: dict) -> dict:
        s = await self.repo.get_supplier(supplier_id)
        if s is None:
            raise HTTPException(404, "Supplier not found.")
        before = self._snap(s)
        for f in ("name", "phone", "email", "service", "gstin", "address", "currency",
                  "payment_terms_days", "lead_time_days", "supplier_type", "whatsapp_phone"):
            if f in data and data[f] is not None:
                setattr(s, f, data[f])
        if "gstin" in data and data["gstin"]:
            s.state_code = state_code_from_gstin(data["gstin"])
        # editing a contact onto a previously contactless matched vendor clears the
        # block + (for a fresh email) resets the bounce flag so §5 send is unblocked (§9c).
        if data.get("email"):
            s.email_status = SupplierEmailStatus.VALID.value
        await self._audit(user, "SUPPLIER_EDIT", s.id, before=before, after=self._snap(s))
        await self.db.commit()
        return await self.get_supplier(s.id)

    async def deactivate_supplier(self, user, supplier_id: uuid.UUID) -> dict:
        s = await self.repo.get_supplier(supplier_id)
        if s is None:
            raise HTTPException(404, "Supplier not found.")
        before = self._snap(s)
        s.is_active = False
        await self._audit(user, "SUPPLIER_DEACTIVATE", s.id, before=before, after=self._snap(s))
        await self.db.commit()
        return {"id": str(s.id), "is_active": s.is_active}

    async def reactivate_supplier(self, user, supplier_id: uuid.UUID) -> dict:
        s = await self.repo.get_supplier(supplier_id)
        if s is None:
            raise HTTPException(404, "Supplier not found.")
        s.is_active = True
        await self._audit(user, "SUPPLIER_REACTIVATE", s.id, after=self._snap(s))
        await self.db.commit()
        return {"id": str(s.id), "is_active": s.is_active}

    # ══════════════════════════════════════════════════════════════════════
    # Matching (§1) — used by po_service when generating POs
    # ══════════════════════════════════════════════════════════════════════
    async def match_line(self, *, name: str | None, color: str | None,
                         category: str | None, aliases: list[Alias],
                         today: date | None = None) -> MatchResult:
        """Resolve one shortfall line to a supplier (§1b/§1c). Builds the line key the
        same way Stage 4 does, fetches the small candidate pool set-based, and runs the
        deterministic-first matcher + ranking."""
        today = today or datetime.now(timezone.utc).date()
        bom_key = bom_line_key(name, color)
        line_tokens = tokens(bom_key)

        like_terms: set[str] = set()
        if line_tokens:
            like_terms.add(max(line_tokens, key=len))
        for al in aliases:
            if tokens(al.bom_term) and tokens(al.bom_term) <= line_tokens:
                like_terms.add(al.inventory_key)
        category_mode = mode_for_bom_category(category)
        modes = {category_mode} if category_mode else None

        rows = await self.repo.fetch_supply_history_candidates(
            like_terms=like_terms, modes=modes)
        candidates = [
            HistoryCandidate(
                supplier_id=s.id, supplier_name=s.name,
                normalized_description=h.normalized_description, mode=h.mode,
                txn_count=h.txn_count, last_purchased_at=h.last_purchased_at,
                last_rate=h.last_rate, uom=h.uom,
                has_contact=bool(s.email or s.phone),
            )
            for (h, s) in rows
        ]
        return match_shortfall(bom_key, color, category_mode, candidates, aliases, today=today)

    # ── helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _snap(s: Supplier) -> dict:
        return {"name": s.name, "phone": s.phone, "email": s.email, "service": s.service,
                "gstin": s.gstin, "is_active": s.is_active, "email_status": s.email_status,
                "supplier_type": s.supplier_type}

    async def _audit(self, user, action, entity_id, *, before=None, after=None) -> None:
        self.db.add(AuditLog(
            actor_user_id=getattr(user, "id", None), action=action,
            entity_type="supplier", entity_id=entity_id, before=before, after=after,
            at=datetime.now(timezone.utc),
        ))
