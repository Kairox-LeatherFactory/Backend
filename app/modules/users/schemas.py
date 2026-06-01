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

from pydantic import BaseModel, ConfigDict

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
    """Direct manager creates a staff/manager/viewer login."""
    name: str
    phone: str
    email: str | None = None
    role: UserRole = UserRole.VIEWER
    password: str | None = None          # defaults to the phone number if omitted
    employee_id: uuid.UUID | None = None


class ClientUserCreate(BaseModel):
    """Direct manager provisions a CLIENT login bound to an existing client."""
    name: str
    phone: str
    email: str | None = None
    client_id: uuid.UUID
    password: str | None = None          # defaults to the phone number if omitted


class PasswordChange(BaseModel):
    current_password: str
    new_password: str
