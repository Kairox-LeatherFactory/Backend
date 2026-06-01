"""
================================================================================
modules/users/repository.py — Async data access for the users table
================================================================================
The ONLY layer that touches the User table directly. Every method is async and
takes an AsyncSession. Lookups are by phone (login), email (alternate), or id.
================================================================================
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import User


class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.db.get(User, user_id)

    async def get_by_phone(self, phone: str) -> User | None:
        res = await self.db.execute(select(User).where(User.phone == phone))
        return res.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        res = await self.db.execute(select(User).where(User.email == email))
        return res.scalar_one_or_none()

    async def list_all(self, active_only: bool = True) -> list[User]:
        stmt = select(User).order_by(User.name)
        if active_only:
            stmt = stmt.where(User.is_active.is_(True))
        res = await self.db.execute(stmt)
        return list(res.scalars())

    async def create(self, **kw) -> User:
        user = User(**kw)
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def save(self, user: User) -> User:
        await self.db.commit()
        await self.db.refresh(user)
        return user
