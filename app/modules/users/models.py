"""
================================================================================
modules/users/models.py — The single, centralised User table for ALL logins
================================================================================

PURPOSE
    ONE table authenticates everyone — direct manager, cutting/stitching
    managers, shop-floor employees, clients, and read-only viewers. Authorisation
    is then a matter of the `role` column. This is the "centralise all users into
    one table" requirement: one place to log in, role-based separation after.

WHY ONE TABLE (not one per role)
    Authentication logic lives in exactly one place. Adding a role never means a
    new login flow or a new table — just a new UserRole value. JOINs that need
    "who did this" point at one foreign key.

IDENTITY & CREDENTIALS
    phone  : the LOGIN identifier (unique, indexed). Users type this to log in.
    email  : optional, also unique-indexed, available as an alternate lookup.
    name   : display name (shown in the UI / token).
    password_hash : bcrypt hash. For seeded v1 users this is hash(phone).
    must_change_password : True for seeded users so a future "reset on first
                           login" flow can be switched on with no migration.

LINKAGES (role-specific detail without extra login tables)
    employee_id : if this user IS a shop-floor worker, points at employees.id
                  (their wage/production identity). NULL for managers/clients.
    client_id   : if this user is an external CLIENT login, points at client.id
                  so the app can scope them to only their own orders. The manager
                  creates these client logins (the "manager creates client user
                  id and password" requirement).
================================================================================
"""
import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.enums import UserRole
from app.core.models import GUID, TimestampMixin, UUIDMixin


class User(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "app_user"

    name: Mapped[str] = mapped_column(String(120), index=True)
    phone: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(160), unique=True, index=True)

    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.VIEWER, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)

    # Optional links to the role-specific record this login represents.
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("employee.id"), nullable=True, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client.id"), nullable=True, index=True
    )
