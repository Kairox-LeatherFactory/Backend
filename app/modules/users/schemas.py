"""
================================================================================
modules/users/schemas.py — API contract for auth + user management
================================================================================
These Pydantic shapes ARE the contract the frontend mocks against. They never
expose password_hash. Login returns a bearer token; user objects are read-only
projections of the User model.
================================================================================
"""
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import UserRole


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: UserRole
    name: str
    user_id: uuid.UUID
    must_change_password: bool = False


class LoginRequest(BaseModel):
    """JSON login (phone + password). The form-based OAuth2 flow is also
    supported on the same endpoint for Swagger's Authorize button."""
    username: str
    password: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    phone: str
    email: str | None
    role: UserRole
    is_active: bool
    employee_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None


class UserCreate(BaseModel):
    """Direct manager creates a staff/manager/viewer login.

    NO `employee_id`. An EMPLOYEE IS NOT A USER — a shop-floor worker has no
    login at all (CLAUDE.md §3), so a caller of POST /users has nothing to point
    this at, and the field only ever invited the all-zero UUID that was being
    sent to satisfy it. The link still exists in the DATABASE (app_user.
    employee_id) because a STAFF login — HR, a manager, security — does own an
    employee row for their own attendance and wages. That link is made by the
    system on the /employees path, never typed into this request.
    """
    name: str
    phone: str
    email: str | None = None
    role: UserRole = UserRole.VIEWER
    password: str       # defaults to the phone number if omitted


class StaffUserCreate(UserCreate):
    """INTERNAL ONLY — not a request body on any route.

    employees.create() mints a staff login in the same transaction as the
    employee row and must bind the two, so it needs the one field UserCreate
    deliberately no longer exposes. Keeping it on a subclass means the public
    contract cannot grow it back by accident.
    """
    employee_id: uuid.UUID | None = None


class ClientUserCreate(BaseModel):
    """Direct manager provisions a CLIENT login bound to an existing client."""
    name: str
    phone: str
    email: str | None = None
    client_id: uuid.UUID
    password: str         # defaults to the phone number if omitted


class PasswordChange(BaseModel):
    current_password: str
    # F43: a minimum length so the forced-change flow can't replace a guessable
    # password with a weaker one. (Equality-with-phone is additionally checked in
    # the service, which has the user's phone on hand.)
    new_password: str = Field(..., min_length=8)