"""
================================================================================
modules/bom/notification_service.py — Stage-3 BOM-ready notifications (§2)
================================================================================

The in-app "BOM is ready for review" notice + its 2-hour auto-email escalation.

DESIGN (stage-3 spec §2)
  - Delivery is a VIEW; the `notification` table is the source of truth. SSE pushes
    (GET /notifications/stream), a plain GET polls (GET /notifications), and both read
    the same rows — a missed/closed stream loses nothing.
  - On `ready_for_review` BOTH the MD and the DM get their OWN row (two recipients,
    two independent 2-hour escalations). `scheduled_for = now + escalation_hours`.
  - `opened_at` (set when a recipient views the notice) CANCELS the email escalation.
  - The 2-hour sweep is DB-driven + idempotent (repo.due_escalations' NOT-EXISTS
    guard), so it survives restarts and never double-emails. The single-replica
    caveat lives in config (notification_sweeper_enabled / the docstring there).

LAYERING. This service owns the `notification` table (procurement). The MD/DM
recipient users come from `users.service` (a permitted service→service call) — it
never queries the users repository directly (CLAUDE.md §3.2).

FUNCTION GUIDE
  STREAM_POLL_SECONDS  how often an open SSE stream re-checks the table (10s).
  NotificationService(db)  holds the session + a BomRepository.
  create_review_notifications(bom_id, *, style_name?, order_number?) -> [Notification]
      Write one in-app row per recipient (MD always; DM if notify_dm_on_review), each
      with scheduled_for = now + escalation_hours. CALLED FROM: BomService.confirm_cutting.
  list_for_user(user_id, *, unread_only=False) -> {notifications, unread}
      The poll fallback + initial load. CALLED FROM: GET /notifications.
  mark_opened(notification_id, user) -> dict
      Stamp opened_at + status=opened (recipient only) — the signal that CANCELS the
      email escalation. CALLED FROM: POST /notifications/{id}/open.
  stream(user_id, *, poll_seconds?) -> async generator of SSE frames
      Emits the unseen backlog on connect (marking each SENT), then polls for new rows
      with keep-alive comments until the client disconnects. CALLED FROM: GET /notifications/stream.
  run_escalations() -> int
      The 2-hour sweep: find due+unseen+un-escalated notices (repo.due_escalations),
      send an email child per row (threadpool, idempotent), return the count dispatched.
      CALLED FROM: the in-process sweeper started in main.py lifespan.
  _view(n) -> dict   [static] serialize a Notification for the API/SSE.
================================================================================
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.enums import UserRole
from app.core.enums import (
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)
from app.core.models import Notification
from app.core.notifier import get_notifier
from app.modules.bom.repository import BomRepository

# SSE poll cadence for the live stream (how often the open stream re-checks the
# table for new/updated rows). Short — it's a cheap indexed query per recipient.
STREAM_POLL_SECONDS = 10


class NotificationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = BomRepository(db)

    # ══════════════════════════════════════════════════════════════════════
    # Create — fired when a BOM enters `ready_for_review` (§2a/§2b)
    # ══════════════════════════════════════════════════════════════════════
    async def create_review_notifications(
        self, bom_id: uuid.UUID, *, style_name: str | None = None,
        order_number: str | None = None,
    ) -> list[Notification]:
        """One in-app `notification` row per recipient (MD always; DM when
        `notify_dm_on_review`). Each carries `scheduled_for = now + escalation_hours`
        — the deadline the sweeper escalates at if still unseen."""
        from app.modules.users.service import UserService

        roles = [UserRole.MANAGING_DIRECTOR]
        if settings.notify_dm_on_review:
            roles.append(UserRole.DIRECT_MANAGER)
        recipients = await UserService(self.db).list_by_roles(roles)
        if not recipients:
            return []

        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=settings.bom_review_escalation_hours)
        subject = "BOM ready for review"
        label = style_name or "a style"
        if order_number:
            label = f"{label} (order {order_number})"
        link = f"{settings.frontend_base_url}/boms/{bom_id}"
        body = f"BOM for {label} is ready for your review and approval. {link}"

        created: list[Notification] = []
        # de-dupe recipients (MD who is also flagged etc.) by id
        seen: set[uuid.UUID] = set()
        for u in recipients:
            if u.id in seen:
                continue
            seen.add(u.id)
            n = Notification(
                recipient_user_id=u.id,
                channel=NotificationChannel.IN_APP.value,
                type=NotificationType.BOM_AWAITING_REVIEW.value,
                subject=subject, body=body,
                entity_type="bom", entity_id=bom_id,
                status=NotificationStatus.PENDING.value,
                scheduled_for=deadline,
            )
            self.db.add(n)
            created.append(n)
        await self.db.commit()
        for n in created:
            await self.db.refresh(n)
        return created

    # ══════════════════════════════════════════════════════════════════════
    # Read — the poll fallback (§2a)
    # ══════════════════════════════════════════════════════════════════════
    async def list_for_user(self, user_id: uuid.UUID, *, unread_only: bool = False) -> dict:
        rows = await self.repo.list_notifications_for_user(user_id, unread_only=unread_only)
        items = [self._view(n) for n in rows]
        return {"notifications": items, "unread": sum(1 for n in rows if n.opened_at is None)}

    async def mark_opened(self, notification_id: uuid.UUID, user) -> dict:
        """Mark a notice SEEN (sets `opened_at` + status `opened`) — the signal that
        CANCELS the 2-hour email escalation. Only the recipient may open it."""
        n = await self.repo.get_notification(notification_id)
        if n is None:
            raise HTTPException(404, "Notification not found.")
        if n.recipient_user_id != getattr(user, "id", None):
            raise HTTPException(403, "Not your notification.")
        if n.opened_at is None:
            n.opened_at = datetime.now(timezone.utc)
            n.status = NotificationStatus.OPENED.value
            await self.repo.save(n)
        return self._view(n)

    # ══════════════════════════════════════════════════════════════════════
    # SSE — the live push (§2a). Backbone is still the table; this is a view.
    # ══════════════════════════════════════════════════════════════════════
    async def stream(self, user_id: uuid.UUID, *, poll_seconds: int | None = None):
        """Async generator of `text/event-stream` frames for one recipient. Emits the
        unseen backlog on connect (marking each SENT so it isn't re-pushed), then
        polls the table for new rows until the client disconnects (cancellation ends
        the generator). The DB stays the source of truth, so a dropped/reconnected
        stream replays from `list_for_user`/this backlog — nothing is lost."""
        import json

        interval = poll_seconds or STREAM_POLL_SECONDS
        seen_ids: set[str] = set()

        async def _emit_new() -> list[str]:
            rows = await self.repo.list_notifications_for_user(user_id, unread_only=True)
            frames: list[str] = []
            for n in rows:
                if str(n.id) in seen_ids:
                    continue
                seen_ids.add(str(n.id))
                if n.status == NotificationStatus.PENDING.value:
                    n.status = NotificationStatus.SENT.value
                    n.sent_at = datetime.now(timezone.utc)
                frames.append(f"data: {json.dumps(self._view(n))}\n\n")
            if frames:
                await self.repo.commit()
            return frames

        # initial backlog
        for frame in await _emit_new():
            yield frame
        # live loop
        while True:
            await asyncio.sleep(interval)
            new_frames = await _emit_new()
            if new_frames:
                for frame in new_frames:
                    yield frame
            else:
                yield ": keep-alive\n\n"          # comment frame keeps the socket warm

    # ══════════════════════════════════════════════════════════════════════
    # Escalate — the 2-hour sweep (§2c). Called by the lifespan sweeper.
    # ══════════════════════════════════════════════════════════════════════
    async def run_escalations(self) -> int:
        """Find in-app BOM-review notices past `scheduled_for` that are STILL unseen
        and not yet escalated; send an email child per row (idempotent). Returns the
        number of emails dispatched."""
        from app.modules.users.service import UserService

        now = datetime.now(timezone.utc)
        due = await self.repo.due_escalations(now)
        if not due:
            return 0
        notifier = get_notifier()
        users = UserService(self.db)
        count = 0
        for parent in due:
            recipient = await users.get(parent.recipient_user_id) if parent.recipient_user_id else None
            to = (getattr(recipient, "email", None)
                  or getattr(recipient, "phone", None)
                  or "unknown@leatherfactory.local")
            child = Notification(
                recipient_user_id=parent.recipient_user_id,
                channel=NotificationChannel.EMAIL.value,
                type=parent.type, subject=parent.subject, body=parent.body,
                entity_type=parent.entity_type, entity_id=parent.entity_id,
                status=NotificationStatus.PENDING.value,
                parent_notification_id=parent.id,
                scheduled_for=parent.scheduled_for,
            )
            ok = await run_in_threadpool(
                notifier.send, to=to,
                subject=f"[Action needed] {parent.subject}",
                body=(parent.body or "") +
                     "\n\n(You have not opened the in-app notification within "
                     f"{settings.bom_review_escalation_hours}h.)",
            )
            child.status = (NotificationStatus.SENT.value if ok
                            else NotificationStatus.FAILED.value)
            child.sent_at = now if ok else None
            self.db.add(child)
            count += 1
        await self.db.commit()
        return count

    # ── view ──────────────────────────────────────────────────────────────
    @staticmethod
    def _view(n: Notification) -> dict:
        return {
            "id": str(n.id),
            "type": n.type,
            "channel": n.channel,
            "subject": n.subject,
            "body": n.body,
            "entity_type": n.entity_type,
            "entity_id": str(n.entity_id) if n.entity_id else None,
            "status": n.status,
            "scheduled_for": n.scheduled_for.isoformat() if n.scheduled_for else None,
            "sent_at": n.sent_at.isoformat() if n.sent_at else None,
            "opened_at": n.opened_at.isoformat() if n.opened_at else None,
        }
