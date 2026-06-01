"""Order hierarchy: Client -> PurchaseOrder -> Style -> SKU.

The SKU (style + colour + size) is the atomic unit everything else joins to.
"""
import uuid
from datetime import date
from sqlalchemy import String, ForeignKey, Integer, Date, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.core.models import UUIDMixin, TimestampMixin, GUID


class Client(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "client"
    name: Mapped[str] = mapped_column(String(200), index=True)
    country: Mapped[str | None] = mapped_column(String(100))
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class PurchaseOrder(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "purchase_order"
    client_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("client.id"), index=True)
    po_number: Mapped[str] = mapped_column(String(50), index=True)
    order_date: Mapped[date | None] = mapped_column(Date)
    delivery_deadline: Mapped[date | None] = mapped_column(Date)
    sea_cutoff_date: Mapped[date | None] = mapped_column(Date)  # last day to make sea freight
    ship_mode: Mapped[str] = mapped_column(String(10), default="sea")  # sea | air
    client: Mapped["Client"] = relationship(back_populates="purchase_orders")
    styles: Mapped[list["Style"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )


class Style(Base, UUIDMixin, TimestampMixin):
    """One model, e.g. CARNABY. A client/PO has many styles."""
    __tablename__ = "style"
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("purchase_order.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), index=True)
    gender: Mapped[str | None] = mapped_column(String(20))
    label: Mapped[str | None] = mapped_column(String(80))
    article: Mapped[str | None] = mapped_column(String(80))
    thickness: Mapped[str | None] = mapped_column(String(40))
    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="styles")
    skus: Mapped[list["SKU"]] = relationship(
        back_populates="style", cascade="all, delete-orphan"
    )


class SKU(Base, UUIDMixin, TimestampMixin):
    """A specific style, in a specific colour, in a specific size.

    This is the unit production events are logged against.
    """
    __tablename__ = "sku"
    __table_args__ = (
        UniqueConstraint("style_id", "color_code", "size", name="uq_sku_identity"),
    )
    style_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("style.id"), index=True)
    color_code: Mapped[str] = mapped_column(String(40))     # "57", "J24"
    color_name: Mapped[str | None] = mapped_column(String(80))  # "PINE GREEN"
    size: Mapped[str] = mapped_column(String(10))           # "M", "42" — string by design
    qty_ordered: Mapped[int] = mapped_column(Integer, default=0)
    style: Mapped["Style"] = relationship(back_populates="skus")
